"""Windows adapter for the installed CUA Driver 0.28.2 CLI.

The CLI flattens successful structured content onto stdout, but can also
print tool errors there with exit code zero. Every response is validated.
Native records and action results are retained without a parallel wire model.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any


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


def _failure(tool: str, exit_code: int, diagnostic: str) -> DriverError:
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
        code, reason = "driver_call_failed", "The CLI did not provide a recognized diagnostic."
    return DriverError(
        f"CUA Driver {tool} failed (exit {exit_code}). {reason}",
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
    """Discover and activate windows, capture state, and dispatch native clicks.

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
        runner: Callable[..., Any] | None = None,
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
        self._runner = runner or subprocess.run

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool not in {"list_windows", "get_window_state", "click", "bring_to_front"}:
            raise DriverError("Only window reads, bring_to_front, and a single click are available.")
        try:
            result = self._runner(
                [self.executable, "call", tool],
                input=json.dumps(arguments, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                check=False,
                shell=False,
                # CUA's telemetry worker can keep captured pipes open after exit.
                env={**os.environ, "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"},
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
        except subprocess.TimeoutExpired:
            raise DriverError(
                f"CUA Driver {tool} exceeded the client timeout.", code="client_timeout", tool=tool
            ) from None
        except FileNotFoundError:
            raise DriverError("CUA Driver executable was not found; supply its installed path.") from None
        except OSError:
            raise DriverError("CUA Driver could not run; check executable and desktop permissions.") from None
        except UnicodeError:
            raise DriverError("CUA Driver output could not be decoded as UTF-8.") from None
        if result.returncode != 0:
            if tool in {"click", "bring_to_front"}:
                try:
                    payload = json.loads(result.stdout, parse_constant=_reject_json_constant)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    error = _failure(tool, result.returncode, _action_diagnostic(payload) or result.stderr)
                    error.context["action_result"] = payload
                    raise error
            # Do not inspect success-looking JSON/tree output as a diagnostic.
            diagnostic = result.stderr or (
                result.stdout if not result.stdout.lstrip().startswith(("{", "[")) else ""
            )
            raise _failure(tool, result.returncode, diagnostic)
        try:
            payload = json.loads(result.stdout, parse_constant=_reject_json_constant)
        except (TypeError, ValueError):
            if not result.stdout.lstrip().startswith(("{", "[")):
                raise _failure(tool, result.returncode, result.stdout or result.stderr) from None
            raise DriverError(
                f"CUA Driver {tool} did not return structured JSON; "
                "the CLI may have reported a tool error despite exit zero."
            ) from None
        if not isinstance(payload, dict):
            raise DriverError(f"CUA Driver {tool} did not return an object.")
        if tool in {"click", "bring_to_front"}:
            # Action facts use a different native contract from observations.
            # In particular, an effect of "unverifiable" is a legitimate result.
            if (
                any(key in payload for key in ("refusal", "code", "error"))
                or ("isError" in payload and payload["isError"] is not False)
                or ("status" in payload and payload["status"] != "success")
                or payload.get("effect") == "refused"
            ):
                error = _failure(tool, 0, _action_diagnostic(payload))
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
            raise _failure(tool, result.returncode, diagnostic)
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
        payload = self._call("click", arguments)
        # These are the public 0.28.2 ActionResult facts, not OS dispatch names.
        effect, route = payload.get("effect"), payload.get("route")
        if (
            not isinstance(effect, str)
            or effect not in {"confirmed", "partial", "unverifiable", "suspected_noop"}
            or not isinstance(route, str)
            or route not in {"accessibility", "synthetic_events", "global_input", "system_api", "dom", "trusted_input"}
        ):
            error = DriverError("CUA click returned no recognized action facts; delivery is unknown.",
                                code="invalid_action_result", tool="click")
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
