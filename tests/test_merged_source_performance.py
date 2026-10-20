"""Bound merged-edit reconciliation work while retaining its conflict safeguards."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import integrity as i
from subtitle_pipeline.subtitles import Chunk, Cue, parse_srt, render_srt


class MergedSourcePerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='merged-source-performance-')
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    def prepare(self, parts, baseline, edited):
        state = {'identity': {'engine': 'whisper_cpp', 'source': {'sha256': 'a' * 64}},
                 'parts': {}, 'generated_hashes': {'原文.srt': i.text_hash(render_srt(baseline))}}
        for chunk, cues in parts:
            folder = self.project / '片段' / f'{chunk.index + 1:04d}'
            folder.mkdir(parents=True)
            (folder / 'source.local.srt').write_text(render_srt(cues), encoding='utf-8', newline='\n')
            public = [Cue(c.start_ms + chunk.audio_start_ms,
                          c.end_ms + chunk.audio_start_ms, c.text) for c in cues]
            (folder / '原文.srt').write_text(render_srt(public), encoding='utf-8', newline='\n')
            state['parts'][str(chunk.index)] = {
                'asr': 'done', 'translation': 'done',
                'asr_evidence': {'provider': 'whisper_cpp', 'source_sha256': 'a' * 64,
                                 'audio_range_ms': [chunk.audio_start_ms, chunk.audio_end_ms]},
                'source_hash': i.sha256(folder / 'source.local.srt'),
                'public_source_hash': i.sha256(folder / '原文.srt')}
        (self.project / '原文.srt').write_text(render_srt(edited), encoding='utf-8', newline='\n')
        return state

    def reconcile(self, parts, state, backup=lambda path: None, persist=lambda: None):
        return i.reconcile_merged_source(
            self.project, [chunk for chunk, _ in parts], state, backup,
            lambda path, text: path.write_text(text, encoding='utf-8', newline='\n'), persist)

    def test_many_published_edits_do_not_compare_every_pair_of_cues(self):
        count = 800
        cues = [Cue(n * 2000, n * 2000 + 1000, f'line {n}') for n in range(count)]
        edited = [Cue(c.start_ms, c.end_ms, f'corrected {n}' if n % 7 == 0 else c.text)
                  for n, c in enumerate(cues)]
        parts = [(Chunk(0, 0, count * 2000, 0, count * 2000), cues)]
        state = self.prepare(parts, cues, edited)
        calls = 0
        original = Cue.__eq__

        def compared(first, second):
            nonlocal calls
            calls += 1
            return original(first, second)

        backups, checkpoints = {}, []

        def backup(path):
            if path.is_file():
                backups[path] = path.read_bytes()

        with patch.object(Cue, '__eq__', compared):
            self.assertTrue(self.reconcile(parts, state, backup,
                                           lambda: checkpoints.append(copy.deepcopy(state))))
        self.assertLess(calls, count * 10, 'Reconciliation repeated whole-track cue comparisons')
        folder = self.project / '片段' / '0001'
        expected = render_srt(edited).encode('utf-8')
        for name in ('source.local.srt', '原文.srt'):
            self.assertEqual((folder / name).read_bytes(), expected)
            self.assertEqual(backups[folder / name], render_srt(cues).encode('utf-8'))
        self.assertEqual((self.project / '原文.srt').read_bytes(), expected)
        self.assertNotIn('translation', state['parts']['0'])
        self.assertEqual(len(checkpoints), 2)
        self.assertIn('merged_source_edit', checkpoints[0])
        self.assertNotIn('merged_source_edit', checkpoints[1])

    def test_boundary_context_only_checks_nearby_published_cues(self):
        count = 800
        boundary = count * 2000
        initial = [Cue(n * 2000, n * 2000 + 1000, f'line {n}') for n in range(count)]
        edge = Cue(boundary - 900, boundary + 100, 'boundary words')
        baseline = initial + [edge, Cue(boundary + 1000, boundary + 2000, 'next words')]
        edited = initial + [Cue(edge.start_ms, edge.end_ms, 'corrected boundary'), baseline[-1]]
        parts = [(Chunk(0, 0, boundary, 0, boundary + 2000), initial + [edge]),
                 (Chunk(1, boundary, boundary + 5000, boundary - 2000, boundary + 5000),
                  [Cue(1150, 2150, 'boundary words'), Cue(3000, 4000, 'next words')])]
        state = self.prepare(parts, baseline, edited)
        with patch.object(i, '_boundary_duplicate', wraps=i._boundary_duplicate) as compared:
            self.assertTrue(self.reconcile(parts, state))
        self.assertLess(compared.call_count, 20, 'A context cue scanned unrelated distant cues')
        folder = self.project / '片段' / '0002'
        self.assertEqual(parse_srt((folder / 'source.local.srt').read_text(encoding='utf-8')),
                         [Cue(1150, 2150, 'corrected boundary'), Cue(3000, 4000, 'next words')])

    def test_identical_published_cues_with_different_edits_still_conflict(self):
        duplicate = Cue(0, 1000, 'repeated')
        parts = [(Chunk(0, 0, 5000, 0, 5000), [duplicate, duplicate])]
        state = self.prepare(parts, [duplicate, duplicate],
                             [Cue(0, 1000, 'first correction'), Cue(0, 1000, 'second correction')])
        before = {path: path.read_bytes() for path in self.project.rglob('*.srt')}
        with self.assertRaises(i.HumanEditConflict):
            self.reconcile(parts, state)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertNotIn('merged_source_edit', state)


if __name__ == '__main__':
    unittest.main()
