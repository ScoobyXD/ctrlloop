"""Default loop behavior using scripted Jev, CUA, and a monotonic fake clock."""

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
LOOP_KEYS = {"changed", "done", "another_window"}


def snapshot(number, label):
    return {
        "snapshot_id": f"private-snapshot-{number}",
        "elements_complete": True,
        "elements": [{**make_element(), "label": label,
                      "element_token": f"private-token-{number}"}],
        "tree_markdown": "PRIVATE RAW TREE",
    }


def decision(*, changed=False, done=False, another_window=False):
    return {key: "yes" if value else "no" for key, value in (
        ("changed", changed), ("done", done), ("another_window", another_window),
    )}


class ScriptedClient:
    """Select observed options while requiring precisely the expected loop batches."""

    def __init__(self, clock, timeline, *, decisions=(), recoveries=(),
                 windows=("window_0",), timing="100ms", wrong_loop_model=False):
        self.clock = clock
        self.timeline = timeline
        self.decisions = deque(decisions)
        self.recoveries = deque(recoveries)
        self.windows = deque(windows)
        self.timing = timing
        self.wrong_loop_model = wrong_loop_model
        self.calls = []

    def system_one(self, **request):
        # Freeze the state as sent, even when local wait accounting changes later.
        self.calls.append(deepcopy(request))
        questions = request["questions"]
        keys = set(questions)
        self.timeline.append(("jev", self.clock(), tuple(questions)))
        chosen = {}
        if keys & LOOP_KEYS:
            if keys != LOOP_KEYS:
                raise AssertionError(f"Loop questions must share one call: {keys}")
            if not self.decisions:
                raise AssertionError("Unexpected additional loop judgment")
            chosen = self.decisions.popleft()
        elif "recovery" in keys or "wait_timing" in keys:
            if keys != {"recovery", "wait_timing"}:
                raise AssertionError(f"Recovery and conditional timing must be batched: {keys}")
            if not self.recoveries:
                raise AssertionError("Unexpected recovery judgment")
            recovery, timing = self.recoveries.popleft()
            chosen = {"recovery": recovery, "wait_timing": timing}
        answers = {}
        for key, question in questions.items():
            options = list(question.criteria)
            if key in chosen:
                value = chosen[key]
            elif key.startswith("windows_"):
                value = self.windows.popleft()
            elif set(options) == set(TIMING_OPTIONS):
                value = self.timing
            else:
                value = options[0]
            if value not in options:
                raise AssertionError(f"Scripted option {value!r} missing for {key}")
            answers[key] = SimpleNamespace(
                type="choice", choice=value, confidence=1,
                probabilities={option: float(option == value) for option in options},
            )
        self.clock.advance(5)
        return SimpleNamespace(
            model="unrecognized-model" if keys == LOOP_KEYS and self.wrong_loop_model else PINNED_MODEL,
            answers=answers, usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )


