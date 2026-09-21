"""Read-only Windows adapter for the installed CUA Driver 0.28.2 CLI.

The CLI flattens successful structured content onto stdout, but can also
print tool errors there with exit code zero. Every response is validated.
Snapshot handles returned here are observation data, not reusable actions.
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
    """A sanitized driver transport, refusal, or observation-contract error."""


def _integer(value: Any, minimum: int = 0, maximum: int | None = None) -> bool:
    return (
        type(value) is int
        and value >= minimum
        and (maximum is None or value <= maximum)
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
    """Discover windows and capture one accessibility snapshot without acting.

    Optional/nullable observation fields are preserved, including missing
    elements or snapshot identity. The pipeline decides whether that observation
    is usable. A false ``elements_complete`` flag is normal on Windows 0.28.2.
    No method starts the daemon or changes window focus.
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
        if tool not in {"list_windows", "get_window_state"}:
            raise DriverError("Only read-only CUA Driver tools are available.")
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
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
        except subprocess.TimeoutExpired:
            raise DriverError(f"CUA Driver {tool} timed out; verify daemon and provider availability.") from None
        except FileNotFoundError:
            raise DriverError("CUA Driver executable was not found; supply its installed path.") from None
        except OSError:
            raise DriverError("CUA Driver could not run; check executable and desktop permissions.") from None
        except UnicodeError:
            raise DriverError("CUA Driver output could not be decoded as UTF-8.") from None
        if result.returncode != 0:
            raise DriverError(
                f"CUA Driver {tool} failed (exit {result.returncode}); "
                "check access to the existing interactive-desktop daemon."
            )
        try:
            payload = json.loads(result.stdout, parse_constant=_reject_json_constant)
        except (TypeError, ValueError):
            raise DriverError(
                f"CUA Driver {tool} did not return structured JSON; "
                "the CLI may have reported a tool error despite exit zero."
            ) from None
        if not isinstance(payload, dict):
            raise DriverError(f"CUA Driver {tool} did not return an object.")
        # The advertised refusal alternative has no stable value enum. None of
        # these markers is in the documented success payload, so fail closed.
        if any(key in payload for key in ("refusal", "status", "code", "error")):
            raise DriverError(f"CUA Driver {tool} returned a refusal or status response.")
        if "isError" in payload and payload["isError"] is not False:
            raise DriverError(f"CUA Driver {tool} returned a tool error.")
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
