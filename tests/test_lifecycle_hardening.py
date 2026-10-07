"""Offline regressions for scheduler abort, readiness cancellation and Job errors."""
from contextlib import ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as w, integrity as i, runner as r, windows_job as jobs
from subtitle_pipeline.subtitles import Chunk, Cue, render_srt


class OfflineFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='lifecycle-hardening-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        guards = ExitStack()
        self.addCleanup(guards.close)
        for target, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                             (socket, 'create_connection')):
            guards.enter_context(patch.object(target, name,
                side_effect=AssertionError('This regression must stay offline')))


class SchedulerAbortTests(OfflineFixture):
    def exercise(self, *, translation=False):
        source = self.root / 'synthetic-source.bin'
        source.write_bytes(b'isolated synthetic source')
        config = r.PipelineConfig(source, self.root / 'project', chunk_seconds=1,
            overlap_seconds=0, workers=2, translate=translation)
        stop = threading.Event()
        started, cancelled, release_cleanup = (threading.Event() for _ in range(3))
        failure = RuntimeError('scheduler cannot continue')
        observed = []
        real_wait = r.futures.wait
        waits = 0

        def blocking_worker(worker_stop):
            started.set()
            if not worker_stop.wait(4):
                raise AssertionError('scheduler did not signal its active worker')
            cancelled.set()
            if not release_cleanup.wait(4):
                raise AssertionError('worker cleanup barrier was not released')
            raise r.Cancelled('worker stopped')

        def recognize(config, chunk, folder, worker_stop):
            if not translation and chunk.index == 1:
                blocking_worker(worker_stop)
            return [Cue(100, 800, 'saved source')]

        def translate(texts, language, target, cache, *, stop_event):
            blocking_worker(stop_event)

        def wait(owned, *args, **kwargs):
            nonlocal waits
            waits += 1
            if translation and waits == 1:
                return real_wait(owned, timeout=3, return_when=r.futures.ALL_COMPLETED)
            self.assertTrue(started.wait(3))
            if not translation:
                # The other owned ASR result must already be recoverable.
                self.assertTrue(real_wait(owned, timeout=3,
                    return_when=r.futures.FIRST_COMPLETED)[0])
            raise failure

        def run():
            try:
                r.run_pipeline(config, stop)
            except BaseException as error:
                observed.append(error)

        with patch.object(r, 'probe_media', return_value=1000 if translation else 2000), \
                patch.object(r, 'detect_silences', return_value=[]), \
                patch.object(r, 'recognize_chunk', side_effect=recognize), \
                patch.object(r, 'translate_texts', side_effect=translate), \
                patch.object(r.futures, 'wait', side_effect=wait):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(cancelled.wait(1.5), 'scheduler fault must stop active workers')
                self.assertTrue(thread.is_alive(), 'must wait for worker cleanup before returning')
                self.assertFalse(stop.is_set(), 'an internal fault is not a user cancellation')
            finally:
                release_cleanup.set()
                thread.join(3)
                if thread.is_alive():
                    stop.set()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [failure])
        self.assertFalse(stop.is_set())
        state = json.loads((config.project / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'asr_incomplete')
        self.assertEqual(state['parts']['0']['asr'], 'done')
        self.assertEqual(state['scheduler_error']['message'], str(failure))
        if translation:
            self.assertNotEqual(state['parts']['0'].get('translation'), 'done')
        else:
            self.assertEqual(state['scheduler_error']['recovered_asr'], 1)
            self.assertNotEqual(state['parts']['1'].get('asr'), 'done')
        self.assertFalse((config.project / 'run.lock').exists())
        with r.ProjectLock(config.project):
            pass

    def test_scheduler_fault_stops_asr_and_recovers_finished_peer_before_returning(self):
        self.exercise()

    def test_scheduler_fault_stops_translation_before_releasing_project(self):
        self.exercise(translation=True)


class ReadinessCancellationTests(OfflineFixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / 'input.wav'
        self.source.write_bytes(b'x' * (2 * 1024 * 1024))
        self.project = self.root / '整片'
        folder = self.project / '片段' / '0001'
        folder.mkdir(parents=True)
        source_hash = i.sha256(self.source)
        identity = {'engine':'qwen_asr', 'asr_model':'qwen-audio-3.1-asr-flash',
            'api':'dashscope-v1', 'translation_model':'deepseek-flash',
            'language':'ja', 'target':'zh-CN',
            'source':{'path':str(self.source), 'sha256':source_hash}}
        for name in ('source.local.srt', 'target.local.srt'):
            (folder / name).write_text(render_srt([Cue(100, 800, 'offline cue')]), encoding='utf-8')
        (folder / 'asr-response.json').write_text('{"offline":true}', encoding='utf-8')
        part = {'asr':'done', 'translation':'done', 'asr_evidence':{
            'provider':'qwen_asr', 'model':identity['asr_model'], 'api':identity['api'],
            'language':'ja', 'source_sha256':source_hash, 'audio_range_ms':[0,1000]},
            'source_hash':i.sha256(folder/'source.local.srt'),
            'translation_source_hash':i.sha256(folder/'source.local.srt'),
            'target_hash':i.sha256(folder/'target.local.srt'),
            'raw_response_hash':i.sha256(folder/'asr-response.json')}
        self.state = {'status':'complete', 'identity':identity, 'duration_ms':1000,
            'chunks':[asdict(Chunk(0,0,1000,0,1000))], 'parts':{'0':part}}
        r.atomic_json(self.project / 'state.json', self.state)
        self.manifest = {'source':identity['source'], 'language':'ja', 'target':'zh-CN',
            'duration_ms':1000, 'samples':[], 'status':'prepared'}
        w.write_manifest(self.root, self.manifest)
        self.stop = threading.Event()

    def services(self, *, samples=False, cached=False):
        guards = ExitStack()
        guards.enter_context(patch.object(w, 'verify_source', return_value=self.source))
        settings = SimpleNamespace(asr_input_rate=.8, asr_output_rate=2.7,
            public_config=lambda: {})
        guards.enter_context(patch.object(w, 'load_settings', return_value=settings))
        guards.enter_context(patch.object(r, 'run_pipeline', return_value=self.state))
        guards.enter_context(patch.object(w, 'emit'))
        guards.enter_context(patch.object(w, 'build_review'))
        if samples:
            self.manifest['samples'] = [{'folder':'.', 'start_sec':0, 'end_sec':1,
                'name':'offline sample', 'audio_hash':i.sha256(self.source)}]
            w.write_manifest(self.root, self.manifest)
            guards.enter_context(patch.object(w, 'sample_review_media', return_value=self.source))
            if not cached:
                guards.enter_context(patch.object(w, '_samples_unchanged', return_value=False))
            # Only media extraction and its fixed audio path are substituted.
            guards.enter_context(patch.object(w, 'sha256', return_value=i.sha256(self.source)))
            self.project.rename(self.root / '识别任务')
        return guards

    def assert_cancel_during_readiness(self, operation):
        reads = []
        original = i._evidence_sha256
        def cancel(path, stop):
            reads.append(Path(path))
            self.stop.set()
            return original(path, stop)
        with patch.object(i, '_evidence_sha256', side_effect=cancel):
            with self.assertRaises(r.Cancelled):
                operation()
        self.assertEqual(reads, [self.source])
        saved = w.read_json(self.root / 'campaign.json')
        self.assertNotIn(saved['status'], ('full_ready', 'samples_ready'))

    def test_full_readiness_cancel_does_not_hash_remaining_evidence_or_publish_ready(self):
        with self.services():
            self.assert_cancel_during_readiness(lambda: w.run_full(self.root, self.stop, draft=True))

    def test_sample_readiness_cancel_does_not_hash_remaining_evidence_or_publish_ready(self):
        with self.services(samples=True):
            self.assert_cancel_during_readiness(lambda: w.run_samples(self.root, self.stop))

    def test_cached_sample_readiness_also_stops_at_first_cancelled_evidence_read(self):
        with self.services(samples=True, cached=True):
            self.assert_cancel_during_readiness(lambda: w.run_samples(self.root, self.stop))

    def test_cancel_after_cost_read_prevents_full_ready_publication(self):
        original = w.cost_report
        calls = 0
        def cost(campaign):
            nonlocal calls
            calls += 1
            value = original(campaign)
            if calls == 2:
                self.stop.set()
            return value
        with self.services(), patch.object(w, 'cost_report', side_effect=cost):
            with self.assertRaises(r.Cancelled):
                w.run_full(self.root, self.stop, draft=True)
        self.assertNotEqual(w.read_json(self.root / 'campaign.json')['status'], 'full_ready')


@unittest.skipUnless(os.name == 'nt', 'Windows Job lifecycle regression')
class JobCleanupContextTests(OfflineFixture):
    def test_handled_encoder_failure_does_not_hide_fallback_child_cleanup_error(self):
        prior = RuntimeError('hardware encoder failed; use CPU fallback')
        failure = OSError('owned child exit could not be confirmed')
        reap = jobs._terminate_and_reap
        def fail_after_real_reap(child):
            reap(child)
            raise failure
        child = subprocess.Popen([sys.executable, '-B', '-c', 'pass'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            try:
                raise prior
            except RuntimeError:
                with patch.object(jobs, '_terminate_and_reap', side_effect=fail_after_real_reap):
                    with self.assertRaises(OSError) as caught:
                        with jobs.owned_process(child):
                            child.wait(timeout=3)
                self.assertIs(caught.exception, failure)
            self.assertFalse(getattr(prior, '__notes__', []))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
