"""Resumable local ASR with a single, overlapping translation queue."""
from __future__ import annotations

import argparse
import concurrent.futures as futures
from collections import deque
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace

from .subtitles import Cue, Chunk, parse_srt, render_srt, plan_chunks, merge_chunk_cues, check_cues
from .translate import translate_texts
from .atomic_io import cleanup_temporary, replace_with_retry
from .windows_job import owned_process
from .languages import validate_languages, target_filename, output_names, manifest_languages
from . import translation_context
from .integrity import (sha256, fingerprint, reconcile_source, reconcile_translation,
                        HumanEditConflict, verify_part, validate_chunk_plan, evidence_matches,
                        sample_state_evidence, reconcile_merged_source)


class Cancelled(RuntimeError):
    pass


class _WorkerStop:
    """Let scheduler failures stop workers without recording user cancellation."""
    def __init__(self, requested):
        self._requested = requested
        self._aborted = threading.Event()

    def is_set(self):
        return self._requested.is_set() or self._aborted.is_set()

    def set(self):
        # Provider safety stops must still reach the shared caller event.
        self._requested.set()

    def abort(self):
        self._aborted.set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self.is_set()
            self._aborted.wait(.05 if remaining is None else min(.05, remaining))
        return True


@contextmanager
def _abort_workers_on_error(stop):
    try:
        yield
    except BaseException:
        # This scope exits before either executor waits for its owned workers.
        stop.abort()
        raise


def _note_secondary_error(primary,stage,error):
    details=tuple(getattr(error,'__notes__',()))
    primary.add_note(f'{stage}: {type(error).__name__}: {error}')
    for detail in details:
        primary.add_note(f'{stage}: {detail}')


@dataclass
class PipelineConfig:
    source: Path
    project: Path
    language: str = 'ja'
    target: str = 'zh-CN'
    chunk_seconds: float = 300
    overlap_seconds: float = 2
    workers: int = 1
    threads: int = 6
    translate: bool = True
    seed_srt: Path | None = None
    seed_complete_until_ms: int = 0
    asr_provider: str = 'whisper_cpp'
    asr_api_version: str = 'dashscope-v1'
    asr_model: str = 'qwen-audio-3.1-asr-flash'
    translation_provider: str = 'google'
    translation_model: str = 'deepseek-flash'
    budget_cny: float = 20.0
    budget_ledger: Path | None = None
    workflow_stage: str = 'sample'
    approval_path: Path | None = None


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                dir=path.parent, prefix=path.name+'.', suffix='.tmp', delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(tmp, path)
    finally:
        if tmp is not None:
            cleanup_temporary(tmp)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def backup_user_file(project, path):
    if path.exists():
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()[:12]
        destination = project/'用户修改备份'/f'{path.parent.name}-{path.stem}-{digest}{path.suffix}'
        destination.parent.mkdir(parents=True,exist_ok=True)
        if not destination.exists():
            destination.write_bytes(content)


def _output_checkpoint(state):
    # Error messages and progress counters may change after an I/O failure;
    # only the chunk plan and content-bound completion records define outputs.
    fields=('asr','source_hash','translation','translation_source_hash','target_hash')
    return fingerprint({'chunks':state['chunks'],'parts':{
        index:{key:part.get(key) for key in fields}
        for index,part in state['parts'].items()}})


def _recover_pending_outputs(project, state):
    if 'pending_outputs' not in state:
        return
    journal=state['pending_outputs']
    names=output_names(state['identity'].get('target','zh-CN'))
    allowed=set(names)|{str(Path('自动更新')/name) for name in names}|{'需复核.json','需复核.txt'}
    if (not isinstance(journal,dict) or type(journal.get('version')) is not int
        or journal['version']!=1 or journal.get('identity')!=fingerprint(state['identity'])
        or journal.get('checkpoint')!=_output_checkpoint(state)
        or not isinstance(journal.get('outputs'),dict) or not 1<=len(journal['outputs'])<=5):
        raise HumanEditConflict('字幕输出恢复记录无效，请保留状态和字幕文件后核对')
    for relative,digest in journal['outputs'].items():
        if (relative not in allowed or not isinstance(digest,str)
            or re.fullmatch(r'[0-9a-f]{64}',digest) is None
            or not (project/relative).resolve().is_relative_to(project.resolve())):
            raise HumanEditConflict('字幕输出恢复记录的路径或校验值无效，请保留文件后核对')
    recovered={}
    for relative,digest in journal['outputs'].items():
        path=project/relative
        if path.exists() and sha256(path)==digest:
            recovered[relative]=digest
    # Old, missing, or independently edited files never acquire a new expected
    # hash. Normal regeneration/edit reconciliation handles those separately.
    state.setdefault('generated_hashes',{}).update(recovered)
    state.pop('pending_outputs')


