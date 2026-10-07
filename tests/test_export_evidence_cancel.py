"""Export preflight cancellation uses owned records and fake media only."""
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import runner
from tests import test_draft_video as draft_fixture
from tests.test_cloud_workflow import write_complete_qwen_evidence


class ExportEvidenceCancellationTests(unittest.TestCase):
    write = draft_fixture.DraftVideoTests.write
    fake_encode = draft_fixture.DraftVideoTests.fake_encode

    def setUp(self):
        # Register fixture cleanup on this running case, so errors reach its
        # result instead of being hidden in an unrun TestCase's doCleanups().
        draft_fixture.DraftVideoTests.setUp(self)
        self.fixture = self

    def test_complete_check_with_initial_stop_never_reads_evidence(self):
        f = self.fixture
        f.stop.set()
        with patch.object(workflow, 'sample_state_evidence') as evidence:
            with self.assertRaises(runner.Cancelled):
                workflow.state_is_complete(f.folder, stop=f.stop)
        evidence.assert_not_called()

    def test_stop_after_valid_evidence_does_not_return_complete(self):
        f = self.fixture
        original = workflow.sample_state_evidence

        def finish_then_stop(*args, **kwargs):
            value = original(*args, **kwargs)
            f.stop.set()
            return value

        with patch.object(workflow, 'sample_state_evidence', side_effect=finish_then_stop):
            with self.assertRaises(runner.Cancelled):
                workflow.state_is_complete(f.folder, stop=f.stop)

    def test_stop_with_io_failure_keeps_cause_instead_of_false(self):
        f = self.fixture
        failure = OSError('owned generation-record read failed')

        def fail(*args, **kwargs):
            f.stop.set()
            raise failure

        with patch.object(workflow, 'sample_state_evidence', side_effect=fail):
            with self.assertRaises(runner.Cancelled) as caught:
                workflow.state_is_complete(f.folder, stop=f.stop)
        self.assertIs(caught.exception.__cause__, failure)

    def test_non_cancelled_bad_record_still_returns_false(self):
        f = self.fixture
        with patch.object(workflow, 'sample_state_evidence', side_effect=ValueError('invalid')):
            self.assertFalse(workflow.state_is_complete(f.folder, stop=f.stop))
        self.assertFalse(f.stop.is_set())

    def test_default_complete_check_preserves_legacy_call_signature(self):
        f = self.fixture
        with patch.object(workflow, 'sample_state_evidence', return_value={'valid': True}) as evidence:
            self.assertTrue(workflow.state_is_complete(f.folder))
        evidence.assert_called_once_with(f.folder, language='ja', target='zh-CN')

    def test_draft_and_formal_stop_after_evidence_before_any_media(self):
        f = self.fixture
        f.write(f.campaign / 'final-review.json', {
            'status': 'sampled_approved', 'language': 'ja', 'target': 'zh-CN',
            'source_sha256': f.manifest['source']['sha256'],
            'artifacts': workflow.artifacts_for(f.campaign, f.manifest, full=True),
        })
        before = {p: p.read_bytes() for p in f.campaign.rglob('*') if p.is_file()}
        original = workflow.sample_state_evidence

        def finish_then_stop(*args, **kwargs):
            value = original(*args, **kwargs)
            f.stop.set()
            return value

        for draft in (True, False):
            with self.subTest(draft=draft):
                f.stop.clear()
                f.encoder.reset_mock()
                with patch.object(workflow, 'sample_state_evidence', side_effect=finish_then_stop), \
                     patch.object(workflow, 'media_info', side_effect=AssertionError('no media after cancel')):
                    with self.assertRaises(runner.Cancelled):
                        workflow.export_video(f.campaign, f.stop, draft=draft)
                f.encoder.assert_not_called()
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertFalse((f.campaign / '导出').exists())

    def test_generation_source_scan_stops_after_one_bounded_read(self):
        f = self.fixture
        f.source.write_bytes(b'owned-source-block' * 200000)
        f.manifest['source']['sha256'] = workflow.sha256(f.source)
        f.write(f.campaign / 'campaign.json', f.manifest)
        write_complete_qwen_evidence(f.folder, f.source, 40000)
        original_open = Path.open
        opened = []
        reads = []

        class StoppingReader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def read(self, size=-1):
                data = self.stream.read(size)
                reads.append((size, len(data)))
                f.stop.set()
                return data
            def readinto(self, buffer):
                size = self.stream.readinto(buffer)
                reads.append((len(buffer), size))
                f.stop.set()
                return size

        def open_source(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == f.source and args and args[0] == 'rb':
                opened.append(stream)
                if len(opened) == 1:
                    return StoppingReader(stream)
            return stream

        with patch.object(Path, 'open', new=open_source), \
             patch.object(workflow, 'media_info', side_effect=AssertionError('no media after cancel')):
            with self.assertRaises(runner.Cancelled):
                workflow.export_video(f.campaign, f.stop, draft=True)
        self.assertEqual(len(opened), 1)
        self.assertEqual(reads, [(1024 * 1024, 1024 * 1024)])
        self.assertTrue(all(stream.closed for stream in opened))
        f.encoder.assert_not_called()
        self.assertFalse((f.campaign / '导出').exists())


if __name__ == '__main__':
    unittest.main()
