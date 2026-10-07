"""Local React desktop host. Credentials never leave the account settings POST."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
import hmac
import hashlib
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit, quote
import webbrowser

from . import cloud_settings
from .cloud_gui import available_actions, build_command, siliconflow_ready, start_process
from .gui import default_project, project_config_from_state
from .file_layout import timestamped_project, subtitle_filename, save_subtitle_snapshot
from .export_progress import sanitize_export_progress
from .languages import default_target, output_names, manifest_languages, video_output_path
from .read_cache import SrtReadCache
from .studio_http import StudioHTTPServer as ThreadingHTTPServer, static_file_path
from .studio_origin import create_server
from .runner import PipelineConfig, ProjectLock, atomic_json, engine_paths, pid_alive, run_pipeline, validate_config
from .subtitles import parse_srt
from .windows_job import owned_process, wait_for_process_exit

ROOT=Path(__file__).resolve().parents[1]
PROVIDERS={'siliconflow':'SILICONFLOW_API_KEY','bailian':'DASHSCOPE_API_KEY','deepseek':'DEEPSEEK_API_KEY'}
OUTPUTS=('原文.srt','中文草稿.srt','双语草稿.srt')
SETTINGS={'asr_endpoint':'QWEN_ASR_ENDPOINT','asr_input_rate':'QWEN_ASR_INPUT_CNY_PER_MILLION',
          'asr_output_rate':'QWEN_ASR_OUTPUT_CNY_PER_MILLION','deepseek_input_rate':'DEEPSEEK_INPUT_CNY_PER_MILLION',
          'deepseek_output_rate':'DEEPSEEK_OUTPUT_CNY_PER_MILLION','verified_on':'CLOUD_PRICING_VERIFIED_ON',
          'pricing_reference':'CLOUD_PRICING_REFERENCE'}
DEFAULT_SETTINGS={'asr_endpoint':'https://dashscope.aliyuncs.com/api/v1','asr_input_rate':.8,
                  'asr_output_rate':2.7,'deepseek_input_rate':2,'deepseek_output_rate':8,
                  'verified_on':'','pricing_reference':''}


class ProjectSelectionChanged(ValueError):
    """A different browser selected another project after this result loaded."""


def data_directory():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'SubtitlePipeline'


def read_json(path):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def account_environment():
    return cloud_settings.read_environment((*cloud_settings.ENV_NAMES,*cloud_settings.SILICONFLOW_ENV_NAMES))


def encrypted_names():
    from .credential_store import read_saved_secrets
    return set(read_saved_secrets())


def save_secrets(values):
    from .credential_store import save_secrets as save
    save(values)


def write_public_environment(values):
    from .cloud_gui import _write_environment
    allowed=set(SETTINGS.values())|{'SILICONFLOW_ASR_CNY_PER_SECOND','SILICONFLOW_PRICING_VERIFIED_ON','SILICONFLOW_PRICING_REFERENCE'}
    if not set(values)<=allowed:raise ValueError('不支持的公开配置字段')
    _write_environment(values)


def engine_available():
    try:return all(path.is_file() for path in engine_paths())
    except (OSError,ValueError,RuntimeError):return False


def public_settings(values,*,errors=None):
    result=dict(DEFAULT_SETTINGS)
    for label,name in SETTINGS.items():
        if values.get(name):
            try:
                value=float(values[name]) if label.endswith('_rate') else str(values[name])
                if label.endswith('_rate') and (not math.isfinite(value) or value<=0):raise ValueError()
                result[label]=value
            except (ValueError,TypeError,OverflowError):
                if errors is not None and not errors:
                    errors.append('本机计价配置包含无效数值，请重新核实价格并保存。')
    return result


def authorize_request(headers,token,host):
    if headers.get('Host')!=host:return False
    if headers.get('Origin') not in (None,'http://'+host):return False
    if headers.get('Sec-Fetch-Site')=='cross-site':return False
    supplied=headers.get('X-Subtitle-Token','')
    if not supplied:
        try:
            cookie=SimpleCookie(headers.get('Cookie',''))
            name='subtitle_session_'+host.rsplit(':',1)[-1]
            supplied=cookie[name].value if name in cookie else ''
        except Exception:return False
    return isinstance(supplied,str) and bool(supplied) and supplied.isascii() and hmac.compare_digest(supplied,token)


def byte_range(header,size):
    if size<=0:raise ValueError('文件为空')
    if not header:return 0,size-1
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
    if not match or not any(match.groups()):raise ValueError('无效的媒体范围')
    left,right=match.groups()
    if not left:
        amount=int(right)
        if amount<=0:raise ValueError('无效的媒体范围')
        return max(0,size-amount),size-1
    start=int(left);end=min(int(right),size-1) if right else size-1
    if start>=size or end<start:raise ValueError('无效的媒体范围')
    return start,end


class StudioController:
    def __init__(self,*,source='',campaign='',baseline='',state_path=None):
        self.lock=threading.RLock()
        self._selection_revision=0
        self._track_cache=SrtReadCache()
        self._video_checks=(threading.Lock(),{})
        self.state_path=Path(state_path) if state_path else data_directory()/'studio.json'
        saved=read_json(self.state_path)
        self.source=str(source or saved.get('source',''))
        self.campaign=str(campaign or saved.get('campaign',''))
        self.baseline=str(baseline or saved.get('baseline',''))
        recent=saved.get('recent',[])
        self.recent=[x for x in recent if isinstance(x,dict) and isinstance(x.get('path'),str)][:12] if isinstance(recent,list) else []
        self.stop_event=threading.Event()
        self.worker=None
        self._redactions=[]
        self._cancel_marker=None
        self.persistence_warning=''
        self._last_saved_progress=0.0
        self._persist_active=False
        self._persist_pending=None
        self._persist_revision=0
        self._persist_result_revision=0
        summaries=saved.get('last_runs',{})
        self._last_runs={}
        if isinstance(summaries,dict):
            for identity,summary in list(summaries.items())[-12:]:
                if isinstance(identity,str) and re.fullmatch(r'[0-9a-f]{64}',identity):
                    cleaned=self._run_summary(summary)
                    if cleaned:self._last_runs[identity]=cleaned
        self.job=self._restore_job()

    @staticmethod
    def _idle_job():
        return {'busy':False,'action':'','status':'idle','message':'选择素材，开始一个字幕任务',
                'total':0,'recognized':0,'translated':0,'logs':[]}

    def _scrub(self,value,limit=800):
        text=value if isinstance(value,str) else ''
        for secret in self._redactions:
            if secret:text=text.replace(secret,'[已隐藏]')
        text=re.sub(r'(?i)\bsk-[a-z0-9_-]{8,}', '[已隐藏]',text)
        text=re.sub(r'(?i)((?:authorization|api[_ -]?key)\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+',r'\1[已隐藏]',text)
        return text[:limit]

    def _run_summary(self,value):
        if not isinstance(value,dict) or value.get('action') not in (
                'local','prepare','samples','siliconflow-pilot','approve','full','accept-final','export','export-draft'):
            return None
        status=value.get('status','needs_attention')
        if not isinstance(status,str) or not re.fullmatch(r'[a-z_]{1,40}',status):status='needs_attention'
        logs=value.get('logs',[])
        return {'action':value['action'],'status':status,'busy':value.get('busy') is True,
                'message':self._scrub(value.get('message')),
                **{name:value.get(name,0) if type(value.get(name,0)) is int and 0<=value.get(name,0)<=10_000_000 else 0
                   for name in ('total','recognized','translated')},
                'logs':[self._scrub(line,500) for line in logs[-30:] if isinstance(line,str)] if isinstance(logs,list) else [],
                'interrupted':value.get('interrupted') is True}

    def _remember_job(self):
        summary=self._run_summary(self.job)
        if not summary or not self.campaign:return
        identity=self._project_id()
        self._last_runs.pop(identity,None)
        self._last_runs[identity]=summary
        self._last_runs=dict(list(self._last_runs.items())[-12:])

    def _restore_job(self):
        job=self._idle_job()
        try:summary=self._last_runs.get(self._project_id())
        except (ValueError,OSError):return job
        if not summary:return job
        job.update(summary,restored=True,busy=False)
        if summary['busy'] or summary['status'] in ('starting','running','preparing'):
            job.update(status='needs_attention',interrupted=True,
                       message='上次运行中断，已保存结果保留；请检查后继续，未自动重新提交请求。')
        elif summary['status'] in ('complete','completed','done','prepared','samples_ready','full_ready',
                                   'approved','final_reviewed','exported','text_audition_ready'):
            try:
                project=self._project_path()
                evidence=[self._saved_state(),read_json(self._contained_path(project,'campaign.json')),
                          read_json(self._contained_path(project,'硅基流动试听状态.json'))]
                evidenced=any(item.get('status')==summary['status'] for item in evidence)
            except (ValueError,OSError,RuntimeError):evidenced=False
            if not evidenced:
                job.update(status='needs_attention',
                           message='上次运行已有结束记录，但当前项目缺少对应结果状态，请检查项目文件后继续。')
        return job

    def _project_path(self):
        if not self.campaign:raise ValueError('请先选择输出项目')
        return Path(self.campaign).resolve()

    def _project_id(self):
        paths=[os.path.normcase(str(Path(value).resolve())) if value else ''
               for value in (self.source,self.campaign)]
        return hashlib.sha256(json.dumps(paths,ensure_ascii=False).encode('utf-8')).hexdigest()

    def _assert_project(self,project_id):
        if project_id is not None and (not isinstance(project_id,str) or
                not re.fullmatch(r'[0-9a-f]{64}',project_id) or
                not hmac.compare_digest(project_id,self._project_id())):
            raise ProjectSelectionChanged('其他窗口已切换项目，请刷新当前结果后再操作')

    def _read_view(self,reader,project_id=None,finish=None):
        # File reads and parsing must not hold the live controller lock needed
        # by stop/progress. Only immutable paths and isolated display data cross
        # into this detached view; it has no worker, stop event, or credentials.
        with self.lock:
            self._assert_project(project_id)
            revision=self._selection_revision
            view=object.__new__(type(self))
            view.lock=threading.RLock()
            view.source=self.source
            view.campaign=self.campaign
            view.baseline=self.baseline
            view.job=deepcopy(self.job)
            view.recent=deepcopy(self.recent)
            view.persistence_warning=self.persistence_warning
            view._track_cache=self._track_cache
            view._video_checks=self._video_checks
        succeeded=False
        try:
            result=reader(view)
            succeeded=True
        finally:
            with self.lock:
                if revision!=self._selection_revision:
                    raise ProjectSelectionChanged('其他窗口已切换项目，请刷新当前结果后再操作')
                if succeeded and finish is not None:
                    result=finish(view,result)
        return result

    def _output_dir(self):
        project=self._project_path()
        full=self._contained_path(project,'整片')
        return full if self._contained_path(full,'state.json').is_file() else project

    @staticmethod
    def _contained_path(parent,name):
        parent=Path(parent).resolve()
        child=(parent/name).resolve()
        if not child.is_relative_to(parent):raise ValueError('结果路径不在所选项目内')
        return child

    def _saved_state(self):
        if not self.campaign:return {}
        return read_json(self._contained_path(self._output_dir(),'state.json'))

    def _is_cloud_project(self,previous=None):
        if not self.campaign:return False
        if self._contained_path(self._project_path(),'campaign.json').is_file():return True
        previous=self._saved_state() if previous is None else previous
        provider=previous.get('config',{}).get('asr_provider') or previous.get('identity',{}).get('engine')
        return bool(provider and provider!='whisper_cpp')

    def _budget_summary(self,config,manifest):
        """Read a validated ledger without reconciliation, locks, or raw records."""
        if not self.campaign or not self.source:return None
        try:
            source=Path(self.source).resolve()
            if manifest.get('asr_provider')=='qwen_asr' and Path(manifest.get('source',{}).get('path','')).resolve()==source:
                from .cloud_workflow import budget_ledger_path
                ledger_path=(budget_ledger_path(self._project_path(),manifest) if 'budget_ledger' in manifest
                             else self._contained_path(self._project_path(),'费用账本.json'))
                budget=manifest.get('budget_cny',20);stop=manifest.get('stop_cny',18);scope='shared'
            elif (config.get('asr_provider')=='qwen_asr'
                  and Path(config.get('source','')).resolve()==source
                  and Path(config.get('project','')).resolve()==self._output_dir()):
                ledger_path=Path(config.get('budget_ledger') or self._output_dir()/'费用账本.json').resolve()
                budget=config.get('budget_cny',20);stop=min(18,budget-2)
                scope='project' if ledger_path.parent==self._output_dir() else 'shared'
            else:return None
            if not ledger_path.is_file():return None
            from .cloud_budget import BudgetLedger
            ledger=BudgetLedger(ledger_path,budget_cny=budget,stop_cny=stop)
            # summary() also reconciles interrupted requests. UI polling must
            # never alter request state, so reuse only validation and totals.
            data=ledger._load();spent,reserved=ledger._totals(data)
            return {'budget_cny':float(ledger.budget),'stop_cny':float(ledger.stop),
                    'spent_cny':float(spent),'reserved_cny':float(reserved),
                    'committed_cny':float(spent+reserved),
                    'remaining_cny':float(max(0,ledger.budget-spent-reserved)),
                    'scope':scope,'estimated':True}
        except (ValueError,OSError,RuntimeError,TypeError,OverflowError):return None

    def _persist_result_locked(self,revision,failed):
        if revision<self._persist_result_revision:return
        self._persist_result_revision=revision
        self.persistence_warning=('界面运行记录暂时无法保存；当前任务继续执行，请以项目目录中的处理结果为准。'
                                  if failed else '')

    def _queue_persist_locked(self,status_hint=None):
        self._persist_revision+=1
        revision=self._persist_revision
        try:
            self._remember_job()
            if self.campaign:
                status=self.job['status'] if self.job['action'] else status_hint or self.job['status']
                entry={'path':self.campaign,'title':Path(self.source).stem if self.source else Path(self.campaign).name,
                       'source':self.source,'baseline':self.baseline,'status':status,'updated':time.time()}
                previous=next((x for x in self.recent if x['path']==self.campaign),{})
                if previous.get('created') is not None:entry['created']=previous['created']
                self.recent=[entry]+[x for x in self.recent if x['path']!=self.campaign][:11]
            snapshot=deepcopy({'source':self.source,'campaign':self.campaign,'baseline':self.baseline,
                               'recent':self.recent,'last_runs':self._last_runs})
            self._persist_pending=(revision,snapshot)
        except (OSError,ValueError,RuntimeError,TypeError):
            self._persist_result_locked(revision,True)
        self._last_saved_progress=time.monotonic()

    def _drain_persistence(self):
        with self.lock:
            if self._persist_active or self._persist_pending is None:return
            self._persist_active=True
        try:
            while True:
                with self.lock:
                    pending=self._persist_pending
                    if pending is None:
                        # Release ownership in the same critical section as the
                        # empty check, so the next enqueue cannot be stranded.
                        self._persist_active=False
                        return
                    self._persist_pending=None
                revision,snapshot=pending
                failed=False
                try:
                    atomic_json(self.state_path,snapshot)
                except (OSError,ValueError,RuntimeError,TypeError):
                    failed=True
                with self.lock:
                    self._persist_result_locked(revision,failed)
        except BaseException:
            with self.lock:self._persist_active=False
            raise

    def _persist(self):
        # Compatibility entry point for direct callers. Lifecycle mutations
        # enqueue while already locked and drain only after releasing that lock.
        with self.lock:
            selection=self._selection_revision
            idle=bool(self.campaign) and not self.job['action']
        status_hint=None
        if idle:
            try:
                status_hint=self._read_view(lambda view:
                    read_json(view._project_path()/'campaign.json').get('status')
                    or view._saved_state().get('status',view.job['status']))
            except ProjectSelectionChanged:
                self._drain_persistence()
                return
            except (OSError,ValueError,RuntimeError,TypeError):
                with self.lock:
                    if selection==self._selection_revision:
                        self._persist_revision+=1
                        self._persist_result_locked(self._persist_revision,True)
                self._drain_persistence()
                return
        with self.lock:
            if selection==self._selection_revision:self._queue_persist_locked(status_hint)
        self._drain_persistence()

    def idle_ready(self):
        with self.lock:
            return not self.job['busy'] and not self._persist_active and self._persist_pending is None

    def _recent_state(self):
        result=[]
        for item in self.recent:
            try:missing=not Path(item['path']).is_dir()
            except (OSError,ValueError):missing=True
            result.append({**item,'missing':missing})
        return result

    def progress(self):
        """Frequent progress updates only copy owned memory, never project files."""
        with self.lock:
            return {'project_id':self._project_id(),'source':self.source,'campaign':self.campaign,
                    'job':deepcopy(self.job),'persistence_warning':self.persistence_warning}

    def state(self):
        # State polling has no project token. Retry one changed selection so a
        # normal project switch does not appear to the UI as a lost connection.
        for attempt in range(2):
            try:
                return self._read_view(lambda view:view._state_data(),finish=self._refresh_state_job)
            except ProjectSelectionChanged:
                if attempt:
                    raise

    def _refresh_state_job(self,view,result):
        # Called under the live lock after I/O. A run may have started, stopped,
        # or progressed meanwhile; return its current job without reading files
        # again or allowing stale busy-dependent actions through to the UI.
        if self.job!=view.job:
            job=deepcopy(self.job)
            accounts=result['accounts']
            actions=available_actions(result['campaign_status'],approval_exists=result['approval_exists'],
                ready=accounts['bailian']['ready'],busy=job['busy'],
                siliconflow_ready=accounts['siliconflow']['ready'],
                siliconflow_result_exists=result['siliconflow_result_exists'])
            actions['local']=not job['busy'] and view._local_action_allowed
            result.update(job=job,actions=actions)
        result['persistence_warning']=self.persistence_warning
        return result

    def _state_data(self):
        with self.lock:
            error=''
            try:values=account_environment();saved=encrypted_names()
            except (ValueError,OSError,RuntimeError):
                values={};saved=set();error='本机密钥存储无法读取，请检查当前 Windows 用户或重新配置。'
            try:cloud_settings.load_settings(values);ready=True
            except (ValueError,RuntimeError):ready=False
            setting_errors=[]
            settings=public_settings(values,errors=setting_errors)
            if setting_errors and not error:error=setting_errors[0]
            accounts={provider:{'configured':bool(values.get(name)),'storage':'encrypted' if name in saved else 'environment' if values.get(name) else 'none',
                                 'ready':bool(values.get(name))}
                      for provider,name in PROVIDERS.items()}
            accounts['siliconflow'].update(ready=siliconflow_ready(values),
                verified_on=values.get('SILICONFLOW_PRICING_VERIFIED_ON',''),
                price_per_second=values.get('SILICONFLOW_ASR_CNY_PER_SECOND') or '0.000220',
                pricing_reference=values.get('SILICONFLOW_PRICING_REFERENCE',''))
            accounts['bailian']['ready']=ready
            manifest=read_json(self._project_path()/'campaign.json') if self.campaign else {}
            stage=manifest.get('status','')
            manual_status=self._manual_status(manifest)
            if stage in ('final_reviewed','exported') and manual_status.get('invalidates_approval'):
                stage='full_ready'
            approval=bool(self.campaign and (self._project_path()/'approval.json').is_file())
            sf_result=bool(self.campaign and (self._project_path()/'硅基流动识别试听.html').is_file())
            job=json.loads(json.dumps(self.job))
            previous=self._saved_state()
            if not job['action'] and previous:
                for name in ('status','message','total','recognized','translated'):
                    if name in previous:job[name]=previous[name]
            actions=available_actions(stage,approval_exists=approval,ready=ready,busy=job['busy'],
                                      siliconflow_ready=accounts['siliconflow']['ready'],siliconflow_result_exists=sf_result)
            local=engine_available()
            self._local_action_allowed=local and not self._is_cloud_project(previous)
            actions['local']=not job['busy'] and self._local_action_allowed
            config=previous.get('config',{})
            project_config={k:config[k] for k in ('language','target','chunk_seconds','workers','threads','translate','asr_provider') if k in config}
            if manifest:
                language,target=manifest_languages(manifest)
                project_config.update(language=language,target=target,translate=True)
            elif config:
                project_config.setdefault('language','ja');project_config.setdefault('target','zh-CN')
            provider=manifest.get('asr_provider') or config.get('asr_provider')
            if not provider and config:provider='whisper_cpp'
            if provider:
                project_config['asr_provider']=provider
                project_config['engine']={'qwen_asr':'bailian','whisper_cpp':'local','siliconflow':'siliconflow'}.get(provider,'')
            if config and provider=='whisper_cpp':
                project_config['model']='small'
            return {'app':'字幕工坊','source':self.source,'campaign':self.campaign,'baseline':self.baseline,
                    'project_id':self._project_id(),
                    'language_options':{'sources':['ja','en','zh'],'targets':['zh-CN','en','ja']},
                    'job':job,'accounts':accounts,'settings':settings,'settings_error':error,
                    'recent':self._recent_state(),'local_available':local,'actions':actions,'campaign_status':stage,
                    'approval_exists':approval,'siliconflow_result_exists':sf_result,
                    'project_config':project_config,'budget':self._budget_summary(config,manifest),
                    'persistence_warning':self.persistence_warning,
                    'manual_review':manual_status,
                    'file_layout':self._file_layout_data(previous)}

    def _manual_status(self,manifest):
        result={'supported':True}
        if not self.campaign:return result
        approval=read_json(self._contained_path(self._project_path(),'final-review.json'))
        if not (self._output_dir()/'人工校对'/'校对记录.json').exists():
            if approval.get('manual_revision'):
                result.update(conflict='已验收的人工校对记录缺失，请恢复后重新核对。',invalidates_approval=True)
            return result
        try:
            from .manual_review import load_review
            language,target=manifest_languages(manifest) if manifest else self._selection_languages(self._preview_selections()[0])
            review=load_review(self._output_dir(),language=language,target=target)
            result.update(revision=review['revision'],summary=review['summary'])
            result['invalidates_approval']=approval.get('manual_revision')!=review['revision']
        except (ValueError,OSError,RuntimeError):
            result.update(conflict='原始结果或校对记录已变化，请在校对页核对。',invalidates_approval=True)
        return result

    def _missing_approved_review(self,folder):
        if not self.campaign or folder!=self._project_path()/'整片':return False
        approval=read_json(self._contained_path(self._project_path(),'final-review.json'))
        return bool(approval.get('manual_revision') and
                    not self._contained_path(self._contained_path(folder,'人工校对'),'校对记录.json').exists())

    def _file_layout_data(self,previous=None):
        result={'source_path':self.source,'project_path':self.campaign,
                'exports_path':'','created_at':'','video_path':''}
        if not self.campaign:return result
        project=self._project_path()
        result['exports_path']=str(self._contained_path(project,'导出'))
        recent=next((x for x in self.recent if x['path']==self.campaign),{})
        previous=self._saved_state() if previous is None else previous
        created=recent.get('created') or previous.get('created')
        if type(created) in (int,float) and math.isfinite(created) and created>0:
            try:result['created_at']=datetime.fromtimestamp(created).astimezone().isoformat(timespec='seconds')
            except (ValueError,OSError,OverflowError):pass
        video=self._exported_video_path()
        if video:result['video_path']=str(video)
        return result

    @staticmethod
    def _validate_loaded_project(path,old,manifest):
        if not path.is_dir():raise ValueError('项目目录不存在或已移动，请重新选择字幕项目目录')
        if 'config' in old:
            try:
                config=project_config_from_state(old)
                raw=old['config']
                if (not isinstance(raw.get('source'),str) or not Path(raw['source']).is_absolute()
                    or '\0' in raw['source'] or len(raw['source'])>4096
                    or not isinstance(raw.get('project'),str) or not Path(raw['project']).is_absolute()
                    or '\0' in raw['project'] or config['project']!=path.resolve()):raise ValueError()
                return
            except (ValueError,TypeError,OSError):
                raise ValueError('字幕项目配置已损坏或目录不匹配，请选择正确的项目目录') from None
        source=manifest.get('source')
        source_path=source.get('path') if isinstance(source,dict) else None
        if (isinstance(source_path,str) and source_path.strip() and '\0' not in source_path
            and len(source_path)<=4096 and Path(source_path).is_absolute()
            and manifest.get('asr_provider') in ('qwen_asr','azure_fast','siliconflow')
            and (manifest.get('baseline') is None or isinstance(manifest.get('baseline'),str))
            and isinstance(manifest.get('status'),str)):
            return
        raise ValueError('此目录不是可识别的字幕项目，或项目配置已损坏；新任务请使用“新建任务”')

    def select_project(self,data):
        with self.lock:
            if self.job['busy']:raise ValueError('请先停止当前任务，再切换项目')
            fresh=data.get('new_task',False)
            if type(fresh) is not bool:raise ValueError('新建任务选项必须为布尔值')
            values={}
            status_hint=None
            for name in ('source','campaign','baseline'):
                if name in data:
                    value=data[name]
                    if not isinstance(value,str) or len(value)>4096 or '\0' in value:raise ValueError('文件路径无效')
                    values[name]=str(Path(value).expanduser().resolve()) if value.strip() else ''
            if fresh:
                if not values.get('source'):raise ValueError('新建任务请先选择素材')
                values.setdefault('baseline','')
                if values.get('campaign'):
                    selected=Path(values['campaign'])
                    if selected.exists() and (not selected.is_dir() or any(
                            p.name!='run.guard.lock' or not p.is_file() or p.is_symlink() for p in selected.iterdir())):
                        raise ValueError('新建任务需要空项目目录；若要使用已有结果，请选择载入任务')
                else:
                    base=timestamped_project(Path(values['source']),ROOT/'字幕任务')
                    selected=base;index=2
                    reserved={x['path'] for x in self.recent}
                    while selected.exists() or selected.is_symlink() or str(selected) in reserved:
                        selected=base.with_name(f'{base.name}-{index:02d}');index+=1
                    values['campaign']=str(selected)
            if values.get('campaign'):
                path=Path(values['campaign'])
                old=read_json(self._contained_path(path,'state.json'))
                manifest=read_json(self._contained_path(path,'campaign.json'))
                status_hint=manifest.get('status') or old.get('status')
                if not fresh and 'source' not in data:self._validate_loaded_project(path,old,manifest)
                stored_source=old.get('config',{}).get('source') or manifest.get('source',{}).get('path')
                if stored_source and values.get('source') and Path(stored_source).resolve()!=Path(values['source']).resolve():
                    raise ValueError('这个项目已属于另一个素材，请为新素材选择新的输出项目')
                if old.get('config'):
                    config=project_config_from_state(old)
                    values['source']=str(config['source'])
                elif manifest.get('source',{}).get('path'):
                    values.update(source=manifest['source']['path'],baseline=manifest.get('baseline') or '')
                else:
                    recent=next((x for x in self.recent if x['path']==str(path)),{})
                    if 'source' not in values and recent.get('source'):values['source']=recent['source']
            elif not fresh and 'campaign' in data and 'source' not in data:
                raise ValueError('请先选择要载入的字幕项目目录')
            self._remember_job()
            for name,value in values.items():setattr(self,name,value)
            if self.source and not self.campaign:self.campaign=str(default_project(Path(self.source),ROOT/'字幕任务'))
            if fresh:
                # Keep metadata out of the new directory: both initializers
                # deliberately require an empty destination before processing.
                self.recent=[{'path':self.campaign,'created':time.time()}]+[
                    x for x in self.recent if x['path']!=self.campaign][:11]
            self._selection_revision+=1
            selection=self._selection_revision
            self.job=self._restore_job()
            self._queue_persist_locked(status_hint)
        self._drain_persistence()
        with self.lock:
            if selection!=self._selection_revision:
                raise ProjectSelectionChanged('其他窗口已切换项目，请刷新当前结果后再操作')
        result=self.state()
        with self.lock:
            if selection!=self._selection_revision:
                raise ProjectSelectionChanged('其他窗口已切换项目，请刷新当前结果后再操作')
        return result

    def save_credentials(self,data):
        with self.lock:
            if self.job['busy']:raise ValueError('请在当前任务停止后修改 API Key')
            if 'free_confirmed' in data:raise ValueError('免费资格确认已停用，请刷新界面并保存当前账户的实际价格')
            provider=data.get('provider')
            if provider not in PROVIDERS:raise ValueError('请选择支持的服务')
            key=data.get('key','')
            if not isinstance(key,str) or len(key)>8192 or any(ch.isspace() or ord(ch)<32 or ord(ch)==127 for ch in key):
                raise ValueError('API Key 格式无效，请检查空格和换行')
            if key:save_secrets({PROVIDERS[provider]:key})
            elif not account_environment().get(PROVIDERS[provider]):raise ValueError('请先填写 API Key')
            return {'saved':True,'provider':provider,'message':'已加密保存到本机，重启后自动读取；没有发起模型请求。'}

    def save_siliconflow_pricing(self,data):
        with self.lock:
            if self.job['busy']:raise ValueError('请在当前任务停止后修改计价配置')
            if data.get('confirmed') is not True:raise ValueError('请先核实当前账户价格并勾选确认')
            values={'SILICONFLOW_API_KEY':'validation-only',
                    'SILICONFLOW_ASR_CNY_PER_SECOND':data.get('price_per_second'),
                    'SILICONFLOW_PRICING_VERIFIED_ON':date.today().isoformat(),
                    'SILICONFLOW_PRICING_REFERENCE':data.get('pricing_reference','')}
            settings=cloud_settings.load_siliconflow_settings(values)
            write_public_environment({'SILICONFLOW_ASR_CNY_PER_SECOND':str(settings.price_per_second),
                'SILICONFLOW_PRICING_VERIFIED_ON':settings.verified_on,
                'SILICONFLOW_PRICING_REFERENCE':settings.pricing_reference})
            return {'saved':True,'settings':settings.public_config(),'message':'已保存当前账户价格；没有发起模型请求。'}

    def save_settings(self,data):
        with self.lock:
            if self.job['busy']:raise ValueError('请在当前任务停止后修改计价配置')
            if data.get('confirmed') is not True:raise ValueError('请先核实价格并勾选确认')
            values=account_environment()
            for field,name in SETTINGS.items():
                if field in data:values[name]=str(data[field])
            values['CLOUD_PRICING_VERIFIED_ON']=date.today().isoformat()
            # Validate pricing independently of whether credentials were saved.
            validation={**values,'DASHSCOPE_API_KEY':'validation-only','DEEPSEEK_API_KEY':'validation-only'}
            settings=cloud_settings.load_settings(validation)
            normalized=settings.public_config()
            write_public_environment({name:str(normalized[field]) for field,name in SETTINGS.items()})
            return {'saved':True,'settings':normalized,'message':'已保存当前核价记录；没有发起模型请求。'}

    def _progress_locked(self,data):
        previous_status=self.job['status']
        for name in ('status','total','recognized','translated'):
            if name in data:self.job[name]=data[name]
        if self.job['action'] in ('export','export-draft') and 'export_progress' in data:
            progress=sanitize_export_progress(data['export_progress'])
            if progress is not None:self.job['export_progress']=progress
        # Bound and redact the live response too, not just the persisted
        # summary at task completion. Redact before any text truncation.
        message=self._scrub(str(data.get('message','')))
        if 'message' in data:self.job['message']=message
        if message:
            displayed=message[:490]
            line=datetime.now().strftime('%H:%M:%S')+'  '+displayed
            if not self.job['logs'] or self.job['logs'][-1][10:]!=displayed:self.job['logs'].append(line)
            self.job['logs']=self.job['logs'][-150:]
        if self.job['action'] and (self._persist_active or self.job['status']!=previous_status
                or time.monotonic()-self._last_saved_progress>=1):
            self._queue_persist_locked()

    def _progress(self,data):
        with self.lock:self._progress_locked(data)
        self._drain_persistence()

    def start(self,data):
        launch_error=None
        with self.lock:
            self._assert_project(data.get('project_id'))
            if self.job['busy']:raise ValueError('已有任务正在处理')
            action=data.get('action')
            if action=='local' and self._is_cloud_project():
                raise ValueError('这个项目属于云端识别，请通过云端流程继续；本地识别请新建任务')
            state=self.state()
            if action not in state['actions'] or not state['actions'][action] or action in ('stop','review','siliconflow-review'):
                raise ValueError('当前步骤尚未满足条件，请先配置账户或完成前一步审核')
            project=self._project_path()
            if action in ('local','prepare','samples') and not Path(self.source).is_file():raise ValueError('请选择存在的视频或音频文件')
            values=account_environment()
            self._redactions=[values.get(name,'') for name in PROVIDERS.values()]
            if action=='local':
                old=read_json(project/'state.json')
                if old:
                    if old.get('identity',{}).get('engine')!='whisper_cpp':raise ValueError('这个项目属于云端识别，请通过原引擎继续')
                    config=PipelineConfig(**project_config_from_state(old))
                else:
                    config=PipelineConfig(Path(self.source),project,language=data.get('language','ja'),
                        target=data.get('target',default_target(data.get('language','ja'))),
                        chunk_seconds=data.get('chunk_seconds',300),workers=data.get('workers',1),translate=data.get('translate',False))
                    if config.language not in ('ja','en','zh','auto') or type(config.translate) is not bool or type(config.workers) is not int:
                        raise ValueError('识别选项无效')
                    if isinstance(config.chunk_seconds,bool) or not isinstance(config.chunk_seconds,(int,float)):
                        raise ValueError('分段时长无效')
                validate_config(config)
                payload=config
            else:
                payload=build_command(action,campaign=project,source=self.source,baseline=self.baseline,
                    **({'language':data.get('language',state['project_config'].get('language','ja')),
                        'target':data.get('target',state['project_config'].get('target',default_target(data.get('language','ja'))))} if action=='prepare' else {}),
                    reviewed=data.get('reviewed'),timing_passed=data.get('timing_passed'),content_passed=data.get('content_passed',False),
                    executable=sys.executable,
                    encoder=data.get('encoder') if action in ('export','export-draft') else None)
            self.stop_event=threading.Event()
            self._cancel_marker=(self.state_path.parent/'stops'/(secrets.token_hex(16)+'.stop')) if action!='local' else None
            self.job={**self._idle_job(),'busy':True,'action':action,'status':'starting','message':'正在准备任务…'}
            self._queue_persist_locked()
            try:
                self.worker=threading.Thread(target=self._execute,args=(action,payload),daemon=True)
                self.worker.start()
            except Exception:
                self.worker=None
                self._progress_locked({'status':'needs_attention','message':'后台任务未能启动，请稍后重试；没有提交新的模型请求。'})
                self._finish_job_locked()
                launch_error=self.job['message']
        self._drain_persistence()
        if launch_error is not None:raise RuntimeError(launch_error) from None
        return {'started':True,'action':action}

    def _execute(self,action,payload):
        child=None
        exit_confirmed=False
        try:
            if action=='local':
                result=run_pipeline(payload,self.stop_event,on_progress=self._progress)
                self._progress(result)
            else:
                if self.stop_event.is_set():
                    self._progress({'status':'cancelled','message':'任务已停止，未启动新的模型请求。'})
                    return
                child=start_process(payload,environ={**os.environ,'SUBTITLE_STUDIO_STOP_FILE':str(self._cancel_marker)})
                try:
                    with owned_process(child):
                        output_error=None
                        def stop_after_output_error(error):
                            nonlocal output_error
                            if output_error is None:output_error=error
                            try:self.stop()
                            except Exception:
                                # A failed marker write or progress callback must not
                                # release ownership of a child that is still running.
                                pass
                        try:
                            for line in child.stdout:
                                if output_error is not None:continue
                                try:data=json.loads(line)
                                except (ValueError,TypeError):continue
                                if isinstance(data,dict):
                                    try:self._progress(data)
                                    except Exception as error:
                                        stop_after_output_error(error)
                                        # Keep draining a healthy pipe after a callback
                                        # failure, otherwise the child may block writing.
                        except Exception as error:
                            stop_after_output_error(error)
                            try:child.stdout.close()
                            except (OSError,ValueError):pass
                        finally:
                            # An output error does not mean the child has exited. Keep
                            # the controller busy and its cooperative stop marker alive
                            # until the child is reaped, before allowing another run.
                            child.wait()
                        if output_error is not None:raise output_error
                finally:
                    primary=sys.exc_info()[1]
                    try:child.stdout.close()
                    except (OSError,ValueError) as close_error:
                        if primary is None:raise
                        primary.add_note(f'工作进程输出管道清理失败：{close_error}')
                self._wait_for_worker_exit(child)
                exit_confirmed=True
                code=child.returncode
                if self.stop_event.is_set():self._progress({'status':'cancelled','message':'任务已停止，已保存结果保留。'})
                elif code:
                    self._progress({'status':'needs_attention','message':self.job['message'] if self.job['logs'] else '任务未完成，请检查项目日志；已保存结果会保留。'})
                else:
                    filename='硅基流动试听状态.json' if action=='siliconflow-pilot' else 'campaign.json'
                    result=read_json(self._project_path()/filename)
                    if result.get('status') and action!='export-draft':
                        self._progress({'status':result['status']})
        except Exception as error:
            self._progress({'status':'needs_attention','message':str(error) if isinstance(error,(ValueError,RuntimeError)) else '任务运行出错，已保存结果保留，请检查项目日志。'})
        finally:
            if child is not None and not exit_confirmed:
                self._wait_for_worker_exit(child)
            self._finish_job()

    def _wait_for_worker_exit(self,child):
        try:
            if wait_for_process_exit(child,0):return
        except OSError:pass
        # A failed cleanup is not proof of exit. Preserve busy and this run's
        # stop marker until the native process signals, even if reporting fails.
        with self.lock:message=self.job['message']
        try:self.stop()
        except Exception:pass
        try:self._progress({'status':'needs_attention',
            'message':message+'；后台进程退出状态尚未确认，正在等待回收。'})
        except Exception:pass
        while True:
            try:
                if wait_for_process_exit(child,250):
                    try:self._progress({'message':message+'；后台进程已退出，已保存结果保留。'})
                    except Exception:pass
                    return
            except OSError:
                # The stop event is usually set, so it cannot pace this wait.
                time.sleep(.25)

    def _finish_job_locked(self):
        if self._cancel_marker:
            try:
                self._cancel_marker.unlink(missing_ok=True)
            except OSError:
                # A per-run marker cannot affect the next run. Failure to
                # remove it must not prevent releasing the controller.
                self._progress_locked({'message':self.job['message']+'；临时停止标记暂未清理，不影响其他任务。'})
            finally:
                self._cancel_marker=None
        self.job['busy']=False
        self.job['message']=self._scrub(self.job.get('message'))
        self.job['logs']=[self._scrub(line,500) for line in self.job.get('logs',[]) if isinstance(line,str)]
        self._queue_persist_locked()
        self._redactions=[]

    def _finish_job(self):
        with self.lock:self._finish_job_locked()
        self._drain_persistence()

    def stop(self):
        with self.lock:
            if not self.job['busy']:return {'stopped':False}
            if self.job['action']!='local':
                marker=self._cancel_marker
                if marker:
                    marker.parent.mkdir(parents=True,exist_ok=True)
                    marker.write_text('Stop requested for this run',encoding='utf-8')
            self.stop_event.set()
            self._progress_locked({'message':'正在停止后续工作，等待当前步骤保存…'})
        self._drain_persistence()
        return {'stopping':True}

    def _preview_selections(self):
        folder=self._output_dir() if self.campaign else None
        choices=[{'id':'main','name':'整片' if folder and folder.name=='整片' else '当前任务',
                  'offset_ms':0,'folder':folder,'sample_folder':None}]
        if not self.campaign:return choices
        project=self._project_path()
        manifest=read_json(self._contained_path(project,'campaign.json'))
        samples=manifest.get('samples',[])
        if not isinstance(samples,list):return choices
        for index,sample in enumerate(samples,1):
            if not isinstance(sample,dict):continue
            relative=sample.get('folder')
            start=sample.get('start_sec',0)
            if (not isinstance(relative,str) or not relative or Path(relative).is_absolute()
                or isinstance(start,bool) or not isinstance(start,(int,float))
                or not math.isfinite(start) or start<0):continue
            try:
                sample_folder=self._contained_path(project,relative)
                if sample_folder==project:continue
                job=self._contained_path(sample_folder,'识别任务')
            except (ValueError,OSError):continue
            choices.append({'id':f'sample-{index}','name':str(sample.get('name') or f'样片 {index}'),
                            'offset_ms':round(start*1000),'folder':job,'sample_folder':sample_folder,
                            'source_kind':manifest.get('source_kind','video')})
        return choices

    def _read_track(self,choice,name):
        if choice['folder'] is None:return []
        path=self._contained_path(choice['folder'],name)
        if path.is_file():return self._track_cache.read(path,parse_srt)
        self._track_cache.invalidate(path)
        return []

    def _preview_selection(self,sample=None):
        choices=self._preview_selections()
        if sample is not None:
            selected=next((item for item in choices if item['id']==sample),None)
            if selected is None:raise ValueError('所选样片不属于当前项目，请刷新结果')
        else:
            selected=next((item for item in choices if self._read_track(item,OUTPUTS[0])),choices[0])
        return selected,choices

    def _selection_media(self,selected):
        if selected['id']=='main':return Path(self.source) if self.source else None
        names=('preview.mp4','input.wav') if selected.get('source_kind')=='audio' else ('preview.mp4',)
        for name in names:
            path=self._contained_path(selected['sample_folder'],name)
            if path.is_file():return path
        return None

    def media_path(self,sample=None,project_id=None):
        return self._read_view(lambda view:view._media_path(sample),project_id)

    def _media_path(self,sample=None):
        selected,_=self._preview_selection(sample)
        media=self._selection_media(selected)
        if media is None or not media.is_file():raise ValueError('没有选择可播放的素材')
        return media

    def exported_video_path(self,project_id=None):
        return self._read_view(lambda view:view._exported_video_path(),project_id)

    def draft_video_path(self,project_id=None):
        return self._read_view(lambda view:view._draft_video_path(),project_id)

    def _video_matches(self,path,digest):
        # Hash once per observed file generation, not once per media range.
        # The bounded cache is shared by detached read views; its I/O does not
        # hold the controller lock used for stopping a worker.
        def stamp(info):
            return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        lock,cache=self._video_checks
        key=str(path)
        with lock:
            try:
                with path.open('rb') as stream:
                    before=stamp(os.fstat(stream.fileno()));path_before=stamp(path.stat())
                    # On Windows fd and path ctime can represent different
                    # timestamps. Compare each with its own later observation.
                    if before[:4]!=path_before[:4]:return False
                    signature=(digest,before,path_before)
                    if cache.get(key)==signature:return True
                    cache.pop(key,None)
                    actual=hashlib.file_digest(stream,'sha256').hexdigest()
                    if actual!=digest or stamp(os.fstat(stream.fileno()))!=before or stamp(path.stat())!=path_before:
                        return False
                if len(cache)>=8:cache.pop(next(iter(cache)))
                cache[key]=signature
                return True
            except OSError:
                cache.pop(key,None)
                return False

    def _draft_video_path(self):
        """Expose only the recorded draft corresponding to current saved cues."""
        if not self.campaign or not self.source:return None
        manifest=read_json(self._contained_path(self._project_path(),'campaign.json'))
        recorded=manifest.get('draft_output');binding=manifest.get('draft_output_binding')
        if (not isinstance(recorded,str) or not isinstance(binding,dict)
            or binding.get('render_version')!=1 or binding.get('review_status')!='unreviewed_draft'
            or not re.fullmatch(r'[0-9a-f]{64}',str(manifest.get('draft_output_sha256','')))):
            return None
        source=Path(self.source).resolve()
        if Path(manifest.get('source',{}).get('path','')).resolve()!=source:return None
        if binding.get('source_sha256')!=manifest.get('source',{}).get('sha256'):return None
        language,target=manifest_languages(manifest)
        if (binding.get('language','ja'),binding.get('target','zh-CN'))!=(language,target):return None
        output=Path(recorded).resolve()
        if output!=video_output_path(source,target,draft=True) or not output.is_file():return None
        folder=self._contained_path(self._project_path(),'整片')
        try:
            if self._missing_approved_review(folder):return None
            for name,field in zip(output_names(target)[:2],('machine_source_sha256','machine_target_sha256')):
                if hashlib.sha256(self._contained_path(folder,name).read_bytes()).hexdigest()!=binding.get(field):return None
            record=self._contained_path(self._contained_path(folder,'人工校对'),'校对记录.json')
            if record.exists():
                from .manual_review import load_review
                if load_review(folder,language=language,target=target)['revision']!=binding.get('manual_revision'):return None
            elif binding.get('manual_revision') is not None:return None
        except (ValueError,OSError,RuntimeError):return None
        return output if self._video_matches(output,manifest['draft_output_sha256']) else None

    def _exported_video_path(self):
        """Accept only the official export record at its deterministic target."""
        if not self.campaign or not self.source:return None
        manifest=read_json(self._contained_path(self._project_path(),'campaign.json'))
        recorded=manifest.get('output')
        binding=manifest.get('output_binding')
        if (manifest.get('status')!='exported' or not isinstance(recorded,str)
            or not isinstance(binding,dict) or binding.get('render_version')!=1
            or not re.fullmatch(r'[0-9a-f]{64}',str(manifest.get('output_sha256','')))):
            return None
        if self._manual_status(manifest).get('invalidates_approval'):return None
        source=Path(self.source).resolve()
        if Path(manifest.get('source',{}).get('path','')).resolve()!=source:return None
        if binding.get('source_sha256')!=manifest.get('source',{}).get('sha256'):return None
        language,target=manifest_languages(manifest)
        if (binding.get('language','ja'),binding.get('target','zh-CN'))!=(language,target):return None
        expected=video_output_path(source,target)
        output=Path(recorded).resolve()
        if output!=expected or not output.is_file():return None
        return output if self._video_matches(output,manifest['output_sha256']) else None

    def environment(self):
        # Explicit, bounded local probes do not occupy the task control lock.
        from .environment import probe_environment
        return probe_environment()

    def preview(self,sample=None,project_id=None):
        return self._read_view(lambda view:view._preview_data(sample),project_id)

    def _preview_data(self,sample=None):
        selected,choices=self._preview_selection(sample)
        language,target_language=self._selection_languages(selected)
        media=self._selection_media(selected)
        identity=self._project_id()
        result={'cues':[],'issues':[],'auditions':[],'downloads':[],
                'source_language':language,'target_language':target_language,
                'media_available':bool(media and media.is_file()),
                'source_name':Path(self.source).name if self.source else '',
                'selections':[{key:item[key] for key in ('id','name','offset_ms')} for item in choices],
                'selected_id':selected['id'],'offset_ms':selected['offset_ms'],'project_id':identity,
                'media_url':'/api/media?sample='+selected['id']+'&project='+identity,'exported_video':None,'draft_video':None}
        if not self.campaign:return result
        exported=self._exported_video_path()
        if exported:
            result['exported_video']={'name':exported.name,'media_url':'/api/output-video?project='+identity,
                                      'download_url':'/api/download?name=video&project='+identity}
        draft=self._draft_video_path()
        if draft:
            result['draft_video']={'name':draft.name,'review_status':'unreviewed_draft',
                                  'media_url':'/api/draft-video?project='+identity,
                                  'download_url':'/api/download?name=draft-video&project='+identity}
        folder=selected['folder']
        if self._missing_approved_review(folder):
            message='已验收的人工校对记录缺失，请恢复后重新核对；未回退到机器字幕。'
            result['manual_review']={'supported':True,'conflict':message}
            result['issues'].append(message)
            return result
        names=output_names(target_language)
        tracks=[self._read_track(selected,name) for name in names[:2]]
        source,target=tracks
        translated={(cue.start_ms,cue.end_ms):cue.text for cue in target}
        result['cues']=[{'id':index+1,'start_ms':cue.start_ms,'end_ms':cue.end_ms,'ja':cue.text,
                         'source_text':cue.text,'target_text':translated.get((cue.start_ms,cue.end_ms),''),
                         'zh':translated.get((cue.start_ms,cue.end_ms),'')} for index,cue in enumerate(source)]
        result['downloads']=[name for name in names if self._contained_path(folder,name).is_file()]
        state=read_json(self._contained_path(folder,'state.json'))
        # A content-bound manual overlay is shared by preview and exports;
        # the original generation evidence and machine subtitles stay intact.
        if source and target and type(state.get('duration_ms')) is int and state.get('identity',{}).get('source',{}).get('sha256'):
            try:
                from .manual_review import load_review
                review=load_review(folder,language=language,target=target_language)
                result['manual_review']={'supported':True,'revision':review['revision'],'summary':review['summary']}
                result['cues']=[{**cue,'ja':cue['source_text'],'zh':cue['target_text']} for cue in review['cues']]
            except (ValueError,OSError,RuntimeError) as error:
                result['manual_review']={'supported':True,'conflict':str(error)}
                result['issues'].append('人工校对记录需要处理：'+str(error))
        if state.get('review_status')!='reviewed' and source:result['issues'].append('自动生成结果仍需结合原音复核，不代表逐句人工校对。')
        if state.get('manual_outputs'):result['issues'].append('人工修改已保留，自动更新目录含新的机器结果，请核对后使用。')
        for choice in choices[1:]:
            candidate=read_json(self._contained_path(choice['sample_folder'],'siliconflow-candidate.json'))
            if isinstance(candidate.get('text'),str):result['auditions'].append({'name':choice['name'],'text':candidate['text'],'timing_verified':False})
        return result

    def _selection_languages(self,selected):
        manifest=read_json(self._contained_path(self._project_path(),'campaign.json')) if self.campaign else {}
        if manifest:return manifest_languages(manifest)
        folder=selected.get('folder')
        config=read_json(self._contained_path(folder,'state.json')).get('config',{}) if folder else {}
        return config.get('language','ja'),config.get('target','zh-CN')

    def download_path(self,name,sample=None,project_id=None):
        return self._read_view(lambda view:view._download_path(name,sample),project_id)

    def download_info(self,name,sample=None,project_id=None):
        def read(view):
            path=view._download_path(name,sample)
            if name in ('video','draft-video'):return path,path.name
            selected,_=view._preview_selection(sample)
            source=Path(view.source);selection_name=selected['name']
            # Resolve the timestamp after the response pins a file handle, so
            # an atomic subtitle update cannot mix a new body with an old name.
            return path,lambda modified:subtitle_filename(source,selection_name,name,modified)
        return self._read_view(read,project_id)

    def save_subtitles(self,data):
        # This is an explicit, small local write. Hold the selection lock so
        # starting work or switching projects cannot race the snapshot, and
        # acquire the same process locks used by the recognition workers.
        with self.lock:
            if not data.get('project_id'):raise ValueError('请刷新当前项目后再保存字幕版本')
            self._assert_project(data['project_id'])
            if self.job['busy']:raise ValueError('请在当前任务停止后保存字幕版本')
            project=self._project_path()
            state=read_json(self._contained_path(project,'state.json'))
            manifest=read_json(self._contained_path(project,'campaign.json'))
            self._validate_loaded_project(project,state,manifest)
            source=state.get('config',{}).get('source') or manifest.get('source',{}).get('path')
            if not self.source or Path(source).resolve()!=Path(self.source).resolve():
                raise ValueError('项目来源已变化，请重新载入任务后再保存字幕版本')
            selected,_=self._preview_selection(data.get('sample'))
            language,target=self._selection_languages(selected)
            selected={**selected,'tracks':output_names(target),'source_language':language,'target_language':target}
            folder=selected['folder']
            if folder is None or not folder.is_dir():raise ValueError('所选片段的字幕尚未生成')
            with ExitStack() as locks:
                locks.enter_context(ProjectLock(project))
                if folder!=project:locks.enter_context(ProjectLock(folder))
                if self._missing_approved_review(folder):
                    raise ValueError('已验收的人工校对记录缺失，请恢复后再保存字幕版本')
                if (folder/'人工校对'/'校对记录.json').exists():
                    from .manual_review import materialize_review
                    revision=materialize_review(folder,language=language,target=target)
                    selected.update(track_folder=revision['folder'],manual_review={
                        'revision':revision['revision'],'summary':revision['summary'],'binding':revision['binding']})
                return save_subtitle_snapshot(project,Path(self.source),selected,
                    created_at=self._file_layout_data()['created_at'])

    def _download_path(self,name,sample=None):
        selected,_=self._preview_selection(sample)
        if name in ('video','draft-video'):
            path=self._draft_video_path() if name=='draft-video' else self._exported_video_path()
            if path is None:raise ValueError('尚无与当前字幕一致的导出视频')
            return path
        if name not in output_names(self._selection_languages(selected)[1]):raise ValueError('不支持下载此文件')
        if selected['folder'] is None:raise ValueError('字幕尚未生成')
        if self._missing_approved_review(selected['folder']):
            raise ValueError('已验收的人工校对记录缺失，请恢复后再下载字幕')
        if (selected['folder']/'人工校对'/'校对记录.json').exists():
            from .manual_review import materialize_review
            language,target=self._selection_languages(selected)
            with ExitStack() as locks:
                project=self._project_path()
                locks.enter_context(ProjectLock(project))
                if selected['folder']!=project:locks.enter_context(ProjectLock(selected['folder']))
                revision=materialize_review(selected['folder'],language=language,target=target)
                return self._contained_path(revision['folder'],name)
        path=self._contained_path(selected['folder'],name)
        if not path.is_file():raise ValueError('字幕尚未生成')
        return path

    def open_result(self,target,project_id=None):
        with self.lock:
            self._assert_project(project_id)
            return self._open_result(target)

    def _review_selection_for_write(self,data):
        if not data.get('project_id'):raise ValueError('请刷新当前项目后再保存校对')
        self._assert_project(data['project_id'])
        if self.job['busy']:raise ValueError('请在当前任务停止后保存校对')
        if not isinstance(data.get('expected_revision'),str) or not data['expected_revision']:
            raise ValueError('缺少校对版本，请刷新结果')
        project=self._project_path()
        self._validate_loaded_project(project,read_json(self._contained_path(project,'state.json')),
                                      read_json(self._contained_path(project,'campaign.json')))
        selected,_=self._preview_selection(data.get('sample'))
        if selected['folder'] is None:raise ValueError('字幕尚未生成')
        return project,selected

    def review_cue(self,data):
        from .manual_review import save_review
        mode=data.get('response_mode','preview')
        if mode not in ('preview','cue'):raise ValueError('校对返回方式无效')
        with self.lock:
            project,selected=self._review_selection_for_write(data)
            language,target=self._selection_languages(selected)
            with ExitStack() as locks:
                locks.enter_context(ProjectLock(project))
                if selected['folder']!=project:locks.enter_context(ProjectLock(selected['folder']))
                review=save_review(selected['folder'],{key:value for key,value in data.items() if key not in ('project_id','sample','response_mode')},
                            language=language,target=target)
            if mode=='cue':
                cue=review['cues'][data['cue_id']-1]
                return {'kind':'review-cue','project_id':self._project_id(),'selected_id':selected['id'],
                        'base_revision':data['expected_revision'],
                        'cue':{**cue,'ja':cue['source_text'],'zh':cue['target_text']},
                        'manual_review':{'supported':True,'revision':review['revision'],'summary':review['summary']},
                        'exported_video':None,'draft_video':None}
            return self._preview_data(selected['id'])

    def review_accept(self,data):
        from .cloud_workflow import accept_final
        with self.lock:
            if data.get('content_passed') is not True:raise ValueError('请明确确认已听看抽检并处理疑点')
            if data.get('sample')!='main':raise ValueError('请在整片校对页完成最终验收')
            project,selected=self._review_selection_for_write(data)
            if selected['folder']!=project/'整片':raise ValueError('当前任务尚未生成可验收的整片')
            with ExitStack() as locks:
                locks.enter_context(ProjectLock(project))
                locks.enter_context(ProjectLock(selected['folder']))
                result=accept_final(project,True,expected_revision=data['expected_revision'])
            return result

    def _open_result(self,target):
        project=self._project_path()
        if target in ('draft-video','draft-video-folder'):
            video=self.draft_video_path()
            if video is None:raise ValueError('尚无与当前字幕一致的草稿视频')
            path=video if target=='draft-video' else video.parent
            os.startfile(path)
            return {'opened':True,'path':str(path)}
        if target in ('video','video-folder'):
            video=self.exported_video_path()
            if video is None:raise ValueError('尚无可用的正式导出视频')
            path=video if target=='video' else video.parent
            os.startfile(path)
            return {'opened':True,'path':str(path)}
        candidates={'project':project,'review':self._contained_path(project,'review.html'),
                    'exports':self._contained_path(project,'导出'),
                    'source-folder':Path(self.source).resolve().parent if self.source else None,
                    'siliconflow-review':self._contained_path(project,'硅基流动识别试听.html')}
        path=candidates.get(target)
        if path is None or not path.exists():raise ValueError('结果尚未生成')
        os.startfile(path)
        return {'opened':True,'path':str(path)}

    def pick(self,kind):
        if kind not in ('source','campaign','baseline'):raise ValueError('不支持的文件选择类型')
        script="import tkinter as t;from tkinter import filedialog as f;import sys;r=t.Tk();r.withdraw();r.attributes('-topmost',True);k=sys.argv[1];p=f.askdirectory(parent=r,title='选择字幕项目') if k=='campaign' else f.askopenfilename(parent=r,title='选择文件',filetypes=[('字幕','*.srt')] if k=='baseline' else [('视频与音频','*.mp4 *.mkv *.mov *.avi *.webm *.wav *.mp3 *.m4a'),('所有文件','*.*')]);print(p);r.destroy()"
        child=subprocess.run([sys.executable,'-X','utf8','-c',script,kind],capture_output=True,text=True,encoding='utf-8',
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if child.returncode:raise ValueError('无法打开文件选择器，请直接粘贴文件路径')
        return {'path':child.stdout.strip()}


def make_handler(controller,token,host,static_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def headers_common(self):
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

        def json_response(self,value,status=200,*,session=False):
            body=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status);self.headers_common()
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            if session:self.send_header('Set-Cookie',f'subtitle_session_{host.rsplit(":",1)[-1]}={token}; Path=/; HttpOnly; SameSite=Strict')
            self.end_headers();self.wfile.write(body)

        def file_response(self,path,download=False,filename=None):
            with path.open('rb') as stream:
                # Pin one file generation while writers atomically publish
                # newer subtitles. Headers and body must describe that handle.
                info=os.fstat(stream.fileno());size=info.st_size
                download_name=filename(info.st_mtime) if callable(filename) else filename or path.name
                partial=bool(self.headers.get('Range'))
                try:start,end=byte_range(self.headers.get('Range'),size) if size or partial else (0,-1)
                except ValueError:
                    self.send_response(416);self.headers_common()
                    self.send_header('Content-Range',f'bytes */{size}')
                    self.send_header('Content-Length','0');self.end_headers();return
                self.send_response(206 if partial else 200);self.headers_common()
                mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
                if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                if download:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(download_name))
                self.end_headers()
                stream.seek(start);remaining=end-start+1
                while remaining:
                    block=stream.read(min(1024*256,remaining))
                    if not block:break
                    self.wfile.write(block);remaining-=len(block)

        def do_GET(self):
            parsed=urlsplit(self.path);path=unquote(parsed.path)
            if self.headers.get('Host')!=host:return self.json_response({'error':'本机地址不匹配'},403)
            if path.startswith('/api/'):
                if not authorize_request(self.headers,token,host):return self.json_response({'error':'请从桌面入口重新打开程序'},403)
                self.server.last_touch=time.monotonic()
                try:
                    query=parse_qs(parsed.query,keep_blank_values=True)
                    sample=query.get('sample',[None])[0]
                    project_id=query.get('project',[None])[0]
                    if path=='/api/state':return self.json_response(controller.state())
                    if path=='/api/progress':return self.json_response(controller.progress())
                    if path=='/api/environment':return self.json_response(controller.environment())
                    if path=='/api/preview':return self.json_response(controller.preview(sample,project_id))
                    if path=='/api/media':
                        return self.file_response(controller.media_path(sample,project_id))
                    if path=='/api/output-video':
                        media=controller.exported_video_path(project_id)
                        if media is None:raise ValueError('尚无可用的正式导出视频')
                        return self.file_response(media)
                    if path=='/api/draft-video':
                        media=controller.draft_video_path(project_id)
                        if media is None:raise ValueError('尚无与当前字幕一致的草稿视频')
                        return self.file_response(media)
                    if path=='/api/download':
                        output,filename=controller.download_info(query.get('name',[''])[0],sample,project_id)
                        return self.file_response(output,True,filename)
                    return self.json_response({'error':'接口不存在'},404)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):return
                except ProjectSelectionChanged as error:return self.json_response({'error':str(error)},400)
                except (ValueError,OSError,RuntimeError):return self.json_response({'error':'无法读取所选结果，请检查文件是否存在。'},400)
            try:resolved=static_file_path(static_dir,path)
            except (ValueError,OSError):return self.json_response({'error':'页面不存在'},404)
            try:self.file_response(resolved)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):pass

        def do_POST(self):
            if not authorize_request(self.headers,token,host):return self.json_response({'error':'本机请求校验失败，请从桌面入口重新打开'},403)
            self.server.last_touch=time.monotonic()
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.json_response({'error':'需要JSON请求'},415)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=64_000:raise ValueError('请求大小无效')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('请求格式无效')
                path=urlsplit(self.path).path
                routes={'/api/project':controller.select_project,'/api/credentials':controller.save_credentials,
                        '/api/siliconflow-settings':controller.save_siliconflow_pricing,
                        '/api/settings':controller.save_settings,'/api/run':controller.start,'/api/stop':lambda _:controller.stop(),
                        '/api/save-subtitles':controller.save_subtitles,
                        '/api/review-cue':controller.review_cue,'/api/review-accept':controller.review_accept,
                        '/api/open':lambda d:controller.open_result(d.get('target'),d.get('project_id')),'/api/pick':lambda d:controller.pick(d.get('kind'))}
                if path=='/api/session':return self.json_response({'connected':True},session=True)
                if path not in routes:return self.json_response({'error':'接口不存在'},404)
                return self.json_response(routes[path](data))
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):return
            except (ValueError,RuntimeError) as error:
                # Key-bearing routes return a fixed message, never a library exception.
                message='API Key 保存失败，请检查格式或当前 Windows 用户的存储权限。' if urlsplit(self.path).path=='/api/credentials' else str(error)
                return self.json_response({'error':message},400)
            except Exception:return self.json_response({'error':'操作未完成，请检查本机文件与配置。'},500)

    return Handler


def open_desktop_window(url):
    candidates=[Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',
                Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Microsoft/Edge/Application/msedge.exe']
    browser=next((str(path) for path in candidates if path.is_file()),None)
    if browser:
        subprocess.Popen([browser,'--app='+url,'--window-size=1320,920'],stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else:webbrowser.open(url)


def running_instance(path):
    """Reconnect to a live loopback instance without mistaking busy for absent."""
    value=read_json(path)
    url=value.get('url','')
    match=re.fullmatch(r'http://127\.0\.0\.1:(\d{1,5})/#token=([A-Za-z0-9_-]{20,100})',url)
    if not match or not pid_alive(value.get('pid')):return None
    port=int(match[1])
    if not 0<port<65536:return None
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    try:
        connection.request('GET','/api/state',headers={'X-Subtitle-Token':match[2]})
        response=connection.getresponse()
        content=response.read(1_000_001)
        if response.status==200 and len(content)<=1_000_000:
            state=json.loads(content)
            if isinstance(state,dict) and state.get('app')=='字幕工坊':return value
    except ConnectionRefusedError:
        # The recorded process may still be exiting, but its port has no host.
        return None
    except TimeoutError:
        # A cold browser launch or a settings save can delay this endpoint.
        # Reopen its existing URL; never overwrite the runtime and create a
        # second controller merely because the first did not answer in time.
        return value if pid_alive(value.get('pid')) else None
    except (OSError,ValueError,http.client.HTTPException):pass
    finally:connection.close()
    if not pid_alive(value.get('pid')):return None
    raise RuntimeError('字幕工坊进程仍在运行，但本机服务状态暂时无法确认；请稍候重开，或先关闭原窗口。')


def main(argv=None):
    parser=argparse.ArgumentParser(description='字幕工坊 · 本机桌面工作台')
    for name in ('source','campaign','baseline'):parser.add_argument('--'+name,default='')
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    if sys.stdout and hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    static=ROOT/'frontend'/'dist'
    if not (static/'index.html').is_file():raise RuntimeError('前端尚未构建，请在 frontend 目录执行 npm run build')
    runtime=data_directory()/'runtime.json'
    lock_dir=data_directory()/'instance';lock_dir.mkdir(parents=True,exist_ok=True)
    with ProjectLock(lock_dir):
        existing=running_instance(runtime)
        if existing:
            if sys.stdout:print(json.dumps({**existing,'reused':True}),flush=True)
            if not args.no_browser:open_desktop_window(existing['url'])
            return 0
        controller=StudioController(source=args.source,campaign=args.campaign,baseline=args.baseline)
        token=secrets.token_urlsafe(32)
        server,origin_warning=create_server(ThreadingHTTPServer,BaseHTTPRequestHandler,
                                           data_directory()/'browser-port.json',port=args.port)
        host=f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass=make_handler(controller,token,host,static)
        server.last_touch=time.monotonic()
        url='http://'+host+'/#token='+token
        atomic_json(runtime,{'url':url,'pid':os.getpid()})
        # Explicit launch selections must survive a restart even if no UI
        # action occurs. Constructors and reused instances remain read-only.
        if any((args.source,args.campaign,args.baseline)):controller._persist()
        if origin_warning:
            controller.persistence_warning='；'.join(filter(None,(controller.persistence_warning,origin_warning)))
    if sys.stdout:print(json.dumps({'url':url,'pid':os.getpid()},ensure_ascii=False),flush=True)
    if not args.no_browser:open_desktop_window(url)
    def idle_shutdown():
        while True:
            time.sleep(10)
            if controller.idle_ready() and time.monotonic()-server.last_touch>180:
                server.shutdown();return
    threading.Thread(target=idle_shutdown,daemon=True).start()
    try:server.serve_forever(poll_interval=.3)
    finally:
        server.server_close()
        if read_json(runtime).get('pid')==os.getpid():runtime.unlink(missing_ok=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
