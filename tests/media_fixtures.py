"""Offline synthetic media only; never read, modify, or approve personal media."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline.runner import atomic_json, atomic_text
from subtitle_pipeline.subtitles import Cue, render_srt


def fixture_plan():
    return {
        'synthetic_fixture': True, 'duration_ms': 18000, 'seek_seconds': [2.0, 8.0, 14.0],
        'cues': [
            {'start_ms': 600, 'end_ms': 4900,
             'source_text': 'Welcome to the subtitle workshop.', 'target_text': '欢迎来到字幕工坊。'},
            {'start_ms': 6600, 'end_ms': 10900,
             'source_text': 'Jump to the second section.', 'target_text': '跳转到第二个片段。'},
            {'start_ms': 12600, 'end_ms': 17500,
             'source_text': 'The final section keeps both audio tracks.', 'target_text': '最后一个片段保留两条音轨。'}],
        'variants': [{'name': 'h264_aac', 'extension': '.mp4', 'codec': 'libx264', 'audio_tracks': 1},
                     {'name': 'h265_aac', 'extension': '.mp4', 'codec': 'libx265', 'audio_tracks': 1},
                     {'name': 'mkv_two_audio', 'extension': '.mkv', 'codec': 'libx264', 'audio_tracks': 2}],
    }


def run_command(args, log_path, *, timeout=90):
    """Bound every generator process and retain diagnostics on this fixture."""
    with Path(log_path).open('wb') as log:
        return subprocess.run([str(value) for value in args], stdin=subprocess.DEVNULL,
                              stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _speech_track(root, plan):
    if os.name != 'nt':
        raise RuntimeError('Speech fixtures require Windows System.Speech; use --tones explicitly elsewhere')
    speech_files = []
    for index, cue in enumerate(plan['cues'], 1):
        target = root / f'speech-{index}.wav'
        quoted_path = str(target).replace("'", "''")
        quoted_text = cue['source_text'].replace("'", "''")
        script = ("$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Speech; "
                  "$fixtureSynth=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$fixtureSynth.SelectVoice('Microsoft Zira Desktop'); $fixtureSynth.Rate=-1; "
                  f"$fixtureSynth.SetOutputToWaveFile('{quoted_path}'); "
                  f"$fixtureSynth.Speak('{quoted_text}'); $fixtureSynth.Dispose()")
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        run_command(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                    root / f'speech-{index}.log', timeout=30)
        speech_files.append(target)
    track = root / 'speech-timeline.wav'
    args = ['ffmpeg', '-nostdin', '-v', 'error']
    for path in speech_files:
        args.extend(['-i', path])
    filters = [f'[{index}:a]adelay={cue["start_ms"]}:all=1[a{index}]'
               for index, cue in enumerate(plan['cues'])]
    filters.append('[a0][a1][a2]amix=inputs=3:normalize=0,apad,atrim=duration=18[out]')
    args.extend(['-filter_complex', ';'.join(filters), '-map', '[out]', '-ar', '48000', '-ac', '2',
                 '-c:a', 'pcm_s16le', track])
    run_command(args, root / 'speech-timeline.log')
    return track


def _write_project(project, source, cues, duration_ms):
    project.mkdir(parents=True)
    source_identity = {'path': str(source.resolve()), 'sha256': workflow.sha256(source)}
    parts, chunks = {}, []
    section_ms = 6000
    for index, start_ms in enumerate(range(0, duration_ms, section_ms)):
        end_ms = min(duration_ms, start_ms + section_ms)
        chunk = {'index': index, 'core_start_ms': start_ms, 'core_end_ms': end_ms,
                 'audio_start_ms': start_ms, 'audio_end_ms': end_ms}
        chunks.append(chunk)
        folder = project / '片段' / f'{index + 1:04d}'
        folder.mkdir(parents=True)
        selected = [cue for cue in cues if start_ms <= cue['start_ms'] < end_ms]
        for field, filename in [('source_text', 'source.local.srt'), ('target_text', 'target.local.srt')]:
            atomic_text(folder / filename, render_srt([
                Cue(cue['start_ms'] - start_ms, cue['end_ms'] - start_ms, cue[field]) for cue in selected]))
        # These records model the generation file format solely for offline export.
        # The label is explicit at every boundary; no recognizer or API is invoked.
        atomic_json(folder / 'asr-response.json', {'synthetic_fixture': True, 'provider_called': False})
        source_hash = workflow.sha256(folder / 'source.local.srt')
        parts[str(index)] = {
            'asr': 'done', 'translation': 'done', 'synthetic_fixture': True,
            'source_hash': source_hash, 'translation_source_hash': source_hash,
            'target_hash': workflow.sha256(folder / 'target.local.srt'),
            'raw_response_hash': workflow.sha256(folder / 'asr-response.json'),
            'asr_evidence': {'provider': 'qwen_asr', 'model': 'qwen-audio-3.1-asr-flash',
                             'api': 'dashscope-v1', 'language': 'en',
                             'source_sha256': source_identity['sha256'], 'audio_range_ms': [start_ms, end_ms]},
        }
    for field, filename in [('source_text', '原文.srt'), ('target_text', '中文草稿.srt')]:
        atomic_text(project / filename, render_srt([
            Cue(cue['start_ms'], cue['end_ms'], cue[field]) for cue in cues]))
    atomic_text(project / '双语草稿.srt', render_srt([
        Cue(cue['start_ms'], cue['end_ms'], cue['source_text'] + '\n' + cue['target_text']) for cue in cues]))
    atomic_json(project / '需复核.json', [])
    atomic_json(project / 'state.json', {
        'status': 'complete', 'synthetic_fixture': True, 'duration_ms': duration_ms,
        'identity': {'engine': 'qwen_asr', 'api': 'dashscope-v1',
                     'asr_model': 'qwen-audio-3.1-asr-flash', 'translation_model': 'deepseek-flash',
                     'source': source_identity, 'language': 'en', 'target': 'zh-CN'},
        'chunks': chunks, 'parts': parts,
        'generated_hashes': {name: workflow.sha256(project / name)
                             for name in ('原文.srt', '中文草稿.srt', '双语草稿.srt')},
    })
    return source_identity


def write_campaign(source, campaign, *, plan=None):
    """Create a fresh, clearly labelled export fixture; refuse every existing target."""
    source, campaign = Path(source).resolve(), Path(campaign).resolve()
    campaign.mkdir(parents=True, exist_ok=False)
    plan = fixture_plan() if plan is None else plan
    identity = _write_project(campaign / '整片', source, plan['cues'], plan['duration_ms'])
    manifest = {'source': identity, 'source_kind': 'video', 'asr_provider': 'qwen_asr', 'samples': [],
                'status': 'final_reviewed', 'duration_ms': plan['duration_ms'],
                'language': 'en', 'target': 'zh-CN', 'synthetic_fixture': True}
    atomic_json(campaign / 'campaign.json', manifest)
    atomic_json(campaign / 'final-review.json', {
        'status': 'sampled_approved', 'source_sha256': identity['sha256'],
        'language': 'en', 'target': 'zh-CN',
        'artifacts': workflow.artifacts_for(campaign, manifest, full=True),
        'synthetic_fixture': True, 'human_reviewed': False,
        'note': 'Offline test harness approval only; never an actual user review.',
    })
    return campaign


def create_fixtures(root, *, tones=False):
    """Generate small real encoded files sequentially, under a fresh owned directory."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    plan = fixture_plan()
    atomic_json(root / 'fixture-owner.json', {'synthetic_fixture': True, 'generator': __file__})
    speech = None if tones else _speech_track(root, plan)
    generated = []
    for variant in plan['variants']:
        source = root / (variant['name'] + variant['extension'])
        args = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=640x360:rate=24,drawbox=x=0:y=240:w=iw:h=120:color=0x142030:t=fill']
        if speech:
            args.extend(['-i', speech])
        else:
            args.extend(['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000'])
        if variant['audio_tracks'] == 2:
            args.extend(['-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000'])
        args.extend(['-t', '18', '-map', '0:v:0', '-map', '1:a:0'])
        if variant['audio_tracks'] == 2:
            args.extend(['-map', '2:a:0', '-metadata:s:a:1', 'language=zho',
                         '-metadata:s:a:1', 'title=Diagnostic 880 Hz tone', '-disposition:a:1', '0'])
        args.extend(['-c:v', variant['codec'], '-threads', '2', '-preset', 'ultrafast',
                     '-pix_fmt', 'yuv420p', '-g', '48', '-c:a', 'aac', '-ac', '2',
                     '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0', 'title=Synthetic English speech',
                     '-disposition:a:0', 'default'])
        if variant['codec'] == 'libx265':
            args.extend(['-x265-params', 'pools=1:frame-threads=1:log-level=error', '-tag:v', 'hvc1'])
        args.append(source)
        run_command(args, root / (variant['name'] + '-generate.log'))
        campaign = write_campaign(source, root / (variant['name'] + '-campaign'))
        generated.append({**variant, 'source': str(source), 'campaign': str(campaign),
                          'sha256': workflow.sha256(source)})
    # Real browser-playable previews expose offsets 0, 6, 12 seconds in the UI.
    main = Path(generated[0]['campaign'])
    manifest = workflow.read_json(main / 'campaign.json')
    for index in range(3):
        relative = f'样片/{index + 1:04d}'
        folder = main / relative
        folder.mkdir(parents=True)
        preview = folder / 'preview.mp4'
        run_command(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(index * 6), '-i', generated[0]['source'],
                     '-t', '6', '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast',
                     '-c:a', 'aac', preview], folder / 'preview-generate.log')
        cue = dict(plan['cues'][index])
        cue['start_ms'] -= index * 6000
        cue['end_ms'] -= index * 6000
        _write_project(folder / '识别任务', preview, [cue], 6000)
        manifest['samples'].append({'folder': relative, 'name': f'合成片段 {index + 1}',
                                    'start_sec': index * 6, 'end_sec': (index + 1) * 6,
                                    'preview_hash': workflow.sha256(preview)})
    atomic_json(main / 'campaign.json', manifest)
    result = {**plan, 'root': str(root), 'audio_mode': 'diagnostic_tones' if tones else 'windows_offline_speech',
              'speech_voice': None if tones else 'Microsoft Zira Desktop', 'variants': generated,
              'browser_source': generated[0]['source'], 'browser_campaign': str(main),
              'limits': ['Synthetic short media only', 'No cloud ASR or translation called',
                         'Human listening and full-length media quality are separate acceptance steps']}
    atomic_json(root / 'fixtures.json', result)
    return result
