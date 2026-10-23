"""Pinned browser MP4 downloads use real receipts and bounded file reads."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from subtitle_pipeline import studio
from subtitle_pipeline.integrity import sha256
from tests import test_studio_draft_video, test_studio_export_integrity, test_studio_source_video_export


class StudioVideoDownloadTests(unittest.TestCase):
    def fixture(self, kind='draft'):
        classes = {'draft': test_studio_draft_video.StudioDraftVideoTests,
                   'approved': test_studio_export_integrity.StudioExportIntegrityTests,
                   'source': test_studio_source_video_export.StudioSourceVideoPathTests}
        fixture = classes[kind](); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        if kind == 'source':
            fixture.fixture.export(encoder='cpu'); video = fixture.fixture.current()
        else:
            fixture.publish_fixture(); video = fixture.video if kind == 'draft' else fixture.output
        self.app = fixture.app; self.video = video; self.kind = 'video' if kind == 'approved' else 'draft-video'
        self.view = self.app.preview('main')
        self.metadata = self.view['exported_video' if kind == 'approved' else 'draft_video']
        return fixture

    def server(self):
        # The offline gate forbids all sockets, including loopback. Exercise
        # the real handler/controller with in-memory transport; actual browser
        # downloads and studio_http_smoke cover the loopback HTTP connection.
        self.server_instance = SimpleNamespace(server_port=7777, last_touch=0)
        self.host = '127.0.0.1:7777'; self.token = 'synthetic-download-session-token'
        self.handler_type = studio.make_handler(self.app, self.token, self.host, self.video.parent)

    def request(self, method, url=None, headers=None, authorized=True):
        fields = {'Host': self.host}
        if authorized:fields['X-Subtitle-Token'] = self.token
        fields.update(headers or {})
        handler = object.__new__(self.handler_type)
        handler.command = method; handler.path = url or self.metadata['download_url']
        handler.headers = fields; handler.wfile = io.BytesIO()
        handler.send_response = Mock(); handler.send_header = Mock(); handler.end_headers = Mock()
        handler.server = self.server_instance
        try:
            handler.do_HEAD() if method=='HEAD' else handler.do_GET()
            return (handler.send_response.call_args.args[0],
                    dict(call.args for call in handler.send_header.call_args_list), handler.wfile.getvalue())
        finally:
            if getattr(handler, '_api_request_active', False):self.app.end_request(self.server_instance)

    def test_each_export_kind_exposes_project_bound_artifact_without_token(self):
        for kind in ('source', 'draft', 'approved'):
            with self.subTest(kind=kind):
                self.fixture(kind)
                artifact = self.metadata.get('artifact_id', '')
                self.assertRegex(artifact, r'^[0-9a-f]{64}$')
                query = parse_qs(urlsplit(self.metadata['download_url']).query)
                self.assertEqual(query['artifact'], [artifact])
                self.assertEqual(query['project'], [self.view['project_id']])
                self.assertNotIn('token', query)

    def test_head_has_same_download_metadata_and_no_body(self):
        self.fixture(); self.server()
        head = self.request('HEAD'); get = self.request('GET')
        self.assertEqual(head[0], 200); self.assertEqual(head[2], b'')
        self.assertEqual(get[2], self.video.read_bytes())
        for name in ('Content-Type', 'Content-Length', 'Content-Disposition', 'X-Subtitle-Artifact'):
            self.assertEqual(head[1][name], get[1][name])
        self.assertEqual(head[1]['Content-Type'], 'video/mp4')
        self.assertEqual(head[1]['X-Subtitle-Artifact'], self.metadata['artifact_id'])
        self.assertIn("filename*=UTF-8''", head[1]['Content-Disposition'])

    def test_cookie_auth_and_cross_origin_rejection(self):
        self.fixture(); self.server()
        cookie = { 'Cookie': f'subtitle_session_{self.server_instance.server_port}={self.token}' }
        self.assertEqual(self.request('HEAD', headers=cookie, authorized=False)[0], 200)
        for method in ('HEAD', 'GET'):
            for fields in ({}, {**cookie, 'Origin': 'https://external.invalid'}):
                result = self.request(method, headers=fields, authorized=False)
                self.assertEqual(result[0], 403); self.assertNotIn('Content-Disposition', result[1])
                if method == 'HEAD': self.assertEqual(result[2], b'')

    def test_stale_project_and_artifact_are_409_without_attachment(self):
        self.fixture(); self.server(); url = self.metadata['download_url']
        for old, new in ((self.view['project_id'], 'f'*64), (self.metadata['artifact_id'], 'e'*64)):
            for method in ('HEAD', 'GET'):
                result = self.request(method, url.replace(old, new))
                self.assertEqual(result[0], 409); self.assertNotIn('Content-Disposition', result[1])
                if method == 'HEAD': self.assertEqual(result[2], b'')

    def test_head_requires_pins_but_legacy_get_stays_compatible(self):
        self.fixture(); self.server()
        url = '/api/download?name=draft-video'
        self.assertEqual(self.request('GET', url)[0], 200)
        result = self.request('HEAD', url)
        self.assertEqual(result[0], 409); self.assertEqual(result[2], b'')
        self.assertNotIn('Content-Disposition', result[1])

    def test_missing_or_corrupt_output_is_410(self):
        self.fixture(); self.server()
        self.video.write_bytes(b'corrupt')
        for method in ('HEAD', 'GET'):
            result = self.request(method)
            self.assertEqual(result[0], 410); self.assertNotIn('Content-Disposition', result[1])
        self.video.unlink()
        self.assertEqual(self.request('HEAD')[0], 410)

    def test_same_size_atomic_replacement_with_kept_mtime_is_rejected(self):
        self.fixture(); self.server(); original = self.video.stat()
        replacement = self.video.with_suffix('.replacement'); replacement.write_bytes(b'x'*original.st_size)
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns)); replacement.replace(self.video)
        self.assertEqual(self.request('HEAD')[0], 410)

    def test_range_metadata_and_bytes_stay_bound(self):
        self.fixture(); self.server()
        result = self.request('GET', headers={'Range': 'bytes=2-5'})
        self.assertEqual(result[0], 206); self.assertEqual(result[2], self.video.read_bytes()[2:6])
        self.assertEqual(result[1]['X-Subtitle-Artifact'], self.metadata['artifact_id'])
        invalid = self.request('HEAD', headers={'Range': 'bytes=999999-'})
        self.assertEqual(invalid[0], 416); self.assertEqual(invalid[2], b'')
        self.assertNotIn('Content-Disposition', invalid[1])

    def test_unchanged_video_reuses_digest_for_head_and_get(self):
        self.fixture(); self.server()
        with patch.object(studio.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            self.assertEqual(self.request('HEAD')[0], 200)
            self.assertEqual(self.request('GET')[0], 200)
        digest.assert_not_called()

    def test_old_artifact_cannot_download_a_new_output_at_same_name(self):
        fixture = self.fixture(); self.server()
        self.video.write_bytes(b'new encoded version')
        manifest = json.loads((fixture.campaign/'campaign.json').read_text(encoding='utf-8'))
        manifest['draft_output_sha256'] = sha256(self.video)
        fixture.fixture.write(fixture.campaign/'campaign.json', manifest)
        self.assertEqual(self.request('HEAD')[0], 409)

    def test_json_download_errors_allow_only_same_origin_frame(self):
        self.fixture(); self.server(); self.video.unlink()
        result = self.request('GET')
        self.assertEqual(result[0], 410)
        self.assertIn("frame-ancestors 'self'", result[1]['Content-Security-Policy'])
        state = self.request('GET', '/api/state')
        self.assertIn("frame-ancestors 'none'", state[1]['Content-Security-Policy'])

    def test_replacement_between_resolution_and_pin_never_gets_attachment(self):
        self.fixture(); self.server()
        original = studio.StudioController._video_result
        replaced = []
        def resolve(view, name):
            result = original(view, name)
            if not replaced:
                replacement = self.video.with_suffix('.replacement')
                replacement.write_bytes(b'replaced before response opened the body')
                replacement.replace(self.video); replaced.append(True)
            return result
        with patch.object(studio.StudioController, '_video_result', resolve):
            result = self.request('GET')
        self.assertEqual(result[0], 410); self.assertNotIn('Content-Disposition', result[1])

    def test_atomic_replacement_after_descriptor_keeps_verified_handle(self):
        self.fixture(); self.server(); expected = self.video.read_bytes()
        original = self.app.video_download_info
        def resolve(*args):
            result = original(*args)
            replacement = self.video.with_suffix('.replacement')
            replacement.write_bytes(b'new bytes must not enter an old attachment')
            try:replacement.replace(self.video)
            except PermissionError:
                # Windows denies replacing a pinned open file. POSIX replaces
                # the directory entry while the response retains its handle.
                if os.name!='nt':raise
            return result
        with patch.object(self.app, 'video_download_info', side_effect=resolve):
            result = self.request('GET')
        self.assertEqual(result[0], 200); self.assertEqual(result[2], expected)
        self.assertEqual(int(result[1]['Content-Length']), len(expected))

    def test_stream_reads_are_bounded_for_large_output(self):
        fixture = self.fixture()
        self.video.write_bytes(b'synthetic MP4 body'*160000)
        manifest = json.loads((fixture.campaign/'campaign.json').read_text(encoding='utf-8'))
        manifest['draft_output_sha256'] = sha256(self.video)
        fixture.fixture.write(fixture.campaign/'campaign.json', manifest)
        self.metadata = self.app.preview('main')['draft_video']; self.server()
        original = self.app.video_download_info; reads = []
        class Observed:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def seek(self, *args): return self.stream.seek(*args)
            def read(self, size): reads.append(size); return self.stream.read(size)
        def resolve(*args):
            path, stream, artifact = original(*args)
            return path, Observed(stream), artifact
        with patch.object(self.app, 'video_download_info', side_effect=resolve):
            result = self.request('GET')
        self.assertEqual(result[0], 200)
        self.assertEqual(hashlib.sha256(result[2]).hexdigest(), manifest['draft_output_sha256'])
        self.assertGreater(len(reads), 1); self.assertLessEqual(max(reads), 256*1024)

    def test_exiting_head_is_503_bodyless_and_without_attachment(self):
        self.fixture(); self.server()
        with patch.object(self.app, 'begin_request', return_value=False):
            result = self.request('HEAD')
        self.assertEqual(result[0], 503); self.assertEqual(result[2], b'')
        self.assertNotIn('Content-Disposition', result[1])

    def test_saved_subtitle_change_withdraws_old_download(self):
        fixture = self.fixture(); self.server()
        (fixture.folder/'原文.srt').write_bytes(b'changed subtitle binding')
        for method in ('HEAD', 'GET'):
            result = self.request(method)
            self.assertEqual(result[0], 410); self.assertNotIn('Content-Disposition', result[1])

    def test_malformed_current_binding_is_410_without_attachment(self):
        fixture = self.fixture(); self.server()
        manifest = json.loads((fixture.campaign/'campaign.json').read_text(encoding='utf-8'))
        manifest['source'] = []
        fixture.fixture.write(fixture.campaign/'campaign.json', manifest)
        for method in ('HEAD', 'GET'):
            result = self.request(method)
            self.assertEqual(result[0], 410); self.assertNotIn('Content-Disposition', result[1])

    def test_project_change_during_resolution_closes_the_pinned_handle(self):
        self.fixture(); opened = []
        original = studio.verified_open
        def observe(*args):
            result = original(*args)
            if result is not None:opened.append(result)
            return result
        original_read = studio.StudioController._video_result
        reads = []
        def resolve(view, name):
            result = original_read(view, name); reads.append(result)
            if len(reads)==2:
                with self.app.lock:self.app._selection_revision+=1
            return result
        with patch.object(studio, 'verified_open', side_effect=observe), \
                patch.object(studio.StudioController, '_video_result', resolve):
            with self.assertRaises(studio.ProjectSelectionChanged):
                self.app.video_download_info(self.kind, self.view['project_id'], self.metadata['artifact_id'])
        self.assertTrue(opened); self.assertTrue(all(stream.closed for stream in opened))


if __name__ == '__main__': unittest.main()
