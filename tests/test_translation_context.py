import copy
import unittest

from subtitle_pipeline.subtitles import Chunk, Cue
from subtitle_pipeline import translation_context as context


class TranslationContextTests(unittest.TestCase):
    def setUp(self):
        self.chunk = Chunk(1, 20000, 40000, 18000, 42000)
        self.previous = Chunk(0, 0, 20000, 0, 22000)
        self.source = 'a' * 64
        self.previous_hash = 'b' * 64

    def build(self, previous_cues, current=None):
        return context.build_snapshot(self.chunk, self.source, current or [Cue(2000, 3000, 'current')],
            self.previous, self.previous_hash, previous_cues)

    def test_timed_context_uses_two_nearest_nonoverlapping_cues_and_no_distant_scene(self):
        snapshot = self.build([Cue(100, 900, 'too far'), Cue(10000, 11000, 'older'),
            Cue(17000, 18000, 'prior one'), Cue(18500, 19500, 'prior two'),
            Cue(19500, 20500, 'overlapping current')])
        self.assertEqual(context.source_texts(snapshot), ['prior one', 'prior two'])
        self.assertEqual(snapshot['before'][0]['start_ms'], 17000)
        self.assertEqual(snapshot['before'][0]['index'], 2)
        self.assertEqual(snapshot['previous_source_hash'], self.previous_hash)

    def test_empty_and_long_context_are_saved_without_truncating_subtitles(self):
        snapshot = context.build_snapshot(self.chunk, self.source, [], None, None, [])
        self.assertEqual(context.source_texts(snapshot), [])
        self.assertIsNone(snapshot['first_start_ms'])
        snapshot = self.build([Cue(17000, 18000, 'older'), Cue(18500, 19500, 'x' * 1300)])
        self.assertEqual(context.source_texts(snapshot), [])

    def test_history_keeps_context_when_source_is_edited_and_restored(self):
        part = {}
        original = self.build([Cue(17000, 18000, 'original neighbor')])
        context.remember_snapshot(part, original)
        revised = context.build_snapshot(self.chunk, 'c' * 64, [Cue(2000, 3000, 'edited')],
            self.previous, 'd' * 64, [Cue(17000, 18000, 'new neighbor')])
        context.remember_snapshot(part, revised)
        self.assertEqual(context.existing_snapshot(part, self.source), original)
        self.assertEqual(len(part['translation_contexts']), 2)
        detached = context.existing_snapshot(part, self.source)
        detached['before'].clear()
        self.assertEqual(context.source_texts(context.existing_snapshot(part, self.source)), ['original neighbor'])

    def test_missing_corrupt_or_unbound_history_is_rejected(self):
        part = {}
        context.remember_snapshot(part, self.build([Cue(17000, 18000, 'previous')]))
        for mutation in ('missing', 'changed', 'binding'):
            damaged = copy.deepcopy(part)
            if mutation == 'missing': damaged.pop('translation_contexts')
            elif mutation == 'changed': damaged['translation_contexts'][self.source]['before'][0]['text'] = 'changed'
            else: damaged['translation_context_bindings'][self.source] = '0' * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                context.existing_snapshot(damaged, self.source)

    def test_nonadjacent_previous_chunk_and_invalid_context_structure_rejected(self):
        with self.assertRaises(ValueError):
            context.build_snapshot(self.chunk, self.source, [Cue(2000, 3000, 'current')],
                Chunk(3, 60000, 80000, 58000, 82000), self.previous_hash, [Cue(100, 900, 'wrong')])
        snapshot = self.build([Cue(17000, 18000, 'previous')])
        snapshot['before'][0]['end_ms'] = 21000
        from subtitle_pipeline.integrity import fingerprint
        snapshot['sha256'] = fingerprint({key: value for key, value in snapshot.items() if key != 'sha256'})
        with self.assertRaises(ValueError): context.source_texts(snapshot)
