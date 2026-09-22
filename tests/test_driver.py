"""Contract tests for the native MCP boundary; no desktop access needed."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from src.driver import CuaDriver, DriverError


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


def foreground_result(w):
    return {"landed_on_target": True, "now_fg_hwnd": hex(w["window_id"]),
            "target_hwnd": hex(w["window_id"]), "previous_fg_hwnd": "0x123",
            "restored": False, "raised": True}


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


def tool_result(payload=None, *, text=None, is_error=False):
    """Build the native MCP envelope without changing the native payload."""
    result = {"content": [], "isError": is_error}
    if payload is not None:
        result["structuredContent"] = payload
    if text is not None:
        result["content"].append({"type": "text", "text": text})
    return result


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.close_calls = 0

    def call(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict) and ("content" in response or "structuredContent" in response):
            return response
        return tool_result(response)

    def close(self):
        self.close_calls += 1


class DriverTests(unittest.TestCase):
    def driver(self, *responses, **kwargs):
        transport = FakeTransport(*responses)
        return CuaDriver(executable="fake-cua", transport=transport, **kwargs), transport

    def test_only_two_read_calls_and_exact_arguments(self):
        observed_window = window(custom_metadata={"retained": True})
        observed_state = state()
        driver, transport = self.driver({"windows": [observed_window]}, observed_state)
        windows = driver.list_windows()
        snapshot = driver.get_window_state(windows[0])
        self.assertEqual(windows, [observed_window])
        self.assertEqual(snapshot, observed_state)
        self.assertEqual([call[0] for call in transport.calls], [
            "list_windows", "get_window_state",
        ])
        self.assertEqual(transport.calls[0][1], {})
        self.assertEqual(transport.calls[1][1], {
            "pid": 4242, "window_id": 123456,
            "include_accessibility_tree": True, "include_screenshot": False,
            "max_elements": 5000, "max_depth": 25,
        })

    def test_custom_snapshot_limits(self):
        driver, transport = self.driver(state(), max_elements=80, max_depth=6, timeout=12)
        driver.get_window_state(window())
        args = transport.calls[0][1]
        self.assertEqual((args["max_elements"], args["max_depth"]), (80, 6))

    def test_other_actions_cannot_be_requested_even_through_private_dispatch(self):
        driver, transport = self.driver()
        for tool in ("type_text", "launch_app", "get_desktop_state", "serve"):
            with self.subTest(tool=tool), self.assertRaises(DriverError):
                driver._call(tool, {})
        self.assertEqual(transport.calls, [])

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
        driver, transport = self.driver({"windows": [record]})
        self.assertEqual(driver.list_windows(), [record])
        with self.assertRaisesRegex(DriverError, "usable observed"):
            driver.get_window_state(record)
        self.assertEqual(len(transport.calls), 1)

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
            driver, transport = self.driver(snapshot)
            self.assertEqual(driver.get_window_state(window()), snapshot)
            self.assertEqual(len(transport.calls), 1)

    def test_degraded_partial_snapshot_can_be_used(self):
        snapshot = state(degraded=True, degraded_reason="partial MSAA fallback", truncated=True)
        driver, _ = self.driver(snapshot)
        self.assertEqual(driver.get_window_state(window()), snapshot)

    def test_plain_text_and_invalid_json_are_errors(self):
        for text in ("private tool failure", "", "[]", "null", '{"windows":NaN}'):
            with self.subTest(text=text):
                driver, _ = self.driver(tool_result(text=text))
                with self.assertRaises(DriverError) as raised:
                    driver.list_windows()
                self.assertNotIn("private", str(raised.exception))

    def test_mcp_error_is_sanitized_even_with_successful_looking_payload(self):
        driver, _ = self.driver(tool_result({"windows": []}, text="private secret", is_error=True))
        with self.assertRaises(DriverError) as raised:
            driver.list_windows()
        self.assertNotIn("private", str(raised.exception))

    def test_json_text_fallback_preserves_native_payload(self):
        observed = window()
        driver, _ = self.driver(tool_result(text=json.dumps({"windows": [observed]})))
        self.assertEqual(driver.list_windows(), [observed])

    def test_driver_diagnostics_distinguish_transport_provider_and_target_failures(self):
        cases = (
            ("connect to named pipe: Access is denied. (os error 5)", "pipe_access_denied"),
            ("authorization host unavailable", "approval_unavailable"),
            ("approval_required", "approval_required"),
            ("permission denied", "access_denied"),
            ("window_id 17 belongs to pid 42, not pid 43", "window_owner_mismatch"),
            ("Invalid window handle 0x80070578", "window_unavailable"),
            ("UIA snapshot timed out", "driver_timeout"),
            ("daemon failed to connect", "daemon_unavailable"),
            ("unknown private diagnostic", "driver_call_failed"),
        )
        for diagnostic, code in cases:
            with self.subTest(code=code):
                failure = tool_result(
                    {"tree_markdown": "private snapshot"},
                    text=diagnostic + " private-secret", is_error=True,
                )
                # Unavailable daemon failures allow one read-only retry.
                driver, _ = self.driver(failure, failure)
                with self.assertRaises(DriverError) as raised:
                    driver.get_window_state(window())
                self.assertEqual(raised.exception.details(), {
                    "code": code, "tool": "get_window_state",
                })
                self.assertNotIn("private", str(raised.exception))
                if code != "pipe_access_denied" and code != "daemon_unavailable":
                    self.assertNotIn("daemon", str(raised.exception))

    def test_text_tool_error_preserves_recognized_diagnostic(self):
        driver, _ = self.driver(tool_result(
            text="window_id 17 belongs to pid 42, not pid 43", is_error=True,
        ))
        with self.assertRaises(DriverError) as raised:
            driver.get_window_state(window())
        self.assertEqual(raised.exception.code, "window_owner_mismatch")
        self.assertIsNone(raised.exception.exit_code)

    def test_structured_permission_refusal_has_safe_diagnostic_code(self):
        driver, _ = self.driver({"code": "permission_denied", "error": "private-secret"})
        with self.assertRaises(DriverError) as raised:
            driver.get_window_state(window())
        self.assertEqual(raised.exception.code, "access_denied")
        self.assertNotIn("private-secret", str(raised.exception))

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
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private payload"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                driver, _ = self.driver(error)
                with self.assertRaises(DriverError) as raised:
                    driver.list_windows()
                self.assertNotIn("private", str(raised.exception))

    def test_read_reconnects_once_using_the_same_transport(self):
        for tool, response in (("list_windows", {"windows": [window()]}), ("get_window_state", state())):
            for code in ("transport_disconnected", "client_timeout", "daemon_unavailable"):
                with self.subTest(tool=tool, code=code):
                    driver, transport = self.driver(DriverError("Connection unavailable.", code=code), response)
                    observed = (driver.list_windows() if tool == "list_windows"
                                else driver.get_window_state(window()))
                    self.assertEqual(observed, response["windows"] if tool == "list_windows" else response)
                    self.assertEqual([call[0] for call in transport.calls], [tool, tool])
                    self.assertEqual(transport.calls[0], transport.calls[1])
                    self.assertEqual(transport.close_calls, 1)

    def test_repeated_read_disconnect_stops_after_one_retry(self):
        for tool in ("list_windows", "get_window_state"):
            with self.subTest(tool=tool):
                errors = [DriverError("Connection unavailable.", code="transport_disconnected") for _ in range(2)]
                driver, transport = self.driver(*errors, {"windows": []})
                with self.assertRaises(DriverError) as raised:
                    if tool == "list_windows":
                        driver.list_windows()
                    else:
                        driver.get_window_state(window())
                self.assertIs(raised.exception, errors[-1])
                self.assertEqual([call[0] for call in transport.calls], [tool, tool])
                self.assertEqual(len(transport.responses), 1)

    def test_action_connection_loss_never_replays_the_action(self):
        for tool in ("click", "bring_to_front"):
            for code in ("transport_disconnected", "client_timeout"):
                with self.subTest(tool=tool, code=code):
                    failure = DriverError("Action response unavailable.", code=code)
                    driver, transport = self.driver(failure, {"effect": "confirmed", "route": "accessibility"})
                    target = {"pid": 4242, "window_id": 123456}
                    if tool == "click":
                        target["element_token"] = "old-token"
                    with self.assertRaises(DriverError) as raised:
                        getattr(driver, tool)(target)
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(transport.calls, [(tool, target)])
                    self.assertEqual(transport.close_calls, 1)
                    self.assertEqual(len(transport.responses), 1)

    def test_recovery_requires_a_fresh_snapshot_and_its_exact_target(self):
        action_result = {"effect": "confirmed", "route": "accessibility"}
        for kind in ("token", "index"):
            with self.subTest(kind=kind):
                old_snapshot = state(snapshot_id="old-snapshot", elements=[element(element_token="old-token")])
                fresh_snapshot = state(snapshot_id="fresh-snapshot", elements=[
                    element(element_index=31, element_token="fresh-token"),
                ])
                driver, transport = self.driver(
                    old_snapshot, DriverError("Response lost.", code="transport_disconnected"),
                    {"windows": [window()]}, fresh_snapshot, action_result,
                )
                identity = {"pid": 4242, "window_id": 123456}
                old_target = {**identity, **({"element_token": "old-token"} if kind == "token" else {
                    "element_index": 17, "snapshot_id": "old-snapshot",
                })}
                fresh_target = {**identity, **({"element_token": "fresh-token"} if kind == "token" else {
                    "element_index": 31, "snapshot_id": "fresh-snapshot",
                })}
                driver.get_window_state(window())
                with self.assertRaises(DriverError):
                    driver.click(old_target)
                with self.assertRaises(DriverError):
                    driver.click(old_target)
                self.assertEqual(len(transport.calls), 2)

                # Rediscovering windows alone cannot validate a control token.
                driver.list_windows()
                with self.assertRaises(DriverError):
                    driver.click(old_target)
                self.assertEqual(len(transport.calls), 3)
                driver.get_window_state(window())
                unobserved_target = {**fresh_target, **({"element_token": "never-observed"} if kind == "token" else {
                    "element_index": 999,
                })}
                invalid_targets = [old_target, unobserved_target, {**fresh_target, "window_id": 123457}]
                if kind == "index":
                    invalid_targets.append({**fresh_target, "snapshot_id": "old-snapshot"})
                for target in invalid_targets:
                    with self.subTest(target=target), self.assertRaises(DriverError):
                        driver.click(target)
                self.assertEqual(len(transport.calls), 4)
                self.assertEqual(driver.click(fresh_target), action_result)
                self.assertEqual(transport.calls[-1], ("click", fresh_target))
                self.assertEqual(len(transport.calls), 5)

    def test_newer_snapshot_supersedes_the_recovery_snapshot(self):
        action_result = {"effect": "confirmed", "route": "accessibility"}
        driver, transport = self.driver(
            DriverError("Connection lost.", code="transport_disconnected"),
            state(snapshot_id="recovered", elements=[element(element_token="recovered-token")]),
            action_result,
            state(snapshot_id="newest", elements=[element(element_token="newest-token")]),
            action_result,
        )
        identity = {"pid": 4242, "window_id": 123456}
        recovered_target = {**identity, "element_token": "recovered-token"}
        driver.get_window_state(window())
        driver.click(recovered_target)
        driver.get_window_state(window())
        with self.assertRaises(DriverError):
            driver.click(recovered_target)
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(driver.click({**identity, "element_token": "newest-token"}), action_result)
        self.assertEqual(len(transport.calls), 5)

    def test_close_is_idempotent_and_a_closed_driver_cannot_read(self):
        driver, transport = self.driver({"windows": []})
        driver.list_windows()
        driver.close()
        driver.close()
        self.assertEqual(transport.close_calls, 1)
        for tool in ("list_windows", "get_window_state"):
            with self.subTest(tool=tool), self.assertRaises(DriverError) as raised:
                if tool == "list_windows":
                    driver.list_windows()
                else:
                    driver.get_window_state(window())
            self.assertEqual(raised.exception.code, "driver_closed")
        self.assertEqual(len(transport.calls), 1)

    def test_context_closes_on_failure_without_idle_health_calls(self):
        driver, transport = self.driver()
        with self.assertRaisesRegex(RuntimeError, "request failed"):
            with driver as active:
                self.assertIs(active, driver)
                self.assertEqual(transport.calls, [])
                raise RuntimeError("request failed")
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(transport.calls, [])

    def test_selected_identity_invalid_before_transport(self):
        for field in ("pid", "window_id"):
            for invalid in (None, 0, -1, True, "4242", 1.5, 2**65):
                driver, transport = self.driver()
                with self.subTest(field=field, invalid=invalid), self.assertRaises(DriverError):
                    driver.get_window_state(window(**{field: invalid}))
                self.assertEqual(transport.calls, [])

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
