"""Shared observation/selection stage checks and thin always-loop CLI boundaries."""

import contextlib
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.driver import DriverError
from test_driver import foreground_result
from src.pipeline import (
    PipelineResult, control_candidates,
    window_candidates,
)
from pipeline_fixtures import select_once
from src.selection import ChoiceSelector, PINNED_MODEL, QUESTION
from src.synthetic import START_MENU_KEY, START_MENU_SEARCH_KEY


class RecordingReport:
    """Keep appended milestones inspectable without touching the report directory."""

    def __init__(self):
        self.path = Path("reports") / "test-run.json"
        self.events = []
        self.closed = False
        self.close_count = 0
        self.timing = {"duration_ms": 20, "report_generation_duration_ms": 5,
                       "report_write_duration_ms": 3, "report_write_count": 1,
                       "through_report_duration_ms": 25}
        self._span_id = 0

    def append(self, event, **data):
        # These legacy assertions cover milestones. Timing and detailed selection
        # events are exercised with the real sink in the timing test modules.
        if event.startswith(("timing_", "selection_")):
            return
        self.events.append(json.loads(json.dumps({"event": event, **data})))

    @contextlib.contextmanager
    def span(self, operation, **data):
        self._span_id += 1
        yield {"span_id": self._span_id}

    def record_interval(self, operation, started_ns, finished_ns, **data):
        pass

    def close(self):
        self.closed = True
        self.close_count += 1
        return self.timing

    def on_request(self, request):
        pass

    def on_response(self, response):
        pass


