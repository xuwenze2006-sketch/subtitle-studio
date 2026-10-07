"""An encoder's zero exit does not prove that a usable preview was produced."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as w, runner as r


class PreviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='preview-validation-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source.mp4';self.source.write_bytes(b'source')
        self.campaign=self.root/'campaign';self.campaign.mkdir()
        self.folder=self.campaign/'样片/01';self.folder.mkdir(parents=True)
        self.stop=threading.Event()
        self.whole=self.campaign/'源音频.wav';self.whole.write_bytes(b'audio')
        (self.folder/'input.wav').write_bytes(b'sample audio')
        self.manifest={'version':1,'source':r.file_identity(self.source),'duration_ms':2000,
            'source_kind':'video','language':'ja','target':'zh-CN','status':'preparing',
            'timeline_hash':w.sha256(self.whole),'samples':[{'name':'sample','folder':'样片/01',
            'start_sec':0,'end_sec':2,'audio_hash':w.sha256(self.folder/'input.wav')}]}
        w.write_manifest(self.campaign,self.manifest)
        self.info={'streams':[{'codec_type':'video','width':320,'height':180,'r_frame_rate':'24/1',
                               'duration':'2','nb_frames':'48'},
                              {'codec_type':'audio','duration':'2'}],'format':{'duration':'2'}}
        self.progress='frame=48\nout_time_us=2000000\nprogress=end\n'
        self.first_pts={'v:0':'0.000000','a:0':'0.000000'}
        self.addCleanup(patch.stopall)
        patch.object(w,'emit').start()
        patch('socket.socket.connect',side_effect=AssertionError('offline')).start()
        patch('socket.create_connection',side_effect=AssertionError('offline')).start()

    def run_prepare(self,info=None,decode=None):
        def encode(args,*rest,**kwargs):
            Path(args[-1]).write_bytes(b'new preview')
        iterator=iter(decode) if isinstance(decode,list) else None
        def captured(args,**kwargs):
            if args[0]=='ffprobe':
                stream=args[args.index('-select_streams')+1]
                value=self.first_pts[stream]
                frames=[] if value is None else [{'best_effort_timestamp_time':value}]
                return subprocess.CompletedProcess(args,0,json.dumps({'frames':frames}),'')
            if iterator is not None:return next(iterator)
            if decode is not None:return decode(args,**kwargs)
            return subprocess.CompletedProcess(args,0,self.progress,'')
        with patch.object(r,'run_process',side_effect=encode) as encoder, \
                patch.object(w,'media_info',return_value=info or self.info), \
                patch.object(w,'capture_process',side_effect=captured) as capture:
            result=w.prepare(self.source,self.campaign,None,self.stop)
        return result,encoder,capture

    def assert_unpublished(self):
        self.assertTrue((self.folder/'preview.partial.mp4').exists())
        self.assertFalse((self.folder/'preview.mp4').exists())
        saved=w.read_json(self.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_hash',saved['samples'][0])
        self.assertFalse((self.campaign/'review.html').exists())

    def test_exit_zero_empty_container_cannot_publish_or_complete(self):
        with self.assertRaisesRegex(ValueError,'预览'):
            self.run_prepare(info={'streams':[],'format':{}})
        self.assert_unpublished()

    def test_missing_audio_or_video_cannot_publish(self):
        for kind in ('audio','video'):
            with self.subTest(kind=kind):
                info=copy.deepcopy(self.info)
                info['streams']=[s for s in info['streams'] if s['codec_type']!=kind]
                with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare(info=info)
                self.assert_unpublished()

    def test_bad_duration_or_dimensions_cannot_publish(self):
        for field,value in [('duration','nan'),('duration','0'),('duration','9'),('width',0)]:
            with self.subTest(field=field,value=value):
                info=copy.deepcopy(self.info)
                if field=='duration':info['format'][field]=value
                else:info['streams'][0][field]=value
                with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare(info=info)
                self.assert_unpublished()

    def test_decode_zero_frames_truncated_or_missing_end_is_rejected(self):
        for text in ('frame=0\nout_time_us=2000000\nprogress=end\n',
                     'frame=3\nout_time_us=125000\nprogress=end\n',
                     'frame=48\nout_time_us=2000000\nprogress=continue\n'):
            with self.subTest(text=text):
                self.progress=text
                with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare()
                self.assert_unpublished()

    def test_decode_failure_does_not_fallback_or_replace_old_preview(self):
        prior=self.folder/'preview.mp4';prior.write_bytes(b'prior preview')
        with patch.object(r,'run_process') as encoder, \
                patch.object(w,'media_info',return_value=self.info), \
                patch.object(w,'capture_process',side_effect=subprocess.CalledProcessError(1,['ffmpeg'])):
            # Existing unbound preview must be checked rather than overwritten.
            with self.assertRaises((ValueError,subprocess.CalledProcessError)):
                w.prepare(self.source,self.campaign,None,self.stop)
        self.assertEqual(prior.read_bytes(),b'prior preview')
        encoder.assert_not_called()

    def test_empty_or_truncated_audio_cannot_hide_behind_stream_duration(self):
        for audio_time in (0,100000):
            with self.subTest(audio_time=audio_time):
                replies=[subprocess.CompletedProcess([],0,self.progress,''),
                    subprocess.CompletedProcess([],0,f'out_time_us={audio_time}\nprogress=end\n','')]
                with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare(decode=replies)
                self.assert_unpublished()

    def test_late_first_frame_cannot_be_masked_by_correct_last_timestamp(self):
        for stream in ('v:0','a:0'):
            with self.subTest(stream=stream):
                self.first_pts={'v:0':'0','a:0':'0'}
                self.first_pts[stream]='1.958000'
                with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare()
                self.assert_unpublished()

    def test_unknown_or_nonfinite_first_frame_cannot_be_guessed(self):
        for value in (None,'nan','inf','N/A'):
            with self.subTest(value=value):
                self.first_pts['v:0']=value
                with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare()
                self.assert_unpublished()

    def test_extreme_rate_does_not_make_duration_tolerance_unbounded(self):
        info=copy.deepcopy(self.info)
        info['streams'][0]['r_frame_rate']='1/1000'
        replies=[subprocess.CompletedProcess([],0,'frame=1\nout_time_us=125000\nprogress=end\n',''),
                 subprocess.CompletedProcess([],0,self.progress,'')]
        with self.assertRaisesRegex(ValueError,'预览'):self.run_prepare(info=info,decode=replies)
        self.assert_unpublished()

    def test_previously_prepared_empty_legacy_is_downgraded_without_overwrite(self):
        prior=self.folder/'preview.mp4';prior.write_bytes(b'empty legacy')
        self.manifest.update(status='prepared')
        self.manifest['samples'][0]['preview_hash']=w.sha256(prior)
        w.write_manifest(self.campaign,self.manifest)
        with self.assertRaisesRegex(ValueError,'预览'):
            self.run_prepare(info={'streams':[],'format':{}})
        self.assertEqual(prior.read_bytes(),b'empty legacy')
        saved=w.read_json(self.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_validation',saved['samples'][0])

    def test_unvalidated_legacy_cannot_start_paid_sample_work(self):
        prior=self.folder/'preview.mp4';prior.write_bytes(b'legacy unverified')
        self.manifest['samples'][0]['preview_hash']=w.sha256(prior)
        w.write_manifest(self.campaign,self.manifest)
        with patch.object(w,'load_settings',side_effect=AssertionError('paid configuration reached')) as settings, \
                self.assertRaisesRegex(ValueError,'预览'):
            w.run_samples(self.campaign,self.stop)
        settings.assert_not_called()

    def test_stop_after_decode_preserves_partial_and_no_binding(self):
        def decode(*args,**kwargs):
            self.stop.set()
            return subprocess.CompletedProcess([],0,self.progress,'')
        with self.assertRaises(r.Cancelled):self.run_prepare(decode=decode)
        self.assert_unpublished()

    def test_valid_preview_is_bound_and_resume_skips_decode_and_encode(self):
        manifest,encoder,capture=self.run_prepare()
        self.assertEqual(manifest['status'],'prepared')
        self.assertEqual(encoder.call_count,1)
        self.assertEqual(capture.call_count,4)
        sample=manifest['samples'][0]
        self.assertEqual(sample['preview_validation']['sha256'],sample['preview_hash'])
        self.assertEqual(sample['preview_validation']['video_frames'],48)
        with patch.object(r,'run_process',side_effect=AssertionError('reencoded')), \
                patch.object(w,'media_info',side_effect=AssertionError('reprobed')), \
                patch.object(w,'capture_process',side_effect=AssertionError('redecoded')):
            w.prepare(self.source,self.campaign,None,self.stop)

    def test_legacy_bound_preview_is_checked_once_without_reencoding(self):
        prior=self.folder/'preview.mp4';prior.write_bytes(b'legacy preview')
        self.manifest['samples'][0]['preview_hash']=w.sha256(prior)
        w.write_manifest(self.campaign,self.manifest)
        manifest,encoder,capture=self.run_prepare()
        encoder.assert_not_called()
        self.assertEqual(capture.call_count,4)
        self.assertEqual(prior.read_bytes(),b'legacy preview')
        self.assertIn('preview_validation',manifest['samples'][0])

    def test_stop_during_new_preview_hash_does_not_replace(self):
        original=w.cancellable_sha256
        def digest(path,stop):
            value=original(path,stop)
            if Path(path).name=='preview.partial.mp4':stop.set()
            return value
        with patch.object(w,'cancellable_sha256',side_effect=digest),self.assertRaises(r.Cancelled):
            self.run_prepare()
        self.assert_unpublished()

    def test_stop_after_validation_does_not_publish_or_bind(self):
        original=w._checked_preview
        def checked(*args,**kwargs):
            proof=original(*args,**kwargs)
            self.stop.set()
            return proof
        with patch.object(w,'_checked_preview',side_effect=checked),self.assertRaises(r.Cancelled):
            self.run_prepare()
        self.assert_unpublished()


if __name__=='__main__':unittest.main()
