"""Multi-track publication must preserve each track and its recovery evidence."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import media_export
from subtitle_pipeline.languages import video_output_path
from tests import test_draft_video as fixtures


def media():
    return {'streams':[
        {'codec_type':'video','width':640,'height':360,'r_frame_rate':'30/1','nb_frames':'1200'},
        {'codec_type':'audio','codec_name':'aac','sample_rate':'48000','channels':1,
         'channel_layout':'mono','tags':{'language':'eng'},'disposition':{'default':0,'forced':0}},
        {'codec_type':'audio','codec_name':'aac','sample_rate':'48000','channels':1,
         'channel_layout':'mono','tags':{'language':'jpn'},'disposition':{'default':1,'forced':0}}],
        'format':{'duration':'40'}}


class MultiTrackExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DraftVideoTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.media=media()
        self.probe=patch.object(media_export,'media_info',return_value=self.media)
        self.probe.start();self.addCleanup(self.probe.stop)
        self.digests=[]
        def digest(path,stop=None,*,index=0):
            self.digests.append((Path(path),index))
            return 'SHA256='+str(index+1)*64
        self.digest=patch.object(media_export,'audio_digest',side_effect=digest)
        self.digest.start();self.addCleanup(self.digest.stop)
        self.bulk_calls=[]
        def digests(path,stop=None,*,expected_count):
            self.bulk_calls.append((Path(path),expected_count))
            return [media_export.audio_digest(path,stop,index=index) for index in range(expected_count)]
        self.bulk=patch.object(media_export,'audio_digests',side_effect=digests)
        self.bulk.start();self.addCleanup(self.bulk.stop)

    def export(self):
        workflow.export_video(self.fixture.campaign,self.fixture.stop,draft=True)

    def receipt(self):
        return self.fixture.campaign/'导出'/'草稿视频'/'publication-checkpoint.json'

    def test_all_tracks_are_mapped_and_verified_in_order(self):
        self.export()
        commands=[call.args[0] for call in self.fixture.encoder.call_args_list if str(call.args[0][-1]).endswith('.mp4')]
        self.assertEqual(len(commands),1)
        args=commands[0]
        self.assertIn('0:a',args)
        self.assertNotIn('0:a:0',args)
        self.assertEqual([index for _path,index in self.digests],[0,1,0,1])
        self.assertEqual(self.bulk_calls,[(self.fixture.source,2),
            (self.fixture.campaign/'导出'/'草稿视频'/'result.partial.mp4',2)])
        record=workflow.read_json(self.receipt())
        self.assertEqual(record['output_binding']['audio_policy'],'all_tracks_v1')
        self.assertEqual(len(record['verification']['audio_tracks']['source']),2)
        self.assertEqual(record['verification']['audio_tracks']['source'],record['verification']['audio_tracks']['output'])

    def test_disposition_options_preserve_nondefault_and_default_tracks(self):
        self.export()
        args=next(call.args[0] for call in self.fixture.encoder.call_args_list if str(call.args[0][-1]).endswith('.mp4'))
        self.assertEqual(args[args.index('-disposition:a:0')+1],'-default-forced')
        self.assertEqual(args[args.index('-disposition:a:1')+1],'+default-forced')

    def test_missing_or_swapped_second_track_is_not_published(self):
        for fault in ('missing','swapped','metadata'):
            with self.subTest(fault=fault):
                output=deepcopy(self.media)
                if fault=='missing':output['streams'].pop()
                elif fault=='swapped':output['streams'][1:]=output['streams'][1:][::-1]
                else:output['streams'][2]['disposition']['default']=0
                with patch.object(media_export,'media_info',side_effect=lambda path,stop=None:
                        self.media if Path(path)==self.fixture.source else output):
                    with self.assertRaises(ValueError):self.export()
                self.assertFalse(video_output_path(self.fixture.source,'zh-CN',draft=True).exists())
                self.assertNotIn('draft_output',workflow.read_json(self.fixture.campaign/'campaign.json'))

    def test_changed_second_payload_is_not_hidden_by_matching_first_track(self):
        def digest(path,stop=None,*,index=0):
            value=str(index+1)*64
            if Path(path)!=self.fixture.source and index==1:value='f'*64
            return 'SHA256='+value
        with patch.object(media_export,'audio_digest',side_effect=digest),self.assertRaises(ValueError):
            self.export()
        self.assertFalse(video_output_path(self.fixture.source,'zh-CN',draft=True).exists())

    def test_cancel_during_second_track_validation_preserves_encode_without_publication(self):
        def digest(path,stop=None,*,index=0):
            if index==1:raise workflow.r.Cancelled('stop second audio track check')
            return 'SHA256='+'1'*64
        with patch.object(media_export,'audio_digest',side_effect=digest),self.assertRaises(workflow.r.Cancelled):
            self.export()
        self.assertEqual(workflow.read_json(self.receipt().with_name('encoding-checkpoint.json'))['status'],'encoded')
        self.assertFalse(video_output_path(self.fixture.source,'zh-CN',draft=True).exists())

    def fail_copy(self):
        with patch.object(media_export,'cancellable_copy',side_effect=OSError('synthetic copy failure')),self.assertRaises(OSError):
            self.export()

    def test_truncated_bulk_inventory_is_not_published_or_retried_per_track(self):
        with patch.object(media_export,'audio_digests',side_effect=[
                ['SHA256='+'1'*64,'SHA256='+'2'*64],['SHA256='+'1'*64]]),self.assertRaises(ValueError):
            self.export()
        self.assertEqual(self.digests,[])
        self.assertEqual(workflow.read_json(self.receipt().with_name('encoding-checkpoint.json'))['status'],'encoded')
        self.assertFalse(video_output_path(self.fixture.source,'zh-CN',draft=True).exists())

    def test_copy_retry_reuses_complete_per_track_evidence(self):
        self.fail_copy();self.digests.clear();self.bulk_calls.clear();self.fixture.encoder.reset_mock()
        self.export()
        self.assertEqual(self.fixture.video_encode_count(),0)
        self.assertEqual(self.fixture.decode_check_count(),0)
        self.assertEqual(self.digests,[])
        self.assertEqual(self.bulk_calls,[])

    def test_single_track_keeps_legacy_binding_and_completed_encode_recovery(self):
        one=deepcopy(self.media);one['streams'].pop()
        with patch.object(media_export,'media_info',return_value=one):
            self.fail_copy()
            record=workflow.read_json(self.receipt())
            self.assertNotIn('audio_policy',record['output_binding'])
            args=next(call.args[0] for call in self.fixture.encoder.call_args_list if str(call.args[0][-1]).endswith('.mp4'))
            self.assertIn('0:a:0',args)
            self.digests.clear();self.fixture.encoder.reset_mock()
            self.export()
        self.assertEqual(self.fixture.video_encode_count(),0)
        self.assertEqual(self.fixture.decode_check_count(),0)
        self.assertEqual(self.digests,[])

    def test_missing_per_track_proof_requires_local_revalidation_not_encoding(self):
        self.fail_copy()
        record=workflow.read_json(self.receipt());record['verification'].pop('audio_tracks',None)
        self.fixture.write(self.receipt(),record)
        self.digests.clear();self.fixture.encoder.reset_mock()
        self.export()
        self.assertEqual(self.fixture.video_encode_count(),0)
        self.assertEqual([index for _path,index in self.digests],[0,1,0,1])
        self.assertEqual(self.fixture.decode_check_count(),3)

    def test_final_with_matching_binding_but_missing_multitrack_proof_is_not_certified(self):
        self.export()
        final=video_output_path(self.fixture.source,'zh-CN',draft=True)
        before=final.read_bytes()
        record=workflow.read_json(self.receipt());record['verification'].pop('audio_tracks',None)
        self.fixture.write(self.receipt(),record)
        with self.assertRaisesRegex(ValueError,'无法证明'):self.export()
        self.assertEqual(final.read_bytes(),before)

    def test_conflicting_first_track_summary_does_not_certify_existing_final(self):
        self.export()
        final=video_output_path(self.fixture.source,'zh-CN',draft=True)
        before=final.read_bytes()
        record=workflow.read_json(self.receipt())
        record['verification']['source_audio_sha256']='SHA256='+'a'*64
        record['verification']['output_audio_sha256']='SHA256='+'a'*64
        self.fixture.write(self.receipt(),record)
        with self.assertRaisesRegex(ValueError,'无法证明'):self.export()
        self.assertEqual(final.read_bytes(),before)

    def normalized_inventory(self):
        self.media['streams'][2]['disposition']['default']=0
        output=deepcopy(self.media)
        output['streams'][1]['disposition']['default']=1
        return lambda path,stop=None:self.media if Path(path)==self.fixture.source else output

    def test_unspecified_default_normalization_records_raw_flags_and_resumes_without_encoding(self):
        with patch.object(media_export,'media_info',side_effect=self.normalized_inventory()):
            self.fail_copy()
            record=workflow.read_json(self.receipt())
            proof=record['verification']['audio_tracks']
            self.assertEqual(proof['version'],2)
            self.assertEqual(proof['default_policy'],'mp4_first_when_unspecified')
            self.assertEqual([track['default'] for track in proof['source']],[0,0])
            self.assertEqual([track['default'] for track in proof['output']],[1,0])
            self.digests.clear();self.fixture.encoder.reset_mock()
            self.export()
        self.assertEqual(self.fixture.video_encode_count(),0)
        self.assertEqual(self.fixture.decode_check_count(),0)
        self.assertEqual(self.digests,[])

    def test_false_normalization_policy_cannot_skip_revalidating_completed_encode(self):
        with patch.object(media_export,'media_info',side_effect=self.normalized_inventory()):
            self.fail_copy()
            record=workflow.read_json(self.receipt())
            record['verification']['audio_tracks']['default_policy']='preserved'
            self.fixture.write(self.receipt(),record)
            self.digests.clear();self.fixture.encoder.reset_mock()
            self.export()
        self.assertEqual(self.fixture.video_encode_count(),0)
        self.assertEqual(self.fixture.decode_check_count(),3)
        self.assertEqual([index for _path,index in self.digests],[0,1,0,1])
        self.assertEqual(workflow.read_json(self.receipt())['verification']['audio_tracks']['default_policy'],
                         'mp4_first_when_unspecified')

    def test_legacy_first_track_final_is_not_reused_as_preserved_multitrack_export(self):
        self.export()
        final=video_output_path(self.fixture.source,'zh-CN',draft=True)
        before=final.read_bytes()
        manifest=workflow.read_json(self.fixture.campaign/'campaign.json')
        for key in ('audio_policy','audio_tracks'):manifest['draft_output_binding'].pop(key,None)
        self.fixture.write(self.fixture.campaign/'campaign.json',manifest)
        record=workflow.read_json(self.receipt())
        for key in ('audio_policy','audio_tracks'):
            record['output_binding'].pop(key,None)
            record['verification']['output_binding'].pop(key,None)
        record['verification'].pop('audio_tracks',None)
        self.fixture.write(self.receipt(),record)
        with self.assertRaisesRegex(ValueError,'无法证明'):self.export()
        self.assertEqual(final.read_bytes(),before)


if __name__=='__main__':unittest.main()
