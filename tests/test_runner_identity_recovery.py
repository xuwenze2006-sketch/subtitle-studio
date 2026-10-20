"""Content-identical task recovery and stable single-pass input hashing, offline."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import runner as r
from subtitle_pipeline.subtitles import Cue


@contextmanager
def observe_reads(target, *, after_close=None, after_read=None):
    """Observe actual content reads; optional mutations own only the fixture."""
    original = Path.open
    records = []

    class Reader:
        def __init__(self, stream, record):
            self.stream, self.record = stream, record

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            result = self.stream.__exit__(*args)
            if self.record['eof'] and after_close is not None:
                after_close()
            return result

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def observed(self, count):
            self.record['bytes'] += count
            self.record['reads'] += 1
            self.record['eof'] |= not count
            if count and after_read is not None:
                after_read()

        def read(self, size=-1):
            value = self.stream.read(size)
            self.observed(len(value))
            return value

        def readinto(self, buffer):
            count = self.stream.readinto(buffer)
            self.observed(count)
            return count

    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get('mode', 'r')
        if path == target and mode == 'rb':
            record = {'bytes': 0, 'reads': 0, 'eof': False, 'stream': stream}
            records.append(record)
            return Reader(stream, record)
        return stream

    with patch.object(Path, 'open', opened):
        yield records


class RunnerIdentityRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='runner-identity-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'synthetic.wav'
        self.content = b'synthetic source audio'
        self.source.write_bytes(self.content)
        self.config = r.PipelineConfig(self.source, self.root / 'project',
                                       chunk_seconds=10, overlap_seconds=1)
        self.asr_calls, self.translation_calls = [], []
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(name, side_effect=AssertionError('outbound network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    @contextmanager
    def services(self, *, allow_work=True):
        def recognize(config, chunk, folder, stop, **kwargs):
            if not allow_work:
                self.fail('completed recognition repeated')
            self.asr_calls.append(chunk.index)
            return [Cue(100, 900, 'synthetic speech')]

        def translate(texts, *args, **kwargs):
            if not allow_work:
                self.fail('completed translation repeated')
            self.translation_calls.append(list(texts))
            return ['synthetic translation' for _ in texts]

        with patch.multiple(r, probe_media=lambda *a, **k: 2000,
                            detect_silences=lambda *a: [], recognize_chunk=recognize,
                            translate_texts=translate,
                            cloud_context=lambda *a: self.fail('real credentials reached')):
            yield

    def complete(self, config=None):
        with self.services():
            result = r.run_pipeline(config or self.config)
        self.assertEqual(result['status'], 'complete')
        self.asr_calls.clear()
        self.translation_calls.clear()
        return result

    def resume(self, config=None):
        with self.services(allow_work=False):
            result = r.run_pipeline(config or self.config)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((self.asr_calls, self.translation_calls), ([], []))
        return result

    def touch(self, path):
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000))

    def assert_rejected_without_work(self, config=None):
        state = self.config.project / 'state.json'
        before = state.read_bytes()
        with self.services(allow_work=False), self.assertRaises(ValueError):
            r.run_pipeline(config or self.config)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual((self.asr_calls, self.translation_calls), ([], []))

    def seed(self):
        path = self.root / 'seed.srt'
        path.write_text('1\n00:00:00,100 --> 00:00:00,900\nseed speech\n', encoding='utf-8')
        self.config = replace(self.config, seed_srt=path, seed_complete_until_ms=2000)
        return path

    def test_normal_resume_preserves_identity_and_skips_all_work(self):
        initial = self.complete()
        resumed = self.resume()
        self.assertEqual(resumed['identity'], initial['identity'])

    def test_source_timestamp_only_resume_preserves_saved_identity_and_outputs(self):
        initial = self.complete()
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.config.project.glob('*.srt')}
        self.touch(self.source)
        resumed = self.resume()
        self.assertEqual(resumed['identity'], initial['identity'])
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.config.project.glob('*.srt')}, before)

    def test_same_size_content_change_with_original_mtime_is_rejected(self):
        self.complete()
        before = self.source.stat()
        self.source.write_bytes(b'X' * before.st_size)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assert_rejected_without_work()

    def test_changed_size_is_rejected(self):
        self.complete()
        self.source.write_bytes(self.content + b' changed')
        self.assert_rejected_without_work()

    def test_same_content_at_different_source_path_is_rejected(self):
        self.complete()
        other = self.root / 'other.wav'
        other.write_bytes(self.content)
        self.assert_rejected_without_work(replace(self.config, source=other))

    def test_language_and_chunk_changes_remain_strict(self):
        self.complete()
        for changes in ({'language': 'en'}, {'chunk_seconds': 11}, {'target': 'en'}):
            with self.subTest(changes=changes):
                self.assert_rejected_without_work(replace(self.config, **changes))

    def test_seed_timestamp_only_resume_preserves_saved_identity(self):
        seed = self.seed()
        initial = self.complete()
        self.touch(seed)
        self.assertEqual(self.resume()['identity'], initial['identity'])

    def test_seed_content_or_path_changes_remain_strict(self):
        seed = self.seed()
        self.complete()
        old = seed.stat()
        content = seed.read_bytes()
        seed.write_bytes(content.replace(b'seed speech', b'edit speech'))
        os.utime(seed, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assert_rejected_without_work()
        seed.write_bytes(content)
        os.utime(seed, ns=(old.st_atime_ns, old.st_mtime_ns))
        other = self.root / 'other-seed.srt'
        other.write_bytes(content)
        self.assert_rejected_without_work(replace(self.config, seed_srt=other))

    def test_prepare_resume_accepts_same_content_after_timestamp_change(self):
        with self.services(), patch.object(r, 'probe_media', side_effect=r.Cancelled('fixture preparation stopped')):
            result = r.run_pipeline(self.config)
        self.assertEqual(result['status'], 'cancelled')
        preparing = self.config.project / 'prepare.json'
        self.assertTrue(preparing.exists())
        original_preparation = preparing.read_bytes()
        self.assertFalse((self.config.project / 'state.json').exists())
        self.touch(self.source)
        self.complete()
        self.assertTrue((self.config.project / 'state.json').exists())
        self.assertEqual(preparing.read_bytes(), original_preparation)
        self.resume()

    def test_timestamp_resume_keeps_pending_output_identity_fingerprint(self):
        state = self.complete()
        identity = json.loads(json.dumps(state['identity']))
        # A journal saved before a publication failure binds the old identity.
        published = self.config.project / '原文.srt'
        state['generated_hashes'].pop('原文.srt')
        state['pending_outputs'] = {
            'version': 1, 'identity': r.fingerprint(identity),
            'checkpoint': r._output_checkpoint(state),
            'outputs': {'原文.srt': hashlib.sha256(published.read_bytes()).hexdigest()},
        }
        r.atomic_json(self.config.project / 'state.json', state)
        before = (published.read_bytes(), published.stat().st_mtime_ns)
        self.touch(self.source)
        resumed = self.resume()
        self.assertEqual(resumed['identity'], identity)
        self.assertNotIn('pending_outputs', resumed)
        self.assertFalse(resumed.get('manual_outputs'))
        self.assertEqual((published.read_bytes(), published.stat().st_mtime_ns), before)

    def test_file_identity_reads_content_once_with_and_without_stop(self):
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None), observe_reads(self.source) as reads:
                identity = r.file_identity(self.source, stop)
            self.assertEqual(identity['sha256'], hashlib.sha256(self.content).hexdigest())
            self.assertEqual(len(reads), 1)
            self.assertEqual(reads[0]['bytes'], len(self.content))
            self.assertTrue(reads[0]['eof'])
            self.assertTrue(reads[0]['stream'].closed)

    def test_file_identity_rejects_source_replaced_after_hash(self):
        other = self.root / 'replacement.wav'
        other.write_bytes(b'X' * len(self.content))
        old = self.source.stat()
        os.utime(other, ns=(old.st_atime_ns, old.st_mtime_ns))
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None):
                other.write_bytes(b'X' * len(self.content))
                with observe_reads(self.source, after_close=lambda: other.replace(self.source)), \
                     self.assertRaisesRegex(ValueError, '变化'):
                    r.file_identity(self.source, stop)
                self.assertEqual(self.source.read_bytes(), b'X' * len(self.content))

    def test_file_identity_rejects_in_place_rewrite_after_hash_and_restored_mtime(self):
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None):
                old = self.source.stat()
                def change():
                    self.source.write_bytes(b'Z' * old.st_size)
                    os.utime(self.source, ns=(old.st_atime_ns, old.st_mtime_ns))
                with observe_reads(self.source, after_close=change), self.assertRaisesRegex(ValueError, '变化'):
                    r.file_identity(self.source, stop)
                self.assertEqual(self.source.read_bytes(), b'Z' * old.st_size)

    def test_source_scan_stop_retains_cancelled_pipeline_result_without_state(self):
        self.source.write_bytes(b'x' * (2 * 1024 * 1024 + 7))
        stop = threading.Event()
        with observe_reads(self.source, after_read=stop.set) as reads, self.services(allow_work=False):
            result = r.run_pipeline(self.config, stop)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['reads'], 1)
        self.assertEqual(reads[0]['bytes'], 1024 * 1024)
        self.assertTrue(reads[0]['stream'].closed)
        self.assertFalse((self.config.project / 'state.json').exists())
        self.assertFalse((self.config.project / 'prepare.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows native metadata query')
    def test_native_observation_failure_is_closed_without_hash_or_descriptor_leak(self):
        import ctypes
        original_open = os.open
        for succeeds in (False, True):
            with self.subTest(query_succeeds=succeeds):
                descriptors = []
                def opened(path, flags, *args, **kwargs):
                    descriptor = original_open(path, flags, *args, **kwargs)
                    descriptors.append(descriptor)
                    return descriptor
                def unavailable(*args):
                    ctypes.set_last_error(5)
                    return int(succeeds)
                kernel = SimpleNamespace(GetFileInformationByHandleEx=Mock(side_effect=unavailable))
                with patch.object(ctypes, 'WinDLL', return_value=kernel), \
                     patch.object(os, 'open', side_effect=opened), observe_reads(self.source) as reads, \
                     self.assertRaises(OSError):
                    r.file_identity(self.source, threading.Event())
                self.assertEqual(reads, [])
                self.assertTrue(descriptors)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)


if __name__ == '__main__':
    unittest.main()
