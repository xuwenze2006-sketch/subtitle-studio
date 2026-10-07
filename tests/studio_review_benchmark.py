"""Measure whole-preview versus one-cue writes on synthetic long subtitles.

Run: python tests/studio_review_benchmark.py
No provider requests or user files. Timings cover service work and JSON encoding,
not browser rendering, actual speech recognition, or arbitrary disk hardware.
"""
import json
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subtitle_pipeline import studio
from subtitle_pipeline.integrity import sha256
from subtitle_pipeline.subtitles import Cue, render_srt


def fixture(root, count):
    source=root/'synthetic.mp4'
    source.write_bytes(b'synthetic benchmark only')
    campaign=root/'campaign'; folder=campaign/'整片'; folder.mkdir(parents=True)
    identity={'source':{'path':str(source),'sha256':sha256(source)},'language':'ja','target':'zh-CN'}
    manifest={'source':identity['source'],'language':'ja','target':'zh-CN','asr_provider':'qwen_asr',
              'status':'full_ready','samples':[],'duration_ms':count*2000}
    (campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
    (folder/'state.json').write_text(json.dumps({'status':'complete','identity':identity,'duration_ms':count*2000}),encoding='utf-8')
    (folder/'需复核.json').write_text('[]',encoding='utf-8')
    for name,prefix in [('原文.srt','source'),('中文草稿.srt','translation')]:
        (folder/name).write_text(render_srt(Cue(i*2000,i*2000+1700,f'{prefix} {i}') for i in range(count)),encoding='utf-8')
    return studio.StudioController(source=str(source),campaign=str(campaign),state_path=root/'studio.json')


def measure(app, mode):
    preview=app.preview('main'); timings=[]; sizes=[]
    for index in range(4):
        cue=preview['cues'][0]
        request={'project_id':preview['project_id'],'sample':'main','response_mode':mode,
                 'expected_revision':preview['manual_review']['revision'],'cue_id':cue['id'],
                 'start_ms':cue['start_ms'],'end_ms':cue['end_ms'],'source_text':cue['source_text'],
                 'target_text':f'updated translation {index}','review_status':'checked','note':'',
                 'translation_confirmed':False}
        start=time.perf_counter()
        result=app.review_cue(request)
        encoded=json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8')
        elapsed=(time.perf_counter()-start)*1000
        if index: timings.append(elapsed); sizes.append(len(encoded))
        fresh=app.preview('main')
        if mode=='cue':
            assert result['cue']==fresh['cues'][0] and result['manual_review']==fresh['manual_review']
            assert 'cues' not in result
        else: assert result==fresh
        preview=fresh
    return {'median_ms':round(statistics.median(timings),3),'samples_ms':[round(v,3) for v in timings],
            'response_bytes':sizes,'fresh_preview_equivalent':True}


def main():
    def blocked(*args,**kwargs): raise AssertionError('Outbound network is blocked')
    results=[]
    with tempfile.TemporaryDirectory(prefix='studio-review-benchmark-') as temporary, \
         patch.object(socket.socket,'connect',blocked),patch.object(socket.socket,'connect_ex',blocked), \
         patch.object(socket,'create_connection',blocked):
        root=Path(temporary).resolve()
        for count in (5000,20000):
            entry={'cue_count':count}
            for mode in ('preview','cue'):
                folder=root/f'{count}-{mode}';folder.mkdir()
                entry[mode]=measure(fixture(folder,count),mode)
            results.append(entry)
    print(json.dumps({'synthetic_only':True,'measured_writes_per_mode':3,'results':results},indent=2))


if __name__=='__main__': main()
