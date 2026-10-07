"""Explicit real FFmpeg acceptance; offline, synthetic, bounded and reproducible.

python tests/media_acceptance.py --fixtures-only
python tests/media_acceptance.py --fixtures ROOT/fixtures.json --encoders cpu
JSON, logs and media stay in the repository's ignored 验证样例 directory.
"""
import argparse
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_fixtures import create_fixtures, fixture_plan, write_campaign
from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline.runner import atomic_json


def checked_output_root(repository, root):
    repository, root = Path(repository).resolve(), Path(root).resolve()
    ignored = repository / '验证样例'
    if root == ignored or not root.is_relative_to(ignored):
        raise ValueError('Media acceptance output must be a fresh child of ignored 验证样例')
    return root


def validate_fixture_manifest(fixtures, fixtures_path):
    root = Path(fixtures_path).resolve().parent
    if fixtures.get('synthetic_fixture') is not True or Path(fixtures.get('root', '')).resolve() != root:
        raise ValueError('Only matching synthetic fixture manifests may be reused')
    expected = {variant['name']: variant for variant in fixture_plan()['variants']}
    variants = fixtures.get('variants', [])
    names = [variant.get('name') for variant in variants]
    if len(names) != len(expected) or set(names) != set(expected):
        raise ValueError('Synthetic fixture variant names changed')
    for variant in variants:
        recipe = expected[variant['name']]
        source = Path(variant['source']).resolve()
        if (source.parent != root or source.name != recipe['name'] + recipe['extension']
                or variant.get('audio_tracks') != recipe['audio_tracks']
                or workflow.sha256(source) != variant.get('sha256')):
            raise ValueError('Synthetic fixture variant source binding changed')
    return root


def capture(args, *, timeout=45):
    return subprocess.run([str(value) for value in args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def probe(path):
    return json.loads(capture(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                               '-of', 'json', path]).stdout)


def assess_streams(source, output, expected_duration_ms):
    source_video = next(stream for stream in source['streams'] if stream['codec_type'] == 'video')
    output_video = next(stream for stream in output['streams'] if stream['codec_type'] == 'video')
    source_audio = [stream for stream in source['streams'] if stream['codec_type'] == 'audio']
    output_audio = [stream for stream in output['streams'] if stream['codec_type'] == 'audio']
    duration_ms = float(output['format']['duration']) * 1000
    return {
        'duration': {'passed': math.isfinite(duration_ms) and abs(duration_ms - expected_duration_ms) <= 250,
                     'expected_ms': expected_duration_ms, 'observed_ms': duration_ms, 'tolerance_ms': 250},
        'video_geometry': {'passed': all(source_video.get(field) == output_video.get(field)
                                        for field in ('width', 'height', 'r_frame_rate')),
                           'source': {k: source_video.get(k) for k in ('width', 'height', 'r_frame_rate')},
                           'output': {k: output_video.get(k) for k in ('width', 'height', 'r_frame_rate')}},
        'audio_track_count': {'passed': len(source_audio) == len(output_audio),
                              'source_count': len(source_audio), 'output_count': len(output_audio)},
    }


def caption_pixel_evidence(source_pixels, output_pixels):
    source_bright = sum(value >= 200 for value in source_pixels)
    output_bright = sum(value >= 200 for value in output_pixels)
    added = output_bright - source_bright
    return {'passed': bool(source_pixels) and len(source_pixels) == len(output_pixels) and added >= 50,
            'source_bright_pixels': source_bright, 'output_bright_pixels': output_bright,
            'added_bright_pixels': added, 'threshold': 200, 'required_added_pixels': 50,
            'meaning': 'Added caption-region pixels; semantic glyph legibility still needs visual inspection'}


def assess_encoder(requested, selected, encoder_args):
    actual = None
    try:
        actual = encoder_args[encoder_args.index('-c:v') + 1]
    except (ValueError, IndexError):
        pass
    expected = {'cpu': 'libx264', 'qsv': 'h264_qsv'}.get(selected)
    return {'passed': expected is not None and actual == expected
                      and (requested == 'auto' or requested == selected),
            'requested': requested, 'selected': selected, 'checkpoint_encoder': actual}


def frame_evidence(source, output, seconds, folder):
    evidence = []
    for position in seconds:
        frames = []
        for kind, path in [('source', source), ('output', output)]:
            pixels = capture(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '2', '-ss', str(position),
                              '-i', path, '-frames:v', '1', '-vf', 'crop=iw:ih/3:0:2*ih/3,format=gray',
                              '-f', 'rawvideo', '-']).stdout
            frames.append(pixels)
            capture(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '2', '-ss', str(position),
                     '-i', path, '-frames:v', '1', '-y', folder / f'{kind}-{position:g}s.png'])
        evidence.append({'seconds': position, **caption_pixel_evidence(*frames),
                         'source_frame': str(folder / f'source-{position:g}s.png'),
                         'output_frame': str(folder / f'output-{position:g}s.png')})
    return evidence


