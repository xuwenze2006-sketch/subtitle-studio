"""Offline Azure response limits with real durable budget accounting."""

import http.client
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import wave

from subtitle_pipeline import azure_asr
from subtitle_pipeline.cloud_budget import BudgetLedger, CloudRequestError, SubmissionUnknown


class TrackedResponse(io.BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.read_sizes = []
        self.bytes_read = 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        data = super().read(size)
        self.bytes_read += len(data)
        return data


class OfflineSocket:
    def __init__(self, body):
        self.body = body

    def makefile(self, *args):
        return io.BytesIO(self.body)


class AzureResponseLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audio = self.root / "synthetic.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16001)
        self.ledger = BudgetLedger(self.root / "campaign" / "ledger.json")
        self.raw_copy = self.root / "project" / "raw.json"
        self.options = dict(endpoint="https://fixture.cognitiveservices.azure.com",
                            key="offline-fake-key", ledger=self.ledger,
                            request_id="bounded-azure", raw_path=self.raw_copy,
                            hourly_rate_cny=36.0)
        transport_guard = mock.patch.object(azure_asr.urllib.request, "build_opener",
            side_effect=AssertionError("Offline tests require an explicit fake Azure transport"))
        transport_guard.start()
        self.addCleanup(transport_guard.stop)

    def opener(self, response, status):
        opener = mock.Mock()
        if status == 200:
            opener.open.return_value = response
        else:
            opener.open.side_effect = urllib.error.HTTPError(
                self.options["endpoint"], status, "offline fixture", response.headers, response)
        return opener

    def assert_unknown_without_resubmission(self, response, status):
        opener = self.opener(response, status)
        with mock.patch.object(azure_asr.urllib.request, "build_opener", return_value=opener):
            outcome = "returned"
            try:
                azure_asr.transcribe(self.audio, **self.options)
            except CloudRequestError as error:
                outcome = type(error).__name__
            self.assertEqual(outcome, "SubmissionUnknown")
            self.assertTrue(response.closed)
            summary = self.ledger.summary()
            record = summary["requests"][self.options["request_id"]]
            self.assertEqual(record["status"], "unknown")
            self.assertEqual(record["attempts"][0]["status"], "unknown")
            self.assertEqual(summary["spent_cny"], 0.0)
            self.assertEqual(summary["reserved_cny"], 0.02)
            self.assertFalse(self.raw_copy.exists())
            self.assertEqual(list((self.ledger.path.parent / "responses").rglob("*")), [])
            restarted = BudgetLedger(self.ledger.path)
            with self.assertRaises(SubmissionUnknown):
                azure_asr.transcribe(self.audio, **dict(self.options, ledger=restarted))
        self.assertEqual(opener.open.call_count, 1)

    def test_success_above_production_limit_is_unknown_without_saved_body(self):
        body = b'{"durationMilliseconds":1001,"phrases":[]}'
        response = TrackedResponse(body + b" " * (32 * 1024 * 1024 + 1 - len(body)))
        self.assert_unknown_without_resubmission(response, 200)
        self.assertTrue(all(0 <= size <= 32 * 1024 * 1024 + 1 for size in response.read_sizes))
        self.assertLessEqual(response.bytes_read, 32 * 1024 * 1024 + 1)

    def test_error_above_production_limit_is_unknown_without_saved_body(self):
        response = TrackedResponse(b"x" * (32 * 1024 * 1024 + 1))
        self.assert_unknown_without_resubmission(response, 400)
        self.assertTrue(all(0 <= size <= 32 * 1024 * 1024 + 1 for size in response.read_sizes))
        self.assertLessEqual(response.bytes_read, 32 * 1024 * 1024 + 1)

    def test_missing_invalid_and_underreported_lengths_cannot_bypass_body_bound(self):
        for status in (200, 429):
            for header in (None, "invalid", "-1", "1"):
                with self.subTest(status=status, content_length=header):
                    self.options["request_id"] = f"length-{status}-{header or 'absent'}"
                    self.ledger = BudgetLedger(self.root / self.options["request_id"] / "ledger.json")
                    self.options["ledger"] = self.ledger
                    body = b'{"durationMilliseconds":1001,"phrases":[]}'
                    headers = {} if header is None else {"Content-Length": header}
                    response = TrackedResponse(body + b" " * (65537 - len(body)), headers)
                    with mock.patch.object(azure_asr, "_MAX_RESPONSE_BYTES", 65536, create=True):
                        self.assert_unknown_without_resubmission(response, status)
                    self.assertLessEqual(response.bytes_read, 65537)
                    self.assertTrue(all(0 <= size <= 65537 for size in response.read_sizes))

    def test_exact_production_boundary_is_saved_settled_and_reused(self):
        prefix = b'{"durationMilliseconds":1001,"phrases":[]}'
        body = prefix + b" " * (32 * 1024 * 1024 - len(prefix))
        response = TrackedResponse(body, {"Content-Length": str(len(body))})
        opener = self.opener(response, 200)
        with mock.patch.object(azure_asr.urllib.request, "build_opener", return_value=opener):
            result = azure_asr.transcribe(self.audio, **self.options)
            repeated = azure_asr.transcribe(self.audio, **self.options)
        self.assertEqual(result.cues, [])
        self.assertEqual(repeated, result)
        self.assertTrue(response.closed)
        self.assertEqual(self.raw_copy.stat().st_size, 32 * 1024 * 1024)
        summary = self.ledger.summary()
        self.assertEqual(summary["spent_cny"], 0.02)
        self.assertEqual(summary["reserved_cny"], 0.0)
        self.assertEqual(summary["requests"]["bounded-azure"]["status"], "success")
        self.assertEqual(opener.open.call_count, 1)

    def test_short_reads_continue_until_eof_for_success_and_error(self):
        for status in (200, 400):
            for known_length in (False, True):
                with self.subTest(status=status, known_length=known_length):
                    self.options["request_id"] = f"short-{status}-{known_length}"
                    self.ledger = BudgetLedger(self.root / self.options["request_id"] / "ledger.json")
                    self.options["ledger"] = self.ledger
                    body = b'{"durationMilliseconds":1001,"phrases":[]}'
                    headers = {"Content-Length": str(len(body))} if known_length else {}
                    response = TrackedResponse(body, headers)
                    native_read = response.read
                    response.read = lambda size=-1: native_read(7 if size < 0 else min(7, size))
                    opener = self.opener(response, status)
                    with mock.patch.object(azure_asr.urllib.request, "build_opener", return_value=opener):
                        if status == 200:
                            outcome = "returned"
                            try:
                                result = azure_asr.transcribe(self.audio, **self.options)
                            except CloudRequestError as error:
                                outcome = type(error).__name__
                            self.assertEqual(outcome, "returned")
                            self.assertEqual(result.cues, [])
                        else:
                            with self.assertRaises(CloudRequestError) as raised:
                                azure_asr.transcribe(self.audio, **self.options)
                            self.assertNotIsInstance(raised.exception, SubmissionUnknown)
                    record = self.ledger.summary()["requests"][self.options["request_id"]]
                    self.assertEqual(record["status"], "success" if status == 200 else "rejected")
                    self.assertTrue(response.closed)
                    self.assertEqual(response.bytes_read, len(body))
                    saved = record["raw_path"] if status == 200 else record["attempts"][0]["raw_path"]
                    self.assertEqual(Path(saved).read_bytes(), body)

    def test_truncated_native_content_length_stays_unknown_on_bounded_read(self):
        for status in (200, 400):
            with self.subTest(status=status):
                self.options["request_id"] = f"truncated-{status}"
                self.ledger = BudgetLedger(self.root / self.options["request_id"] / "ledger.json")
                self.options["ledger"] = self.ledger
                body = b'{"durationMilliseconds":1001,"phrases":[]}'
                wire = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + body
                response = http.client.HTTPResponse(OfflineSocket(wire))
                response.begin()
                # HTTPError wraps the same native response in the error branch.
                self.assert_unknown_without_resubmission(response, status)

    def test_read_failure_closes_response_and_retains_reservation(self):
        for status in (200, 400):
            with self.subTest(status=status):
                self.options["request_id"] = f"read-failure-{status}"
                self.ledger = BudgetLedger(self.root / self.options["request_id"] / "ledger.json")
                self.options["ledger"] = self.ledger
                response = TrackedResponse(b"unused")
                response.read = mock.Mock(side_effect=OSError("offline retrieval failure"))
                self.assert_unknown_without_resubmission(response, status)


if __name__ == "__main__":
    unittest.main()
