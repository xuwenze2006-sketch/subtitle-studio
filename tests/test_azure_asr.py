import importlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import wave

from subtitle_pipeline.subtitles import Cue


def phrase(text="テストです。", start=100, duration=900, **extra):
    return dict(text=text, offsetMilliseconds=start,
                durationMilliseconds=duration, confidence=0.9, **extra)


def response(phrases=None, duration=2000):
    return {"durationMilliseconds": duration,
            "phrases": [phrase()] if phrases is None else phrases}


class AzureParsingTests(unittest.TestCase):
    def setUp(self):
        try:
            self.azure = importlib.import_module("subtitle_pipeline.azure_asr")
        except ModuleNotFoundError as error:
            self.fail(f"Azure adapter unavailable: {error.name}")

    def test_phrase_timing_and_text_are_preserved(self):
        result = self.azure.parse_response(response(), 2000)
        self.assertEqual(result.cues, [Cue(100, 1000, "テストです。")])
        self.assertTrue(any(i["reason"] == "word_timing_unavailable" for i in result.issues))
        self.assertEqual(result.metadata["duration_ms"], 2000)

    def test_successful_silence_requires_reported_duration(self):
        self.assertEqual(self.azure.parse_response(response([]), 2000).cues, [])
        for payload in ({}, {"phrases": []}, {"durationMilliseconds": 2000},
                        {"error": {"message": "failure"}, "durationMilliseconds": 2000}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.azure.parse_response(payload, 2000)

    def test_combined_text_cannot_be_mistaken_for_silence_without_phrase_times(self):
        payload = response([])
        payload["combinedPhrases"] = [{"channel": 0, "text": "認識された文字。"}]
        with self.assertRaises(ValueError):
            self.azure.parse_response(payload, 2000)
        payload["combinedPhrases"] = [{"channel": 0, "text": ""}]
        self.assertEqual(self.azure.parse_response(payload, 2000).cues, [])

    def test_wrong_recording_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.azure.parse_response(response(duration=2999), 100000)
        self.azure.parse_response(response([], 101000), 100000)
        with self.assertRaises(ValueError):
            self.azure.parse_response(response([], 101001), 100000)
        self.azure.parse_response(response([], 301500), 300000)

    def test_malformed_phrase_ranges_fail_instead_of_clipping(self):
        for field, value in (("offsetMilliseconds", -1), ("offsetMilliseconds", float("nan")),
                             ("offsetMilliseconds", "100"), ("offsetMilliseconds", True),
                             ("durationMilliseconds", 0), ("durationMilliseconds", -1),
                             ("durationMilliseconds", float("inf")), ("durationMilliseconds", 1901),
                             ("durationMilliseconds", 1.25)):
            item = phrase()
            item[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.azure.parse_response(response([item]), 2000)

    def test_malformed_payload_and_confidence_fail(self):
        bad = [None, [], {"durationMilliseconds": float("nan")},
               {"durationMilliseconds": 2000, "phrases": {}}, response([None]),
               response([dict(phrase(), text="")]), response([dict(phrase(), confidence=2)]),
               response([dict(phrase(), confidence=float("nan"))])]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.azure.parse_response(payload, 2000)

    def test_low_confidence_keeps_every_character(self):
        item = dict(phrase("そのまま残す。"), confidence=0.64)
        result = self.azure.parse_response(response([item]), 2000)
        self.assertEqual(result.cues[0].text, "そのまま残す。")
        self.assertTrue(any(i["reason"] == "low_confidence" for i in result.issues))

    def test_every_review_issue_has_the_corresponding_phrase_time(self):
        fixtures = [
            dict(phrase("甲", start=100, duration=400), confidence=0.4),
            phrase("乙。", start=700, duration=600, words=[
                {"word": "乙", "offsetMilliseconds": 700, "durationMilliseconds": 600, "confidence": 0.4}]),
            phrase("丙", start=1500, duration=300, words=[{"word": "丙"}]),
        ]
        result = self.azure.parse_response(response(fixtures), 2000)
        self.assertEqual({issue["reason"] for issue in result.issues},
                         {"low_confidence", "word_timing_unavailable", "word_text_mismatch"})
        expected = {0: (100, 500), 1: (700, 1300), 2: (1500, 1800)}
        for issue in result.issues:
            with self.subTest(issue=issue):
                self.assertIn("start_ms", issue)
                self.assertIn("end_ms", issue)
                self.assertEqual((issue["start_ms"], issue["end_ms"]), expected[issue["phrase_index"]])
                self.assertIsInstance(issue["reason"], str)

    def test_verified_words_split_at_punctuation_without_losing_spaces(self):
        item = phrase("こんにちは。 次です！", start=100, duration=1700, words=[
            {"word": "こんにちは。", "offsetMilliseconds": 100, "durationMilliseconds": 700},
            {"text": "次です！", "offsetMilliseconds": 900, "durationMilliseconds": 900}])
        result = self.azure.parse_response(response([item]), 2000)
        self.assertEqual(result.cues, [Cue(100, 800, "こんにちは。"), Cue(900, 1800, " 次です！")])
        self.assertEqual("".join(c.text for c in result.cues), item["text"])

    def test_character_and_duration_limits_use_only_real_word_boundaries(self):
        item = phrase("あ" * 20 + "い" * 20 + "う", start=0, duration=8000, words=[
            {"word": "あ" * 20, "offsetMilliseconds": 0, "durationMilliseconds": 1000},
            {"word": "い" * 20, "offsetMilliseconds": 1000, "durationMilliseconds": 1000},
            {"word": "う", "offsetMilliseconds": 7000, "durationMilliseconds": 1000}])
        result = self.azure.parse_response(response([item], 8000), 8000)
        self.assertEqual(result.cues, [Cue(0, 1000, "あ" * 20), Cue(1000, 2000, "い" * 20), Cue(7000, 8000, "う")])

    def test_long_japanese_sentence_splits_at_comma_before_breaking_a_clause(self):
        # Token text/times from the public DiMAPS benchmark, not ASR mocks.
        rows = [("航空", 77760, 78200), ("写真", 78200, 78560),
                ("の", 78560, 78720), ("透過", 78720, 79120),
                ("率", 79120, 79320), ("を変える", 79320, 79800),
                ("ことができ、", 79800, 80920), ("地図", 80920, 81240),
                ("と比較", 81240, 81720), ("して", 81720, 82280),
                ("家", 82280, 82440), ("屋", 82440, 82680),
                ("の", 82680, 82800), ("被災", 82800, 83200),
                ("状況を", 83200, 83720), ("把握", 83720, 84080),
                ("できます。", 84080, 85240)]
        text = "航空写真の透過率を変えることができ、 地図と比較して家屋の被災状況を把握できます。"
        item = phrase(text, start=77760, duration=7480, words=[
            {"word": word, "offsetMilliseconds": start, "durationMilliseconds": end - start}
            for word, start, end in rows])
        result = self.azure.parse_response(response([item], 86000), 86000)
        self.assertEqual(result.cues, [
            Cue(77760, 80920, "航空写真の透過率を変えることができ、"),
            Cue(80920, 85240, " 地図と比較して家屋の被災状況を把握できます。")])
        self.assertEqual("".join(c.text for c in result.cues), text)

    def test_duration_split_rebalances_instead_of_leaving_a_short_predicate(self):
        rows = [("他にも、", 103440, 104400), ("色々な", 104400, 104920),
                ("情報の", 104920, 105960), ("重ね", 105960, 106240),
                ("合わせ", 106240, 106640), ("や", 106640, 107000),
                ("リアル", 107000, 107360), ("タイム", 107360, 107640),
                ("の", 107640, 107760), ("入手", 107760, 108120),
                ("が可能です。", 108120, 109480)]
        text = "他にも、色々な情報の重ね合わせやリアルタイムの入手が可能です。"
        item = phrase(text, start=103440, duration=6040, words=[
            {"word": word, "offsetMilliseconds": start, "durationMilliseconds": end - start}
            for word, start, end in rows])
        result = self.azure.parse_response(response([item], 110000), 110000)
        self.assertEqual(result.cues, [
            Cue(103440, 107000, "他にも、色々な情報の重ね合わせや"),
            Cue(107000, 109480, "リアルタイムの入手が可能です。")])
        self.assertEqual("".join(c.text for c in result.cues), text)

    def test_oversized_single_word_is_never_split_to_invent_a_timestamp(self):
        text = "長" * 40 + "。"
        item = phrase(text, start=100, duration=7000, words=[
            {"word": text, "offsetMilliseconds": 100, "durationMilliseconds": 7000}])
        result = self.azure.parse_response(response([item], 7200), 7200)
        self.assertEqual(result.cues, [Cue(100, 7100, text)])

    def test_missing_word_timing_falls_back_to_complete_phrase(self):
        item = phrase("こんにちは。", words=[{"word": "こんにちは。"}])
        result = self.azure.parse_response(response([item]), 2000)
        self.assertEqual(result.cues, [Cue(100, 1000, "こんにちは。")])
        self.assertTrue(any(i["reason"] == "word_timing_unavailable" for i in result.issues))

    def test_word_mismatch_does_not_reconstruct_or_remove_punctuation(self):
        item = phrase("こんにちは。", words=[{"word": "こんにちは", "offsetMilliseconds": 100, "durationMilliseconds": 900}])
        result = self.azure.parse_response(response([item]), 2000)
        self.assertEqual(result.cues, [Cue(100, 1000, "こんにちは。")])
        self.assertTrue(any(i["reason"] == "word_text_mismatch" for i in result.issues))

    def test_invalid_word_timing_fails_even_when_other_word_timing_is_missing(self):
        for start, duration in ((-1, 100), (900, 101), (float("nan"), 100), (100, -1)):
            item = phrase("甲乙", words=[{"word": "甲"}, {"word": "乙", "offsetMilliseconds": start, "durationMilliseconds": duration}])
            with self.subTest(start=start, duration=duration), self.assertRaises(ValueError):
                self.azure.parse_response(response([item]), 2000)

    def test_reversed_words_and_phrases_are_rejected(self):
        item = phrase("甲乙", words=[
            {"word": "甲", "offsetMilliseconds": 800, "durationMilliseconds": 100},
            {"word": "乙", "offsetMilliseconds": 100, "durationMilliseconds": 100}])
        with self.assertRaises(ValueError):
            self.azure.parse_response(response([item]), 2000)
        with self.assertRaises(ValueError):
            self.azure.parse_response(response([phrase(start=1000, duration=100), phrase(start=100, duration=100)]), 2000)


class FakeLedger:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        result = kwargs["send"]()
        self.http_response = result
        if result.status != 200:
            from subtitle_pipeline.cloud_budget import CloudRequestError
            raise CloudRequestError("mock explicit rejection")
        kwargs["raw_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["raw_path"].write_bytes(result.body)
        return json.loads(result.body)


class HttpResult:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(response([], 1001)).encode("utf-8")


class AzureTransportTests(unittest.TestCase):
    def setUp(self):
        try:
            self.azure = importlib.import_module("subtitle_pipeline.azure_asr")
        except ModuleNotFoundError as error:
            self.fail(f"Azure adapter unavailable: {error.name}")
        transport_guard = mock.patch.object(self.azure.urllib.request, "build_opener",
            side_effect=AssertionError("Unexpected outbound transport: provide an explicit test response"))
        transport_guard.start()
        self.addCleanup(transport_guard.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio = Path(self.directory.name) / "音声.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16001)
        self.ledger = FakeLedger(Path(self.directory.name) / "campaign" / "ledger.json")
        self.kwargs = dict(endpoint="https://example.cognitiveservices.azure.com/", key="secret-key-value",
                           ledger=self.ledger, request_id="recording-chunk-1", raw_path=Path(self.directory.name) / "raw.json",
                           hourly_rate_cny=36.0)

    def test_multipart_matches_fast_api_and_budget_rounds_seconds_up(self):
        opener = mock.Mock()
        opener.open.return_value = HttpResult()
        with mock.patch.object(self.azure.urllib.request, "build_opener", return_value=opener):
            result = self.azure.transcribe(self.audio, **self.kwargs)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe?api-version=2025-10-15")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Ocp-apim-subscription-key"), "secret-key-value")
        self.assertIn(b'name="audio"; filename="audio.wav"', request.data)
        self.assertIn(b'name="definition"', request.data)
        self.assertIn(b'"locales": ["ja-JP"]', request.data)
        self.assertIn(b'"profanityFilterMode": "None"', request.data)
        self.assertNotIn(b"enhanced", request.data)
        self.assertNotIn(b"wordLevelTimestampsEnabled", request.data)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 300)
        self.assertEqual(self.ledger.calls[0]["reserved_cny"], 0.02)
        self.assertEqual(self.ledger.calls[0]["provider"], "azure_fast")
        self.assertEqual(result.cues, [])
        self.assertEqual(result.metadata["input_duration_ms"], 1001)
        self.assertNotIn("secret-key-value", repr(result))

    def test_untrusted_endpoints_fail_before_ledger_or_transport(self):
        invalid = ["http://example.cognitiveservices.azure.com", "https://example.com",
                   "https://example.cognitiveservices.azure.com.evil.com", "https://user:pw@example.cognitiveservices.azure.com",
                   "https://example.cognitiveservices.azure.com?x=1", "https://example.cognitiveservices.azure.com#x",
                   "https://example.cognitiveservices.azure.com/surprise", "https://example.cognitiveservices.azure.com:8443"]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.azure.transcribe(self.audio, **dict(self.kwargs, endpoint=endpoint))
        self.assertEqual(self.ledger.calls, [])

    def test_bad_rate_key_and_version_fail_before_ledger(self):
        for changes in ({"hourly_rate_cny": 0}, {"hourly_rate_cny": -1}, {"hourly_rate_cny": float("inf")},
                        {"hourly_rate_cny": float("nan")}, {"hourly_rate_cny": True}, {"key": ""},
                        {"key": "secret\r\ninjected"}, {"api_version": "2025-10-15&x=1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError) as raised:
                self.azure.transcribe(self.audio, **dict(self.kwargs, **changes))
            self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.ledger.calls, [])

    def test_wrong_wav_format_fails_before_ledger(self):
        with wave.open(str(self.audio), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0" * 16000)
        with self.assertRaises(ValueError):
            self.azure.transcribe(self.audio, **self.kwargs)
        self.assertEqual(self.ledger.calls, [])

    def test_network_failures_are_not_retried_inside_adapter(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("connection lost")
        with mock.patch.object(self.azure.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(urllib.error.URLError):
                self.azure.transcribe(self.audio, **self.kwargs)
        self.assertEqual(opener.open.call_count, 1)

    def test_http_errors_are_returned_to_ledger_as_responses(self):
        from subtitle_pipeline.cloud_budget import CloudRequestError
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.cognitiveservices.azure.com", 429, "limit", {"Retry-After": "2"}, io.BytesIO(b'{}'))
        with mock.patch.object(self.azure.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(CloudRequestError):
                self.azure.transcribe(self.audio, **self.kwargs)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(self.ledger.http_response.status, 429)
        self.assertEqual(self.ledger.http_response.headers["Retry-After"], "2")

    def test_stop_before_start_never_reserves_or_sends(self):
        from subtitle_pipeline.cloud_budget import CloudCancelled
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CloudCancelled):
            self.azure.transcribe(self.audio, **self.kwargs, stop_event=stop)
        self.assertEqual(self.ledger.calls, [])

    def test_redirect_handler_refuses_to_forward_credentials(self):
        captured = []
        def builder(*handlers):
            captured.extend(handlers)
            return mock.Mock(open=mock.Mock(return_value=HttpResult()))
        with mock.patch.object(self.azure.urllib.request, "build_opener", side_effect=builder):
            self.azure.transcribe(self.audio, **self.kwargs)
        redirect = next(handler for handler in captured if isinstance(handler, self.azure.urllib.request.HTTPRedirectHandler))
        self.assertIsNone(redirect.redirect_request(None, None, 302, "redirect", {}, "https://attacker.example"))

    def test_same_paid_response_is_shared_across_project_directories(self):
        from subtitle_pipeline.cloud_budget import BudgetLedger
        ledger = BudgetLedger(self.ledger.path)
        original = b' { "durationMilliseconds" : 1001, "phrases" : [] }\n'
        http = HttpResult()
        http.read = lambda: original
        opener = mock.Mock()
        opener.open.return_value = http
        first = Path(self.directory.name) / "project-one" / "raw.json"
        second = Path(self.directory.name) / "project-two" / "raw.json"
        with mock.patch.object(self.azure.urllib.request, "build_opener", return_value=opener):
            self.azure.transcribe(self.audio, **dict(self.kwargs, ledger=ledger, raw_path=first))
            self.azure.transcribe(self.audio, **dict(self.kwargs, ledger=ledger, raw_path=second))
        self.assertEqual(opener.open.call_count, 1)
        canonical = ledger.path.parent / "responses" / "azure" / "recording-chunk-1.json"
        for path in (first, second, canonical):
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(ledger.summary()["spent_cny"], 0.02)

    def test_missing_phrase_timing_is_cached_without_paid_resubmission(self):
        from subtitle_pipeline.cloud_budget import BudgetLedger
        ledger = BudgetLedger(self.ledger.path)
        invalid = {"durationMilliseconds": 1001, "phrases": [],
                   "combinedPhrases": [{"channel": 0, "text": "Recognized text"}]}
        http = HttpResult()
        http.read = lambda: json.dumps(invalid).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = http
        with mock.patch.object(self.azure.urllib.request, "build_opener", return_value=opener):
            for project in ("first", "second"):
                target = Path(self.directory.name) / project / "raw.json"
                with self.assertRaises(ValueError):
                    self.azure.transcribe(self.audio, **dict(self.kwargs, ledger=ledger, raw_path=target))
                self.assertEqual(json.loads(target.read_bytes()), invalid)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(ledger.summary()["spent_cny"], 0.02)

    def test_optional_raw_path_uses_campaign_response_directory(self):
        opener = mock.Mock()
        opener.open.return_value = HttpResult()
        with mock.patch.object(self.azure.urllib.request, "build_opener", return_value=opener):
            self.azure.transcribe(self.audio, **dict(self.kwargs, raw_path=None))
        canonical = self.ledger.path.parent / "responses" / "azure" / "recording-chunk-1.json"
        self.assertEqual(self.ledger.calls[0]["raw_path"], canonical.resolve())
        self.assertTrue(canonical.is_file())

    def test_unsafe_request_filename_and_ledger_copy_target_fail_before_send(self):
        for changes in ({"request_id": "../escape"}, {"request_id": "CON"},
                        {"request_id": "recording:one"}, {"raw_path": self.ledger.path}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.azure.transcribe(self.audio, **dict(self.kwargs, **changes))
        self.assertEqual(self.ledger.calls, [])


if __name__ == "__main__":
    unittest.main()
