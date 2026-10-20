"""Real editor saves at the durable publication boundary must remain recoverable."""
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner as r
from subtitle_pipeline.subtitles import Cue
from tests import test_runner as fixture


@unittest.skipUnless(os.name=='nt','Windows publication and capture cleanup contract')
class PublicationEditTests(unittest.TestCase):
    setUp=fixture.RunnerTests.setUp
    config=fixture.RunnerTests.config
    services=fixture.RunnerTests.services

    def test_save_after_durable_plan_preserves_actual_replaced_generation(self):
        config=self.config(translate=False)
        target=config.project/'原文.srt'
        requested=threading.Event();finished=threading.Event()
        editor_errors=[];manual=[];fired=[]
        original=r.atomic_json
        def editor():
            if not requested.wait(10):return
            try:
                if not fired:return
                content=target.read_bytes().replace(b'part 0',b'HUMAN_SAVE_AFTER_PLAN')
                self.assertIn(b'HUMAN_SAVE_AFTER_PLAN',content)
                target.write_bytes(content)
                manual.append(content)
            except BaseException as error:editor_errors.append(error)
            finally:finished.set()
        def checkpoint(path,state):
            result=original(path,state)
            if (Path(path).name=='state.json' and state.get('pending_outputs')
                    and target.exists() and not fired):
                fired.append(True);requested.set()
                self.assertTrue(finished.wait(10),'independent editor did not finish')
                if editor_errors:raise editor_errors[0]
            return result
        thread=threading.Thread(target=editor,name='fixture-editor')
        thread.start()
        try:
            with self.services(),patch.object(r,'atomic_json',side_effect=checkpoint):
                with self.assertRaises(r.HumanEditConflict):r.run_pipeline(config)
        finally:
            requested.set();thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(fired)
        self.assertEqual(len(manual),1)
        copies=[p for p in (config.project/'用户修改备份').rglob('*') if p.is_file()]
        self.assertTrue(any(p.read_bytes()==manual[0] for p in copies))
        state=json.loads((config.project/'state.json').read_text(encoding='utf-8'))
        self.assertNotEqual(state['status'],'complete')
        self.assertEqual(state['review_status'],'needs_review')
        self.assertEqual(state['pending_outputs']['version'],2)
        self.assertIn('用户修改备份',state['message'])
        # The same conflict may not start more recognition or remove the saved generation.
        def no_work(*args,**kwargs):self.fail('work started before resolving publication conflict')
        with self.services(recognize=no_work,translate=no_work):
            resumed=r.run_pipeline(config)
        self.assertEqual(resumed['status'],'asr_incomplete')
        self.assertTrue(any(p.exists() and p.read_bytes()==manual[0] for p in copies))

    def test_normal_publication_leaves_no_capture_accumulation(self):
        config=self.config()
        with self.services():state=r.run_pipeline(config)
        self.assertEqual(state['status'],'complete')
        captures=list((config.project/'用户修改备份').glob('发布恢复-*'))
        self.assertEqual(captures,[])
        self.assertNotIn('pending_outputs',state)
        def no_work(*args,**kwargs):self.fail('completed work repeated')
        before={p.name:p.read_bytes() for p in config.project.glob('*.srt')}
        with self.services(recognize=no_work,translate=no_work):
            self.assertEqual(r.run_pipeline(config)['status'],'complete')
        self.assertEqual({p.name:p.read_bytes() for p in config.project.glob('*.srt')},before)

    def test_directory_conflict_at_publication_also_stops_new_work(self):
        config=self.config(translate=False)
        target=config.project/'原文.srt'
        preserved=self.root/'editor-saved-original.srt'
        calls=[];fired=[];original=r.atomic_json
        def recognize(config,chunk,folder,stop):
            calls.append(chunk.index)
            offset=chunk.core_start_ms-chunk.audio_start_ms
            return [Cue(offset+100,offset+900,f'part {chunk.index}')]
        def checkpoint(path,state):
            result=original(path,state)
            if Path(path).name=='state.json' and state.get('pending_outputs') and target.is_file() and not fired:
                fired.append(True)
                target.rename(preserved);target.mkdir()
            return result
        with self.services(recognize=recognize),patch.object(r,'atomic_json',side_effect=checkpoint):
            with self.assertRaises(r.HumanEditConflict):r.run_pipeline(config)
        self.assertEqual(calls,[0,1])
        self.assertIn(b'part 0',preserved.read_bytes())
        self.assertTrue(target.is_dir())
        state=json.loads((config.project/'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['review_status'],'needs_review')
        self.assertEqual(state['pending_outputs']['checkpoint'],r._output_checkpoint(state))

    def test_conflict_aborts_new_work_and_keeps_verified_inflight_result_for_resume(self):
        config=self.config(workers=2,translate=False)
        target=config.project/'原文.srt'
        second_started=threading.Event();release=threading.Event()
        calls=[];saved_journals=[]
        original_json=r.atomic_json
        original_publish=r.output_publication.publish_record
        manual=b'1\n00:00:00,100 --> 00:00:00,900\nHUMAN_CONCURRENT_CREATE\n'
        def recognize(config,chunk,folder,stop):
            calls.append(chunk.index)
            if chunk.index==0:self.assertTrue(second_started.wait(5))
            elif chunk.index==1:
                second_started.set();self.assertTrue(release.wait(5))
            offset=chunk.core_start_ms-chunk.audio_start_ms
            return [Cue(offset+100,offset+900,f'part {chunk.index}')]
        def checkpoint(path,state):
            result=original_json(path,state)
            if Path(path).name=='state.json' and state.get('pending_outputs') and not saved_journals:
                saved_journals.append(json.loads(json.dumps(state['pending_outputs'])))
                target.write_bytes(manual)
            return result
        def publish(project,record,*args,**kwargs):
            result=original_publish(project,record,*args,**kwargs)
            if result['status']!='clean':release.set()
            return result
        with self.services(recognize=recognize),patch.object(r,'atomic_json',side_effect=checkpoint), \
             patch.object(r.output_publication,'publish_record',side_effect=publish):
            with self.assertRaises(r.HumanEditConflict):r.run_pipeline(config)
        self.assertEqual(set(calls),{0,1},'conflict must not submit third chunk')
        state_path=config.project/'state.json'
        state=json.loads(state_path.read_text(encoding='utf-8'))
        journal=state['pending_outputs']
        self.assertEqual(journal['checkpoint'],saved_journals[0]['checkpoint'])
        self.assertNotEqual(journal['checkpoint'],r._output_checkpoint(state))
        self.assertEqual(journal['owned_results_checkpoint'],r._output_checkpoint(state))
        self.assertEqual(state['parts']['1']['asr'],'done')
        self.assertEqual(target.read_bytes(),manual)
        # Reject a malformed or stale extra checkpoint before touching files.
        for extra in (False,'bad','0'*64):
            bad=json.loads(json.dumps(state))
            bad['pending_outputs']['owned_results_checkpoint']=extra
            with self.assertRaises(r.HumanEditConflict):r._recover_pending_outputs(config.project,bad)
            self.assertEqual(target.read_bytes(),manual)
        # Model an explicit user review: archive every named attempt outside
        # the project, retaining all recovery files, and accept the current root.
        archive=self.root/'reviewed-recovery';archive.mkdir()
        for record in journal['publications']:
            attempt=config.project/record['attempt']
            if attempt.exists():attempt.rename(archive/attempt.name)
        # The first publication had no machine baseline. Keep this human file
        # separately and explicitly choose regeneration instead of inventing one.
        target.rename(archive/'human-original.srt')
        resumed_calls=[]
        def remaining(config,chunk,folder,stop):
            resumed_calls.append(chunk.index)
            offset=chunk.core_start_ms-chunk.audio_start_ms
            return [Cue(offset+100,offset+900,f'part {chunk.index}')]
        with self.services(recognize=remaining):resumed=r.run_pipeline(config)
        self.assertEqual(resumed['status'],'complete',json.dumps(resumed,ensure_ascii=False))
        self.assertEqual(resumed_calls,[2],'verified in-flight ASR must not repeat')
        self.assertEqual((archive/'human-original.srt').read_bytes(),manual)
        self.assertNotIn(b'HUMAN_CONCURRENT_CREATE',target.read_bytes())
        self.assertNotIn('pending_outputs',resumed)


if __name__=='__main__':unittest.main()
