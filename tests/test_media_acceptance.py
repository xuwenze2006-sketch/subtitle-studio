"""Cheap contracts for the explicit, separately run FFmpeg acceptance tool."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import studio
from subtitle_pipeline.subtitles import parse_srt


class MediaAcceptanceToolTests(unittest.TestCase):
    def load_tool(self, name):
        path = Path(__file__).with_name(name + '.py')
        self.assertTrue(path.is_file(), 'The reusable media acceptance tool is missing')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_three_sections_have_distinct_seek_targets_and_timed_script(self):
        fixtures = self.load_tool('media_fixtures')
        plan = fixtures.fixture_plan()
        self.assertEqual(plan['duration_ms'], 18000)
        self.assertEqual(plan['seek_seconds'], [2.0, 8.0, 14.0])
        self.assertEqual([(c['start_ms'], c['end_ms']) for c in plan['cues']],
                         [(600, 4900), (6600, 10900), (12600, 17500)])
        self.assertEqual([v['name'] for v in plan['variants']],
                         ['h264_aac', 'h265_aac', 'mkv_two_audio'])

    def test_campaign_passes_real_integrity_and_reconstructs_global_cue_times(self):
        fixtures = self.load_tool('media_fixtures')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'synthetic.mp4'
            source.write_bytes(b'offline test data, never a user file')
            campaign = fixtures.write_campaign(source, root / 'campaign')
            self.assertTrue(workflow.state_is_complete(campaign / '整片', language='en', target='zh-CN'))
            cues = parse_srt((campaign / '整片' / '原文.srt').read_text(encoding='utf-8'))
            self.assertEqual([cue.start_ms for cue in cues], [600, 6600, 12600])
            state = workflow.read_json(campaign / '整片' / 'state.json')
            self.assertEqual(len(state['chunks']), 3)
            self.assertTrue(state['synthetic_fixture'])
            self.assertTrue(workflow.read_json(campaign / 'final-review.json')['synthetic_fixture'])

    def test_campaign_is_loadable_for_real_browser_review_and_export(self):
        fixtures=self.load_tool('media_fixtures')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            source=root/'synthetic.mp4';source.write_bytes(b'owned offline source')
            campaign=fixtures.write_campaign(source,root/'campaign')
            studio.StudioController._validate_loaded_project(
                campaign,{},workflow.read_json(campaign/'campaign.json'))

    def test_existing_campaign_is_never_overwritten(self):
        fixtures = self.load_tool('media_fixtures')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            source.write_bytes(b'source')
            campaign = root / 'campaign'
            campaign.mkdir()
            sentinel = campaign / 'personal.txt'
            sentinel.write_bytes(b'preserve me')
            with self.assertRaises(FileExistsError):
                fixtures.write_campaign(source, campaign)
            self.assertEqual(sentinel.read_bytes(), b'preserve me')
            self.assertEqual(list(campaign.iterdir()), [sentinel])

    def test_stream_assessment_rejects_audio_loss_and_duration_drift(self):
        tool = self.load_tool('media_acceptance')
        source = {'format': {'duration': '18'}, 'streams': [
            {'codec_type': 'video', 'width': 640, 'height': 360, 'r_frame_rate': '24/1'},
            {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2},
            {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2}]}
        lost_audio = {'format': {'duration': '18'}, 'streams': source['streams'][:-1]}
        checks = tool.assess_streams(source, lost_audio, 18000)
        self.assertFalse(checks['audio_track_count']['passed'])
        drift = {'format': {'duration': '17'}, 'streams': source['streams']}
        self.assertFalse(tool.assess_streams(source, drift, 18000)['duration']['passed'])
        self.assertTrue(all(v['passed'] for v in tool.assess_streams(source, source, 18000).values()))

    def test_caption_pixels_require_visible_added_text(self):
        tool = self.load_tool('media_acceptance')
        background = bytes([20] * 1000)
        self.assertFalse(tool.caption_pixel_evidence(background, background)['passed'])
        subtitles = bytes([230] * 100 + [20] * 900)
        result = tool.caption_pixel_evidence(background, subtitles)
        self.assertTrue(result['passed'])
        self.assertEqual(result['added_bright_pixels'], 100)

    def test_cli_rejects_writing_outside_the_ignored_fixture_directory(self):
        tool = self.load_tool('media_acceptance')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, '验证样例'):
                tool.checked_output_root(root, root / 'personal')
            expected = root / '验证样例' / 'acceptance-123'
            self.assertEqual(tool.checked_output_root(root, expected), expected)

    def test_modified_manifest_cannot_escape_its_owned_directory(self):
        tool = self.load_tool('media_acceptance')
        self.assertTrue(hasattr(tool, 'validate_fixture_manifest'), 'Fixture manifest containment validation is missing')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'h264_aac.mp4'
            source.write_bytes(b'fixture')
            manifest = {'synthetic_fixture': True, 'root': str(root), 'variants': [
                {'name': '../../personal', 'source': str(source), 'sha256': workflow.sha256(source)}]}
            with self.assertRaisesRegex(ValueError, 'variant'):
                tool.validate_fixture_manifest(manifest, root / 'fixtures.json')

    def test_cpu_acceptance_catches_silent_qsv_selection_and_wrong_encoder_command(self):
        tool = self.load_tool('media_acceptance')
        self.assertTrue(hasattr(tool, 'assess_encoder'), 'Actual encoder receipt assessment is missing')
        self.assertFalse(tool.assess_encoder('cpu', 'qsv', ['-c:v', 'h264_qsv'])['passed'])
        self.assertFalse(tool.assess_encoder('cpu', 'cpu', ['-c:v', 'h264_qsv'])['passed'])
        self.assertTrue(tool.assess_encoder('cpu', 'cpu', ['-c:v', 'libx264'])['passed'])

    def test_mid_encode_cancel_requires_positive_progress_and_a_live_encoder(self):
        tool = self.load_tool('media_acceptance')
        self.assertTrue(hasattr(tool, 'should_cancel_encoding'), 'Live encoder cancellation gate is missing')
        self.assertFalse(tool.should_cancel_encoding({'phase': 'encoding', 'encoded_seconds': 0}, True))
        self.assertFalse(tool.should_cancel_encoding({'phase': 'encoding', 'encoded_seconds': 1}, False))
        self.assertFalse(tool.should_cancel_encoding({'phase': 'validating', 'encoded_seconds': 18}, True))
        self.assertTrue(tool.should_cancel_encoding({'phase': 'encoding', 'encoded_seconds': 1}, True))


if __name__ == '__main__':
    unittest.main()
