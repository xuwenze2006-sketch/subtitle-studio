"""The imported subtitle identity must bind the exact bytes actually consumed."""
from dataclasses import replace
import hashlib
import os
import threading
import unittest
from unittest.mock import patch

from tests import test_runner_identity_recovery as fixture
from subtitle_pipeline import runner as r


class SeedContentBindingTests(unittest.TestCase):
    # Reuse only fixture helpers, without importing/discovering another suite.
    setUp = fixture.RunnerIdentityRecoveryTests.setUp
    services = fixture.RunnerIdentityRecoveryTests.services
    seed = fixture.RunnerIdentityRecoveryTests.seed
    complete = fixture.RunnerIdentityRecoveryTests.complete
    resume = fixture.RunnerIdentityRecoveryTests.resume

    def setup_seed(self):
        self.seed_path = self.seed()
        self.config = replace(self.config, translate=False)
        self.original = self.seed_path.read_bytes()
        self.changed = self.original.replace(b'seed speech', b'edit speech')

    def assert_no_project_results(self):
        self.assertFalse((self.config.project / 'state.json').exists())
        self.assertFalse((self.config.project / 'prepare.json').exists())
        self.assertEqual(self.asr_calls, [])
        self.assertEqual(self.translation_calls, [])

    def test_seed_changed_after_identity_is_rejected_before_task_publication(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.setup_seed()
                original_identity = r.file_identity
                def identify(path, stop=None):
                    result = original_identity(path, stop)
                    if path == self.seed_path:
                        old = path.stat()
                        if replacement:
                            other = self.root / 'replacement.srt'
                            other.write_bytes(self.changed)
                            os.utime(other, ns=(old.st_atime_ns, old.st_mtime_ns))
                            other.replace(path)
                        else:
                            path.write_bytes(self.changed)
                            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
                    return result
                with self.services(), patch.object(r, 'file_identity', side_effect=identify), \
                     self.assertRaisesRegex(ValueError, '导入字幕.*变化'):
                    r.run_pipeline(self.config)
                self.assert_no_project_results()
                self.assertEqual(self.seed_path.read_bytes(), self.changed)

    def test_normal_bom_multiline_seed_keeps_content_identity(self):
        self.setup_seed()
        self.original = b'\xef\xbb\xbf' + self.original.replace(b'seed speech', '第一行\n第二行'.encode())
        self.seed_path.write_bytes(self.original)
        with fixture.observe_reads(self.seed_path) as reads:
            state = self.complete()
        self.assertEqual(len(reads), 2, 'identity hash plus the same bytes parsed, without a third pass')
        self.assertEqual(sum(read['bytes'] for read in reads), 2*len(self.original))
        self.assertTrue(all(read['stream'].closed for read in reads))
        self.assertEqual(state['identity']['seed']['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertIn('第一行\n第二行', (self.config.project / '原文.srt').read_text(encoding='utf-8'))
        self.assertEqual(self.asr_calls, [])
        self.resume()

    def test_change_during_second_seed_read_is_rejected(self):
        self.setup_seed()
        changed = [False]
        def changed_after_read():
            # First full content read belongs to file_identity; the second is
            # the actual seed load. Use more than one block to mix generations.
            if len(reads) == 2 and not changed[0]:
                changed[0] = True
                self.seed_path.write_bytes(self.changed)
        self.original = self.original + b'\n' * 1_048_576 + b'  '
        self.changed = self.original[:-2] + b'XX'
        self.seed_path.write_bytes(self.original)
        with self.services(), fixture.observe_reads(self.seed_path, after_read=changed_after_read) as reads, \
             self.assertRaisesRegex(ValueError, '导入字幕.*变化'):
            r.run_pipeline(self.config)
        self.assert_no_project_results()

    def test_stop_during_seed_load_leaves_no_task_state(self):
        self.setup_seed()
        stop = threading.Event()
        def stop_after_read():
            if len(reads) == 2:
                stop.set()
        with self.services(), fixture.observe_reads(self.seed_path, after_read=stop_after_read) as reads:
            state = r.run_pipeline(self.config, stop)
        self.assertEqual(state['status'], 'cancelled')
        self.assert_no_project_results()
        self.assertEqual(self.seed_path.read_bytes(), self.original)

    def test_mutation_after_verified_read_does_not_change_consumed_snapshot(self):
        self.setup_seed()
        parse = r.parse_srt
        mutated = [False]
        def consume(text):
            if not mutated[0] and 'seed speech' in text:
                mutated[0] = True
                self.seed_path.write_bytes(self.changed)
            return parse(text)
        with patch.object(r, 'parse_srt', side_effect=consume):
            state = self.complete()
        self.assertTrue(mutated[0])
        self.assertEqual(state['identity']['seed']['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertIn('seed speech', (self.config.project / '原文.srt').read_text(encoding='utf-8'))
        self.assertNotIn('edit speech', (self.config.project / '原文.srt').read_text(encoding='utf-8'))
        self.seed_path.write_bytes(self.original)
        self.resume()


if __name__ == '__main__':
    unittest.main()