def probe_media(source: Path, stop=None) -> int:
    # Local imports avoid a cycle: capture/export helpers use Cancelled above.
    from .local_process import capture_process
    result = capture_process(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'json', source], stop=stop, timeout=30)
    duration = float(json.loads(result.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('无法读取有效音频时长')
    if stop is not None and stop.is_set():
        raise Cancelled('已停止读取媒体时长')
    return round(duration * 1000)


def run_process(args, log: Path, stop: threading.Event, env=None, cwd=None):
    if stop.is_set():
        raise Cancelled('已停止')
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w', encoding='utf-8') as output:
        child = subprocess.Popen([str(a) for a in args], stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=cwd, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with owned_process(child):
            try:
                while child.poll() is None:
                    if stop.wait(.2):
                        raise Cancelled('已停止，已完成片段保留')
                if child.returncode:
                    raise RuntimeError(f'{Path(args[0]).name} 返回 {child.returncode}；详情见 {log.name}')
            finally:
                # Windows ownership closes the Job and preserves the active
                # cancellation if process cleanup also fails.
                if os.name != 'nt' and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)


def detect_silences(source, stop, project):
    log = project / 'silence.log'
    run_process(['ffmpeg','-nostdin','-hide_banner','-i',source,'-map','0:a:0','-vn','-af','silencedetect=noise=-35dB:d=0.35','-threads','1','-f','null','-'], log, stop)
    starts, intervals = [], []
    for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
        found = re.search(r'silence_start: ([\d.]+)', line)
        if found:
            starts.append(round(float(found[1]) * 1000))
        found = re.search(r'silence_end: ([\d.]+)', line)
        if found and starts:
            intervals.append((starts.pop(), round(float(found[1]) * 1000)))
    return intervals


def engine_paths():
    local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    for base in (local / 'SubtitlePipeline/runtimes/whisper.cpp',
                 local / 'Programs/SubtitleEdit/SpeechToText/Cpp'):
        exe, model = base / 'whisper-cli.exe', base / 'Models/small.bin'
        if exe.is_file() and model.is_file():
            return exe, model
    raise FileNotFoundError('未找到已安装的 Whisper CPP / small 模型')


def recognize_chunk(config, chunk, folder, stop, *, before_submit=None):
    audio = folder / 'audio.wav'
    input_path=config.project/'timeline.wav' if config.asr_provider=='qwen_asr' else config.source
    run_process(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-ss',f'{chunk.audio_start_ms/1000:.3f}','-i',input_path,'-t',f'{(chunk.audio_end_ms-chunk.audio_start_ms)/1000:.3f}','-map','0:a:0','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-threads','1','-y',audio], folder / 'extract.log', stop)
    if config.asr_provider=='qwen_asr':
        from .qwen_asr import transcribe
        settings,ledger=cloud_context(config)
        request_id=fingerprint({'provider':'qwen_asr','audio':sha256(audio),'endpoint':settings.asr_endpoint,
            'api':config.asr_api_version,'model':config.asr_model,'language_hints':[config.language],'diarization':False})
        result=transcribe(audio,endpoint=settings.asr_endpoint,key=settings.asr_key,
            model=config.asr_model,ledger=ledger,language=config.language,
            request_id=request_id,raw_path=folder/'asr-response.json',
            input_rate_cny_per_million=settings.asr_input_rate,
            output_rate_cny_per_million=settings.asr_output_rate,stop_event=stop,
            allow_empty_draft=(config.workflow_stage=='draft'),
            **({'before_submit':before_submit} if before_submit is not None else {}))
        atomic_json(folder/'recognition-metadata.json',{'metadata':result.metadata,'issues':result.issues,
            'request_id':request_id,'provider':'qwen_asr'})
        return result.cues
    exe, model = engine_paths()
    environment = os.environ.copy()
    environment.update(OPENBLAS_NUM_THREADS=str(config.threads), OMP_NUM_THREADS=str(config.threads))
    run_process([exe,'-m',model,'-f',audio,'-l',config.language,'-t',str(config.threads),'-ng','-osrt','-of',folder/'asr','--print-progress'], folder/'asr.log', stop, environment)
    return parse_srt((folder/'asr.srt').read_text(encoding='utf-8-sig'))


def file_identity(path, stop=None):
    if stop is not None and stop.is_set():
        raise Cancelled('已停止核对输入文件')
    path = Path(path).resolve()
    stat = path.stat()
    if not path.is_file():
        raise ValueError('输入必须是文件')
    if stop is None:
        digest = sha256(path)
    else:
        from .export_io import cancellable_sha256
        digest = cancellable_sha256(path, stop)
    return {'path':str(path), 'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns,'sha256':digest}


def cloud_context(config):
    from .cloud_settings import load_settings
    from .cloud_budget import BudgetLedger
    ledger_path=Path(config.budget_ledger or config.project/'费用账本.json')
    if (ledger_path.parent/'billing-review-required.json').exists():
        raise ValueError('识别计量异常尚待复核，已暂停新的识别及翻译请求；请保留原始响应与费用记录')
    settings=load_settings(require_deepseek=config.translate and config.translation_provider=='deepseek')
    ledger=BudgetLedger(ledger_path,
                        budget_cny=config.budget_cny,stop_cny=min(18.0,config.budget_cny-2.0))
    return settings,ledger


def _check_pcm_stop(stop, cause=None):
    if stop.is_set():
        raise Cancelled('已停止核对 PCM 文件，未启动新识别，已有产物保留') from cause


def _pcm_file_token(path,stop):
    # Change detection after a full content hash, not a substitute for it.
    # External changes that restore every observed field are outside this guard.
    _check_pcm_stop(stop)
    try:
        value=Path(path).stat()
    except OSError as error:
        _check_pcm_stop(stop,error)
        raise
    _check_pcm_stop(stop)
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def _pcm_changed():
    return ValueError('PCM 文件在内容校验后发生变化，未启动新识别，已有产物保留')


def _verified_pcm_hash(path,stop):
    from .export_io import cancellable_sha256
    before=_pcm_file_token(path,stop)
    try:
        digest=cancellable_sha256(path,stop)
    except OSError as error:
        _check_pcm_stop(stop,error)
        raise
    after=_pcm_file_token(path,stop)
    if before!=after:
        raise _pcm_changed()
    return digest,after


def prepare_cloud_audio(config,state,stop):
    if stop.is_set():
        raise Cancelled('已停止准备识别音频')
    target=config.project/'timeline.wav'
    if target.exists():
        digest,token=_verified_pcm_hash(target,stop)
        if state.get('timeline_hash')==digest:
            return token
    partial=config.project/'timeline.partial.wav'
    run_process(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-i',config.source,
        '-map','0:a:0','-vn','-af','aresample=async=1:first_pts=0','-ac','1','-ar','16000',
        '-c:a','pcm_s16le','-threads','1','-y',partial],config.project/'timeline.log',stop)
    actual=probe_media(partial,stop=stop)
    if abs(actual-state['duration_ms'])>1000:
        raise ValueError('提取音频与原视频时间轴相差超过1秒，需检查源文件')
    digest,token=_verified_pcm_hash(partial,stop)
    if _pcm_file_token(partial,stop)!=token:
        raise _pcm_changed()
    try:
        os.replace(partial,target)
    except OSError as error:
        _check_pcm_stop(stop,error)
        raise
    published=_pcm_file_token(target,stop)
    # Windows rename may change ctime. The object, size and mtime must still
    # match the fully hashed partial; retain the target's full token afterward.
    if published[:4]!=token[:4]:
        raise _pcm_changed()
    state['timeline_hash']=digest
    return published


def validate_full_approval(config,source_hash):
    if not config.approval_path:
        raise ValueError('整片云端处理需要先完成人工样片验收')
    approval=json.loads(Path(config.approval_path).read_text(encoding='utf-8'))
    if (approval.get('status')!='approved' or approval.get('source_sha256')!=source_hash
        or approval.get('asr_api_version')!=config.asr_api_version
        or approval.get('asr_provider')!=config.asr_provider
        or approval.get('asr_model')!=config.asr_model
        or approval.get('translation_model')!=config.translation_model
        or approval.get('language','ja')!=config.language
        or approval.get('target','zh-CN')!=config.target
        or approval.get('reviewed_cues',0)<20
        or approval.get('timing_passed',0)/max(1,approval.get('reviewed_cues',0))<.9
        or not approval.get('content_passed')):
        raise ValueError('样片验收记录不适用于本片或未达到通过条件')
    artifacts=approval.get('artifacts',[])
    if not artifacts or any(sha256(item['path'])!=item['sha256'] for item in artifacts):
        raise ValueError('样片内容已经改变，需要重新验收')
    try:
        campaign=Path(config.approval_path).resolve().parent
        manifest=json.loads((campaign/'campaign.json').read_text(encoding='utf-8'))
        if (manifest_languages(manifest)!=(config.language,config.target)
            or manifest['source']['sha256']!=source_hash
            or sha256(manifest['source']['path'])!=source_hash or not manifest['samples']):
            raise ValueError('审核来源或样片列表无效')
        samples=[]
        for sample in manifest['samples']:
            folder=(campaign/sample['folder']).resolve()
            if not folder.is_relative_to(campaign):
                raise ValueError('样片目录不属于当前项目')
            proof=sample_state_evidence(folder/'识别任务',provider=config.asr_provider,
                model=config.asr_model,api=config.asr_api_version,translation_model=config.translation_model,
                source_path=folder/'input.wav',language=config.language,target=config.target)
            if proof['source_sha256']!=sample.get('audio_hash'):
                raise ValueError('样片音频与准备记录不一致')
            samples.append(proof)
        if approval.get('sample_states')!=samples:
            raise ValueError('审核后样片状态或识别来源发生改变')
    except (OSError,KeyError,TypeError,ValueError):
        raise ValueError('样片实际状态、来源或模型不符合审核记录，请重新核对并验收') from None


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if handle:
            try:
                # Exited processes can remain openable while another process
                # retains their handle; only an unsignaled process is alive.
                return kernel.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel.CloseHandle(handle)
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ProjectLock:
    def __init__(self, project):
        self.path = project / 'run.lock'
        self.owned = False
        self._guard = None

    def __enter__(self):
        from .cloud_budget import _file_lock
        # Keep the guard inode stable; run.lock remains the transient PID marker
        # understood by existing projects and tools. The OS releases this guard
        # after a crash, so stale PID recovery is serialized across processes.
        guard_path = self.path.with_name('run.guard.lock')
        if guard_path.is_symlink():
            raise RuntimeError('项目锁不能指向符号链接')
        guard = ExitStack()
        try:
            if not guard.enter_context(_file_lock(guard_path, blocking=False)):
                raise RuntimeError('这个项目已经在另一个窗口运行')
            self._acquire()
        except BaseException as primary:
            try:
                guard.close()
            except Exception as cleanup_error:
                _note_secondary_error(primary,f'项目锁取得失败后的 guard 释放失败：{guard_path}',cleanup_error)
            raise
        self._guard = guard
        return self

    def _acquire(self):
        for attempt in range(2):
            try:
                with self.path.open('x', encoding='utf-8') as file:
                    json.dump({'pid':os.getpid(), 'created':time.time()}, file)
                self.owned = True
                return self
            except FileExistsError:
                try:
                    saved = json.loads(self.path.read_text(encoding='utf-8'))
                except (ValueError, OSError):
                    raise RuntimeError('项目锁无法读取；请确认另一窗口已关闭后再试')
                if pid_alive(saved.get('pid')):
                    raise RuntimeError('这个项目已经在另一个窗口运行')
                if attempt == 0:
                    self.path.unlink()
        raise RuntimeError('无法取得项目锁')

    def __exit__(self, *args):
        primary=args[1] if len(args)>1 else None
        first_error=None
        marker_interrupt=None
        try:
            try:
                if self.owned:
                    self.path.unlink(missing_ok=True)
            except Exception as error:
                if primary is None:
                    first_error=error
                    error.add_note(f'项目锁标记清理失败，保留路径：{self.path}')
                else:
                    _note_secondary_error(primary,f'项目锁标记清理失败，保留路径：{self.path}',error)
            except BaseException as interruption:
                marker_interrupt=interruption
                raise
        finally:
            # A new interrupt from marker cleanup keeps priority over ordinary
            # guard-close errors; the guard still gets its release attempt.
            self.owned = False
            guard, self._guard = self._guard, None
            if guard is not None:
                try:
                    guard.close()
                except Exception as error:
                    target=marker_interrupt if marker_interrupt is not None else primary if primary is not None else first_error
                    stage=f'项目锁 guard 释放失败：{self.path.with_name("run.guard.lock")}'
                    if target is None:
                        first_error=error
                        error.add_note(stage)
                    else:
                        _note_secondary_error(target,stage,error)
        if primary is None and first_error is not None:
            raise first_error


def validate_config(config):
    validate_languages(config.language,config.target,allow_auto=config.asr_provider=='whisper_cpp')
    config.source, config.project = Path(config.source).resolve(), Path(config.project).resolve()
    if config.seed_srt:
        config.seed_srt = Path(config.seed_srt).resolve()
    for name in ('budget_ledger','approval_path'):
        if getattr(config,name) is not None:
            setattr(config,name,Path(getattr(config,name)).resolve())
    if config.asr_provider not in ('whisper_cpp','qwen_asr') or config.translation_provider not in ('google','deepseek'):
        raise ValueError('不支持的识别或翻译提供方')
    if not math.isfinite(config.budget_cny) or not 2 < config.budget_cny <= 20:
        raise ValueError('本轮预算须大于2元且不得超过20元')
    if config.asr_provider=='qwen_asr':
        if config.seed_srt or config.seed_complete_until_ms:
            raise ValueError('新云端识别禁止导入旧 small 字幕作为成功缓存')
        if (config.asr_api_version!='dashscope-v1'
            or config.asr_model!='qwen-audio-3.1-asr-flash'):
            raise ValueError('本次国内方案固定使用千问3.1 ASR')
        if config.chunk_seconds+2*config.overlap_seconds>180:
            raise ValueError('本次千问方案单段音频不得超过180秒')
        if config.workflow_stage not in ('sample','draft','full'):
            raise ValueError('云端任务阶段无效')
    if config.workers not in (1,2) or not 1 <= config.threads <= 32:
        raise ValueError('识别并发只能是 1 或 2；线程数应为 1–32')
    if not all(math.isfinite(x) for x in (config.chunk_seconds, config.overlap_seconds)) or config.chunk_seconds <= 0 or not 0 <= config.overlap_seconds < config.chunk_seconds/2:
        raise ValueError('分段长度必须大于两倍上下文长度')
    if config.project == config.source or config.source.is_relative_to(config.project):
        raise ValueError('输出项目必须与源文件所在位置分开，避免覆盖输入')
    if config.seed_srt and config.seed_srt.is_relative_to(config.project):
        raise ValueError('导入字幕必须位于项目输出目录之外')


def run_pipeline(config, stop_event=None, on_progress=None):
    """Run one project. Successful ASR and translation stages survive every retry."""
    validate_config(config)
    stop = stop_event or threading.Event()
    config.project.mkdir(parents=True, exist_ok=True)
    with ProjectLock(config.project):
        return _run_locked(config, stop, on_progress or (lambda state: None))


def _run_locked(config, stop, progress):
    project = config.project
    translated_name = target_filename(config.target)
    state_path = project / 'state.json'
    encoded_config = {k: str(v) if isinstance(v,Path) else v for k,v in asdict(config).items()}
    try:
        source_identity=file_identity(config.source,stop=stop)
        seed_identity=file_identity(config.seed_srt,stop=stop) if config.seed_srt else None
    except Cancelled:
        return {'status':'cancelled','message':'已停止准备','total':0,'recognized':0,'translated':0}
    identity = {'source':source_identity, 'language':config.language, 'target':config.target, 'chunk_seconds':config.chunk_seconds, 'overlap_seconds':config.overlap_seconds, 'engine':config.asr_provider,'api':config.asr_api_version,'translation_provider':config.translation_provider,'translation_model':config.translation_model, 'seed_layout':2, 'seed':seed_identity, 'seed_complete_until_ms':config.seed_complete_until_ms}
    if config.asr_provider=='qwen_asr':
        settings,ledger=cloud_context(config)
        identity['asr_endpoint']=settings.asr_endpoint
        identity['asr_model']=config.asr_model
        if config.workflow_stage=='full':
            validate_full_approval(config,identity['source']['sha256'])
    seeded_raw = parse_srt(config.seed_srt.read_text(encoding='utf-8-sig')) if config.seed_srt else []
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if config.workflow_stage!='draft' and any(
                part.get('empty_recognition_requires_review') or part.get('draft_timing_requires_review')
                for part in state.get('parts',{}).values()):
            raise ValueError('项目含尚未确认无对白的草稿片段，需保持草稿模式并人工复核，不能复用为样片或已审核整片')
        if state.get('identity') != identity:
            raise ValueError('输入文件或语言/切段配置发生变化，请选择一个新的项目文件夹')
        chunks = [Chunk(**chunk) for chunk in state['chunks']]
        validate_chunk_plan(chunks,state['duration_ms'])
        if config.asr_provider=='qwen_asr' and config.workflow_stage=='sample' and state['duration_ms']>600_000:
            raise ValueError('样片模式最多10分钟；整片须从云端样片验收入口启动')
    else:
        preparing = project/'prepare.json'
        if preparing.exists():
            if json.loads(preparing.read_text(encoding='utf-8')) != identity:
                raise ValueError('这个目录正在准备不同的输入，请选择新项目')
            if any(p.name not in ('run.lock','run.guard.lock','prepare.json','silence.log') for p in project.iterdir()):
                raise ValueError('准备目录有不属于本任务的文件，请选择新项目')
        elif any(p.name not in ('run.lock','run.guard.lock') for p in project.iterdir()):
            raise ValueError('目录已有文件但不是字幕任务，请选择新的空文件夹')
        else:
            atomic_json(preparing, identity)
        progress({'status':'preparing','message':'读取音频并寻找合适的切点…','total':0,'recognized':0,'translated':0})
        try:
            duration = probe_media(config.source,stop=stop)
            if config.asr_provider=='qwen_asr' and config.workflow_stage=='sample' and duration>600_000:
                raise ValueError('样片模式最多10分钟；整片须从云端样片验收入口启动')
            silences = detect_silences(config.source, stop, project)
        except Cancelled:
            # The optional preparation log is safe to keep but must allow retry.
            (project/'silence.log').unlink(missing_ok=True)
            return {'status':'cancelled','message':'已停止准备','total':0,'recognized':0,'translated':0}
        chunks = plan_chunks(duration, round(config.chunk_seconds*1000), round(config.overlap_seconds*1000), silences)
        # An imported full subtitle already has authoritative cue boundaries.
        # Expand its virtual context rather than clipping a long cue at a cut.
        seed_cues, _ = check_cues(seeded_raw, duration)
        adjusted = []
        for chunk in chunks:
            owned = [c for c in seed_cues if 2*chunk.core_start_ms <= c.start_ms+c.end_ms < 2*chunk.core_end_ms] if chunk.core_end_ms <= config.seed_complete_until_ms else []
            adjusted.append(replace(chunk,audio_start_ms=min([chunk.audio_start_ms]+[c.start_ms for c in owned]),audio_end_ms=max([chunk.audio_end_ms]+[c.end_ms for c in owned])))
        chunks = adjusted
        validate_chunk_plan(chunks,duration)
        state = {'version':2,'identity':identity,'duration_ms':duration,'chunks':[asdict(c) for c in chunks],'parts':{},'created':time.time()}
        if config.translation_provider=='deepseek':
            # Existing projects retain their paid request identities on resume.
            state['translation_context_version']=translation_context.VERSION
    context_version=state.get('translation_context_version',0)
    if type(context_version) is not int or context_version not in (0,translation_context.VERSION):
        raise HumanEditConflict('跨段翻译上下文版本无效，请保留项目记录后核对')
    if context_version==0 and any('translation_contexts' in part or 'translation_context_bindings' in part
                                  for part in state['parts'].values()):
        raise HumanEditConflict('跨段翻译上下文策略记录缺失，不能回退到旧请求；请恢复原记录')
    contextual_translation=context_version==translation_context.VERSION and config.translation_provider=='deepseek'
    state['config'] = encoded_config
    state.pop('finalization_error',None)
    state.pop('scheduler_error',None)
    state['status'] = 'running'
    state['message'] = '准备分段处理'
    state.setdefault('review_status','unreviewed')
    if config.asr_provider=='qwen_asr' and config.workflow_stage=='draft' and state['review_status']!='needs_review':
        state['review_status']='unreviewed'
    seeded, _ = check_cues(seeded_raw,state['duration_ms'])
    total = len(chunks)

    publish_stage=None
    def publish(message=None):
        nonlocal publish_stage
        publish_stage='prepare'
        if message:
            state['message'] = message
            if state.get('manual_outputs'):
                state['message'] += '；手动修改已保留，最新自动结果在“自动更新”目录'
            if state.get('merged_source_edited'):
                state['message'] += '；合并原文修改已采用，旧译文已备份，保留的人工译文及新译文需复核'
        expected=[state['parts'].get(str(ch.index),{}) for ch in chunks]
        state.update(total=total, recognized=sum(p.get('asr')=='done' for p in expected), translated=sum(p.get('translation')=='done' for p in expected), updated=time.time())
        if config.asr_provider=='qwen_asr':
            publish_stage='cost'
            state['cost']=ledger.summary()
        publish_stage='state'
        atomic_json(state_path,state)
        publish_stage='progress'
        progress(dict(state))
        publish_stage='done'

    def folder_for(chunk):
        folder = project/'片段'/f'{chunk.index+1:04d}'
        folder.mkdir(parents=True,exist_ok=True)
        return folder

    prepared_pcm_token=None
    worker_stop=_WorkerStop(stop)

    def admit_new_asr(observed_token, observation_error):
        # Cheap exact PCM length check, after prepared timeline content binding.
        # The ledger invokes it only before a new transmission, never to recover
        # an already paid raw response. It does not prove the source time origin.
        from .audio_range import check_pcm_plan
        _check_pcm_stop(worker_stop)
        if observation_error is not None:
            raise observation_error
        if (prepared_pcm_token is None or observed_token!=prepared_pcm_token
                or _pcm_file_token(project/'timeline.wav',worker_stop)!=prepared_pcm_token):
            raise _pcm_changed()
        check_pcm_plan(project/'timeline.wav',state['duration_ms'],worker_stop)
        if _pcm_file_token(project/'timeline.wav',worker_stop)!=prepared_pcm_token:
            raise _pcm_changed()

    def asr_task(chunk):
        folder = folder_for(chunk)
        if worker_stop.is_set():
            raise Cancelled('已停止')
        if config.seed_srt and chunk.core_end_ms <= config.seed_complete_until_ms:
            cues = [Cue(c.start_ms-chunk.audio_start_ms,c.end_ms-chunk.audio_start_ms,c.text) for c in seeded if 2*chunk.core_start_ms <= c.start_ms+c.end_ms < 2*chunk.core_end_ms]
        else:
            options={}
            if config.asr_provider=='qwen_asr':
                # Observe before extraction, but defer refusal until the ledger
                # knows this is a new send, preserving same-ID paid raw recovery.
                observed_token,observation_error=None,None
                try:
                    observed_token=_pcm_file_token(project/'timeline.wav',worker_stop)
                except OSError as error:
                    observation_error=error
                options['before_submit']=lambda:admit_new_asr(observed_token,observation_error)
            cues = recognize_chunk(config,chunk,folder,worker_stop,**options)
        cues, issues = check_cues(cues,chunk.audio_end_ms-chunk.audio_start_ms)
        if config.asr_provider=='qwen_asr':
            metadata=json.loads((folder/'recognition-metadata.json').read_text(encoding='utf-8'))
            issues.extend(metadata.get('issues',[]))
        for previous,current in zip(cues,cues[1:]):
            if previous.text==current.text and current.start_ms-previous.end_ms<3000:
                issues.append({'start_ms':current.start_ms,'end_ms':current.end_ms,'reason':'相邻重复文字，请听音确认，未自动删除'})
        for cue in cues:
            absolute_mid = (cue.start_ms+cue.end_ms)//2 + chunk.audio_start_ms
            if (chunk.core_start_ms and abs(absolute_mid-chunk.core_start_ms)<=3000) or (chunk.core_end_ms<state['duration_ms'] and abs(absolute_mid-chunk.core_end_ms)<=3000):
                issues.append({'start_ms':cue.start_ms,'end_ms':cue.end_ms,'reason':'切段接缝附近，请核对原音是否漏句或重复'})
        for name in ('source.local.srt','原文.srt'):
            backup_user_file(project,folder/name)
        atomic_text(folder/'source.local.srt',render_srt(cues))
        atomic_text(folder/'原文.srt',render_srt([Cue(c.start_ms+chunk.audio_start_ms,c.end_ms+chunk.audio_start_ms,c.text) for c in cues]))
        atomic_json(folder/'需复核.json',issues)
        evidence={'asr_evidence':{'provider':config.asr_provider,'completed_at':time.time(),
                  'model':config.asr_model if config.asr_provider=='qwen_asr' else 'whisper.cpp-small-v1',
                  'api':config.asr_api_version if config.asr_provider=='qwen_asr' else 'local-cli',
                  'source_sha256':identity['source']['sha256'],
                  'language':config.language,
                  'audio_range_ms':[chunk.audio_start_ms,chunk.audio_end_ms]},'cues':len(cues),
                  'source_hash':sha256(folder/'source.local.srt'),'public_source_hash':sha256(folder/'原文.srt')}
        if config.asr_provider=='qwen_asr':
            evidence['raw_response_hash']=sha256(folder/'asr-response.json')
            if metadata.get('metadata',{}).get('empty_recognition_requires_review') is True:
                evidence['empty_recognition_requires_review']=True
            if metadata.get('metadata',{}).get('draft_timing_requires_review') is True:
                evidence['draft_timing_requires_review']=True
        return evidence

    def translation_snapshot(chunk):
        """Freeze from validated source evidence in the scheduling thread."""
        part=state['parts'][str(chunk.index)]
        folder=folder_for(chunk)
        raw=(folder/'source.local.srt').read_bytes()
        source_hash=hashlib.sha256(raw).hexdigest()
        if source_hash!=part.get('source_hash'):
            raise HumanEditConflict('翻译前原文已变化，请重新载入任务后再继续')
        saved=translation_context.existing_snapshot(part,source_hash)
        if saved is not None:
            if saved['chunk_index']!=chunk.index:
                raise HumanEditConflict('翻译上下文属于其他片段；未重新提交')
            return saved
        cues=parse_srt(raw.decode('utf-8-sig'))
        previous,previous_hash,previous_cues=None,None,[]
        if chunk.index:
            candidate=chunks[chunk.index-1]
            previous_part=state['parts'].get(str(candidate.index),{})
            previous_folder=folder_for(candidate)
            if verify_part(previous_folder,candidate,previous_part,translated=False,
                    cloud=config.asr_provider=='qwen_asr',provider=config.asr_provider,
                    model=config.asr_model,api=config.asr_api_version,
                    source_sha256=identity['source']['sha256'],language=config.language):
                candidate_raw=(previous_folder/'source.local.srt').read_bytes()
                candidate_hash=hashlib.sha256(candidate_raw).hexdigest()
                if candidate_hash==previous_part['source_hash']:
                    previous,previous_hash=candidate,candidate_hash
                    previous_cues=parse_srt(candidate_raw.decode('utf-8-sig'))
        saved=translation_context.build_snapshot(chunk,source_hash,cues,previous,previous_hash,previous_cues)
        translation_context.remember_snapshot(part,saved)
        # A crash after transmission must retain the exact context used for its ID.
        atomic_json(state_path,state)
        return saved

    def translation_task(chunk,context_snapshot=None):
        if worker_stop.is_set():
            raise Cancelled('已停止')
        folder = folder_for(chunk)
        source_bytes=(folder/'source.local.srt').read_bytes()
        source_hash=hashlib.sha256(source_bytes).hexdigest()
        cues = parse_srt(source_bytes.decode('utf-8-sig'))
        context_options={}
        if context_snapshot is not None:
            translation_context.validate_snapshot(context_snapshot,source_hash)
            context_options['context_before']=translation_context.source_texts(context_snapshot)
        if config.translation_provider=='deepseek':
            from .deepseek_translate import translate_texts as deepseek_texts
            settings,translation_ledger=cloud_context(config)
            texts=deepseek_texts([c.text for c in cues],config.language,config.target,
                project/'deepseek-translation-cache.json',ledger=translation_ledger,key=settings.deepseek_key,
                model=config.translation_model,input_rate_cny_per_million=settings.deepseek_input_rate,
                output_rate_cny_per_million=settings.deepseek_output_rate,stop_event=worker_stop,**context_options)
        else:
            texts = translate_texts([c.text for c in cues], config.language, config.target, project/'translation-cache.json',stop_event=worker_stop)
        if len(texts)!=len(cues) or any(not t.strip() for t in texts):
            raise RuntimeError('翻译结果与字幕数量不一致，未保存该段译文')
        if sha256(folder/'source.local.srt')!=source_hash:
            raise HumanEditConflict('翻译期间原文被修改；已返回的响应保留，未覆盖字幕，请重新载入任务')
        translated = [Cue(c.start_ms,c.end_ms,t) for c,t in zip(cues,texts)]
        for name in ('target.local.srt',translated_name):
            backup_user_file(project,folder/name)
        atomic_text(folder/'target.local.srt',render_srt(translated))
        atomic_text(folder/translated_name,render_srt([Cue(c.start_ms+chunk.audio_start_ms,c.end_ms+chunk.audio_start_ms,c.text) for c in translated]))
        return {'translation_source_hash':source_hash,
                'target_hash':sha256(folder/'target.local.srt'),'public_target_hash':sha256(folder/translated_name)}

    def combine(*,preserve_pending=False):
        _recover_pending_outputs(project,state)
        source_parts, all_issues, translations = [], [], {}
        for chunk in chunks:
            folder = folder_for(chunk)
            part = state['parts'].get(str(chunk.index),{})
            if part.get('asr')=='done':
                original = parse_srt((folder/'source.local.srt').read_text(encoding='utf-8-sig'))
                source_parts.append((chunk,original))
                for issue in json.loads((folder/'需复核.json').read_text(encoding='utf-8')):
                    all_issues.append({**issue,'part':chunk.index+1,'start_ms':issue['start_ms']+chunk.audio_start_ms,'end_ms':issue['end_ms']+chunk.audio_start_ms})
                if part.get('translation')=='done':
                    target = parse_srt((folder/'target.local.srt').read_text(encoding='utf-8-sig'))
                    if len(original)!=len(target) or any((a.start_ms,a.end_ms)!=(b.start_ms,b.end_ms) for a,b in zip(original,target)):
                        raise ValueError('缓存译文时间轴发生变化，请保留原文件并检查该段')
                    for a,b in zip(original,target):
                        translations.setdefault(Cue(a.start_ms+chunk.audio_start_ms,a.end_ms+chunk.audio_start_ms,a.text),b.text)
        merged = merge_chunk_cues(source_parts)
        planned={}
        hashes = state.setdefault('generated_hashes',{})
        def plan(path, text, current):
            relative=str(path.relative_to(project))
            planned[relative]=(text,hashlib.sha256(text.encode('utf-8')).hexdigest(),current)
        def generated(name, text):
            path = project/name
            manual = state.setdefault('manual_outputs',[])
            current=None
            if name not in manual:
                current=sha256(path) if path.exists() else None
                if current is not None and current!=hashes.get(name):
                    backup_user_file(project,path)
                    manual.append(name)
            if name in manual:
                path = project/'自动更新'/name
                current=sha256(path) if path.exists() else None
            relative = str(path.relative_to(project))
            if current is not None and current!=hashes.get(relative):
                backup_user_file(project,path)
            plan(path,text,current)
            state.setdefault('outputs',{})[name] = relative
        generated('原文.srt',render_srt(merged))
        if any(p.get('translation')=='done' for p in state['parts'].values()):
            generated(translated_name,render_srt([Cue(c.start_ms,c.end_ms,translations[c]) for c in merged if c in translations]))
            generated('双语草稿.srt',render_srt([Cue(c.start_ms,c.end_ms,translations[c]+'\n'+c.text) for c in merged if c in translations]))
        path=project/'需复核.json'
        plan(path,json.dumps(all_issues,ensure_ascii=False,indent=2),sha256(path) if path.exists() else None)
        def clock_stamp(value):
            seconds = max(0,value)//1000
            return f'{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}'
        lines = ['自动检查疑点（不是人工听音校对结论）',f'共有 {len(all_issues)} 项提示；同一条字幕可能有多项提示。','请在 Subtitle Edit 中配合原视频检查以下时间位置。','']
        lines.extend(f"第 {i['part']:02d} 段  {clock_stamp(i['start_ms'])}–{clock_stamp(i['end_ms'])}  {i['reason']}" for i in all_issues)
        path=project/'需复核.txt'
        plan(path,'\n'.join(lines)+'\n',sha256(path) if path.exists() else None)
        if any(digest!=current for _,digest,current in planned.values()):
            # Persist completed part records and exact publication intent before
            # replacing the first output; an abrupt exit cannot mislabel our own
            # new subtitle as a user edit or lose completed paid work.
            state['pending_outputs']={'version':1,'identity':fingerprint(state['identity']),
                'checkpoint':_output_checkpoint(state),
                'outputs':{relative:digest for relative,(_,digest,_) in planned.items()}}
            try:
                atomic_json(state_path,state)
                for relative,(text,digest,current) in planned.items():
                    if digest!=current:
                        atomic_text(project/relative,text)
            except BaseException as error:
                journal=state['pending_outputs']
                recovered=False
                try:
                    try:
                        _recover_pending_outputs(project,state)
                        recovered=True
                    except Exception as recovery_error:
                        _note_secondary_error(error,'合并输出恢复失败',recovery_error)
                finally:
                    # Final diagnostics retain the original publication intent.
                    # During scheduling, successful recovery must clear it before
                    # a later result changes the part checkpoint (legacy behavior).
                    if preserve_pending or not recovered:
                        state['pending_outputs']=journal
                raise
            state.pop('pending_outputs')
        hashes.update({relative:digest for relative,(_,digest,_) in planned.items()})

    try:
        _recover_pending_outputs(project,state)
        reconcile_merged_source(project,chunks,state,lambda path:backup_user_file(project,path),
                                atomic_text,lambda:atomic_json(state_path,state))
        state.pop('merged_source_needs_review',None)
    except (OSError,ValueError) as error:
        state['merged_source_needs_review']=True
        state['review_status']='needs_review'
        state['status']='asr_incomplete'
        publish('合并原文需要人工复核；未重新识别或翻译：'+str(error))
        return state
    publish()
    asr_jobs, translation_jobs = {}, {}
    asr_pending, translation_pending = deque(), deque()
    asr_errors, translation_errors = [], []

    def recover_owned_results(primary):
        # Both executor contexts have exited. Recover only these still-owned
        # futures, never infer completion from files or submit additional work.
        diagnostic={'type':type(primary).__name__,'message':str(primary),
                    'recovered_asr':0,'recovered_translation':0}
        state['scheduler_error']=diagnostic
        for is_asr,jobs in ((True,asr_jobs),(False,translation_jobs)):
            for future,chunk in jobs.items():
                stage=f'第 {chunk.index+1} 段'+('识别' if is_asr else '翻译')+'结果恢复'
                try:
                    if future.cancelled() or not future.done():
                        continue
                    # A stored worker interrupt is a failed result, whereas an
                    # interrupt raised by this observation is a new interruption.
                    stored_error=future.exception()
                    if stored_error is not None and not isinstance(stored_error,Exception):
                        _note_secondary_error(primary,stage,stored_error)
                        continue
                    result=future.result()
                    required=({'asr_evidence','cues','source_hash','public_source_hash'} if is_asr
                              else {'translation_source_hash','target_hash','public_target_hash'})
                    if is_asr and config.asr_provider=='qwen_asr':
                        required.add('raw_response_hash')
                    if not isinstance(result,dict) or not required.issubset(result):
                        raise ValueError('片段返回结果不完整；保留文件，未登记完成')
                    part=state['parts'][str(chunk.index)]
                    candidate=dict(part)
                    candidate.update(result)
                    if is_asr:
                        candidate['asr']='done'
                        candidate.pop('translation',None)
                        candidate.pop('error',None)
                    else:
                        candidate['translation']='done'
                        candidate.pop('translation_error',None)
                    folder=folder_for(chunk)
                    # Deliberately omit stop: these completed local results
                    # must retain their proof even when the task was stopped.
                    if not verify_part(folder,chunk,candidate,translated=not is_asr,
                            cloud=config.asr_provider=='qwen_asr',provider=config.asr_provider,
                            model=config.asr_model,api=config.asr_api_version,
                            source_sha256=identity['source']['sha256'],language=config.language):
                        raise ValueError('片段内容或来源证明不匹配；保留文件，未登记完成')
                    # verify_part covers local files; public copies can have
                    # independent human edits since the worker completed.
                    if sha256(folder/'原文.srt')!=candidate.get('public_source_hash'):
                        raise ValueError('片段公开原文已变化；保留文件，未登记完成')
                    if not is_asr and sha256(folder/translated_name)!=candidate.get('public_target_hash'):
                        raise ValueError('片段公开译文已变化；保留文件，未登记完成')
                    if is_asr:
                        count=candidate['cues']
                        if (type(count) is not int or count!=len(parse_srt(
                                (folder/'source.local.srt').read_text(encoding='utf-8-sig')))):
                            raise ValueError('片段字幕数量与返回结果不一致；保留文件，未登记完成')
                    part.clear()
                    part.update(candidate)
                    diagnostic['recovered_asr' if is_asr else 'recovered_translation']+=1
                except Exception as error:
                    _note_secondary_error(primary,stage,error)

    preparation_cancelled = False
    body_failed=False
    try:
        with futures.ThreadPoolExecutor(max_workers=config.workers) as asr_pool, futures.ThreadPoolExecutor(max_workers=1) as translate_pool, _abort_workers_on_error(worker_stop):
            for chunk in chunks:
                part = state['parts'].setdefault(str(chunk.index),{})
                folder = folder_for(chunk)
                if part.get('asr') in ('done','needs_review'):
                    try:
                        parse_srt((folder/'source.local.srt').read_text(encoding='utf-8-sig'))
                        json.loads((folder/'需复核.json').read_text(encoding='utf-8'))
                        if (not evidence_matches(part,chunk,cloud=config.asr_provider=='qwen_asr',
                            provider=config.asr_provider,model=config.asr_model,api=config.asr_api_version,
                            source_sha256=identity['source']['sha256'],language=config.language) or not part.get('source_hash')):
                            raise ValueError('旧缓存没有识别来源证明')
                        changed=reconcile_source(folder,chunk,part,
                            lambda path:backup_user_file(project,path),atomic_text)
                        if changed:
                            state['review_status']='needs_review'
                        if config.asr_provider=='qwen_asr' and sha256(folder/'asr-response.json')!=part.get('raw_response_hash'):
                            raise ValueError('识别原始结果缺失或被修改')
                        part['asr']='done'
                        part.pop('error',None)
                    except (OSError,ValueError):
                        if (part.get('asr')=='needs_review'
                            or part.get('source_hash') and (folder/'source.local.srt').exists()):
                            part['error']='缓存或人工修改需要核对；未覆盖、未重新付费识别'
                            asr_errors.append(part['error'])
                            state['review_status']='needs_review'
                            part['asr']='needs_review'
                            continue
                        # Rebuilding missing ASR files must not forget a paid
                        # translation's frozen context. Preserve even malformed
                        # history so validation blocks instead of changing IDs.
                        context_history={key:part[key] for key in
                            ('translation_contexts','translation_context_bindings') if key in part}
                        part.clear()
                        part.update(context_history)
                if part.get('translation') in ('done','needs_review'):
                    try:
                        if part.get('translation_source_hash')!=part.get('source_hash'):
                            raise ValueError('源文内容变化，旧译文失效')
                        changed=reconcile_translation(folder,chunk,part,
                            lambda path:backup_user_file(project,path),atomic_text,target=config.target)
                        target = parse_srt((folder/'target.local.srt').read_text(encoding='utf-8-sig'))
                        source = parse_srt((folder/'source.local.srt').read_text(encoding='utf-8-sig'))
                        if len(target)!=len(source) or any((a.start_ms,a.end_ms)!=(b.start_ms,b.end_ms) for a,b in zip(source,target)):
                            raise ValueError('cached translation timeline mismatch')
                        if changed:
                            state['review_status']='needs_review'
                        part['translation']='done'
                        part.pop('translation_error',None)
                    except HumanEditConflict as exc:
                        part['translation']='needs_review'
                        part['translation_error']=str(exc)
                        translation_errors.append(str(exc))
                        state['review_status']='needs_review'
                        continue
                    except (OSError,ValueError):
                        if part.get('translation')=='needs_review':
                            part['translation_error']='人工译文冲突尚未解决；未覆盖、未重新翻译'
                            translation_errors.append(part['translation_error'])
                            state['review_status']='needs_review'
                            continue
                        backup_user_file(project,folder/'target.local.srt')
                        part.pop('translation',None)
                if part.get('asr')!='done':
                    asr_pending.append(chunk)
                elif config.translate and part.get('translation')!='done':
                    translation_pending.append(chunk)
            if config.asr_provider=='qwen_asr' and asr_pending and not stop.is_set():
                # Validate cached work first: translation-only and completed
                # resumes do not need a decoded copy of the full source.
                # Persist reconciliation before extraction can leave partial
                # files, then persist the prepared hash before submitting ASR.
                publish()
                try:
                    prepared_pcm_token=prepare_cloud_audio(config,state,stop)
                except Cancelled:
                    preparation_cancelled = True
                    asr_pending.clear()
                    translation_pending.clear()
                else:
                    publish()
            while asr_pending or translation_pending or asr_jobs or translation_jobs:
                if stop.is_set():
                    asr_pending.clear()
                    translation_pending.clear()
                    for future in list(asr_jobs)+list(translation_jobs):
                        future.cancel()
                # Keep the executors' queues bounded by their active slots.
                # Unscheduled chunks remain cheap references and are discarded
                # immediately on cancellation, without creating more futures.
                while asr_pending and len(asr_jobs)<config.workers and not stop.is_set():
                    chunk=asr_pending.popleft()
                    asr_jobs[asr_pool.submit(asr_task,chunk)] = chunk
                while translation_pending and not translation_jobs and not stop.is_set():
                    if contextual_translation:
                        pending_asr={ch.index for ch in asr_pending}|{ch.index for ch in asr_jobs.values()}
                        selected=None
                        for candidate in sorted(translation_pending,key=lambda ch:ch.index):
                            part=state['parts'][str(candidate.index)]
                            # A previous failed/missing neighbor does not block work.
                            # Once frozen, even an empty snapshot survives its recovery.
                            frozen=(part.get('source_hash') in part.get('translation_context_bindings',{})
                                    if isinstance(part.get('translation_context_bindings',{}),dict) else True)
                            if frozen or candidate.index-1 not in pending_asr:
                                selected=candidate
                                break
                        if selected is None:break
                        chunk=selected
                        translation_pending.remove(chunk)
                        try:
                            context_snapshot=translation_snapshot(chunk)
                        except (OSError,ValueError) as exc:
                            part=state['parts'][str(chunk.index)]
                            part['translation_error']=str(exc)
                            state['review_status']='needs_review'
                            translation_errors.append(str(exc))
                            publish(f'第 {chunk.index+1} 段翻译上下文需要核对：{exc}')
                            continue
                    else:
                        chunk=translation_pending.popleft()
                        context_snapshot=None
                    translation_jobs[translate_pool.submit(translation_task,chunk,context_snapshot)] = chunk
                if not asr_jobs and not translation_jobs:
                    break
                done,_ = futures.wait(list(asr_jobs)+list(translation_jobs), timeout=.3, return_when=futures.FIRST_COMPLETED)
                for future in done:
                    is_asr = future in asr_jobs
                    chunk = (asr_jobs if is_asr else translation_jobs).pop(future)
                    part = state['parts'][str(chunk.index)]
                    try:
                        result = future.result()
                        if is_asr:
                            part.update(result)
                            part.update(asr='done')
                            part.pop('translation',None)
                            part.pop('error',None)
                            if config.translate and not stop.is_set():
                                translation_pending.append(chunk)
                        else:
                            part.update(result)
                            part['translation']='done'
                            part.pop('translation_error',None)
                        combine()
                        publish(f'第 {chunk.index+1}/{total} 段：'+('识别、检查已保存' if is_asr else '译文已保存'))
                    except (Cancelled,futures.CancelledError):
                        pass
                    except Exception as exc:
                        if stop.is_set():
                            continue
                        key = 'error' if is_asr else 'translation_error'
                        part[key] = str(exc)
                        (asr_errors if is_asr else translation_errors).append(str(exc))
                        publish(f'第 {chunk.index+1} 段未完成：{exc}')
                if stop.is_set() and not done:
                    continue
    except BaseException:
        body_failed=True
        raise
    finally:
        # sys.exception() alone can be an unrelated caller's handled exception.
        primary=sys.exception() if body_failed else None
        scheduler_failed=isinstance(primary,Exception)
        stage='recover'
        try:
            if scheduler_failed:
                recover_owned_results(primary)
            stage='combine'
            combine(preserve_pending=True)
            if preparation_cancelled or stop.is_set():
                state['status'] = 'cancelled'
            elif scheduler_failed:
                # Candidate proofs above are bounded by owned worker slots.
                # A scheduler fault is not completion, even if all were saved.
                state['status'] = 'asr_incomplete'
            else:
                stage='verify'
                asr_valid=all(verify_part(folder_for(ch),ch,state['parts'].get(str(ch.index),{}),
                                         translated=False,cloud=config.asr_provider=='qwen_asr',provider=config.asr_provider,
                                         model=config.asr_model,api=config.asr_api_version,
                                         source_sha256=identity['source']['sha256'],language=config.language) for ch in chunks)
                translation_valid=not config.translate or all(verify_part(folder_for(ch),ch,state['parts'].get(str(ch.index),{}),
                                         translated=True,cloud=config.asr_provider=='qwen_asr',provider=config.asr_provider,
                                         model=config.asr_model,api=config.asr_api_version,
                                         source_sha256=identity['source']['sha256'],language=config.language) for ch in chunks)
                state['status'] = ('cancelled' if stop.is_set() else 'asr_incomplete' if asr_errors or not asr_valid else 'translation_incomplete' if translation_errors or not translation_valid else 'complete')
            stage='publish'
            message={'complete':'处理完成；译文为草稿，请查看需复核清单','cancelled':'已停止，已完成片段已保存；可继续','asr_incomplete':'部分识别失败；已完成片段保留，请查看状态并继续','translation_incomplete':'识别已保存，部分翻译失败；可稍后继续'}[state['status']]
            if scheduler_failed:
                message=('已停止；' if state['status']=='cancelled' else '')+'调度中断，已核验的完成片段已保存；任务未完成，可继续'
            publish(message)
        except Exception as error:
            failure_stage=f'publish_{publish_stage}' if stage=='publish' else stage
            failure=primary if primary is not None else error
            if primary is not None:
                _note_secondary_error(primary,f'任务收尾 {failure_stage} 失败',error)
            state['status']='cancelled' if preparation_cancelled or stop.is_set() else 'asr_incomplete'
            saved_before_error=failure_stage=='publish_progress'
            state['finalization_error']={'stage':failure_stage,'type':type(error).__name__,
                'message':str(error),'final_state_saved_before_error':saved_before_error}
            state['message']=('字幕与任务状态已保存，但进度通知失败；未确认本次收尾完成' if saved_before_error
                              else '字幕合并或任务收尾保存失败；已有文件和恢复记录保留，请重试')
            expected=[state['parts'].get(str(ch.index),{}) for ch in chunks]
            state.update(total=total,recognized=sum(p.get('asr')=='done' for p in expected),
                         translated=sum(p.get('translation')=='done' for p in expected),updated=time.time())
            try:
                # One diagnostic write only: no ledger, callback or combine retry.
                atomic_json(state_path,state)
            except Exception as diagnostic_error:
                _note_secondary_error(failure,f'收尾诊断保存失败：{state_path}',diagnostic_error)
            if primary is None:
                raise
    return state


def main():
    parser=argparse.ArgumentParser(description='本地分段字幕流水线')
    parser.add_argument('source',type=Path)
    parser.add_argument('--project',required=True,type=Path)
    parser.add_argument('--language',default='ja')
    parser.add_argument('--target',default='zh-CN')
    parser.add_argument('--chunk-seconds',type=float,default=300)
    parser.add_argument('--overlap-seconds',type=float,default=2)
    parser.add_argument('--workers',type=int,choices=(1,2),default=1)
    parser.add_argument('--threads',type=int,default=6)
    parser.add_argument('--no-translate',action='store_true')
    parser.add_argument('--seed-srt',type=Path)
    parser.add_argument('--seed-until-ms',type=int,default=0)
    args=parser.parse_args()
    config=PipelineConfig(args.source,args.project,args.language,args.target,args.chunk_seconds,args.overlap_seconds,args.workers,args.threads,not args.no_translate,args.seed_srt,args.seed_until_ms)
    stop=threading.Event()
    import signal
    signal.signal(signal.SIGINT,lambda *a: stop.set())
    result=run_pipeline(config,stop,on_progress=lambda state: print(json.dumps({k:state.get(k) for k in ('status','total','recognized','translated','message')},ensure_ascii=False),flush=True))
    return 0 if result['status']=='complete' else 2


if __name__=='__main__':
    raise SystemExit(main())
