"""Staged local preparation, paid pilot, explicit review, full run and export."""
from __future__ import annotations
import argparse
from dataclasses import asdict, replace
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from fractions import Fraction

from . import runner as r
from .integrity import fingerprint, sha256, verify_part, sample_state_evidence, valid_translation_content
from .subtitles import Cue, Chunk, parse_srt, render_srt, plan_chunks, merge_chunk_cues
from .cloud_settings import load_settings, readiness
from . import media_export
from .media_export import audio_digest, audio_digests, _publish_video, _publication_evidence
from .export_io import (cancellable_sha256, cancellable_copy, copy_and_hash,
                        rename_with_retry, unlink_with_retry)
from .local_process import capture_process
from .languages import (SOURCE_LABELS, TARGET_LABELS, default_target, manifest_languages,
                        output_names, target_filename, validate_languages, video_output_path)

WINDOWS=((30,120),(1680,1800),(5400,5490))


def sample_windows(duration_ms):
    """Choose at most five minutes without overlapping or leaving the source."""
    if duration_ms >= WINDOWS[-1][1] * 1000:
        return WINDOWS
    if duration_ms <= 300000:
        return ((0, duration_ms / 1000),)
    middle_start = (duration_ms - 120000) // 2
    return ((0, 90), (middle_start / 1000, (middle_start + 120000) / 1000),
            ((duration_ms - 90000) / 1000, duration_ms / 1000))


