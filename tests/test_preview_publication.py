"""Publishing a checked preview cannot clobber a race winner or redo encoding."""
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as w, runner as r
from tests import test_preview_validation as fixtures


class PreviewPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PreviewValidationTests('test_exit_zero_empty_container_cannot_publish_or_complete')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.preview=self.fixture.folder/'preview.mp4'
        self.partial=self.fixture.folder/'preview.partial.mp4'
        self.stop=self.fixture.stop
        self.rename=os.rename;self.replace=os.replace
        self.calls=[]

    def intercept(self,operation):
        def moved(source,target,*args,**kwargs):
            if Path(target)==self.preview:
                self.calls.append((Path(source),Path(target)))
                return operation(source,target)
            return self.replace(source,target,*args,**kwargs)
        return patch.object(w.os,'replace',side_effect=moved),patch.object(w.os,'rename',side_effect=moved)

    def share_error(self):
        error=PermissionError('fixture sharing violation');error.winerror=32
        return error

    @unittest.skipUnless(os.name=='nt','Windows sharing retry')
    def test_transient_share_retries_same_partial_without_reencoding(self):
        error=self.share_error()
        def operation(source,target):
            if len(self.calls)<3:raise error
            return self.rename(source,target)
        a,b=self.intercept(operation)
        with a,b:
            result,encoder,capture=self.fixture.run_prepare()
        self.assertEqual(result['status'],'prepared')
        self.assertEqual(encoder.call_count,1)
        self.assertEqual(capture.call_count,4)
        self.assertEqual(self.calls,[(self.partial,self.preview)]*3)
        self.assertEqual(self.preview.read_bytes(),b'new preview')
        self.assertFalse(self.partial.exists())

    @unittest.skipUnless(os.name=='nt','Windows sharing retry')
    def test_persistent_share_is_bounded_preserves_partial_and_primary_error(self):
        error=self.share_error()
        a,b=self.intercept(lambda *args:(_ for _ in ()).throw(error))
        with a,b,self.assertRaises(PermissionError) as caught:self.fixture.run_prepare()
        self.assertIs(caught.exception,error)
        self.assertEqual(len(self.calls),6)
        self.fixture.assert_unpublished()

    @unittest.skipUnless(os.name=='nt','Windows sharing retry')
    def test_stop_after_first_sharing_failure_prevents_second_attempt(self):
        error=self.share_error()
        def blocked(*args):
            self.stop.set();raise error
        a,b=self.intercept(blocked)
        with a,b,self.assertRaises(r.Cancelled) as caught:self.fixture.run_prepare()
        self.assertIs(caught.exception.__cause__,error)
        self.assertEqual(len(self.calls),1)
        self.fixture.assert_unpublished()

    def test_file_created_after_validation_is_never_overwritten(self):
        original=w._checked_preview
        def validate(*args,**kwargs):
            proof=original(*args,**kwargs)
            self.preview.write_bytes(b'foreign preview arrived during encoding')
            return proof
        with patch.object(w,'_checked_preview',side_effect=validate), \
                self.assertRaises((ValueError,FileExistsError)):
            self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'foreign preview arrived during encoding')
        self.assertEqual(self.partial.read_bytes(),b'new preview')
        saved=w.read_json(self.fixture.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_hash',saved['samples'][0])

    def test_media_changed_at_move_cannot_be_bound_as_verified(self):
        def operation(source,target):
            Path(source).write_bytes(b'changed after validation')
            return self.rename(source,target)
        a,b=self.intercept(operation)
        with a,b,self.assertRaisesRegex(ValueError,'预览'):self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'changed after validation')
        saved=w.read_json(self.fixture.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_hash',saved['samples'][0])

    def test_cancel_after_target_hash_retains_file_without_complete_binding(self):
        original=w.cancellable_sha256
        def digest(path,stop):
            value=original(path,stop)
            if Path(path)==self.preview:stop.set()
            return value
        with patch.object(w,'cancellable_sha256',side_effect=digest),self.assertRaises(r.Cancelled):
            self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'new preview')
        saved=w.read_json(self.fixture.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_hash',saved['samples'][0])
        self.stop.clear()
        result,encoder,_=self.fixture.run_prepare()
        encoder.assert_not_called()
        self.assertEqual(result['status'],'prepared')

    def test_target_changed_during_hash_cannot_save_stale_binding(self):
        original=w.cancellable_sha256
        def digest(path,stop):
            value=original(path,stop)
            if Path(path)==self.preview:Path(path).write_bytes(b'changed while being hashed')
            return value
        with patch.object(w,'cancellable_sha256',side_effect=digest),self.assertRaisesRegex(ValueError,'预览'):
            self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'changed while being hashed')
        saved=w.read_json(self.fixture.campaign/'campaign.json')
        self.assertNotIn('preview_hash',saved['samples'][0])

    def test_stop_after_publication_does_not_commit_complete_binding(self):
        original=w._publish_preview
        def publish(*args,**kwargs):
            original(*args,**kwargs)
            self.stop.set()
        with patch.object(w,'_publish_preview',side_effect=publish),self.assertRaises(r.Cancelled):
            self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'new preview')
        saved=w.read_json(self.fixture.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_hash',saved['samples'][0])
        self.assertNotIn('preview_validation',saved['samples'][0])
        self.stop.clear()
        result,encoder,_=self.fixture.run_prepare()
        encoder.assert_not_called()
        self.assertEqual(result['status'],'prepared')

    def test_conflicting_unbound_preview_cannot_be_adopted_on_resume(self):
        self.preview.write_bytes(b'foreign preview')
        self.partial.write_bytes(b'checked own preview')
        with patch.object(w,'_checked_preview',side_effect=AssertionError('adopted conflict')), \
                self.assertRaisesRegex(ValueError,'预览.*冲突'):
            self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'foreign preview')
        self.assertEqual(self.partial.read_bytes(),b'checked own preview')
        saved=w.read_json(self.fixture.campaign/'campaign.json')
        self.assertEqual(saved['status'],'preparing')
        self.assertNotIn('preview_hash',saved['samples'][0])

    def test_identical_unbound_preview_and_partial_are_locally_revalidated(self):
        self.preview.write_bytes(b'same preview');self.partial.write_bytes(b'same preview')
        result,encoder,capture=self.fixture.run_prepare()
        encoder.assert_not_called()
        self.assertEqual(capture.call_count,4)
        self.assertEqual(result['status'],'prepared')
        self.assertEqual(self.partial.read_bytes(),b'same preview')

    def test_trusted_preview_keeps_unrelated_old_partial_on_resume(self):
        self.preview.write_bytes(b'trusted preview');self.partial.write_bytes(b'old partial')
        sample=self.fixture.manifest['samples'][0]
        digest=w.sha256(self.preview)
        sample.update(preview_hash=digest,preview_validation={
            'version':2,'sha256':digest,'expected_duration_ms':2000,'video_frames':48})
        w.write_manifest(self.fixture.campaign,self.fixture.manifest)
        with patch.object(w,'_checked_preview',side_effect=AssertionError('duplicate validation')):
            result,encoder,capture=self.fixture.run_prepare()
        encoder.assert_not_called();capture.assert_not_called()
        self.assertEqual(result['status'],'prepared')
        self.assertEqual(self.partial.read_bytes(),b'old partial')

    @unittest.skipUnless(os.name=='nt','Windows sharing retry')
    def test_race_winner_on_retry_does_not_get_replaced(self):
        error=self.share_error()
        def operation(source,target):
            if len(self.calls)==1:
                self.preview.write_bytes(b'foreign winner');raise error
            return self.rename(source,target)
        a,b=self.intercept(operation)
        with a,b,self.assertRaises((ValueError,FileExistsError)):self.fixture.run_prepare()
        self.assertEqual(self.preview.read_bytes(),b'foreign winner')
        self.assertEqual(self.partial.read_bytes(),b'new preview')
        self.assertEqual(len(self.calls),2)

    def test_posix_strategy_links_without_replacing_then_removes_owned_partial(self):
        self.partial.write_bytes(b'checked preview')
        expected=w.sha256(self.partial)
        # Exercise the POSIX algorithm with precreated Path objects; Windows
        # supports hard links too. This is not a claim of Linux GUI acceptance.
        with patch.object(w.os,'name','posix'), \
                patch.object(w,'rename_with_retry',side_effect=AssertionError('unsafe POSIX rename')):
            w._publish_preview(self.partial,self.preview,self.stop,expected_hash=expected)
        self.assertFalse(self.partial.exists())
        self.assertEqual(self.preview.read_bytes(),b'checked preview')

    def test_posix_strategy_preserves_existing_target_and_partial(self):
        self.partial.write_bytes(b'checked preview');self.preview.write_bytes(b'foreign')
        expected=w.sha256(self.partial)
        with patch.object(w.os,'name','posix'),self.assertRaisesRegex(ValueError,'未覆盖'):
            w._publish_preview(self.partial,self.preview,self.stop,expected_hash=expected)
        self.assertEqual(self.partial.read_bytes(),b'checked preview')
        self.assertEqual(self.preview.read_bytes(),b'foreign')

    def test_cancelled_publication_does_not_touch_existing_files(self):
        self.partial.write_bytes(b'checked preview');self.preview.write_bytes(b'foreign')
        self.stop.set()
        with patch.object(w,'rename_with_retry',side_effect=AssertionError('moved after stop')), \
                patch.object(w.os,'link',side_effect=AssertionError('linked after stop')), \
                self.assertRaises(r.Cancelled):
            w._publish_preview(self.partial,self.preview,self.stop,expected_hash='0'*64)
        self.assertEqual(self.partial.read_bytes(),b'checked preview')
        self.assertEqual(self.preview.read_bytes(),b'foreign')


if __name__=='__main__':unittest.main()
