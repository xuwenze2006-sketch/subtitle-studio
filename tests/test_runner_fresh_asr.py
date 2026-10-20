"""Local CLI attempts cannot adopt raw subtitles retained from earlier work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner as r
from subtitle_pipeline.subtitles import Chunk, parse_srt


_PARTIAL = '1\n00:00:00,100 --> 00:00:01,000\npartial from failed attempt\n\n'
_CURRENT = _PARTIAL.replace('partial from failed attempt', 'current successful attempt')
_WRITE = ('from pathlib import Path; import sys; '
          'Path(sys.argv[1]).write_text(sys.argv[2],encoding="utf-8"); '
          'raise SystemExit(int(sys.argv[3]))')


class FreshLocalASRTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'synthetic.wav'
        self.source.write_bytes(b'synthetic input; media decoding is stubbed')
        self.config = r.PipelineConfig(self.source, self.root/'project', language='en',
                                      translate=False, chunk_seconds=10, overlap_seconds=1)
        self.folder = self.config.project / '片段' / '0001'
        self.chunk = Chunk(0, 0, 9000, 0, 9000)
        self.invocations = []
        self.children = []
        self.result_text = _CURRENT
        self.exit_code = 0
        self.program = None
        self.real_run = r.run_process
        self.real_popen = r.subprocess.Popen
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch.object(r, 'probe_media', return_value=9000))
        self.guards.enter_context(patch.object(r, 'detect_silences', return_value=[]))
        self.guards.enter_context(patch.object(r, 'engine_paths', return_value=(
            Path('owned-fake-cli.exe'), Path('fake-small.bin'))))
        self.guards.enter_context(patch.object(r, 'run_process', side_effect=self.run_cli))
        self.guards.enter_context(patch.object(r.subprocess, 'Popen', side_effect=self.popen))
        for name, target in (('connect', socket.socket), ('connect_ex', socket.socket),
                             ('create_connection', socket)):
            self.guards.enter_context(patch.object(target, name, side_effect=AssertionError(
                'No external requests in local output regression')))

    def popen(self, *args, **kwargs):
        child = self.real_popen(*args, **kwargs)
        self.children.append(child)
        return child

    def run_cli(self, args, log, stop, env=None, cwd=None):
        if str(args[0]) == 'ffmpeg':
            Path(args[-1]).write_bytes(b'synthetic decoded PCM')
            return
        output = Path(str(args[args.index('-of')+1]) + '.srt')
        self.invocations.append(output)
        if self.program:
            command = [sys.executable, '-X', 'utf8', '-c', self.program, output,
                       self.root/'child-ready']
        elif self.result_text is None:
            command = [sys.executable, '-X', 'utf8', '-c', 'raise SystemExit(0)']
        else:
            command = [sys.executable, '-X', 'utf8', '-c', _WRITE, output,
                       self.result_text, str(self.exit_code)]
        return self.real_run(command, log, stop, env, cwd)

    def direct(self, stop=None):
        self.folder.mkdir(parents=True, exist_ok=True)
        return r.recognize_chunk(self.config, self.chunk, self.folder,
                                 stop if stop is not None else threading.Event())

    def retained_files(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        content = {'asr.srt': _PARTIAL.encode(),
                   'source.local.srt': b'accepted human source bytes',
                   '原文.srt': b'accepted public subtitle bytes'}
        for name, raw in content.items():
            (self.folder/name).write_bytes(raw)
        return content

    def assert_retained_and_reaped(self, content):
        self.assertEqual({name: (self.folder/name).read_bytes() for name in content}, content)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertFalse(list(self.folder.glob('.asr-*')))

    def test_failed_attempt_then_empty_retry_cannot_promote_retained_partial(self):
        self.result_text, self.exit_code = _PARTIAL, 7
        failed = r.run_pipeline(self.config)
        self.assertEqual((failed['status'], failed['recognized']), ('asr_incomplete', 0))
        self.assertNotIn('asr_evidence', failed['parts']['0'])
        # Historical fixed-path raw files remain legitimate recovery evidence,
        # but they cannot stand in for a newly invoked CLI's missing output.
        retained = self.folder/'asr.srt'
        retained.write_text(_PARTIAL, encoding='utf-8')
        before = retained.read_bytes()
        self.result_text, self.exit_code = None, 0
        retried = r.run_pipeline(self.config)
        self.assertEqual((retried['status'], retried['recognized']), ('asr_incomplete', 0))
        self.assertNotIn('asr_evidence', retried['parts']['0'])
        self.assertNotIn('source_hash', retried['parts']['0'])
        self.assertEqual(retained.read_bytes(), before)
        self.assertEqual(len(self.invocations), 2)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertFalse((self.config.project/'run.lock').exists())

    def test_missing_output_preserves_legacy_raw_and_accepted_files(self):
        retained = self.retained_files()
        self.result_text = None
        with self.assertRaises(FileNotFoundError):
            self.direct()
        self.assert_retained_and_reaped(retained)

    def test_current_output_is_used_and_legacy_raw_is_unchanged(self):
        retained = self.retained_files()
        cues = self.direct()
        self.assertEqual([cue.text for cue in cues], ['current successful attempt'])
        self.assert_retained_and_reaped(retained)
        self.assertTrue((self.folder/'asr.log').is_file())

    def test_malformed_current_output_never_falls_back_to_legacy_raw(self):
        retained = self.retained_files()
        self.result_text = 'malformed current SRT'
        with self.assertRaisesRegex(ValueError, 'Invalid SRT'):
            self.direct()
        self.assert_retained_and_reaped(retained)

    def test_current_empty_output_remains_a_valid_silent_result(self):
        retained = self.retained_files()
        self.result_text = ''
        self.assertEqual(self.direct(), [])
        self.assert_retained_and_reaped(retained)

    def test_nonzero_current_attempt_is_not_accepted_or_retained_as_success(self):
        retained = self.retained_files()
        self.exit_code = 7
        with self.assertRaisesRegex(RuntimeError, '返回 7'):
            self.direct()
        self.assert_retained_and_reaped(retained)

    def test_cancellation_reaps_child_before_removing_its_scratch_output(self):
        retained = self.retained_files()
        self.program = ('from pathlib import Path; import sys,time; '
                        'Path(sys.argv[1]).write_text("partial",encoding="utf-8"); '
                        'Path(sys.argv[2]).write_text("ready"); time.sleep(30)')
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.direct, stop)
            try:
                deadline = time.monotonic()+5
                while not (self.root/'child-ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((self.root/'child-ready').is_file(), 'owned child did not start')
            finally:
                stop.set()
            with self.assertRaises(r.Cancelled):
                future.result(timeout=5)
        self.assert_retained_and_reaped(retained)

    def test_completed_resume_reuses_bound_source_without_a_new_cli_attempt(self):
        completed = r.run_pipeline(self.config)
        self.assertEqual((completed['status'], completed['recognized']), ('complete', 1))
        evidence = completed['parts']['0']['asr_evidence'].copy()
        accepted = (self.folder/'source.local.srt').read_bytes()
        self.assertEqual([cue.text for cue in parse_srt(accepted.decode())],
                         ['current successful attempt'])
        self.result_text = None
        resumed = r.run_pipeline(self.config)
        self.assertEqual((resumed['status'], resumed['recognized']), ('complete', 1))
        self.assertEqual(resumed['parts']['0']['asr_evidence'], evidence)
        self.assertEqual((self.folder/'source.local.srt').read_bytes(), accepted)
        self.assertEqual(len(self.invocations), 1)
        self.assertTrue(all(child.poll() is not None for child in self.children))


if __name__ == '__main__':
    unittest.main()
