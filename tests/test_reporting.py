"""End-of-run reports and final SDK HTTP payload capture, entirely offline."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx2
from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient, TypeSafeError

from src.reporting import RunReport
from src.selection import PINNED_MODEL


def read_events(path):
    return json.loads(path.read_text(encoding="utf-8"))


def response_body():
    return {
        "model": PINNED_MODEL,
        "answers": {
            "windows_r1_g0": {
                "type": "choice", "choice": "window_1", "confidence": 0.75,
                "probabilities": {"window_0": 0.25, "window_1": 0.75},
            },
        },
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


class FailingStream(httpx2.SyncByteStream):
    def __init__(self, error):
        self.error = error
        self.closed = False

    def __iter__(self):
        yield b'{"partial":'
        raise self.error

    def close(self):
        self.closed = True


class ReportTests(unittest.TestCase):
    def setUp(self):
        # Inherit workspace ACLs; Windows tempfile mode 0o700 excludes sandbox tokens.
        self.workspace = Path(__file__).resolve().parents[1]
        self.path = self.workspace / f".report-tests-{uuid4().hex}"
        self.path.mkdir()
        self.addCleanup(self.cleanup_directory)

    def cleanup_directory(self):
        target = self.path.resolve()
        if target.parent != self.workspace or not target.name.startswith(".report-tests-"):
            raise ValueError("Unexpected test cleanup path")
        shutil.rmtree(target)

    def test_events_stay_in_memory_until_one_final_write(self):
        with RunReport(self.path / "nested") as report:
            self.assertTrue(report.path.is_absolute())
            self.assertEqual(report.path.suffix, ".json")
            self.assertFalse(report.path.exists())
            with patch("src.reporting.os.fsync", wraps=os.fsync) as sync, patch(
                "src.reporting.json.dumps", wraps=json.dumps,
            ) as encode, patch("src.reporting.json.loads", wraps=json.loads) as decode:
                report.append("windows_observed", windows=[{"title": "résumé\nsecond line"}])
                report.append("completed", status="selected")
                self.assertFalse(report.path.exists())
                sync.assert_not_called()
                encode.assert_not_called()
                decode.assert_not_called()
                timing = report.close()
                self.assertEqual(timing["report_write_count"], 1)
                sync.assert_called_once()
                encode.assert_called_once()
                decode.assert_not_called()
                self.assertEqual(report.close(), timing)
                sync.assert_called_once()
                first = read_events(report.path)
                self.assertIn('\n  {\n    "timestamp":', report.path.read_text(encoding="utf-8"))
                self.assertEqual(len(first), 2)
                self.assertEqual(first[0]["windows"], [{"title": "résumé\nsecond line"}])
                self.assertEqual(first[0]["event"], "windows_observed")
                self.assertEqual(datetime.fromisoformat(first[0]["timestamp"]).utcoffset(), timedelta(0))
                self.assertTrue(first[0]["timestamp"].endswith("Z"))

    def test_failed_final_write_leaves_no_partial_report_and_is_not_retried(self):
        for operation in ("os.fsync", "os.replace"):
            with self.subTest(operation=operation), RunReport(self.path) as report:
                report.append("completed")
                with patch(f"src.reporting.{operation}", side_effect=OSError("disk failure")) as failing:
                    with self.assertRaises(OSError):
                        report.close()
                    report.close()
                    failing.assert_called_once()
                self.assertFalse(report.path.exists())
                self.assertEqual(list(self.path.glob("*.tmp")), [])

    def test_later_mutations_do_not_change_saved_events(self):
        windows = [{"title": "original"}]
        with RunReport(self.path) as report:
            report.append("windows_observed", windows=windows)
            windows[0]["title"] = "changed"
            report.append("completed")
            report.close()
            self.assertEqual(read_events(report.path)[0]["windows"], [{"title": "original"}])

    def test_separate_runs_have_unique_paths_and_restrictive_posix_mode(self):
        with RunReport(self.path) as first, RunReport(self.path) as second:
            self.assertNotEqual(first.path, second.path)
            self.assertFalse(first.path.exists())
            self.assertFalse(second.path.exists())
            first.close()
            second.close()
            self.assertTrue(first.path.exists())
            self.assertTrue(second.path.exists())
            if os.name == "posix":
                self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)

    def test_context_failure_retains_completed_prefix_and_closes_file(self):
        with self.assertRaisesRegex(RuntimeError, "later failure"):
            with RunReport(self.path) as report:
                report.append("windows_observed", windows=[])
                raise RuntimeError("later failure")
        self.assertEqual([event["event"] for event in read_events(report.path)], ["windows_observed"])
        with self.assertRaises(ValueError):
            report.append("too_late")
        report.close()

    def sdk_call(self, report, handler):
        http_client = httpx2.Client(
            transport=httpx2.MockTransport(handler),
            event_hooks={"request": [report.on_request], "response": [report.on_response]},
        )
        with TypeSafeClient(
            api_key="secret-api-key", model=PINNED_MODEL, retry=RetryPolicy(max_retries=0),
            http_client=http_client, base_url="https://api.typesafe.ai", timeout=30.0,
        ) as client:
            return client.system_one(
                state="  Find résumé’s Save.\r\nKeep the wording.  ",
                questions={
                    "windows_r1_g0": Choice(
                        instructions="Choose the best window.",
                        criteria={
                            "window_0": {"title": "first", "is_on_screen": False},
                            "window_1": {"title": "second", "bounds": {"x": -10, "y": 0}},
                        },
                    ),
                },
            )

    def test_sdk_hooks_capture_actual_serialized_request_and_complete_response(self):
        wire_requests = []
        expected_response = response_body()

        def handler(request):
            wire_requests.append(request.content)
            # Transport must never wait for a report write.
            self.assertFalse(report.path.exists())
            self.assertEqual(request.headers["authorization"], "Bearer secret-api-key")
            return httpx2.Response(200, json=expected_response, headers={"set-cookie": "secret-cookie"})

        with RunReport(self.path) as report:
            result = self.sdk_call(report, handler)
            report.close()
            events = read_events(report.path)
            self.assertEqual([event["event"] for event in events], ["jev_request", "jev_response"])
            request_event, response_event = events
            self.assertEqual(request_event["body"], json.loads(wire_requests[0]))
            self.assertEqual(request_event["body_bytes"], len(wire_requests[0]))
            self.assertEqual(request_event["body"]["state"], "  Find résumé’s Save.\r\nKeep the wording.  ")
            self.assertEqual(response_event["body"], expected_response)
            self.assertEqual(response_event["status_code"], 200)
            self.assertEqual(response_event["request_id"], request_event["request_id"])
            self.assertEqual(result.answers["windows_r1_g0"].probabilities, {"window_0": 0.25, "window_1": 0.75})
            self.assertNotIn("body_text", request_event)
            self.assertNotIn("body_text", response_event)
            text = report.path.read_text(encoding="utf-8")
            for secret in ("secret-api-key", "authorization", "secret-cookie", "set-cookie"):
                self.assertNotIn(secret, text)

    def test_network_failure_preserves_request_before_sdk_raises(self):
        def handler(request):
            raise httpx2.ConnectError("secret transport diagnostic", request=request)

        with RunReport(self.path) as report:
            with self.assertRaises(TypeSafeError):
                self.sdk_call(report, handler)
            report.close()
            events = read_events(report.path)
            self.assertEqual([event["event"] for event in events], ["jev_request"])
            self.assertNotIn("secret transport diagnostic", report.path.read_text(encoding="utf-8"))

    def test_refusal_non_json_and_invalid_model_responses_are_recorded_before_sdk_rejection(self):
        cases = [
            (403, b'{"error":"refused","code":"access_denied"}', {"error": "refused", "code": "access_denied"}),
            (503, b"temporarily unavailable", None),
            (200, b'{"model":"jev-1.13.0","answers":{},"usage":"invalid"}',
             {"model": "jev-1.13.0", "answers": {}, "usage": "invalid"}),
        ]
        for status, content, parsed in cases:
            with self.subTest(status=status, content=content), RunReport(self.path) as report:
                with self.assertRaises(TypeSafeError):
                    self.sdk_call(report, lambda request: httpx2.Response(status, content=content))
                report.close()
                event = read_events(report.path)[-1]
                self.assertEqual(event["event"], "jev_response")
                self.assertEqual(event["status_code"], status)
                if parsed is None:
                    self.assertEqual(event["body_text"], content.decode("utf-8"))
                    self.assertNotIn("body", event)
                else:
                    self.assertEqual(event["body"], parsed)
                    self.assertNotIn("body_text", event)

    def test_correlated_request_ids_and_url_secrets_are_not_recorded(self):
        sent = []

        def handler(request):
            sent.append(request.content)
            return httpx2.Response(200, json={"ok": True})

        with RunReport(self.path) as report, httpx2.Client(
            transport=httpx2.MockTransport(handler),
            event_hooks={"request": [report.on_request], "response": [report.on_response]},
        ) as client:
            for index in range(2):
                client.post(
                    "https://url-user:url-password@example.com/path?token=secret-query#secret-fragment",
                    content=json.dumps({"index": index}).encode("utf-8"),
                    headers={"authorization": "Bearer secret-header"},
                )
            report.close()
            events = read_events(report.path)
            self.assertEqual([event["request_id"] for event in events], [1, 1, 2, 2])
            self.assertEqual([json.loads(body) for body in sent], [{"index": 0}, {"index": 1}])
            for event in events[::2]:
                self.assertEqual(event["url"], "https://example.com/path")
            text = report.path.read_text(encoding="utf-8")
            for secret in ("url-user", "url-password", "secret-query", "secret-fragment", "secret-header"):
                self.assertNotIn(secret, text)

    def test_nonstandard_or_nonfinite_response_numbers_are_retained_as_text(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e999"):
            content = ('{"confidence":' + number + '}').encode("utf-8")
            with self.subTest(number=number), RunReport(self.path) as report:
                with self.assertRaises(TypeSafeError):
                    self.sdk_call(report, lambda request: httpx2.Response(200, content=content))
                report.close()
                event = read_events(report.path)[-1]
                self.assertEqual(event["event"], "jev_response")
                self.assertEqual(event["body_text"], content.decode("utf-8"))
                self.assertNotIn("body", event)

    def test_response_read_failures_and_cancellation_keep_status_and_original_exception(self):
        for error in (httpx2.ReadError("secret read failure"), KeyboardInterrupt("secret cancellation")):
            with self.subTest(error=type(error).__name__), RunReport(self.path) as report:
                stream = FailingStream(error)
                with httpx2.Client(
                    transport=httpx2.MockTransport(lambda request: httpx2.Response(200, stream=stream)),
                    event_hooks={"request": [report.on_request], "response": [report.on_response]},
                ) as client:
                    with self.assertRaises(type(error)) as raised:
                        client.post("https://example.com", json={"state": "test"})
                self.assertIs(raised.exception, error)
                self.assertTrue(stream.closed)
                report.close()
                events = read_events(report.path)
                self.assertEqual([event["event"] for event in events], ["jev_request", "jev_response_error"])
                self.assertEqual(events[-1]["request_id"], events[0]["request_id"])
                self.assertEqual(events[-1]["status_code"], 200)
                self.assertEqual(events[-1]["error_type"], type(error).__name__)
                self.assertNotIn("secret", report.path.read_text(encoding="utf-8"))

    def test_report_failure_during_response_failure_does_not_replace_original_error(self):
        original = KeyboardInterrupt("original cancellation")
        request = httpx2.Request("POST", "https://example.com", json={"state": "test"})
        response = httpx2.Response(200, stream=FailingStream(original), request=request)
        with RunReport(self.path) as report:
            report.on_request(request)
            with patch.object(report, "append", side_effect=OSError("disk failure")):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    report.on_response(response)
            self.assertIs(raised.exception, original)
            report.close()
            self.assertEqual([event["event"] for event in read_events(report.path)], ["jev_request"])
        response.close()


if __name__ == "__main__":
    unittest.main()