def environment():
    result = {'python': sys.version, 'platform': platform.platform(),
              'ffmpeg_path': shutil.which('ffmpeg'), 'ffprobe_path': shutil.which('ffprobe')}
    for name in ('ffmpeg', 'ffprobe'):
        result[name + '_version'] = capture([name, '-version']).stdout.decode('utf-8', 'replace').splitlines()[0]
    encoders = capture(['ffmpeg', '-hide_banner', '-encoders']).stdout.decode('utf-8', 'replace')
    result['encoder_declarations'] = {name: name in encoders for name in ('libx264', 'libx265', 'h264_qsv')}
    result['qsv_declaration_is_runtime_proof'] = False
    return result


def export_once(campaign, encoder, stop=None):
    transcript = io.StringIO()
    stop = stop or threading.Event()
    watchdog = threading.Timer(120, stop.set)
    watchdog.daemon = True
    watchdog.start()
    try:
        with redirect_stdout(transcript):
            workflow.export_video(campaign, stop, encoder=encoder)
    finally:
        watchdog.cancel()
    return transcript.getvalue()


def verify_variant(variant, encoder, fixtures_root):
    # Separate sources prevent different encoder requests from colliding at publication.
    folder = fixtures_root / f'acceptance-{encoder}' / variant['name']
    folder.mkdir(parents=True, exist_ok=False)
    source = folder / Path(variant['source']).name
    shutil.copyfile(variant['source'], source)
    source_hash = workflow.sha256(source)
    campaign = write_campaign(source, folder / 'campaign')
    start = time.monotonic()
    transcript = export_once(campaign, encoder)
    (folder / 'export-events.jsonl').write_text(transcript, encoding='utf-8')
    manifest = workflow.read_json(campaign / 'campaign.json')
    output = Path(manifest['output'])
    source_info, output_info = probe(source), probe(output)
    checks = assess_streams(source_info, output_info, fixture_plan()['duration_ms'])
    selected = manifest.get('output_binding', {}).get('encoder')
    receipt = workflow.read_json(campaign / '导出' / 'encoding-checkpoint.json')
    checks['encoder_selection'] = assess_encoder(encoder, selected, receipt['identity']['encoder_args'])
    checks['source_unchanged'] = {'passed': workflow.sha256(source) == source_hash, 'sha256': source_hash}
    verification = workflow.read_json(campaign / '导出' / 'verification.json')
    checks['audio_hashes'] = {'passed': verification['source_audio_sha256'] == verification['output_audio_sha256'],
                             'source': verification['source_audio_sha256'], 'output': verification['output_audio_sha256'],
                             'all_tracks': verification.get('audio_tracks')}
    if variant['audio_tracks'] > 1:
        tracks = verification.get('audio_tracks', {})
        checks['audio_hashes']['passed'] = (checks['audio_hashes']['passed']
            and len(tracks.get('source', [])) == variant['audio_tracks']
            and len(tracks.get('output', [])) == variant['audio_tracks']
            and all(a['sha256'] == b['sha256'] for a, b in zip(tracks['source'], tracks['output'])))
    frames = frame_evidence(source, output, fixture_plan()['seek_seconds'], folder)
    # Decode a cue-free interval too, retaining a timing visibility boundary.
    blank = frame_evidence(source, output, [5.5], folder)[0]
    checks['caption_free_gap'] = {'passed': blank['added_bright_pixels'] < 50, 'frame_evidence': blank}
    return {'variant': variant['name'], 'requested_encoder': encoder, 'encoder_binding': selected,
            'source': str(source), 'campaign': str(campaign), 'output': str(output),
            'elapsed_seconds': round(time.monotonic() - start, 3), 'checks': checks, 'frames': frames,
            'passed': all(value['passed'] for value in checks.values()) and all(frame['passed'] for frame in frames)}