def emit(message, **data):
    print(json.dumps({'message':message,**data},ensure_ascii=False),flush=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _stop_files(campaign):
    """Keep a Studio job's stop signal separate from the legacy reusable flag."""
    paths=(Path(campaign).resolve()/'STOP.flag',)
    studio_stop=os.environ.get('SUBTITLE_STUDIO_STOP_FILE','').strip()
    return paths+(Path(studio_stop).resolve(),) if studio_stop else paths


def write_manifest(campaign,manifest):
    manifest['updated_at']=time.time()
    r.atomic_json(campaign/'campaign.json',manifest)


def validate_review(ready,reviewed,timing_passed,content_passed):
    if not ready:
        raise ValueError('样片尚未全部识别和翻译完成')
    if (isinstance(reviewed,bool) or isinstance(timing_passed,bool)
        or not isinstance(reviewed,int) or not isinstance(timing_passed,int)
        or reviewed<20 or not 0<=timing_passed<=reviewed or timing_passed/reviewed<.9):
        raise ValueError('需抽查至少20句，且至少90%的起止时间偏差在0.5秒内')
    if content_passed is not True:
        raise ValueError('必须由听看样片的人确认内容通过，程序不能代替听音校对')


def _baseline_name(language):
    return '旧日语.srt' if language=='ja' else '旧原文.srt'


def render_review(samples,*,language='ja',target='zh-CN'):
    validate_languages(language,target)
    source_label=html.escape(SOURCE_LABELS[language])
    target_label=html.escape(TARGET_LABELS[target])
    body=[]
    for n,sample in enumerate(samples):
        def cues(key):
            return [{'start':c.start_ms/1000,'end':c.end_ms/1000,'text':c.text}
                    for c in sample[key]]
        data=json.dumps({k:cues(k) for k in ('old','new','zh')},ensure_ascii=False).replace('<','\\u003c')
        media_tag='audio' if sample.get('media_kind')=='audio' else 'video'
        body.append(f'''<section><h2>{html.escape(sample["name"])} · 原片 {sample["start"]} 秒起</h2>
<{media_tag} controls preload="metadata" src="{html.escape(sample["video"],quote=True)}" data-index="{n}"></{media_tag}>
<div class="columns"><article><h3>旧{source_label}</h3><p id="old{n}"></p></article>
<article><h3>千问 新{source_label}</h3><p id="new{n}"></p></article>
<article><h3>DeepSeek {target_label}</h3><p id="zh{n}"></p></article></div>
<script type="application/json" id="data{n}">{data}</script></section>''')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>字幕样片对照</title><style>body{font:16px/1.6 "Microsoft YaHei",sans-serif;background:#f4f6f8;color:#172b40;max-width:1120px;margin:30px auto;padding:0 20px}
section{background:white;border-radius:14px;padding:24px;margin:24px 0}video,audio{width:100%;max-height:460px;background:#111}.columns{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}article{background:#f4f6f8;padding:12px;border-radius:8px}p{white-space:pre-wrap;min-height:70px}h3{font-size:15px;color:#355577}@media(max-width:700px){.columns{grid-template-columns:1fr}}</style>
<h1>同一原音，三列字幕对照</h1><p>播放时同步显示当前句。新字幕缺失表示尚未完成云端处理，不是“这段没有对白”。旧字幕只用于比较。
请检查清晰对白是否漏句、改义或重复；至少记录20句，其中至少90%的起止偏差不超过0.5秒。这里不会自动声称识别准确。</p>''' + ''.join(body) + '''
<script>
document.querySelectorAll('video,audio').forEach(video=>{
const n=video.dataset.index,data=JSON.parse(document.getElementById('data'+n).textContent);
function show(){for(const k of ['old','new','zh']){
 const active=data[k].filter(c=>c.start<=video.currentTime&&video.currentTime<c.end);
 document.getElementById(k+n).textContent=active.map(c=>c.text).join('\\n')||
 (data[k].length?'（当前无字幕）':'（尚未生成）');
}}video.addEventListener('timeupdate',show);show();});
</script></html>'''


def verify_source(manifest,stop=None):
    source=Path(manifest['source']['path'])
    if not source.is_file():
        raise ValueError('原视频内容已改变或缺失；请新建项目，不复用旧缓存')
    digest=sha256(source) if stop is None else cancellable_sha256(source,stop)
    if digest!=manifest['source']['sha256']:
        raise ValueError('原始视频已变化或不存在，请建立新项目')
    return source


def build_review(campaign,manifest):
    samples=[]
    language,target=manifest_languages(manifest)
    audio_only=manifest.get('source_kind')=='audio'
    for sample in manifest['samples']:
        folder=campaign/sample['folder']
        job=folder/'识别任务'
        def load(path):
            return parse_srt(path.read_text(encoding='utf-8-sig')) if path.exists() else []
        samples.append({'name':sample['name'],'start':sample['start_sec'],
            'video':(Path(sample['folder'])/('input.wav' if audio_only else 'preview.mp4')).as_posix(),
            'media_kind':'audio' if audio_only else 'video',
            'old':load(folder/_baseline_name(language)),'new':load(job/'原文.srt'),
            'zh':load(job/target_filename(target))})
    r.atomic_text(campaign/'review.html',render_review(samples,language=language,target=target))


def budget_ledger_path(campaign,manifest=None):
    campaign=Path(campaign).resolve()
    if manifest is None:
        path=campaign/'campaign.json'
        manifest=read_json(path) if path.exists() else {}
    if 'budget_ledger' not in manifest:
        return campaign/'费用账本.json'
    value=manifest['budget_ledger']
    if not isinstance(value,str) or not value or not Path(value).is_absolute():
        raise ValueError('项目费用账本路径无效，必须保留原账本的绝对路径')
    return Path(value).resolve()


def prepare(source,campaign,baseline,stop,*,budget_ledger=None,language='ja',target=None):
    target=default_target(language) if target is None else target
    validate_languages(language,target)
    if stop.is_set():raise r.Cancelled('已停止样片准备')
    source=Path(source).resolve()
    requested_ledger=Path(budget_ledger).resolve() if budget_ledger is not None else None
    existing=campaign/'campaign.json'
    if existing.exists():
        manifest=read_json(existing)
        if manifest_languages(manifest)!=(language,target):
            raise ValueError('已有项目不能更改原文或译文语言，请建立新项目以保留原字幕和进度')
        if requested_ledger is not None and requested_ledger!=budget_ledger_path(campaign,manifest):
            raise ValueError('已有项目不能更换费用账本；请保留原账本以延续支出和预留记录')
        if Path(manifest['source']['path'])!=source:
            raise ValueError('此项目属于不同视频')
        verify_source(manifest,stop)
        cached=[]
        for sample in manifest['samples']:
            folder=campaign/sample['folder']
            cached.extend(((folder/'input.wav',sample.get('audio_hash')),
                           (folder/'preview.mp4',sample.get('preview_hash'))))
        for path,expected in cached:
            if expected and path.exists() and cancellable_sha256(path,stop)!=expected:
                raise ValueError(f'已保存的媒体发生变化：{path.name}；原文件和识别结果已保留，请核对或建立新项目')
    else:
        if any(p.name not in ('run.lock','run.guard.lock') for p in campaign.iterdir()):
            raise ValueError('项目目录已有非本任务文件，请选择新的空目录')
        duration=r.probe_media(source,stop=stop)
        tracks=media_info(source,stop).get('streams',[])
        kinds={track.get('codec_type') for track in tracks if isinstance(track,dict)}
        if 'audio' not in kinds:
            raise ValueError('素材没有可识别的音频轨，请选择带声音的视频或音频文件')
        manifest={'version':1,'source':r.file_identity(source,stop=stop),'duration_ms':duration,
            'language':language,'target':target,
            'source_kind':'video' if 'video' in kinds else 'audio',
            'baseline':str(Path(baseline).resolve()) if baseline else None,
            'status':'preparing','budget_cny':20,'stop_cny':18,'asr_api_version':'dashscope-v1',
            'asr_provider':'qwen_asr','asr_model':'qwen-audio-3.1-asr-flash',
            'translation_model':'deepseek-flash','samples':[
                {'name':f'样片 {i+1}','folder':f'样片/{i+1:02d}','start_sec':a,'end_sec':b}
                for i,(a,b) in enumerate(sample_windows(duration))]}
        if requested_ledger is not None:
            manifest['budget_ledger']=str(requested_ledger)
        write_manifest(campaign,manifest)
    whole=campaign/'源音频.wav'
    expected=manifest.get('timeline_hash')
    cached_audio=bool(expected) and whole.exists()
    if cached_audio and cancellable_sha256(whole,stop)!=expected:
        raise ValueError(f'已保存的媒体发生变化：{whole.name}；原文件和识别结果已保留，请核对或建立新项目')
    if not cached_audio:
        emit('正在提取保持原视频时间轴的音频（本地，不收费）')
        partial=campaign/'源音频.partial.wav'
        r.run_process(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-i',source,
            '-map','0:a:0','-vn','-af','aresample=async=1:first_pts=0','-ac','1','-ar','16000',
            '-c:a','pcm_s16le','-threads','1','-y',partial],
            campaign/'extract.log',stop)
        if abs(r.probe_media(partial,stop=stop)-manifest['duration_ms'])>1000:
            raise ValueError('音频时间轴与视频不一致，未启动云端任务')
        digest=cancellable_sha256(partial,stop)
        if stop.is_set():raise r.Cancelled('已停止样片准备，提取音频已保留')
        os.replace(partial,whole)
        manifest['timeline_hash']=digest
        write_manifest(campaign,manifest)
    baseline_path=Path(manifest['baseline']) if manifest.get('baseline') else None
    old=parse_srt(baseline_path.read_text(encoding='utf-8-sig')) if baseline_path and baseline_path.exists() else []
    for sample in manifest['samples']:
        if stop.is_set():raise r.Cancelled('已停止样片准备')
        folder=campaign/sample['folder'];folder.mkdir(parents=True,exist_ok=True)
        a,b=sample['start_sec'],sample['end_sec']
        audio=folder/'input.wav'
        if not audio.exists() or cancellable_sha256(audio,stop)!=sample.get('audio_hash'):
            r.run_process(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-ss',str(a),'-i',whole,
                '-t',str(b-a),'-c:a','pcm_s16le','-y',audio],folder/'extract.log',stop)
            sample['audio_hash']=cancellable_sha256(audio,stop)
            write_manifest(campaign,manifest)
        preview=folder/'preview.mp4'
        if manifest.get('source_kind')!='audio' and preview.exists():
            digest=cancellable_sha256(preview,stop)
            if not _preview_proof_matches(sample,digest,b-a):
                # A legacy hash proves bytes, not that an encoder produced video.
                # Revalidate locally, preserving the old file even on failure.
                if manifest['status']=='prepared':
                    manifest['status']='preparing';write_manifest(campaign,manifest)
                pending=folder/'preview.partial.mp4'
                if not sample.get('preview_hash') and pending.exists():
                    # A failed no-clobber publication can leave a foreign target.
                    # Readability alone must not make that race winner our cache.
                    if cancellable_sha256(pending,stop)!=digest:
                        raise ValueError('预览文件来源冲突：目标与待保存副本不同；两份文件已保留，请核对后继续')
                proof=_checked_preview(preview,b-a,stop,expected_hash=digest)
                if stop.is_set():raise r.Cancelled('已停止预览检查，文件已保留')
                sample.update(preview_hash=proof['sha256'],preview_validation=proof)
                write_manifest(campaign,manifest)
        elif manifest.get('source_kind')!='audio':
            emit(sample['name']+'：生成本地预览视频')
            common=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-ss',str(a),'-i',source,
                '-t',str(b-a),'-map','0:v:0','-map','0:a:0','-vf','setpts=PTS-STARTPTS',
                '-af','asetpts=PTS-STARTPTS','-threads','4']
            partial=folder/'preview.partial.mp4'
            try:
                r.run_process(common+['-c:v','h264_qsv','-global_quality','22','-preset','fast',
                    '-c:a','aac','-b:a','128k','-movflags','+faststart','-y',partial],folder/'preview.log',stop)
            except RuntimeError as error:
                if isinstance(error,r.Cancelled):raise
                r.run_process(common+['-c:v','libx264','-preset','ultrafast','-crf','22',
                    '-c:a','aac','-b:a','128k','-movflags','+faststart','-y',partial],folder/'preview-cpu.log',stop)
            # Validation failures are not encoder failures: do not run another
            # encoder on the same bad timeline or replace a usable old preview.
            proof=_checked_preview(partial,b-a,stop)
            if stop.is_set():raise r.Cancelled('已停止预览检查，文件已保留')
            _publish_preview(partial,preview,stop,expected_hash=proof['sha256'])
            if stop.is_set():raise r.Cancelled('已停止预览确认，已保存的文件保留')
            sample.update(preview_hash=proof['sha256'],preview_validation=proof)
            write_manifest(campaign,manifest)
        if not (folder/_baseline_name(language)).exists():
            start_ms,end_ms=round(a*1000),round(b*1000)
            clipped=[Cue(max(c.start_ms,start_ms)-start_ms,min(c.end_ms,end_ms)-start_ms,c.text)
                     for c in old if c.end_ms>start_ms and c.start_ms<end_ms]
            r.atomic_text(folder/_baseline_name(language),render_srt(clipped))
    if stop.is_set():raise r.Cancelled('已停止样片准备，已完成文件保留')
    if manifest['status']=='preparing':
        manifest['status']='prepared'
        write_manifest(campaign,manifest)
    build_review(campaign,manifest)
    if stop.is_set():raise r.Cancelled('已停止样片准备，已完成文件保留')
    sample_seconds=sum(s['end_sec']-s['start_sec'] for s in manifest['samples'])
    emit(f'样片准备完成（{len(manifest["samples"])}段，共{sample_seconds:g}秒）；本步骤未调用千问或DeepSeek',status=manifest['status'])
    return manifest


def config_for(source,project,campaign,*,full=False):
    manifest=read_json(campaign/'campaign.json')
    language,target=manifest_languages(manifest)
    return r.PipelineConfig(Path(source),project,language=language,target=target,
        workers=2,chunk_seconds=120,asr_provider='qwen_asr',
        translation_provider='deepseek',budget_ledger=budget_ledger_path(campaign),
        workflow_stage='full' if full else 'sample',approval_path=campaign/'approval.json' if full else None)


def _check_workflow_stop(stop,cause=None):
    if stop is not None and stop.is_set():
        raise r.Cancelled('已停止处理，已完成的字幕和记录保留') from cause


def state_is_complete(project,*,language='ja',target='zh-CN',stop=None,expected_source=None):
    _check_workflow_stop(stop)
    try:
        options={'language':language,'target':target}
        if stop is not None:options['stop']=stop
        if expected_source is not None:options['expected_source']=expected_source
        sample_state_evidence(project,**options)
        _check_workflow_stop(stop)
        return True
    except (OSError,ValueError,KeyError,TypeError) as error:
        _check_workflow_stop(stop,error)
        if expected_source is not None:
            from .integrity import SourceIdentityMismatch
            if isinstance(error,(SourceIdentityMismatch,OSError)):raise
        return False


def cost_report(campaign):
    from .cloud_budget import BudgetLedger
    summary=BudgetLedger(budget_ledger_path(campaign)).summary()
    r.atomic_json(campaign/'费用记录.json',summary)
    return summary


def sample_review_media(campaign,manifest,sample):
    """Require exact sample audio; only probed audio sources omit video proof."""
    folder=campaign/sample['folder']
    audio=folder/'input.wav'
    if not audio.is_file() or sha256(audio)!=sample.get('audio_hash'):
        raise ValueError('样片音频发生变化或缺失，请重新准备并核对')
    if manifest.get('source_kind')=='audio':
        return audio
    preview=folder/'preview.mp4'
    if not preview.is_file():
        raise ValueError('样片预览视频发生变化或缺失，请重新准备并核对')
    digest=sha256(preview)
    if not _preview_proof_matches(sample,digest,sample['end_sec']-sample['start_sec']):
        raise ValueError('样片预览尚未通过本地验证，请先继续准备；未启动付费识别')
    return preview


def _has_unresolved_draft_parts(state):
    return any(part.get('empty_recognition_requires_review') or part.get('draft_timing_requires_review')
               for part in state.get('parts',{}).values())


def _samples_unchanged(campaign,manifest,stop=None):
    """Permit a local-only resume only with intact saved generation records."""
    _check_workflow_stop(stop)
    if not manifest['samples']:
        return False
    content_hash=sha256 if stop is None else lambda path:cancellable_sha256(path,stop)
    try:
        language,target=manifest_languages(manifest)
        for sample in manifest['samples']:
            _check_workflow_stop(stop)
            folder=campaign/sample['folder']
            project=folder/'识别任务'
            proof=sample_state_evidence(project,source_path=folder/'input.wav',language=language,target=target,stop=stop)
            if proof['source_sha256']!=sample.get('audio_hash'):
                return False
            state=read_json(project/'state.json')
            if state.get('manual_outputs') or state.get('merged_source_edit') or _has_unresolved_draft_parts(state):
                return False
            artifacts=[]
            for name in output_names(target):
                path=project/name
                saved=state.get('generated_hashes',{}).get(name)
                if not saved or content_hash(path)!=saved:
                    return False
                artifacts.append({'path':str(path.resolve()),'sha256':saved})
            for chunk in state['chunks']:
                part=state['parts'][str(chunk['index'])]
                part_folder=project/'片段'/f"{chunk['index']+1:04d}"
                if any(content_hash(part_folder/name)!=part.get(field) for name,field in (
                        ('原文.srt','public_source_hash'),(target_filename(target),'public_target_hash'))):
                    return False
            validate_public_subtitles(project,artifacts,language=language,target=target)
            if content_hash(project/'state.json')!=proof['sha256']:
                return False
        _check_workflow_stop(stop)
        return True
    except (OSError,ValueError,KeyError,TypeError) as error:
        _check_workflow_stop(stop,error)
        return False


def run_samples(campaign,stop):
    manifest=read_json(campaign/'campaign.json');verify_source(manifest,stop)
    language,target=manifest_languages(manifest)
    for sample in manifest['samples']:
        _check_workflow_stop(stop)
        sample_review_media(campaign,manifest,sample)
    if _samples_unchanged(campaign,manifest,stop=stop):
        manifest.update(status='samples_ready',cost=cost_report(campaign))
        _check_workflow_stop(stop)
        write_manifest(campaign,manifest);build_review(campaign,manifest)
        _check_workflow_stop(stop)
        emit('样片缓存完整且未修改，已恢复验收入口；本次未调用云端服务',status='samples_ready')
        return True
    settings=load_settings()
    manifest['pricing']=settings.public_config()
    estimated=sum(math.ceil((s['end_sec']-s['start_sec'])/120) for s in manifest['samples'])*asr_reservation(settings)+1
    if cost_report(campaign)['committed_cny']+estimated>=18:
        raise ValueError('按当前单价，样片预计费用超过剩余追加额度')
    for sample in manifest['samples']:
        if stop.is_set():break
        folder=campaign/sample['folder']
        if sha256(folder/'input.wav')!=sample.get('audio_hash'):
            raise ValueError('样片音频发生变化，请重新准备并核对')
        emit(sample['name']+'：开始千问识别和DeepSeek翻译（计入20元总预算）')
        config=config_for(folder/'input.wav',folder/'识别任务',campaign)
        r.run_pipeline(config,stop,on_progress=lambda s:emit(s.get('message','处理中'),
            status=s.get('status'),recognized=s.get('recognized'),translated=s.get('translated')))
        build_review(campaign,manifest)
    _check_workflow_stop(stop)
    ready=all(state_is_complete(campaign/s['folder']/'识别任务',language=language,target=target,stop=stop)
              for s in manifest['samples'])
    manifest['status']='samples_ready' if ready else 'samples_incomplete'
    manifest['cost']=cost_report(campaign)
    _check_workflow_stop(stop)
    write_manifest(campaign,manifest);build_review(campaign,manifest)
    _check_workflow_stop(stop)
    emit('样片生成完成，等待你听看验收；整片尚未启动' if ready else '样片未全部完成，请查看各段状态',
         status=manifest['status'])
    return ready


def artifacts_for(campaign,manifest,*,full=False):
    language,target=manifest_languages(manifest)
    paths=[]
    if full:
        paths=[campaign/'整片'/name for name in (*output_names(target),'需复核.json')]
    else:
        for sample in manifest['samples']:
            folder=campaign/sample['folder']
            paths.extend([sample_review_media(campaign,manifest,sample),folder/_baseline_name(language),
                          folder/'识别任务'/'原文.srt',folder/'识别任务'/target_filename(target)])
    return [{'path':str(path.resolve()),'sha256':sha256(path)} for path in paths]


def validate_public_subtitles(project,artifacts,*,language='ja',target='zh-CN'):
    """Validate the exact public files being reviewed, including human wording."""
    state=read_json(project/'state.json')
    validate_languages(language,target)
    if manifest_languages(state.get('identity',{}))!=(language,target):
        raise ValueError('字幕语言与项目不一致，请建立新项目或恢复原语言设置')
    target_name=target_filename(target)
    expected={Path(item['path']).resolve():item['sha256'] for item in artifacts}
    parsed={}
    hashes={}
    for name in ('原文.srt',target_name):
        path=project/name
        data=path.read_bytes()
        hashes[name]=hashlib.sha256(data).hexdigest()
        if expected.get(path.resolve())!=hashes[name]:
            raise ValueError('公开字幕在验收或导出过程中发生变化，请重新核对')
        parsed[name]=parse_srt(data.decode('utf-8-sig'))
    chunks=[Chunk(**value) for value in state['chunks']]
    baseline=merge_chunk_cues([(chunk,parse_srt(
        (project/'片段'/f'{chunk.index+1:04d}'/'source.local.srt').read_text(encoding='utf-8-sig')))
        for chunk in chunks])
    original,translated=parsed['原文.srt'],parsed[target_name]
    if original!=baseline:
        raise ValueError('合并原文与已验证片段不一致，请先继续任务协调人工修改后再验收')
    if len(original)!=len(translated) or any(
            not 0<=a.start_ms<a.end_ms<=state['duration_ms']
            or (a.start_ms,a.end_ms)!=(b.start_ms,b.end_ms)
            or not valid_translation_content(a.text,b.text) for a,b in zip(original,translated)):
        raise ValueError('公开译文缺句、缺少实际内容或时间轴无效，请核对后再验收')
    return {'cue_count':len(original),'captions_sha256':hashes[target_name]}


def approve(campaign,reviewed,timing_passed,content_passed):
    manifest=read_json(campaign/'campaign.json');verify_source(manifest)
    language,target=manifest_languages(manifest)
    ready=manifest['status']=='samples_ready' and all(
        state_is_complete(campaign/s['folder']/'识别任务',language=language,target=target)
        for s in manifest['samples'])
    validate_review(ready,reviewed,timing_passed,content_passed)
    artifacts=artifacts_for(campaign,manifest)
    available_cues=0
    for sample in manifest['samples']:
        project=campaign/sample['folder']/'识别任务'
        if _has_unresolved_draft_parts(read_json(project/'state.json')):
            raise ValueError('样片包含尚未解决的草稿空识别或时间轴疑点，请先复核后再验收')
        available_cues+=validate_public_subtitles(project,artifacts,language=language,target=target)['cue_count']
    if available_cues<20:
        raise ValueError(f'样片实际只有{available_cues}句，不足20句，请补充样本后验收')
    if reviewed>available_cues:
        raise ValueError(f'填写的抽查句数超过样片实际的{available_cues}句')
    sample_states=[]
    for sample in manifest['samples']:
        folder=(campaign/sample['folder']).resolve()
        proof=sample_state_evidence(folder/'识别任务',source_path=folder/'input.wav',language=language,target=target)
        if proof['source_sha256']!=sample.get('audio_hash'):
            raise ValueError('样片音频与准备记录不一致，不能验收')
        sample_states.append(proof)
    approval={'status':'approved','source_sha256':manifest['source']['sha256'],
        'language':language,'target':target,
        'reviewed_cues':reviewed,'available_cues':available_cues,
        'timing_passed':timing_passed,'content_passed':True,
        'asr_provider':'qwen_asr','asr_api_version':'dashscope-v1',
        'asr_model':'qwen-audio-3.1-asr-flash','translation_model':'deepseek-flash',
        'artifacts':artifacts,'sample_states':sample_states,'reviewed_at':time.time()}
    r.atomic_json(campaign/'approval.json',approval)
    emit('已保存你的样片验收结果；可启动整片，尚未自动调用',status=manifest['status'])


def asr_reservation(settings):
    # Official short-audio model request limits, not Filetrans internal windows.
    return (7168*settings.asr_input_rate+1024*settings.asr_output_rate)/1_000_000


def run_full(campaign,stop,*,draft=False):
    manifest=read_json(campaign/'campaign.json');source=verify_source(manifest,stop)
    language,target=manifest_languages(manifest)
    config=config_for(source,campaign/'整片',campaign,full=True)
    if draft:
        config=replace(config,workflow_stage='draft',approval_path=None)
        manifest.update(generation_mode='unreviewed_draft',review_status='unreviewed')
        manifest.setdefault('draft_requested_at',time.time())
    else:
        r.validate_full_approval(config,manifest['source']['sha256'])
    settings=load_settings()
    summary=cost_report(campaign)
    # Silence-adjusted boundaries may be up to 10 seconds earlier than nominal.
    max_requests=math.ceil(manifest['duration_ms']/110_000)
    sample_ids=set()
    for sample in manifest['samples']:
        cache=campaign/sample['folder']/'识别任务'/'deepseek-translation-cache.json'
        if cache.is_file():
            sample_ids.update('deepseek-'+digest for digest in read_json(cache)['entries'])
    ds_sample=sum(v.get('actual_cny',0) for request_id,v in summary['requests'].items()
        if request_id in sample_ids and v.get('provider')=='deepseek')
    sample_ms=sum((s['end_sec']-s['start_sec'])*1000 for s in manifest['samples']) or 300_000
    estimate=summary['committed_cny']+max_requests*asr_reservation(settings)+max(2,ds_sample*manifest['duration_ms']/sample_ms*1.5)
    manifest['estimated_total_cny']=round(estimate,4)
    write_manifest(campaign,manifest)
    if not (campaign/'整片'/'state.json').exists() and estimate>=18:
        raise ValueError('按已核实价格和样片用量，预计整片超过18元追加线；未提交整片')
    emit(f'开始整片处理，估算合计 {estimate:.2f} 元；实际请求仍受共享账本限制')
    state=r.run_pipeline(config,stop,on_progress=lambda s:emit(s.get('message','处理中'),
        status=s.get('status'),recognized=s.get('recognized'),translated=s.get('translated')))
    manifest['status']='full_ready' if state_is_complete(campaign/'整片',language=language,target=target,stop=stop) else 'full_incomplete'
    manifest['cost']=cost_report(campaign)
    _check_workflow_stop(stop)
    write_manifest(campaign,manifest)
    _check_workflow_stop(stop)
    emit('整片字幕草稿生成完成，仍需检查疑点和抽检' if manifest['status']=='full_ready' else '整片尚未完成，已保存进度',
         status=manifest['status'])
    return manifest['status']=='full_ready'


def accept_final(campaign,content_passed,*,expected_revision=None):
    manifest=read_json(campaign/'campaign.json');verify_source(manifest)
    language,target=manifest_languages(manifest)
    if content_passed is not True or not state_is_complete(campaign/'整片',language=language,target=target):
        raise ValueError('须整片生成完整，并由你确认已查看抽检、处理严重疑点')
    manual={}
    if expected_revision is not None or (campaign/'整片'/'人工校对'/'校对记录.json').exists():
        from .manual_review import load_review,materialize_review
        review=load_review(campaign/'整片',language=language,target=target)
        if not expected_revision or expected_revision!=review['revision']:
            raise ValueError('校对版本已变化，请刷新校对页后重新确认验收')
        identity=review['binding'].get('identity',{})
        if identity.get('source',{}).get('sha256')!=manifest['source']['sha256']:
            raise ValueError('人工校对来源与原视频不一致')
        # Manual edits are an overlay. The untouched machine base still has
        # to contain every verified chunk, not just internally aligned SRTs.
        validate_public_subtitles(campaign/'整片',artifacts_for(campaign,manifest,full=True),
                                  language=language,target=target)
        snapshot=materialize_review(campaign/'整片',language=language,target=target,require_accepted=True)
        if snapshot['revision']!=expected_revision:raise ValueError('校对版本冲突，请重新验收')
        artifacts=snapshot['files']
        manual={'manual_revision':snapshot['revision'],'manual_binding':snapshot['binding'],
                'review_summary':snapshot['summary'],
                'reviewed_cue_ids':[cue['id'] for cue in review['cues'] if cue['review_status']=='checked']}
    else:
        # Keep the established sample-first path; completed drafts instead use
        # explicit cue-level records above, never a forged sample approval.
        r.validate_full_approval(config_for(Path(manifest['source']['path']),campaign/'整片',campaign,full=True),
                                 manifest['source']['sha256'])
        artifacts=artifacts_for(campaign,manifest,full=True)
        validate_public_subtitles(campaign/'整片',artifacts,language=language,target=target)
    r.atomic_json(campaign/'final-review.json',{'status':'sampled_approved','content_passed':True,
        'language':language,'target':target,
        'source_sha256':manifest['source']['sha256'],'artifacts':artifacts,
        **manual,'reviewed_at':time.time(),'note':'用户确认抽检和严重疑点处理，不代表逐句人工校对'})
    manifest['status']='final_reviewed';write_manifest(campaign,manifest)
    emit('已记录你的最终抽检确认；可压制MP4',status='final_reviewed')
    return {'accepted':True,'reviewed_cues':len(manual.get('reviewed_cue_ids',[])),
            'review_status':'sampled_approved'}


def media_info(path,stop=None):
    process=capture_process(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],
        stop=stop,timeout=60)
    return json.loads(process.stdout)


def _preview_proof_matches(sample,digest,seconds):
    proof=sample.get('preview_validation')
    return (isinstance(proof,dict) and type(proof.get('version')) is int and proof['version']==2
        and proof.get('sha256')==digest and sample.get('preview_hash')==digest
        and type(proof.get('expected_duration_ms')) is int
        and proof['expected_duration_ms']==round(seconds*1000)
        and type(proof.get('video_frames')) is int and proof['video_frames']>0)


def _publish_preview(partial,preview,stop,*,expected_hash):
    """Move checked bytes without replacing a file created during preparation."""
    if stop.is_set():raise r.Cancelled('已停止预览保存，文件已保留')
    try:
        if os.name=='nt':
            rename_with_retry(partial,preview,stop)
        else:
            # POSIX rename replaces existing files; hard-link publication is
            # atomic/no-clobber on this same-directory pair of local files.
            os.link(partial,preview)
    except FileExistsError as error:
        raise ValueError('目标预览已存在，未覆盖新文件；已验证的partial保留') from error
    def token():
        stat=preview.stat()
        return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
    before=token()
    digest=cancellable_sha256(preview,stop)
    if stop.is_set():raise r.Cancelled('已停止预览确认，已保存的文件保留')
    if token()!=before or digest!=expected_hash:
        raise ValueError('预览发布期间内容发生变化，未保存完成证明；文件已保留')
    if os.name!='nt':
        partial.unlink()


def _checked_preview(path,seconds,stop,*,expected_hash=None):
    """Accept a preview only after real video decoding and a content binding."""
    if stop.is_set():raise r.Cancelled('已停止预览检查')
    def token():
        stat=path.stat()
        return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
    before=token()
    info=media_info(path,stop)
    try:
        if not math.isfinite(seconds) or seconds<=0:raise ValueError()
        streams=info['streams']
        video=next(s for s in streams if s.get('codec_type')=='video')
        audio=next(s for s in streams if s.get('codec_type')=='audio')
        if any(type(video.get(k)) is not int or video[k]<=0 for k in ('width','height')):
            raise ValueError()
        rate=Fraction(video['r_frame_rate'])
        if rate<=0:raise ValueError()
        tolerance=max(.25,min(.5,float(2/rate)))
        for value in (info['format']['duration'],):
            duration=float(value)
            if not math.isfinite(duration) or duration<=0 or abs(duration-seconds)>tolerance:
                raise ValueError()
        if video.get('duration') is not None:
            duration=float(video['duration'])
            if not math.isfinite(duration) or duration<=0 or abs(duration-seconds)>tolerance:
                raise ValueError()
    except (KeyError,TypeError,ValueError,ZeroDivisionError,StopIteration) as error:
        raise ValueError('预览没有有效的音视频或时长不匹配，原文件与诊断结果已保留') from error
    # Container/stream start_time can be zero even when the only video frame is
    # near the end. Inspect decoded first frames, not just header timestamps.
    # AAC priming packets may have negative PTS; decoded audio starts are used.
    for stream in ('v:0','a:0'):
        first=capture_process(['ffprobe','-v','error','-select_streams',stream,
            '-read_intervals','%+#8','-show_frames','-show_entries',
            'frame=best_effort_timestamp_time','-of','json',str(path)],
            stop=stop,timeout=30,max_output_bytes=64*1024)
        if stop.is_set():raise r.Cancelled('已停止预览检查，文件已保留')
        try:
            frames=json.loads(first.stdout)['frames']
            first_pts=float(frames[0]['best_effort_timestamp_time'])
            if (not math.isfinite(first_pts) or first.stderr.strip()
                    or abs(first_pts)>max(.05,min(.25,float(2/rate)))):
                raise ValueError()
        except (IndexError,KeyError,TypeError,ValueError) as error:
            raise ValueError('预览实际音视频起点缺失或偏移，文件已保留') from error
    # Video-only output time is essential: long audio must not hide one-frame
    # or truncated video. Decode every video frame, without generating a file.
    process=capture_process(['ffmpeg','-nostdin','-v','error','-xerror','-threads','2',
        '-i',str(path),'-map','0:v:0','-an','-progress','pipe:1','-f','null','-'],
        stop=stop,timeout=max(30,seconds*2+10),max_output_bytes=2*1024*1024)
    if stop.is_set():raise r.Cancelled('已停止预览检查，文件已保留')
    fields=dict(line.split('=',1) for line in process.stdout.splitlines() if '=' in line)
    try:
        frames=int(fields['frame'])
        decoded_seconds=int(fields['out_time_us'])/1_000_000
        if (frames<=0 or fields.get('progress')!='end' or process.stderr.strip()
                or decoded_seconds<=0 or abs(decoded_seconds-seconds)>tolerance):
            raise ValueError()
    except (KeyError,TypeError,ValueError) as error:
        raise ValueError('预览视频未完整解码或没有有效画面，诊断结果已保留') from error
    # Some containers report the full duration even for a track with no packets.
    # Verify audio separately, so either stream cannot mask the other's truncation.
    audio_process=capture_process(['ffmpeg','-nostdin','-v','error','-xerror','-threads','2',
        '-i',str(path),'-map','0:a:0','-vn','-progress','pipe:1','-f','null','-'],
        stop=stop,timeout=max(30,seconds*2+10),max_output_bytes=2*1024*1024)
    if stop.is_set():raise r.Cancelled('已停止预览检查，文件已保留')
    fields=dict(line.split('=',1) for line in audio_process.stdout.splitlines() if '=' in line)
    try:
        decoded_seconds=int(fields['out_time_us'])/1_000_000
        if (fields.get('progress')!='end' or audio_process.stderr.strip()
                or decoded_seconds<=0 or abs(decoded_seconds-seconds)>tolerance):
            raise ValueError()
    except (KeyError,TypeError,ValueError) as error:
        raise ValueError('预览音频未完整解码或没有有效数据，文件已保留') from error
    digest=cancellable_sha256(path,stop)
    if stop.is_set():raise r.Cancelled('已停止预览检查，文件已保留')
    if token()!=before or (expected_hash is not None and digest!=expected_hash):
        raise ValueError('预览检查期间文件发生变化，未保存完成记录')
    return {'version':2,'sha256':digest,'expected_duration_ms':round(seconds*1000),
            'video_frames':frames}


def export_video(campaign,stop,*,draft=False,encoder='auto'):
    """Preserve the workflow entry point while keeping media export independent."""
    dependencies=media_export.ExportDependencies(
        state_is_complete=state_is_complete,artifacts_for=artifacts_for,
        validate_public_subtitles=validate_public_subtitles,
        write_manifest=write_manifest,emit=emit)
    return media_export.export_video(campaign,stop,draft=draft,encoder=encoder,dependencies=dependencies)



def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='千问/DeepSeek分阶段字幕重做，总预算20元')
    parser.add_argument('action',choices=('prepare','samples','approve','full','draft','accept-final','export','export-draft','preflight'))
    parser.add_argument('--campaign',type=Path,required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--budget-ledger',type=Path,help='准备新项目时绑定共享费用账本（总预算20元）')
    parser.add_argument('--language',choices=('ja','en','zh'),default='ja',help='准备新项目的原文语言')
    parser.add_argument('--target',choices=tuple(TARGET_LABELS),help='准备新项目的译文语言')
    parser.add_argument('--encoder',choices=('auto','qsv','cpu'),default='auto',
                        help='视频导出编码器：auto 优先可运行 QSV，否则使用 CPU')
    parser.add_argument('--reviewed',type=int,default=0)
    parser.add_argument('--timing-passed',type=int,default=0)
    parser.add_argument('--content-passed',action='store_true')
    args=parser.parse_args(argv);campaign=args.campaign.resolve();campaign.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();finished=threading.Event()
    stop_files=_stop_files(campaign)
    def watch():
        while not finished.wait(.2):
            if any(path.exists() for path in stop_files):stop.set()
    try:
        completed=True
        with r.ProjectLock(campaign):
            if stop_files[0] not in stop_files[1:]:
                stop_files[0].unlink(missing_ok=True)
            if any(path.exists() for path in stop_files):stop.set()
            if stop.is_set():raise r.Cancelled('已停止，尚未开始本次操作')
            threading.Thread(target=watch,daemon=True).start()
            if args.action=='preflight':
                report=readiness();emit(report['message'],ready=report['ready']);return 0 if report['ready'] else 2
            if args.action=='prepare':
                if not args.source:raise ValueError('准备样片需要 --source 原始视频')
                prepare(args.source,campaign,args.baseline,stop,budget_ledger=args.budget_ledger,
                        language=args.language,target=args.target)
            elif args.action=='samples':completed=run_samples(campaign,stop)
            elif args.action=='approve':approve(campaign,args.reviewed,args.timing_passed,args.content_passed)
            elif args.action=='full':completed=run_full(campaign,stop)
            elif args.action=='draft':completed=run_full(campaign,stop,draft=True)
            elif args.action=='accept-final':accept_final(campaign,args.content_passed)
            elif args.action=='export':export_video(campaign,stop,encoder=args.encoder)
            elif args.action=='export-draft':export_video(campaign,stop,draft=True,encoder=args.encoder)
        return 0 if completed and not stop.is_set() else 2
    except Exception as error:
        # Provider exceptions have already stripped server payloads and credentials.
        emit(str(error),status='cancelled' if stop.is_set() else 'needs_attention')
        return 2
    finally:
        finished.set()


if __name__=='__main__':raise SystemExit(main())
