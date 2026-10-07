"""A one-cue acknowledgement must retain the strict review write contract."""
import unittest
from unittest.mock import patch

from tests import test_studio_review as fixtures


class StudioReviewReceiptTests(unittest.TestCase):
    setUp = fixtures.StudioReviewTests.setUp
    write = fixtures.StudioReviewTests.write
    request = fixtures.StudioReviewTests.request

    def test_compact_save_returns_only_the_saved_cue_without_full_preview(self):
        request = self.request(response_mode='cue', target_text='新的译文', review_status='checked')
        with patch.object(self.app, '_preview_data', side_effect=AssertionError('full preview rebuilt')):
            result = self.app.review_cue(request)
        self.assertEqual(result['kind'], 'review-cue')
        self.assertEqual(result['project_id'], request['project_id'])
        self.assertEqual(result['selected_id'], 'main')
        self.assertEqual(result['base_revision'], request['expected_revision'])
        self.assertNotIn('cues', result)
        fresh = self.app.preview('main')
        self.assertEqual(result['cue'], fresh['cues'][0])
        self.assertEqual(result['manual_review'], fresh['manual_review'])
        self.assertNotEqual(result['manual_review']['revision'], request['expected_revision'])
        self.assertIsNone(result['exported_video'])
        self.assertIsNone(result['draft_video'])

    def test_compact_receipt_still_rejects_a_stale_revision_without_overwriting(self):
        request = self.request(response_mode='cue', target_text='first')
        result = self.app.review_cue(request)
        with self.assertRaisesRegex(ValueError, '版本'):
            self.app.review_cue({**request, 'target_text': 'stale'})
        self.assertEqual(self.app.preview('main')['manual_review']['revision'], result['manual_review']['revision'])
        self.assertEqual(self.app.preview('main')['cues'][0]['target_text'], 'first')

    def test_unknown_response_mode_is_rejected_before_writing(self):
        request = self.request(response_mode=True, target_text='not saved')
        with self.assertRaisesRegex(ValueError, '返回方式'):
            self.app.review_cue(request)
        self.assertEqual(self.app.preview('main')['manual_review']['revision'], request['expected_revision'])
