"""Restoration ordering and native refusal tests; no desktop actions."""

import json
import subprocess
import unittest
from unittest.mock import patch

from src.driver import CuaDriver, DriverError
from pipeline_fixtures import select_once
from src.selection import ChoiceSelector
from test_driver import FakeRunner, window, state, element, foreground_result
from test_pipeline import ChoosingClient, RecordingReport
from test_click import ACTION_RESULT


RESTORED = {
    "previous_fg_hwnd": "0x123", "now_fg_hwnd": hex(123456),
    "target_hwnd": hex(123456), "landed_on_target": True,
    "raised": True, "restored": True,
}


class RestoreTests(unittest.TestCase):
    def test_restore_precedes_fresh_snapshot_and_click(self):
        runner = FakeRunner(
            {"windows": [window(minimized=True)]}, RESTORED,
            state(elements=[element(element_token="after-restore")]), ACTION_RESULT,
        )
        report = RecordingReport()
        result = select_once("open slack", CuaDriver(executable="fake", runner=runner),
                              ChoiceSelector(ChoosingClient()), deliver_click=True, report=report)
        self.assertEqual([call[0][-1] for call in runner.calls],
                         ["list_windows", "bring_to_front", "get_window_state", "click"])
        self.assertEqual(json.loads(runner.calls[1][1]["input"]),
                         {"pid": 4242, "window_id": 123456})
        self.assertEqual(json.loads(runner.calls[3][1]["input"])["element_token"], "after-restore")
        self.assertEqual(result.action_result, ACTION_RESULT)
        returned = [e for e in report.events if e["event"] == "action_returned"]
        self.assertEqual(returned[0]["tool"], "bring_to_front")
        self.assertEqual(returned[0]["action_result"], RESTORED)

    def test_observation_stage_brings_minimized_window_forward(self):
        runner = FakeRunner({"windows": [window(minimized=True)]}, RESTORED, state())
        select_once("open slack", CuaDriver(executable="fake", runner=runner),
                     ChoiceSelector(ChoosingClient()))
        self.assertEqual([c[0][-1] for c in runner.calls], ["list_windows", "bring_to_front", "get_window_state"])

    def test_every_window_state_is_brought_forward_before_later_stages(self):
        for deliver_click in (False, True):
            for metadata in ({}, {"minimized": False}, {"minimized": None}, {"minimized": True}):
                with self.subTest(deliver_click=deliver_click, metadata=metadata):
                    selected = window(**metadata)
                    runner = FakeRunner({"windows": [selected]}, foreground_result(selected),
                                        state(elements=[element(element_token="fresh")]), ACTION_RESULT)
                    select_once("open slack", CuaDriver(executable="fake", runner=runner),
                                 ChoiceSelector(ChoosingClient()), deliver_click=deliver_click)
                    self.assertEqual([c[0][-1] for c in runner.calls],
                                     ["list_windows", "bring_to_front", "get_window_state"]
                                     + (["click"] if deliver_click else []))

    def test_only_final_window_winner_is_activated_before_control_questions(self):
        windows = [window(window_id=123456 + i) for i in range(12)]
        selected = windows[-1]
        runner = FakeRunner({"windows": windows}, foreground_result(selected),
                            state(window_id=selected["window_id"], elements=[element(), element(element_index=18)]))
        driver = CuaDriver(executable="fake", runner=runner)
        client = ChoosingClient()
        timeline = []
        run_native = runner.__call__
        ask = client.system_one

        def native_call(args, **kwargs):
            timeline.append(args[-1])
            return run_native(args, **kwargs)

        def question_call(**kwargs):
            timeline.append(next(iter(kwargs["questions"])).split("_r")[0])
            return ask(**kwargs)

        driver._runner = native_call
        with patch.object(client, "system_one", side_effect=question_call):
            select_once("open slack", driver, ChoiceSelector(client))

        self.assertEqual(timeline, ["list_windows", "windows", "windows", "bring_to_front",
                                    "get_window_state", "controls"])
        self.assertEqual(json.loads(runner.calls[1][1]["input"]),
                         {"pid": selected["pid"], "window_id": selected["window_id"]})

    def test_unreadable_activation_response_stops_without_retry(self):
        for response in (
            subprocess.CompletedProcess([], 0, "foreground_unavailable", ""),
            subprocess.CompletedProcess([], 0, "{broken", ""),
            subprocess.TimeoutExpired("fake", 60),
        ):
            runner = FakeRunner({"windows": [window()]}, response)
            with self.assertRaises(DriverError):
                select_once("open slack", CuaDriver(executable="fake", runner=runner),
                             ChoiceSelector(ChoosingClient()))
            self.assertEqual([c[0][-1] for c in runner.calls], ["list_windows", "bring_to_front"])

    def test_restore_refusal_stops_before_snapshot_and_control_selection(self):
        refusal = {**RESTORED, "landed_on_target": False, "code": "foreground_unavailable",
                   "effect": "refused"}
        for exit_code in (0, 1):
            runner = FakeRunner({"windows": [window(minimized=True)]},
                                subprocess.CompletedProcess([], exit_code, json.dumps(refusal), ""))
            client = ChoosingClient()
            with self.assertRaises(DriverError) as raised:
                select_once("open slack", CuaDriver(executable="fake", runner=runner),
                             ChoiceSelector(client), deliver_click=True)
            self.assertEqual(raised.exception.code, "foreground_unavailable")
            self.assertEqual(raised.exception.context["stage"], "bring_to_front")
            self.assertEqual(raised.exception.context["action_result"], refusal)
            self.assertEqual([c[0][-1] for c in runner.calls], ["list_windows", "bring_to_front"])
            self.assertEqual(len(client.calls), 1)

    def test_missing_or_wrong_target_confirmation_is_not_success(self):
        for payload in ({}, {**RESTORED, "now_fg_hwnd": "0x999"},
                        {**RESTORED, "landed_on_target": False}):
            driver = CuaDriver(executable="fake", runner=FakeRunner(payload))
            with self.assertRaises(DriverError) as raised:
                driver.bring_to_front({"pid": 4242, "window_id": 123456})
            self.assertEqual(raised.exception.context["action_result"], payload)

    def test_invalid_target_never_dispatches(self):
        for target in ({}, {"pid": True, "window_id": 123456},
                       {"pid": 4242, "window_id": 0},
                       {"pid": 4242, "window_id": 123456, "extra": True}):
            runner = FakeRunner()
            with self.assertRaises(DriverError):
                CuaDriver(executable="fake", runner=runner).bring_to_front(target)
            self.assertEqual(runner.calls, [])

    def test_minimized_click_refusal_has_useful_diagnostic(self):
        refusal = {"code": "window_minimized", "effect": "refused", "status": "refused",
                   "refusal": {"code": "window_minimized", "message": "Window is minimized."}}
        with self.assertRaises(DriverError) as raised:
            CuaDriver(executable="fake", runner=FakeRunner(refusal)).click(
                {"pid": 4242, "window_id": 123456, "element_token": "fresh"})
        self.assertEqual(raised.exception.code, "window_minimized")
        self.assertEqual(raised.exception.context["action_result"], refusal)
