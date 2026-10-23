"""Source-only overlays must never manufacture or accept translated subtitles."""
import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import manual_review as api
from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


class SourceOnlyReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.state = {'identity': {'source': {'sha256': 'a' * 64},
                                  'language': 'ja', 'target': 'zh-CN'},
                      'duration_ms': 4000, 'config': {'translate': False}}
        self.write_state()
        self.folder.joinpath('原文.srt').write_text(
            render_srt([Cue(0, 1000, '原文'), Cue(2000, 3000, 'second')]), encoding='utf-8')
        self.folder.joinpath('需复核.json').write_text(json.dumps([
            {'start_ms': 0, 'end_ms': 1000, 'reason': 'listen again'}]), encoding='utf-8')

    def write_state(self):
        self.folder.joinpath('state.json').write_text(json.dumps(self.state), encoding='utf-8')

    def originals(self):
        return {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}

    def request(self, view, **changes):
        cue_id = changes.get('cue_id', 1)
        cue = view['cues'][cue_id - 1] if type(cue_id) is int and 1 <= cue_id <= 2 else view['cues'][0]
        data = {k: cue[k] for k in ('start_ms', 'end_ms', 'source_text', 'target_text',
                                    'review_status', 'note')}
        data.update(expected_revision=view['revision'], cue_id=cue_id,
                    translation_confirmed=False)
        data.update(changes)
        return data

    def save(self, view, **changes):
        return api.save_review(self.folder, self.request(view, **changes))

    def record_path(self):
        return self.folder / '人工校对' / '校对记录.json'

    def test_source_only_load_does_not_write(self):
        before = self.originals()
        view = api.load_review(self.folder)
        self.assertEqual(view['mode'], 'source_only')
        self.assertIs(view['acceptance_supported'], False)
        self.assertFalse(view['exists'])
        self.assertEqual(view, api.load_review(self.folder))
        self.assertEqual(view['cues'][0]['warnings'], ['listen again'])
        self.assertTrue(all(c['target_text'] == '' and c['translation_stale'] is False
                            for c in view['cues']))
        self.assertEqual(self.originals(), before)
        self.assertFalse(self.folder.joinpath('人工校对').exists())

    def test_source_only_edit_and_materialize_preserve_machine_bytes(self):
        before = self.originals()
        view = self.save(api.load_review(self.folder), source_text='corrected',
                         start_ms=100, end_ms=1200, review_status='checked', note='heard')
        self.assertEqual(view['cues'][0]['review_status'], 'checked')
        self.assertFalse(view['cues'][0]['translation_stale'])
        raw = self.record_path().read_bytes()
        record = json.loads(raw)
        self.assertEqual(set(record), {'version', 'mode', 'binding', 'patches'})
        self.assertEqual(record['version'], 2)
        self.assertEqual(record['mode'], 'source_only')
        self.assertEqual(record['binding']['mode'], 'source_only')
        self.assertNotIn('target_sha256', record['binding'])
        self.assertEqual(record['patches'], {'1': {'start_ms': 100, 'end_ms': 1200,
                         'source_text': 'corrected', 'review_status': 'checked', 'note': 'heard'}})
        self.assertEqual(hashlib.sha256(raw).hexdigest(), view['revision'])
        self.assertEqual(importlib.reload(api).load_review(self.folder), view)
        result = api.materialize_review(self.folder)
        self.assertEqual(result['mode'], 'source_only')
        self.assertIs(result['acceptance_supported'], False)
        self.assertEqual(result['revision'], view['revision'])
        self.assertEqual({p.name for p in result['folder'].iterdir()}, {'原文.srt', '校对记录.json'})
        self.assertEqual(result['folder'].joinpath('校对记录.json').read_bytes(), raw)
        self.assertEqual(parse_srt(result['folder'].joinpath('原文.srt').read_text(encoding='utf-8'))[0],
                         Cue(100, 1200, 'corrected'))
        self.assertEqual(api.materialize_review(self.folder), result)
        self.assertEqual(self.originals(), before)

    def test_content_change_clears_omitted_status_and_note_survives(self):
        view = self.save(api.load_review(self.folder), review_status='checked', note='keep')
        request = self.request(view, source_text='changed')
        request.pop('review_status')
        view = api.save_review(self.folder, request)
        self.assertEqual(view['cues'][0]['review_status'], 'unchecked')
        self.assertEqual(view['cues'][0]['note'], 'keep')
        view = self.save(view, review_status='issue', note='unclear word')
        self.assertEqual(view['summary']['issues'], 1)
        self.assertEqual(view['cues'][0]['note'], 'unclear word')
        request = self.request(view, note='clarified issue')
        request.pop('review_status')
        self.assertEqual(api.save_review(self.folder, request)['cues'][0]['review_status'], 'issue')

    def test_every_checked_source_still_cannot_be_accepted(self):
        view = self.save(api.load_review(self.folder), review_status='checked')
        view = self.save(view, cue_id=2, review_status='checked')
        self.assertEqual(view['summary']['checked'], 2)
        self.assertEqual(view['summary']['pending_translation'], 0)
        self.assertFalse(view['summary']['can_accept'])
        with self.assertRaisesRegex(ValueError, '原文'):
            api.materialize_review(self.folder, require_accepted=True)
        self.assertFalse(self.folder.joinpath('人工校对/版本').exists())

    def test_hidden_translation_and_invalid_requests_preserve_saved_bytes(self):
        view = self.save(api.load_review(self.folder), note='preserve')
        before = self.record_path().read_bytes()
        for changes in ({'target_text': 'hidden'}, {'target_text': None}, {'target_text': ' '},
                        {'translation_confirmed': True}, {'translation_confirmed': 0},
                        {'mode': 'source_only'}, {'start_ms': True}, {'end_ms': 4001},
                        {'source_text': ''}, {'source_text': 'a\n\nb'}, {'note': 'x' * 2001},
                        {'review_status': 'accepted'}, {'cue_id': True}, {'cue_id': 0},
                        {'expected_revision': 'stale'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save(view, **changes)
        self.assertEqual(self.record_path().read_bytes(), before)

    def test_cas_concurrent_writers_only_one_commits(self):
        view = api.load_review(self.folder)
        barrier = threading.Barrier(2)
        def writer(note):
            barrier.wait()
            try:
                return self.save(view, note=note)
            except api.ReviewConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ['first', 'second']))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(api.load_review(self.folder), next(x for x in results if x is not None))

    def test_changed_base_or_issues_conflicts_and_retains_overlay(self):
        view = self.save(api.load_review(self.folder), note='keep')
        before = self.record_path().read_bytes()
        for name in ('原文.srt', '需复核.json', 'state.json'):
            path = self.folder / name
            original = path.read_bytes()
            path.write_bytes(b'changed')
            with self.subTest(name=name), self.assertRaises(api.ReviewConflict):
                api.load_review(self.folder)
            with self.assertRaises(api.ReviewConflict):
                self.save(view, note='overwrite')
            self.assertEqual(self.record_path().read_bytes(), before)
            path.write_bytes(original)

    def test_save_and_publication_recheck_single_track_qualification(self):
        view = self.save(api.load_review(self.folder), note='keep')
        before = self.record_path().read_bytes()
        original_base = api._base
        reads = 0
        def changed_base(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                (self.folder / '双语草稿.srt').write_bytes(b'new cached output')
            return original_base(*args, **kwargs)
        with patch.object(api, '_base', side_effect=changed_base):
            with self.assertRaises(ValueError):
                self.save(view, note='not saved')
        self.assertEqual(self.record_path().read_bytes(), before)
        self.assertEqual(reads, 2)
        (self.folder / '双语草稿.srt').unlink()
        reads = 0
        with patch.object(api, '_base', side_effect=changed_base):
            with self.assertRaises(ValueError):
                api.materialize_review(self.folder)
        self.assertEqual(reads, 2)
        self.assertFalse(any(self.folder.glob('人工校对/版本/*')))

    def test_commit_time_source_drift_does_not_save_or_publish(self):
        view = self.save(api.load_review(self.folder), note='keep')
        before = self.record_path().read_bytes()
        source = self.folder / '原文.srt'
        original = source.read_bytes()
        original_base = api._base
        reads = 0
        def changed_base(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                source.write_bytes(original.replace('原文'.encode(), b'changed'))
            return original_base(*args, **kwargs)
        for operation in (lambda: self.save(view, note='not saved'),
                          lambda: api.materialize_review(self.folder)):
            reads = 0
            source.write_bytes(original)
            with patch.object(api, '_base', side_effect=changed_base):
                with self.assertRaises(api.ReviewConflict):
                    operation()
            self.assertEqual(reads, 2)
            self.assertEqual(self.record_path().read_bytes(), before)
            self.assertFalse(any(self.folder.glob('人工校对/版本/*')))

    def test_existing_v2_mode_or_tracks_change_conflicts(self):
        view = self.save(api.load_review(self.folder), note='keep')
        before = self.record_path().read_bytes()
        for config in ({}, {'translate': True}, {'translate': None}, {'translate': 0}):
            self.state['config'] = config
            self.write_state()
            with self.subTest(config=config), self.assertRaises(api.ReviewConflict):
                api.load_review(self.folder)
            self.assertEqual(self.record_path().read_bytes(), before)
        self.state['config'] = {'translate': False}
        self.write_state()
        for name in ('中文草稿.srt', '英文草稿.srt', '日文草稿.srt', '双语草稿.srt'):
            path = self.folder / name
            path.write_bytes(b'')
            with self.subTest(name=name), self.assertRaises(api.ReviewConflict):
                self.save(view, note='overwrite')
            self.assertEqual(self.record_path().read_bytes(), before)
            path.unlink()

    def test_unknown_translation_config_does_not_infer_source_mode(self):
        for config in (None, {}, {'translate': True}, {'translate': 0}, {'translate': 'false'}):
            self.state['config'] = config
            self.write_state()
            with self.subTest(config=config), self.assertRaises(ValueError):
                api.load_review(self.folder)
        self.assertFalse(self.record_path().exists())

    def test_cached_translated_tracks_are_not_source_only(self):
        for name in ('中文草稿.srt', '英文草稿.srt', '日文草稿.srt', '双语草稿.srt'):
            path = self.folder / name
            path.write_bytes(b'bad cached target')
            with self.subTest(name=name), self.assertRaises(ValueError):
                api.load_review(self.folder)
            path.unlink()
        self.assertFalse(self.record_path().exists())

    def test_valid_cached_bilingual_base_retains_v1_contract(self):
        self.folder.joinpath('中文草稿.srt').write_text(
            render_srt([Cue(0, 1000, '译文'), Cue(2000, 3000, 'translation')]), encoding='utf-8')
        view = api.load_review(self.folder)
        self.assertNotIn('mode', view)
        self.assertNotIn('acceptance_supported', view)
        self.assertEqual(view['cues'][0]['target_text'], '译文')
        self.save(view, note='v1 cached')
        self.assertEqual(json.loads(self.record_path().read_bytes())['version'], 1)

    def test_source_mode_requires_valid_identity_duration_and_requested_languages(self):
        original = json.loads(json.dumps(self.state))
        for state in ({**original, 'identity': {}}, {**original, 'duration_ms': True},
                      {**original, 'duration_ms': 0},
                      {**original, 'identity': {**original['identity'], 'source': {'sha256': 'bad'}}}):
            self.state = state
            self.write_state()
            with self.subTest(state=state), self.assertRaises(ValueError):
                api.load_review(self.folder)
        self.state = original
        self.write_state()
        for language, target in (('en', 'zh-CN'), ('ja', 'en'), ('unknown', 'zh-CN')):
            with self.subTest(language=language, target=target), self.assertRaises(ValueError):
                api.load_review(self.folder, language=language, target=target)
        self.assertFalse(self.record_path().exists())

    def test_auto_language_is_allowed_for_source_only_not_v1(self):
        self.state['identity']['language'] = 'auto'
        self.write_state()
        view = api.load_review(self.folder, language='auto')
        self.assertEqual(view['binding']['language'], 'auto')
        data = self.request(view, source_text='detected source', review_status='checked')
        view = api.save_review(self.folder, data, language='auto')
        self.assertEqual(api.materialize_review(self.folder, language='auto')['revision'], view['revision'])
        self.record_path().unlink()
        self.folder.joinpath('中文草稿.srt').write_text(render_srt([Cue(0, 1000, '译文')]), encoding='utf-8')
        with self.assertRaises(ValueError):
            api.load_review(self.folder, language='auto')

    def test_missing_corrupt_state_and_source_do_not_create_records(self):
        for name in ('state.json', '原文.srt'):
            path = self.folder / name
            original = path.read_bytes()
            path.unlink()
            with self.subTest(name=name, kind='missing'), self.assertRaises(ValueError):
                api.load_review(self.folder)
            path.write_bytes(b'corrupt')
            with self.subTest(name=name, kind='corrupt'), self.assertRaises(ValueError):
                api.load_review(self.folder)
            path.write_bytes(original)
        self.assertFalse(self.record_path().exists())

    def test_corrupt_or_unknown_existing_records_are_preserved(self):
        view = self.save(api.load_review(self.folder), note='keep')
        original = json.loads(self.record_path().read_bytes())
        corruptions = [b'{bad', b'{}', b'{"version":2,"version":2}',
                       json.dumps({**original, 'version': 3}).encode(),
                       json.dumps({**original, 'mode': 'bilingual'}).encode(),
                       json.dumps({**original, 'patches': {'1': {'target_text': 'hidden'}}}).encode(),
                       json.dumps({**original, 'patches': {'1': {'translation_stale': False}}}).encode(),
                       json.dumps({**original, 'patches': {'01': {'note': 'bad'}}}).encode()]
        for raw in corruptions:
            self.record_path().write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.save(view, note='overwrite')
            self.assertEqual(self.record_path().read_bytes(), raw)

    def test_snapshot_content_and_exact_file_set_are_checked(self):
        result = api.materialize_review(self.folder)
        snapshot = result['folder']
        source = snapshot / '原文.srt'
        original = source.read_bytes()
        source.write_bytes(b'tamper')
        with self.assertRaises(api.ReviewConflict):
            api.materialize_review(self.folder)
        self.assertEqual(source.read_bytes(), b'tamper')
        source.write_bytes(original)
        extra = snapshot / '中文草稿.srt'
        extra.write_bytes(b'hidden')
        with self.assertRaises(api.ReviewConflict):
            api.materialize_review(self.folder)
        self.assertEqual(extra.read_bytes(), b'hidden')

    def test_v1_bytes_revision_and_snapshot_unchanged(self):
        self.folder.joinpath('原文.srt').write_text(render_srt([Cue(0, 1000, '原文')]), encoding='utf-8')
        self.folder.joinpath('中文草稿.srt').write_text(render_srt([Cue(0, 1000, '译文')]), encoding='utf-8')
        self.folder.joinpath('双语草稿.srt').write_bytes(b'machine bilingual')
        self.folder.joinpath('需复核.json').unlink()
        self.state.pop('config')
        self.state['duration_ms'] = 2000
        self.write_state()
        view = api.load_review(self.folder)
        self.assertEqual(set(view), {'revision', 'binding', 'cues', 'summary', 'exists'})
        self.assertEqual(view['revision'], '548a755b926e84f992d38ae811ea4a08e1b364f924b49b10a9a6620f38f6b6b1')
        view = self.save(view, note='baseline')
        self.assertEqual(view['revision'], 'cc0d64a6de5b1f1dbaa11ac91657275800e5ab2a776e1d9a7b9a60d371c79a4e')
        result = api.materialize_review(self.folder)
        self.assertEqual({Path(x['path']).name: x['sha256'] for x in result['files']}, {
            '原文.srt': '04914ade54729f62394fe722e11c49f14e7a6da56768d69e7e4bdeb8717c48da',
            '中文草稿.srt': '690c87783ba1960097cc8bfceb8b85055e401b8f7f63f01ffe8504f1a4b68d9f',
            '双语草稿.srt': 'c7f4a9cc148caebd2998a96d2c6d3ed3743448d91066b5a9c0b2f0a580ddd536',
            '校对记录.json': 'cc0d64a6de5b1f1dbaa11ac91657275800e5ab2a776e1d9a7b9a60d371c79a4e'})
        # Existing v1 takes priority even after config is explicitly source-only.
        self.state['config'] = {'translate': False}
        self.write_state()
        self.assertEqual(api.load_review(self.folder), view)
        self.assertEqual(json.loads(self.record_path().read_bytes())['version'], 1)


if __name__ == '__main__':
    unittest.main()
