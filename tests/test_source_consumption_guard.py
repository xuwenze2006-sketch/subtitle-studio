"""Keep the source immutable from identity through actual pipeline consumption."""
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import os
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner as r
from tests import test_runner_identity_recovery as fixture


@unittest.skipUnless(os.name == 'nt', 'Windows native source sharing')
class SourceConsumptionGuardTests(unittest.TestCase):
    setUp = fixture.RunnerIdentityRecoveryTests.setUp
    services = fixture.RunnerIdentityRecoveryTests.services
    resume = fixture.RunnerIdentityRecoveryTests.resume

    def assert_protected(self):
        with self.assertRaises(OSError) as error:
            self.source.write_bytes(b'changed source')
        self.assertEqual(error.exception.errno, 13)  # CRT reports EACCES.
        other = self.root / 'replacement.wav'
        other.write_bytes(b'replacement source')
        with self.assertRaises(OSError) as error:
            other.replace(self.source)
        self.assertEqual(error.exception.errno, 13)
        with open(self.source, 'rb') as reader:
            self.assertEqual(reader.read(), self.content)

    def assert_released(self):
        self.source.write_bytes(b'changed after run')
        other = self.root / 'replacement.wav'
        other.write_bytes(self.content)
        other.replace(self.source)
        # A leaked project lock must not obstruct another valid acquisition.
        with r.ProjectLock(self.config.project):
            pass

    def test_preparation_extraction_and_finalization_keep_same_source(self):
        self.config = replace(self.config, translate=False, chunk_seconds=2,
                              overlap_seconds=.2, workers=2)
        observed = []
        def probe(*args, **kwargs):
            self.assert_protected()
            observed.append('probe')
            return 5000
        def recognize(config, chunk, folder, stop, **kwargs):
            # Two workers may run concurrently; each can still open a reader.
            with self.source.open('rb') as stream:
                self.assertEqual(stream.read(), self.content)
            with self.assertRaises(OSError):
                with self.source.open('r+b'):
                    pass
            self.asr_calls.append(chunk.index)
            return [r.Cue(100, 900, 'synthetic speech')]
        def progress(state):
            if state.get('status') in ('preparing', 'complete'):
                self.assert_protected()
                observed.append(state['status'])
        with self.services(), patch.object(r, 'probe_media', side_effect=probe), \
             patch.object(r, 'recognize_chunk', side_effect=recognize), \
             fixture.observe_reads(self.source) as reads:
            state = r.run_pipeline(self.config, on_progress=progress)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(observed, ['preparing', 'probe', 'complete'])
        self.assertEqual(sorted(self.asr_calls), [0, 1, 2])
        identity = hashlib.sha256(self.content).hexdigest()
        self.assertEqual(state['identity']['source']['sha256'], identity)
        self.assertTrue(all(p['asr_evidence']['source_sha256'] == identity
                            for p in state['parts'].values()))
        # One hash read plus one actual ASR source read for each chunk. The
        # protection itself must not add a second whole-file content pass.
        self.assertEqual(len(reads), 4)
        self.assertTrue(all(item['stream'].closed for item in reads))
        self.asr_calls.clear()
        self.resume()
        self.assert_released()

    def test_cancel_during_preparation_releases_source_and_project(self):
        stop = threading.Event()
        def probe(*args, **kwargs):
            self.assert_protected()
            stop.set()
            raise r.Cancelled('synthetic cancellation')
        with self.services(), patch.object(r, 'probe_media', side_effect=probe):
            result = r.run_pipeline(self.config, stop)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse((self.config.project / 'state.json').exists())
        self.assert_released()

    def test_preparation_error_releases_source_and_project(self):
        def probe(*args, **kwargs):
            self.assert_protected()
            raise RuntimeError('synthetic probe failure')
        with self.services(), patch.object(r, 'probe_media', side_effect=probe), \
             self.assertRaisesRegex(RuntimeError, 'synthetic probe failure'):
            r.run_pipeline(self.config)
        self.assert_released()

    def test_existing_writer_is_refused_before_identity_or_project_results(self):
        with self.source.open('r+b'), self.services(), \
             patch.object(r, 'file_identity', side_effect=AssertionError('read before protection')), \
             self.assertRaises(OSError) as error:
            r.run_pipeline(self.config)
        self.assertEqual(error.exception.winerror, 32)
        self.assertFalse((self.config.project / 'prepare.json').exists())
        self.assertFalse((self.config.project / 'state.json').exists())
        self.assert_released()

    def test_initial_stop_precedes_missing_or_writable_source_admission(self):
        stop = threading.Event()
        stop.set()
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.source.unlink()
                    writer = nullcontext()
                else:
                    writer = self.source.open('r+b')
                with writer, self.services(), patch.object(r, 'stable_read_guard',
                        side_effect=AssertionError('input admitted after initial stop')):
                    state = r.run_pipeline(self.config, stop)
                self.assertEqual(state, {'status':'cancelled', 'message':'已停止准备',
                    'total':0, 'recognized':0, 'translated':0})
                self.assertFalse((self.config.project / 'prepare.json').exists())
        self.source.write_bytes(self.content)
        self.assert_released()

    def test_stop_during_failed_source_admission_keeps_cancellation_priority(self):
        stop = threading.Event()
        def refuse(path):
            stop.set()
            raise PermissionError('synthetic admission failure after stop')
        with self.services(), patch.object(r, 'stable_read_guard', side_effect=refuse):
            state = r.run_pipeline(self.config, stop)
        self.assertEqual(state['status'], 'cancelled')
        self.assertFalse((self.config.project / 'prepare.json').exists())
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