def verify_cancel_resume(variant, root, encoder='cpu'):
    """Cancel after a real completed encode, then prove checkpoint and final reuse.

    The injected signal observes the on-disk checkpoint, never supplies media bytes
    or bypasses integrity verification. It creates a repeatable cancellation boundary.
    """
    folder = root / ('cancel-resume-' + encoder)
    folder.mkdir(exist_ok=False)
    source = folder / Path(variant['source']).name
    shutil.copyfile(variant['source'], source)
    campaign = write_campaign(source, folder / 'campaign')
    stop = threading.Event()
    original_json = workflow.r.atomic_json
    signal_observed = False

    def observe_checkpoint(path, value):
        nonlocal signal_observed
        result = original_json(path, value)
        if Path(path).name == 'encoding-checkpoint.json' and value.get('status') == 'encoded':
            signal_observed = True
            stop.set()
        return result

    cancelled = False
    transcript = io.StringIO()
    with redirect_stdout(transcript), patch.object(workflow.r, 'atomic_json', side_effect=observe_checkpoint):
        try:
            workflow.export_video(campaign, stop, encoder=encoder)
        except workflow.r.Cancelled:
            cancelled = True
    receipt = workflow.read_json(campaign / '导出' / 'encoding-checkpoint.json')
    partial = campaign / '导出' / 'result.partial.mp4'
    partial_hash = workflow.sha256(partial)
    final_before_resume = bool(workflow.read_json(campaign / 'campaign.json').get('output'))
    encoder_calls = []
    original_run = workflow.r.run_process

    def observe_process(args, *args_rest, **kwargs):
        if '-c:v' in [str(arg) for arg in args]:
            encoder_calls.append([str(arg) for arg in args])
        return original_run(args, *args_rest, **kwargs)

    with patch.object(workflow.r, 'run_process', side_effect=observe_process):
        transcript.write(export_once(campaign, encoder))
    output = Path(workflow.read_json(campaign / 'campaign.json')['output'])
    output_hash = workflow.sha256(output)
    resume_calls = len(encoder_calls)
    all_processes = []

    def observe_reuse(args, *args_rest, **kwargs):
        all_processes.append([str(arg) for arg in args])
        return original_run(args, *args_rest, **kwargs)

    with patch.object(workflow.r, 'run_process', side_effect=observe_reuse):
        transcript.write(export_once(campaign, encoder))
    (folder / 'export-events.jsonl').write_text(transcript.getvalue(), encoding='utf-8')
    checks = {
        'cancelled_at_encoded_checkpoint': cancelled and signal_observed and receipt['status'] == 'encoded',
        'no_final_record_before_resume': not final_before_resume,
        'encoded_checkpoint_matches_file': receipt['sha256'] == partial_hash,
        'resume_avoids_reencode': resume_calls == 0,
        'resume_publishes_exact_encoded_file': output_hash == partial_hash,
        'completed_export_reuse_avoids_runner_media_processes': len(all_processes) == 0,
        'completed_export_reuse_preserves_hash': workflow.sha256(output) == output_hash,
    }
    return {'passed': all(checks.values()), 'checks': checks, 'campaign': str(campaign),
            'output': str(output), 'output_sha256': output_hash, 'resume_encoder_calls': resume_calls,
            'repeat_export_runner_process_calls': len(all_processes),
            'process_count_scope': 'runner.run_process only; environment encoder trials and ffprobe capture_process calls are excluded',
            'cancellation_boundary': 'after successful encoding and exact input/output checkpoint validation; before publication',
            'mid_encode_process_termination_tested': False}


def should_cancel_encoding(progress, process_running):
    seconds = progress.get('encoded_seconds')
    return (process_running is True and progress.get('phase') == 'encoding'
            and type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0)


