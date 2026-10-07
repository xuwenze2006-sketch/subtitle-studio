import unittest
from dataclasses import FrozenInstanceError
from subtitle_pipeline import subtitles as s


class SubtitleBoundaryTests(unittest.TestCase):
    def test_empty_srt_and_empty_video(self):
        self.assertEqual(s.parse_srt('\ufeff\r\n '), [])
        self.assertEqual(s.render_srt([]), '')
        self.assertEqual(s.plan_chunks(0), [])

    def test_one_bad_block_is_not_silently_skipped(self):
        with self.assertRaises(ValueError):
            s.parse_srt('1\n00:00:01,000 --> 00:00:02,000\nGood\n\n2\ninvalid\nBad')
        with self.assertRaises(ValueError):
            s.parse_srt('1\n00:61:01,000 --> 00:62:02,000\nBad')

    def test_records_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            s.Cue(0, 100, 'x').start_ms = 20
        with self.assertRaises(FrozenInstanceError):
            s.plan_chunks(5000)[0].index = 9

    def test_negative_duration_rejected_and_distant_silence_ignored(self):
        with self.assertRaises(ValueError):
            s.plan_chunks(-1)
        chunks = s.plan_chunks(610000, silences=[(1000, 2000), (580000, 582000)])
        self.assertEqual(chunks[0].core_end_ms, 300000)

    def test_merge_is_sorted_after_parallel_completion(self):
        chunks = s.plan_chunks(610000)
        result = s.merge_chunk_cues([(chunks[1], [s.Cue(4000, 5000, 'second')]),
                                    (chunks[0], [s.Cue(1000, 2000, 'first')])])
        self.assertEqual([c.text for c in result], ['first', 'second'])

    def test_shifted_duplicate_at_boundary_removed(self):
        chunks = s.plan_chunks(610000)
        result = s.merge_chunk_cues([(chunks[0], [s.Cue(299000, 300500, 'yes')]),
                                    (chunks[1], [s.Cue(1400, 3300, 'yes')])])
        self.assertEqual(len(result), 1)

    def test_same_text_without_overlapping_audio_kept(self):
        chunks = s.plan_chunks(610000)
        result = s.merge_chunk_cues([(chunks[0], [s.Cue(298000, 299000, 'yes')]),
                                    (chunks[1], [s.Cue(3000, 4000, 'yes')])])
        self.assertEqual(len(result), 2)

    def test_context_outside_core_ignored_and_end_clipped(self):
        chunk = s.plan_chunks(5000)[0]
        self.assertEqual(s.merge_chunk_cues([(chunk, [s.Cue(4000, 5100, 'end')])]),
                         [s.Cue(4000, 5000, 'end')])

    def test_check_drops_invalid_and_exact_duplicates_without_rewriting(self):
        cues, issues = s.check_cues([s.Cue(500, 100, 'bad'),
                                    s.Cue(1000, 2000, ' 同一句。 '),
                                    s.Cue(1000, 2000, '同一句。'),
                                    s.Cue(3000, 5000, 'different')], 4000)
        self.assertEqual(cues, [s.Cue(1000, 2000, '同一句。'),
                               s.Cue(3000, 4000, 'different')])
        self.assertGreaterEqual(len(issues), 3)


if __name__ == '__main__':
    unittest.main()
