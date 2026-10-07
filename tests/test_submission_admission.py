"""Offline admission hooks must run before durable paid-request transitions."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import cloud_budget as cloud


class SubmissionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='submission-admission-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'budget.json'
        self.raw = self.root / 'one.json'
        self.ledger = cloud.BudgetLedger(self.path)
        self.stop = threading.Event()
        self.send = Mock(return_value=cloud.HttpResponse(200, {}, b'{"ok":true}'))
        for target in ('socket.create_connection', 'socket.socket.connect'):
            guard = patch(target, side_effect=AssertionError('offline only'))
            guard.start()
            self.addCleanup(guard.stop)

    def execute(self, **kwargs):
        options = dict(request_id='one', provider='fixture', reserved_cny=2,
                       raw_path=self.raw, send=self.send, stop_event=self.stop)
        options.update(kwargs)
        return self.ledger.execute(**options)

    def seed(self, status, *, exhausted=False):
        record = {'provider': 'fixture', 'reserved_cny': 2, 'raw_path': str(self.raw),
                  'status': status, 'attempts': [], 'created_at': 1.0}
        if status in ('received', 'success', 'submitting', 'unknown'):
            record['attempts'] = [{'number': 1, 'status': status, 'reserved_cny': 2}]
        if status in ('received', 'success'):
            self.raw.write_bytes(b'{"ok":true}')
            record['raw_sha256'] = hashlib.sha256(self.raw.read_bytes()).hexdigest()
            record['attempts'][0]['http_status'] = 200
        if status == 'success':
            record['actual_cny'] = 1
        if status == 'retry_wait':
            record.update(next_attempt_at=0, attempts=[{'number': 1, 'status': 'rejected',
                                                      'http_status': 429, 'actual_cny': 0}])
        if status == 'rejected':
            record['attempts'] = [{'number': 1, 'status': 'rejected', 'http_status': 403}]
        if exhausted:
            record['attempts'] = [{'number': i, 'status': 'cancelled_before_send'} for i in (1, 2, 3)]
        self.ledger._save({'version': 1, 'budget_cny': 20.0, 'stop_cny': 18.0,
                           'requests': {'one': record}})
        # Validate the fixture through the production reader before testing it.
        self.ledger._load()
        return record

    def test_fresh_refusal_propagates_exact_error_without_reservation_or_send(self):
        error = ValueError('input coverage rejected')
        hook = Mock(side_effect=error)
        with self.assertRaises(ValueError) as caught:
            self.execute(before_submit=hook)
        self.assertIs(caught.exception, error)
        hook.assert_called_once_with()
        self.send.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.raw.exists())

    def test_restored_refusal_preserves_every_existing_byte_and_reservation(self):
        for status in ('cancelled', 'reserved', 'retry_wait'):
            with self.subTest(status=status):
                self.seed(status)
                before = self.path.read_bytes()
                error = RuntimeError('do not transmit this input')
                with self.assertRaises(RuntimeError) as caught:
                    self.execute(before_submit=Mock(side_effect=error))
                self.assertIs(caught.exception, error)
                self.assertEqual(self.path.read_bytes(), before)
                self.send.assert_not_called()

    def test_received_and_success_resume_locally_without_hook_or_send(self):
        for status in ('received', 'success'):
            with self.subTest(status=status):
                self.seed(status)
                hook = Mock(side_effect=AssertionError('cached response entered admission'))
                self.assertEqual(self.execute(before_submit=hook), {'ok': True})
                hook.assert_not_called()
                self.send.assert_not_called()
                self.assertEqual(self.ledger._load()['requests']['one']['status'], 'success')

    def test_unknown_submitting_rejected_and_exhausted_never_call_hook(self):
        for status, exhausted, expected in (
                ('unknown', False, cloud.SubmissionUnknown),
                ('submitting', False, cloud.SubmissionUnknown),
                ('rejected', False, cloud.CloudRequestError),
                ('cancelled', True, cloud.CloudRequestError)):
            with self.subTest(status=status, exhausted=exhausted):
                self.seed(status, exhausted=exhausted)
                hook = Mock(side_effect=AssertionError('blocked request entered admission'))
                with self.assertRaises(expected):
                    self.execute(before_submit=hook)
                hook.assert_not_called()
                self.send.assert_not_called()

    def test_configuration_and_cached_body_checks_still_precede_admission(self):
        self.seed('reserved')
        hook = Mock(side_effect=AssertionError('invalid identity entered admission'))
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(provider='different', before_submit=hook)
        hook.assert_not_called()
        self.seed('success')
        self.raw.write_bytes(b'{"corrupt":true}')
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(before_submit=hook)
        hook.assert_not_called()
        self.send.assert_not_called()

    def test_exhausted_pending_records_are_invalid_before_admission(self):
        for status in ('reserved', 'retry_wait'):
            with self.subTest(status=status):
                self.seed(status)
                data = self.ledger._load()
                data['requests']['one'].update(
                    next_attempt_at=0,
                    attempts=[{'number': i, 'status': 'rejected', 'http_status': 429}
                              for i in (1, 2, 3)])
                self.ledger._save(data)
                before = self.path.read_bytes()
                hook = Mock(side_effect=AssertionError('exhausted request entered admission'))
                with self.assertRaisesRegex(cloud.CloudRequestError, 'ledger is invalid'):
                    self.execute(before_submit=hook)
                hook.assert_not_called()
                self.send.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_restored_retry_admission_runs_after_persisted_wait_without_mutation(self):
        for status in ('reserved', 'retry_wait', 'cancelled'):
            with self.subTest(status=status):
                self.seed('retry_wait')
                data = self.ledger._load()
                data['requests']['one'].update(status=status, next_attempt_at=110)
                self.ledger._save(data)
                before = self.path.read_bytes()
                order = []
                error = ValueError('input changed while waiting')
                def wait(delay, stop):
                    self.assertEqual(delay, 10)
                    order.append('wait')
                    return False
                def hook():
                    order.append('admission')
                    raise error
                with patch.object(cloud.time, 'time', return_value=100), patch.object(cloud, '_wait_retry', side_effect=wait):
                    with self.assertRaises(ValueError) as caught:
                        self.execute(before_submit=hook)
                self.assertIs(caught.exception, error)
                self.assertEqual(order, ['wait', 'admission'])
                self.assertEqual(self.path.read_bytes(), before)
                self.send.assert_not_called()

    def test_callback_cancellation_does_not_create_or_change_reservations(self):
        for status in (None, 'cancelled', 'reserved', 'retry_wait'):
            with self.subTest(status=status):
                self.stop.clear()
                if status is None:
                    before = None
                else:
                    self.seed(status)
                    before = self.path.read_bytes()
                hook = Mock(side_effect=self.stop.set)
                with self.assertRaises(cloud.CloudCancelled):
                    self.execute(before_submit=hook)
                hook.assert_called_once_with()
                self.send.assert_not_called()
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_already_cancelled_fresh_request_does_not_call_admission(self):
        self.stop.set()
        hook = Mock(side_effect=AssertionError('admission after cancellation'))
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(before_submit=hook)
        hook.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_each_retry_gets_admission_and_refusal_preserves_retry_wait(self):
        observed = []
        error = ValueError('source changed before retry')
        snapshot = []
        def hook():
            observed.append(1)
            if len(observed) == 2:
                snapshot.append(self.path.read_bytes())
                raise error
        self.send.return_value = cloud.HttpResponse(429, {'Retry-After': '0'}, b'{"rate_limited":true}')
        with self.assertRaises(ValueError) as caught:
            self.execute(before_submit=hook)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(observed), 2)
        self.send.assert_called_once_with()
        self.assertEqual(self.path.read_bytes(), snapshot[0])
        record = self.ledger._load()['requests']['one']
        self.assertEqual(record['status'], 'retry_wait')
        self.assertEqual(len(record['attempts']), 1)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 2)

    def test_allowed_retry_hooks_once_per_send_with_three_attempt_cap(self):
        hook = Mock()
        self.send.return_value = cloud.HttpResponse(429, {'Retry-After': '0'}, b'{"rate_limited":true}')
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(before_submit=hook)
        self.assertEqual(hook.call_count, 3)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)

    def test_hook_releases_shared_lock_but_keeps_exact_request_lock(self):
        observed = []
        def hook():
            with cloud._file_lock(self.ledger.lock_path, blocking=False) as shared:
                self.assertTrue(shared, 'admission holds the global budget lock')
            with cloud._file_lock(self.ledger._request_lock('one'), blocking=False) as exact:
                self.assertFalse(exact, 'admission released the exact request lock')
            observed.append(1)
        self.assertEqual(self.execute(before_submit=hook), {'ok': True})
        self.assertEqual(observed, [1])
        self.send.assert_called_once_with()

    def test_other_request_can_finish_during_hook_then_budget_is_reloaded(self):
        entered, release, other_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = {}
        first_send, second_send = Mock(return_value=cloud.HttpResponse(200, {}, b'{"ok":true}')), Mock(return_value=cloud.HttpResponse(200, {}, b'{"ok":true}'))
        def hook():
            entered.set()
            if not release.wait(4):
                raise AssertionError('test did not release admission')
        def first():
            try:
                self.execute(reserved_cny=10, send=first_send, before_submit=hook)
                outcomes['first'] = 'success'
            except Exception as error:
                outcomes['first'] = error
        def second():
            try:
                self.execute(request_id='two', raw_path=self.root/'two.json', reserved_cny=10, send=second_send)
                outcomes['second'] = 'success'
            except Exception as error:
                outcomes['second'] = error
            finally:
                other_done.set()
        one = threading.Thread(target=first)
        two = threading.Thread(target=second)
        one.start()
        try:
            self.assertTrue(entered.wait(2), outcomes)
            two.start()
            self.assertTrue(other_done.wait(2), 'unrelated request blocked behind admission')
        finally:
            release.set()
            one.join(4)
            if two.ident is not None:
                two.join(4)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertEqual(outcomes['second'], 'success')
        self.assertIsInstance(outcomes['first'], cloud.BudgetExceeded)
        first_send.assert_not_called()
        second_send.assert_called_once_with()
        data = self.ledger._load()
        self.assertEqual(set(data['requests']), {'two'}, 'stale admission overwrote another request')
        self.assertEqual(self.ledger.summary()['committed_cny'], 10)


if __name__ == '__main__':
    unittest.main()
