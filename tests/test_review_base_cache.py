"""Content-bound warm review reads, including cache invalidation and budgets."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import manual_review as review
from subtitle_pipeline.subtitles import Cue, render_srt


class ReviewBaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.make_base('first', 'A')
        if hasattr(review, 'ReviewBaseCache'):
            guard = patch.object(review, '_BASE_CACHE', review.ReviewBaseCache())
            guard.start()
            self.addCleanup(guard.stop)

    def factory(self, **options):
        factory = getattr(review, 'ReviewBaseCache', None)
        self.assertIsNotNone(factory, 'The bounded validated-base cache is not implemented')
        return factory(**options)

    def make_base(self, name, text):
        folder = self.root / name
        folder.mkdir()
        for name, prefix in [('原文.srt', 'original'), ('中文草稿.srt', 'translation')]:
            cues = [Cue(index * 2000, index * 2000 + 1500, f'{prefix} {text} {index}') for index in range(2)]
            (folder / name).write_text(render_srt(cues), encoding='utf-8')
        (folder / 'state.json').write_text(json.dumps({'identity': {
            'source': {'sha256': 'a' * 64}, 'language': 'ja', 'target': 'zh-CN'},
            'duration_ms': 4000}), encoding='utf-8')
        (folder / '需复核.json').write_text('[]', encoding='utf-8')
        return folder

    def state(self, change):
        path = self.folder / 'state.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        change(data)
        path.write_text(json.dumps(data), encoding='utf-8')

    def record(self, view, patches=None):
        path = self.folder / '人工校对' / '校对记录.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'version': 1, 'binding': view['binding'], 'patches': patches or {}}), encoding='utf-8')
        return path

    def test_warm_load_reuses_parse_and_base_validation_but_revalidates_overlay_and_warnings(self):
        reads = []
        original_read = Path.read_bytes
        def read(path):
            reads.append(path.name)
            return original_read(path)
        with patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser, \
                patch.object(review, '_validate_cues', wraps=review._validate_cues) as validate, \
                patch.object(review, '_warnings', wraps=review._warnings) as warnings, \
                patch.object(Path, 'read_bytes', read):
            first = review.load_review(self.folder)
            self.assertEqual(first, review.load_review(self.folder))
        self.assertEqual(parser.call_count, 2)
        self.assertEqual(validate.call_count, 3)  # first base + both overlays
        self.assertEqual(warnings.call_count, 2)
        for name in ('state.json', '原文.srt', '中文草稿.srt', '需复核.json', '校对记录.json'):
            self.assertEqual(reads.count(name), 2, name)

    def test_same_size_source_edit_with_original_mtime_is_read_and_reparsed(self):
        path = self.folder / '原文.srt'
        original = path.stat()
        with patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
            first = review.load_review(self.folder)
            path.write_bytes(path.read_bytes().replace(b'original A', b'original B'))
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(path.stat().st_size, original.st_size)
            second = review.load_review(self.folder)
            self.assertNotEqual(first['revision'], second['revision'])
            self.assertEqual(second['cues'][0]['source_text'], 'original B 0')
            self.assertEqual(parser.call_count, 4)

    def test_complete_binding_changes_invalidate_even_if_both_track_bytes_are_unchanged(self):
        with patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
            review.load_review(self.folder)
            self.state(lambda state: state.update(duration_ms=5000))
            review.load_review(self.folder)
            self.assertEqual(parser.call_count, 4)
            self.state(lambda state: state['identity']['source'].update(sha256='b' * 64))
            review.load_review(self.folder)
            self.assertEqual(parser.call_count, 6)
            self.state(lambda state: state['identity'].update(language='en'))
            review.load_review(self.folder, language='en')
            self.assertEqual(parser.call_count, 8)
            (self.folder / '需复核.json').write_text('[{"start_ms":0,"end_ms":1500,"reason":"new issue"}]', encoding='utf-8')
            self.assertEqual(review.load_review(self.folder, language='en')['cues'][0]['warnings'], ['new issue'])
            self.assertEqual(parser.call_count, 10)

    def test_changed_duration_language_identity_or_report_never_bypasses_current_checks(self):
        review.load_review(self.folder)
        state_path = self.folder / 'state.json'
        original = state_path.read_bytes()
        for change in (lambda state: state.update(duration_ms=100),
                       lambda state: state['identity'].update(language='en'),
                       lambda state: state['identity']['source'].update(sha256='bad')):
            with self.subTest(change=change):
                state_path.write_bytes(original)
                self.state(change)
                with self.assertRaises(ValueError):
                    review.load_review(self.folder)
        state_path.write_bytes(original)
        (self.folder / '需复核.json').write_text('[{"reason": 12}]', encoding='utf-8')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, '原因'):
                review.load_review(self.folder)

    def test_record_binding_conflict_is_checked_before_a_cached_base_is_returned(self):
        view = review.load_review(self.folder)
        record = self.record(view)
        before = record.read_bytes()
        self.state(lambda state: state['identity'].update(other_identity_field='changed'))
        with patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
            with self.assertRaises(review.ReviewConflict):
                review.load_review(self.folder)
            self.assertEqual(parser.call_count, 0)
        self.assertEqual(record.read_bytes(), before)

    def test_corrupt_base_record_and_overlay_are_never_replaced_by_cached_good_data(self):
        view = review.load_review(self.folder)
        track = self.folder / '原文.srt'
        good = track.read_bytes()
        track.write_bytes(b'malformed subtitle')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, '机器字幕格式'):
                review.load_review(self.folder)
        track.write_bytes(good)
        record = self.record(view)
        record.write_bytes(b'{bad json')
        with self.assertRaisesRegex(ValueError, '损坏'):
            review.load_review(self.folder)
        self.record(view, {'1': {'end_ms': 99999}})
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, '媒体范围'):
                review.load_review(self.folder)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(ValueError, '无法读取'):
                review.load_review(self.folder)

    def test_returned_view_and_base_mutations_do_not_poison_cached_values_or_binding(self):
        with patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
            view, base, _, _ = review._load(self.folder.resolve(), 'ja', 'zh-CN')
            view['cues'][0]['source_text'] = 'edited caller copy'
            view['cues'][0]['warnings'].append('caller warning')
            view['binding']['identity']['source']['sha256'] = 'c' * 64
            base[0]['source_text'] = 'changed base'
            base[0]['warnings'].append('changed base warning')
            base.pop()
            again = review.load_review(self.folder)
            self.assertEqual(again['cues'][0]['source_text'], 'original A 0')
            self.assertEqual(again['cues'][0]['warnings'], [])
            self.assertEqual(len(again['cues']), 2)
            self.assertEqual(again['binding']['identity']['source']['sha256'], 'a' * 64)
            self.assertEqual(parser.call_count, 2)

    def test_parser_and_validator_replacement_each_invalidate_validated_base(self):
        parse, validate = review.parse_srt, review._validate_cues
        first, second = Mock(side_effect=parse), Mock(side_effect=parse)
        check, replacement = Mock(side_effect=validate), Mock(side_effect=validate)
        with patch.object(review, 'parse_srt', first), patch.object(review, '_validate_cues', check):
            review.load_review(self.folder)
            review.load_review(self.folder)
            self.assertEqual(first.call_count, 2)
            with patch.object(review, 'parse_srt', second):
                review.load_review(self.folder)
                self.assertEqual(second.call_count, 2)
                with patch.object(review, '_validate_cues', replacement):
                    review.load_review(self.folder)
                    self.assertEqual(second.call_count, 4)
                    self.assertEqual(replacement.call_count, 2)

    def test_concurrent_reads_share_one_base_parse_and_keep_independent_results(self):
        entered, release = threading.Event(), threading.Event()
        parse = review.parse_srt
        def slow(text):
            entered.set()
            if not release.wait(3):
                raise AssertionError('parser was not released')
            return parse(text)
        with patch.object(review, 'parse_srt', side_effect=slow) as parser, ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(review.load_review, self.folder)
            pending = []
            try:
                self.assertTrue(entered.wait(2))
                pending = [pool.submit(review.load_review, self.folder) for _ in range(3)]
            finally:
                release.set()
            views = [future.result(timeout=3) for future in [first, *pending]]
            self.assertEqual(parser.call_count, 2)
            views[0]['cues'][0]['warnings'].append('caller change')
            self.assertTrue(all(not view['cues'][0]['warnings'] for view in views[1:]))

    def test_entry_budget_evicts_least_recently_used_binding(self):
        second, third = self.make_base('second', 'B'), self.make_base('third', 'C')
        with patch.object(review, '_BASE_CACHE', self.factory(max_entries=2)), \
                patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
            for folder in (self.folder, second, self.folder, third, self.folder):
                review.load_review(folder)
            self.assertEqual(parser.call_count, 6)
            review.load_review(second)
            self.assertEqual(parser.call_count, 8)

    def test_source_byte_and_cue_budgets_evict_entries(self):
        second = self.make_base('second', 'B')
        size = sum((self.folder / name).stat().st_size for name in ('原文.srt', '中文草稿.srt'))
        for options in ({'max_source_bytes': size}, {'max_cues': 2}):
            with self.subTest(options=options), patch.object(review, '_BASE_CACHE', self.factory(**options)), \
                    patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
                for folder in (self.folder, second, self.folder):
                    review.load_review(folder)
                self.assertEqual(parser.call_count, 6)

    def test_oversized_or_disabled_cache_never_retains_base_but_still_validates(self):
        for options in ({'max_entries': 0}, {'max_source_bytes': 1}, {'max_cues': 1}):
            with self.subTest(options=options), patch.object(review, '_BASE_CACHE', self.factory(**options)), \
                    patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser, \
                    patch.object(review, '_validate_cues', wraps=review._validate_cues) as validate:
                self.assertEqual(review.load_review(self.folder), review.load_review(self.folder))
                self.assertEqual(parser.call_count, 4)
                self.assertEqual(validate.call_count, 4)

    def test_invalid_limits_are_rejected(self):
        for name in ('max_entries', 'max_source_bytes', 'max_cues'):
            for value in (-1, True, 1.5):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.factory(**{name: value})

    def test_current_manual_revision_and_cas_are_not_reused_from_warm_view(self):
        with patch.object(review, 'parse_srt', wraps=review.parse_srt) as parser:
            old = review.load_review(self.folder)
            path = self.record(old, {'1': {'note': 'external edit'}})
            current = review.load_review(self.folder)
            self.assertNotEqual(old['revision'], current['revision'])
            self.assertEqual(current['cues'][0]['note'], 'external edit')
            before = path.read_bytes()
            cue = old['cues'][0]
            with self.assertRaises(review.ReviewConflict):
                review.save_review(self.folder, {key: cue[key] for key in
                    ('start_ms', 'end_ms', 'source_text', 'target_text', 'note', 'review_status')} |
                    {'expected_revision': old['revision'], 'cue_id': 1, 'translation_confirmed': False})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(parser.call_count, 2)


if __name__ == '__main__':
    unittest.main()
