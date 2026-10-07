"""Owned temporary cleanup cannot hide durable-write failures or resend work."""
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import azure_asr, cloud_budget, deepseek_translate, qwen_asr, runner


class PersistenceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'saved.json'
        self.target.write_bytes(b'original bytes')
        self.primary = OSError('primary durable-write failure')
        self.secondary = PermissionError('secondary temporary cleanup failure')
        self.unlink = Path.unlink

    def block_owned_temporary(self, path, *args, **kwargs):
        if path.parent == self.root and path.suffix == '.tmp':
            raise self.secondary
        return self.unlink(path, *args, **kwargs)

    def check_primary(self, callback, module, *, wrapped=False, operation='replace_with_retry'):
        with patch.object(module, operation, side_effect=self.primary), \
             patch.object(Path, 'unlink', autospec=True, side_effect=self.block_owned_temporary):
            with self.assertRaises(deepseek_translate.TranslationError if wrapped else OSError) as caught:
                callback()
        error = caught.exception
        self.assertIs(error.__cause__ if wrapped else error, self.primary)
        temporary = list(self.root.glob('*.tmp'))
        self.assertEqual(len(temporary), 1)
        self.assertTrue(any(str(temporary[0]) in note for note in getattr(error, '__notes__', [])))
        self.assertEqual(self.target.read_bytes(), b'original bytes')

    def test_runner_preserves_write_error_and_owned_temporary_diagnostic(self):
        self.check_primary(lambda: runner.atomic_text(self.target, 'new text'), runner)

    def test_budget_preserves_write_error_and_original_ledger(self):
        self.check_primary(lambda: cloud_budget._write_bytes(self.target, b'new ledger'), cloud_budget)

    def test_translation_cache_keeps_original_cause_and_cleanup_diagnostic(self):
        self.check_primary(lambda: deepseek_translate._save_cache(self.target, {'entries': {}}),
                           deepseek_translate, wrapped=True)

    def test_billing_stop_marker_preserves_failed_publication_error(self):
        marker = self.root / 'billing-review-required.json'
        self.check_primary(lambda: qwen_asr._billing_marker(marker, 'offline-request', 'test-model',
                             qwen_asr.MeteringError('invalid local usage'), {}),
                           qwen_asr.os, operation='link')
        self.assertFalse(marker.exists())

    def test_shared_asr_raw_copy_preserves_source_and_publication_failure(self):
        source = self.root / 'paid-response.json'
        source.write_bytes(b'{"retained":"original response"}')
        self.check_primary(lambda: azure_asr._copy_raw_response(source, self.target),
                           azure_asr.os, operation='replace')
        self.assertEqual(source.read_bytes(), b'{"retained":"original response"}')

    def test_successful_raw_publication_cleanup_failure_is_visible_and_raw_retained(self):
        raw = self.root / 'raw-response.json'
        with patch.object(Path, 'unlink', autospec=True, side_effect=self.block_owned_temporary):
            with self.assertRaises(PermissionError) as caught:
                cloud_budget._write_bytes(raw, b'{"ok":true}', replace=False)
        self.assertIs(caught.exception, self.secondary)
        self.assertEqual(raw.read_bytes(), b'{"ok":true}')
        self.assertEqual(self.target.read_bytes(), b'original bytes')

    def test_raw_cleanup_failure_keeps_unknown_reservation_without_resubmission(self):
        ledger = cloud_budget.BudgetLedger(self.root / 'budget.json')
        raw = self.root / 'raw.json'
        sends = []
        def raw_cleanup(path, *args, **kwargs):
            if path.parent == self.root and path.name.startswith('.raw.json.'):
                raise self.secondary
            return self.unlink(path, *args, **kwargs)
        def send():
            sends.append(1)
            return cloud_budget.HttpResponse(200, {}, b'{"ok":true}')
        with patch.object(Path, 'unlink', autospec=True, side_effect=raw_cleanup):
            with self.assertRaises(cloud_budget.SubmissionUnknown):
                ledger.execute('cleanup-failure', 'offline-provider', 2, raw, send)
        self.assertEqual(raw.read_bytes(), b'{"ok":true}')
        ledger = cloud_budget.BudgetLedger(ledger.path)
        with self.assertRaises(cloud_budget.SubmissionUnknown):
            ledger.execute('cleanup-failure', 'offline-provider', 2, raw,
                           lambda: self.fail('unknown request must not be resubmitted'))
        self.assertEqual(sends, [1])
        summary = ledger.summary()
        self.assertEqual(summary['reserved_cny'], 2)
        self.assertEqual(summary['spent_cny'], 0)
        self.assertEqual(summary['requests']['cleanup-failure']['status'], 'unknown')

    def test_failed_cache_and_cleanup_resumes_real_ledger_without_second_request(self):
        # Compose the HTTP fixture without rediscovering its TestCase class.
        fixture_module = importlib.import_module('tests.test_deepseek_translate')
        fixture = fixture_module.DeepSeekTranslationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ledger = cloud_budget.BudgetLedger(fixture.cache.parent / 'ledger.json')
        root = fixture.cache.parent
        def cache_cleanup(path, *args, **kwargs):
            if path.parent == root and path.name.startswith(fixture.cache.name + '.'):
                raise self.secondary
            return self.unlink(path, *args, **kwargs)
        with fixture.opener() as send, \
             patch.object(deepseek_translate, 'replace_with_retry', side_effect=self.primary), \
             patch.object(Path, 'unlink', autospec=True, side_effect=cache_cleanup):
            with self.assertRaises(deepseek_translate.TranslationError) as caught:
                fixture.translate(['hello'])
        self.assertIs(caught.exception.__cause__, self.primary)
        self.assertEqual(send.call_count, 1)
        before = fixture.ledger.summary()
        self.assertEqual(len(before['requests']), 1)
        self.assertEqual(next(iter(before['requests'].values()))['status'], 'success')
        self.assertEqual(before['reserved_cny'], 0)
        fixture.ledger = cloud_budget.BudgetLedger(fixture.ledger.path)
        with fixture.opener(lambda *_a, **_k: self.fail('completed raw response must not be resubmitted')) as send:
            self.assertEqual(fixture.translate(['hello']), ['译文 0'])
        self.assertEqual(send.call_count, 0)
        self.assertEqual(fixture.ledger.summary(), before)
        self.assertEqual(len(json.loads(fixture.cache.read_bytes())['entries']), 1)


if __name__ == '__main__':
    unittest.main()
