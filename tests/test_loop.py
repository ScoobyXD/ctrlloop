"""Action selection and fresh-evidence verification with no live Jev or CUA."""

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.driver import DriverError
from src.pipeline import run_pipeline
from src.reporting import RunReport
from src.selection import ChoiceSelector, PINNED_MODEL, SelectionError
from test_driver import foreground_result
from test_pipeline import make_element, make_window
from test_pipeline_timing import FakeClock


TIMING_OPTIONS = ("<50ms", "100ms", "200ms", "300ms", "400ms", ">500ms")
OBSOLETE_QUESTIONS = {"changed", "done", "another_window", "recovery", "wait_timing"}


def snapshot(number, label):
    return {
        "snapshot_id": f"private-snapshot-{number}",
        "elements_complete": True,
        "elements": [{**make_element(), "label": label,
                      "element_token": f"private-token-{number}"}],
        "tree_markdown": "PRIVATE RAW TREE",
    }


def evidence_calls(client):
    return [call for call in client.calls
            if any(key.startswith("evidence_") for key in call["questions"])]


class ScriptedClient:
    """Choose fixture candidates (or inject invalid none) and freeze request state."""

    def __init__(self, clock, timeline, *, windows=("window_0",), timing="100ms",
                 confirmation="yes", none_stages=(), evidence_choices=(), wrong_evidence_model=False):
        self.clock = clock
        self.timeline = timeline
        self.windows = deque(windows)
        self.timing = timing
        self.confirmation = confirmation
        self.none_stages = set(none_stages)
        self.evidence_choices = deque(evidence_choices)
        self.wrong_evidence_model = wrong_evidence_model
        self.calls = []

    def system_one(self, **request):
        self.calls.append(deepcopy(request))
        questions = request["questions"]
        if OBSOLETE_QUESTIONS & set(questions):
            raise AssertionError("Removed routing/recovery questions were sent")
        self.timeline.append(("jev", self.clock(), tuple(questions)))
        answers = {}
        for key, question in questions.items():
            options = list(question.criteria)
            stage = key.split("_", 1)[0]
            if set(options) == set(TIMING_OPTIONS):
                value = self.timing
            elif set(options) == {"yes", "no"}:
                value = self.confirmation
            elif stage == "windows":
                value = self.windows.popleft()
            elif stage == "evidence" and self.evidence_choices:
                value = self.evidence_choices.popleft()
            elif stage in self.none_stages:
                value = "none"
            else:
                value = next(option for option in options if option.startswith("control_"))
            if value not in options and value != "none":
                raise AssertionError(f"Scripted option {value!r} missing for {key}")
            answers[key] = SimpleNamespace(
                type="choice", choice=value, confidence=1,
                probabilities={option: float(option == value) for option in options},
            )
        self.clock.advance(5)
        wrong_model = self.wrong_evidence_model and any(key.startswith("evidence_") for key in questions)
        return SimpleNamespace(
            model="unrecognized-model" if wrong_model else PINNED_MODEL,
            answers=answers, usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )


