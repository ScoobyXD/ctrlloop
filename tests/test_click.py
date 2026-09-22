"""One-click contract checks through the real adapter with an offline CLI runner."""

from copy import deepcopy
import json
import subprocess
import unittest
from unittest.mock import patch

from src.driver import CuaDriver, DriverError
from src.pipeline import run_pipeline
from src.selection import ChoiceSelector, PINNED_MODEL
from src.synthetic import START_MENU_KEY
from test_driver import FakeRunner, element, state, window, foreground_result
from test_pipeline import ChoosingClient, RecordingReport


ACTION_RESULT = {
    "effect": "unverifiable", "route": "accessibility", "delivery": {"mode": "background"},
}


class ClickPipelineTests(unittest.TestCase):
    def set_up_run(self, *, snapshot=None, windows=None, action_result=None, preferred_key=None):
        runner = FakeRunner(
            {"windows": [window()] if windows is None else windows},
            foreground_result((windows or [window()])[-1]),
            state(elements=[element(element_token="observed-token")]) if snapshot is None else snapshot,
            ACTION_RESULT if action_result is None else action_result,
        )
        driver = CuaDriver(executable="fake-cua", runner=runner)
        client = ChoosingClient(preferred_key=preferred_key)
        return driver, runner, client

    def test_selected_records_dispatch_one_token_click_after_the_only_snapshot(self):
        selected_window = window(pid=4243, window_id=123457, title="Second editor")
        selected_control = element(element_index=901, element_token="observed-token:901", label="Save")
        snapshot = state(pid=4243, window_id=123457, elements=[element(), selected_control])
        native_result = {**ACTION_RESULT, "native_detail": {"retained": True}}
        driver, runner, client = self.set_up_run(
            windows=[window(), selected_window], snapshot=snapshot, action_result=native_result,
        )
        request = "  Click Save.\r\nKeep this wording. "

        result = run_pipeline(request, driver, ChoiceSelector(client), execute_click=True)

        self.assertEqual(result.status, "action_returned")
        self.assertEqual(result.action_result, native_result)
        self.assertEqual(result.window, selected_window)
        self.assertEqual(result.control, selected_control)
        self.assertEqual(result.snapshot_id, snapshot["snapshot_id"])
        self.assertEqual([call[0] for call in runner.calls], [
            ["fake-cua", "call", "list_windows"],
            ["fake-cua", "call", "bring_to_front"],
            ["fake-cua", "call", "get_window_state"],
            ["fake-cua", "call", "click"],
        ])
        self.assertEqual(json.loads(runner.calls[-1][1]["input"]), {
            "pid": 4243, "window_id": 123457, "element_token": "observed-token:901",
        })
        self.assertEqual(len(client.calls), 2)
        for stage, call in zip(("windows", "controls"), client.calls):
            self.assertEqual(call["state"], request)
            self.assertEqual(call["model"], PINNED_MODEL)
            self.assertTrue(all(key.startswith(stage + "_") for key in call["questions"]))
        self.assertEqual(result.to_dict()["action_result"], native_result)

    def test_index_fallback_keeps_the_exact_snapshot_and_sparse_element_index(self):
        driver, runner, client = self.set_up_run(snapshot=state(
            snapshot_id="snapshot:chosen", elements=[element(element_index=901, element_token=None)],
        ))

        result = run_pipeline("Click Save", driver, ChoiceSelector(client), execute_click=True)

        self.assertEqual(result.status, "action_returned")
        self.assertEqual(json.loads(runner.calls[-1][1]["input"]), {
            "pid": 4242, "window_id": 123456,
            "element_index": 901, "snapshot_id": "snapshot:chosen",
        })
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(len(client.calls), 1)  # One control needs no Jev question.

    def test_selection_only_remains_the_default(self):
        driver, runner, client = self.set_up_run()

        result = run_pipeline("Find Save", driver, ChoiceSelector(client))

        self.assertEqual(result.status, "selected")
        self.assertIsNone(result.action_result)
        self.assertEqual([call[0][-1] for call in runner.calls], ["list_windows", "bring_to_front", "get_window_state"])

    def test_synthetic_start_search_stops_without_a_snapshot_or_native_action(self):
        for windows in ([], [window()]):
            with self.subTest(real_windows=bool(windows)):
                driver, runner, client = self.set_up_run(windows=windows, preferred_key=START_MENU_KEY)

                result = run_pipeline("Open Start", driver, ChoiceSelector(client), execute_click=True)

                self.assertEqual(result.status, "no_action")
                self.assertTrue(result.reason)
                self.assertIsNone(result.action_result)
                self.assertTrue(result.window["synthetic"])
                self.assertTrue(result.control["synthetic"])
                self.assertEqual([call[0][-1] for call in runner.calls], ["list_windows"])
                self.assertLessEqual(len(client.calls), 1)

    def test_unusable_observations_keep_their_status_and_never_click(self):
        cases = (
            (state(elements=[]), "no_controls"),
            (state(elements=[], degraded=True), "observation_degraded"),
            (state(elements=None), "observation_unavailable"),
            (state(snapshot_id=None), "observation_unavailable"),
        )
        for snapshot, status in cases:
            with self.subTest(status=status, snapshot=snapshot):
                driver, runner, client = self.set_up_run(snapshot=snapshot)

                result = run_pipeline("Click Save", driver, ChoiceSelector(client), execute_click=True)

                self.assertEqual(result.status, status)
                self.assertIsNone(result.action_result)
                self.assertEqual([call[0][-1] for call in runner.calls], ["list_windows", "bring_to_front", "get_window_state"])

    def test_native_refusals_preserve_the_result_without_retry_or_reselection(self):
        for code in ("stale_element_token", "background_unavailable"):
            native_result = {
                "status": "refused",
                "refusal": {"code": code, "message": "Native target refusal."},
            }
            with self.subTest(code=code):
                driver, runner, client = self.set_up_run(action_result=native_result)

                with self.assertRaises(DriverError) as raised:
                    run_pipeline("Click Save", driver, ChoiceSelector(client), execute_click=True)

                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.context["action_result"], native_result)
                self.assertEqual(raised.exception.exit_code, 0)
                self.assertEqual([call[0][-1] for call in runner.calls], [
                    "list_windows", "bring_to_front", "get_window_state", "click",
                ])
                self.assertEqual(len(client.calls), 1)

    def test_failed_post_action_report_keeps_the_native_result_and_does_not_retry(self):
        for report_error in (OSError("private-report-path"), ValueError("private-report-state")):
            with self.subTest(error=type(report_error).__name__):
                driver, runner, client = self.set_up_run()
                report = RecordingReport()
                append_original = report.append

                def append_until_action(event, **data):
                    if event == "action_returned" and data.get("tool") != "bring_to_front":
                        raise report_error
                    append_original(event, **data)

                with patch.object(report, "append", side_effect=append_until_action):
                    with self.assertLogs("src.pipeline", level="WARNING") as logged:
                        result = run_pipeline(
                            "Click Save", driver, ChoiceSelector(client), execute_click=True, report=report,
                        )

                self.assertEqual(result.status, "action_returned")
                self.assertEqual(result.action_result, ACTION_RESULT)
                self.assertEqual([call[0][-1] for call in runner.calls], [
                    "list_windows", "bring_to_front", "get_window_state", "click",
                ])
                self.assertEqual(report.events[-1]["event"], "action_requested")
                self.assertIn("Could not save the action result", " ".join(logged.output))
                self.assertNotIn("private-report", " ".join(logged.output))

    def test_failed_pre_action_report_stops_before_dispatch(self):
        for report_error in (OSError("report write failed"), ValueError("report closed")):
            with self.subTest(error=type(report_error).__name__):
                driver, runner, client = self.set_up_run()
                report = RecordingReport()
                append_original = report.append

                def append_until_request(event, **data):
                    if event == "action_requested" and data.get("tool") != "bring_to_front":
                        raise report_error
                    append_original(event, **data)

                with patch.object(report, "append", side_effect=append_until_request):
                    with self.assertRaises(type(report_error)) as raised:
                        run_pipeline("Click Save", driver, ChoiceSelector(client), execute_click=True, report=report)

                self.assertIs(raised.exception, report_error)
                self.assertEqual([call[0][-1] for call in runner.calls], ["list_windows", "bring_to_front", "get_window_state"])
                self.assertEqual(report.events[-1]["event"], "control_selected")


