"""Damaged preview metadata must preserve recovery and manual-edit evidence."""
import http.client
import json
import unittest
from urllib.parse import quote

from tests import test_studio as fixture
from tests import test_studio_project_recovery as recovery
from tests import test_studio_review as review_fixture


class StudioPreviewRecoveryTests(unittest.TestCase):
    setUp = fixture.StudioTests.setUp
    server = recovery.StudioProjectRecoveryTests.server

    def tracks(self):
        source = '1\n00:00:00,000 --> 00:00:01,000\nOriginal\n'
        (self.campaign / '原文.srt').write_text(source, encoding='utf-8')
        (self.campaign / '中文草稿.srt').write_text(source.replace('Original', '译文'), encoding='utf-8')

    def get(self, token, port, route):
        connection = self.s.http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            connection.request('GET', route, headers={'X-Subtitle-Token': token})
            try:
                response = connection.getresponse()
            except http.client.RemoteDisconnected:
                return 0, b''
            return response.status, response.read()
        finally:
            connection.close()

    def test_invalid_config_returns_http_error_and_retains_service_and_files(self):
        self.tracks()
        path = self.campaign / 'state.json'
        with self.server() as (token, port):
            for config in (None, [], 42, 'old'):
                path.write_text(json.dumps({'config': config}), encoding='utf-8')
                before = {p: p.read_bytes() for p in self.campaign.rglob('*') if p.is_file()}
                for route in ('/api/preview?sample=main', '/api/download?sample=main&name=' + quote('原文.srt')):
                    with self.subTest(config=config, route=route):
                        status, body = self.get(token, port, route)
                        self.assertEqual(status, 400, 'Damaged metadata must produce an HTTP error, not disconnect')
                        self.assertTrue(json.loads(body)['error'])
                status, body = self.get(token, port, '/api/state')
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)['project_error'])
                self.assertEqual(self.get(token, port, '/api/progress')[0], 200)
                self.assertEqual(before, {p: p.read_bytes() for p in before})
            path.write_text(json.dumps({'config': {'language': 'en', 'target': 'zh-CN'}}), encoding='utf-8')
            status, body = self.get(token, port, '/api/preview?sample=main')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['cues'][0]['target_text'], '译文')

    def test_invalid_unused_identity_keeps_independent_raw_tracks_readable(self):
        self.tracks()
        path = self.campaign / 'state.json'
        for identity in (None, [], {'source': None}, {'source': []}):
            with self.subTest(identity=identity):
                path.write_text(json.dumps({'config': {}, 'identity': identity, 'duration_ms': 1000}), encoding='utf-8')
                original = path.read_bytes()
                preview = self.app.preview('main')
                self.assertEqual(preview['cues'][0]['target_text'], '译文')
                self.assertNotIn('manual_review', preview)
                self.assertEqual(self.app.download_path('中文草稿.srt', 'main'), self.campaign / '中文草稿.srt')
                self.assertEqual(path.read_bytes(), original)


class StudioManualPreviewRecoveryTests(unittest.TestCase):
    setUp = review_fixture.StudioReviewTests.setUp
    write = review_fixture.StudioReviewTests.write
    request = review_fixture.StudioReviewTests.request

    def test_existing_manual_record_cannot_silently_disappear_with_damaged_base_metadata(self):
        self.app.review_cue(self.request(target_text='人工保留译文', translation_confirmed=True))
        original = dict(self.state)
        cases = [
            '{broken', '[]',
            json.dumps({key: value for key, value in original.items() if key != 'identity'}),
            json.dumps({key: value for key, value in original.items() if key != 'duration_ms'}),
            json.dumps({**original, 'identity': None}),
            json.dumps({**original, 'identity': {'source': None}}),
        ]
        path = self.folder / 'state.json'
        for content in cases:
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                before = {p: p.read_bytes() for p in self.campaign.rglob('*') if p.is_file()}
                view = self.app.preview('main')
                self.assertTrue(view.get('manual_review', {}).get('conflict'),
                                'A saved manual overlay must remain explicit when its base is invalid')
                self.assertTrue(view['issues'])
                self.assertNotIn('revision', view['manual_review'])
                with self.assertRaises(ValueError):
                    self.app.download_path('中文草稿.srt', 'main', self.app._project_id())
                self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.write(path, original)
        self.assertEqual(self.app.preview('main')['cues'][0]['target_text'], '人工保留译文')

    def test_existing_manual_record_reports_missing_track_instead_of_unsupported_review(self):
        self.app.review_cue(self.request(target_text='人工保留译文', translation_confirmed=True))
        for name in ('原文.srt', '中文草稿.srt'):
            path = self.folder / name
            content = path.read_bytes()
            path.unlink()
            try:
                view = self.app.preview('main')
                self.assertTrue(view.get('manual_review', {}).get('conflict'))
                with self.assertRaises(ValueError): self.app.download_path(name, 'main')
            finally:
                path.write_bytes(content)
        self.assertEqual(self.app.preview('main')['cues'][0]['target_text'], '人工保留译文')

    def test_healthy_manifest_and_manual_binding_survive_unrelated_config_damage(self):
        corrected = self.app.review_cue(self.request(target_text='人工保留译文', translation_confirmed=True))
        path = self.folder / 'state.json'
        self.write(path, {**self.state, 'config': None})
        original = path.read_bytes()
        view = self.app.preview('main')
        self.assertTrue(self.app.state()['project_error'])
        self.assertEqual(view['manual_review']['revision'], corrected['manual_review']['revision'])
        self.assertEqual(view['cues'][0]['target_text'], '人工保留译文')
        self.assertIn('人工保留译文', self.app.download_path('中文草稿.srt', 'main').read_text(encoding='utf-8'))
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__': unittest.main()
