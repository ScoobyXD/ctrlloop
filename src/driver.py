"""One isolated headless Browser Use session per ctrlloop worker.

Browser Use owns DOM filtering, target identities and browser action execution.
This adapter does not create a second browser observation or action schema.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit

os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / ".local" / "browser-use"))
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

from browser_use.agent.views import ActionResult
from browser_use.browser.chrome import find_chrome_executable
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserStateSummary
from browser_use.tools.service import Tools


class DriverError(Exception):
    """A browser lifecycle or target error with a safe public summary."""

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


class BrowserDriver:
    """Own a local browser, fresh profile and native Browser Use target map."""

    def __init__(
        self, *, executable_path: str | Path | None = None,
        viewport: dict[str, int] | None = None,
    ) -> None:
        executable = str(executable_path) if executable_path is not None else find_chrome_executable()
        if executable is not None and not Path(executable).is_file():
            raise DriverError("The browser executable does not exist.", code="browser_executable_missing")
        # Browser Use recognizes this prefix as an already-temporary profile;
        # otherwise its Chrome profile setup creates a second, unowned copy.
        self._profile = tempfile.TemporaryDirectory(prefix="browser-use-user-data-dir-ctrlloop-", ignore_cleanup_errors=True)
        self.profile_path = Path(self._profile.name).resolve()
        self.browser = BrowserSession(
            headless=True, use_cloud=False, keep_alive=False,
            user_data_dir=self.profile_path,
            **({"executable_path": executable} if executable else {}),
            **({"viewport": viewport} if viewport is not None else {}),
        )
        self.tools = Tools()
        self._action_model = self.tools.registry.create_action_model(include_actions=["navigate", "click", "input"])
        self._started = False
        self._closed = False
        self._state: BrowserStateSummary | None = None
        self._capture_number = 0
        self.snapshot_id: str | None = None
        self._observed_target_id: str | None = None
        self._action_used = False

    async def start(self, url: str) -> None:
        """Start this browser and perform the caller-supplied initial navigation."""
        if self._started or self._closed:
            raise DriverError("This browser driver cannot be started twice.", code="browser_lifecycle_error")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("Provide an explicit starting URL.")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("The starting URL must be an absolute HTTP or HTTPS URL.")
        try:
            await self.browser.start()
            self._started = True
            actual_profile = self.browser.browser_profile.user_data_dir
            if actual_profile is None or Path(actual_profile).resolve() != self.profile_path:
                raise DriverError(
                    "Browser Use substituted the owned temporary profile.",
                    code="browser_profile_changed", tool="navigate",
                )
            action = self._action_model.model_validate({"navigate": {"url": url, "new_tab": False}})
            outcome = await self.tools.act(action, browser_session=self.browser)
        except DriverError:
            raise
        except Exception as error:
            raise DriverError(
                f"The headless browser could not start or navigate ({type(error).__name__}).",
                code="browser_start_failed", tool="navigate",
            ) from error
        if outcome.error:
            error = DriverError("Initial browser navigation failed.", code="browser_navigation_failed", tool="navigate")
            error.context["action_result"] = outcome.model_dump(mode="json", exclude_none=True)
            raise error

    async def get_state(self) -> BrowserStateSummary:
        """Return Browser Use's fresh native state, including its filtered map."""
        if not self._started or self._closed:
            raise DriverError("Start the browser before reading page state.", code="browser_not_started")
        self._state = None
        self.snapshot_id = None
        self._observed_target_id = None
        self._action_used = False
        try:
            state = await self.browser.get_browser_state_summary(include_screenshot=False, cached=False)
        except Exception as error:
            raise DriverError(
                f"Browser state capture failed ({type(error).__name__}).",
                code="browser_observation_failed", tool="get_browser_state_summary",
            ) from error
        self._state = state
        self._capture_number += 1
        # An application observation ID for reports; never passed to CDP as a node token.
        self.snapshot_id = f"{self.browser.id}:observation:{self._capture_number}"
        self._observed_target_id = self.browser.agent_focus_target_id
        return state

    async def act(self, action: str, index: int, text: str | None = None) -> ActionResult:
        """Submit one native action for the current observation; never replay it."""
        if action not in {"click", "type_text"}:
            raise DriverError("The selected action must be click or type_text.", code="unsupported_action")
        if type(index) is not int or self._state is None or self._action_used:
            raise DriverError("Capture fresh state before another action.", code="stale_browser_observation", tool=action)
        node = self._state.dom_state.selector_map.get(index)
        if node is None or self.browser.agent_focus_target_id != self._observed_target_id:
            raise DriverError("The target is not in the current browser observation.", code="stale_browser_target", tool=action)
        if await self.browser.get_element_by_index(index) is not node:
            raise DriverError("Browser Use replaced the observed target map.", code="stale_browser_target", tool=action)
        if action == "type_text":
            if not isinstance(text, str) or not text:
                raise DriverError("Typing requires nonempty prepared text.", code="missing_prepared_text", tool=action)
            payload = {"input": {"index": index, "text": text, "clear": False}}
        else:
            payload = {"click": {"index": index}}
        native_action = self._action_model.model_validate(payload)
        # Set before dispatch: an uncertain return must not make replay permissible.
        self._action_used = True
        try:
            return await self.tools.act(native_action, browser_session=self.browser)
        except Exception as error:
            raise DriverError(
                f"Browser action failed with unknown delivery ({type(error).__name__}); it was not retried.",
                code="browser_action_failed", tool=action,
            ) from error

    async def close(self) -> None:
        """Kill only this session and remove only its owned temporary profile."""
        if self._closed:
            return
        self._closed = True
        self._state = None
        try:
            await self.browser.kill()
        except Exception as error:
            raise DriverError(
                f"Owned browser cleanup failed ({type(error).__name__}).",
                code="browser_cleanup_failed", tool="close",
            ) from error
        finally:
            # TemporaryDirectory owns this exact path; never accept a caller's profile.
            if self.profile_path == Path(self._profile.name).resolve():
                self._profile.cleanup()
