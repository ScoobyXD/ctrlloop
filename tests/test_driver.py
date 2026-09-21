"""Contract tests for the read-only CLI boundary; no desktop access needed."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import unittest

from ctrlloop.driver import CuaDriver, DriverError


def window(**updates):
    record = {
        "pid": 4242,
        "window_id": 123456,
        "app_name": "example.exe",
        "title": "Observed window",
        "bounds": {"x": -200, "y": 0, "width": 800, "height": 600},
        "is_on_screen": True,
        "z_index": None,
    }
    record.update(updates)
    return record


def element(**updates):
    record = {"element_index": 17, "role": "Button", "depth": 3}
    record.update(updates)
    return record


def state(**updates):
    record = {
        "pid": 4242,
        "window_id": 123456,
        "snapshot_id": "snapshot-observed",
        "elements": [element()],
        "elements_complete": False,
    }
    record.update(updates)
    return record


class FakeRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(args, 0, json.dumps(response), "private diagnostic")


class DriverTests(unittest.TestCase):
    def driver(self, *responses, **kwargs):
        runner = FakeRunner(*responses)
        return CuaDriver(executable="fake-cua", runner=runner, **kwargs), runner

    def test_only_two_read_calls_and_exact_arguments(self):
        observed_window = window(custom_metadata={"retained": True})
        observed_state = state()
        driver, runner = self.driver({"windows": [observed_window]}, observed_state)
        windows = driver.list_windows()
        snapshot = driver.get_window_state(windows[0])
        self.assertEqual(windows, [observed_window])
        self.assertEqual(snapshot, observed_state)
        self.assertEqual([call[0] for call in runner.calls], [
            ["fake-cua", "call", "list_windows"],
            ["fake-cua", "call", "get_window_state"],
        ])
        self.assertEqual(json.loads(runner.calls[0][1]["input"]), {})
        self.assertEqual(json.loads(runner.calls[1][1]["input"]), {
            "pid": 4242, "window_id": 123456,
            "include_accessibility_tree": True, "include_screenshot": False,
            "max_elements": 5000, "max_depth": 25,
        })
        for _, options in runner.calls:
            self.assertFalse(options["shell"])
            self.assertTrue(options["capture_output"])
            self.assertEqual(options["encoding"], "utf-8")
            self.assertEqual(options["timeout"], 60.0)
            self.assertNotIn("stderr", options)
            if os.name == "nt":
                self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_custom_limits_and_timeout(self):
        driver, runner = self.driver(state(), max_elements=80, max_depth=6, timeout=12)
        driver.get_window_state(window())
        self.assertEqual(runner.calls[0][1]["timeout"], 12)
        args = json.loads(runner.calls[0][1]["input"])
        self.assertEqual((args["max_elements"], args["max_depth"]), (80, 6))

    def test_actions_cannot_be_requested_even_through_private_dispatch(self):
        driver, runner = self.driver()
        for tool in ("click", "type_text", "launch_app", "bring_to_front", "get_desktop_state", "serve"):
            with self.subTest(tool=tool), self.assertRaises(DriverError):
                driver._call(tool, {})
        self.assertEqual(runner.calls, [])

    def test_sanitized_recorded_samples_pass(self):
        examples = Path(__file__).resolve().parents[1] / "docs/cua-driver/0.28.2/examples"
        windows = json.loads((examples / "list_windows.output.json").read_text(encoding="utf-8"))
        snapshot = json.loads((examples / "get_window_state.output.json").read_text(encoding="utf-8"))
        driver, _ = self.driver(windows, snapshot)
        result = driver.get_window_state(driver.list_windows()[1])
        self.assertEqual(result, snapshot)
        self.assertFalse(result["elements_complete"])
        self.assertNotIn("parent_index", result["elements"][0])

    def test_empty_discovery_is_successful(self):
        driver, _ = self.driver({"windows": []})
        self.assertEqual(driver.list_windows(), [])

    def test_partial_nullable_window_fields_are_preserved(self):
        record = window(pid=None, minimized=None, layer=None, on_current_space=None, space_ids=None)
        driver, runner = self.driver({"windows": [record]})
        self.assertEqual(driver.list_windows(), [record])
        with self.assertRaisesRegex(DriverError, "usable observed"):
            driver.get_window_state(record)
        self.assertEqual(len(runner.calls), 1)

    def test_partial_nullable_control_fields_are_preserved(self):
        control = element(
            element_token=None, label=None, value=None, enabled=None, selected=None,
            actions=None, parent_index=None, frame=None, in_web_content=None,
            value_description=None, min=None, max=None,
        )
        snapshot = state(elements=[control], tree_markdown=None, degraded=None)
        driver, _ = self.driver(snapshot)
        result = driver.get_window_state(window())
        self.assertEqual(result, snapshot)
        self.assertEqual(result["elements"][0]["element_index"], 17)

    def test_missing_or_null_observations_are_not_fabricated_as_empty(self):
        for observation in (
            {"pid": 4242, "window_id": 123456},
            {"pid": 4242, "window_id": 123456, "elements": None, "snapshot_id": None},
        ):
            with self.subTest(observation=observation):
                driver, _ = self.driver(observation)
                self.assertEqual(driver.get_window_state(window()), observation)

    def test_empty_success_and_degraded_empty_remain_distinct(self):
        for degraded in (False, True):
            snapshot = state(elements=[], degraded=degraded, returned_element_count=0)
            if degraded:
                snapshot.update(degraded_reason="UIA unavailable", escalation={"recommended": "foreground"})
            driver, runner = self.driver(snapshot)
            self.assertEqual(driver.get_window_state(window()), snapshot)
            self.assertEqual(len(runner.calls), 1)

    def test_degraded_partial_snapshot_can_be_used(self):
        snapshot = state(degraded=True, degraded_reason="partial MSAA fallback", truncated=True)
        driver, _ = self.driver(snapshot)
        self.assertEqual(driver.get_window_state(window()), snapshot)

    def test_zero_exit_plain_text_and_invalid_json_are_errors(self):
        for stdout in ("private tool failure", "", "[]", "null", '{"windows":NaN}'):
            with self.subTest(stdout=stdout):
                driver, _ = self.driver(subprocess.CompletedProcess([], 0, stdout, "private stderr"))
                with self.assertRaises(DriverError) as raised:
                    driver.list_windows()
                self.assertNotIn("private", str(raised.exception))

    def test_nonzero_exit_is_sanitized_even_when_stdout_looks_successful(self):
        driver, _ = self.driver(subprocess.CompletedProcess([], 1, '{"windows":[]}', "private secret"))
        with self.assertRaisesRegex(DriverError, "exit 1") as raised:
            driver.list_windows()
        self.assertNotIn("private", str(raised.exception))

    def test_structured_refusals_do_not_pass_as_success(self):
        for marker in ("refusal", "status", "code", "error", "isError"):
            for success in ({"windows": []}, state()):
                response = dict(success, **{marker: True})
                with self.subTest(marker=marker, success=success):
                    driver, _ = self.driver(response)
                    with self.assertRaises(DriverError):
                        if "windows" in response:
                            driver.list_windows()
                        else:
                            driver.get_window_state(window())

    def test_transport_exceptions_are_sanitized(self):
        errors = [
            FileNotFoundError("private path"), PermissionError("private diagnostic"),
            subprocess.TimeoutExpired("private command", 10, output="private stdout", stderr="private stderr"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private payload"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                driver, _ = self.driver(error)
                with self.assertRaises(DriverError) as raised:
                    driver.list_windows()
                self.assertNotIn("private", str(raised.exception))

    def test_selected_identity_invalid_before_subprocess(self):
        for field in ("pid", "window_id"):
            for invalid in (None, 0, -1, True, "4242", 1.5, 2**65):
                driver, runner = self.driver()
                with self.subTest(field=field, invalid=invalid), self.assertRaises(DriverError):
                    driver.get_window_state(window(**{field: invalid}))
                self.assertEqual(runner.calls, [])

    def test_requested_identity_must_match_response(self):
        for changes in ({"pid": 4243}, {"window_id": 123457}, {"pid": True}, {"window_id": None}):
            driver, _ = self.driver(state(**changes))
            with self.subTest(changes=changes), self.assertRaisesRegex(DriverError, "does not match"):
                driver.get_window_state(window())

    def test_discovery_success_schema_is_validated(self):
        invalid = [
            {}, {"windows": None}, {"windows": [{}]},
            {"windows": [window(title=None)]}, {"windows": [window(pid=True)]},
            {"windows": [window(bounds={"x": 0})]}, {"windows": [window(is_on_screen=1)]},
            {"windows": [window(z_index="front")]},
        ]
        for response in invalid:
            with self.subTest(response=response):
                driver, _ = self.driver(response)
                with self.assertRaises(DriverError):
                    driver.list_windows()

    def test_invalid_present_snapshot_fields_are_rejected(self):
        for changes in (
            {"elements": {}}, {"snapshot_id": 8}, {"degraded": "yes"},
            {"elements_complete": 0}, {"returned_element_count": True},
            {"returned_element_count": 2}, {"total_element_count": -1},
            {"window_bounds": {"x": 0}},
        ):
            with self.subTest(changes=changes):
                driver, _ = self.driver(state(**changes))
                with self.assertRaises(DriverError):
                    driver.get_window_state(window())

    def test_invalid_control_required_and_optional_fields_are_rejected(self):
        controls = [
            {}, element(element_index=True), element(depth=-1), element(role=None),
            element(actions="invoke"), element(actions=[1]), element(label=4),
            element(parent_index=-1), element(enabled=0), element(frame={"x": 0}),
        ]
        for control in controls:
            with self.subTest(control=control):
                driver, _ = self.driver(state(elements=[control]))
                with self.assertRaises(DriverError):
                    driver.get_window_state(window())

    def test_duplicate_indices_rejected_but_sparse_indices_and_omitted_ancestors_work(self):
        driver, _ = self.driver(state(elements=[element(), element()]))
        with self.assertRaisesRegex(DriverError, "duplicate"):
            driver.get_window_state(window())
        controls = [element(element_index=4, parent_index=1), element(element_index=88)]
        driver, _ = self.driver(state(elements=controls, element_count=100, returned_element_count=2))
        self.assertEqual(driver.get_window_state(window())["elements"], controls)

    def test_input_window_is_not_mutated(self):
        observed = window()
        before = deepcopy(observed)
        driver, _ = self.driver(state())
        driver.get_window_state(observed)
        self.assertEqual(observed, before)

    def test_constructor_rejects_invalid_limits(self):
        for args in (
            {"timeout": 0}, {"timeout": float("inf")}, {"timeout": float("nan")},
            {"max_elements": 0}, {"max_depth": True}, {"executable": ""},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                CuaDriver(**args)


if __name__ == "__main__":
    unittest.main()
