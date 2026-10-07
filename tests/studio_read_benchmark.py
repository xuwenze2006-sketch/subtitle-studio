"""Compare Studio read requests with/without parsing cache, using temp data only.

Run: python tests/studio_read_benchmark.py
This measures local warm reads, not cloud processing or GUI rendering speed.
"""

from collections import Counter
import json
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from subtitle_pipeline import studio
from subtitle_pipeline.read_cache import SrtReadCache
from subtitle_pipeline.subtitles import Cue,render_srt


def blocked(*args,**kwargs):
    raise AssertionError('Outbound network is disabled in the read benchmark')


def fixture(root):
    source=root/'video.mp4'
    source.write_bytes(b'synthetic fixture only')
    project=root/'project'
    project.mkdir()
    for name,prefix in (('原文.srt','source'),('中文草稿.srt','translation'),('双语草稿.srt','bilingual')):
        (project/name).write_text(render_srt(
            Cue(index*2000,index*2000+1700,f'{prefix} fixture subtitle {index:04d}')
            for index in range(4500)),encoding='utf-8')
    parts={str(index):{'asr':'done','translation':'done','source_hash':'a'*64,
        'target_hash':'b'*64,'asr_evidence':{'provider':'qwen_asr','source_sha256':'c'*64},
        'audit_padding':'x'*5000} for index in range(131)}
    (project/'state.json').write_text(json.dumps({
        'status':'complete','review_status':'unreviewed','parts':parts}),encoding='utf-8')
    (project/'campaign.json').write_text(json.dumps({
        'status':'full_ready','samples':[]}),encoding='utf-8')
    return source,project


def benchmark(root,source,project):
    original_parse=studio.parse_srt
    original_json=studio.read_json
    counts=Counter()
    def parse(text):
        counts['parse_calls']+=1
        return original_parse(text)
    def read_json(path):
        counts['json_reads']+=1
        return original_json(path)
    results={}
    reference={}
    # Keep the parser identity stable: the cache intentionally invalidates when
    # its parser changes, including when tests install a different mock.
    with patch.object(studio,'parse_srt',new=parse),patch.object(studio,'read_json',new=read_json):
        for mode in ('uncached','cached'):
            app=studio.StudioController(source=str(source),campaign=str(project),
                state_path=root/(mode+'-studio.json'))
            if mode=='uncached':app._track_cache=SrtReadCache(max_entries=0)
            operations=(('preview_default',app.preview),
                ('preview_explicit',lambda:app.preview('main')),
                ('media_default',app.media_path),
                ('media_explicit',lambda:app.media_path('main')),
                ('download_default',lambda:app.download_path('原文.srt')),
                ('state',app.state))
            results[mode]={}
            for label,operation in operations:
                counts.clear()
                operation()
                warmup=dict(counts)
                samples=[]
                parse_counts=[]
                json_counts=[]
                for _ in range(5):
                    counts.clear()
                    started=time.perf_counter()
                    value=operation()
                    samples.append(round((time.perf_counter()-started)*1000,3))
                    parse_counts.append(counts['parse_calls'])
                    json_counts.append(counts['json_reads'])
                    if mode=='uncached':reference[label]=value
                    elif value!=reference[label]:
                        raise AssertionError('Caching changed the '+label+' response')
                results[mode][label]={'median_ms':round(statistics.median(samples),3),
                    'samples_ms':samples,'parse_calls_each':parse_counts,
                    'json_reads_each':json_counts,'warmup_counts':warmup}
    return {'fixture_cues':4500,'fixture_state_bytes':(project/'state.json').stat().st_size,
        'warmup_requests_per_operation':1,'measured_requests_per_operation':5,
        'cached_and_uncached_responses_equal':True,'results':results}


def main():
    with tempfile.TemporaryDirectory(prefix='studio-read-benchmark-') as folder, \
         patch.object(socket.socket,'connect',blocked), \
         patch.object(socket.socket,'connect_ex',blocked), \
         patch.object(socket,'create_connection',blocked), \
         patch.object(studio,'account_environment',return_value={}), \
         patch.object(studio,'encrypted_names',return_value=set()), \
         patch.object(studio,'engine_available',return_value=False):
        root=Path(folder)
        source,project=fixture(root)
        print(json.dumps(benchmark(root,source,project),ensure_ascii=True,indent=2))


if __name__=='__main__':
    main()
