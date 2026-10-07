"""A text-only audition of the user's existing SiliconFlow account.

This entry point cannot approve, transcribe the full film, or invent SRT timings.
"""
import argparse
from decimal import Decimal
import html
import json
import math
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request
import uuid
import wave

from .cloud_budget import BudgetLedger, HttpResponse, CloudCancelled, CloudRequestError
from .integrity import sha256, fingerprint
from .runner import ProjectLock, atomic_json, atomic_text
from .cloud_workflow import WINDOWS, emit, read_json, verify_source, _stop_files
from . import cloud_settings
from .languages import SOURCE_LABELS, manifest_languages, validate_languages

MODEL='Qwen/Qwen3-ASR-1.7B'
BASE='https://api.siliconflow.cn/v1'


def configuration():
    return cloud_settings.load_siliconflow_settings().key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def request(path,key,body=None,content_type=None):
    if path not in ('/models','/audio/transcriptions'):raise ValueError('Unexpected service path')
    headers={'Authorization':'Bearer '+key}
    if content_type:headers['Content-Type']=content_type
    req=urllib.request.Request(BASE+path,data=body,headers=headers,method='POST' if body is not None else 'GET')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=180) as response:
            content=response.read(10_000_001)
            if len(content)>10_000_000:raise ValueError('Response exceeds size limit')
            return HttpResponse(response.status,dict(response.headers),content)
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code,dict(error.headers),error.read(100_000))
    except Exception:
        raise RuntimeError('硅基流动连接未获得可确认响应') from None


def verify_model(key):
    response=request('/models',key)
    if response.status!=200:raise ValueError(f'硅基流动模型列表读取失败（HTTP {response.status}）')
    try:ids={item['id'] for item in json.loads(response.body)['data']}
    except (ValueError,KeyError,TypeError):raise ValueError('硅基流动模型列表格式不符合接口说明') from None
    if MODEL not in ids:raise ValueError('当前账号模型列表未包含 Qwen3-ASR-1.7B，未上传音频；不会自动换成小模型')


def audio_frames(audio):
    audio=Path(audio)
    if audio.stat().st_size>10_000_000:raise ValueError('试听音频文件超过大小限制')
    try:
        with wave.open(str(audio),'rb') as stream:
            if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate(),stream.getcomptype())!=(1,2,16000,'NONE'):
                raise ValueError('样片必须是单声道16kHz、16位PCM WAV')
            frames=stream.getnframes()
            if not 0<frames<=180*16000:raise ValueError('试听单段只允许180秒以内')
            if len(stream.readframes(frames+1))!=frames*2:
                raise ValueError('试听音频数据已截断，未上传')
    except (wave.Error,EOFError):raise ValueError('试听音频不是有效WAV，未上传') from None
    return frames


def validate_samples(campaign,manifest):
    samples=manifest.get('samples')
    if not isinstance(samples,list) or len(samples)!=3:
        raise ValueError('试听限定已准备的三个样片，总计5分钟')
    total_frames=0
    for sample,(start,end) in zip(samples,WINDOWS):
        if (sample.get('start_sec'),sample.get('end_sec'))!=(start,end):
            raise ValueError('试听区间与固定三个样片计划不一致')
        audio=campaign/sample['folder']/'input.wav'
        if sha256(audio)!=sample.get('audio_hash'):
            raise ValueError('样片音频发生变化，未上传')
        frames=audio_frames(audio)
        if frames!=(end-start)*16000:
            raise ValueError('样片实际音频时长与90/120/90秒计划不一致，未上传')
        total_frames+=frames
    if total_frames>300*16000:
        raise ValueError('样片实际音频总时长超过5分钟，未上传')
    return samples


def _check_legacy_billing(ledger):
    if any(record['provider']=='siliconflow_free_asr' for record in ledger.summary()['requests'].values()):
        raise CloudRequestError('项目含旧版免费计量记录，请先核对实际账单并处理旧账本；不会重新上传。')


def transcribe(audio,key,ledger,stop_event=None,*,price_per_second=None,language='ja'):
    validate_languages(language,'zh-CN')
    audio=Path(audio)
    seconds=(audio_frames(audio)+15999)//16000
    if price_per_second is None:
        price_per_second=cloud_settings.load_siliconflow_settings().price_per_second
    try:
        if isinstance(price_per_second,bool):raise ValueError
        rate=float(price_per_second)
        if not math.isfinite(rate) or rate<0:raise ValueError
    except (TypeError,ValueError,OverflowError):
        raise ValueError('硅基流动每秒价格必须是已核实的非负有限数值') from None
    reserved=float(Decimal(seconds)*Decimal(str(rate)))
    _check_legacy_billing(ledger)
    audio_hash=sha256(audio)
    legacy=fingerprint({'provider':'siliconflow','model':MODEL,'audio_sha256':audio_hash,'schema':1})
    if (ledger.path.parent/'responses'/'siliconflow'/f'{legacy}.json').exists():
        raise CloudRequestError('存在旧版免费计量响应缓存，请先核对实际账单；不会重新上传。')
    # A stable audio identity lets the ledger reject a changed price instead of
    # treating it as permission to upload and charge for the same audio again.
    request_identity={'provider':'siliconflow','model':MODEL,'audio_sha256':audio_hash,'schema':2}
    # Keep existing Japanese paid request IDs reusable. New languages use their
    # own provenance; the documented model/file endpoint still auto-detects.
    if language!='ja':request_identity['language']=language
    identity=fingerprint(request_identity)
    raw=ledger.path.parent/'responses'/'siliconflow'/f'{identity}.json'
    boundary='subtitle-'+uuid.uuid4().hex
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{MODEL}\r\n'
          f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\n'
          'Content-Type: audio/wav\r\n\r\n').encode()+audio.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
    def send():
        if stop_event and stop_event.is_set():raise CloudCancelled('Cancelled before upload')
        return request('/audio/transcriptions',key,body,f'multipart/form-data; boundary={boundary}')
    # The response does not guarantee billable seconds. Keep the full local
    # rounded-duration reservation; token usage is not an ASR duration receipt.
    payload=ledger.execute(identity,'siliconflow_asr_seconds_v2',reserved,raw,send,stop_event=stop_event)
    if not isinstance(payload.get('text'),str):raise ValueError('识别响应缺少文字字段，原始结果已保留，不会重复上传')
    return {'text':payload['text'],'model':MODEL,'timing_verified':False,
            'language':language,'language_detection':'provider_auto',
            'raw_sha256':sha256(raw),'audio_sha256':sha256(audio),'request_id':identity,
            'billing_version':2,'price_per_second':rate,'reserved_cost_cny':reserved,
            'billable_seconds_estimate':seconds,'cost_basis':'rounded_audio_duration_estimate',
            'note':'未对齐：这是识别文字试听，不是带已验证时间轴的字幕。'}


