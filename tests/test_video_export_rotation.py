"""Export compares the displayed picture and lays captions onto that canvas."""
from copy import deepcopy
import unittest

from subtitle_pipeline import media_export
from tests import test_source_video_export as fixtures


def media(angle=None, *, width=640, height=360):
    video = {'codec_type': 'video', 'width': width, 'height': height,
             'r_frame_rate': '30/1', 'nb_frames': '120'}
    if angle is not None:
        video['side_data_list'] = [{'side_data_type': 'Display Matrix', 'rotation': angle}]
    return {'streams': [video], 'format': {'duration': '4.0'}}


class VideoRotationParameterTests(unittest.TestCase):
    def test_transposed_display_matches_unrotated_encoded_picture(self):
        for angle in (90, -90, 270, 450, -450, 89.5, 269.6):
            with self.subTest(angle=angle):
                source = media(angle)
                output = media(width=360, height=640)
                self.assertEqual(media_export._validate_video_parameters(source, output), output['streams'][0])

    def test_self_preflight_and_non_transposed_angles_keep_their_canvas(self):
        for angle in (None, 0, 180, -180, 360, 45, 89, 91, 90.6, 270.6):
            with self.subTest(angle=angle):
                source = media(angle)
                self.assertEqual(media_export._validate_video_parameters(source, source), source['streams'][0])
                media_export._validate_video_parameters(source, media())

    def test_matrix_overrides_old_rotate_tag_and_legacy_tag_is_supported(self):
        source = media(90)
        source['streams'][0]['tags'] = {'rotate': '0'}
        media_export._validate_video_parameters(source, media(width=360, height=640))
        source = media()
        source['streams'][0]['tags'] = {'rotate': '-90'}
        media_export._validate_video_parameters(source, media(width=360, height=640))

    def test_wrong_dimensions_are_still_rejected(self):
        for source, output in ((media(90), media()),
                               (media(), media(width=360, height=640)),
                               (media(90), media(width=362, height=640)),
                               (media(90), media(width=360, height=642))):
            with self.subTest(source=source, output=output), self.assertRaisesRegex(ValueError, '参数不一致'):
                media_export._validate_video_parameters(source, output)

    def test_full_matrix_preserves_precision_lost_in_probe_rotation(self):
        # Actual ffprobe matrices: its printed rotation truncates, FFmpeg rounds.
        for rotation, a, b, c, d, expected in (
                (89, 458, -65534, 65534, 458, (360, 640)),
                (90, -686, -65532, 65532, -686, (640, 360)),
                (-89, 458, 65534, -65534, 458, (360, 640)),
                (-90, -686, 65532, -65532, -686, (640, 360)),
                (88, 1144, -65526, 65526, 1144, (640, 360)),
                (91, -1144, -65526, 65526, -1144, (640, 360))):
            with self.subTest(rotation=rotation, matrix=(a, b, c, d)):
                source = media(rotation)
                source['streams'][0]['side_data_list'][0]['displaymatrix'] = (
                    f'\n00000000: {a} {b} 0\n00000001: {c} {d} 0\n00000002: 0 0 1073741824\n')
                output = media(width=expected[0], height=expected[1])
                media_export._validate_video_parameters(source, output)
                source['streams'][0]['side_data_list'][0]['rotation'] = 0
                media_export._validate_video_parameters(source, output)

    def test_invalid_full_matrix_cannot_silently_fall_back_to_integer_rotation(self):
        for matrix in ('', '00000000: 0 -65536 0',
                       '00000000: 0 0 0\n00000001: 0 0 0\n00000002: 0 0 1073741824'):
            with self.subTest(matrix=matrix), self.assertRaisesRegex(ValueError, '旋转信息无效'):
                source = media(90)
                source['streams'][0]['side_data_list'][0]['displaymatrix'] = matrix
                media_export._validate_video_parameters(source, media(width=360, height=640))


class SourceRotationRenderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SourceVideoExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_rotated_source_uses_upright_subtitle_canvas_and_reuses_published_video(self):
        fixture = self.fixture
        source_info = deepcopy(fixture.info)
        source_info['streams'][0]['side_data_list'] = [
            {'side_data_type': 'Display Matrix', 'rotation': 90}]
        output_info = deepcopy(fixture.info)
        output_info['streams'][0].update(width=360, height=640)
        fixture.probe.side_effect = lambda path, *args: source_info if path == fixture.source else output_info
        record = fixture.export(encoder='cpu')
        final = fixture.current()
        self.assertIsNotNone(final)
        self.assertEqual(record['status'], 'draft_exported')
        ass = next(fixture.project.glob('导出/原文视频/*/captions.ass')).read_text(encoding='utf-8')
        self.assertIn('PlayResX: 360', ass)
        self.assertIn('PlayResY: 640', ass)
        self.assertIn(f'Style: Default,Microsoft YaHei,{round(640 * 25 / 474)},', ass)
        count = fixture.encodes()
        fixture.export(encoder='cpu')
        self.assertEqual(fixture.encodes(), count)
        self.assertEqual(fixture.current(), final)


if __name__ == '__main__':
    unittest.main()
