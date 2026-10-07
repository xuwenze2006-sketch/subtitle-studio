import json
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_pipeline import runner as r


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'原 始.wav'
        self.source.write_bytes(b'test audio')

    def config(self, **kwargs):
        return r.PipelineConfig(self.source,self.root/'job',chunk_seconds=10,overlap_seconds=1,**kwargs)

    def services(self, recognize=None, translate=None):
        from subtitle_pipeline.subtitles import Cue
        def default_asr(config,chunk,folder,stop):
            return [Cue(chunk.core_start_ms-chunk.audio_start_ms+100,chunk.core_start_ms-chunk.audio_start_ms+900,f'part {chunk.index}')]
        return patch.multiple(r,probe_media=lambda p,**kw:25000,detect_silences=lambda *a:[],recognize_chunk=recognize or default_asr,translate_texts=translate or (lambda texts,*a,**k:['译 '+t for t in texts]))

    def test_resume_skips_completed_and_offsets_correct(self):
        from subtitle_pipeline.subtitles import parse_srt
        with self.services(): state=r.run_pipeline(self.config())
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['translated'],3)
        cues=parse_srt((self.root/'job'/'原文.srt').read_text(encoding='utf-8-sig'))
        self.assertEqual([c.start_ms for c in cues],[100,10100,20100])
        with self.services(recognize=lambda *a: self.fail('ASR repeated'),translate=lambda *a,**k:self.fail('translation repeated')):
            r.run_pipeline(self.config())

    def crash_during_output(self, config, output_name, before=False):
        script='''
import os, socket, sys
from pathlib import Path
from unittest.mock import patch
from subtitle_pipeline import runner as r
from subtitle_pipeline.subtitles import Cue
source, project = Path(sys.argv[1]), Path(sys.argv[2])
output_name, before = sys.argv[3], sys.argv[4] == 'before'
real_write = r.atomic_text
def write(path, text):
    target = path.parent == project and path.name == output_name
    if target and before:
        os._exit(17)
    real_write(path, text)
    if target:
        os._exit(17)
def blocked(*args, **kwargs):
    raise AssertionError('Outbound network disabled')
with patch.object(socket.socket, 'connect', blocked), patch.object(socket.socket, 'connect_ex', blocked), patch.object(socket, 'create_connection', blocked), \
     patch.object(r, 'probe_media', return_value=9000), patch.object(r, 'detect_silences', return_value=[]), \
     patch.object(r, 'recognize_chunk', return_value=[Cue(100,900,'cached source')]), \
     patch.object(r, 'translate_texts', return_value=['cached translation']), patch.object(r, 'atomic_text', side_effect=write):
    r.run_pipeline(r.PipelineConfig(source, project, chunk_seconds=10, overlap_seconds=1))
'''
        child=subprocess.run([sys.executable,'-c',script,str(config.source),str(config.project),
            output_name,'before' if before else 'after'],capture_output=True,timeout=10,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(child.returncode,17,child.stderr.decode(errors='replace'))

    def test_crash_after_output_replace_resumes_checkpoint_without_repeating_work(self):
        for name in ('原文.srt','中文草稿.srt'):
            with self.subTest(output=name):
                config=self.config()
                config.project=self.root/name
                self.crash_during_output(config,name)
                translate=(lambda *a,**k:self.fail('completed translation repeated')) if name=='中文草稿.srt' else None
                with self.services(recognize=lambda *a:self.fail('completed ASR repeated'),translate=translate):
                    state=r.run_pipeline(config)
                self.assertEqual(state['status'],'complete')
                self.assertEqual((state['recognized'],state['translated']),(1,1))
                self.assertFalse(state.get('manual_outputs'))
                self.assertNotIn('pending_outputs',state)
                self.assertIn('cached source',(config.project/'原文.srt').read_text(encoding='utf-8'))

    def test_crash_before_output_replace_regenerates_missing_output_from_checkpoint(self):
        config=self.config()
        self.crash_during_output(config,'原文.srt',before=True)
        self.assertFalse((config.project/'原文.srt').exists())
        with self.services(recognize=lambda *a:self.fail('completed ASR repeated')):
            state=r.run_pipeline(config)
        self.assertEqual(state['status'],'complete')
        self.assertIn('cached source',(config.project/'原文.srt').read_text(encoding='utf-8'))

    def test_resume_preserves_genuine_edit_after_interrupted_first_publication(self):
        config=self.config()
        self.crash_during_output(config,'原文.srt')
        root=config.project/'原文.srt'
        changed=root.read_text(encoding='utf-8').replace('cached source','human revised source')
        root.write_text(changed,encoding='utf-8')
        with self.services(recognize=lambda *a:self.fail('ASR started during edit conflict'),
                           translate=lambda *a,**k:self.fail('translation started during edit conflict')):
            state=r.run_pipeline(config)
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertEqual(state['review_status'],'needs_review')
        self.assertEqual(root.read_text(encoding='utf-8'),changed)

    def test_completed_resume_does_not_replace_unchanged_outputs(self):
        config=self.config()
        with self.services():
            r.run_pipeline(config)
        names=('原文.srt','中文草稿.srt','双语草稿.srt','需复核.json','需复核.txt')
        before={name:(config.project/name).stat().st_mtime_ns for name in names}
        with self.services(recognize=lambda *a:self.fail('ASR repeated'),
                           translate=lambda *a,**k:self.fail('translation repeated')):
            state=r.run_pipeline(config)
        self.assertEqual(state['status'],'complete')
        self.assertEqual({name:(config.project/name).stat().st_mtime_ns for name in names},before)

    def test_invalid_output_journal_blocks_resume_without_replacing_files(self):
        config=self.config()
        self.crash_during_output(config,'原文.srt')
        path=config.project/'state.json'
        saved=json.loads(path.read_text(encoding='utf-8'))
        output=(config.project/'原文.srt').read_bytes()
        valid=saved['pending_outputs']
        journals=(None,[],{**valid,'version':True},{**valid,'identity':'0'*64},
            {**valid,'checkpoint':'0'*64},{**valid,'outputs':{}},
            {**valid,'outputs':{'../outside.srt':'0'*64}},
            {**valid,'outputs':{'原文.srt':'invalid hash'}},
            {**valid,'outputs':{name:'0'*64 for name in
                ('原文.srt','中文草稿.srt','双语草稿.srt','需复核.json','需复核.txt',str(Path('自动更新')/'原文.srt'))}})
        for journal in journals:
            with self.subTest(journal=journal):
                path.write_text(json.dumps({**saved,'pending_outputs':journal},ensure_ascii=False),encoding='utf-8')
                with self.services(recognize=lambda *a:self.fail('ASR started with invalid journal'),
                                   translate=lambda *a,**k:self.fail('translation started with invalid journal')):
                    state=r.run_pipeline(config)
                self.assertEqual(state['status'],'asr_incomplete')
                self.assertEqual(state['review_status'],'needs_review')
                self.assertEqual((config.project/'原文.srt').read_bytes(),output)

    def test_null_output_journal_is_not_treated_as_completed_publication(self):
        config=self.config()
        with self.services():
            r.run_pipeline(config)
        path=config.project/'state.json'
        state=json.loads(path.read_text(encoding='utf-8'))
        state['pending_outputs']=None
        path.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
        with self.services(recognize=lambda *a:self.fail('ASR repeated'),
                           translate=lambda *a,**k:self.fail('translation repeated')):
            resumed=r.run_pipeline(config)
        self.assertEqual(resumed['status'],'asr_incomplete')
        self.assertEqual(resumed['review_status'],'needs_review')

    def test_failed_journal_checkpoint_does_not_poison_later_part_checkpoints(self):
        config=self.config(workers=2)
        original=r.atomic_json
        failed=False
        def write(path,value):
            nonlocal failed
            if path.name=='state.json' and value.get('pending_outputs') and not failed:
                failed=True
                raise PermissionError('temporary checkpoint sharing conflict')
            return original(path,value)
        with self.services(),patch.object(r,'atomic_json',side_effect=write):
            r.run_pipeline(config)
        self.assertTrue(failed)
        with self.services(recognize=lambda *a:self.fail('completed ASR repeated'),
                           translate=lambda *a,**k:self.fail('completed translation repeated')):
            state=r.run_pipeline(config)
        self.assertEqual(state['status'],'complete')
        self.assertNotIn('pending_outputs',state)
        self.assertFalse(state.get('manual_outputs'))

    def test_translation_failure_preserves_asr_and_resumes(self):
        def fail(*a,**k): raise RuntimeError('offline')
        with self.services(translate=fail): state=r.run_pipeline(self.config())
        self.assertEqual(state['status'],'translation_incomplete')
        self.assertEqual(state['recognized'],3)
        self.assertEqual(state['translated'],0)
        self.assertFalse((self.root/'job'/'中文草稿.srt').exists())
        with self.services(recognize=lambda *a:self.fail('lost cached ASR')):
            state=r.run_pipeline(self.config())
        self.assertEqual(state['status'],'complete')

    def test_changed_source_or_config_rejected(self):
        with self.services(): r.run_pipeline(self.config())
        self.source.write_bytes(b'changed audio')
        with self.services(),self.assertRaises(ValueError): r.run_pipeline(self.config())

    def test_changed_language_rejected(self):
        with self.services(): r.run_pipeline(self.config())
        with self.services(),self.assertRaises(ValueError): r.run_pipeline(self.config(language='en'))

    def test_translation_pipeline_starts_before_all_asr_done(self):
        from subtitle_pipeline.subtitles import Cue
        translated=threading.Event()
        def asr(config,chunk,folder,stop):
            if chunk.index==1: self.assertTrue(translated.wait(4),'translation did not overlap ASR')
            return [Cue(1100,1500,f'part{chunk.index}')]
        def tr(texts,*a,**k): translated.set(); return ['中'+t for t in texts]
        with self.services(asr,tr): state=r.run_pipeline(self.config())
        self.assertEqual(state['status'],'complete')

    def test_scheduler_bounds_pending_work_for_new_and_cached_chunks(self):
        from subtitle_pipeline.subtitles import Cue,parse_srt
        for cached in (False,True):
            with self.subTest(cached=cached):
                config=self.config(workers=2)
                config.project=self.root/('cached' if cached else 'new')
                if cached:
                    config.translate=False
                    with self.services(),patch.object(r,'probe_media',return_value=240000):
                        r.run_pipeline(config)
                    config.translate=True
                release=threading.Event()
                executors=[]
                initial_submissions=[]
                real_wait=r.futures.wait
                class ObservedExecutor(ThreadPoolExecutor):
                    def __init__(self,*args,**kwargs):
                        super().__init__(*args,**kwargs)
                        self.submitted=[]
                        executors.append(self)
                    def submit(self,fn,*args,**kwargs):
                        future=super().submit(fn,*args,**kwargs)
                        self.submitted.append(future)
                        return future
                def wait_for_jobs(*args,**kwargs):
                    if not release.is_set():
                        initial_submissions.extend(len(pool.submitted) for pool in executors)
                        release.set()
                    return real_wait(*args,**kwargs)
                def recognize(config,chunk,folder,stop):
                    if not release.wait(4):
                        raise AssertionError('scheduler never waited for active jobs')
                    return [Cue(chunk.core_start_ms-chunk.audio_start_ms+100,
                                chunk.core_start_ms-chunk.audio_start_ms+900,f'part {chunk.index}')]
                def translate(texts,*args,**kwargs):
                    if not release.wait(4):
                        raise AssertionError('scheduler never waited for active jobs')
                    return ['译 '+text for text in texts]
                with self.services(recognize,translate),patch.object(r,'probe_media',return_value=240000), \
                     patch.object(r.futures,'ThreadPoolExecutor',ObservedExecutor), \
                     patch.object(r.futures,'wait',side_effect=wait_for_jobs):
                    state=r.run_pipeline(config)
                self.assertEqual(state['status'],'complete')
                self.assertEqual((state['recognized'],state['translated']),(24,24))
                output=parse_srt((config.project/'中文草稿.srt').read_text(encoding='utf-8'))
                self.assertEqual([cue.text for cue in output],['译 part '+str(i) for i in range(24)])
                self.assertEqual(initial_submissions,[0,1] if cached else [2,0],
                                 'only worker slots should be submitted before waiting for results')

    def test_cancelled_job_does_not_mark_unfinished_complete(self):
        stop=threading.Event()
        def asr(*a): stop.set(); raise r.Cancelled('stopped')
        with self.services(asr): state=r.run_pipeline(self.config(),stop_event=stop)
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual(state['recognized'],0)
        self.assertFalse((self.root/'job'/'run.lock').exists())

    def test_cancellation_keeps_inflight_results_without_submitting_remaining_chunks(self):
        from subtitle_pipeline.subtitles import Cue
        stop=threading.Event()
        started=threading.Barrier(3)
        submitted=[]
        real_wait=r.futures.wait
        class ObservedExecutor(ThreadPoolExecutor):
            def submit(self,fn,chunk):
                submitted.append(chunk.index)
                return super().submit(fn,chunk)
        def recognize(config,chunk,folder,event):
            started.wait(timeout=4)
            if not event.wait(4):
                raise AssertionError('active work was not cancelled')
            return [Cue(1200,1500,f'part {chunk.index}')]
        def wait_for_jobs(*args,**kwargs):
            if not stop.is_set():
                started.wait(timeout=4)
                stop.set()
            return real_wait(*args,**kwargs)
        with self.services(recognize,lambda *a,**k:self.fail('translation started after cancellation')), \
             patch.object(r,'probe_media',return_value=240000), \
             patch.object(r.futures,'ThreadPoolExecutor',ObservedExecutor), \
             patch.object(r.futures,'wait',side_effect=wait_for_jobs):
            state=r.run_pipeline(self.config(workers=2),stop_event=stop)
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual((state['recognized'],state['translated']),(2,0))
        self.assertEqual(submitted,[0,1])
        saved=list((self.root/'job'/'片段').glob('*/source.local.srt'))
        self.assertEqual(sorted(path.parent.name for path in saved),['0001','0002'])

    def test_recognition_failure_does_not_starve_remaining_chunks(self):
        from subtitle_pipeline.subtitles import Cue,parse_srt
        def recognize(config,chunk,folder,event):
            if chunk.index==0:
                raise RuntimeError('first chunk failed')
            return [Cue(1200,1500,f'part {chunk.index}')]
        with self.services(recognize):
            state=r.run_pipeline(self.config())
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertEqual((state['recognized'],state['translated']),(2,2))
        self.assertEqual(state['parts']['0']['error'],'first chunk failed')
        output=parse_srt((self.root/'job'/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertEqual([cue.text for cue in output],['译 part 1','译 part 2'])

    def test_duplicate_project_lock_blocks_concurrent_run(self):
        project=self.root/'job'
        project.mkdir()
        import os
        (project/'run.lock').write_text(json.dumps({'pid':os.getpid()}))
        with self.services(),self.assertRaises(RuntimeError): r.run_pipeline(self.config())

    def test_stale_project_lock_reclaimers_never_overlap(self):
        project=self.root/'job'
        project.mkdir()
        (project/'run.lock').write_text('{"pid":999999999}',encoding='utf-8')
        first_checked,second_observed,release_first,first_entered,finish_first=(threading.Event() for _ in range(5))
        def alive(pid):
            if threading.current_thread().name.endswith('_0'):
                first_checked.set()
                if not release_first.wait(4):
                    raise AssertionError('second contender did not run')
            else:
                second_observed.set()
                if not first_entered.wait(4):
                    raise AssertionError('first owner did not acquire its lock')
            return False
        def first_owner():
            with r.ProjectLock(project):
                first_entered.set()
                if not finish_first.wait(4):
                    raise AssertionError('contender did not finish')
        def second_owner():
            try:
                with r.ProjectLock(project):
                    return 'overlap'
            except RuntimeError:
                return 'blocked'
            finally:
                second_observed.set()
        with patch.object(r,'pid_alive',side_effect=alive), ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(first_owner)
            try:
                self.assertTrue(first_checked.wait(4))
                second=pool.submit(second_owner)
                self.assertTrue(second_observed.wait(4))
                release_first.set()
                outcome=second.result(timeout=4)
            finally:
                release_first.set()
                finish_first.set()
            first.result(timeout=4)
        self.assertEqual(outcome,'blocked')

    def test_project_lock_blocks_other_process_and_recovers_after_abnormal_exit(self):
        project=self.root/'job'
        project.mkdir()
        script='''
import os, socket, sys, time
from pathlib import Path
from unittest.mock import patch
from subtitle_pipeline.runner import ProjectLock
def blocked(*args, **kwargs):
    raise AssertionError('Outbound network disabled')
project=Path(sys.argv[1])
with patch.object(socket.socket,'connect',blocked), patch.object(socket.socket,'connect_ex',blocked), patch.object(socket,'create_connection',blocked):
    with ProjectLock(project):
        (project/'ready').write_text('ready')
        while not (project/'crash').exists():
            time.sleep(.01)
        os._exit(0)
'''
        child=subprocess.Popen([sys.executable,'-c',script,str(project)],stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+5
            while not (project/'ready').exists() and child.poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue((project/'ready').is_file(),'child lock owner did not start')
            # Even an outdated liveness result must not remove another owner.
            with patch.object(r,'pid_alive',return_value=False), self.assertRaises(RuntimeError):
                with r.ProjectLock(project):
                    pass
        finally:
            (project/'crash').write_text('crash')
            try:
                output,error=child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                output,error=child.communicate(timeout=5)
        self.assertEqual(child.returncode,0,error.decode(errors='replace'))
        with r.ProjectLock(project):
            self.assertTrue((project/'run.lock').is_file())
        self.assertFalse((project/'run.lock').exists())
        self.assertTrue((project/'run.guard.lock').is_file())

    def test_atomic_text_concurrent_writers_keep_their_own_complete_content(self):
        destination=self.root/'state.json'
        first_ready=threading.Event()
        finish_first=threading.Event()
        replace=r.os.replace
        def delayed_replace(source,target):
            if Path(source).read_text(encoding='utf-8')=='first':
                first_ready.set()
                if not finish_first.wait(4):
                    raise AssertionError('second writer never completed')
            replace(source,target)
        with patch.object(r.os,'replace',side_effect=delayed_replace), ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(r.atomic_text,destination,'first')
            try:
                self.assertTrue(first_ready.wait(4))
                second=pool.submit(r.atomic_text,destination,'second')
                second.result(timeout=4)
                self.assertEqual(destination.read_text(encoding='utf-8'),'second')
            finally:
                finish_first.set()
            first.result(timeout=4)
        self.assertEqual(destination.read_text(encoding='utf-8'),'first')
        self.assertEqual(list(self.root.glob('state.json*')),[destination])

    def test_atomic_text_failure_preserves_destination_and_cleans_temporary_file(self):
        destination=self.root/'state.json'
        for operation in ('fsync','replace'):
            with self.subTest(operation=operation):
                destination.write_text('original',encoding='utf-8')
                with patch.object(r.os,operation,side_effect=OSError('disk write failed')):
                    with self.assertRaisesRegex(OSError,'disk write failed'):
                        r.atomic_text(destination,'replacement')
                self.assertEqual(destination.read_text(encoding='utf-8'),'original')
                self.assertEqual(list(self.root.glob('state.json*')),[destination])

    @unittest.skipUnless(r.os.name=='nt','Windows sharing semantics required')
    def test_atomic_text_recovers_after_real_reader_releases_delete_sharing_conflict(self):
        import ctypes
        from ctypes import wintypes
        destination=self.root/'state.json'
        destination.write_text('original',encoding='utf-8')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                                    wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        kernel.CloseHandle.restype=wintypes.BOOL
        # Share READ and WRITE, but deliberately omit FILE_SHARE_DELETE.
        handle=kernel.CreateFileW(str(destination),0x80000000,0x1|0x2,None,3,0x80,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        replace=r.os.replace
        temporary_paths=[]
        conflicts=[]
        def replace_with_reader(source,target):
            nonlocal handle
            temporary_paths.append(Path(source))
            try:
                return replace(source,target)
            except PermissionError as error:
                conflicts.append(error.winerror)
                self.assertEqual(destination.read_text(encoding='utf-8'),'original')
                self.assertEqual(Path(source).read_text(encoding='utf-8'),'replacement')
                kernel.CloseHandle(handle)
                handle=None
                raise
        try:
            with patch.object(r.os,'replace',side_effect=replace_with_reader), \
                 patch.object(r.os,'fsync',wraps=r.os.fsync) as sync:
                r.atomic_text(destination,'replacement')
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
        self.assertEqual(len(conflicts),1)
        self.assertIn(conflicts[0],(5,32,33))
        self.assertEqual(len(temporary_paths),2)
        self.assertEqual(temporary_paths[0],temporary_paths[1])
        self.assertEqual(sync.call_count,1)
        self.assertEqual(destination.read_text(encoding='utf-8'),'replacement')
        self.assertEqual(list(self.root.glob('state.json*')),[destination])

    @unittest.skipUnless(r.os.name=='nt','Windows replacement retries required')
    def test_atomic_text_retries_only_replace_using_the_same_synced_temporary_file(self):
        destination=self.root/'state.json'
        replace=r.os.replace
        for code in (5,32,33):
            with self.subTest(winerror=code):
                destination.write_text('original',encoding='utf-8')
                attempts=[]
                pauses=[]
                error=PermissionError('temporary sharing conflict')
                error.winerror=code
                def intermittently_locked(source,target):
                    attempts.append(Path(source))
                    if len(attempts)<3:
                        raise error
                    replace(source,target)
                with patch.object(r.os,'replace',side_effect=intermittently_locked), \
                     patch.object(r.os,'fsync',wraps=r.os.fsync) as sync, \
                     patch.object(r.time,'sleep',side_effect=pauses.append):
                    r.atomic_text(destination,'replacement')
                self.assertEqual(len(attempts),3)
                self.assertEqual(len(set(attempts)),1)
                self.assertEqual(sync.call_count,1)
                self.assertGreater(sum(pauses),0)
                self.assertLessEqual(sum(pauses),1)
                self.assertEqual(destination.read_text(encoding='utf-8'),'replacement')
                self.assertEqual(list(self.root.glob('state.json*')),[destination])

    @unittest.skipUnless(r.os.name=='nt','Windows replacement retries required')
    def test_atomic_text_persistent_sharing_error_remains_visible_after_bounded_retries(self):
        destination=self.root/'state.json'
        for code in (5,32,33):
            with self.subTest(winerror=code):
                destination.write_text('original',encoding='utf-8')
                error=PermissionError('persistent denied replacement')
                error.winerror=code
                pauses=[]
                with patch.object(r.os,'replace',side_effect=error) as replace, \
                     patch.object(r.os,'fsync',wraps=r.os.fsync) as sync, \
                     patch.object(r.time,'sleep',side_effect=pauses.append):
                    with self.assertRaises(PermissionError) as caught:
                        r.atomic_text(destination,'replacement')
                self.assertIs(caught.exception,error)
                self.assertGreater(replace.call_count,1)
                self.assertLessEqual(replace.call_count,8)
                self.assertGreater(sum(pauses),0)
                self.assertLessEqual(sum(pauses),1)
                self.assertEqual(sync.call_count,1)
                self.assertEqual(destination.read_text(encoding='utf-8'),'original')
                self.assertEqual(list(self.root.glob('state.json*')),[destination])

    def test_atomic_text_does_not_retry_other_errors(self):
        destination=self.root/'state.json'
        protected=PermissionError('write protected')
        protected.winerror=19
        for error in (protected,PermissionError('permission denied'),OSError('disk write failed')):
            with self.subTest(error=repr(error)):
                destination.write_text('original',encoding='utf-8')
                with patch.object(r.os,'replace',side_effect=error) as replace, \
                     patch.object(r.time,'sleep',side_effect=AssertionError('unrelated errors must not retry')):
                    with self.assertRaises(type(error)) as caught:
                        r.atomic_text(destination,'replacement')
                self.assertIs(caught.exception,error)
                self.assertEqual(replace.call_count,1)
                self.assertEqual(destination.read_text(encoding='utf-8'),'original')
                self.assertEqual(list(self.root.glob('state.json*')),[destination])

    def test_full_seed_skips_asr(self):
        seed=self.root/'seed.srt'
        seed.write_text('1\n00:00:00,100 --> 00:00:01,000\nHello\n\n2\n00:00:20,100 --> 00:00:21,000\nWorld\n',encoding='utf-8')
        with self.services(recognize=lambda *a:self.fail('seed ignored')):
            state=r.run_pipeline(self.config(seed_srt=seed,seed_complete_until_ms=25000))
        self.assertEqual(state['recognized'],3)

    def test_preparation_failure_can_retry_same_project(self):
        def fail(source,stop,project):
            (project/'silence.log').write_text('decoder stopped')
            raise RuntimeError('decoder stopped')
        with self.services(),patch.object(r,'detect_silences',side_effect=fail):
            with self.assertRaises(RuntimeError): r.run_pipeline(self.config())
        with self.services():
            self.assertEqual(r.run_pipeline(self.config())['status'],'complete')

    def test_cloud_audio_preparation_interruption_can_resume_same_project(self):
        from subtitle_pipeline.subtitles import Cue
        for error_type in (RuntimeError,r.Cancelled):
            with self.subTest(error=error_type.__name__):
                config=self.config(asr_provider='qwen_asr',translate=False)
                config.project=self.root/error_type.__name__
                stop=threading.Event()
                interrupted=False
                def extraction(args,log,event):
                    nonlocal interrupted
                    Path(args[-1]).write_bytes(b'synthetic audio')
                    log.write_text('synthetic extraction',encoding='utf-8')
                    if not interrupted:
                        interrupted=True
                        if error_type is r.Cancelled:
                            event.set()
                        raise error_type('extraction interrupted')
                def recognize(config,chunk,folder,event,*,before_submit=None):
                    # Recognition itself is stubbed here; real admission is
                    # covered with a local ledger and WAV in test_qwen_admission.
                    (folder/'recognition-metadata.json').write_text('{"issues":[]}',encoding='utf-8')
                    (folder/'asr-response.json').write_text('{}',encoding='utf-8')
                    return [Cue(1200,1500,'synthetic source')]
                context=(SimpleNamespace(asr_endpoint='https://invalid.example'),
                         SimpleNamespace(summary=lambda:{}))
                with self.services(recognize), patch.object(r,'cloud_context',return_value=context), \
                     patch.object(r,'run_process',side_effect=extraction):
                    if error_type is r.Cancelled:
                        state=r.run_pipeline(config,stop)
                        self.assertEqual(state['status'],'cancelled')
                    else:
                        with self.assertRaisesRegex(error_type,'extraction interrupted'):
                            r.run_pipeline(config,stop)
                    state_path=config.project/'state.json'
                    self.assertTrue(state_path.is_file(),'prepared chunk plan must survive audio extraction interruption')
                    persisted=json.loads(state_path.read_text(encoding='utf-8'))
                    self.assertEqual(persisted['recognized'],0)
                    if error_type is r.Cancelled:
                        self.assertEqual(persisted['status'],'cancelled')
                    self.assertFalse((config.project/'run.lock').exists())
                    stop.clear()
                    resumed=r.run_pipeline(config,stop)
                self.assertEqual(resumed['status'],'complete')
                self.assertEqual(resumed['recognized'],3)

    def test_translated_timeline_uses_source_deduplication(self):
        from subtitle_pipeline.subtitles import Cue,parse_srt
        def asr(config,chunk,folder,stop):
            if chunk.index==0: return [Cue(9400,10400,'same source')]
            if chunk.index==1: return [Cue(600,1800,'same source')]
            return []
        calls=[]
        def tr(texts,*args,**kwargs):
            calls.append(1)
            return [('译法一' if len(calls)==1 else '译法二') for t in texts]
        with self.services(asr,tr): r.run_pipeline(self.config())
        original=parse_srt((self.root/'job'/'原文.srt').read_text(encoding='utf-8'))
        translated=parse_srt((self.root/'job'/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertEqual([(c.start_ms,c.end_ms) for c in original],[(c.start_ms,c.end_ms) for c in translated])

    def test_manual_corrections_are_backed_up_before_generated_update(self):
        with self.services(): r.run_pipeline(self.config())
        final=self.root/'job'/'中文草稿.srt'
        edited=final.read_text(encoding='utf-8').replace('译 part 0','人工校对文本')
        final.write_text(edited,encoding='utf-8')
        with self.services(): r.run_pipeline(self.config())
        backups=list((self.root/'job'/'用户修改备份').glob('*.srt'))
        self.assertTrue(any(p.read_text(encoding='utf-8')==edited for p in backups))

    def test_missing_cached_translation_recovers(self):
        with self.services(): r.run_pipeline(self.config())
        cached=self.root/'job'/'片段'/'0001'/'target.local.srt'
        original=cached.read_bytes()
        cached.unlink()
        with self.services(recognize=lambda *a:self.fail('ASR repeated')):
            state=r.run_pipeline(self.config())
        self.assertEqual(state['status'],'complete')
        self.assertEqual(cached.read_bytes(),original)

    def test_translation_structure_edits_require_review_without_retranslation(self):
        edits={
            'timing':lambda text:text.replace('00:00:00,900','00:00:00,950').replace('译 part 0','人工校对'),
            'deleted_cue':lambda text:'',
            'invalid_srt':lambda text:'人工校对，尚未补完时间轴',
        }
        cases=[(side,label,edit) for side in ('local','public') for label,edit in edits.items()]
        for side,label,edit in cases:
            with self.subTest(side=side,edit=label):
                config=self.config()
                config.project=self.root/(side+'-'+label)
                with self.services():
                    r.run_pipeline(config)
                folder=config.project/'片段'/'0001'
                local,public=folder/'target.local.srt',folder/'中文草稿.srt'
                edited_path=local if side=='local' else public
                original=edited_path.read_text(encoding='utf-8')
                edited_path.write_text(edit(original),encoding='utf-8')
                before=(local.read_bytes(),public.read_bytes())
                translated=[]
                def translate(texts,*args,**kwargs):
                    translated.extend(texts)
                    return ['retranslated '+text for text in texts]
                with self.services(translate=translate):
                    for _ in range(2):
                        state=r.run_pipeline(config)
                        self.assertEqual((local.read_bytes(),public.read_bytes()),before)
                        self.assertEqual(state['status'],'translation_incomplete')
                        self.assertEqual(state['parts']['0']['translation'],'needs_review')
                    edited_path.write_text(original.replace('译 part 0','人工校对'),encoding='utf-8')
                    resolved=r.run_pipeline(config)
                self.assertEqual(translated,[])
                self.assertEqual(resolved['status'],'complete')
                self.assertIn('人工校对',local.read_text(encoding='utf-8'))

    def test_seed_cue_spanning_cut_keeps_original_times_once(self):
        from subtitle_pipeline.subtitles import Cue,render_srt,parse_srt
        expected=[Cue(5000,15000,'long original'),Cue(18000,24500,'another original')]
        seed=self.root/'full.srt'
        seed.write_text(render_srt(expected),encoding='utf-8')
        with self.services():r.run_pipeline(self.config(seed_srt=seed,seed_complete_until_ms=25000))
        actual=parse_srt((self.root/'job'/'原文.srt').read_text(encoding='utf-8'))
        self.assertEqual(actual,expected)

    def test_real_child_process_cancel_is_reaped(self):
        import sys,time
        stop=threading.Event()
        timer=threading.Timer(.3,stop.set)
        timer.start()
        start=time.monotonic()
        with self.assertRaises(r.Cancelled):
            r.run_process([sys.executable,'-c','import time; time.sleep(30)'],self.root/'cancel.log',stop)
        timer.join()
        self.assertLess(time.monotonic()-start,6)

    def test_two_recognition_workers_overlap(self):
        from subtitle_pipeline.subtitles import Cue
        barrier=threading.Barrier(2)
        def recognize(config,chunk,folder,stop):
            if chunk.index<2: barrier.wait(timeout=3)
            return [Cue(1200,1500,'parallel')]
        with self.services(recognize): state=r.run_pipeline(self.config(workers=2))
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['recognized'],3)


if __name__=='__main__': unittest.main()
