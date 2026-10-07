"""Offline manual review persistence, conflict detection and coherent exports."""
import hashlib
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


class ManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'project'
        self.folder.mkdir()
        self.make_base()

    def api(self):
        try:
            return importlib.import_module('subtitle_pipeline.manual_review')
        except ModuleNotFoundError:
            self.fail('manual review persistence API is missing')

    def make_base(self, count=3):
        source = [Cue(i * 2000, i * 2000 + 1500, f'原文 {i}') for i in range(count)]
        target = [Cue(c.start_ms, c.end_ms, f'译文 {i}') for i, c in enumerate(source)]
        (self.folder / '原文.srt').write_text(render_srt(source), encoding='utf-8')
        (self.folder / '中文草稿.srt').write_text(render_srt(target), encoding='utf-8')
        (self.folder / '双语草稿.srt').write_text('machine bilingual sentinel', encoding='utf-8')
        (self.folder / 'state.json').write_text(json.dumps({
            'identity': {'source': {'sha256': 'a' * 64}, 'language': 'ja', 'target': 'zh-CN'},
            'duration_ms': count * 2000, 'status': 'complete'}), encoding='utf-8')
        (self.folder / '需复核.json').write_text(json.dumps([
            {'start_ms': 1200, 'end_ms': 2300, 'reason': '自动疑点'}]), encoding='utf-8')

    def update(self, view, cue_id=1, **changes):
        cue = view['cues'][cue_id - 1] if type(cue_id) is int and 1 <= cue_id <= len(view['cues']) else view['cues'][0]
        data = {key: cue[key] for key in
                ('start_ms', 'end_ms', 'source_text', 'target_text', 'review_status', 'note')}
        data.update(expected_revision=view['revision'], cue_id=cue_id, translation_confirmed=False)
        data.update(changes)
        return self.api().save_review(self.folder, data)

    def originals(self):
        return {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}

    def test_load_is_read_only_deterministic_and_does_not_check_automatic_warnings(self):
        api = self.api()
        originals = self.originals()
        view = api.load_review(self.folder)
        self.assertEqual(view, api.load_review(self.folder))
        self.assertFalse(view['exists'])
        self.assertEqual(view['summary'], {'total': 3, 'checked': 0, 'issues': 0,
                         'pending_translation': 0, 'required_checks': 3, 'can_accept': False})
        self.assertEqual([cue['warnings'] for cue in view['cues']],
                         [['自动疑点'], ['自动疑点'], []])
        self.assertEqual(self.originals(), originals)
        self.assertFalse((self.folder / '人工校对').exists())

    def test_edits_and_explicit_status_survive_restart_without_touching_originals(self):
        api = self.api()
        originals = self.originals()
        before = api.load_review(self.folder)
        saved = self.update(before, source_text='人工原文', target_text='人工译文',
                            review_status='checked', note='听音确认')
        self.assertNotEqual(saved['revision'], before['revision'])
        self.assertTrue(saved['exists'])
        api = importlib.reload(api)
        self.assertEqual(api.load_review(self.folder), saved)
        self.assertEqual(saved['cues'][0]['source_text'], '人工原文')
        self.assertEqual(saved['summary']['checked'], 1)
        self.assertEqual(self.originals(), originals)
        record = self.folder / '人工校对' / '校对记录.json'
        self.assertEqual(hashlib.sha256(record.read_bytes()).hexdigest(), saved['revision'])
        self.assertEqual(set(json.loads(record.read_text(encoding='utf-8'))['patches']), {'1'})

    def test_source_edit_makes_translation_stale_until_human_confirms_or_edits_target(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), review_status='checked')
        view = self.update(view, source_text='修正原文', review_status='unchecked')
        self.assertTrue(view['cues'][0]['translation_stale'])
        self.assertEqual(view['summary']['pending_translation'], 1)
        with self.assertRaises(ValueError):
            self.update(view, review_status='checked')
        view = self.update(view, translation_confirmed=True, review_status='checked')
        self.assertFalse(view['cues'][0]['translation_stale'])
        view = self.update(view, source_text='再次修正', review_status='unchecked')
        view = self.update(view, target_text='更新的译文')
        self.assertFalse(view['cues'][0]['translation_stale'])

    def test_timing_changes_are_shared_allow_overlaps_and_clear_check_unless_explicit(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), review_status='checked')
        data = dict(expected_revision=view['revision'], cue_id=1, start_ms=100, end_ms=2200,
                    source_text='原文 0', target_text='译文 0', note='', translation_confirmed=False)
        view = api.save_review(self.folder, data)
        self.assertEqual(view['cues'][0]['review_status'], 'unchecked')
        self.assertEqual((view['cues'][0]['start_ms'], view['cues'][0]['end_ms']), (100, 2200))
        view = self.update(view, end_ms=2300, review_status='checked')
        self.assertEqual(view['cues'][0]['review_status'], 'checked')

    def test_two_writers_same_revision_only_one_wins(self):
        api = self.api()
        view = api.load_review(self.folder)
        barrier = threading.Barrier(2)
        def writer(note):
            barrier.wait()
            try:
                return self.update(view, note=note)
            except api.ReviewConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ['first', 'second']))
        self.assertEqual(sum(result is not None for result in results), 1)
        winner = next(result for result in results if result is not None)
        self.assertEqual(api.load_review(self.folder), winner)

    def test_changed_original_binding_blocks_load_and_save_preserving_record(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), note='preserve me')
        record = self.folder / '人工校对' / '校对记录.json'
        before = record.read_bytes()
        (self.folder / '原文.srt').write_text('changed corrupt source', encoding='utf-8')
        with self.assertRaises(api.ReviewConflict):
            api.load_review(self.folder)
        with self.assertRaises(api.ReviewConflict):
            self.update(view, note='overwrite')
        self.assertEqual(record.read_bytes(), before)

    def test_corrupt_record_is_rejected_and_never_replaced(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), note='saved')
        record = self.folder / '人工校对' / '校对记录.json'
        for content in ('{invalid', '{}', '{"version":1,"version":1}',
                        json.dumps({'version': True, 'binding': {}, 'patches': {}})):
            with self.subTest(content=content):
                record.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.update(view, note='overwrite')
                self.assertEqual(record.read_text(encoding='utf-8'), content)

    def test_invalid_shapes_ranges_text_and_flags_do_not_change_saved_record(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), note='keep')
        before = (self.folder / '人工校对' / '校对记录.json').read_bytes()
        for changes in ({'cue_id': True}, {'cue_id': 0}, {'cue_id': 4},
                        {'start_ms': True}, {'start_ms': 1.5}, {'start_ms': -1},
                        {'end_ms': 0}, {'end_ms': 6001}, {'start_ms': 2100, 'end_ms': 2200},
                        {'source_text': ''}, {'source_text': 'bad\n\nblock'},
                        {'target_text': '...'}, {'target_text': 'x\x00'},
                        {'target_text': 'x' * 4001}, {'note': 'x' * 2001},
                        {'review_status': 'complete'}, {'translation_confirmed': 'false'},
                        {'expected_revision': 'wrong'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.update(view, **changes)
        self.assertEqual((self.folder / '人工校对' / '校对记录.json').read_bytes(), before)

    def test_missing_identity_duration_or_misaligned_translation_fails_clearly(self):
        api = self.api()
        state_path = self.folder / 'state.json'
        state = json.loads(state_path.read_text())
        for invalid in ({}, {**state, 'identity': {}}, {**state, 'duration_ms': True}):
            with self.subTest(state=invalid):
                state_path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    api.load_review(self.folder)
        state_path.write_text(json.dumps(state))
        (self.folder / '中文草稿.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n译文\n')
        with self.assertRaises(ValueError):
            api.load_review(self.folder)

    def test_acceptance_requires_short_sample_all_or_twenty_and_no_manual_issue(self):
        api = self.api()
        view = api.load_review(self.folder)
        for cue_id in range(1, 4):
            view = self.update(view, cue_id, review_status='checked')
        self.assertTrue(view['summary']['can_accept'])
        view = self.update(view, 3, review_status='issue', note='需核对')
        self.assertFalse(view['summary']['can_accept'])
        self.assertEqual(view['summary']['issues'], 1)
        # Another project checks the long-track threshold independently.
        self.folder = self.folder.parent / 'long'
        self.folder.mkdir()
        self.make_base(21)
        view = api.load_review(self.folder)
        for cue_id in range(1, 21):
            view = self.update(view, cue_id, review_status='checked')
        self.assertEqual(view['summary']['required_checks'], 20)
        self.assertTrue(view['summary']['can_accept'])
        view = self.update(view, 21, source_text='尚未翻译')
        self.assertFalse(view['summary']['can_accept'])

    def test_materialization_is_coherent_immutable_and_corruption_is_not_overwritten(self):
        api = self.api()
        originals = self.originals()
        view = self.update(api.load_review(self.folder), source_text='人工原文', target_text='人工译文',
                           start_ms=100, end_ms=1600, review_status='checked')
        self.assertFalse((self.folder / '人工校对' / '版本').exists())
        with self.assertRaises(ValueError):
            api.materialize_review(self.folder, require_accepted=True)
        snapshot = api.materialize_review(self.folder)
        self.assertEqual(snapshot['revision'], view['revision'])
        self.assertEqual(snapshot['folder'].name, view['revision'])
        self.assertEqual(len(snapshot['files']), 4)
        self.assertEqual(api.materialize_review(self.folder), snapshot)
        folder = snapshot['folder']
        source = parse_srt((folder / '原文.srt').read_text(encoding='utf-8'))
        target = parse_srt((folder / '中文草稿.srt').read_text(encoding='utf-8'))
        dual = parse_srt((folder / '双语草稿.srt').read_text(encoding='utf-8'))
        self.assertEqual(source[0], Cue(100, 1600, '人工原文'))
        self.assertEqual(target[0], Cue(100, 1600, '人工译文'))
        self.assertEqual(dual[0], Cue(100, 1600, '人工译文\n人工原文'))
        self.assertEqual(hashlib.sha256((folder / '校对记录.json').read_bytes()).hexdigest(),
                         view['revision'])
        self.assertEqual(self.originals(), originals)
        (folder / '原文.srt').write_text('tampered', encoding='utf-8')
        with self.assertRaises(api.ReviewConflict):
            api.materialize_review(self.folder)
        self.assertEqual((folder / '原文.srt').read_text(encoding='utf-8'), 'tampered')

    def test_materialized_old_revision_stays_unchanged_after_further_edit(self):
        api = self.api()
        view = api.load_review(self.folder)
        first = api.materialize_review(self.folder)
        content = (first['folder'] / '原文.srt').read_bytes()
        view = self.update(view, source_text='new', translation_confirmed=True)
        second = api.materialize_review(self.folder)
        self.assertNotEqual(first['folder'], second['folder'])
        self.assertEqual((first['folder'] / '原文.srt').read_bytes(), content)

    def test_persisted_invalid_patch_and_state_change_are_not_silently_repaired(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), note='keep')
        path = self.folder / '人工校对' / '校对记录.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for patches in ({'4': {'note': 'bad'}}, {'1': {'unknown': 'bad'}},
                        {'1': {'translation_stale': 'false'}},
                        {'1': {'review_status': 'checked', 'translation_stale': True}},
                        {'1': {'start_ms': True}}, {'01': {'note': 'bad'}}):
            with self.subTest(patches=patches):
                raw = json.dumps({**original, 'patches': patches})
                path.write_text(raw, encoding='utf-8')
                with self.assertRaises(ValueError):
                    api.load_review(self.folder)
                self.assertEqual(path.read_text(encoding='utf-8'), raw)
        path.write_text(json.dumps(original), encoding='utf-8')
        state_path = self.folder / 'state.json'
        state = json.loads(state_path.read_text())
        state['duration_ms'] += 1
        state_path.write_text(json.dumps(state))
        with self.assertRaises(api.ReviewConflict):
            api.load_review(self.folder)

    def test_checked_snapshot_can_be_required_for_acceptance(self):
        api = self.api()
        view = api.load_review(self.folder)
        for cue_id in range(1, 4):
            view = self.update(view, cue_id, review_status='checked')
        result = api.materialize_review(self.folder, require_accepted=True)
        self.assertTrue(result['summary']['can_accept'])
        for item in result['files']:
            self.assertEqual(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest(), item['sha256'])

    def test_linked_review_directory_cannot_write_outside_project(self):
        api = self.api()
        outside = self.folder.parent / 'outside'
        outside.mkdir()
        link = self.folder / '人工校对'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
            self.addCleanup(lambda: link.rmdir())
        else:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(ValueError):
            api.load_review(self.folder)
        with self.assertRaises(ValueError):
            api.materialize_review(self.folder)
        self.assertEqual(list(outside.iterdir()), [])

    def test_warning_ranges_map_long_overlaps_points_and_ignore_removed_invalid_cues(self):
        api = self.api()
        source = [Cue(0, 5000, 'long'), Cue(2000, 3500, 'short'), Cue(4000, 5500, 'last')]
        for name in ('原文.srt', '中文草稿.srt'):
            (self.folder / name).write_text(render_srt(source), encoding='utf-8')
        issues = [{'start_ms': 4000, 'end_ms': 4000, 'reason': 'point at start'},
                  {'start_ms': 3500, 'end_ms': 3500, 'reason': 'point at end'},
                  {'start_ms': 1500, 'end_ms': 2000, 'reason': 'range boundary'},
                  {'start_ms': 4500, 'end_ms': 4400, 'reason': 'removed invalid cue'},
                  {'reason': 'untimed warning'}]
        (self.folder / '需复核.json').write_text(json.dumps(issues), encoding='utf-8')
        view = api.load_review(self.folder)
        self.assertEqual([cue['warnings'] for cue in view['cues']], [
            ['point at start', 'point at end', 'range boundary'], [], ['point at start']])

    def test_interrupted_atomic_replace_preserves_previous_revision(self):
        api = self.api()
        view = self.update(api.load_review(self.folder), note='kept')
        path = self.folder / '人工校对' / '校对记录.json'
        before = path.read_bytes()
        with patch('subtitle_pipeline.runner.replace_with_retry', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.update(view, note='not saved')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(api.load_review(self.folder), view)
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows extended path resolution')
    def test_windows_extended_path_alias_does_not_reject_valid_review_lock(self):
        api = self.api()
        view = api.load_review(self.folder)
        original_resolve = Path.resolve
        def extended(path, *args, **kwargs):
            result = original_resolve(path, *args, **kwargs)
            if path.name == 'review.guard.lock' and not str(result).startswith('\\\\?\\'):
                return Path('\\\\?\\' + str(result))
            return result
        with patch.object(Path, 'resolve', extended):
            saved = self.update(view, note='valid Windows alias')
        self.assertEqual(saved['cues'][0]['note'], 'valid Windows alias')


if __name__ == '__main__':
    unittest.main()
