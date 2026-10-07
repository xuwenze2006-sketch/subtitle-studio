"""Explicit local FFmpeg/QSV export smoke test; no user audio or network.

Run: python tests/media_smoke.py
Test-only generation and review fixtures stay under ignored 验证样例.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from subtitle_pipeline import cloud_workflow as w
from subtitle_pipeline.runner import atomic_json, atomic_text
from subtitle_pipeline.subtitles import Cue, render_srt


def main():
    root=Path(__file__).resolve().parents[1]/'验证样例'/('国内字幕压制自检-'+str(time.time_ns()))
    root.mkdir(parents=True)
    source=root/'synthetic.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=0x24374d:s=842x474:r=24',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6','-c:v','libx264',
        '-pix_fmt','yuv420p','-c:a','aac','-ac','2',str(source)],check=True)
    campaign=root/'campaign';project=campaign/'整片';part=project/'片段'/'0001';part.mkdir(parents=True)
    source_id={'path':str(source),'sha256':w.sha256(source)}
    jp=render_srt([Cue(500,2500,'テスト字幕'),Cue(3000,5500,'字幕の確認')])
    zh=render_srt([Cue(500,2500,'中性测试：字幕清晰显示'),Cue(3000,5500,'检查位置、时长与声音')])
    for path,text in [(part/'source.local.srt',jp),(project/'原文.srt',jp),
                      (part/'target.local.srt',zh),(project/'中文草稿.srt',zh),
                      (project/'双语草稿.srt',zh),(project/'需复核.json','[]')]:atomic_text(path,text)
    atomic_json(part/'asr-response.json',{'synthetic_fixture':True})
    source_hash=w.sha256(part/'source.local.srt')
    evidence={'asr':'done','translation':'done','source_hash':source_hash,'translation_source_hash':source_hash,
        'target_hash':w.sha256(part/'target.local.srt'),'raw_response_hash':w.sha256(part/'asr-response.json'),
        'asr_evidence':{'provider':'qwen_asr','model':'qwen-audio-3.1-asr-flash','api':'dashscope-v1',
                        'source_sha256':source_id['sha256'],'audio_range_ms':[0,6000]}}
    atomic_json(project/'state.json',{'status':'complete','duration_ms':6000,'parts':{'0':evidence},
        'identity':{'engine':'qwen_asr','api':'dashscope-v1','asr_model':'qwen-audio-3.1-asr-flash',
                    'translation_model':'deepseek-flash','source':source_id},
        'chunks':[{'index':0,'core_start_ms':0,'core_end_ms':6000,'audio_start_ms':0,'audio_end_ms':6000}]})
    manifest={'source':source_id,'samples':[],'status':'final_reviewed','synthetic_fixture':True}
    atomic_json(campaign/'campaign.json',manifest)
    atomic_json(campaign/'final-review.json',{'status':'sampled_approved','source_sha256':source_id['sha256'],
        'artifacts':w.artifacts_for(campaign,manifest,full=True),'synthetic_fixture':True})
    w.export_video(campaign,threading.Event())
    result=w.read_json(campaign/'campaign.json');output=Path(result['output'])
    subprocess.run(['ffmpeg','-nostdin','-v','error','-ss','1','-i',str(output),'-frames:v','1',str(root/'frame.png')],check=True)
    print(json.dumps({'fixture_only':True,'root':str(root),'output':str(output),
        'verification':w.read_json(campaign/'导出'/'verification.json')},ensure_ascii=False))


if __name__=='__main__':main()
