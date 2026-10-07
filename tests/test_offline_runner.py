"""The offline entrypoint must canonicalize aliased temporary fixture paths."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class OfflineRunnerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows short-path aliases')
    def test_short_temp_alias_is_canonical_before_discovery(self):
        with tempfile.TemporaryDirectory(prefix='subtitle-offline-long-temp-') as temporary:
            root = Path(temporary).resolve()
            get_short_path = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
            get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            get_short_path.restype = ctypes.c_uint
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_short_path(str(root), buffer, len(buffer))
            if not length or length >= len(buffer) or Path(buffer.value) == root:
                self.skipTest('The temporary volume does not provide a distinct 8.3 alias')
            alias = buffer.value
            self.assertEqual(Path(alias).resolve(), root)

            tests = root / 'tests'
            tests.mkdir()
            runner = tests / 'run_offline.py'
            shutil.copyfile(Path(__file__).with_name('run_offline.py'), runner)
            (tests / 'test_fixture_paths.py').write_text(textwrap.dedent('''\
                import os
                from pathlib import Path
                import tempfile
                import unittest

                # Discovery imports this module before the test body runs.
                fixture = tempfile.TemporaryDirectory()

                class FixturePathsTests(unittest.TestCase):
                    @classmethod
                    def tearDownClass(cls):
                        fixture.cleanup()

                    def test_fixture_uses_canonical_temp_parent(self):
                        self.assertEqual(
                            Path(fixture.name).parent,
                            Path(os.environ['SUBTITLE_TEST_CANONICAL_TEMP']),
                        )
                '''), encoding='utf-8')
            env = dict(os.environ, TMP=alias, TEMP=alias, TMPDIR=alias,
                       SUBTITLE_TEST_CANONICAL_TEMP=str(root), PYTHONUTF8='1')
            result = subprocess.run(
                [sys.executable, '-B', str(runner)], cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
