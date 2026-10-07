import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


class SrtReadCacheTests(unittest.TestCase):
    def setUp(self):
        from subtitle_pipeline.read_cache import SrtReadCache
        self.factory = SrtReadCache
        self.cache = SrtReadCache()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.write('track.srt', 'aaa')
        self.parser = Mock(side_effect=parse_srt)

    def write(self, name, text):
        path = self.root / name
        path.write_text(render_srt([Cue(0, 1000, text)]), encoding='utf-8-sig')
        return path

    def test_unchanged_file_parsed_once_and_returns_independent_lists(self):
        result = self.cache.read(self.path, self.parser)
        result.clear()
        again = self.cache.read(self.path, self.parser)
        self.assertEqual(again, [Cue(0, 1000, 'aaa')])
        self.assertEqual(self.parser.call_count, 1)

    def test_same_size_edit_with_restored_mtime_is_not_stale(self):
        self.cache.read(self.path, self.parser)
        original = self.path.stat()
        self.write(self.path.name, 'bbb')
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, original.st_size)
        self.assertEqual(self.cache.read(self.path, self.parser)[0].text, 'bbb')
        self.assertEqual(self.parser.call_count, 2)

    def test_atomic_replacement_is_seen(self):
        self.cache.read(self.path, self.parser)
        self.write('replacement.srt', 'new').replace(self.path)
        self.assertEqual(self.cache.read(self.path, self.parser)[0].text, 'new')

    def test_deleted_unreadable_and_invalid_tracks_never_return_cached_cues(self):
        self.cache.read(self.path, self.parser)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.cache.read(self.path, self.parser)
        self.cache.read(self.path, self.parser)
        self.assertEqual(self.parser.call_count, 2)
        self.path.write_text('malformed', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.cache.read(self.path, self.parser)
        self.write(self.path.name, 'aaa')
        self.cache.read(self.path, self.parser)
        self.assertEqual(self.parser.call_count, 4)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.cache.read(self.path, self.parser)

    def test_parser_change_and_explicit_invalidation(self):
        self.cache.read(self.path, self.parser)
        other = Mock(side_effect=parse_srt)
        self.cache.read(self.path, other)
        self.assertEqual(other.call_count, 1)
        self.cache.invalidate(self.path)
        self.cache.read(self.path, other)
        self.assertEqual(other.call_count, 2)

    def test_entry_limit_evicts_least_recently_used(self):
        cache = self.factory(max_entries=2)
        second = self.write('second.srt', 'bbb')
        third = self.write('third.srt', 'ccc')
        for path in (self.path, second, self.path, third, self.path):
            cache.read(path, self.parser)
        self.assertEqual(self.parser.call_count, 3)
        cache.read(second, self.parser)
        self.assertEqual(self.parser.call_count, 4)

    def test_source_byte_limit_and_cue_limit_bound_retention(self):
        second = self.write('second.srt', 'bbb')
        for options in ({'max_source_bytes': self.path.stat().st_size}, {'max_cues': 1}):
            with self.subTest(options=options):
                cache = self.factory(**options)
                parser = Mock(side_effect=parse_srt)
                for path in (self.path, second, self.path):
                    cache.read(path, parser)
                self.assertEqual(parser.call_count, 3)

    def test_oversized_tracks_are_read_but_not_retained(self):
        for options in ({'max_source_bytes': 1}, {'max_cues': 0}, {'max_entries': 0}):
            with self.subTest(options=options):
                cache = self.factory(**options)
                parser = Mock(side_effect=parse_srt)
                self.assertEqual(cache.read(self.path, parser), cache.read(self.path, parser))
                self.assertEqual(parser.call_count, 2)

    def test_concurrent_reads_share_one_parse(self):
        entered = threading.Event()
        release = threading.Event()
        def slow(text):
            entered.set()
            if not release.wait(3):
                raise AssertionError('parser not released')
            return parse_srt(text)
        parser = Mock(side_effect=slow)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.cache.read, self.path, parser)
            try:
                self.assertTrue(entered.wait(2))
                pending = [pool.submit(self.cache.read, self.path, parser) for _ in range(3)]
            finally:
                release.set()
            for future in [first, *pending]:
                self.assertEqual(future.result(timeout=2), [Cue(0, 1000, 'aaa')])
        self.assertEqual(parser.call_count, 1)

    def test_invalid_limits_rejected(self):
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                self.factory(max_entries=value)


if __name__ == '__main__':
    unittest.main()
