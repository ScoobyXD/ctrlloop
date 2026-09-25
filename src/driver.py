"""Attach each command to a durable, independently running headless browser.

Browser Use owns DOM filtering, target identities and browser action execution.
This adapter does not create a second browser observation or action schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlsplit

os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / ".local" / "browser-use"))
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

from browser_use.agent.views import ActionResult
from browser_use.browser.chrome import find_chrome_executable, get_chrome_profile_path
from browser_use.browser.events import NavigateToUrlEvent, StorageStateLoadedEvent
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserStateSummary
from browser_use.tools.service import Tools

# Apply after Browser Use's own logger setup, including explicitly configured
# child loggers. CDP payloads contain authentication values.
for _transport_logger in ("cdp_use", "cdp_use.client", "websockets.client"):
    logging.getLogger(_transport_logger).setLevel(logging.WARNING)


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
    """Control a named persistent browser; close() only detaches the command."""

    def __init__(
        self, *, executable_path: str | Path | None = None,
        viewport: dict[str, int] | None = None,
        user_data_dir: str | Path | None = None,
        profile_directory: str | None = None,
        fresh_profile: bool = False,
        browser_profile: str = "default",
    ) -> None:
        if fresh_profile and (user_data_dir is not None or profile_directory is not None):
            raise DriverError(
                "--fresh-profile cannot be combined with --user-data-dir or --profile-directory.",
                code="browser_profile_configuration_invalid",
            )
        if __package__:
            from .browser_runtime import BrowserRuntime
        else:
            from browser_runtime import BrowserRuntime
        self.runtime = BrowserRuntime(browser_profile, executable_path=executable_path, viewport=viewport)
        self.profile_path = self.runtime.profile_path
        self._fresh_profile = fresh_profile
        self._source_user_data_dir = user_data_dir
        self._source_profile_directory = profile_directory
        self._source_profile: Path | None = None
        self._executable = str(executable_path) if executable_path is not None else find_chrome_executable()
        self._viewport = viewport
        self.browser: BrowserSession | None = None
        self.session_info: dict[str, Any] = {
            "mode": "persistent", "browser_profile": browser_profile,
            "persistent_user_data_dir": str(self.profile_path),
            "fresh_profile": fresh_profile, "headless": True,
            "close_behavior": "disconnect_only",
        }
        self.tools = Tools()
        self._action_model = self.tools.registry.create_action_model(include_actions=["navigate", "click", "input"])
        self._started = False
        self._closed = False
        self._state: BrowserStateSummary | None = None
        self._capture_number = 0
        self.snapshot_id: str | None = None
        self._observed_target_id: str | None = None
        self._action_used = False
        self._navigation_response: dict[str, Any] | None = None
        self._runtime_acquired = False

    @staticmethod
    def _resolve_user_profile(
        user_data_dir: str | Path | None, profile_directory: str | None, executable: str | None,
    ) -> tuple[Path, str]:
        """Select Chrome's last-used profile without reading account identities."""
        source = user_data_dir if user_data_dir is not None else get_chrome_profile_path(None, executable_path=executable)
        if source is None:
            raise DriverError(
                "No Chrome profile location was found. Provide --user-data-dir, or use --fresh-profile to run signed out.",
                code="browser_profile_missing",
            )
        root = Path(source).expanduser().resolve()
        def valid_directory(name: Any) -> bool:
            if not isinstance(name, str) or not name or name in {".", ".."} or any(c in name for c in "/\\:"):
                return False
            candidate = root / name
            return (
                candidate.resolve().parent == root and candidate.resolve() == candidate
                and candidate.is_dir() and (candidate / "Preferences").is_file()
            )

        try:
            if not root.is_dir() or not (root / "Local State").is_file():
                raise DriverError(
                    "The Chrome user-data directory or its Local State file is missing. Open Chrome and sign in, "
                    "or provide --user-data-dir pointing to the directory containing Local State. "
                    "Use --fresh-profile only to run signed out.",
                    code="browser_profile_missing",
                )
            with (root / "Local State").open(encoding="utf-8") as handle:
                local_state = json.load(handle)
            metadata = local_state.get("profile", {}) if isinstance(local_state, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            if profile_directory is not None:
                if not valid_directory(profile_directory):
                    raise DriverError(
                        "--profile-directory must name one existing profile directly inside --user-data-dir "
                        "(for example Default or Profile 1), containing its Preferences file.",
                        code="browser_profile_missing",
                    )
                selected = profile_directory
            elif valid_directory(metadata.get("last_used")):
                selected = metadata["last_used"]
            else:
                available = sorted(child.name for child in root.iterdir() if valid_directory(child.name))
                if len(available) != 1:
                    failure = DriverError(
                        "Chrome's last-used profile could not be determined. Select one with --profile-directory, "
                        "or open Chrome with the intended profile and retry.",
                        code="browser_profile_ambiguous" if available else "browser_profile_missing",
                    )
                    failure.context["available_profile_directories"] = available
                    raise failure
                selected = available[0]
            return root, selected
        except DriverError as error:
            error.context.update({"source_user_data_dir": str(root), "profile_directory": profile_directory})
            raise
        except (OSError, ValueError) as error:
            failure = DriverError(
                "The Chrome profile metadata is unreadable. Select your running Chrome user-data directory "
                "with --user-data-dir. Use --fresh-profile only to run signed out.",
                code="browser_profile_unreadable",
            )
            failure.context.update({
                "source_user_data_dir": str(root), "profile_directory": locals().get("selected", profile_directory),
                "cause_type": type(error).__name__,
            })
            raise failure from error

    @staticmethod
    def _cookie_identity(cookie: dict[str, Any]) -> tuple[str, str, str, str, str, int]:
        return (
            cookie["domain"], cookie["name"], cookie["path"],
            json.dumps(cookie.get("partitionKey"), sort_keys=True, separators=(",", ":")),
            cookie.get("sourceScheme", "Unset"), cookie.get("sourcePort", -1),
        )

    async def _import_live_session(self, url: str) -> dict[tuple[str, str, str, str, str, int], tuple[float, bytes]]:
        """Transfer native storage state in memory; retain only hashes for checks."""
        if self._fresh_profile:
            return {}
        if __package__:
            from .browser_session import import_live_session
        else:
            from browser_session import import_live_session
        storage, metadata = await import_live_session(
            self._source_profile, self.session_info["profile_directory"], self._executable, url,
        )
        self.session_info.update(metadata)
        expected = {
            self._cookie_identity(cookie): (
                cookie.get("expires", -1), hashlib.sha256(cookie["value"].encode()).digest(),
            ) for cookie in storage.get("cookies", [])
        }
        try:
            self.browser.browser_profile.storage_state = storage
        except Exception:
            # Pydantic validation errors can include the supplied input value.
            raise DriverError(
                "The imported session could not be prepared for Browser Use.",
                code="browser_session_import_failed",
            ) from None
        return expected

    async def _verify_cookie_import(self, expected: dict[tuple[str, str, str, str, str, int], tuple[float, bytes]]) -> None:
        """Check imported values and partition identities before any navigation."""
        if self._fresh_profile:
            return
        storage = self.browser.browser_profile.storage_state
        loaded_events = [
            event for event in self.browser.event_bus.event_history.values()
            if isinstance(event, StorageStateLoadedEvent)
        ]
        if not isinstance(storage, dict) or not any(
            event.cookies_count == len(storage.get("cookies", []))
            and event.origins_count == len(storage.get("origins", []))
            for event in loaded_events
        ):
            raise DriverError(
                "Browser Use did not finish loading the imported cookies and origin storage.",
                code="browser_session_unavailable",
            )
        try:
            cookies = await asyncio.wait_for(self.browser.cookies(), timeout=10)
            loaded = {
                self._cookie_identity(cookie): hashlib.sha256(cookie["value"].encode()).digest()
                for cookie in cookies
            }
            loaded_count = len(cookies)
            del cookies
        except Exception as error:
            failure = DriverError(
                "The worker's imported browser session could not be verified.",
                code="browser_session_unavailable",
            )
            failure.context.update({"browser_session": dict(self.session_info), "cause_type": type(error).__name__})
            raise failure from None
        now = time.time()
        needed = {key: digest for key, (expiry, digest) in expected.items() if expiry <= 0 or expiry > now}
        missing = sum(loaded.get(key) != digest for key, digest in needed.items())
        self.session_info.update({"loaded_cookie_count": loaded_count, "missing_or_changed_cookie_count": missing})
        if missing:
            self.session_info["cookie_import_status"] = "cookies_missing_or_changed"
            failure = DriverError(
                "The worker could not retain all imported login cookies. Startup stopped before navigation; "
                "it will not silently continue signed out.", code="browser_session_unavailable",
            )
            failure.context["browser_session"] = dict(self.session_info)
            raise failure
        self.session_info["cookie_import_status"] = "cookie_values_verified" if needed else "no_source_cookies"

    async def _watch_navigation_response(self) -> Callable[[], None]:
        """Observe the main document's status on this existing CDP connection."""
        session = await self.browser.get_or_create_cdp_session()
        registry = session.cdp_client.register._registry
        methods = ("Network.requestWillBeSent", "Network.responseReceivedExtraInfo")
        # The pinned CDP registry allows one callback per method. Never replace
        # a native Browser Use consumer (for example, an enabled HAR recorder).
        if any(method in registry.get_registered_methods() for method in methods):
            return lambda: None
        frame_tree = await session.cdp_client.send.Page.getFrameTree(session_id=session.session_id)
        main_frame_id = frame_tree["frameTree"]["frame"]["id"]
        await session.cdp_client.send.Network.enable(session_id=session.session_id)

        def request(event: dict[str, Any], session_id: str | None) -> None:
            if session_id == session.session_id and event.get("type") == "Document" and event.get("frameId") == main_frame_id:
                self._navigation_response = {
                    "requestId": event["requestId"], "url": event["request"]["url"],
                }

        def response(event: dict[str, Any], session_id: str | None) -> None:
            if session_id == session.session_id and self._navigation_response is not None:
                if event.get("requestId") == self._navigation_response["requestId"]:
                    # ExtraInfo also arrives when an HTTP failure prevents a
                    # responseReceived event; keep no headers/cookies/bodies.
                    self._navigation_response["statusCode"] = event["statusCode"]

        callbacks = (request, response)
        # CDP awaits above may let a native consumer register in the meantime.
        if any(method in registry.get_registered_methods() for method in methods):
            return lambda: None
        for method, callback in zip(methods, callbacks):
            registry.register(method, callback)

        def stop() -> None:
            for method, callback in zip(methods, callbacks):
                if registry._handlers.get(method) is callback:
                    registry.unregister(method)

        return stop

    async def _navigation_context(self, url: str) -> dict[str, Any]:
        """Keep the native navigation cause that Tools.act reduces to unavailable."""
        context: dict[str, Any] = {
            "requested_url": url, "fresh_profile": self._fresh_profile, "headless": True,
            "browser_session": dict(self.session_info),
        }
        if self._navigation_response is not None:
            context["document_response"] = dict(self._navigation_response)
        for event in reversed(list(self.browser.event_bus.event_history.values())):
            if isinstance(event, NavigateToUrlEvent) and event.url == url:
                failures = [str(result.error) for result in event.event_results.values() if result.error is not None]
                if failures:
                    context["navigation_errors"] = failures
                break
        try:
            # Native cached target information does not refresh the selector map.
            target = await self.browser.get_current_target_info()
            if target is not None:
                context["current_page"] = target
        except Exception:
            # Diagnostics must never replace the original navigation failure.
            pass
        if any("ERR_HTTP_RESPONSE_CODE_FAILURE" in item for item in context.get("navigation_errors", [])):
            context["guidance"] = (
                "Chrome rejected the navigation because of an HTTP response failure. "
                + ("The browser profile was initialized signed out. " if self._fresh_profile else
                   "The browser uses a persistent profile. Session reuse does not guarantee site access. ")
                +
                "The response alone does not identify whether the site or an intermediary "
                "rejected the request."
            )
        return context

    async def start(self, url: str | None = None) -> None:
        """Attach to the existing browser, optionally navigating its current tab."""
        if self._started or self._closed:
            raise DriverError("This browser driver cannot be started twice.", code="browser_lifecycle_error")
        if url is not None:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("The starting URL must be an absolute HTTP or HTTPS URL.")
        try:
            self.runtime.acquire()
            self._runtime_acquired = True
            saved = self.runtime.read_state()
            initialized = bool(saved.get("initialized"))
            if initialized and self._fresh_profile:
                raise DriverError(
                    "This browser profile already exists. Omit --fresh-profile to reuse it, or choose "
                    "a new --browser-profile NAME to create a separate signed-out profile.",
                    code="browser_profile_already_initialized",
                )
            if initialized and (self._source_user_data_dir is not None or self._source_profile_directory is not None):
                raise DriverError(
                    "This browser profile already has its own persistent session. Source Chrome profile flags "
                    "only apply on first use; choose a new --browser-profile NAME to initialize another account.",
                    code="browser_profile_already_initialized",
                )
            if not initialized and not self._fresh_profile and url is None:
                raise DriverError(
                    "The first command for this browser profile needs --url to import its initial login. "
                    "Later commands can omit --url to continue the current page.",
                    code="browser_profile_initialization_required",
                )
            endpoint = self.runtime.ensure_started()
            self.session_info.update(self.runtime.metadata)
            # External CDP ownership is deliberate: Browser Use must never
            # launch, copy, kill, or delete the persistent runtime's profile.
            self.browser = BrowserSession(
                cdp_url=endpoint, is_local=False, use_cloud=False, headless=True,
                keep_alive=True, user_data_dir=self.profile_path,
                downloads_path=self.profile_path.parent / "downloads",
                viewport=self.runtime.read_state()["viewport"],
            )
            if self.browser.is_local or Path(self.browser.browser_profile.user_data_dir).resolve() != self.profile_path:
                raise DriverError("Browser Use substituted the persistent profile or ownership.", code="browser_profile_changed")
            expected_cookies = None
            if not initialized and not self._fresh_profile:
                self._source_profile, source_directory = self._resolve_user_profile(
                    self._source_user_data_dir, self._source_profile_directory, self._executable,
                )
                self.session_info.update({
                    "source_user_data_dir": str(self._source_profile), "profile_directory": source_directory,
                })
                expected_cookies = await self._import_live_session(url)
            await self.browser.start()
            self._started = True
            if expected_cookies is not None:
                await self._verify_cookie_import(expected_cookies)
                # Do not reapply the original snapshot if this adapter reconnects.
                self.browser.browser_profile.storage_state = None
            if not initialized:
                self.runtime.update_state(initialized=True, initialization_mode=(
                    "signed_out" if self._fresh_profile else "live_session_import"
                ))
            self.session_info["session_source"] = (
                "persistent_profile" if initialized else "signed_out" if self._fresh_profile else "initial_live_import"
            )
            last_target = saved.get("last_target_id")
            targets = self.browser.session_manager.get_all_page_targets()
            if last_target and any(target.target_id == last_target for target in targets):
                await self.browser.get_or_create_cdp_session(last_target, focus=True)
            if url is None:
                self.session_info["navigation"] = "continued_current_page"
                return
            action = self._action_model.model_validate({"navigate": {"url": url, "new_tab": False}})
            try:
                stop_observing = await self._watch_navigation_response()
            except Exception:
                stop_observing = lambda: None
            try:
                outcome = await self.tools.act(action, browser_session=self.browser)
            finally:
                stop_observing()
            if outcome.error:
                context = await self._navigation_context(url)
                status = context.get("document_response", {}).get("statusCode")
                message = f"Initial browser navigation failed (HTTP {status})." if status is not None else "Initial browser navigation failed."
                error = DriverError(message, code="browser_navigation_failed", tool="navigate")
                error.context["action_result"] = outcome.model_dump(mode="json", exclude_none=True)
                error.context.update(context)
                raise error
        except DriverError:
            raise
        except Exception as error:
            failure = DriverError(
                f"The persistent browser could not {'navigate' if self._started else 'connect'} ({type(error).__name__}).",
                code="browser_navigation_failed" if self._started else "browser_start_failed", tool="navigate",
            )
            failure.context["browser_session"] = dict(self.session_info)
            raise failure from None

    async def screenshot(self) -> bytes:
        """Capture native PNG bytes without refreshing Browser Use's target map."""
        if not self._started or self._closed:
            raise DriverError("Start the browser before capturing a screenshot.", code="browser_not_started")
        try:
            return await self.browser.take_screenshot()
        except Exception as error:
            raise DriverError(
                f"Browser screenshot failed ({type(error).__name__}).",
                code="browser_screenshot_failed", tool="take_screenshot",
            ) from error

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
        """Release this command's connection and lock, leaving Chrome and its data alive."""
        if self._closed:
            return
        self._closed = True
        self._state = None
        failure: Exception | None = None
        try:
            if self.browser is not None:
                if self._runtime_acquired and self.browser.agent_focus_target_id:
                    try:
                        self.runtime.update_state(last_target_id=self.browser.agent_focus_target_id)
                    except Exception as error:
                        failure = error
                # stop() with external ownership and keep_alive=True detaches
                # CDP/watchdogs. It does not send Browser.close or kill Chrome.
                try:
                    await self.browser.stop()
                except Exception as error:
                    failure = failure or error
                    await self.browser.reset()
        finally:
            if self._runtime_acquired:
                self.runtime.release()
                self._runtime_acquired = False
        if failure is not None:
            raise DriverError(
                f"Browser disconnect failed ({type(failure).__name__}); the persistent browser was left running.",
                code="browser_disconnect_failed", tool="close",
            ) from None