def verify_mid_encode_cancel(variant, root):
    """Stop an actual live CPU encoder, then restart the incomplete encode.

    Four stream-copy loops create a 72-second input (about 7 MB), large enough
    for the unmodified production progress watcher to see the live process.
    Neither the command nor the encoder speed is altered for this test.
    """
    folder = root / 'mid-encode-cancel-cpu'
    folder.mkdir(exist_ok=False)
    source = folder / 'synthetic-72s.mp4'
    capture(['ffmpeg', '-nostdin', '-v', 'error', '-stream_loop', '3', '-i', variant['source'],
             '-t', '72', '-c', 'copy', source])
    base = fixture_plan()
    extended = {**base, 'duration_ms': 72000, 'cues': [
        {**cue, 'start_ms': cue['start_ms'] + index * 18000,
         'end_ms': cue['end_ms'] + index * 18000}
        for index in range(4) for cue in base['cues']]}
    campaign = write_campaign(source, folder / 'campaign', plan=extended)
    project = campaign / '整片'
    protected = {path: workflow.sha256(path) for path in project.rglob('*') if path.is_file()}
    source_hash = workflow.sha256(source)
    partial = campaign / '导出' / 'result.partial.mp4'
    original_popen, original_emit = workflow.r.subprocess.Popen, workflow.emit
    stop = threading.Event()
    children, observation = [], {}
    transcript = io.StringIO()

    def observe_popen(command, *positional, **kwargs):
        child = original_popen(command, *positional, **kwargs)
        if command and Path(str(command[-1])).resolve() == partial.resolve():
            children.append(child)
        return child

    def observe_progress(message, **data):
        progress = data.get('export_progress', {})
        live = bool(children) and children[-1].poll() is None
        if not observation and should_cancel_encoding(progress, live):
            observation.update({'progress': dict(progress), 'encoder_pid': children[-1].pid,
                                'encoder_running_at_stop': True})
            stop.set()
        return original_emit(message, **data)

    cancelled = False
    watchdog = threading.Timer(120, stop.set)
    watchdog.daemon = True
    watchdog.start()
    try:
        with redirect_stdout(transcript), patch.object(workflow.r.subprocess, 'Popen', side_effect=observe_popen), \
                patch.object(workflow, 'emit', side_effect=observe_progress):
            try:
                workflow.export_video(campaign, stop, encoder='cpu')
            except workflow.r.Cancelled:
                cancelled = True
    finally:
        watchdog.cancel()
    receipt = workflow.read_json(campaign / '导出' / 'encoding-checkpoint.json')
    partial_hash = workflow.sha256(partial) if partial.is_file() else None
    partial_size = partial.stat().st_size if partial.is_file() else 0
    checks = {
        'cancelled_with_positive_progress_and_live_encoder': cancelled and bool(observation),
        'encoder_exited_after_cancel': bool(children) and children[-1].poll() is not None,
        'incomplete_receipt_is_not_resumable_encoded': receipt['status'] == 'encoding',
        'no_final_record_after_cancel': not workflow.read_json(campaign / 'campaign.json').get('output'),
        'no_published_mp4_after_cancel': not list(folder.glob('*修订版.mp4')),
        'incomplete_file_preserved': partial_size > 0,
        'original_subtitle_and_state_files_preserved': all(workflow.sha256(path) == digest for path, digest in protected.items()),
        'source_preserved_after_cancel': workflow.sha256(source) == source_hash,
    }
    if not all(checks.values()):
        (folder / 'export-events.jsonl').write_text(transcript.getvalue(), encoding='utf-8')
        return {'passed': False, 'checks': checks, 'stop_observation': observation, 'campaign': str(campaign),
                'error': 'Expected live encoding cancellation boundary was not observed; artifacts preserved'}
    encoder_calls = []
    original_run = workflow.r.run_process

    def observe_restart(command, *positional, **kwargs):
        if '-c:v' in [str(value) for value in command]:
            encoder_calls.append([str(value) for value in command])
        return original_run(command, *positional, **kwargs)

    with patch.object(workflow.r, 'run_process', side_effect=observe_restart):
        transcript.write(export_once(campaign, 'cpu'))
    manifest = workflow.read_json(campaign / 'campaign.json')
    output = Path(manifest['output'])
    archived = list((campaign / '导出' / '保留的编码').glob('*.mp4'))
    checks.update({
        'resume_performs_one_full_encode': len(encoder_calls) == 1,
        'interrupted_file_archived_without_changes': any(workflow.sha256(path) == partial_hash for path in archived),
        'full_duration_after_restart': abs(float(probe(output)['format']['duration']) - 72) <= 0.25,
        'published_hash_matches_manifest': workflow.sha256(output) == manifest['output_sha256'],
        'source_preserved_after_resume': workflow.sha256(source) == source_hash,
        'original_subtitle_and_state_files_preserved_after_resume': all(workflow.sha256(path) == digest for path, digest in protected.items()),
    })
    (folder / 'export-events.jsonl').write_text(transcript.getvalue(), encoding='utf-8')
    return {'passed': all(checks.values()), 'checks': checks, 'stop_observation': observation,
            'campaign': str(campaign), 'source': str(source), 'source_size_bytes': source.stat().st_size,
            'partial_size_bytes_after_stop': partial_size, 'partial_sha256_after_stop': partial_hash,
            'archived_interrupted_files': [str(path) for path in archived], 'output': str(output),
            'resume_encoder_calls': len(encoder_calls), 'frame_level_resume_claimed': False,
            'cancellation_boundary': 'positive production encoding progress while actual unmodified FFmpeg is running'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--fixtures', type=Path, help='Reuse the generated fixtures.json under 验证样例')
    parser.add_argument('--fixtures-only', action='store_true')
    parser.add_argument('--mid-encode-cancel-only', action='store_true',
                        help='Reuse fixtures and only check live CPU encoder termination and full restart')
    parser.add_argument('--tones', action='store_true', help='Explicitly omit speech; diagnostic tones only')
    parser.add_argument('--encoders', default='cpu', help='Comma-separated cpu,auto,qsv; each is actually exercised')
    args = parser.parse_args(argv)
    encoders = args.encoders.split(',')
    if not encoders or any(value not in {'cpu', 'auto', 'qsv'} for value in encoders):
        parser.error('--encoders must contain cpu,auto,qsv')
    if args.fixtures:
        fixtures_path = args.fixtures.resolve()
        checked_output_root(ROOT, fixtures_path.parent)
        fixtures = workflow.read_json(fixtures_path)
        root = validate_fixture_manifest(fixtures, fixtures_path)
    else:
        root = checked_output_root(ROOT, args.output_root or ROOT / '验证样例' / f'media-acceptance-{time.time_ns()}')
        fixtures = create_fixtures(root, tones=args.tones)
    report = {'synthetic_fixture': True, 'environment': environment(), 'fixtures': fixtures,
              'results': [], 'manual_acceptance': 'pending; browser interaction, listening and glyph legibility are separate',
              'limits': fixtures['limits']}
    if not args.fixtures_only:
        run_root = root / f'run-{time.time_ns()}'
        run_root.mkdir(exist_ok=False)
        for encoder in ([] if args.mid_encode_cancel_only else encoders):
            for variant in fixtures['variants']:
                try:
                    report['results'].append(verify_variant(variant, encoder, run_root))
                except Exception as error:
                    report['results'].append({'variant': variant['name'], 'requested_encoder': encoder,
                                              'passed': False, 'error': f'{type(error).__name__}: {error}'})
            try:
                report.setdefault('cancel_resume', {})[encoder] = verify_cancel_resume(fixtures['variants'][0], run_root, encoder)
            except Exception as error:
                report.setdefault('cancel_resume', {})[encoder] = {'passed': False, 'error': f'{type(error).__name__}: {error}'}
        if args.mid_encode_cancel_only or 'cpu' in encoders:
            try:
                report['mid_encode_cancel'] = verify_mid_encode_cancel(fixtures['variants'][0], run_root)
            except Exception as error:
                report['mid_encode_cancel'] = {'passed': False, 'error': f'{type(error).__name__}: {error}'}
    report['passed'] = (all(result['passed'] for result in report['results'])
                        and all(result['passed'] for result in report.get('cancel_resume', {}).values())
                        and report.get('mid_encode_cancel', {'passed': True})['passed'])
    report['fixtures_only'] = args.fixtures_only
    report_path = (root / 'fixtures-report.json' if args.fixtures_only else run_root / 'acceptance-report.json')
    atomic_json(report_path, report)
    print(json.dumps({'passed': report['passed'], 'fixtures_only': args.fixtures_only,
                      'report': str(report_path), 'fixtures': str(root / 'fixtures.json'),
                      'browser_source': fixtures['browser_source'], 'browser_campaign': fixtures['browser_campaign'],
                      'results': [{'variant': r['variant'], 'encoder': r['requested_encoder'],
                                   'passed': r['passed'], **({'error': r['error']} if 'error' in r else {})}
                                  for r in report['results']],
                      'cancel_resume': report.get('cancel_resume', {}),
                      'mid_encode_cancel': report.get('mid_encode_cancel')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
