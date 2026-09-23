"""Offline timing boundaries preserve native actions and one call per round."""

import json
import io
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.driver import DriverError
from src import main as cli
from pipeline_fixtures import select_once
from src.reporting import RunReport
from src.selection import ChoiceSelector, QUESTION
from test_driver import foreground_result
from test_pipeline import ChoosingClient, make_element, make_window


class FakeClock:
    def __init__(self):
        self.nanoseconds = 5_000_000_000

    def __call__(self):
        return self.nanoseconds

    def advance(self, milliseconds):
        self.nanoseconds += milliseconds * 1_000_000


class TimedDriver:
    """Deterministic fixture costs, never measurements of the actual desktop."""

    durations = {"list_windows": 11, "bring_to_front": 13, "get_window_state": 17, "click": 19}

    def __init__(self, clock, *, windows=None, elements=None, fail_tool=None, failure=None):
        self.clock = clock
        self.windows = [make_window()] if windows is None else windows
        self.elements = [make_element()] if elements is None else elements
        self.calls = []
        self.intervals = {}
        self.fail_tool = fail_tool
        self.failure = failure
        self.native_result = {
            "effect": "unverifiable", "route": "accessibility", "delivery": {"mode": "background"},
        }

    def _call(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        started = self.clock()
        self.clock.advance(self.durations[tool])
        self.intervals[tool] = (started, self.clock())
        if tool == self.fail_tool:
            raise self.failure

    def list_windows(self):
        self._call("list_windows")
        return self.windows

    def bring_to_front(self, arguments):
        self._call("bring_to_front", arguments)
        return foreground_result(arguments)

    def get_window_state(self, window):
        self._call("get_window_state", window)
        return {"snapshot_id": "fixture-snapshot", "elements": self.elements, "elements_complete": False}

    def click(self, arguments):
        self._call("click", arguments)
        return self.native_result

    def close(self):
        self.closed = True


class PipelineTimingTests(unittest.TestCase):
    def setUp(self):
        # Inherit workspace ACLs, as in the existing report tests on Windows.
        self.workspace = Path(__file__).resolve().parents[1]
        self.directory = self.workspace / f".pipeline-timing-tests-{uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(self.cleanup_directory)
        self.clock = FakeClock()
        self.origin_ns = self.clock()
        self.clock_patch = patch("src.reporting.perf_counter_ns", side_effect=self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def cleanup_directory(self):
        target = self.directory.resolve()
        if target.parent != self.workspace or not target.name.startswith(".pipeline-timing-tests-"):
            raise ValueError("Unexpected test cleanup path")
        shutil.rmtree(target)

    @staticmethod
    def events(report):
        return json.loads(report.path.read_text(encoding="utf-8"))

    def finished(self, report, operation):
        return next(
            event for event in self.events(report)
            if event["event"] == "timing_finished" and event["operation"] == operation
        )

    def assert_paired(self, report):
        events = self.events(report)
        started = {event["span_id"]: event for event in events if event["event"] == "timing_started"}
        finished = {event["span_id"]: event for event in events if event["event"] == "timing_finished"}
        self.assertEqual(started.keys(), finished.keys())
        for span_id, end in finished.items():
            start = started[span_id]
            self.assertEqual(start["operation"], end["operation"])
            self.assertGreaterEqual(end["started_elapsed_ms"], start["elapsed_ms"])
            self.assertEqual(end["duration_ms"], end["finished_elapsed_ms"] - end["started_elapsed_ms"])

    def test_pipeline_buffers_until_one_final_write_excluded_from_operational_timing(self):
        driver = TimedDriver(self.clock)
        report = RunReport(self.directory)
        write_document = report._write_document

        def slow_write(text):
            write_document(text)
            self.clock.advance(250)

        with patch.object(report, "_write_document", side_effect=slow_write) as write:
            result = select_once("Click Save", driver, ChoiceSelector(ChoosingClient()),
                                  report=report, deliver_click=True)
            report.append("run_finished", result=result.to_dict())
            write.assert_not_called()
            self.assertFalse(report.path.exists())
            timing = report.close()
            write.assert_called_once()

        self.assertEqual(timing["duration_ms"], sum(driver.durations.values()))
        self.assertEqual(timing["report_generation_duration_ms"], 250)
        self.assertEqual(timing["through_report_duration_ms"], sum(driver.durations.values()) + 250)
        self.assertEqual(timing["report_write_count"], 1)

        self.assertEqual(result.status, "action_returned")
        self.assertIs(result.action_result, driver.native_result)
        self.assertEqual([tool for tool, _ in driver.calls], [
            "list_windows", "bring_to_front", "get_window_state", "click",
        ])
        for operation, tool in (
            ("window_discovery", "list_windows"), ("foreground_activation", "bring_to_front"),
            ("control_acquisition", "get_window_state"), ("native_action", "click"),
        ):
            finished = self.finished(report, operation)
            self.assertEqual(finished["status"], "ok")
            self.assertEqual(finished["duration_ms"], driver.durations[tool])
            native_start, native_end = driver.intervals[tool]
            self.assertEqual(finished["started_elapsed_ms"], (native_start - self.origin_ns) / 1_000_000)
            self.assertEqual(finished["finished_elapsed_ms"], (native_end - self.origin_ns) / 1_000_000)
        dispatch = self.finished(report, "action_dispatch")
        native = self.finished(report, "native_action")
        selected = next(event for event in self.events(report) if event["event"] == "control_selected")
        self.assertEqual(selected["span_id"], dispatch["span_id"])
        self.assertEqual(native["parent_span_id"], dispatch["span_id"])
        self.assertEqual(native["started_elapsed_ms"], selected["elapsed_ms"])
        self.assertEqual(dispatch["duration_ms"], native["duration_ms"])
        self.assertEqual(native["completion_scope"], "call_return")
        operations = {event.get("operation") for event in self.events(report)}
        self.assertTrue({"candidate_preparation", "observation_processing", "synthetic_resolution",
                         "window_selection", "control_selection", "foreground_preparation",
                         "action_preparation"}.issubset(operations))
        self.assert_paired(report)

    def test_reporting_preserves_round_questions_and_the_exact_single_action(self):
        plain_client, timed_client = ChoosingClient(), ChoosingClient()
        many_candidates = {
            "windows": [make_window(index) for index in range(11)],
            "elements": [make_element(index) for index in range(21)],
        }
        plain_driver = TimedDriver(self.clock, **many_candidates)
        timed_driver = TimedDriver(self.clock, **many_candidates)
        plain = select_once("Click Save", plain_driver, ChoiceSelector(plain_client), deliver_click=True)
        with RunReport(self.directory) as report:
            timed = select_once("Click Save", timed_driver, ChoiceSelector(timed_client),
                                 report=report, deliver_click=True)

        def requests(client):
            return [
                {**call, "questions": {key: question.model_dump() for key, question in call["questions"].items()}}
                for call in client.calls
            ]

        self.assertEqual(requests(plain_client), requests(timed_client))
        self.assertEqual([list(call["questions"]) for call in timed_client.calls], [
            ["windows_r1_g0", "windows_r1_g1"], ["windows_r2_g0"],
            ["controls_r1_g0", "controls_r1_g1", "controls_r1_g2"], ["controls_r2_g0"],
        ])
        for call in timed_client.calls:
            self.assertEqual(call["state"]["original_request"], "Click Save")
            for question in call["questions"].values():
                self.assertEqual(question.instructions, QUESTION)
        self.assertEqual(plain_driver.calls, timed_driver.calls)
        self.assertEqual([tool for tool, _ in timed_driver.calls], [
            "list_windows", "bring_to_front", "get_window_state", "click",
        ])
        self.assertEqual(plain.control, timed.control)
        self.assertEqual(plain.window, timed.window)
        self.assertEqual(plain.action_result, timed.action_result)
        self.assertEqual(timed_driver.calls[-1][1], {
            "pid": timed.window["pid"], "window_id": timed.window["window_id"],
            "element_token": timed.control["element_token"],
        })

    def test_cli_finishes_cleanup_then_writes_once_before_returning_complete_timing(self):
        driver = TimedDriver(self.clock)
        client = ChoosingClient()
        reports = []
        cleanup = []
        stdout = io.StringIO()

        def make_report(directory, **kwargs):
            report = RunReport(self.directory, **kwargs)
            reports.append(report)
            write_document = report._write_document

            def delayed_write(text):
                self.assertEqual(cleanup, ["sdk", "http"])
                self.assertTrue(driver.closed)
                self.assertEqual(driver.calls[-1][0], "click")
                self.assertEqual(stdout.getvalue(), "")
                write_document(text)
                self.clock.advance(250)

            report._write_document = delayed_write
            return report

        def finish_client(name, delay):
            cleanup.append(name)
            self.clock.advance(delay)
            return False

        def completed_pipeline(*args, **kwargs):
            # Isolate the CLI cleanup/report boundary from loop routing. The
            # shared stages still incur their deterministic fake driver costs.
            result = select_once(*args, **kwargs, deliver_click=True)
            result.status = "action_verified"
            return result

        with (
            patch.object(cli, "perf_counter_ns", side_effect=self.clock),
            patch.dict("os.environ", {"TYPESAFE_API_KEY": "fixture-key"}, clear=True),
            patch.object(cli, "load_dotenv"),
            patch.object(cli, "RunReport", side_effect=make_report),
            patch.object(cli, "CuaDriver", return_value=driver),
            patch.object(cli, "run_pipeline", side_effect=completed_pipeline),
            patch.object(cli, "TypeSafeClient") as sdk,
            patch.object(cli.httpx2, "Client") as http,
            patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()),
        ):
            sdk.return_value.__enter__.return_value = client
            sdk.return_value.__exit__.side_effect = lambda *args: finish_client("sdk", 2)
            http.return_value.__exit__.side_effect = lambda *args: finish_client("http", 3)
            status = cli.main(["Click Save"])

        self.assertEqual(status, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "action_verified")
        self.assertEqual(output["action_result"], driver.native_result)
        timing = output["timing"]
        self.assertEqual(timing["duration_ms"], 65)
        self.assertEqual(timing["report_generation_duration_ms"], 250)
        self.assertEqual(timing["through_report_duration_ms"], 315)
        self.assertEqual(timing["report_write_count"], 1)
        report = reports[0]
        terminal = self.events(report)[-1]
        self.assertEqual(terminal["event"], "run_finished")
        self.assertEqual(terminal["timing"]["duration_ms"], 65)
        self.assertEqual(terminal["timing"]["end_boundary"], "operational_end_before_report_generation")
        self.assertNotIn("report_generation_duration_ms", terminal["timing"])
        self.assertEqual(self.finished(report, "client_cleanup")["duration_ms"], 5)

    def test_native_errors_and_interrupts_close_the_active_boundaries_without_retries(self):
        cases = (
            ("list_windows", "window_discovery"), ("bring_to_front", "foreground_activation"),
            ("get_window_state", "control_acquisition"), ("click", "native_action"),
        )
        for tool, operation in cases:
            for error in (DriverError("Fixture native failure.", tool=tool), KeyboardInterrupt()):
                with self.subTest(tool=tool, error=type(error).__name__):
                    driver = TimedDriver(self.clock, fail_tool=tool, failure=error)
                    with RunReport(self.directory) as report:
                        with self.assertRaises(type(error)) as raised:
                            select_once("Click Save", driver, ChoiceSelector(ChoosingClient()),
                                         report=report, deliver_click=True)
                    self.assertIs(raised.exception, error)
                    finished = self.finished(report, operation)
                    self.assertEqual(finished["status"], "interrupted" if isinstance(error, KeyboardInterrupt) else "error")
                    self.assertEqual(finished["error_type"], type(error).__name__)
                    self.assertEqual(finished["duration_ms"], driver.durations[tool])
                    self.assertEqual(driver.calls[-1][0], tool)
                    self.assertEqual(sum(call[0] == tool for call in driver.calls), 1)
                    if isinstance(error, DriverError) and tool != "list_windows":
                        self.assertEqual(error.context["stage"], tool)
                    self.assert_paired(report)

    def test_synthetic_skip_and_empty_observation_never_invent_action_intervals(self):
        for driver, expected in (
            (TimedDriver(self.clock, windows=[]), "no_action"),
            (TimedDriver(self.clock, elements=[]), "no_controls"),
        ):
            with self.subTest(expected=expected):
                with RunReport(self.directory) as report:
                    result = select_once("Click Save", driver, ChoiceSelector(ChoosingClient()),
                                          report=report, deliver_click=True)
                self.assertEqual(result.status, expected)
                operations = {event.get("operation") for event in self.events(report)}
                self.assertNotIn("native_action", operations)
                self.assertNotIn("click", [tool for tool, _ in driver.calls])
                if expected == "no_action":
                    self.assertEqual([tool for tool, _ in driver.calls], ["list_windows"])
                    self.assertNotIn("foreground_activation", operations)
                    self.assertNotIn("control_acquisition", operations)
                self.assert_paired(report)

    def test_failed_click_start_boundary_prevents_the_native_action(self):
        for error in (OSError("Fixture sink unavailable."), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                driver = TimedDriver(self.clock)
                with RunReport(self.directory) as report:
                    append = report.append

                    def fail_start(event, **data):
                        if event == "timing_started" and data.get("operation") == "native_action":
                            raise error
                        append(event, **data)

                    with patch.object(report, "append", side_effect=fail_start):
                        with self.assertRaises(type(error)) as raised:
                            select_once("Click Save", driver, ChoiceSelector(ChoosingClient()),
                                         report=report, deliver_click=True)
                self.assertIs(raised.exception, error)
                self.assertNotIn("click", [tool for tool, _ in driver.calls])
                expected = "error" if isinstance(error, OSError) else "interrupted"
                self.assertEqual(self.finished(report, "action_dispatch")["status"], expected)


if __name__ == "__main__":
    unittest.main()
