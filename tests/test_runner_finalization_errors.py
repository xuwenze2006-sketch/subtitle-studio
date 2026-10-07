"""Actual offline pipeline finalization retains primary errors and recovery data."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from subtitle_pipeline import runner as r
from subtitle_pipeline.cloud_budget import _file_lock
from tests import test_runner_cancelled_finalization as fixtures


class RunnerFinalizationErrorsTests(unittest.TestCase):
    setUp=fixtures.CancelledFinalizationTests.setUp
    recognize=fixtures.CancelledFinalizationTests.recognize
    prepare=fixtures.CancelledFinalizationTests.prepare
    services=fixtures.CancelledFinalizationTests.services
    run_pipeline=fixtures.CancelledFinalizationTests.run_pipeline

    def saved(self):
        return json.loads((self.config.project/'state.json').read_text(encoding='utf-8'))

    def assert_notes(self,error,*texts):
        notes='\n'.join(getattr(error,'__notes__',[]))
        for text in texts:self.assertIn(text,notes)

    @contextmanager
    def failed_output(self,error,*,destination=None,diagnostic_error=None):
        target=destination or self.config.project/'原文.srt'
        original_text=r.atomic_text;original_json=r.atomic_json
        journals=[];diagnostics=[];writes=[]
        def text(path,value):
            if Path(path)==target:
                writes.append(Path(path));raise error
            return original_text(path,value)
        def saved(path,value):
            if Path(path)==self.config.project/'state.json':
                if value.get('pending_outputs'):journals.append(copy.deepcopy(value['pending_outputs']))
                if value.get('finalization_error'):
                    diagnostics.append(copy.deepcopy(value))
                    if diagnostic_error is not None:raise diagnostic_error
            return original_json(path,value)
        with patch.object(r,'atomic_text',side_effect=text),patch.object(r,'atomic_json',side_effect=saved):
            yield journals,diagnostics,writes

    def assert_unlocked(self):
        self.assertFalse((self.config.project/'run.lock').exists())
        with _file_lock(self.config.project/'run.guard.lock',blocking=False) as acquired:
            self.assertTrue(acquired)

    def test_scheduler_and_combine_errors_keep_primary_and_exact_pending_intent(self):
        primary=RuntimeError('owned scheduler original');failure=OSError('owned combine original')
        with self.services(),patch.object(r.futures,'wait',side_effect=primary),self.failed_output(failure) as captured:
            with self.assertRaises(RuntimeError) as caught:self.run_pipeline()
        self.assertIs(caught.exception,primary)
        self.assert_notes(primary,'combine',str(failure))
        state=self.saved();journals,diagnostics,writes=captured
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertEqual(state['finalization_error']['stage'],'combine')
        self.assertEqual(state['pending_outputs'],journals[0])
        self.assertEqual(len(diagnostics),1);self.assertEqual(len(writes),1)
        self.assertNotIn('complete',self.progress);self.assert_unlocked()

    def test_actual_pipeline_scheduler_combine_and_marker_errors_keep_scheduler_primary(self):
        primary=RuntimeError('scheduler body');failure=OSError('combine secondary')
        cleanup=PermissionError('marker secondary');original=Path.unlink
        marker=self.config.project/'run.lock'
        def unlink(path,*args,**kwargs):
            if Path(path)==marker:raise cleanup
            return original(path,*args,**kwargs)
        with self.services(),patch.object(r.futures,'wait',side_effect=primary), \
                self.failed_output(failure),patch.object(Path,'unlink',unlink):
            with self.assertRaises(RuntimeError) as caught:self.run_pipeline()
        self.assertIs(caught.exception,primary)
        self.assert_notes(primary,str(failure),str(cleanup),str(marker))
        self.assertEqual(self.saved()['status'],'asr_incomplete')
        self.assertIn('pending_outputs',self.saved());self.assertTrue(marker.exists())
        with _file_lock(self.config.project/'run.guard.lock',blocking=False) as acquired:self.assertTrue(acquired)
        marker.unlink()

    def test_cancelled_combine_failure_is_not_hidden_and_saves_cancelled_diagnostic(self):
        failure=OSError('owned cancelled merge failure')
        def stop_on_running(state):
            if state['status']=='running':self.stop.set()
        with self.services(),self.failed_output(failure) as captured:
            with self.assertRaises(OSError) as caught:self.run_pipeline(stop_on_running)
        self.assertIs(caught.exception,failure)
        state=self.saved()
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual(state['finalization_error']['stage'],'combine')
        self.assertEqual(state['pending_outputs'],captured[0][0])
        self.assertEqual(len(captured[1]),1);self.assert_unlocked()

    def test_unrelated_callers_handled_exception_does_not_hide_new_merge_failure(self):
        failure=OSError('actual finalization failure');unrelated=RuntimeError('caller context only')
        def stop_on_running(state):
            if state['status']=='running':self.stop.set()
        try:raise unrelated
        except RuntimeError:
            with self.services(),self.failed_output(failure):
                with self.assertRaises(OSError) as caught:self.run_pipeline(stop_on_running)
        self.assertIs(caught.exception,failure)
        self.assertFalse(getattr(unrelated,'__notes__',[]));self.assert_unlocked()

    def test_diagnostic_write_failure_never_replaces_scheduler_or_first_merge_error(self):
        for has_primary in (False,True):
            with self.subTest(has_primary=has_primary):
                self.config.project=self.root/('with-primary' if has_primary else 'without-primary')
                self.stop.clear();primary=RuntimeError('first scheduler')
                failure=OSError('first merge');diagnostic=PermissionError('diagnostic failed')
                def stop_on_running(state):
                    if not has_primary and state['status']=='running':self.stop.set()
                with self.services(),self.failed_output(failure,diagnostic_error=diagnostic) as captured:
                    if has_primary:
                        with patch.object(r.futures,'wait',side_effect=primary):
                            with self.assertRaises(RuntimeError) as caught:self.run_pipeline()
                    else:
                        with self.assertRaises(OSError) as caught:self.run_pipeline(stop_on_running)
                expected=primary if has_primary else failure
                self.assertIs(caught.exception,expected);self.assert_notes(expected,str(diagnostic))
                if has_primary:self.assert_notes(expected,str(failure))
                state=self.saved()
                self.assertEqual(state['status'],'running')
                self.assertEqual(state['pending_outputs'],captured[0][0])
                self.assertEqual(len(captured[1]),1);self.assertEqual(len(captured[2]),1)
                self.assert_unlocked()

    def test_pending_recovery_failure_is_secondary_to_combine_and_scheduler(self):
        primary=RuntimeError('original scheduler');failure=OSError('original combine')
        recovery=PermissionError('original recovery');original=r._recover_pending_outputs
        def recover(project,state):
            if state.get('pending_outputs'):raise recovery
            return original(project,state)
        with self.services(),patch.object(r.futures,'wait',side_effect=primary), \
                patch.object(r,'_recover_pending_outputs',side_effect=recover),self.failed_output(failure) as captured:
            with self.assertRaises(RuntimeError) as caught:self.run_pipeline()
        self.assertIs(caught.exception,primary)
        self.assert_notes(primary,str(failure),str(recovery))
        self.assertEqual(self.saved()['pending_outputs'],captured[0][0]);self.assert_unlocked()

    def test_all_valid_manual_outputs_survive_merge_failure_and_resume_without_work(self):
        with self.services():self.assertEqual(self.run_pipeline()['status'],'complete')
        target=self.config.project/'中文草稿.srt'
        manual='1\n00:00:00,100 --> 00:00:00,900\n人工保留文本\n'.encode('utf-8')
        target.write_bytes(manual)
        failure=OSError('automatic translation output denied')
        auto=self.config.project/'自动更新'/'中文草稿.srt'
        self.progress.clear();self.verifications.clear()
        def no_work(*args,**kwargs):self.fail('complete cached work was submitted again')
        with self.services(recognize=no_work,translate=no_work),self.failed_output(failure,destination=auto) as captured:
            with self.assertRaises(OSError) as caught:self.run_pipeline()
        self.assertIs(caught.exception,failure)
        failed=self.saved()
        self.assertEqual(failed['status'],'asr_incomplete')
        self.assertEqual((failed['recognized'],failed['translated']),(3,3))
        self.assertEqual(failed['pending_outputs'],captured[0][0])
        self.assertEqual(target.read_bytes(),manual)
        self.assertTrue(any(path.read_bytes()==manual for path in
            (self.config.project/'用户修改备份').iterdir() if path.is_file()))
        self.assertEqual(self.verifications,[])
        observed=[]
        def observe(state):observed.append('finalization_error' in state)
        with self.services(recognize=no_work,translate=no_work):
            resumed=self.run_pipeline(observe)
        self.assertEqual(resumed['status'],'complete')
        self.assertNotIn('pending_outputs',self.saved())
        self.assertNotIn('finalization_error',self.saved())
        self.assertTrue(observed);self.assertFalse(any(observed))
        self.assertEqual(target.read_bytes(),manual);self.assertTrue(auto.is_file());self.assert_unlocked()

    def test_scheduler_publish_write_failure_uses_one_diagnostic_without_callbacks_or_ledger(self):
        primary=RuntimeError('scheduler exception');failure=OSError('terminal state denied')
        original=r.atomic_json;diagnostics=[];failed=False;post_failure_progress=[]
        def save(path,state):
            nonlocal failed
            if Path(path)==self.config.project/'state.json' and state.get('status')!='running':
                if not state.get('finalization_error'):
                    failed=True;raise failure
                diagnostics.append(copy.deepcopy(state))
            return original(path,state)
        def observe(state):
            if failed:post_failure_progress.append(state['status'])
        with self.services(),patch.object(r.futures,'wait',side_effect=primary),patch.object(r,'atomic_json',side_effect=save):
            with self.assertRaises(RuntimeError) as caught:self.run_pipeline(observe)
        self.assertIs(caught.exception,primary);self.assert_notes(primary,str(failure),'publish')
        self.assertEqual(len(diagnostics),1);self.assertEqual(post_failure_progress,[])
        self.assertEqual(self.saved()['finalization_error']['stage'],'publish_state')
        self.assertTrue((self.config.project/'原文.srt').is_file());self.assert_unlocked()

    def test_progress_failure_after_successful_state_save_is_not_called_again(self):
        failure=RuntimeError('final progress callback failed');committed=[];callbacks=[]
        def observe(state):
            if state['status']=='complete':
                callbacks.append(state['status']);committed.append(self.saved())
                raise failure
        with self.services():
            with self.assertRaises(RuntimeError) as caught:self.run_pipeline(observe)
        self.assertIs(caught.exception,failure)
        self.assertEqual(callbacks,['complete'])
        self.assertEqual(committed[0]['status'],'complete')
        self.assertEqual(self.saved()['status'],'asr_incomplete')
        self.assertEqual(self.saved()['finalization_error']['stage'],'publish_progress')
        self.assertIn('已保存',self.saved()['message'])
        self.assertEqual((self.saved()['recognized'],self.saved()['translated']),(3,3))
        self.assertTrue((self.config.project/'双语草稿.srt').is_file());self.assert_unlocked()

    def test_ledger_summary_failure_is_not_retried_by_diagnostic(self):
        primary=RuntimeError('scheduler failure');failure=OSError('ledger local summary failure')
        armed=False;calls=[]
        def wait(*args,**kwargs):
            nonlocal armed
            armed=True;raise primary
        def summary():
            calls.append(armed)
            if armed:raise failure
            return {}
        from types import SimpleNamespace
        context=(SimpleNamespace(asr_endpoint='https://invalid.example'),SimpleNamespace(summary=summary))
        with self.services(),patch.object(r,'cloud_context',return_value=context), \
                patch.object(r.futures,'wait',side_effect=wait):
            with self.assertRaises(RuntimeError) as caught:self.run_pipeline()
        self.assertIs(caught.exception,primary);self.assert_notes(primary,str(failure))
        self.assertEqual(calls.count(True),1)
        self.assertEqual(self.saved()['finalization_error']['stage'],'publish_cost');self.assert_unlocked()

    def test_new_interrupt_during_combine_recovery_retains_priority_and_pending(self):
        primary=RuntimeError('scheduler');failure=OSError('merge')
        interruption=KeyboardInterrupt('new interrupt');original=r._recover_pending_outputs
        def recover(project,state):
            if state.get('pending_outputs'):raise interruption
            return original(project,state)
        with self.services(),patch.object(r.futures,'wait',side_effect=primary), \
                patch.object(r,'_recover_pending_outputs',side_effect=recover),self.failed_output(failure):
            with self.assertRaises(KeyboardInterrupt) as caught:self.run_pipeline()
        self.assertIs(caught.exception,interruption)
        self.assertIn('pending_outputs',self.saved());self.assert_unlocked()


class ProjectLockErrorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        temporary=tempfile.TemporaryDirectory(prefix='owned-project-lock-errors-')
        self.addCleanup(temporary.cleanup);self.project=Path(temporary.name)

    def assert_guard_free(self):
        with _file_lock(self.project/'run.guard.lock',blocking=False) as acquired:self.assertTrue(acquired)

    def exercise(self,*,body=None,marker=None,guard=None):
        lock=r.ProjectLock(self.project);original=Path.unlink;called=[];caught=None
        def unlink(path,*args,**kwargs):
            if Path(path)==lock.path and marker is not None:raise marker
            return original(path,*args,**kwargs)
        try:
            with patch.object(Path,'unlink',unlink):
                with lock:
                    real_guard=lock._guard
                    class Guard:
                        def close(inner):
                            # This injected error occurs after real release; it
                            # does not simulate failure inside the OS unlock.
                            called.append('guard');real_guard.close()
                            if guard is not None:raise guard
                    lock._guard=Guard()
                    if body is not None:raise body
        except BaseException as error:caught=error
        self.assertEqual(called,['guard']);self.assertFalse(lock.owned);self.assertIsNone(lock._guard)
        self.assert_guard_free()
        return caught,lock.path

    def test_body_error_remains_primary_when_marker_and_guard_cleanup_fail(self):
        body=RuntimeError('body primary');marker=PermissionError('marker failure');guard=OSError('guard failure')
        caught,path=self.exercise(body=body,marker=marker,guard=guard)
        self.assertIs(caught,body)
        notes='\n'.join(body.__notes__)
        for text in (str(path),str(marker),str(guard)):self.assertIn(text,notes)
        self.assertTrue(path.exists())

    def test_without_body_first_cleanup_error_stays_primary_and_later_is_noted(self):
        marker=PermissionError('first marker');guard=OSError('later guard')
        caught,path=self.exercise(marker=marker,guard=guard)
        self.assertIs(caught,marker);self.assertIn(str(guard),'\n'.join(marker.__notes__))
        self.assertTrue(path.exists())

    def test_without_body_guard_error_is_primary_and_successful_marker_stays_removed(self):
        guard=OSError('guard failure')
        caught,path=self.exercise(guard=guard)
        self.assertIs(caught,guard);self.assertFalse(path.exists())

    def test_unrelated_callers_handled_exception_does_not_hide_guard_failure(self):
        unrelated=RuntimeError('caller context only');guard=OSError('real guard failure')
        try:raise unrelated
        except RuntimeError:caught,path=self.exercise(guard=guard)
        self.assertIs(caught,guard);self.assertFalse(path.exists())
        self.assertFalse(getattr(unrelated,'__notes__',[]))

    def test_normal_cleanup_preserves_body_error_without_notes(self):
        body=RuntimeError('body only');caught,path=self.exercise(body=body)
        self.assertIs(caught,body);self.assertFalse(path.exists())
        self.assertFalse(getattr(body,'__notes__',[]))

    def test_new_marker_interrupt_keeps_priority_but_guard_is_still_closed(self):
        body=RuntimeError('older body');interruption=KeyboardInterrupt('new marker interrupt')
        guard=OSError('guard error after interrupt')
        caught,path=self.exercise(body=body,marker=interruption,guard=guard)
        self.assertIs(caught,interruption)
        self.assertIn(str(guard),'\n'.join(interruption.__notes__))
        self.assertTrue(path.exists())

    def test_new_guard_interrupt_is_not_swallowed_for_body_or_marker_error(self):
        interruption=SystemExit('new guard interrupt')
        caught,path=self.exercise(body=RuntimeError('body'),marker=PermissionError('marker'),guard=interruption)
        self.assertIs(caught,interruption);self.assertTrue(path.exists())

    def test_guard_failure_before_actual_release_is_reported_not_claimed_released(self):
        primary=RuntimeError('body primary');failure=OSError('guard did not release')
        lock=r.ProjectLock(self.project);real_guard=None
        try:
            with self.assertRaises(RuntimeError) as caught:
                with lock:
                    real_guard=lock._guard
                    class FailingGuard:
                        def close(inner):raise failure
                    lock._guard=FailingGuard()
                    raise primary
            self.assertIs(caught.exception,primary)
            self.assertIn(str(failure),'\n'.join(primary.__notes__))
            self.assertFalse(lock.path.exists())
            with _file_lock(self.project/'run.guard.lock',blocking=False) as acquired:
                self.assertFalse(acquired)
        finally:
            if real_guard is not None:real_guard.close()
        self.assert_guard_free()


if __name__=='__main__':unittest.main()
