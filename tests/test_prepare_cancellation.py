"""Preparation cancellation must precede subsequent reads and publication."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as w, runner as r


class CancelAfterRead:
    def __init__(self, stream, stop):
        self.stream, self.stop = stream, stop
        self.reads = 0

    def read(self, size):
        self.reads += 1
        value = self.stream.read(size)
        self.stop.set()
        return value

    def readinto(self, buffer):
        self.reads += 1
        value = self.stream.readinto(buffer)
        self.stop.set()
        return value

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class PreparationCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='preparation-stop-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source.mp4'
        self.source.write_bytes(b'synthetic source')
        self.campaign = self.root/'campaign'
        self.campaign.mkdir()
        self.stop = threading.Event()
        self.addCleanup(patch.stopall)
        patch.object(w, 'emit').start()
        patch('socket.create_connection', side_effect=AssertionError('offline')).start()
        patch('socket.socket.connect', side_effect=AssertionError('offline')).start()

    def manifest(self, whole_hash=None):
        result = {'version':1, 'source':r.file_identity(self.source), 'duration_ms':2000,
                  'language':'ja', 'target':'zh-CN', 'status':'prepared', 'samples':[]}
        if whole_hash is not None:
            result['timeline_hash'] = whole_hash
        w.write_manifest(self.campaign, result)
        return result

    def stop_during_read(self, target):
        original = Path.open
        observed = []
        def opened(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get('mode', 'r')
            if path == target and mode == 'rb':
                wrapper = CancelAfterRead(stream, self.stop)
                observed.append(wrapper)
                return wrapper
            return stream
        return patch.object(Path, 'open', opened), observed

    def test_source_verification_stop_does_not_read_cached_audio_or_write_manifest(self):
        whole = self.campaign/'源音频.wav'
        whole.write_bytes(b'cached audio')
        self.manifest(w.sha256(whole))
        before = (self.campaign/'campaign.json').read_bytes()
        interceptor, observed = self.stop_during_read(self.source)
        with interceptor, self.assertRaises(r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual([x.reads for x in observed], [1])
        self.assertEqual((self.campaign/'campaign.json').read_bytes(), before)
        self.assertEqual(whole.read_bytes(), b'cached audio')

    def test_cloud_entrypoints_stop_before_settings_or_paid_work(self):
        self.manifest()
        before = (self.campaign/'campaign.json').read_bytes()
        for action in ('samples', 'full-draft'):
            with self.subTest(action=action):
                self.stop.clear()
                interceptor, observed = self.stop_during_read(self.source)
                caught = None
                with interceptor, patch.object(w, 'load_settings', side_effect=ValueError('settings reached')) as settings:
                    try:
                        if action == 'samples':
                            w.run_samples(self.campaign, self.stop)
                        else:
                            w.run_full(self.campaign, self.stop, draft=True)
                    except Exception as error:
                        caught = error
                self.assertIsInstance(caught, r.Cancelled)
                settings.assert_not_called()
                self.assertEqual([x.reads for x in observed], [1])
                self.assertEqual((self.campaign/'campaign.json').read_bytes(), before)

    def test_pipeline_identity_stop_does_not_write_preparing_or_read_duration(self):
        project = self.root/'pipeline'
        interceptor, observed = self.stop_during_read(self.source)
        with interceptor, patch.object(r, 'probe_media', side_effect=ValueError('duration reached')) as probe:
            state = r.run_pipeline(r.PipelineConfig(self.source, project), self.stop)
        self.assertEqual(state['status'], 'cancelled')
        probe.assert_not_called()
        self.assertEqual([x.reads for x in observed], [1])
        self.assertFalse((project/'prepare.json').exists())
        self.assertFalse((project/'state.json').exists())

    def test_cloud_audio_cache_scan_can_stop_without_reextraction(self):
        project = self.root/'pipeline'
        project.mkdir()
        target = project/'timeline.wav'
        target.write_bytes(b'cached pipeline audio')
        state = {'duration_ms':2000, 'timeline_hash':r.sha256(target)}
        saved = dict(state)
        interceptor, observed = self.stop_during_read(target)
        with interceptor, patch.object(r, 'run_process', side_effect=AssertionError('reextracted')), \
                self.assertRaises(r.Cancelled):
            r.prepare_cloud_audio(r.PipelineConfig(self.source, project), state, self.stop)
        self.assertEqual([x.reads for x in observed], [1])
        self.assertEqual(state, saved)

    def test_stop_during_last_baseline_write_does_not_report_prepared(self):
        whole = self.campaign/'源音频.wav'
        whole.write_bytes(b'cached whole audio')
        manifest = self.manifest(w.sha256(whole))
        sample = self.campaign/'样片'/'01'
        sample.mkdir(parents=True)
        (sample/'input.wav').write_bytes(b'cached sample')
        manifest.update(source_kind='audio', status='preparing', samples=[{
            'name':'sample', 'folder':'样片/01', 'start_sec':0, 'end_sec':2,
            'audio_hash':w.sha256(sample/'input.wav')}])
        w.write_manifest(self.campaign, manifest)
        original = r.atomic_text
        def write(path, text):
            original(path, text)
            if Path(path).name == '旧日语.srt':
                self.stop.set()
        with patch.object(r, 'atomic_text', side_effect=write), self.assertRaises(r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(w.read_json(self.campaign/'campaign.json')['status'], 'preparing')
        self.assertFalse((self.campaign/'review.html').exists())

    def test_stop_during_review_does_not_return_or_emit_success(self):
        whole = self.campaign/'源音频.wav'
        whole.write_bytes(b'cached whole audio')
        self.manifest(w.sha256(whole))
        with patch.object(w, 'build_review', side_effect=lambda *args:self.stop.set()), \
                patch.object(w, 'emit') as emit, self.assertRaises(r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertFalse(any('准备完成' in str(call) for call in emit.call_args_list))

    def test_cached_audio_stop_does_not_complete_scan_or_change_manifest(self):
        whole = self.campaign/'源音频.wav'
        whole.write_bytes(b'cached audio')
        self.manifest(w.sha256(whole))
        before = (self.campaign/'campaign.json').read_bytes()
        interceptor, observed = self.stop_during_read(whole)
        with interceptor, self.assertRaises(r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual([x.reads for x in observed], [1])
        self.assertEqual((self.campaign/'campaign.json').read_bytes(), before)

    def test_new_source_hash_stop_does_not_create_campaign_manifest(self):
        interceptor, observed = self.stop_during_read(self.source)
        with patch.object(r, 'probe_media', return_value=2000), \
                patch.object(w, 'media_info', return_value={'streams':[{'codec_type':'audio'}]}), \
                interceptor, self.assertRaises(r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual([x.reads for x in observed], [1])
        self.assertFalse((self.campaign/'campaign.json').exists())

    def test_unchanged_cached_audio_is_scanned_once_on_resume(self):
        whole = self.campaign/'源音频.wav'
        whole.write_bytes(b'cached audio')
        self.manifest(w.sha256(whole))
        original = Path.open
        scans = []
        def opened(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if path == whole and mode == 'rb':
                scans.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', opened), \
                patch.object(r, 'run_process', side_effect=AssertionError('cached audio reencoded')):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(scans, [whole], 'resume read the whole audio more than once')

    def test_invalid_extracted_audio_does_not_replace_existing_audio(self):
        whole = self.campaign/'源音频.wav'
        whole.write_bytes(b'keep prior audio')
        self.manifest()
        before = (self.campaign/'campaign.json').read_bytes()
        def extract(args, *rest, **kwargs):
            Path(args[-1]).write_bytes(b'bad new audio')
        with patch.object(r, 'run_process', side_effect=extract), \
                patch.object(r, 'probe_media', return_value=999999), \
                self.assertRaisesRegex(ValueError, '时间轴'):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(whole.read_bytes(), b'keep prior audio')
        self.assertEqual((self.campaign/'源音频.partial.wav').read_bytes(), b'bad new audio')
        self.assertEqual((self.campaign/'campaign.json').read_bytes(), before)

    def test_stopping_real_duration_probe_reaps_child_before_other_preparation(self):
        original = subprocess.Popen
        children = []
        ready = threading.Event()
        done = threading.Event()
        marker = self.root/'probe-ready'
        script = ('from pathlib import Path; import sys,time; '
                  'Path(sys.argv[1]).write_text("ready"); '
                  'time.sleep(2); print(\'{"format":{"duration":"2"}}\')')
        def launch(args, *rest, **kwargs):
            self.assertEqual(args[0], 'ffprobe')
            child = original([sys.executable, '-c', script, str(marker)], *rest, **kwargs)
            children.append(child)
            ready.set()
            return child
        def cancel():
            if not ready.wait(3):
                return
            deadline = time.monotonic()+3
            while not marker.exists() and time.monotonic()<deadline and not done.wait(.01):
                pass
            if marker.exists():
                self.stop.set()
        worker = threading.Thread(target=cancel, daemon=True)
        worker.start()
        try:
            with patch.object(subprocess, 'Popen', side_effect=launch), \
                    patch.object(w, 'media_info', return_value={'streams':[{'codec_type':'audio'}]}) as info:
                started = time.monotonic()
                with self.assertRaises(r.Cancelled):
                    w.prepare(self.source, self.campaign, None, self.stop)
                elapsed = time.monotonic()-started
                self.assertFalse((self.campaign/'campaign.json').exists(), 'cancelled probe wrote a manifest')
                info.assert_not_called()
                self.assertLess(elapsed, 1.5, 'probe ignored stop until synthetic process finished')
                self.assertEqual(len(children), 1)
                from subtitle_pipeline.windows_job import wait_for_process_exit
                self.assertTrue(wait_for_process_exit(children[0], milliseconds=1000),
                                'probe returned before native process exit')
        finally:
            done.set()
            worker.join(timeout=4)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                for stream in (child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == '__main__':
    unittest.main()
