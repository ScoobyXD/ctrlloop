"""Windows adapter with one CUA Driver MCP client per user request.

Native records and action results are retained without a parallel wire model.
The client connects to the existing daemon; it never starts or stops it.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
from collections.abc import Callable, Mapping
from typing import Any


_ACTION_TOOLS = frozenset({"click", "bring_to_front", "hotkey", "type_text"})


class DriverError(Exception):
    """A driver transport, refusal, or contract error with a safe summary."""

    def __init__(
        self, message: str, *, code: str = "driver_error", tool: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.tool = tool
        self.exit_code = exit_code
        self.context: dict[str, Any] = {}

    def details(self) -> dict[str, Any]:
        details = {"code": self.code, **self.context}
        if self.tool is not None:
            details["tool"] = self.tool
        if self.exit_code is not None:
            details["exit_code"] = self.exit_code
        return details


def _failure(tool: str, exit_code: int | None, diagnostic: str) -> DriverError:
    """Recognize useful error categories without echoing private driver output."""
    text = diagnostic.casefold()
    if "connect to named pipe" in text and "access is denied" in text:
        code, reason = "pipe_access_denied", "Access to the existing CUA daemon pipe was denied."
    elif ("approval" in text or "authorization host" in text) and "unavailable" in text:
        code, reason = "approval_unavailable", "CUA requires an approval host that is unavailable."
    elif any(marker in text for marker in ("approval_required", "requires approval", "approval required")):
        code, reason = "approval_required", "CUA requires approval for this call."
    elif any(marker in text for marker in (
        "permission denied", "permission_denied", "authorization denied",
        "access is denied", "access denied", "not authorized",
    )):
        code, reason = "access_denied", "CUA denied access for this call."
    elif "stale_element_token" in text:
        code, reason = "stale_element_token", "CUA rejected the stale snapshot target; no retry was attempted."
    elif "snapshot_id_required" in text:
        code, reason = "snapshot_id_required", "CUA requires the observed snapshot identity for this target."
    elif "background_unavailable" in text:
        code, reason = "background_unavailable", "CUA refused background delivery; no escalation was attempted."
    elif "window_minimized" in text:
        code, reason = "window_minimized", "CUA requires the minimized window to be restored before this action."
    elif "foreground_unavailable" in text:
        code, reason = "foreground_unavailable", "CUA could not bring the selected window to the foreground."
    elif "belongs to pid" in text:
        code, reason = "window_owner_mismatch", "The window no longer belongs to the selected process; rediscover windows."
    elif any(marker in text for marker in ("invalid window handle", "window not found", "0x80070578")):
        code, reason = "window_unavailable", "The selected window is no longer available; rediscover windows."
    elif "timed out" in text or "timeout" in text:
        code, reason = "driver_timeout", "The driver timed out while processing the call."
    elif "daemon" in text and any(marker in text for marker in ("not running", "failed to connect", "connection refused")):
        code, reason = "daemon_unavailable", "The existing CUA daemon could not be reached."
    else:
        code, reason = "driver_call_failed", "The driver did not provide a recognized diagnostic."
    exit_detail = f" (exit {exit_code})" if exit_code is not None else ""
    return DriverError(
        f"CUA Driver {tool} failed{exit_detail}. {reason}",
        code=code, tool=tool, exit_code=exit_code,
    )


def _integer(value: Any, minimum: int = 0, maximum: int | None = None) -> bool:
    return (
        type(value) is int
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _action_diagnostic(payload: Mapping[str, Any]) -> str:
    # Snapshot refusals have a nested {code, message}; background delivery
    # errors have a top-level code. Keep both native forms in action_result.
    refusal = payload.get("refusal")
    records = (payload, refusal) if isinstance(refusal, Mapping) else (payload,)
    return " ".join(
        record[key] for record in records for key in ("code", "status", "refusal", "error", "message")
        if isinstance(record.get(key), str)
    )


def _number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _invalid(field: str) -> None:
    # Field paths are authored here; never include observed text in errors.
    raise DriverError(f"CUA Driver returned an invalid {field} field.")


def _optional(record: Mapping[str, Any], field: str, predicate: Callable) -> None:
    value = record.get(field)
    if value is not None and not predicate(value):
        _invalid(field)


def _bounds(value: Any, dimensions: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(
        key in value and _number(value[key]) for key in dimensions
    )


def _default_executable() -> str:
    found = shutil.which("cua-driver")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        installed = Path(local_app_data) / "Programs/Cua/cua-driver/bin/cua-driver.exe"
        if installed.is_file():
            return str(installed)
    return "cua-driver"


def _reject_json_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON numeric constant")


class CuaDriver:
    """Observe windows and dispatch native clicks, desktop hotkeys, and text.

    Optional/nullable observation fields are preserved, including missing
    elements or snapshot identity. The pipeline decides whether that observation
    is usable. A false ``elements_complete`` flag is normal on Windows 0.28.2.
    No method starts the daemon. bring_to_front explicitly changes window focus.
    """

    def __init__(
        self,
        executable: str | None = None,
        timeout: float = 60.0,
        max_elements: int = 5000,
        max_depth: int = 25,
        *,
        transport: Any = None,
    ) -> None:
        if executable is not None and (not isinstance(executable, str) or not executable):
            raise ValueError("executable must be a nonempty string")
        if not _number(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if not _integer(max_elements, 1) or not _integer(max_depth, 1):
            raise ValueError("max_elements and max_depth must be positive integers")
        self.executable = executable or _default_executable()
        self.timeout = timeout
        self.max_elements = max_elements
        self.max_depth = max_depth
        if transport is None:
            # Import after DriverError is defined: the transport shares this
            # existing error contract and does not introduce a parallel model.
            if __package__:
                from .cua_mcp import McpTransport
            else:
                from cua_mcp import McpTransport
            transport = McpTransport(self.executable, self.timeout)
        self._transport = transport
        self._closed = False
        self._recovery_required = False
        self._recovery_snapshot: dict[str, Any] | None = None

    def __enter__(self) -> CuaDriver:
        if self._closed:
            raise DriverError("This request's CUA client is closed.", code="driver_closed")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """End this request's client once; leave the shared daemon running."""
        if not self._closed:
            self._closed = True
            self._recovery_snapshot = None
            self._transport.close()

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool not in _ACTION_TOOLS | {"list_windows", "get_window_state"}:
            raise DriverError("This CUA tool is not available through the adapter.")
        if self._closed:
            raise DriverError("This request's CUA client is closed.", code="driver_closed", tool=tool)
        for attempt in range(2):
            try:
                return self._payload(tool, self._transport.call(tool, arguments))
            except DriverError as error:
                reconnect = error.code in {"transport_disconnected", "client_timeout", "daemon_unavailable"}
                if reconnect or error.code in {"invalid_driver_response", "driver_call_failed"}:
                    self._recovery_required = True
                    self._recovery_snapshot = None
                    self._transport.close()
                # Reads are safe to repeat once. Never replay an input/activation
                # after a lost response: it may already have been delivered.
                if reconnect and attempt == 0 and tool in {"list_windows", "get_window_state"}:
                    continue
                raise
            except (OSError, UnicodeError):
                self._recovery_required = True
                self._recovery_snapshot = None
                self._transport.close()
                raise DriverError("CUA transport failed; check the client and daemon connection.",
                                  code="transport_disconnected", tool=tool) from None
        raise AssertionError("Unreachable CUA retry state")

    @staticmethod
    def _payload(tool: str, result: Any) -> dict[str, Any]:
        """Unwrap the native MCP envelope, then retain the existing validators."""
        if not isinstance(result, dict) or type(result.get("isError", False)) is not bool:
            raise DriverError("CUA returned an invalid MCP result.", code="invalid_driver_response", tool=tool)
        content = result.get("content", [])
        if not isinstance(content, list):
            raise DriverError("CUA returned invalid MCP content.", code="invalid_driver_response", tool=tool)
        text = "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
        payload = result.get("structuredContent")
        if "structuredContent" not in result:
            try:
                payload = json.loads(text, parse_constant=_reject_json_constant)
            except (ValueError, TypeError):
                if result.get("isError"):
                    raise _failure(tool, None, text) from None
                raise DriverError("CUA returned no structured JSON result.",
                                  code="invalid_driver_response", tool=tool) from None
        if result.get("isError"):
            diagnostic = _action_diagnostic(payload) if isinstance(payload, dict) else ""
            error = _failure(tool, None, diagnostic or text)
            if tool in _ACTION_TOOLS and isinstance(payload, dict):
                error.context["action_result"] = payload
            raise error
        if not isinstance(payload, dict):
            raise DriverError(f"CUA Driver {tool} did not return an object.",
                              code="invalid_driver_response", tool=tool)
        if tool in _ACTION_TOOLS:
            # Action facts use a different native contract from observations.
            # In particular, an effect of "unverifiable" is a legitimate result.
            if (
                any(key in payload for key in ("refusal", "code", "error"))
                or ("isError" in payload and payload["isError"] is not False)
                or ("status" in payload and payload["status"] != "success")
                or payload.get("effect") == "refused"
            ):
                error = _failure(tool, None, _action_diagnostic(payload))
                error.context["action_result"] = payload
                raise error
            return payload
        # The advertised refusal alternative has no stable value enum. None of
        # these markers is in the documented success payload, so fail closed.
        if any(key in payload for key in ("refusal", "status", "code", "error")):
            diagnostic = " ".join(
                payload[key] for key in ("code", "status", "refusal", "error")
                if isinstance(payload.get(key), str)
            )
            raise _failure(tool, None, diagnostic)
        if "isError" in payload and payload["isError"] is not False:
            raise DriverError(f"CUA Driver {tool} returned a tool error.")
        return payload

    def bring_to_front(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Restore and activate the exact observed window using native CUA."""
        if (
            not isinstance(arguments, Mapping)
            or set(arguments) != {"pid", "window_id"}
            or not _integer(arguments.get("pid"), 1, 2**32 - 1)
            or not _integer(arguments.get("window_id"), 1, 2**64 - 1)
        ):
            raise DriverError("bring_to_front requires an observed pid/window_id pair.",
                              code="invalid_window_target", tool="bring_to_front")
        payload = self._call("bring_to_front", arguments)
        if (
            payload.get("landed_on_target") is not True
            or payload.get("now_fg_hwnd") != hex(arguments["window_id"])
            or payload.get("target_hwnd") != hex(arguments["window_id"])
        ):
            error = DriverError("CUA did not confirm foreground activation of the selected window.",
                                code="invalid_action_result", tool="bring_to_front")
            error.context["action_result"] = payload
            raise error
        return payload

    def click(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch native element-target arguments once; never retry or observe.

        The caller must use the selected control from its pre-action snapshot.
        The daemon rejects superseded tokens, including changes made by other
        clients. A timeout or unreadable response leaves delivery unknown.
        """
        allowed = {"pid", "window_id", "element_token", "element_index", "snapshot_id"}
        if not isinstance(arguments, Mapping) or set(arguments) - allowed:
            raise DriverError("Click requires native window and snapshot-bound element arguments.",
                              code="invalid_click_target", tool="click")
        if (
            not _integer(arguments.get("pid"), 1, 2**32 - 1)
            or not _integer(arguments.get("window_id"), 1, 2**64 - 1)
        ):
            raise DriverError("Click requires a usable observed pid/window_id pair.",
                              code="invalid_click_target", tool="click")
        if "element_token" in arguments:
            valid = (
                isinstance(arguments["element_token"], str) and bool(arguments["element_token"].strip())
                and not {"element_index", "snapshot_id"}.intersection(arguments)
            )
        else:
            valid = (
                _integer(arguments.get("element_index"))
                and isinstance(arguments.get("snapshot_id"), str)
                and bool(arguments["snapshot_id"].strip())
            )
        if not valid:
            raise DriverError("Click requires an observed element_token or element_index with snapshot_id.",
                              code="invalid_click_target", tool="click")
        if self._recovery_required:
            snapshot = self._recovery_snapshot
            fresh = bool(snapshot) and all(
                snapshot.get(key) == arguments[key] for key in ("pid", "window_id")
            )
            elements = (snapshot or {}).get("elements") or []
            if "element_token" in arguments:
                fresh = fresh and any(
                    element.get("element_token") == arguments["element_token"] for element in elements
                )
            else:
                fresh = fresh and snapshot.get("snapshot_id") == arguments["snapshot_id"] and any(
                    element["element_index"] == arguments["element_index"] for element in elements
                )
            if not fresh:
                raise DriverError(
                    "Refresh the window state and reselect a control after a CUA connection loss.",
                    code="stale_element_token", tool="click",
                )
        return self._input_action("click", arguments)

    def hotkey(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Send one native desktop hotkey without reads, waits, or retries."""
        self._validate_desktop_input("hotkey", arguments, {"keys"})
        keys = arguments.get("keys")
        if (
            not isinstance(keys, list) or len(keys) < 2
            or not all(isinstance(key, str) and key.strip() for key in keys)
        ):
            raise DriverError("hotkey requires a native keys array with at least two key names.",
                              code="invalid_hotkey_arguments", tool="hotkey")
        return self._input_action("hotkey", arguments)

    def type_text(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Type the exact supplied string into the current desktop focus once.

        Focus and readiness are the caller's responsibility. This method does
        not acquire state, select a field, normalize text, or send special keys.
        """
        self._validate_desktop_input("type_text", arguments, {"text", "delay_ms"})
        if not isinstance(arguments.get("text"), str) or (
            "delay_ms" in arguments and not _integer(arguments["delay_ms"], 0, 200)
        ):
            raise DriverError("type_text requires text and an optional delay_ms integer from 0 to 200.",
                              code="invalid_type_text_arguments", tool="type_text")
        return self._input_action("type_text", arguments)

    @staticmethod
    def _validate_desktop_input(
        tool: str, arguments: Mapping[str, Any], fields: set[str],
    ) -> None:
        # Expose the native desktop form explicitly. Window/element input can
        # be added through these same native methods when the pipeline uses it.
        if (
            not isinstance(arguments, Mapping)
            or set(arguments) - {"scope", "session"} - fields
            or arguments.get("scope") != "desktop"
            or ("session" in arguments and not isinstance(arguments["session"], str))
        ):
            raise DriverError(f"{tool} requires scope desktop without a window or element target.",
                              code="invalid_desktop_input", tool=tool)

    def _input_action(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._call(tool, arguments)
        # These are the public 0.28.2 ActionResult facts, not OS dispatch names.
        effect, route = payload.get("effect"), payload.get("route")
        if (
            not isinstance(effect, str)
            or effect not in {"confirmed", "partial", "unverifiable", "suspected_noop"}
            or not isinstance(route, str)
            or route not in {"accessibility", "synthetic_events", "global_input", "system_api", "dom", "trusted_input"}
        ):
            error = DriverError(f"CUA {tool} returned no recognized action facts; delivery is unknown.",
                                code="invalid_action_result", tool=tool)
            error.context["action_result"] = payload
            raise error
        return payload

    def list_windows(self) -> list[dict[str, Any]]:
        payload = self._call("list_windows", {})
        windows = payload.get("windows")
        if not isinstance(windows, list):
            _invalid("windows")
        _optional(payload, "current_space_id", _integer)
        for window in windows:
            if not isinstance(window, dict):
                _invalid("windows record")
            required = ("pid", "window_id", "app_name", "title", "bounds", "is_on_screen", "z_index")
            if any(key not in window for key in required):
                _invalid("windows record (missing required field)")
            if not _integer(window["window_id"], maximum=2**64 - 1):
                _invalid("window_id")
            if window["pid"] is not None and not _integer(window["pid"], maximum=2**32 - 1):
                _invalid("pid")
            for key in ("app_name", "title"):
                if not isinstance(window[key], str):
                    _invalid(key)
            if not _bounds(window["bounds"], ("x", "y", "width", "height")):
                _invalid("bounds")
            if type(window["is_on_screen"]) is not bool:
                _invalid("is_on_screen")
            for key in ("z_index", "layer"):
                _optional(window, key, lambda value: type(value) is int)
            for key in ("minimized", "on_current_space"):
                _optional(window, key, lambda value: type(value) is bool)
            _optional(window, "current_space_id", _integer)
            _optional(window, "space_ids", lambda value: isinstance(value, list) and all(_integer(item) for item in value))
        return windows

    def get_window_state(self, window: Mapping[str, Any]) -> dict[str, Any]:
        self._recovery_snapshot = None
        if not isinstance(window, Mapping):
            raise DriverError("Select an observed window record before reading its state.")
        pid, window_id = window.get("pid"), window.get("window_id")
        if not _integer(pid, 1, 2**32 - 1) or not _integer(window_id, 1, 2**64 - 1):
            raise DriverError("The selected window lacks a usable observed pid/window_id pair; rediscover windows.")
        payload = self._call("get_window_state", {
            "pid": pid,
            "window_id": window_id,
            "include_accessibility_tree": True,
            "include_screenshot": False,
            "max_elements": self.max_elements,
            "max_depth": self.max_depth,
        })
        if (
            not _integer(payload.get("pid"), 1, 2**32 - 1)
            or not _integer(payload.get("window_id"), 1, 2**64 - 1)
            or payload["pid"] != pid
            or payload["window_id"] != window_id
        ):
            raise DriverError("CUA Driver state does not match the selected pid/window_id pair; rediscover windows.")
        for key in ("snapshot_id", "tree_markdown", "window_title", "app_name", "degraded_reason", "truncation_reason", "_note"):
            _optional(payload, key, lambda value: isinstance(value, str))
        for key in ("degraded", "elements_complete", "truncated"):
            _optional(payload, key, lambda value: type(value) is bool)
        for key in ("element_count", "total_element_count", "returned_element_count", "filtered_element_count"):
            _optional(payload, key, _integer)
        _optional(payload, "window_bounds", lambda value: _bounds(value, ("x", "y", "width", "height")))
        _optional(payload, "escalation", lambda value: isinstance(value, dict))
        elements = payload.get("elements")
        if elements is not None:
            if not isinstance(elements, list):
                _invalid("elements")
            indices: set[int] = set()
            for element in elements:
                self._validate_element(element)
                index = element["element_index"]
                if index in indices:
                    _invalid("elements (duplicate element_index)")
                indices.add(index)
            returned_count = payload.get("returned_element_count")
            if returned_count is not None and returned_count != len(elements):
                _invalid("returned_element_count (does not match elements)")
        if self._recovery_required:
            self._recovery_snapshot = payload
        return payload

    @staticmethod
    def _validate_element(element: Any) -> None:
        if not isinstance(element, dict):
            _invalid("element record")
        for key in ("element_index", "depth"):
            if not _integer(element.get(key)):
                _invalid(key)
        if not isinstance(element.get("role"), str):
            _invalid("role")
        for key in ("element_token", "label", "value", "value_description"):
            _optional(element, key, lambda value: isinstance(value, str))
        for key in ("enabled", "selected", "in_web_content"):
            _optional(element, key, lambda value: type(value) is bool)
        for key in ("min", "max"):
            _optional(element, key, _number)
        _optional(element, "parent_index", _integer)
        _optional(element, "frame", lambda value: _bounds(value, ("x", "y", "w", "h")))
        _optional(element, "actions", lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value))