def render_audition(samples,language='ja'):
    validate_languages(language,'zh-CN')
    label=SOURCE_LABELS[language]
    sections=[]
    for sample in samples:
        sections.append(f'<section><h2>{html.escape(sample["name"])}</h2>'
            f'<video controls preload="metadata" src="{html.escape(sample["video"],quote=True)}"></video>'
            f'<div><article><h3>旧{label}字幕全文</h3><pre>{html.escape(sample["old"])}</pre></article>'
            f'<article><h3>硅基流动新识别文字 · 未对齐</h3><pre>{html.escape(sample["text"])}</pre></article></div></section>')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>硅基流动识别试听</title>'
        '<style>body{font:16px/1.6 "Microsoft YaHei",sans-serif;max-width:1100px;margin:30px auto;background:#f2f5f9;color:#203040}'
        'section{background:white;padding:20px;margin:20px;border-radius:12px}video{width:100%;max-height:400px}'
        'section>div{display:grid;grid-template-columns:1fr 1fr;gap:20px}pre{white-space:pre-wrap;font:inherit;max-height:400px;overflow:auto}</style>'
        f'<h1>硅基流动 · {label}识别试听</h1><p>比较原音和新旧文字。新文字尚未对齐，不会当成正式SRT，不会据此自动开始整片。'
        '结果只说明本次样片表现；不代表已测得准确率。</p>'+''.join(sections)+'</html>')


def run(campaign,stop,*,start_watch=None):
    campaign=Path(campaign).resolve()
    stop_files=_stop_files(campaign)
    with ProjectLock(campaign):
        # Only the current lock owner may clear a previous stop or start a
        # watcher. The Studio job's independent stop signal must survive.
        if stop_files[0] not in stop_files[1:]:
            stop_files[0].unlink(missing_ok=True)
        if any(path.exists() for path in stop_files):stop.set()
        if stop.is_set():raise CloudCancelled('已停止，尚未提交请求')
        if start_watch is not None:start_watch()
        settings=cloud_settings.load_siliconflow_settings()
        key=settings.key
        manifest=read_json(campaign/'campaign.json');verify_source(manifest)
        language,target=manifest_languages(manifest)
        prepared_samples=validate_samples(campaign,manifest)
        if any(path.exists() for path in stop_files):stop.set()
        if stop.is_set():raise CloudCancelled('已停止，尚未提交请求')
        ledger=BudgetLedger(campaign/'费用账本.json');_check_legacy_billing(ledger)
        emit('检查硅基流动账户模型列表（尚未上传音频）');verify_model(key)
        samples=[]
        for sample in prepared_samples:
            if stop.is_set():raise CloudCancelled('已停止，已保存的结果可恢复')
            folder=campaign/sample['folder'];audio=folder/'input.wav'
            if sha256(audio)!=sample['audio_hash']:raise ValueError('样片音频发生变化，未上传')
            emit(sample['name']+'：硅基流动试识别，按已核实的每秒价格计入预算')
            result=transcribe(audio,key,ledger,stop,price_per_second=settings.price_per_second,language=language)
            atomic_json(folder/'siliconflow-candidate.json',result)
            atomic_text(folder/'硅基流动_未对齐.txt',result['text'])
            baseline=folder/('旧日语.srt' if language=='ja' else '旧原文.srt')
            samples.append({'name':sample['name'],'video':sample['folder']+'/preview.mp4',
                'old':baseline.read_text(encoding='utf-8-sig') if baseline.is_file() else '',
                'text':result['text']})
            atomic_text(campaign/'硅基流动识别试听.html',render_audition(samples,language))
        atomic_json(campaign/'硅基流动试听状态.json',{'status':'text_audition_ready','model':MODEL,
            'timing_verified':False,'human_review':'pending','samples':len(samples),'cost':ledger.summary(),
            'language':language,'target':target,
            'billing_version':2,'pricing':settings.public_config()})
        emit('硅基流动5分钟识别试听已保存；时间轴未验收，整片未启动',status='text_audition_ready')


def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='仅做硅基流动5分钟识别试听，不生成正式SRT')
    parser.add_argument('--campaign',required=True,type=Path);args=parser.parse_args(argv)
    stop=threading.Event();done=threading.Event()
    stop_files=_stop_files(args.campaign)
    def watch():
        while not done.wait(.2):
            if any(path.exists() for path in stop_files):stop.set()
    try:
        run(args.campaign,stop,start_watch=lambda:threading.Thread(target=watch,daemon=True).start())
        return 0
    except Exception as error:emit(str(error),status='cancelled' if stop.is_set() else 'needs_attention');return 2
    finally:done.set()


if __name__=='__main__':raise SystemExit(main())