class ScriptedDriver:
    durations = {"list_windows": 11, "bring_to_front": 13, "get_window_state": 17, "click": 19}

    def __init__(self, clock, timeline, *, observations, windows=None, click_error=None):
        self.clock = clock
        self.timeline = timeline
        self.observations = deque(observations)
        self.windows = deque(windows if windows is not None else [[make_window()]])
        self.current_windows = None
        self.calls = []
        self.intervals = []
        self.click_error = click_error
        self.native_result = {"effect": "unverifiable", "route": "accessibility",
                              "delivery": {"mode": "background"}}

    def _call(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        started = self.clock()
        self.timeline.append((tool, started, arguments))
        self.clock.advance(self.durations[tool])
        self.intervals.append((tool, started, self.clock()))

    def list_windows(self):
        self._call("list_windows")
        if self.windows:
            self.current_windows = self.windows.popleft()
        return self.current_windows

    def bring_to_front(self, arguments):
        self._call("bring_to_front", arguments)
        return foreground_result(arguments)

    def get_window_state(self, window):
        self._call("get_window_state", window)
        if not any(window is item for item in self.current_windows):
            raise AssertionError("Observation must use the freshly discovered window record")
        if not self.observations:
            raise AssertionError("Unexpected extra observation")
        return self.observations.popleft()

    def click(self, arguments):
        self._call("click", arguments)
        if self.click_error is not None:
            raise self.click_error
        return self.native_result


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.timeline = []
        self.workspace = Path(__file__).resolve().parents[1]
        self.directory = self.workspace / f".loop-tests-{uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(self.cleanup_directory)
        clock_patch = patch("src.reporting.perf_counter_ns", side_effect=self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def cleanup_directory(self):
        target = self.directory.resolve()
        if target.parent != self.workspace or not target.name.startswith(".loop-tests-"):
            raise ValueError("Unexpected test cleanup path")
        shutil.rmtree(target)

    def sleep(self, seconds):
        self.timeline.append(("sleep", self.clock(), seconds))
        self.clock.advance(round(seconds * 1000))

    def run_loop(self, *, observations=None, windows=None, window_choices=("window_0",),
                 timing="100ms", confirmation="yes", none_stages=(), evidence_choices=(),
                 original_request="Click Save, then confirm the document was saved"):
        driver = ScriptedDriver(
            self.clock, self.timeline,
            observations=observations if observations is not None else [snapshot(1, "Save"), snapshot(2, "Saved")],
            windows=windows,
        )
        client = ScriptedClient(
            self.clock, self.timeline, windows=window_choices, timing=timing,
            confirmation=confirmation, none_stages=none_stages, evidence_choices=evidence_choices,
        )
        with RunReport(self.directory) as report:
            result = run_pipeline(original_request, driver, ChoiceSelector(client),
                                  report=report, sleep=self.sleep)
            report.append("run_finished", result=result.to_dict())
        events = json.loads(report.path.read_text(encoding="utf-8"))
        return result, driver, client, events

    @staticmethod
    def tool_calls(driver, tool):
        return [arguments for called, arguments in driver.calls if called == tool]

    def test_one_action_then_fresh_evidence_selection_and_confirmation(self):
        request = "  Save my resum\u00e9.\nThen export it.  "
        result, driver, client, events = self.run_loop(original_request=request)
        self.assertEqual(result.status, "action_verified")
        self.assertEqual(result.last_control["status"], "(verified)")
        self.assertEqual(len(result.iterations), 2)
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual([name for name, _, _ in self.timeline], [
            "list_windows", "jev", "bring_to_front", "get_window_state", "jev",
            "click", "sleep", "list_windows", "get_window_state", "jev",
        ])
        evidence = evidence_calls(client)
        self.assertEqual(len(evidence), 1)
        self.assertTrue(any(set(question.criteria) == {"yes", "no"}
                            for question in evidence[0]["questions"].values()))
        for call in client.calls:
            self.assertEqual(call["model"], PINNED_MODEL)
            self.assertEqual(call["state"]["original_request"], request)
            wire = json.dumps({"state": call["state"], "questions": {
                key: question.model_dump() for key, question in call["questions"].items()
            }})
            for local_only in ("private-snapshot", "private-token", "PRIVATE RAW TREE",
                               '"element_index"', '"element_token"', '"snapshot_id"'):
                self.assertNotIn(local_only, wire)
            self.assertTrue(OBSOLETE_QUESTIONS.isdisjoint(call["questions"]))
        finished = next(event for event in events if event["event"] == "run_finished")
        self.assertEqual(finished["result"]["status"], "action_verified")

    def test_settling_timer_starts_after_native_return_and_timing_accounts_for_each_call_once(self):
        result, driver, client, events = self.run_loop(timing=">500ms")
        self.assertEqual(result.status, "action_verified")
        click_interval = next(interval for interval in driver.intervals if interval[0] == "click")
        sleep = next(item for item in self.timeline if item[0] == "sleep")
        self.assertEqual(sleep[1], click_interval[2])
        self.assertEqual(sleep[2], 0.5)
        discoveries = [item for item in driver.intervals if item[0] == "list_windows"]
        self.assertEqual(discoveries[1][1], click_interval[2] + 500_000_000)
        timings = [event for event in events if event["event"] == "timing_finished"]
        settling = [event for event in timings if event["operation"] == "settling_wait"]
        self.assertEqual([event["duration_ms"] for event in settling], [500])
        native = next(event for event in timings if event["operation"] == "native_action")
        self.assertEqual(native["duration_ms"], 19)
        self.assertEqual(settling[0]["started_elapsed_ms"], native["finished_elapsed_ms"])
        self.assertEqual([event["duration_ms"] for event in timings
                          if event["operation"] == "selection.sdk_call"], [5] * len(client.calls))
        total = sum((end - start) / 1_000_000 for _, start, end in driver.intervals)
        total += 5 * len(client.calls) + 500
        self.assertEqual(events[-1]["timing"]["duration_ms"], total)
        self.assertEqual([event["iteration"] for event in timings
                          if event["operation"] == "loop.iteration"], [1, 2])
        traces = [round_["api_call"] for rounds in result.selections.values() for round_ in rounds]
        traces.sort(key=lambda trace: trace["sdk_span_id"])
        self.assertEqual(len(traces), len(client.calls))
        for trace, call in zip(traces, client.calls, strict=True):
            self.assertEqual(trace["state"], call["state"])
            self.assertEqual(set(trace["questions"]), set(call["questions"]))

    def test_each_evidence_group_advances_to_confirmation_without_second_action(self):
        after = snapshot(2, "No evidence")
        after["elements"] = [make_element(index) for index in range(21)]
        result, driver, client, _ = self.run_loop(
            observations=[snapshot(1, "Save"), after], confirmation="no",
        )
        self.assertEqual(result.status, "action_unverified")
        self.assertEqual(result.last_control["status"], "(unverified)")
        evidence = evidence_calls(client)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(len(evidence[0]["questions"]), 3)
        self.assertFalse(any(set(question.criteria) == {"yes", "no"}
                             for question in evidence[0]["questions"].values()))
        self.assertEqual([round_["candidate_count"] for round_ in result.selections["evidence"]], [21, 3])
        self.assertTrue(any(set(question.criteria) == {"yes", "no"}
                            for question in evidence[-1]["questions"].values()))
        self.assertTrue(all("none" not in question.criteria
                            for call in evidence for question in call["questions"].values()))
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 2)
        self.assertEqual([item[2] for item in self.timeline if item[0] == "sleep"], [0.1])

    def test_invalid_none_evidence_cannot_verify_or_repeat_action(self):
        driver = ScriptedDriver(self.clock, self.timeline,
                                observations=[snapshot(1, "Save"), snapshot(2, "Saved")])
        client = ScriptedClient(self.clock, self.timeline, none_stages={"evidence"}, confirmation="yes")
        with self.assertRaisesRegex(SelectionError, "unknown option label"):
            run_pipeline("Save document", driver, ChoiceSelector(client), sleep=self.sleep)
        self.assertEqual(len(evidence_calls(client)), 1)
        self.assertEqual(evidence_calls(client)[0]["state"]["last_control"]["status"], "(unverified)")
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)

    def test_rejected_confirmation_does_not_mark_verified_or_repeat_action(self):
        result, driver, client, _ = self.run_loop(confirmation="no")
        self.assertEqual(result.status, "action_unverified")
        self.assertEqual(result.last_control["status"], "(unverified)")
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(evidence_calls(client)), 1)
        self.assertEqual([item[2] for item in self.timeline if item[0] == "sleep"], [0.1])

    def test_invalid_none_control_prevents_click_wait_and_verification(self):
        driver = ScriptedDriver(self.clock, self.timeline, observations=[snapshot(1, "Save")])
        client = ScriptedClient(self.clock, self.timeline, none_stages={"controls"})
        with self.assertRaisesRegex(SelectionError, "unknown option label"):
            run_pipeline("Save document", driver, ChoiceSelector(client), sleep=self.sleep)
        self.assertEqual(self.tool_calls(driver, "click"), [])
        self.assertEqual(evidence_calls(client), [])
        self.assertFalse(any(item[0] == "sleep" for item in self.timeline))

    def test_empty_or_unavailable_post_action_observation_cannot_verify(self):
        for after in ({"snapshot_id": "empty", "elements": []},
                      {"snapshot_id": "partial", "elements": [], "degraded": True},
                      {"snapshot_id": "unavailable"}, {"elements": [make_element()]}):
            with self.subTest(observation=after):
                self.timeline.clear()
                result, driver, client, _ = self.run_loop(observations=[snapshot(1, "Save"), after])
                self.assertNotEqual(result.status, "action_verified")
                self.assertEqual(result.last_control["status"], "(unverified)")
                self.assertEqual(evidence_calls(client), [])
                self.assertEqual(len(self.tool_calls(driver, "click")), 1)

    def test_missing_selected_window_stops_without_querying_absent_handle_or_selecting_another(self):
        result, driver, client, _ = self.run_loop(
            windows=[[make_window()], [make_window(1)]], observations=[snapshot(1, "Save")],
        )
        self.assertEqual(result.status, "no_evidence")
        self.assertEqual(result.last_control["status"], "(unverified)")
        self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 1)
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(evidence_calls(client), [])
        self.assertEqual(sum(any(key.startswith("windows_") for key in call["questions"])
                             for call in client.calls), 1)

    def test_native_delivery_errors_propagate_without_wait_or_verification(self):
        for code in ("background_unavailable", "driver_timeout", "invalid_action_result"):
            with self.subTest(code=code):
                self.timeline.clear()
                error = DriverError("Fixture native action failure", code=code, tool="click")
                driver = ScriptedDriver(self.clock, self.timeline,
                                        observations=[snapshot(1, "Save")], click_error=error)
                client = ScriptedClient(self.clock, self.timeline)
                with self.assertRaises(DriverError) as raised:
                    run_pipeline("Save document", driver, ChoiceSelector(client), sleep=self.sleep)
                self.assertIs(raised.exception, error)
                self.assertEqual(error.context["stage"], "click")
                self.assertEqual(len(self.tool_calls(driver, "click")), 1)
                self.assertFalse(any(item[0] == "sleep" for item in self.timeline))
                self.assertEqual(evidence_calls(client), [])

    def test_evidence_round_keeps_pinned_model_validation(self):
        driver = ScriptedDriver(self.clock, self.timeline,
                                observations=[snapshot(1, "Save"), snapshot(2, "Saved")])
        client = ScriptedClient(self.clock, self.timeline, wrong_evidence_model=True)
        with self.assertRaises(SelectionError):
            run_pipeline("Save document", driver, ChoiceSelector(client), sleep=self.sleep)
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(evidence_calls(client)), 1)

    def test_interrupt_during_action_or_wait_propagates_without_retry_or_observation(self):
        for boundary in ("action", "wait"):
            with self.subTest(boundary=boundary):
                self.timeline.clear()
                interruption = KeyboardInterrupt()
                driver = ScriptedDriver(
                    self.clock, self.timeline, observations=[snapshot(1, "Save")],
                    click_error=interruption if boundary == "action" else None,
                )
                client = ScriptedClient(self.clock, self.timeline)

                def interrupted_sleep(seconds):
                    self.timeline.append(("sleep", self.clock(), seconds))
                    raise interruption

                with self.assertRaises(KeyboardInterrupt) as raised:
                    run_pipeline("Save document", driver, ChoiceSelector(client), sleep=interrupted_sleep)
                self.assertIs(raised.exception, interruption)
                self.assertEqual(len(self.tool_calls(driver, "click")), 1)
                self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 1)
                self.assertEqual(evidence_calls(client), [])
                self.assertEqual(sum(item[0] == "sleep" for item in self.timeline),
                                 1 if boundary == "wait" else 0)


if __name__ == "__main__":
    unittest.main()
