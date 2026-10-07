import unittest
from subtitle_pipeline import subtitles as s


class SubtitleTests(unittest.TestCase):
    def test_unicode_multiline_roundtrip(self):
        cues = [s.Cue(1001, 2420, '你好\nこんにちは'), s.Cue(3600001, 3600500, 'Yes')]
        self.assertEqual(s.parse_srt(s.render_srt(cues)), cues)

    def test_parse_bom_crlf_and_dot_milliseconds(self):
        self.assertEqual(s.parse_srt('\ufeff1\r\n00:00:01.001 --> 00:00:02,050\r\nHello\r\n'), [s.Cue(1001, 2050, 'Hello')])

    def test_malformed_nonempty_srt_rejected(self):
        with self.assertRaises(ValueError):
            s.parse_srt('not a subtitle')

    def test_plan_context_and_last_short_chunk(self):
        chunks = s.plan_chunks(610000)
        self.assertEqual([(c.core_start_ms,c.core_end_ms,c.audio_start_ms,c.audio_end_ms) for c in chunks], [(0,300000,0,302000),(300000,600000,298000,602000),(600000,610000,598000,610000)])

    def test_silence_cut_preserves_contiguous_coverage(self):
        chunks=s.plan_chunks(610000,silences=[(297000,299000)])
        self.assertEqual(chunks[0].core_end_ms,298000)
        self.assertEqual(chunks[0].core_end_ms,chunks[1].core_start_ms)
        self.assertEqual(chunks[-1].core_end_ms,610000)

    def test_invalid_chunk_parameters(self):
        for kwargs in [dict(chunk_ms=0),dict(overlap_ms=-1),dict(chunk_ms=1000,overlap_ms=1000)]:
            with self.assertRaises(ValueError): s.plan_chunks(5000,**kwargs)

    def test_global_offset_not_last_subtitle_end(self):
        chunks=s.plan_chunks(610000)
        merged=s.merge_chunk_cues([(chunks[0],[s.Cue(1000,2000,'first')]),(chunks[1],[s.Cue(4000,5000,'second')])])
        self.assertEqual([(c.start_ms,c.end_ms) for c in merged],[(1000,2000),(302000,303000)])

    def test_context_duplicates_removed_real_repeats_kept(self):
        chunks=s.plan_chunks(610000)
        merged=s.merge_chunk_cues([(chunks[0],[s.Cue(299000,301000,'same')]),(chunks[1],[s.Cue(1000,3000,'same'),s.Cue(5000,6000,'same')])])
        self.assertEqual(len(merged),2)
        self.assertEqual(merged[-1].start_ms,303000)

    def test_distinct_overlap_dialogue_not_destroyed(self):
        chunks=s.plan_chunks(610000)
        merged=s.merge_chunk_cues([(chunks[0],[s.Cue(298000,300000,'A')]),(chunks[1],[s.Cue(2000,4000,'B')])])
        self.assertEqual([c.text for c in merged],['A','B'])

    def test_checks_preserve_semantics_and_flag_suspicious(self):
        cleaned,issues=s.check_cues([s.Cue(-100,1000,'  原文  '),s.Cue(2000,30000,'长句'),s.Cue(31000,31500,'非常非常长的句子，需要复核识别和时间。')],32000)
        self.assertEqual(cleaned[0],s.Cue(0,1000,'原文'))
        self.assertGreaterEqual(len(issues),2)
        self.assertTrue(all('reason' in issue for issue in issues))


if __name__=='__main__': unittest.main()
