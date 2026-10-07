import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from subtitle_pipeline import runner as r
from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        network_guard=patch('socket.create_connection',side_effect=AssertionError('Network disabled in integrity tests'))
        network_guard.start()
        self.addCleanup(network_guard.stop)
        process_guard=patch.object(r,'run_process',side_effect=AssertionError('External commands disabled in integrity tests'))
        process_guard.start()
        self.addCleanup(process_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'video.wav'
        self.source.write_bytes(b'original sound')
        self.config = r.PipelineConfig(self.source, self.root/'job', chunk_seconds=10, overlap_seconds=1)

    def services(self, translate=None, recognize=None):
        return patch.multiple(r, probe_media=lambda _,**kw:25000, detect_silences=lambda *a:[],
            recognize_chunk=recognize or (lambda c,ch,f,s:[Cue(100,900,f'line {ch.index}')]),
            translate_texts=translate or (lambda texts,*a,**kw:['中文'+x for x in texts]))

    def edit(self,path,old,new):
        path.write_text(path.read_text(encoding='utf-8').replace(old,new),encoding='utf-8')

    def test_conflicting_source_edits_remain_blocked_until_reconciled(self):
        with self.services():r.run_pipeline(self.config)
        folder=self.config.project/'片段'/'0001'
        local,public=folder/'source.local.srt',folder/'原文.srt'
        self.edit(local,'line 0','local human')
        self.edit(public,'line 0','public human')
        before=(local.read_bytes(),public.read_bytes())
        asr_calls=[]
        def recognize(*args):
            asr_calls.append(1)
            return [Cue(100,900,'unexpected new ASR')]
        with self.services(recognize=recognize):
            for _ in range(2):
                state=r.run_pipeline(self.config)
                self.assertEqual(state['status'],'asr_incomplete')
                self.assertEqual(state['parts']['0']['asr'],'needs_review')
                self.assertEqual((local.read_bytes(),public.read_bytes()),before)
            local.unlink()
            state=r.run_pipeline(self.config)
            self.assertEqual(state['status'],'asr_incomplete')
            self.assertEqual(state['parts']['0']['asr'],'needs_review')
            self.assertEqual(public.read_bytes(),before[1])
            local.write_bytes(before[0])
            public.write_bytes(local.read_bytes())
            state=r.run_pipeline(self.config)
        self.assertEqual(asr_calls,[])
        self.assertEqual(state['status'],'complete')
        self.assertIn('local human',(self.config.project/'原文.srt').read_text(encoding='utf-8'))

    def test_public_translation_edit_is_merged_without_retranslation(self):
        with self.services():r.run_pipeline(self.config)
        folder=self.config.project/'片段'/'0001'
        self.edit(folder/'中文草稿.srt','中文line 0','人工公开译文')
        translated=[]
        def translate(texts,*args,**kwargs):
            translated.extend(texts)
            return ['unexpected translation' for _ in texts]
        with self.services(translate):state=r.run_pipeline(self.config)
        self.assertEqual(translated,[])
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['review_status'],'needs_review')
        self.assertIn('人工公开译文',(folder/'target.local.srt').read_text(encoding='utf-8'))
        self.assertIn('人工公开译文',(self.config.project/'中文草稿.srt').read_text(encoding='utf-8'))

    def test_local_translation_edit_syncs_public_copy(self):
        with self.services():r.run_pipeline(self.config)
        folder=self.config.project/'片段'/'0001'
        self.edit(folder/'target.local.srt','中文line 0','人工本地译文')
        with self.services():state=r.run_pipeline(self.config)
        self.assertEqual(state['status'],'complete')
        self.assertIn('人工本地译文',(folder/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertIn('人工本地译文',(self.config.project/'中文草稿.srt').read_text(encoding='utf-8'))

    def test_conflicting_translation_edits_stay_blocked_until_reconciled(self):
        with self.services():r.run_pipeline(self.config)
        folder=self.config.project/'片段'/'0001'
        local,public=folder/'target.local.srt',folder/'中文草稿.srt'
        self.edit(local,'中文line 0','本地修改')
        self.edit(public,'中文line 0','公开修改')
        before=(local.read_bytes(),public.read_bytes())
        translated=[]
        def translate(texts,*args,**kwargs):
            translated.extend(texts)
            return ['unexpected translation' for _ in texts]
        with self.services(translate):
            for _ in range(2):
                state=r.run_pipeline(self.config)
                self.assertEqual(state['status'],'translation_incomplete')
                self.assertEqual(state['parts']['0']['translation'],'needs_review')
                self.assertEqual((local.read_bytes(),public.read_bytes()),before)
            local.unlink()
            state=r.run_pipeline(self.config)
            self.assertEqual(state['status'],'translation_incomplete')
            self.assertEqual(state['parts']['0']['translation'],'needs_review')
            self.assertEqual(public.read_bytes(),before[1])
            local.write_bytes(before[0])
            public.write_bytes(local.read_bytes())
            state=r.run_pipeline(self.config)
        self.assertEqual(translated,[])
        self.assertEqual(state['status'],'complete')
        self.assertIn('本地修改',(self.config.project/'中文草稿.srt').read_text(encoding='utf-8'))

    def test_public_translation_edit_keeps_chunk_offset_in_merged_output(self):
        def recognize(config,chunk,folder,stop):
            offset=chunk.core_start_ms-chunk.audio_start_ms
            return [Cue(offset+100,offset+900,f'line {chunk.index}')]
        with self.services(recognize=recognize):r.run_pipeline(self.config)
        folder=self.config.project/'片段'/'0002'
        self.edit(folder/'中文草稿.srt','中文line 1','第二段人工译文')
        with self.services(recognize=recognize):state=r.run_pipeline(self.config)
        from subtitle_pipeline.subtitles import parse_srt
        local=parse_srt((folder/'target.local.srt').read_text(encoding='utf-8'))
        merged=parse_srt((self.config.project/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertEqual(local,[Cue(1100,1900,'第二段人工译文')])
        self.assertIn(Cue(10100,10900,'第二段人工译文'),merged)
        self.assertEqual(state['status'],'complete')

    def test_merged_source_text_edit_updates_parts_and_retranslates_without_asr(self):
        with self.services():r.run_pipeline(self.config)
        public=self.config.project/'原文.srt'
        old_translation=(self.config.project/'中文草稿.srt').read_bytes()
        self.edit(public,'line 0','root correction')
        seen=[]
        def translate(texts,*args,**kwargs):
            seen.extend(texts)
            return ['retranslated '+text for text in texts]
        asr=[]
        def recognize(*args):
            asr.append(1)
            return []
        with self.services(translate,recognize):state=r.run_pipeline(self.config)
        self.assertEqual(asr,[])
        self.assertIn('root correction',seen)
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['review_status'],'needs_review')
        folder=self.config.project/'片段'/'0001'
        self.assertIn('root correction',(folder/'source.local.srt').read_text(encoding='utf-8'))
        self.assertIn('root correction',(folder/'原文.srt').read_text(encoding='utf-8'))
        self.assertIn('retranslated root correction',(self.config.project/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertTrue(any(path.read_bytes()==old_translation for path in (self.config.project/'用户修改备份').glob('*.srt')))

    def test_merged_source_edit_updates_context_copy_but_not_real_repetition(self):
        def recognize(config,chunk,folder,stop):
            if chunk.index==0:return [Cue(9400,10400,'same words')]
            if chunk.index==1:return [Cue(400,1400,'same words'),Cue(5000,6000,'same words')]
            return []
        with self.services(recognize=recognize):r.run_pipeline(self.config)
        public=self.config.project/'原文.srt'
        cues=parse_srt(public.read_text(encoding='utf-8'))
        self.assertEqual(len(cues),2)
        cues[0]=Cue(cues[0].start_ms,cues[0].end_ms,'corrected boundary')
        public.write_text(render_srt(cues),encoding='utf-8')
        with self.services(recognize=recognize):state=r.run_pipeline(self.config)
        first=parse_srt((self.config.project/'片段'/'0001'/'source.local.srt').read_text(encoding='utf-8'))
        second=parse_srt((self.config.project/'片段'/'0002'/'source.local.srt').read_text(encoding='utf-8'))
        self.assertEqual(first,[Cue(9400,10400,'corrected boundary')])
        self.assertEqual(second,[Cue(400,1400,'corrected boundary'),Cue(5000,6000,'same words')])
        self.assertEqual(state['status'],'complete')

    def test_complex_merged_source_edit_stays_blocked_without_recognition_or_translation(self):
        with self.services():r.run_pipeline(self.config)
        public=self.config.project/'原文.srt'
        original=public.read_bytes()
        edited=parse_srt(public.read_text(encoding='utf-8'))+[Cue(4000,5000,'added line')]
        public.write_text(render_srt(edited),encoding='utf-8')
        expected=public.read_bytes()
        calls=[]
        with self.services(translate=lambda *a,**k:calls.append('translation') or [],
                           recognize=lambda *a:calls.append('recognition') or []):
            for _ in range(2):
                state=r.run_pipeline(self.config)
                self.assertEqual(state['status'],'asr_incomplete')
                self.assertTrue(state.get('merged_source_needs_review'))
                self.assertEqual(public.read_bytes(),expected)
            public.unlink()
            state=r.run_pipeline(self.config)
            self.assertEqual(state['status'],'asr_incomplete')
            public.write_bytes(original)
            state=r.run_pipeline(self.config)
        self.assertEqual(state['status'],'complete')
        self.assertEqual(calls,[])

    def test_root_human_translation_is_preserved_and_flagged_after_source_edit(self):
        with self.services():r.run_pipeline(self.config)
        public=self.config.project/'原文.srt'
        translation=self.config.project/'中文草稿.srt'
        self.edit(translation,'中文line 0','preserved human translation')
        saved=translation.read_bytes()
        self.edit(public,'line 0','new original wording')
        with self.services():state=r.run_pipeline(self.config)
        self.assertEqual(translation.read_bytes(),saved)
        self.assertEqual(state['review_status'],'needs_review')
        self.assertIn('new original wording',(self.config.project/'自动更新'/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertIn('原文',state['message'])
        self.assertIn('复核',state['message'])

    def test_merged_edit_does_not_change_another_published_overlapping_repeat(self):
        def recognize(config,chunk,folder,stop):
            return [Cue(2000,4000,'repeated'),Cue(2400,4400,'repeated')] if chunk.index==0 else []
        with self.services(recognize=recognize):r.run_pipeline(self.config)
        public=self.config.project/'原文.srt'
        cues=parse_srt(public.read_text(encoding='utf-8'))
        cues[0]=Cue(2000,4000,'first correction')
        public.write_text(render_srt(cues),encoding='utf-8')
        with self.services(recognize=recognize):state=r.run_pipeline(self.config)
        actual=parse_srt((self.config.project/'片段'/'0001'/'source.local.srt').read_text(encoding='utf-8'))
        self.assertEqual(actual,[Cue(2000,4000,'first correction'),Cue(2400,4400,'repeated')])
        self.assertEqual(state['status'],'complete')

    def test_interrupted_merged_edit_recovers_before_any_retranslation(self):
        with self.services():r.run_pipeline(self.config)
        self.edit(self.config.project/'原文.srt','line 0','recoverable correction')
        original_write=r.atomic_text
        def failing_write(path,text):
            if path.name=='原文.srt' and path.parent.name=='0001':
                raise OSError('simulated write interruption')
            return original_write(path,text)
        translated=[]
        def translate(texts,*args,**kwargs):
            translated.extend(texts)
            return ['updated '+text for text in texts]
        with self.services(translate),patch.object(r,'atomic_text',side_effect=failing_write):
            interrupted=r.run_pipeline(self.config)
        self.assertEqual(interrupted['status'],'asr_incomplete')
        self.assertIn('merged_source_edit',interrupted)
        self.assertEqual(translated,[])
        with self.services(translate):resumed=r.run_pipeline(self.config)
        self.assertEqual(resumed['status'],'complete')
        self.assertNotIn('merged_source_edit',resumed)
        self.assertEqual(translated,['recoverable correction'])
        folder=self.config.project/'片段'/'0001'
        self.assertEqual((folder/'source.local.srt').read_bytes(),(folder/'原文.srt').read_bytes())

    def test_same_size_same_mtime_source_change_invalidates_identity(self):
        with self.services():r.run_pipeline(self.config)
        stat=self.source.stat()
        self.source.write_bytes(b'changed! sound')
        os.utime(self.source, ns=(stat.st_atime_ns,stat.st_mtime_ns))
        with self.services(), self.assertRaises(ValueError):
            r.run_pipeline(self.config)

    def test_local_source_text_change_retranslates_without_paid_asr(self):
        with self.services():r.run_pipeline(self.config)
        local=self.config.project/'片段'/'0001'/'source.local.srt'
        local.write_text(local.read_text(encoding='utf-8').replace('line 0','corrected'),encoding='utf-8')
        seen=[]
        def translate(texts,*a,**kw):
            seen.extend(texts);return ['新译'+x for x in texts]
        with self.services(translate):
            state=r.run_pipeline(self.config)
        self.assertIn('corrected',seen)
        self.assertEqual(state['review_status'],'needs_review')

    def test_generation_does_not_approve_review(self):
        with self.services():state=r.run_pipeline(self.config)
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state.get('review_status'),'unreviewed')

    def test_settings_module_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.cloud_settings'))


if __name__=='__main__':unittest.main()
