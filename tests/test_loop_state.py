"""Offline regressions for one mutable state and bounded verification inputs."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.pipeline import control_candidates, run_pipeline
from src.reporting import RunReport
from src.selection import ChoiceSelector
from test_loop import ScriptedClient, ScriptedDriver, evidence_calls, snapshot
from test_pipeline import make_element, make_window
from test_pipeline_timing import FakeClock


class StateRecordingClient(ScriptedClient):
    """Keep object identity as well as frozen request records."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state_objects = []

    def system_one(self, **request):
        self.state_objects.append(request["state"])
        return super().system_one(**request)


class StateInspectingDriver(ScriptedDriver):
    """Inspect shared context at native-call entry before any action returns."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = None
        self.state_observations = []

    def _call(self, tool, arguments=None):
        shared_state = self.client.state_objects[0] if self.client.state_objects else None
        self.state_observations.append((tool, deepcopy(shared_state)))
        return super()._call(tool, arguments)


class LoopStateTests(unittest.TestCase):
    def run_loop(self, *, observations, windows=None, window_choices=("window_0",),
                 request="Save the document", confirmation="yes", none_stages=(), report=None):
        clock = FakeClock()
        timeline = []
        driver = StateInspectingDriver(clock, timeline, observations=observations, windows=windows)
        client = StateRecordingClient(
            clock, timeline, windows=window_choices, confirmation=confirmation, none_stages=none_stages,
        )
        driver.client = client
        result = run_pipeline(
            request, driver, ChoiceSelector(client), report=report,
            sleep=lambda seconds: clock.advance(round(seconds * 1000)),
        )
        return result, driver, client

    def test_one_shared_state_and_stable_iteration_across_window_control_and_evidence_rounds(self):
        request = "  Save my resum\u00e9.\r\nKeep this exact request.  "
        first = snapshot(1, "Save")
        first.update(app_name="Fresh Editor", window_title="Fresh document")
        first["elements"] = [make_element(index) for index in range(21)]
        second = snapshot(2, "Saved")
        second["elements"] = [{**make_element(index), "label": f"Evidence {index}"} for index in range(21)]
        windows = [make_window(index) for index in range(17)]
        result, driver, client = self.run_loop(
            request=request, windows=[windows], observations=[first, second],
            window_choices=("window_0", "window_9", "window_0"),
        )
        self.assertEqual(result.status, "action_verified")
        self.assertTrue(all(state is client.state_objects[0] for state in client.state_objects))
        window_calls = [call for call in client.calls
                        if any(key.startswith("windows_") for key in call["questions"])]
        controls = [call for call in client.calls
                    if any(key.startswith("controls_") for key in call["questions"])]
        evidence = evidence_calls(client)
        self.assertEqual([len(window_calls), len(controls), len(evidence)], [2, 2, 2])
        for call in client.calls:
            self.assertEqual(call["state"]["original_request"], request)
        for call in window_calls:
            self.assertEqual(call["state"]["iteration"], 1)
            self.assertIsNone(call["state"]["current_window"])
            self.assertIsNone(call["state"]["last_control"])
        for call in controls:
            self.assertEqual(call["state"]["iteration"], 1)
            self.assertEqual(call["state"]["current_window"], {
                "app_name": "Fresh Editor", "title": "Fresh document",
            })
            self.assertIsNone(call["state"]["last_control"])
        for call in evidence:
            self.assertEqual(call["state"]["iteration"], 2)
            self.assertEqual(call["state"]["last_control"]["status"], "(unverified)")
        self.assertTrue(any("_settling_" in key for key in controls[-1]["questions"]))
        self.assertTrue(any(set(question.criteria) == {"yes", "no"}
                            for question in evidence[-1]["questions"].values()))
        discoveries = [state for tool, state in driver.state_observations if tool == "list_windows"]
        self.assertEqual(discoveries[1]["iteration"], 2)

    def test_selected_control_is_unverified_before_click_then_verified_only_after_confirmation(self):
        result, driver, client = self.run_loop(observations=[snapshot(1, "Save"), snapshot(2, "Saved")])
        clicks = [state for tool, state in driver.state_observations if tool == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["last_control"]["control"]["label"], "Save")
        self.assertEqual(clicks[0]["last_control"]["status"], "(unverified)")
        self.assertEqual(clicks[0]["last_control"]["window"], clicks[0]["current_window"])
        evidence = evidence_calls(client)[0]
        self.assertEqual(evidence["state"]["last_control"]["status"], "(unverified)")
        self.assertEqual(result.last_control["status"], "(verified)")
        self.assertIs(result.last_control, client.state_objects[0]["last_control"])
        self.assertEqual(result.last_control["control"]["label"], "Save")

    def test_fresh_window_title_updates_context_without_rewriting_last_control_origin(self):
        first = {**make_window(), "title": "Selected document"}
        refreshed = {**first, "title": "Discovery title"}
        before = snapshot(1, "Save")
        before["window_title"] = "Observed unsaved document"
        after = snapshot(2, "Saved")
        after["window_title"] = "Observed saved document"
        _, driver, client = self.run_loop(windows=[[first], [refreshed]], observations=[before, after])
        activation = next(state for tool, state in driver.state_observations if tool == "bring_to_front")
        self.assertEqual(activation["current_window"]["title"], "Selected document")
        state = evidence_calls(client)[0]["state"]
        self.assertEqual(state["current_window"]["title"], "Observed saved document")
        self.assertEqual(state["last_control"]["window"]["title"], "Observed unsaved document")
        self.assertTrue(all(state is client.state_objects[0] for state in client.state_objects))

    def test_missing_window_updates_the_shared_state_and_retains_unverified_last_selection(self):
        result, driver, client = self.run_loop(
            windows=[[make_window()], []], observations=[snapshot(1, "Save")],
        )
        self.assertEqual(result.status, "no_evidence")
        shared_state = client.state_objects[0]
        self.assertIsNone(shared_state["current_window"])
        self.assertEqual(shared_state["iteration"], 2)
        self.assertTrue(shared_state["observation"]["window_missing"])
        self.assertEqual(shared_state["last_control"]["status"], "(unverified)")
        self.assertEqual(shared_state["last_control"]["control"]["label"], "Save")
        self.assertEqual(sum(tool == "get_window_state" for tool, _ in driver.calls), 1)
        self.assertEqual(sum(tool == "click" for tool, _ in driver.calls), 1)

    def test_each_fresh_observation_prepares_control_candidates_once(self):
        observations = [snapshot(1, "Save"), snapshot(2, "Saved")]
        with patch("src.pipeline.control_candidates", wraps=control_candidates) as prepare:
            result, _, _ = self.run_loop(observations=observations)
        self.assertEqual(result.status, "action_verified")
        self.assertEqual(prepare.call_count, len(observations))
        self.assertEqual([call.args[0] for call in prepare.call_args_list],
                         [observation["elements"] for observation in observations])

    def test_state_mutations_do_not_rewrite_earlier_report_or_selection_traces(self):
        with patch.object(RunReport, "_write_document") as write:
            with RunReport(Path(__file__).resolve().parents[1]) as report:
                result, _, client = self.run_loop(
                    observations=[snapshot(1, "Save"), snapshot(2, "Saved")], report=report,
                )
                client.state_objects[0]["iteration"] = 999
                client.state_objects[0]["last_control"]["control"]["label"] = "Later mutation"
        events = json.loads(write.call_args.args[0])
        first_selection = result.selections["windows"][0]["api_call"]["state"]
        self.assertEqual(first_selection["iteration"], 1)
        self.assertIsNone(first_selection["current_window"])
        self.assertIsNone(first_selection["last_control"])
        evidence_trace = result.selections["evidence"][0]["api_call"]["state"]
        self.assertEqual(evidence_trace["iteration"], 2)
        self.assertEqual(evidence_trace["last_control"]["status"], "(unverified)")
        self.assertEqual(evidence_trace["last_control"]["control"]["label"], "Save")
        verification = next(event for event in events if event["event"] == "action_verification")
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["last_control"]["status"], "(verified)")
        self.assertEqual(verification["last_control"]["control"]["label"], "Save")
        self.assertNotIn("Later mutation", json.dumps(events))

    def test_partial_observation_coverage_and_current_values_reach_evidence_questions(self):
        before = snapshot(1, "Save")
        after = snapshot(2, "Saved status")
        after.update(elements_complete=False, degraded=True, degraded_reason="Fixture partial read",
                     truncated=True, truncation_reason="Fixture limit", element_count=1,
                     total_element_count=99, filtered_element_count=98)
        after["elements"][0].update(value="Saved", selected=True, enabled=False)
        result, _, client = self.run_loop(observations=[before, after], confirmation="no")
        self.assertEqual(result.status, "action_unverified")
        evidence = evidence_calls(client)[0]
        observation = evidence["state"]["observation"]
        self.assertFalse(observation["elements_complete"])
        self.assertTrue(observation["degraded"])
        self.assertTrue(observation["truncated"])
        self.assertIn("bounded observation", observation["coverage"])
        for count_key in ("element_count", "total_element_count", "filtered_element_count", "observed_control_count"):
            self.assertNotIn(count_key, observation)
        group = next(question for question in evidence["questions"].values() if "control_0" in question.criteria)
        self.assertEqual(group.criteria["control_0"]["value"], "Saved")
        self.assertIs(group.criteria["control_0"]["selected"], True)
        self.assertIs(group.criteria["control_0"]["enabled"], False)
        confirmation = next(question for question in evidence["questions"].values()
                            if set(question.criteria) == {"yes", "no"})
        self.assertIn("partial", json.dumps(confirmation.instructions))
        self.assertEqual(result.last_control["status"], "(unverified)")

    def test_large_observation_keeps_shared_state_small_and_places_only_fresh_candidates_in_groups(self):
        before = snapshot(1, "Open report")
        before["elements"] = [
            {**make_element(index), "label": "Open report" if index == 0 else f"Old-only row {index}",
             "value": f"Old-only details {index}" if index else "Open report"}
            for index in range(402)
        ]
        after = snapshot(2, "Report")
        after["elements"] = [
            {**make_element(index), "label": f"Current row {index}", "value": f"Current value {index}"}
            for index in range(343)
        ]
        result, _, client = self.run_loop(observations=[before, after])
        self.assertEqual(result.status, "action_verified")
        evidence = evidence_calls(client)
        self.assertEqual([round_["candidate_count"] for round_ in result.selections["evidence"]], [343, 35, 4])
        first_groups = [question for question in evidence[0]["questions"].values()
                        if "none" in question.criteria]
        candidates = {key: description for question in first_groups
                      for key, description in question.criteria.items() if key != "none"}
        self.assertEqual(len(candidates), 343)
        self.assertEqual([candidates[f"control_{index}"]["label"] for index in range(343)],
                         [f"Current row {index}" for index in range(343)])
        for call in evidence:
            state = call["state"]
            self.assertEqual(set(state), {"original_request", "iteration", "current_window", "last_control",
                                          "observation", "last_action"})
            wire_state = json.dumps(state)
            self.assertNotIn("Old-only", wire_state)
            self.assertNotIn("Current row", wire_state)
            self.assertNotIn("Current value", wire_state)
            self.assertIn("Open report", wire_state)
            self.assertEqual(state["last_action"]["tool"], "click")
            self.assertEqual(state["last_action"]["requested_wait_ms_so_far"], 100)
            self.assertNotIn("private-token", wire_state)
            self.assertNotIn("private-snapshot", wire_state)


if __name__ == "__main__":
    unittest.main()