class ScriptedDriver:
    durations = {"list_windows": 11, "bring_to_front": 13, "get_window_state": 17, "click": 19}

    def __init__(self, clock, timeline, *, observations, windows=None, click_error=None):
        self.clock = clock
        self.timeline = timeline
        self.observations = deque(observations)
        self.windows = deque(windows or [[make_window()]])
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

    def run_loop(self, *, observations=None, windows=None, decisions=(), recoveries=(),
                 window_choices=("window_0",), timing="100ms",
                 original_request="Click Save, then confirm the document was saved"):
        driver = ScriptedDriver(
            self.clock, self.timeline,
            observations=observations or [snapshot(1, "Save"), snapshot(2, "Saved")],
            windows=windows,
        )
        client = ScriptedClient(self.clock, self.timeline, decisions=decisions,
                                recoveries=recoveries, windows=window_choices, timing=timing)
        with RunReport(self.directory) as report:
            result = run_pipeline(original_request, driver, ChoiceSelector(client),
                                  report=report, sleep=self.sleep)
            report.append("run_finished", result=result.to_dict())
        events = json.loads(report.path.read_text(encoding="utf-8"))
        return result, driver, client, events

    @staticmethod
    def tool_calls(driver, tool):
        return [arguments for called, arguments in driver.calls if called == tool]

    @staticmethod
    def loop_calls(client):
        return [call for call in client.calls if set(call["questions"]) == LOOP_KEYS]

    def test_first_pass_skips_loop_judgment_and_done_overrides_unchanged(self):
        request = "  Save my résumé.\nKeep the original request.  "
        result, driver, client, events = self.run_loop(
            original_request=request, decisions=[decision(done=True, another_window=True)],
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(self.loop_calls(client)), 1)
        ordered = [name for name, _, _ in self.timeline]
        self.assertEqual(ordered, [
            "list_windows", "jev", "bring_to_front", "get_window_state", "jev",
            "click", "sleep", "list_windows", "get_window_state", "jev",
        ])
        self.assertFalse(any("recovery" in call["questions"] for call in client.calls))
        loop_call = self.loop_calls(client)[0]
        for question in loop_call["questions"].values():
            self.assertEqual(set(question.criteria), {"yes", "no"})
        wire_state = json.dumps(loop_call["state"], ensure_ascii=False)
        self.assertIn(json.dumps(request, ensure_ascii=False)[1:-1], wire_state)
        self.assertIn("Save", wire_state)
        self.assertIn("Saved", wire_state)
        self.assertIn("click", wire_state)
        self.assertIn("unverifiable", wire_state)
        for call in client.calls:
            self.assertEqual(call["model"], PINNED_MODEL)
            wire = json.dumps({"state": call["state"], "questions": {
                key: question.model_dump() for key, question in call["questions"].items()
            }})
            for local_only in ("private-snapshot", "private-token", "PRIVATE RAW TREE",
                               '"element_index"', '"element_token"', '"snapshot_id"'):
                self.assertNotIn(local_only, wire)
        self.assertEqual(len(result.iterations), 2)
        self.assertTrue(any(event["event"] == "loop_judgment" for event in events))

    def test_settling_timer_starts_after_native_return_before_fresh_observation(self):
        result, driver, client, events = self.run_loop(decisions=[decision(done=True)], timing=">500ms")
        self.assertEqual(result.status, "completed")
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
        self.assertEqual([event["duration_ms"] for event in timings
                          if event["operation"] == "control_acquisition"], [17, 17])
        total = sum((end - start) / 1_000_000 for _, start, end in driver.intervals)
        total += 5 * len(client.calls) + 500
        self.assertEqual(events[-1]["timing"]["duration_ms"], total)

        traces = [round_["api_call"] for rounds in result.selections.values() for round_ in rounds]
        traces.extend(event["api_call"] for event in events if event["event"] == "loop_judgment")
        traces.sort(key=lambda trace: trace["sdk_span_id"])
        self.assertEqual(len(traces), len(client.calls))
        for trace, call in zip(traces, client.calls, strict=True):
            self.assertEqual(trace["state"], call["state"])
            self.assertEqual(set(trace["questions"]), set(call["questions"]))
            for key, question in call["questions"].items():
                self.assertEqual(trace["questions"][key]["instructions"], question.instructions)
                self.assertEqual(trace["questions"][key]["criteria"], question.criteria)
                answer = trace["answers"][key]
                self.assertEqual(set(answer["probabilities"]), set(question.criteria))
                self.assertEqual(answer["probabilities"][answer["choice"]], 1)
        self.assertEqual([event["iteration"] for event in timings
                          if event["operation"] == "loop.iteration"], [1, 2])

    def test_changed_current_window_reuses_fresh_snapshot_and_no_window_reselection(self):
        fresh_windows = [[{**make_window(), "title": title}] for title in ("Editing", "Saving", "Saved")]
        result, driver, client, _ = self.run_loop(
            windows=fresh_windows,
            observations=[snapshot(1, "Save"), snapshot(2, "Confirm"), snapshot(3, "Saved")],
            decisions=[decision(changed=True), decision(done=True)],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual([call["element_token"] for call in self.tool_calls(driver, "click")],
                         ["private-token-1", "private-token-2"])
        self.assertEqual(len(self.tool_calls(driver, "bring_to_front")), 1)
        self.assertEqual(sum(any(key.startswith("windows_") for key in call["questions"])
                             for call in client.calls), 1)
        self.assertEqual([window["title"] for window in self.tool_calls(driver, "get_window_state")],
                         ["Editing", "Saving", "Saved"])
        self.assertEqual(len(result.iterations), 3)
        self.assertEqual(len(result.selections["controls"]), 2)

    def test_changed_other_window_selects_then_observes_new_target(self):
        first = make_window()
        next_first = {**first, "title": "Export opened"}
        next_window = {**make_window(1), "app_name": "Export Tool", "title": "Confirm export"}
        final_window = {**next_window, "title": "Export complete"}
        result, driver, client, _ = self.run_loop(
            windows=[[first], [next_first, next_window], [next_first, final_window]],
            observations=[snapshot(1, "Export"), snapshot(2, "Opened"),
                          snapshot(3, "Confirm export"), snapshot(4, "Export complete")],
            decisions=[decision(changed=True, another_window=True), decision(done=True)],
            window_choices=("window_0", "window_1"),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual([call["window_id"] for call in self.tool_calls(driver, "click")],
                         [first["window_id"], next_window["window_id"]])
        self.assertEqual([call["element_token"] for call in self.tool_calls(driver, "click")],
                         ["private-token-1", "private-token-3"])
        self.assertEqual(len(self.tool_calls(driver, "bring_to_front")), 2)
        observed = self.tool_calls(driver, "get_window_state")
        self.assertIs(observed[1], next_first)
        self.assertIs(observed[2], next_window)
        self.assertIs(observed[3], final_window)
        self.assertEqual(sum(any(key.startswith("windows_") for key in call["questions"])
                             for call in client.calls), 2)

    def test_unchanged_wait_observes_again_without_repeating_action(self):
        result, driver, client, _ = self.run_loop(
            observations=[snapshot(1, "Save"), snapshot(2, "Saving"), snapshot(3, "Saved")],
            decisions=[decision(), decision(done=True)], recoveries=[("wait", "400ms")],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 3)
        self.assertEqual([item[2] for item in self.timeline if item[0] == "sleep"], [0.1, 0.4])
        recovery = next(call for call in client.calls if "recovery" in call["questions"])
        self.assertEqual(set(recovery["questions"]), {"recovery", "wait_timing"})
        self.assertEqual(set(recovery["questions"]["recovery"].criteria), {"wait", "error"})
        self.assertEqual(tuple(recovery["questions"]["wait_timing"].criteria), TIMING_OPTIONS)
        self.assertEqual([call["state"]["last_action"]["requested_wait_ms_so_far"]
                          for call in self.loop_calls(client)], [100, 500])
        self.assertEqual(len(result.iterations), 3)

    def test_unchanged_error_stops_without_consuming_conditional_wait(self):
        result, driver, client, _ = self.run_loop(
            decisions=[decision()], recoveries=[("error", ">500ms")],
        )
        self.assertEqual(result.status, "unchanged_error")
        self.assertTrue(result.reason)
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual([item[2] for item in self.timeline if item[0] == "sleep"], [0.1])
        self.assertEqual(len(self.loop_calls(client)), 1)

    def test_default_executes_and_continues_past_twenty_passes_until_done(self):
        result, driver, client, _ = self.run_loop(
            observations=[snapshot(number, f"Step {number}") for number in range(1, 27)],
            decisions=[decision(changed=True) for _ in range(24)] + [decision(done=True)],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 26)
        self.assertEqual(len(self.tool_calls(driver, "click")), 25)
        self.assertEqual(len(self.loop_calls(client)), 25)
        self.assertEqual(len(result.iterations), 26)
        self.assertEqual([call["element_token"] for call in self.tool_calls(driver, "click")],
                         [f"private-token-{number}" for number in range(1, 26)])

    def test_repeated_waits_continue_past_twenty_passes_without_action_replay(self):
        result, driver, client, _ = self.run_loop(
            observations=[snapshot(number, "Loading") for number in range(1, 27)],
            decisions=[decision() for _ in range(24)] + [decision(done=True)],
            recoveries=[("wait", "<50ms") for _ in range(24)],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 26)
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(result.iterations), 26)
        self.assertEqual(len(self.loop_calls(client)), 25)
        self.assertEqual([item[2] for item in self.timeline if item[0] == "sleep"],
                         [0.1] + [0.05] * 24)
        self.assertEqual(self.loop_calls(client)[-1]["state"]["last_action"]["requested_wait_ms_so_far"],
                         1300)

    def test_native_refusal_or_unknown_delivery_propagates_without_wait_or_replay(self):
        for code in ("background_unavailable", "driver_timeout", "invalid_action_result"):
            with self.subTest(code=code):
                self.timeline.clear()
                error = DriverError("Fixture native action failure", code=code, tool="click")
                driver = ScriptedDriver(self.clock, self.timeline,
                                        observations=[snapshot(1, "Save")], click_error=error)
                client = ScriptedClient(self.clock, self.timeline)
                with self.assertRaises(DriverError) as raised:
                    run_pipeline("Save document", driver, ChoiceSelector(client),
                                 sleep=self.sleep)
                self.assertIs(raised.exception, error)
                self.assertEqual(error.context["stage"], "click")
                self.assertEqual(len(self.tool_calls(driver, "click")), 1)
                self.assertEqual(len(self.tool_calls(driver, "get_window_state")), 1)
                self.assertFalse(any(item[0] == "sleep" for item in self.timeline))
                self.assertEqual(self.loop_calls(client), [])

    def test_loop_judgment_keeps_pinned_model_validation(self):
        driver = ScriptedDriver(self.clock, self.timeline,
                                observations=[snapshot(1, "Save"), snapshot(2, "Saved")])
        client = ScriptedClient(self.clock, self.timeline, decisions=[decision(done=True)],
                                wrong_loop_model=True)
        with self.assertRaises(SelectionError):
            run_pipeline("Save document", driver, ChoiceSelector(client),
                         sleep=self.sleep)
        self.assertEqual(len(self.tool_calls(driver, "click")), 1)
        self.assertEqual(len(self.loop_calls(client)), 1)

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
                self.assertEqual(self.loop_calls(client), [])
                self.assertEqual(sum(item[0] == "sleep" for item in self.timeline),
                                 1 if boundary == "wait" else 0)


if __name__ == "__main__":
    unittest.main()
