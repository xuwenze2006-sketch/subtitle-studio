"""Offline budget and paid-request recovery tests; never contact a provider."""

import hashlib
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch


try:
    cloud = importlib.import_module("subtitle_pipeline.cloud_budget")
except ModuleNotFoundError:
    cloud = None


class RecordingStop:
    def __init__(self, cancel_on_wait=False):
        self.delays = []
        self.cancel_on_wait = cancel_on_wait

    def is_set(self):
        return False

    def wait(self, delay):
        self.delays.append(delay)
        return self.cancel_on_wait


class CancelDuringRealWait:
    """Set a real Event only after the caller enters its retry wait."""
    def __init__(self):
        self.event = threading.Event()
        self.delays = []

    def is_set(self):
        return self.event.is_set()

    def wait(self, delay):
        self.delays.append(delay)
        timer = threading.Timer(0.02, self.event.set)
        timer.start()
        try:
            return self.event.wait(delay)
        finally:
            timer.cancel()
            timer.join()


def process_reserve(path, request_id, entered, release, results):
    from subtitle_pipeline.cloud_budget import BudgetLedger, HttpResponse

    def send():
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test coordination timeout")
        return HttpResponse(200, {}, b'{"ok":true}')

    try:
        ledger = BudgetLedger(Path(path))
        ledger.execute(request_id, "azure", 10, Path(path).parent / (request_id + ".json"), send)
        results.put("success")
    except Exception as error:
        results.put(type(error).__name__)


class CloudBudgetTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cloud, "cloud_budget safety core is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "budget.json"
        self.ledger = cloud.BudgetLedger(self.path)
        self.raw = self.directory / "raw.json"

    def response(self, status=200, data=None, headers=None, body=None):
        return cloud.HttpResponse(status, headers or {}, body if body is not None else json.dumps(data or {"ok": True}).encode())

    def execute(self, send=None, **kwargs):
        options = dict(request_id="one", provider="azure", reserved_cny=2, raw_path=self.raw,
                       send=send or (lambda: self.response()))
        options.update(kwargs)
        return self.ledger.execute(**options)

    def record(self, request_id="one"):
        return self.ledger.summary()["requests"][request_id]

    def must_not_send(self):
        self.fail("a previously submitted request was sent again")

    def test_success_is_persisted_and_same_id_reloads_without_send(self):
        payload = {"usage": {"total_tokens": 12}, "output": "hello"}
        result = self.execute(lambda: self.response(data=payload))
        self.assertEqual(payload, result)
        self.assertEqual(payload, json.loads(self.raw.read_bytes()))
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(), self.record()["raw_sha256"])
        self.ledger = cloud.BudgetLedger(self.path)
        self.assertEqual(payload, self.execute(self.must_not_send))
        self.assertEqual(1, len(self.record()["attempts"]))
        self.assertEqual(2, self.ledger.summary()["spent_cny"])

    def test_cached_raw_corruption_never_resubmits(self):
        self.execute()
        self.raw.write_text('{"tampered":true}', encoding="utf-8")
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)
        self.assertEqual(2, self.ledger.summary()["spent_cny"])

    def test_cached_raw_missing_never_resubmits(self):
        self.execute()
        self.raw.unlink()
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)

    def test_same_id_configuration_changes_are_refused(self):
        self.execute()
        for changes in ({"provider": "deepseek"}, {"reserved_cny": 3}, {"raw_path": self.directory / "other.json"}):
            with self.subTest(changes=changes), self.assertRaises(cloud.CloudRequestError):
                self.execute(self.must_not_send, **changes)
        with self.assertRaises(cloud.CloudRequestError):
            cloud.BudgetLedger(self.path, budget_cny=100, stop_cny=99).summary()

    def test_reservation_must_be_finite_nonnegative(self):
        for amount in (-1, float("nan"), float("inf"), True):
            with self.subTest(amount=amount), self.assertRaises((ValueError, cloud.CloudRequestError)):
                self.execute(self.must_not_send, reserved_cny=amount)

    def test_strict_stop_line_prevents_request_reaching_eighteen(self):
        self.execute(reserved_cny=17)
        with self.assertRaises(cloud.BudgetExceeded):
            self.execute(self.must_not_send, request_id="two", reserved_cny=1, raw_path=self.directory / "two.json")

    def test_zero_reservation_is_allowed_below_stop_line(self):
        self.execute(reserved_cny=0)
        self.assertEqual(0, self.ledger.summary()["spent_cny"])

    def test_usage_cost_settles_lower_than_reservation(self):
        self.execute(lambda: self.response(data={"usage": {"cost": 0.25}}), actual_cost=lambda body: body["usage"]["cost"])
        self.assertEqual(0.25, self.ledger.summary()["spent_cny"])
        self.assertEqual(0, self.ledger.summary()["reserved_cny"])

    def test_invalid_or_missing_usage_keeps_reservation_as_spent(self):
        callbacks = (lambda _: None, lambda _: float("nan"), lambda _: -1, lambda _: True, lambda _: 1 / 0)
        for index, callback in enumerate(callbacks):
            self.execute(request_id=str(index), raw_path=self.directory / (str(index) + ".json"), actual_cost=callback)
        self.assertEqual(10, self.ledger.summary()["spent_cny"])

    def test_actual_cost_above_reservation_is_recorded_and_blocks_more(self):
        self.execute(actual_cost=lambda _: 21)
        self.assertEqual(21, self.ledger.summary()["spent_cny"])
        with self.assertRaises(cloud.BudgetExceeded):
            self.execute(self.must_not_send, request_id="two", raw_path=self.directory / "two.json")

    def test_unknown_network_failure_holds_budget_and_never_resubmits(self):
        def send():
            raise TimeoutError("secret-key body and headers")
        with self.assertRaises(cloud.SubmissionUnknown) as caught:
            self.execute(send)
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertEqual(2, self.ledger.summary()["reserved_cny"])
        self.assertEqual("unknown", self.record()["status"])
        self.ledger = cloud.BudgetLedger(self.path)
        with self.assertRaises(cloud.SubmissionUnknown):
            self.execute(self.must_not_send)

    def test_server_error_is_unknown_without_retry(self):
        calls = []
        def send():
            calls.append(1)
            return self.response(status=500, body=b"secret-response")
        with self.assertRaises(cloud.SubmissionUnknown) as caught:
            self.execute(send)
        self.assertNotIn("secret-response", str(caught.exception))
        self.assertEqual(1, len(calls))
        self.assertEqual(2, self.ledger.summary()["reserved_cny"])

    def test_client_rejections_release_reservation_without_retry(self):
        for status in (400, 401, 403, 422):
            with self.subTest(status=status), self.assertRaises(cloud.CloudRequestError):
                self.execute(lambda: self.response(status=status), request_id=str(status), raw_path=self.directory / (str(status) + ".json"))
            self.assertEqual(1, len(self.record(str(status))["attempts"]))
        self.assertEqual(0, self.ledger.summary()["committed_cny"])

    def test_rate_limit_has_three_total_attempts_and_default_backoff(self):
        stop = RecordingStop()
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(lambda: self.response(status=429), stop_event=stop)
        self.assertEqual([2, 4], stop.delays)
        self.assertEqual(3, len(self.record()["attempts"]))
        self.assertEqual(0, self.ledger.summary()["reserved_cny"])
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)

    def test_rate_limit_retry_after_and_later_success(self):
        replies = iter((self.response(status=429, headers={"retry-after": "0.01"}), self.response(data={"done": True})))
        stop = RecordingStop()
        self.assertEqual({"done": True}, self.execute(lambda: next(replies), stop_event=stop))
        self.assertEqual([0.01], stop.delays)
        self.assertEqual([429, 200], [attempt["http_status"] for attempt in self.record()["attempts"]])

    def test_each_attempt_records_charge_and_saved_body_hash(self):
        replies = iter((self.response(status=429, headers={"Retry-After": "0"}), self.response()))
        self.execute(lambda: next(replies), actual_cost=lambda _: 0.5)
        attempts = self.record()["attempts"]
        self.assertEqual([0, 0.5], [attempt["actual_cny"] for attempt in attempts])
        self.assertEqual([2, 2], [attempt["reserved_cny"] for attempt in attempts])
        for attempt in attempts:
            self.assertEqual(hashlib.sha256(Path(attempt["raw_path"]).read_bytes()).hexdigest(), attempt["raw_sha256"])

    def test_cancellation_interrupts_backoff_and_releases_budget(self):
        stop = RecordingStop(cancel_on_wait=True)
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(lambda: self.response(status=429), stop_event=stop)
        self.assertEqual(1, len(self.record()["attempts"]))
        self.assertEqual(0, self.ledger.summary()["committed_cny"])

    def test_huge_retry_after_can_be_cancelled_without_overflow_or_resubmission(self):
        stop = CancelDuringRealWait()
        sent = []
        def send():
            sent.append(True)
            return self.response(status=429, headers={'Retry-After': '1e100'})
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(send, stop_event=stop)
        record = self.record()
        self.assertEqual(sent, [True])
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['next_attempt_at'], 1e100)
        self.assertEqual(len(record['attempts']), 1)
        self.assertEqual(record['attempts'][0]['http_status'], 429)
        self.assertTrue(Path(record['attempts'][0]['raw_path']).is_file())
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)
        self.assertTrue(stop.delays and all(0 <= delay <= 60 for delay in stop.delays))

    def test_restored_huge_retry_deadline_can_be_cancelled_without_new_send(self):
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(lambda: self.response(status=429), stop_event=RecordingStop(True))
        data = json.loads(self.path.read_bytes())
        data['requests']['one'].update(status='retry_wait', next_attempt_at=1e100)
        self.path.write_text(json.dumps(data), encoding='utf-8')
        sent = []
        def send():
            sent.append(True)
            return self.response()
        stop = CancelDuringRealWait()
        with self.assertRaises(cloud.CloudCancelled):
            cloud.BudgetLedger(self.path).execute('one', 'azure', 2, self.raw, send, stop_event=stop)
        record = self.record()
        self.assertEqual(sent, [])
        self.assertEqual(record['next_attempt_at'], 1e100)
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(len(record['attempts']), 1)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)

    def test_long_retry_wait_is_split_without_sending_before_full_delay(self):
        stop = RecordingStop()
        sent = []
        def send():
            sent.append(True)
            if len(sent) == 1:
                return self.response(status=429, headers={'Retry-After': '150'})
            self.assertGreaterEqual(sum(stop.delays), 150)
            return self.response(data={'done': True})
        self.assertEqual(self.execute(send, stop_event=stop), {'done': True})
        self.assertEqual(len(sent), 2)
        self.assertEqual(sum(stop.delays), 150)
        self.assertTrue(all(0 <= delay <= 60 for delay in stop.delays))

    def test_restored_long_deadline_waits_all_remaining_time_before_sending(self):
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(lambda: self.response(status=429), stop_event=RecordingStop(True))
        data = json.loads(self.path.read_bytes())
        data['requests']['one'].update(status='retry_wait', next_attempt_at=1150.0)
        self.path.write_text(json.dumps(data), encoding='utf-8')
        stop = RecordingStop()
        sent = []
        def send():
            self.assertGreaterEqual(sum(stop.delays), 150)
            sent.append(True)
            return self.response()
        # RecordingStop models completed waits without delaying the test.
        with patch.object(cloud.time, 'time', return_value=1000.0):
            self.execute(send, stop_event=stop)
        self.assertEqual(sent, [True])
        self.assertEqual(sum(stop.delays), 150)
        self.assertTrue(all(0 <= delay <= 60 for delay in stop.delays))
        self.assertEqual(len(self.record()['attempts']), 2)

    def test_long_retry_wait_without_event_uses_bounded_complete_sleeps(self):
        delays = []
        wait_retry = cloud._wait_retry
        def observe_retry(delay, stop_event):
            # time is shared by persistence and locking. Record only sleeps
            # within the actual retry waiter, not unrelated storage backoff.
            with patch.object(cloud.time, 'sleep', side_effect=delays.append):
                return wait_retry(delay, stop_event)
        replies = iter((self.response(status=429, headers={'Retry-After': '150'}), self.response()))
        def send():
            if delays:
                self.assertGreaterEqual(sum(delays), 150)
            return next(replies)
        with patch.object(cloud, '_wait_retry', side_effect=observe_retry):
            self.execute(send)
        self.assertEqual(sum(delays), 150)
        self.assertTrue(all(0 <= delay <= 60 for delay in delays))
        self.assertEqual(len(self.record()['attempts']), 2)

    def test_malformed_persisted_retry_deadlines_fail_closed_without_mutation(self):
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(lambda: self.response(status=429), stop_event=RecordingStop(True))
        original = json.loads(self.path.read_bytes())
        for deadline in (None, 'tomorrow', -1, True, {}, 10 ** 400, float('nan'), float('inf'), 'missing'):
            with self.subTest(deadline=repr(deadline)):
                data = json.loads(json.dumps(original))
                record = data['requests']['one']
                record['status'] = 'retry_wait'
                if deadline == 'missing':
                    record.pop('next_attempt_at', None)
                else:
                    record['next_attempt_at'] = deadline
                self.path.write_text(json.dumps(data), encoding='utf-8')
                before = self.path.read_bytes()
                sent = []
                def send():
                    sent.append(True)
                    return self.response()
                with self.assertRaises(cloud.CloudRequestError):
                    cloud.BudgetLedger(self.path).execute('one', 'azure', 2, self.raw, send)
                self.assertEqual(sent, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_cancelled_rate_limit_cannot_lose_deadline_and_resume_immediately(self):
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(lambda: self.response(status=429), stop_event=RecordingStop(True))
        data = json.loads(self.path.read_bytes())
        data['requests']['one'].pop('next_attempt_at')
        self.path.write_text(json.dumps(data), encoding='utf-8')
        before = self.path.read_bytes()
        sent = []
        def send():
            sent.append(True)
            return self.response()
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(send)
        self.assertEqual(sent, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_already_cancelled_request_never_sends(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(self.must_not_send, stop_event=stop)
        self.assertEqual(0, self.ledger.summary()["committed_cny"])

    def test_send_explicit_preflight_cancellation_releases_budget(self):
        def send():
            raise cloud.CloudCancelled("cancelled before upload")
        with self.assertRaises(cloud.CloudCancelled):
            self.execute(send)
        self.assertEqual(0, self.ledger.summary()["committed_cny"])

    def test_preflight_cancellation_hides_transport_exception_details(self):
        def send():
            raise cloud.CloudCancelled("private-key-and-headers")
        try:
            self.execute(send)
        except cloud.CloudCancelled:
            detail = traceback.format_exc()
        self.assertNotIn("private-key-and-headers", detail)

    def test_cancelled_rate_limit_resumes_with_only_remaining_attempts(self):
        for _ in range(2):
            with self.assertRaises(cloud.CloudCancelled):
                self.execute(lambda: self.response(status=429, headers={"Retry-After": "0"}), stop_event=RecordingStop(True))
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(lambda: self.response(status=429, headers={"Retry-After": "0"}))
        self.assertEqual(3, len(self.record()["attempts"]))
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)

    def test_cancelled_send_cannot_exceed_three_total_attempts(self):
        def send():
            raise cloud.CloudCancelled("before upload")
        for _ in range(3):
            with self.assertRaises(cloud.CloudCancelled):
                self.execute(send)
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)
        self.assertEqual(3, len(self.record()["attempts"]))
        self.assertEqual(0, self.ledger.summary()["committed_cny"])

    def test_recovered_retry_wait_cannot_submit_a_fourth_attempt(self):
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(lambda: self.response(status=429, headers={"Retry-After": "0"}))
        data = json.loads(self.path.read_bytes())
        data["requests"]["one"]["status"] = "retry_wait"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        sent = []
        def send():
            sent.append(True)
            return self.response()
        with self.assertRaises(cloud.CloudRequestError):
            cloud.BudgetLedger(self.path).execute("one", "azure", 2, self.raw, send)
        self.assertEqual([], sent)

    def test_impossible_attempt_count_or_status_fails_closed_on_load(self):
        self.execute()
        original = json.loads(self.path.read_bytes())
        for mutation in ("excess_attempts", "invalid_number", "invalid_status", "success_with_submitting_attempt"):
            data = json.loads(json.dumps(original))
            record = data["requests"]["one"]
            if mutation == "excess_attempts":
                record["attempts"] *= 4
            elif mutation == "invalid_number":
                record["attempts"][0]["number"] = 99
            elif mutation == "invalid_status":
                record["attempts"][0]["status"] = "unrecognized"
            else:
                record["attempts"][0]["status"] = "submitting"
            self.path.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(mutation=mutation), self.assertRaises(cloud.CloudRequestError):
                self.ledger.summary()

    def test_invalid_json_200_persists_body_and_holds_budget(self):
        body = b'{"secret-response":'
        with self.assertRaises(cloud.SubmissionUnknown):
            self.execute(lambda: self.response(body=body))
        self.assertEqual(body, self.raw.read_bytes())
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.record()["raw_sha256"])
        self.assertEqual(2, self.ledger.summary()["reserved_cny"])
        with self.assertRaises(cloud.SubmissionUnknown):
            self.execute(self.must_not_send)

    def test_non_object_json_200_is_not_treated_as_completed(self):
        with self.assertRaises(cloud.SubmissionUnknown):
            self.execute(lambda: self.response(body=b"[]"))
        self.assertEqual(b"[]", self.raw.read_bytes())

    def test_submitting_record_after_crash_becomes_unknown(self):
        class SimulatedCrash(BaseException):
            pass
        def send():
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.execute(send)
        self.ledger = cloud.BudgetLedger(self.path)
        with self.assertRaises(cloud.SubmissionUnknown):
            self.execute(self.must_not_send)
        self.assertEqual("unknown", self.record()["status"])
        self.assertEqual(2, self.ledger.summary()["reserved_cny"])

    def test_summary_marks_crashed_submission_unknown_without_retransmission(self):
        class SimulatedCrash(BaseException):
            pass
        def send():
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.execute(send)
        self.assertEqual("unknown", cloud.BudgetLedger(self.path).summary()["requests"]["one"]["status"])

    def test_success_without_recorded_cost_is_corrupt_and_fails_closed(self):
        self.execute()
        data = json.loads(self.path.read_bytes())
        del data["requests"]["one"]["actual_cny"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(cloud.CloudRequestError):
            self.ledger.summary()

    def test_completed_response_is_on_disk_before_cost_callback(self):
        observations = []
        def settle(body):
            raw = self.raw.read_bytes()
            record = json.loads(self.path.read_text(encoding="utf-8"))["requests"]["one"]
            observations.append((body, json.loads(raw), record["status"], record["raw_sha256"], hashlib.sha256(raw).hexdigest()))
            return 0.5
        self.execute(actual_cost=settle)
        self.assertEqual(1, len(observations))
        body, saved_body, status, recorded_hash, actual_hash = observations[0]
        self.assertEqual(body, saved_body)
        self.assertEqual("received", status)
        self.assertEqual(actual_hash, recorded_hash)
        self.assertEqual(0.5, self.ledger.summary()["spent_cny"])

    def test_crash_during_local_settlement_resumes_without_send(self):
        class SimulatedCrash(BaseException):
            pass
        def settle(_):
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.execute(actual_cost=settle)
        self.ledger = cloud.BudgetLedger(self.path)
        self.assertEqual({"ok": True}, self.execute(self.must_not_send, actual_cost=lambda _: 0.5))
        self.assertEqual(0.5, self.ledger.summary()["spent_cny"])

    @unittest.skipUnless(os.name == 'nt', 'Windows replacement retries required')
    def test_transient_ledger_sharing_conflicts_do_not_interrupt_or_repeat_paid_send(self):
        replace = cloud.os.replace
        failures, sends = {}, []
        def conflicting_replace(source, target):
            if Path(target) == self.path:
                status = json.loads(Path(source).read_bytes())['requests']['one']['status']
                count = failures.get(status, 0)
                if count < 2:
                    failures[status] = count + 1
                    error = PermissionError('temporary ledger reader')
                    error.winerror = 5
                    raise error
            return replace(source, target)
        def send():
            sends.append(1)
            return self.response()
        with patch.object(cloud.os, 'replace', side_effect=conflicting_replace), \
             patch.object(cloud.time, 'sleep'):
            try:
                result = self.execute(send, actual_cost=lambda _: .5)
            except cloud.CloudRequestError as error:
                self.fail(f'a temporary ledger reader interrupted the request: {error}')
        self.assertEqual(result, {'ok': True})
        self.assertEqual(failures, dict.fromkeys(('reserved', 'submitting', 'received', 'success'), 2))
        self.assertEqual(sends, [1])
        self.assertEqual(self.execute(self.must_not_send), result)
        self.assertEqual(self.ledger.summary()['spent_cny'], .5)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)
        self.assertFalse(list(self.directory.glob('*.tmp')))

    @unittest.skipUnless(os.name == 'nt', 'Windows replacement retries required')
    def test_persistent_received_save_failure_keeps_paid_request_unknown_without_resending(self):
        replace = cloud.os.replace
        blocked, sends = [], []
        error = PermissionError('persistent ledger reader')
        error.winerror = 5
        def conflicting_replace(source, target):
            if (Path(target) == self.path and
                    json.loads(Path(source).read_bytes())['requests']['one']['status'] == 'received'):
                blocked.append(Path(source))
                raise error
            return replace(source, target)
        def send():
            sends.append(1)
            return self.response()
        with patch.object(cloud.os, 'replace', side_effect=conflicting_replace), \
             patch.object(cloud.time, 'sleep'):
            with self.assertRaises(cloud.CloudRequestError):
                self.execute(send)
        self.assertEqual(len(blocked), 6)
        self.assertEqual(len(set(blocked)), 1)
        self.assertEqual(json.loads(self.path.read_bytes())['requests']['one']['status'], 'submitting')
        self.assertEqual(json.loads(self.raw.read_bytes()), {'ok': True})
        self.ledger = cloud.BudgetLedger(self.path)
        with self.assertRaises(cloud.SubmissionUnknown):
            self.execute(self.must_not_send)
        self.assertEqual(sends, [1])
        self.assertEqual(self.record()['status'], 'unknown')
        self.assertEqual(self.ledger.summary()['reserved_cny'], 2)
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_unrelated_requests_can_send_concurrently_without_global_network_lock(self):
        barrier = threading.Barrier(2)
        failures = []
        def work(index):
            def send():
                barrier.wait(3)
                return self.response()
            try:
                cloud.BudgetLedger(self.path).execute(str(index), "azure", 2, self.directory / (str(index) + ".json"), send)
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(4, self.ledger.summary()["spent_cny"])

    def test_same_id_inflight_cannot_send_twice(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        def send():
            entered.set()
            release.wait(5)
            return self.response()
        def work():
            try:
                self.execute(send)
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(cloud.SubmissionUnknown):
                cloud.BudgetLedger(self.path).execute("one", "azure", 2, self.raw, self.must_not_send)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual([], failures)
        self.assertEqual(1, len(self.record()["attempts"]))

    def test_atomic_cross_process_reservations_cannot_overbook(self):
        context = multiprocessing.get_context("spawn")
        entered, release, results = context.Event(), context.Event(), context.Queue()
        first = context.Process(target=process_reserve, args=(str(self.path), "first", entered, release, results))
        second = context.Process(target=process_reserve, args=(str(self.path), "second", context.Event(), release, results))
        first.start()
        try:
            self.assertTrue(entered.wait(8))
            second.start()
            self.assertEqual("BudgetExceeded", results.get(timeout=8))
            self.assertEqual(10, self.ledger.summary()["reserved_cny"])
        finally:
            release.set()
            first.join(8)
            if second.pid:
                second.join(8)
            for process in (first, second):
                if process.pid and process.is_alive():
                    process.terminate()
                    process.join()
        self.assertEqual("success", results.get(timeout=2))
        self.assertEqual(10, self.ledger.summary()["spent_cny"])

    def test_cross_process_same_id_is_not_transmitted_twice(self):
        context = multiprocessing.get_context("spawn")
        entered, release, results = context.Event(), context.Event(), context.Queue()
        first = context.Process(target=process_reserve, args=(str(self.path), "same", entered, release, results))
        second = context.Process(target=process_reserve, args=(str(self.path), "same", context.Event(), release, results))
        first.start()
        try:
            self.assertTrue(entered.wait(8))
            second.start()
            self.assertEqual("SubmissionUnknown", results.get(timeout=8))
        finally:
            release.set()
            first.join(8)
            if second.pid:
                second.join(8)
            for process in (first, second):
                if process.pid and process.is_alive():
                    process.terminate()
                    process.join()
        self.assertEqual("success", results.get(timeout=2))
        self.assertEqual(1, len(self.record("same")["attempts"]))

    def test_raw_paths_cannot_be_reused_for_another_request(self):
        self.execute()
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send, request_id="two")

    @unittest.skipUnless(os.name == "nt", "Windows paths are case insensitive")
    def test_case_aliased_raw_path_cannot_start_a_second_paid_request(self):
        entered, release = threading.Event(), threading.Event()
        sent, failures = [], []

        def first_send():
            sent.append("first")
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test coordination timeout")
            return self.response()

        def work():
            try:
                self.execute(first_send, request_id="first", raw_path=self.directory / "RAW.json")
            except Exception as error:
                failures.append(error)

        def second_send():
            sent.append("second")
            return self.response()

        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(cloud.CloudRequestError):
                cloud.BudgetLedger(self.path).execute("second", "azure", 2, self.raw, second_send)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["first"], sent)
        self.assertEqual([], failures)
        self.assertEqual(2, self.ledger.summary()["spent_cny"])
        self.assertEqual(0, self.ledger.summary()["reserved_cny"])

    @unittest.skipUnless(os.name == "nt", "Windows paths are case insensitive")
    def test_cancelled_request_can_resume_with_case_aliased_raw_path(self):
        def cancelled_send():
            raise cloud.CloudCancelled("before upload")

        with self.assertRaises(cloud.CloudCancelled):
            self.execute(cancelled_send, raw_path=self.directory / "RAW.json")
        try:
            result = self.execute(raw_path=self.raw)
        except cloud.CloudRequestError as error:
            self.fail(f"The same Windows response path must remain resumable: {error}")
        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(self.record()["attempts"]))
        self.assertEqual(2, self.ledger.summary()["spent_cny"])

    def test_existing_untracked_raw_file_is_not_overwritten(self):
        self.raw.write_bytes(b"keep me")
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)
        self.assertEqual(b"keep me", self.raw.read_bytes())

    def test_corrupt_ledger_fails_closed(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(self.must_not_send)

    def test_duplicate_ledger_members_cannot_hide_previously_spent_budget(self):
        self.execute()
        corrupted=self.path.read_text(encoding='utf-8').rstrip()[:-1]+',"requests":{}}'
        self.path.write_text(corrupted,encoding='utf-8')
        before=self.path.read_bytes()
        sent=[]
        with self.assertRaises(cloud.CloudRequestError):
            self.execute(lambda:sent.append('second') or self.response(),request_id='second',reserved_cny=17,
                         raw_path=self.directory/'second.json')
        self.assertEqual(sent,[])
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.directory/'second.json').exists())

    def test_ambiguous_success_json_is_preserved_without_settlement_or_resubmission(self):
        for body in (b'{"usage":{"tokens":8},"usage":{"tokens":0}}',
                     b'{"usage":{"tokens":8,"tokens":0}}'):
            with self.subTest(body=body):
                index=str(len(list(self.directory.glob('ambiguous*.json'))))
                raw=self.directory/('ambiguous'+index+'.json')
                settled=[]
                def cannot_settle(_):
                    settled.append(True)
                    return 0
                with self.assertRaises(cloud.SubmissionUnknown):
                    self.execute(lambda:self.response(body=body),request_id=index,
                                 raw_path=raw,actual_cost=cannot_settle)
                self.assertEqual(settled,[])
                self.assertEqual(raw.read_bytes(),body)
                record=self.record(index)
                self.assertEqual(record['status'],'unknown')
                self.assertNotIn('actual_cny',record)
                with self.assertRaises(cloud.SubmissionUnknown):
                    self.execute(self.must_not_send,request_id=index,raw_path=raw)


if __name__ == "__main__":
    unittest.main()