class ChoosingClient:
    """Choose a preferred option or the last one with unhelpful confidence."""

    def __init__(self, preferred_key=None):
        self.calls = []
        self.preferred_key = preferred_key

    def system_one(self, **request):
        self.calls.append(request)
        answers = {}
        for key, question in request["questions"].items():
            labels = list(question.criteria)
            answers[key] = SimpleNamespace(
                type="choice",
                choice=self.preferred_key if self.preferred_key in labels else labels[-1],
                confidence=0,
                probabilities={label: 1 / len(labels) for label in labels},
            )
        return SimpleNamespace(
            model=PINNED_MODEL, answers=answers,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


class ObservingDriver:
    """Observe and bring forward; deliberately has no click or screenshot API."""

    def __init__(self, windows, snapshot):
        self.windows = windows
        self.snapshot = snapshot
        self.calls = []

    def list_windows(self):
        self.calls.append(("list_windows",))
        return self.windows

    def bring_to_front(self, arguments):
        self.calls.append(("bring_to_front", arguments))
        return foreground_result(arguments)

    def get_window_state(self, window):
        self.calls.append(("get_window_state", window))
        return self.snapshot


def make_window(index=0):
    return {"pid": 900 + index, "window_id": 4000 + index,
            "app_name": "Editor", "title": f"Document {index}"}


def make_element(index=0):
    return {"element_index": 700 + index * 3, "role": "button", "depth": 2,
            "label": f"Action {index}", "element_token": f"snapshot-secret:{index}"}


class PipelineTests(unittest.TestCase):
    def run_observation(self, snapshot, windows=None, request="Find save"):
        client = ChoosingClient()
        driver = ObservingDriver([make_window()] if windows is None else windows, snapshot)
        return select_once(request, driver, ChoiceSelector(client)), driver, client

    def test_both_stages_preserve_observed_identity_and_use_one_snapshot(self):
        windows = [make_window(index) for index in range(11)]
        elements = [make_element(index) for index in range(101)]
        snapshot = {"snapshot_id": "snapshot-secret", "elements": elements,
                    "elements_complete": False, "tree_markdown": "RAW TREE SECRET"}
        request = "  Find the résumé’s Save button.\r\nKeep this wording.\n "
        result, driver, client = self.run_observation(snapshot, windows, request)

        self.assertEqual(result.status, "selected")
        self.assertIs(result.window, windows[-1])
        self.assertIs(result.control, elements[-1])
        self.assertEqual(result.control["element_index"], 1000)
        self.assertEqual(result.control["element_token"], "snapshot-secret:100")
        self.assertEqual(result.snapshot_id, "snapshot-secret")
        self.assertEqual(driver.calls, [("list_windows",), ("bring_to_front", {"pid": windows[-1]["pid"], "window_id": windows[-1]["window_id"]}), ("get_window_state", windows[-1])])
        self.assertIs(driver.calls[2][1], windows[-1])
        self.assertEqual([r["candidate_count"] for r in result.selections["windows"]], [12, 2])
        self.assertEqual([r["candidate_count"] for r in result.selections["controls"]], [101, 11, 2])
        self.assertIs(result.observation["elements_complete"], False)
        self.assertEqual(result.observation["observed_control_count"], 101)

        stages = []
        for call in client.calls:
            self.assertEqual(call["state"], request)
            self.assertEqual(call["model"], PINNED_MODEL)
            for key, question in call["questions"].items():
                stages.append(key.split("_")[0])
                self.assertEqual(question.instructions, QUESTION)
                self.assertLessEqual(len(question.criteria), 10)
            wire_content = json.dumps({
                "state": call["state"],
                "questions": {key: q.model_dump() for key, q in call["questions"].items()},
            })
            for private in ("snapshot-secret", "RAW TREE SECRET", '"pid"', '"window_id"',
                            '"element_index"', '"snapshot_id"', '"element_token"'):
                self.assertNotIn(private, wire_content)
        self.assertEqual(stages, sorted(stages, key={"windows": 0, "controls": 1}.get))
        # Multiple independent questions are combined before advancing a round.
        self.assertTrue(any(len(call["questions"]) > 1 for call in client.calls))

    def test_no_real_windows_forwards_virtual_start_and_search_without_jev_or_window_read(self):
        result, driver, client = self.run_observation({}, windows=[])
        self.assertEqual(result.status, "selected")
        self.assertEqual(result.window["title"], "Start Menu")
        self.assertTrue(result.window["synthetic"])
        self.assertEqual(result.control["label"], "Search")
        self.assertTrue(result.control["synthetic"])
        self.assertEqual(result.selections, {"windows": [], "controls": []})
        self.assertEqual(driver.calls, [("list_windows",)])
        self.assertEqual(client.calls, [])

    def test_virtual_start_win_has_one_search_target_without_observing_or_acting(self):
        request = "  Open an app from Start.\r\n"
        driver = ObservingDriver([make_window()], {"elements": [make_element()]})
        client = ChoosingClient(preferred_key=START_MENU_KEY)
        selector = ChoiceSelector(client)
        with patch.object(selector, "select", wraps=selector.select) as select:
            result = select_once(request, driver, selector)

        self.assertEqual(result.status, "selected")
        self.assertEqual(driver.calls, [("list_windows",)])
        self.assertEqual(select.call_count, 2)
        control_call = select.call_args_list[1]
        self.assertEqual(control_call.args[0], request)
        self.assertEqual(control_call.kwargs, {"stage": "controls"})
        controls = control_call.args[1]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0].key, START_MENU_SEARCH_KEY)
        self.assertIs(result.control, controls[0].record)
        self.assertEqual(result.window["title"], "Start Menu")
        self.assertTrue(result.window["synthetic"])
        self.assertEqual(result.control["role"], "Edit")
        self.assertEqual(result.control["label"], "Search")
        self.assertEqual(result.control["input_type"], "text")
        self.assertEqual(result.control["actions"], ["type_text"])
        self.assertEqual(result.control["target"], "windows_start_menu_search")
        self.assertTrue(result.control["synthetic"])
        self.assertIsNone(result.snapshot_id)
        self.assertEqual(result.observation["source"], "synthetic")
        self.assertNotIn("observed_control_count", result.observation)
        for record in (result.window, result.control):
            for field in (
                "pid", "window_id", "element_index", "element_token", "snapshot_id", "frame", "bounds",
            ):
                self.assertNotIn(field, record)
        self.assertEqual(result.selections["controls"], [])
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(key.startswith("windows_") for key in client.calls[0]["questions"]))
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["control"], result.control)

    def test_window_candidates_offer_one_start_route_and_preserve_other_records(self):
        editor = make_window()
        start = {"pid": 123, "window_id": 456, "app_name": "StartMenuExperienceHost.exe", "title": "Start"}
        other = make_window(1)
        other["title"] = "Start"
        search = {"pid": 124, "window_id": 457, "app_name": "SearchHost.exe", "title": "Search"}
        windows = [editor, start, other, search]
        candidates = window_candidates(windows)

        self.assertEqual(candidates[0].key, START_MENU_KEY)
        self.assertEqual(candidates[0].description["title"], "Start Menu")
        self.assertEqual([candidate.record for candidate in candidates[1:]], [editor, other, search])
        self.assertEqual([candidate.key for candidate in candidates[1:]], ["window_0", "window_2", "window_3"])
        for candidate, record in zip(candidates[1:], [editor, other, search]):
            self.assertIs(candidate.record, record)
        self.assertEqual(sum(bool(candidate.record.get("synthetic")) for candidate in candidates), 1)
        self.assertEqual(windows, [editor, start, other, search])

    def test_discovery_failure_does_not_turn_into_a_virtual_start_success(self):
        driver = ObservingDriver([], {})
        client = ChoosingClient(preferred_key=START_MENU_KEY)
        error = DriverError("Discovery was denied.", code="access_denied", tool="list_windows")
        with patch.object(driver, "list_windows", side_effect=error) as discover:
            with self.assertRaises(DriverError) as raised:
                select_once("Open Start", driver, ChoiceSelector(client))
        self.assertIs(raised.exception, error)
        discover.assert_called_once_with()
        self.assertEqual(driver.calls, [])
        self.assertEqual(client.calls, [])

    def test_failed_window_read_preserves_selection_without_retry_or_control_judgment(self):
        windows = [make_window(index) for index in range(11)]
        windows[-1].update({
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True, "minimized": False,
            "unrelated_data": "RAW BODY SECRET",
        })
        error = DriverError(
            "Window read failed.", code="driver_call_failed",
            tool="get_window_state", exit_code=1,
        )
        driver = ObservingDriver(windows, {})
        client = ChoosingClient()

        def failed_read(window):
            driver.calls.append(("get_window_state", window))
            raise error

        with patch.object(driver, "get_window_state", side_effect=failed_read):
            with self.assertRaises(DriverError) as raised:
                select_once("Find save", driver, ChoiceSelector(client))

        self.assertIs(raised.exception, error)
        self.assertEqual(error.context["stage"], "get_window_state")
        self.assertEqual(error.context["window"], {
            key: value for key, value in windows[-1].items() if key != "unrelated_data"
        })
        self.assertEqual(
            [round_["candidate_count"] for round_ in error.context["selections"]["windows"]],
            [12, 2],
        )
        self.assertNotIn("controls", error.context["selections"])
        self.assertNotIn("RAW BODY SECRET", json.dumps(error.details()))
        self.assertEqual(driver.calls, [("list_windows",), ("bring_to_front", {"pid": windows[-1]["pid"], "window_id": windows[-1]["window_id"]}), ("get_window_state", windows[-1])])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(
            key.startswith("windows_") for call in client.calls for key in call["questions"]
        ))

    def test_failed_window_read_does_not_fabricate_optional_window_metadata(self):
        window = make_window()
        driver = ObservingDriver([window], {})
        error = DriverError("Window read failed.")
        with patch.object(driver, "get_window_state", side_effect=error) as read:
            with self.assertRaises(DriverError):
                select_once("Find save", driver, ChoiceSelector(ChoosingClient()))
        read.assert_called_once_with(window)
        self.assertEqual(error.context["window"], window)
        self.assertEqual(len(error.context["selections"]["windows"]), 1)
        self.assertEqual(error.context["selections"]["windows"][0]["candidate_count"], 2)

    def test_empty_degraded_and_unavailable_reads_are_distinct(self):
        cases = [
            ({"snapshot_id": "s", "elements": [], "elements_complete": False}, "no_controls"),
            ({"snapshot_id": "s", "elements": [], "degraded": True,
              "degraded_reason": "provider unavailable"}, "observation_degraded"),
            ({"snapshot_id": "s"}, "observation_unavailable"),
            ({"snapshot_id": "s", "elements": None}, "observation_unavailable"),
            ({"elements": [make_element()]}, "observation_unavailable"),
        ]
        for snapshot, expected in cases:
            with self.subTest(snapshot=snapshot):
                result, driver, client = self.run_observation(snapshot)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.control)
                self.assertEqual(len(driver.calls), 3)
                self.assertEqual(len(client.calls), 1)
                self.assertTrue(all(key.startswith("windows_") for key in client.calls[0]["questions"]))
                self.assertTrue(result.reason)
                self.assertEqual(result.window, make_window())

    def test_nonempty_degraded_read_keeps_quality_metadata_visible(self):
        result, _, _ = self.run_observation({
            "snapshot_id": "s", "elements": [make_element()], "degraded": True,
            "degraded_reason": "partial provider read", "truncated": True,
            "truncation_reason": "node limit", "elements_complete": False,
        })
        self.assertEqual(result.status, "selected")
        self.assertTrue(result.observation["degraded"])
        self.assertTrue(result.observation["truncated"])
        self.assertEqual(result.observation["degraded_reason"], "partial provider read")
        self.assertIn("full UI coverage is not established", result.observation["coverage"])

    def test_optional_control_fields_may_be_absent_or_null(self):
        minimal = {"element_index": 99, "role": "text", "depth": 9}
        nullable = {"element_index": 5, "role": "button", "depth": 2, "label": None,
                    "value": None, "actions": None, "parent_index": None, "element_token": None}
        result, _, client = self.run_observation({"snapshot_id": "s", "elements": [minimal, nullable]})
        self.assertIs(result.control, nullable)
        descriptions = next(iter(client.calls[-1]["questions"].values())).criteria
        self.assertEqual(descriptions, {"control_0": {"role": "text"}, "control_1": {"role": "button"}})

    def test_single_observed_control_forwards_without_control_jev_and_result_is_json_serializable(self):
        result, _, client = self.run_observation({"snapshot_id": "s", "elements": [make_element()]})
        self.assertEqual(result.status, "selected")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.selections["controls"], [])
        self.assertEqual(result.selections["windows"][0]["candidate_count"], 2)
        self.assertTrue(all(key.startswith("windows_") for key in client.calls[0]["questions"]))
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["snapshot_id"], "s")

    def test_descriptions_preserve_source_records_and_exclude_local_identity(self):
        window = {**make_window(), "snapshot_id": "local-only", "bounds": {"x": 0, "y": 0},
                  "is_on_screen": False, "minimized": False}
        candidate = window_candidates([window])[1]
        self.assertIs(candidate.record, window)
        self.assertEqual(candidate.description, {
            "app_name": "Editor", "title": "Document 0", "bounds": {"x": 0, "y": 0},
            "is_on_screen": False, "minimized": False,
        })
        element = {**make_element(), "label": "X" * 300, "value": "X" * 300,
                   "enabled": False, "selected": False, "actions": []}
        control = control_candidates([element])[0]
        self.assertIs(control.record, element)
        self.assertEqual(len(control.description["label"]), 240)
        self.assertEqual(len(element["label"]), 300)
        self.assertNotIn("value", control.description)
        self.assertIs(control.description["enabled"], False)
        self.assertIs(control.description["selected"], False)

    def test_only_explicit_observed_parent_indices_supply_ancestry(self):
        elements = [
            {"element_index": 90, "role": "pane", "depth": 0, "label": "Root"},
            {"element_index": 7, "role": "group", "depth": 1, "label": "Tools", "parent_index": 90},
            {"element_index": 123, "role": "button", "depth": 2, "label": "Save", "parent_index": 7},
            {"element_index": 456, "role": "button", "depth": 2, "label": "No indexed parent"},
            {"element_index": 789, "role": "button", "depth": 3, "parent_index": 999},
        ]
        candidates = control_candidates(elements)
        self.assertEqual(candidates[2].description["indexed_ancestors_nearest_first"], [
            {"role": "group", "label": "Tools"}, {"role": "pane", "label": "Root"},
        ])
        for index in (0, 3, 4):
            self.assertNotIn("indexed_ancestors_nearest_first", candidates[index].description)
        self.assertIs(candidates[2].record, elements[2])

    def test_parent_cycles_do_not_repeat_ancestors(self):
        elements = [
            {"element_index": 2, "role": "group", "depth": 0, "parent_index": 8},
            {"element_index": 8, "role": "pane", "depth": 1, "parent_index": 2},
        ]
        descriptions = [candidate.description for candidate in control_candidates(elements)]
        self.assertEqual(descriptions[0]["indexed_ancestors_nearest_first"], [{"role": "pane"}])
        self.assertEqual(descriptions[1]["indexed_ancestors_nearest_first"], [{"role": "group"}])

    def test_report_keeps_discovery_and_selection_when_window_read_fails(self):
        windows = [make_window(index) for index in range(11)]
        windows[-1]["unrecognized_metadata"] = {"preserve": "full discovered record"}
        driver = ObservingDriver(windows, {})
        report = RecordingReport()
        error = DriverError("Window read failed.")
        with patch.object(driver, "get_window_state", side_effect=error):
            with self.assertRaises(DriverError):
                select_once("Find Save", driver, ChoiceSelector(ChoosingClient()), report=report)

        self.assertEqual([entry["event"] for entry in report.events], [
            "windows_discovered", "window_selected", "action_requested", "action_returned",
        ])
        self.assertEqual(report.events[0]["windows"], windows)
        self.assertEqual(report.events[1]["window"], windows[-1])
        self.assertEqual(report.events[1]["selections"], error.context["selections"]["windows"])

    def test_report_records_successful_observation_and_final_control(self):
        windows = [make_window(index) for index in range(2)]
        elements = [make_element(index) for index in range(2)]
        snapshot = {"snapshot_id": "observed-snapshot", "elements": elements,
                    "elements_complete": False, "tree_markdown": "UNNEEDED RAW TREE"}
        report = RecordingReport()
        result = select_once(
            "Find Save", ObservingDriver(windows, snapshot), ChoiceSelector(ChoosingClient()),
            report=report,
        )

        self.assertEqual([entry["event"] for entry in report.events], [
            "windows_discovered", "window_selected", "action_requested", "action_returned", "window_observed", "control_selected",
        ])
        observed = report.events[4]
        self.assertEqual(observed["window"], windows[-1])
        self.assertEqual(observed["snapshot_id"], result.snapshot_id)
        self.assertEqual(observed["observation"], result.observation)
        self.assertEqual(report.events[5]["control"], elements[-1])
        self.assertEqual(report.events[5]["selections"], result.selections["controls"])
        self.assertNotIn("UNNEEDED RAW TREE", json.dumps(report.events))

    def test_report_distinguishes_empty_discovery_from_synthetic_search_selection(self):
        report = RecordingReport()
        result = select_once(
            "Find Save", ObservingDriver([], {}), ChoiceSelector(ChoosingClient()), report=report,
        )
        self.assertEqual(result.status, "selected")
        self.assertTrue(result.window["synthetic"])
        self.assertTrue(result.control["synthetic"])
        self.assertEqual(result.control["label"], "Search")
        self.assertIsNone(result.snapshot_id)
        self.assertEqual(result.observation["source"], "synthetic")
        self.assertNotIn("observed_control_count", result.observation)
        self.assertEqual(report.events, [
            {"event": "windows_discovered", "windows": []},
            {"event": "window_selected", "window": result.window, "selections": []},
            {"event": "control_selected", "control": result.control, "selections": []},
        ])

    def test_report_keeps_unavailable_observation_before_early_return(self):
        report = RecordingReport()
        window = make_window()
        result = select_once(
            "Find Save", ObservingDriver([window], {"snapshot_id": "unavailable"}),
            ChoiceSelector(ChoosingClient()), report=report,
        )
        self.assertEqual(result.status, "observation_unavailable")
        self.assertEqual([entry["event"] for entry in report.events], [
            "windows_discovered", "window_selected", "action_requested", "action_returned", "window_observed",
        ])
        self.assertEqual(report.events[-1]["observation"], result.observation)


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = importlib.import_module("src.main")

    def invoke(self, arguments, *, result=None, api_key="test-key", error=None, report_init_error=None):
        output = io.StringIO()
        self.stderr = io.StringIO()
        self.report = RecordingReport()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"TYPESAFE_API_KEY": api_key}, clear=True))
            stack.enter_context(patch.object(self.cli, "load_dotenv"))
            client = stack.enter_context(patch.object(self.cli, "TypeSafeClient"))
            self.http_client = stack.enter_context(patch.object(self.cli.httpx2, "Client"))
            self.report_factory = stack.enter_context(patch.object(
                self.cli, "RunReport", return_value=self.report, side_effect=report_init_error,
            ))
            driver = stack.enter_context(patch.object(self.cli, "CuaDriver"))
            self.driver_factory = driver
            pipeline = stack.enter_context(patch.object(self.cli, "run_pipeline", side_effect=error,
                                                        return_value=result or PipelineResult("completed")))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(self.stderr))
            status = self.cli.main(arguments)
        return status, json.loads(output.getvalue()), pipeline, client, driver

    def test_request_file_preserves_exact_utf8_text_and_line_endings(self):
        text = "\ufeff  Open résumé’s settings.\r\nSecond line.\n  "
        path = Path("request.txt")
        with patch.object(Path, "read_bytes", autospec=True, return_value=text.encode("utf-8")) as read_bytes:
            status, output, pipeline, client, driver = self.invoke(["--request-file", str(path)])
        read_bytes.assert_called_once_with(path)
        self.assertEqual(status, 0)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(pipeline.call_args.args[0], text)
        self.assertEqual(client.call_args.kwargs["model"], PINNED_MODEL)
        self.assertEqual(client.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(driver.call_args.kwargs["max_elements"], 5000)
        self.assertEqual(driver.call_args.kwargs["max_depth"], 25)
        self.assertEqual(self.report.events[0], {
            "event": "run_started", "request": text,
        })
        self.assertEqual(set(pipeline.call_args.kwargs), {"report"})

    def test_unreadable_request_file_still_leaves_a_closed_failure_report(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("sensitive-file-path")):
            status, output, pipeline, client, driver = self.invoke(["--request-file", "request.txt"])
        self.assertEqual(status, 1)
        self.assertEqual(self.report.events, [{"event": "run_error", **self.without_timing(output)}])
        self.assertNotIn("sensitive-file-path", json.dumps(self.report.events))
        self.assertTrue(self.report.closed)
        pipeline.assert_not_called()
        client.assert_not_called()
        driver.assert_not_called()

    def test_report_constructor_failure_stops_before_driver_or_network_with_safe_error(self):
        status, output, pipeline, client, driver = self.invoke(
            ["Find Save"], report_init_error=PermissionError("sensitive-report-directory"),
        )
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "error")
        self.assertIn("PermissionError", output["error"])
        self.assertNotIn("sensitive-report-directory", json.dumps(output) + self.stderr.getvalue())
        self.assertEqual(self.report.events, [])
        pipeline.assert_not_called()
        client.assert_not_called()
        driver.assert_not_called()
        self.http_client.assert_not_called()

    def test_missing_key_returns_json_without_instantiating_clients(self):
        status, output, pipeline, client, driver = self.invoke(["Find Save"], api_key="")
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "error")
        self.assertIn("TYPESAFE_API_KEY is missing", output["error"])
        pipeline.assert_not_called()
        client.assert_not_called()
        driver.assert_not_called()
        self.http_client.assert_not_called()
        self.assertEqual(self.report.events[-1], {"event": "run_error", **self.without_timing(output)})
        self.assertTrue(self.report.closed)

    def test_changed_model_pin_fails_before_network_or_driver_calls(self):
        with patch.object(self.cli.tomllib, "loads", return_value={"tool": {"ctrlloop": {"jev-model": "latest"}}}):
            status, output, pipeline, client, driver = self.invoke(["Find Save"])
        self.assertEqual(status, 1)
        self.assertIn(PINNED_MODEL, output["error"])
        pipeline.assert_not_called()
        client.assert_not_called()
        driver.assert_not_called()

    def test_no_candidate_result_has_distinct_exit_code_and_observation_json(self):
        status, output, _, _, _ = self.invoke(["Find Save"], result=PipelineResult("no_windows"))
        self.assertEqual(status, 2)
        self.assertEqual(output["status"], "no_windows")

    def test_completed_loop_forwards_once_and_returns_native_action_facts(self):
        native_result = {
            "effect": "unverifiable", "route": "accessibility", "delivery": {"mode": "background"},
        }
        result = PipelineResult(
            "completed", window=make_window(), control=make_element(),
            snapshot_id="snapshot-secret", action_result=native_result,
        )

        status, output, pipeline, _, driver = self.invoke(["Click Save"], result=result)

        self.assertEqual(status, 0)
        self.assertEqual(output, {**result.to_dict(), "timing": self.report.timing})
        pipeline.assert_called_once()
        self.assertEqual(pipeline.call_args.args[0], "Click Save")
        self.assertIs(pipeline.call_args.args[1], driver.return_value)
        self.assertEqual(set(pipeline.call_args.kwargs), {"report"})
        self.assertEqual(self.report.events, [
            {"event": "run_started", "request": "Click Save"},
            {"event": "run_finished", "result": result.to_dict()},
        ])
        self.assertTrue(self.report.closed)

    def test_unsupported_loop_target_returns_no_action_and_exit_two(self):
        result = PipelineResult(
            "no_action", window={"synthetic": True, "title": "Start Menu"},
            control={"synthetic": True, "label": "Search"},
            reason="Synthetic targets have no live CUA click target.",
        )

        status, output, pipeline, _, _ = self.invoke(["Open Start"], result=result)

        self.assertEqual(status, 2)
        self.assertEqual(output, {**result.to_dict(), "timing": self.report.timing})
        self.assertIsNone(output["action_result"])
        self.assertEqual(set(pipeline.call_args.kwargs), {"report"})
        self.assertEqual(self.report.events[-1], {"event": "run_finished", "result": result.to_dict()})
        self.assertTrue(self.report.closed)

    def test_click_refusal_keeps_native_result_and_selection_in_output_and_report(self):
        native_result = {
            "status": "refused", "refusal": {
                "code": "stale_element_token",
                "message": "element_token is stale; call get_window_state again to refresh",
            },
        }
        selected = PipelineResult(
            "selected", window=make_window(), control=make_element(), snapshot_id="snapshot-secret",
            selections={"windows": [], "controls": []},
        )
        error = DriverError(
            "CUA rejected the stale element token.", code="stale_element_token", tool="click", exit_code=0,
        )
        error.context.update(stage="click", action_result=native_result, selection=selected.to_dict())

        status, output, pipeline, _, _ = self.invoke(["Click Save"], error=error)

        self.assertEqual(status, 1)
        self.assertEqual(output, {
            "status": "error", "error": str(error), "code": "stale_element_token", "tool": "click",
            "exit_code": 0, "stage": "click", "action_result": native_result, "selection": selected.to_dict(),
            "timing": self.report.timing,
        })
        pipeline.assert_called_once()
        self.assertEqual(set(pipeline.call_args.kwargs), {"report"})
        self.assertEqual(self.report.events, [
            {"event": "run_started", "request": "Click Save"},
            {"event": "run_error", **self.without_timing(output)},
        ])
        self.assertTrue(self.report.closed)

    def test_completed_loop_keeps_stdout_and_exit_zero_when_final_report_write_fails(self):
        result = PipelineResult(
            "completed", window=make_window(), control=make_element(),
            action_result={
                "effect": "unverifiable", "route": "accessibility", "delivery": {"mode": "background"},
            },
        )
        for report_error in (OSError("private-report-path"), ValueError("private-report-state")):
            with self.subTest(error=type(report_error).__name__):
                def fail_close(report):
                    report.closed = True
                    raise report_error

                with patch.object(RecordingReport, "close", autospec=True, side_effect=fail_close) as close:
                    status, output, pipeline, _, _ = self.invoke(["Click Save"], result=result)

                self.assertEqual(status, 0)
                self.assertEqual(output, result.to_dict())
                pipeline.assert_called_once()
                self.assertEqual(self.report.events, [
                    {"event": "run_started", "request": "Click Save"},
                    {"event": "run_finished", "result": result.to_dict()},
                ])
                close.assert_called_once()
                self.assertIn("Could not save the completed run report", self.stderr.getvalue())
                self.assertNotIn("private-report", self.stderr.getvalue())
                self.assertTrue(self.report.closed)

    def test_os_errors_do_not_expose_raw_sensitive_message(self):
        status, output, _, _, _ = self.invoke(["Find Save"], error=OSError("secret-token-and-body"))
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "error")
        self.assertNotIn("secret-token-and-body", output["error"])
        self.assertIn("OSError", output["error"])
        self.assertNotIn("secret-token-and-body", json.dumps(self.report.events))

    def test_driver_failure_prints_safe_diagnostics_and_completed_window_selection(self):
        error = DriverError(
            "Window read failed.", code="driver_call_failed",
            tool="get_window_state", exit_code=1,
        )
        error.context.update({
            "stage": "get_window_state", "window": make_window(),
            "selections": {"windows": []},
        })
        status, output, pipeline, _, _ = self.invoke(["Find Save"], error=error)
        self.assertEqual(status, 1)
        self.assertEqual(output, {
            "status": "error", "error": "Window read failed.",
            "code": "driver_call_failed", "tool": "get_window_state", "exit_code": 1,
            "stage": "get_window_state", "window": make_window(),
            "selections": {"windows": []},
            "timing": self.report.timing,
        })
        pipeline.assert_called_once()
        self.assertEqual(self.report.events, [
            {"event": "run_started", "request": "Find Save"},
            {"event": "run_error", **self.without_timing(output)},
        ])
        self.assertTrue(self.report.closed)

    def test_success_attaches_wire_hooks_and_returns_report_timing_with_operational_result(self):
        result = PipelineResult("completed", window=make_window(), control=make_element())
        status, output, pipeline, client, _ = self.invoke(["Find Save"], result=result)

        self.assertEqual(status, 0)
        self.assertEqual(output, {**result.to_dict(), "timing": self.report.timing})
        self.assertEqual(self.report.events, [
            {"event": "run_started", "request": "Find Save"},
            {"event": "run_finished", "result": result.to_dict()},
        ])
        self.assertIs(pipeline.call_args.kwargs["report"], self.report)
        self.assertEqual(self.http_client.call_args.kwargs["event_hooks"], {
            "request": [self.report.on_request], "response": [self.report.on_response],
        })
        self.assertIs(client.call_args.kwargs["http_client"], self.http_client.return_value.__enter__.return_value)
        self.assertEqual(self.report_factory.call_args.args[0], Path(self.cli.__file__).resolve().parents[1] / "reports")
        self.assertIn(str(self.report.path), self.stderr.getvalue())
        self.assertTrue(self.report.closed)
        self.assertEqual(self.report.close_count, 1)

    def test_interrupt_keeps_terminal_event_and_closes_report_before_propagating(self):
        for error in (KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(error_type=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    self.invoke(["Find Save"], error=error)
                self.assertIs(raised.exception, error)
                self.assertEqual(self.report.events, [
                    {"event": "run_started", "request": "Find Save"},
                    {"event": "run_interrupted", "error_type": type(error).__name__},
                ])
                self.assertTrue(self.report.closed)

    def test_unexpected_error_keeps_its_type_and_closes_report_before_propagating(self):
        error = RuntimeError("sensitive-unexpected-message")
        with self.assertRaises(RuntimeError) as raised:
            self.invoke(["Find Save"], error=error)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.report.events[-1]["event"], "run_error")
        self.assertEqual(self.report.events[-1]["error_type"], "RuntimeError")
        self.assertNotIn("sensitive-unexpected-message", json.dumps(self.report.events))
        self.assertTrue(self.report.closed)

    def test_report_append_and_close_failures_do_not_replace_driver_error_or_interrupt(self):
        append_original = RecordingReport.append

        for original_error in (DriverError("Window read failed."), KeyboardInterrupt()):
            for append_error in (OSError("sensitive-write-error"), ValueError("closed report")):
                with self.subTest(original=type(original_error).__name__, append=type(append_error).__name__):
                    def append_until_failure(report, event, **data):
                        if event in {"run_error", "run_interrupted"}:
                            raise append_error
                        append_original(report, event, **data)

                    with patch.object(RecordingReport, "append", autospec=True, side_effect=append_until_failure):
                        with patch.object(RecordingReport, "close", side_effect=OSError("sensitive-close-error")) as close:
                            if isinstance(original_error, DriverError):
                                status, output, _, _, _ = self.invoke(["Find Save"], error=original_error)
                                self.assertEqual(status, 1)
                                self.assertEqual(output["error"], "Window read failed.")
                            else:
                                with self.assertRaises(KeyboardInterrupt) as raised:
                                    self.invoke(["Find Save"], error=original_error)
                                self.assertIs(raised.exception, original_error)
                    close.assert_called_once()
                    self.assertEqual(self.report.events, [
                        {"event": "run_started", "request": "Find Save"},
                    ])
                    self.assertIn("Could not buffer", self.stderr.getvalue())
                    self.assertIn("Could not write", self.stderr.getvalue())
                    self.assertNotIn("sensitive-write-error", self.stderr.getvalue())
                    self.assertNotIn("sensitive-close-error", self.stderr.getvalue())

    def test_blank_request_is_rejected_before_clients(self):
        status, output, pipeline, client, driver = self.invoke([" \r\n "])
        self.assertEqual(status, 1)
        self.assertIn("nonempty", output["error"])
        pipeline.assert_not_called()
        client.assert_not_called()
        driver.assert_not_called()

    @staticmethod
    def without_timing(output):
        return {key: value for key, value in output.items() if key != "timing"}


if __name__ == "__main__":
    unittest.main()
