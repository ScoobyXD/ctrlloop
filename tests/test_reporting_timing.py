"""Deterministic clocks and offline transports validate measurement boundaries."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx2

from src import main as cli
from src import reporting
from src.pipeline import PipelineResult
from src.reporting import RunReport


class Clock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now

    def advance(self, nanoseconds):
        self.now += nanoseconds


class TimedStream(httpx2.SyncByteStream):
    def __init__(self, clock, failure=None):
        self.clock = clock
        self.failure = failure

    def __iter__(self):
        self.clock.advance(375_000)
        if self.failure is not None:
            raise self.failure
        yield b'{"ok": true}'


class ReportingTimingTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parents[1]
        self.directory = self.workspace / f".report-timing-tests-{uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(self.cleanup_directory)

    def cleanup_directory(self):
        target = self.directory.resolve()
        if target.parent != self.workspace or not target.name.startswith(".report-timing-tests-"):
            raise ValueError("Unexpected test cleanup path")
        shutil.rmtree(target)

    def events(self, report):
        report.close()
        return json.loads(report.path.read_text(encoding="utf-8"))

    def test_final_report_write_is_separate_from_work_and_wall_clock_adjustment(self):
        clock = Clock()
        with patch("src.reporting.perf_counter_ns", clock), RunReport(
            self.directory, origin_ns=clock.now, origin_wall_ns=1_700_000_000_000_000_000,
            origin_label="test_clock",
        ) as report:
            write = report._write_document

            def slow_write(document):
                clock.advance(2_000_000)
                write(document)

            with patch.object(report, "_write_document", side_effect=slow_write) as writer:
                with report.span("outer") as outer:
                    with report.span("native_action") as action:
                        clock.advance(125_123)
                    # Subsequent wall clock changes must not change elapsed time.
                    with patch("src.reporting.time_ns", return_value=0):
                        report.append("milestone")
                report.append("run_finished")
                writer.assert_not_called()
                self.assertFalse(report.path.exists())
                completion = report.close()
                writer.assert_called_once()
                events = self.events(report)
        start = next(e for e in events if e["event"] == "timing_started" and e["span_id"] == action["span_id"])
        self.assertAlmostEqual(action["duration_ms"], 0.125123)
        self.assertAlmostEqual(action["started_elapsed_ms"] - start["elapsed_ms"], 0.0)
        self.assertNotIn("started_elapsed_ms", start)
        self.assertNotIn("_started_ns", start)
        self.assertEqual(action["parent_span_id"], outer["span_id"])
        self.assertAlmostEqual(outer["duration_ms"], 0.125123)
        self.assertEqual([e["elapsed_ms"] for e in events], sorted(e["elapsed_ms"] for e in events))
        self.assertEqual([e["timestamp"] for e in events], sorted(e["timestamp"] for e in events))
        self.assertLess(datetime.fromisoformat(events[0]["timestamp"]), datetime.fromisoformat(events[-1]["timestamp"]))
        summary = events[-1]["timing"]
        self.assertEqual(summary["origin"], "test_clock")
        self.assertAlmostEqual(summary["duration_ms"], 0.125123)
        self.assertIsNone(summary["time_to_first_jev_request_ms"])
        self.assertEqual(summary["jev_api_call_count"], 0)
        self.assertEqual(summary["end_boundary"], "operational_end_before_report_generation")
        self.assertNotIn("report_write_count", summary)
        self.assertEqual(completion["report_write_count"], 1)
        self.assertAlmostEqual(completion["report_generation_duration_ms"], 2.0)
        self.assertAlmostEqual(completion["report_write_duration_ms"], 2.0)
        self.assertAlmostEqual(completion["through_report_duration_ms"], 2.125123)

    def test_span_closes_on_error_and_interrupt_without_disclosing_exception_message(self):
        for failure in (RuntimeError("private detail"), KeyboardInterrupt("private detail")):
            with self.subTest(type=type(failure).__name__), RunReport(self.directory) as report:
                with self.assertRaises(type(failure)) as raised:
                    with report.span("outer"):
                        with report.span("work"):
                            raise failure
                self.assertIs(raised.exception, failure)
                events = self.events(report)
                starts = [e for e in events if e["event"] == "timing_started"]
                ends = [e for e in events if e["event"] == "timing_finished"]
                self.assertEqual({e["span_id"] for e in starts}, {e["span_id"] for e in ends})
                expected = "interrupted" if isinstance(failure, KeyboardInterrupt) else "error"
                self.assertTrue(all(e["status"] == expected for e in ends))
                self.assertNotIn("private detail", report.path.read_text(encoding="utf-8"))

    def test_timing_capture_failures_propagate_without_recovery(self):
        with RunReport(self.directory) as report:
            append = report.append

            def fail_finish(event, **data):
                if event == "timing_finished":
                    raise OSError("private disk detail")
                return append(event, **data)

            with patch.object(report, "append", side_effect=fail_finish):
                with self.assertRaises(OSError):
                    with report.span("work"):
                        pass
            self.assertTrue(all(e["event"] == "timing_started" for e in self.events(report)))

    def test_api_interval_excludes_capture_encoding_and_report_write_but_includes_full_body(self):
        clock = Clock()
        with patch("src.reporting.perf_counter_ns", clock), RunReport(self.directory) as report:
            write = report._write_document
            append = report.append

            def slow_capture(event, **data):
                if event == "jev_request":
                    clock.advance(3_000_000)
                elif event == "jev_response":
                    clock.advance(4_000_000)
                append(event, **data)

            def slow_write(document):
                clock.advance(2_000_000)
                write(document)

            def handler(request):
                self.assertFalse(report.path.exists())
                clock.advance(5_000_000)
                return httpx2.Response(200, stream=TimedStream(clock))

            with patch.object(report, "_write_document", side_effect=slow_write) as writer, patch.object(
                report, "append", side_effect=slow_capture,
            ), patch("src.reporting._body_fields", wraps=reporting._body_fields) as body_decode, httpx2.Client(
                transport=httpx2.MockTransport(handler),
                event_hooks={"request": [report.on_request], "response": [report.on_response]},
            ) as client:
                with report.span("selection.round", stage="controls", round=1, round_id="controls_r1"):
                    with report.span("selection.sdk_call", question_keys=["controls_r1_g0", "controls_r1_g1"]):
                        clock.advance(7_000_000)  # Local request preparation before the HTTP hook.
                        client.post("https://example.com", json={"questions": {"controls_r1_g0": {}, "controls_r1_g1": {}}})
                    with report.span("selection.response_validation"):
                        clock.advance(2_000_000)
                    with report.span("selection.advancement"):
                        clock.advance(1_000_000)
                report.append("run_finished")
                writer.assert_not_called()
                body_decode.assert_not_called()
                events = self.events(report)
                writer.assert_called_once()
                self.assertEqual(body_decode.call_count, 2)
        request = next(e for e in events if e["event"] == "jev_request")
        response = next(e for e in events if e["event"] == "jev_response")
        self.assertEqual(response["request_id"], request["request_id"])
        self.assertEqual(response["round_id"], request["round_id"])
        self.assertEqual(response["question_keys"], ["controls_r1_g0", "controls_r1_g1"])
        self.assertEqual(request["start_boundary"], "prepared_request_before_transport")
        self.assertAlmostEqual(request["request_preparation_duration_ms"], 10.0)
        self.assertAlmostEqual(response["duration_ms"], 5.375)
        self.assertAlmostEqual(response["response_body_read_duration_ms"], 0.375)
        self.assertAlmostEqual(response["response_headers_elapsed_ms"] - request["elapsed_ms"], 5.0)
        self.assertAlmostEqual(events[-1]["timing"]["time_to_first_jev_request_ms"], request["elapsed_ms"])
        sdk = next(e for e in events if e["event"] == "timing_finished")
        self.assertAlmostEqual(sdk["duration_ms"], 19.375)
        round_timing = next(e for e in events if e["event"] == "timing_finished" and e["operation"] == "selection.round")
        self.assertAlmostEqual(round_timing["response_processing_duration_ms"], 7.0)
        self.assertEqual(round_timing["response_processing_started_elapsed_ms"], response["finished_elapsed_ms"])
        self.assertAlmostEqual(round_timing["response_processing_finished_elapsed_ms"], 22.375)

    def test_repeated_round_ids_correlate_response_processing_with_their_iteration(self):
        clock = Clock()
        with patch("src.reporting.perf_counter_ns", clock), RunReport(self.directory) as report:
            def handler(request):
                clock.advance(5_000_000)
                return httpx2.Response(200, json={"ok": True})

            with httpx2.Client(transport=httpx2.MockTransport(handler), event_hooks={
                "request": [report.on_request], "response": [report.on_response],
            }) as client:
                for iteration, processing_ms in ((1, 3), (2, 7)):
                    with report.span("loop.iteration", iteration=iteration):
                        for stage in ("windows", "controls"):
                            with report.span("selection.round", stage=stage, round=1, round_id=f"{stage}_r1"):
                                with report.span("selection.sdk_call"):
                                    client.post("https://example.com", json={})
                                with report.span("selection.response_validation"):
                                    clock.advance((processing_ms - 1) * 1_000_000)
                                with report.span("selection.advancement"):
                                    clock.advance(1_000_000)
            events = self.events(report)

        rounds = [e for e in events if e["event"] == "timing_finished" and e["operation"] == "selection.round"]
        self.assertEqual(report.close()["jev_api_call_count"], 4)
        self.assertEqual(len(rounds), 4)
        responses = {
            (e["iteration"], e["round_id"]): e for e in events if e["event"] == "jev_response"
        }
        advancements = {
            (e["iteration"], e["round_id"]): e for e in events
            if e["event"] == "timing_finished" and e["operation"] == "selection.advancement"
        }
        for record in rounds:
            key = (record["iteration"], record["round_id"])
            self.assertEqual(record["response_processing_duration_ms"], {1: 3, 2: 7}[record["iteration"]])
            self.assertEqual(record["response_processing_started_elapsed_ms"], responses[key]["finished_elapsed_ms"])
            self.assertEqual(record["response_processing_finished_elapsed_ms"], advancements[key]["finished_elapsed_ms"])

    def test_request_without_response_ends_at_observed_sdk_error_or_interrupt(self):
        for failure in (httpx2.ConnectError("private transport detail"), KeyboardInterrupt("private interrupt")):
            with self.subTest(type=type(failure).__name__), RunReport(self.directory) as report:
                def handler(request):
                    raise failure

                with httpx2.Client(transport=httpx2.MockTransport(handler), event_hooks={
                    "request": [report.on_request], "response": [report.on_response],
                }) as client:
                    with self.assertRaises(type(failure)) as raised:
                        with report.span("selection.sdk_call", round_id="controls_r1"):
                            client.post("https://example.com", json={})
                self.assertIs(raised.exception, failure)
                events = self.events(report)
                request = next(e for e in events if e["event"] == "jev_request")
                unfinished = next(e for e in events if e["event"] == "jev_request_unfinished")
                self.assertEqual(unfinished["request_id"], request["request_id"])
                self.assertEqual(unfinished["round_id"], request["round_id"])
                self.assertEqual(unfinished["end_boundary"], "sdk_call_exit_without_complete_response")
                self.assertEqual(unfinished["finished_elapsed_ms"], events[-1]["finished_elapsed_ms"])
                self.assertNotIn("private", report.path.read_text(encoding="utf-8"))

    def invoke_cli(self, *, result=None, error=None, cleanup_error=None, include_imports=False, api_key="offline-key"):
        output, errors = io.StringIO(), io.StringIO()

        def make_report(directory, **kwargs):
            self.report = RunReport(self.directory, **kwargs)
            return self.report

        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"TYPESAFE_API_KEY": api_key}, clear=True))
            stack.enter_context(patch.object(cli, "load_dotenv"))
            stack.enter_context(patch.object(cli, "RunReport", side_effect=make_report))
            stack.enter_context(patch.object(cli, "CuaDriver"))
            stack.enter_context(patch.object(cli.httpx2, "Client"))
            client = stack.enter_context(patch.object(cli, "TypeSafeClient"))
            client.return_value.__exit__.side_effect = cleanup_error
            stack.enter_context(patch.object(cli, "run_pipeline", side_effect=error,
                                            return_value=result or PipelineResult("action_verified")))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            status = cli.main(["Find Save"], _include_imports=include_imports)
        return status, json.loads(output.getvalue()), errors.getvalue()

    def test_startup_origin_precedes_imports_and_configuration_failure(self):
        status, output, _ = self.invoke_cli(include_imports=True, api_key="")
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "error")
        events = self.events(self.report)
        self.assertEqual(events[0]["event"], "timing_origin")
        self.assertEqual(events[0]["elapsed_ms"], 0.0)
        self.assertEqual(events[0]["origin"], "main_module_after_time_import")
        imports = next(e for e in events if e["event"] == "timing_finished" and e["operation"] == "startup.imports")
        self.assertEqual(imports["started_elapsed_ms"], 0.0)
        self.assertGreater(imports["duration_ms"], 0)
        config = next(e for e in events if e["event"] == "timing_finished" and e["operation"] == "initialization.configuration")
        self.assertEqual(config["status"], "error")
        self.assertEqual(events[-1]["event"], "run_error")
        self.assertEqual(events[-1]["timing"]["duration_ms"], events[-1]["elapsed_ms"])

    def test_cleanup_failure_uses_original_cli_error_path(self):
        result = PipelineResult("action_verified", action_result={"effect": "unverifiable"})
        status, output, errors = self.invoke_cli(result=result, cleanup_error=OSError("private cleanup"))
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "error")
        self.assertIn("OSError", output["error"])
        events = self.events(self.report)
        self.assertEqual(events[-1]["event"], "run_error")
        self.assertEqual(events[-1]["timing"]["origin"], "main_call")
        self.assertNotIn("private cleanup", errors + self.report.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