class ClickDriverTests(unittest.TestCase):
    def test_native_action_results_are_not_rejected_by_read_only_status_rules(self):
        for payload in (ACTION_RESULT, {**ACTION_RESULT, "status": "success"}):
            with self.subTest(payload=payload):
                runner = FakeRunner(payload)
                driver = CuaDriver(executable="fake-cua", runner=runner)
                target = {"pid": 4242, "window_id": 123456, "element_token": "observed-token"}
                before = deepcopy(target)

                self.assertEqual(driver.click(target), payload)

                self.assertEqual(target, before)
                self.assertEqual(json.loads(runner.calls[0][1]["input"]), target)
                self.assertEqual(runner.calls[0][0], ["fake-cua", "call", "click"])

    def test_invalid_or_unsupported_targets_fail_before_subprocess(self):
        token_target = {"pid": 4242, "window_id": 123456, "element_token": "observed-token"}
        index_target = {"pid": 4242, "window_id": 123456, "element_index": 0, "snapshot_id": "observed"}
        invalid_targets = [
            None, [], {},
            {"pid": 4242, "window_id": 123456},
            {"pid": 4242, "window_id": 123456, "element_index": 0},
            {key: value for key, value in token_target.items() if key != "pid"},
            {key: value for key, value in token_target.items() if key != "window_id"},
            {**token_target, "element_index": 0, "snapshot_id": "observed"},
        ]
        for key in ("pid", "window_id"):
            invalid_targets.extend({**token_target, key: value} for value in (None, 0, -1, True, "42", 2**65))
        invalid_targets.extend({**token_target, "element_token": value} for value in (None, "", True, 7))
        invalid_targets.extend({**index_target, "element_index": value} for value in (None, -1, True, "0"))
        invalid_targets.extend({**index_target, "snapshot_id": value} for value in (None, "", True, 7))
        invalid_targets.extend({**token_target, key: value} for key, value in (
            ("action", "press"), ("x", 30), ("y", 40), ("button", "right"), ("foreground", True),
        ))

        for target in invalid_targets:
            with self.subTest(target=target):
                runner = FakeRunner()
                driver = CuaDriver(executable="fake-cua", runner=runner)
                with self.assertRaises(DriverError):
                    driver.click(target)
                self.assertEqual(runner.calls, [])

    def test_zero_exit_plain_text_native_error_is_not_success(self):
        runner = FakeRunner(subprocess.CompletedProcess([], 0, "stale_element_token: snapshot superseded", ""))
        driver = CuaDriver(executable="fake-cua", runner=runner)

        with self.assertRaises(DriverError) as raised:
            driver.click({"pid": 4242, "window_id": 123456, "element_token": "observed-token"})

        self.assertEqual(raised.exception.code, "stale_element_token")
        self.assertEqual(len(runner.calls), 1)

    def test_malformed_action_facts_raise_driver_error_and_preserve_payload(self):
        for field in ("effect", "route"):
            for malformed in ([], {}):
                payload = {**ACTION_RESULT, field: malformed}
                with self.subTest(field=field, malformed=malformed):
                    runner = FakeRunner(payload)
                    driver = CuaDriver(executable="fake-cua", runner=runner)

                    with self.assertRaises(DriverError) as raised:
                        driver.click({"pid": 4242, "window_id": 123456, "element_token": "observed-token"})

                    self.assertEqual(raised.exception.code, "invalid_action_result")
                    self.assertEqual(raised.exception.context["action_result"], payload)
                    self.assertEqual(len(runner.calls), 1)

    def test_nonzero_exit_preserves_native_payload_without_treating_it_as_success(self):
        refusal = {
            "status": "refused", "refusal": {"code": "stale_element_token", "message": "Snapshot superseded."},
        }
        for payload, expected_code in ((refusal, "stale_element_token"), (ACTION_RESULT, "driver_call_failed")):
            with self.subTest(payload=payload):
                runner = FakeRunner(subprocess.CompletedProcess([], 1, json.dumps(payload), "private-stderr"))
                driver = CuaDriver(executable="fake-cua", runner=runner)

                with self.assertRaises(DriverError) as raised:
                    driver.click({"pid": 4242, "window_id": 123456, "element_token": "observed-token"})

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.exit_code, 1)
                self.assertEqual(raised.exception.context["action_result"], payload)
                self.assertNotIn("private-stderr", str(raised.exception))
                self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
