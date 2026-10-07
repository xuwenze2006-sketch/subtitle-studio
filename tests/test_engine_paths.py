import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_pipeline import runner


class EnginePathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = self.root / 'SubtitlePipeline/runtimes/whisper.cpp'
        self.legacy = self.root / 'Programs/SubtitleEdit/SpeechToText/Cpp'
        env = patch.dict(runner.os.environ, {'LOCALAPPDATA': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def install(self, base, *, executable=True, model=True):
        if executable:
            base.mkdir(parents=True, exist_ok=True)
            (base / 'whisper-cli.exe').write_bytes(b'test executable')
        if model:
            (base / 'Models').mkdir(parents=True, exist_ok=True)
            (base / 'Models/small.bin').write_bytes(b'test model')
        return base / 'whisper-cli.exe', base / 'Models/small.bin'

    def test_independent_runtime_works_without_subtitle_edit(self):
        expected = self.install(self.current)
        self.assertEqual(runner.engine_paths(), expected)

    def test_independent_runtime_takes_precedence_over_legacy(self):
        expected = self.install(self.current)
        self.install(self.legacy)
        self.assertEqual(runner.engine_paths(), expected)

    def test_legacy_runtime_remains_supported(self):
        expected = self.install(self.legacy)
        self.assertEqual(runner.engine_paths(), expected)

    def test_incomplete_independent_runtime_uses_complete_legacy_pair(self):
        expected = self.install(self.legacy)
        for executable in (True, False):
            with self.subTest(executable_present=executable):
                self.install(self.current, executable=executable, model=not executable)
                self.assertEqual(runner.engine_paths(), expected)
                present = self.current / ('whisper-cli.exe' if executable else 'Models/small.bin')
                present.unlink()

    def test_missing_both_runtimes_reports_unavailable(self):
        with self.assertRaisesRegex(FileNotFoundError, 'Whisper CPP / small'):
            runner.engine_paths()

    def test_does_not_mix_executable_and_model_from_different_installations(self):
        self.install(self.current, model=False)
        self.install(self.legacy, executable=False)
        with self.assertRaises(FileNotFoundError):
            runner.engine_paths()


if __name__ == '__main__':
    unittest.main()
