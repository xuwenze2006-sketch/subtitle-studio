import unittest
from pathlib import Path
from subtitle_pipeline.gui import default_project, project_config_from_state


class GuiHelperTests(unittest.TestCase):
    def test_default_project_keeps_unicode_and_distinguishes_same_names(self):
        first = default_project(Path('one/日语视频.mp4'))
        second = default_project(Path('two/日语视频.mp4'))
        self.assertIn('日语视频', first.name)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent.name, '字幕任务')

    def test_saved_config_paths_and_resume_options_restored(self):
        values = project_config_from_state({'config': {
            'source': '日语视频.mp4', 'project': '任务目录', 'workers': 2,
            'chunk_seconds': 180, 'translate': False,
            'seed_srt': '已完成.srt', 'seed_complete_until_ms': 120000,
            'unknown_future_field': 'ignored',
        }})
        self.assertIsInstance(values['source'], Path)
        self.assertIsInstance(values['seed_srt'], Path)
        self.assertEqual(values['workers'], 2)
        self.assertFalse(values['translate'])
        self.assertEqual(values['seed_complete_until_ms'], 120000)
        self.assertNotIn('unknown_future_field', values)

    def test_malformed_or_unsafe_form_values_are_rejected(self):
        for state in [{}, {'config': []}, {'config': {'source': 'x', 'project': 'y', 'workers': 3}},
                      {'config': {'source': 'x', 'project': 'y', 'chunk_seconds': 0}},
                      {'config': {'source': 'x', 'project': 'y', 'translate': 'false'}},
                      {'config': {'source': 'x', 'project': 'y', 'seed_complete_until_ms': -1}}]:
            with self.subTest(state=state), self.assertRaises(ValueError):
                project_config_from_state(state)


if __name__ == '__main__':
    unittest.main()
