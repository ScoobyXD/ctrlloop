"""Bounded instruction executor running inside the assigned Omarchy desktop.

Cloud delivery lives separately. This module receives the persisted instruction,
uses the existing Jev rounds, and accepts success only from fresh native state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import httpx2
from typesafe_sdk import RetryPolicy, TypeSafeClient

from .driver import CuaDriver, DriverError
from .pipeline import PipelineResult, run_pipeline
from .reporting import RunReport
from .selection import Candidate, ChoiceSelector, PINNED_MODEL, SelectionError


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _instruction_url(instruction: str) -> str:
    """Parse the LLM's real string, enforcing this MVP's root-navigation scope."""
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 2000:
        raise ValueError("The persisted instruction must contain 1–2000 characters.")
    if not re.search(r"\bchrome\b", instruction, re.I) or not re.search(r"\b(open|navigate|visit|go to)\b", instruction, re.I):
        raise ValueError("This MVP executes instructions to open a site in Google Chrome.")
    # Find complete URL-like tokens, including unsupported destinations. Looking
    # only for an allowed host can mistake part of a larger URL for the target
    # or silently ignore a second destination elsewhere in the instruction.
    urls = []
    for token in re.split(r"[\s<>\"'`\[\]()]+", instruction):
        value = token.rstrip(".,;!")
        if (re.match(r"[a-z][a-z0-9+.-]*:.+", value, re.I)
                or re.search(r"(?:[\w-]+\.)+[\w-]+", value)):
            urls.append(value)
    if len(urls) != 1:
        raise ValueError("The generated instruction must contain exactly one X navigation URL.")
    value = urls[0]
    parsed = urlsplit(value if "://" in value else "https://" + value)
    if (parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com"}
            or parsed.netloc.lower() not in {"x.com", "www.x.com"}
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(character in value for character in ("?", "#", "\\"))):
        raise ValueError("This MVP supports opening the HTTPS X home page only.")
    return "https://" + parsed.hostname + "/"


def _address(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    matches = [row for row in snapshot.get("elements", [])
               if row.get("role") == "entry" and row.get("label") == "Address and search bar"
               and row.get("enabled") is not False and row.get("frame")]
    return matches[0] if len(matches) == 1 else None


def _target(snapshot: dict[str, Any], element: dict[str, Any]) -> dict[str, Any]:
    arguments = {key: snapshot[key] for key in ("pid", "window_id")}
    if element.get("element_token"):
        arguments["element_token"] = element["element_token"]
    else:
        arguments.update(element_index=element["element_index"], snapshot_id=snapshot["snapshot_id"])
    return {**arguments, "delivery_mode": "foreground"}


def _site_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if parsed.scheme == "https" and parsed.hostname in {"x.com", "www.x.com"} and not parsed.username and not parsed.password:
            return parsed.geturl()
    except ValueError:
        pass
    return None


def _guard_observation(snapshot: dict[str, Any]) -> None:
    if snapshot.get("app_name") != "google-chrome":
        raise DriverError("The selected window is outside this MVP's Google Chrome scope.", code="unsupported_window")
    text = (snapshot.get("tree_markdown", "") + " " + snapshot.get("window_title", "")).lower()
    # Legal/permission prompts must be resolved by the user. Never offer their
    # acceptance controls to Jev as navigation candidates.
    dialogs = [row for row in snapshot.get("elements", []) if row.get("role") in {"dialog", "alert"}]
    if "google chrome terms" in text or (dialogs and any(phrase in text for phrase in ("accept and install", "accept and continue", "terms of service"))):
        raise DriverError("Chrome is showing a terms prompt that requires the user's confirmation.", code="browser_consent_required")
    if dialogs:
        raise DriverError("Chrome has an active dialog; resolve it in takeover mode before retrying.", code="browser_dialog_requires_user")


def _navigation_candidates(stage: str, candidates: list[Candidate]) -> list[Candidate]:
    if stage == "windows":
        return [candidate for candidate in candidates if candidate.record.get("app_name") == "google-chrome"]
    # Use actual observed Chrome toolbar controls. Page text and account/feed
    # controls never enter the Jev request or this executor's action scope.
    return [candidate for candidate in candidates
            if candidate.record.get("enabled") is not False and candidate.record.get("frame")
            and any(parent.get("role") == "tool bar"
                    for parent in candidate.description.get("indexed_ancestors_nearest_first", []))
            and ((candidate.record.get("role") == "entry" and candidate.record.get("label") == "Address and search bar")
                 or (candidate.record.get("role") == "button" and candidate.record.get("label") in {"Home", "Reload"}))]


def _evidence(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    address = _address(snapshot)
    url = _site_url(address.get("value") if address else None)
    if not url:
        return None
    title = snapshot.get("window_title", "")
    documents = [row.get("label", "") for row in snapshot.get("elements", [])
                 if row.get("role") in {"document web", "document", "web area", "webarea"}]
    visible_text = snapshot.get("tree_markdown", "").lower()
    if any(text in visible_text for text in ("err_name_not_resolved", "err_connection", "this site can’t be reached", "this site can't be reached")):
        raise DriverError("Chrome reached a browser network error page.", code="site_network_error")
    # The address entry alone may still be an uncommitted edit. Require a loaded
    # document with an X title in the same fresh native-window observation.
    is_x_document = any(label.strip().lower() == "x" or label.lower().endswith(" / x")
                        or "it's what's happening" in label.lower() for label in documents)
    if not is_x_document:
        return None
    return {"verified_url": url, "window_title": title, "document_titles": documents[:3],
            "source": "cua_driver.get_window_state", "snapshot_id": snapshot.get("snapshot_id"),
            "observed_at": _timestamp(), "scope": "X URL and loaded page observed; feed contents not evaluated"}


async def execute_instruction(
    instruction: str, emit: Callable[[dict[str, Any]], Awaitable[None]],
    *, report_directory: Path | None = None,
) -> dict[str, Any]:
    """Execute one delivered instruction; never retry uncertain desktop input."""
    report = RunReport(report_directory or Path(os.environ.get("CTRLLOOP_REPORT_DIR", "reports")))
    driver = CuaDriver(local_endpoint=os.environ.get("CUA_LOCAL_MCP_URL", "http://127.0.0.1:3000/mcp"),
                       local_token=os.environ.get("CUA_LOCAL_MCP_TOKEN"))
    final: dict[str, Any] = {"status": "error"}
    evidence: dict[str, Any] | None = None
    latest: dict[str, Any] = {}
    latest_result: PipelineResult | None = None

    async def event(stage: str, **details: Any) -> None:
        await emit({"stage": stage, "timestamp": _timestamp(), **details})

    async def pipeline_event(name: str, details: dict[str, Any]) -> None:
        stages = {"windows_discovered": "desktop_observed", "window_selected": "jev_window_selection",
                  "control_selected": "jev_selection", "action_requested": "action_dispatch",
                  "action_returned": "action_returned"}
        messages = {"windows_discovered": "Observed the desktop windows", "window_selected": "Selected the Chrome window",
                    "control_selected": "Jev selected the next Chrome control and action", "action_requested": "Sending native input to Chrome",
                    "action_returned": "Native input returned; completion depends on the observed page"}
        await event(stages.get(name, name), message=messages.get(name, name), **details)

    async def observe(snapshot: dict[str, Any], result: PipelineResult) -> bool:
        nonlocal latest, latest_result
        latest = snapshot
        latest_result = result
        _guard_observation(snapshot)
        return False

    async def before_action(result: PipelineResult, tool: str, arguments: dict[str, Any]) -> None:
        nonlocal latest
        control = result.control or {}
        if control.get("role") != "entry" or control.get("label") != "Address and search bar":
            raise DriverError("Jev selected a control outside the bounded navigation action scope.", code="unsupported_control")
        if tool == "type_text":
            # Select-all applies only to Jev's freshly observed address field.
            # Re-observe and resolve a fresh token before the actual text action.
            with report.span("navigation.select_existing_address", tool="press_key"):
                selected = await driver.call("press_key", {**_target(latest, control), "key": "a", "modifiers": ["ctrl"]})
            report.append("address_selection_returned", result=selected)
            if selected.get("effect") not in {"confirmed", "unverifiable"}:
                raise DriverError("Chrome did not confirm address-selection input; it was not replayed.", code="address_selection_unconfirmed")
            latest = await driver.call("get_window_state", {**{key: latest[key] for key in ("pid", "window_id")}, "include_screenshot": False})
            _guard_observation(latest)
            address = _address(latest)
            if address is None:
                raise DriverError("The selected address field disappeared before typing.", code="navigation_target_changed")
            arguments.clear()
            arguments.update(_target(latest, address), text=url)
            result.control, result.snapshot_id = address, latest.get("snapshot_id")

    async def after_action(result: PipelineResult, tool: str) -> bool:
        nonlocal evidence, latest
        target = {key: result.window[key] for key in ("pid", "window_id")}
        latest = await driver.call("get_window_state", {**target, "include_screenshot": False})
        _guard_observation(latest)
        if tool != "type_text":
            return False
        address = _address(latest)
        typed_url = _site_url(address.get("value") if address else None)
        if typed_url is None or typed_url.rstrip("/") != url.rstrip("/"):
            raise DriverError("The exact navigation URL was not read back from Chrome; Enter was not sent.", code="address_readback_failed")
        await event("action_dispatch", tool="press_key", key="Enter", purpose="Commit the observed navigation URL")
        with report.span("navigation.commit", tool="press_key"):
            report.append("action_requested", tool="press_key", boundary="before_cua_driver_call", purpose="commit_verified_address")
            committed = await driver.call("press_key", {**_target(latest, address), "key": "Return"})
        report.append("navigation_committed", result=committed, boundary="last_navigation_command_returned")
        if committed.get("effect") not in {"confirmed", "unverifiable"}:
            raise DriverError("Chrome did not confirm navigation input delivery; it was not replayed.", code="navigation_dispatch_unconfirmed")
        await event("verification_started", message="Waiting for X's URL and loaded page in fresh native snapshots")
        with report.span("navigation.verification"):
            for attempt in range(20):
                if attempt:
                    await asyncio.sleep(1)
                latest = await driver.call("get_window_state", {**target, "include_screenshot": False})
                evidence = _evidence(latest)
                if evidence:
                    report.append("observed_result", **evidence)
                    await event("observed_result", **evidence)
                    return True
        raise DriverError("Navigation was dispatched, but a loaded X page was not verified within 20 observations.", code="outcome_not_verified")

    try:
        url = _instruction_url(instruction)
        report.append("run_started", request=instruction, backend="cua_driver", worker="local_omarchy",
                      jev_model=PINNED_MODEL, url=url)
        await event("execution_started", instruction=instruction, jev_model=PINNED_MODEL, message="Executing the saved instruction inside the assigned desktop")
        api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not api_key:
            raise ValueError("The VM worker is missing TYPESAFE_API_KEY.")
        with report.span("driver_initialization"):
            await driver.start()
        report.append("driver_connected", **driver.session_info)
        await event("driver_connected", driver=driver.server_info, transport="vm_loopback", message="Connected to the desktop's native Cua Driver")
        with httpx2.Client(timeout=30.0, event_hooks={"request": [report.on_request], "response": [report.on_response]}) as http_client:
            with TypeSafeClient(api_key=api_key, base_url="https://api.typesafe.ai", model=PINNED_MODEL,
                                timeout=30.0, retry=RetryPolicy(max_retries=0), http_client=http_client) as client:
                result = await run_pipeline(instruction, driver, ChoiceSelector(client, model=PINNED_MODEL),
                                            report=report, max_iterations=4, text=url, screenshots=True,
                                            on_observation=observe, before_action=before_action,
                                            after_action=after_action, on_event=pipeline_event,
                                            candidate_filter=_navigation_candidates)
        final = {"status": "verified" if result.status == "verified" and evidence else "error",
                 "pipeline": result.to_dict(), "evidence": evidence}
        if evidence:
            final["verified_url"] = evidence["verified_url"]
        else:
            final.update(code=result.status, error=result.reason or "The requested page was not verified.")
            await event("execution_error", code=final["code"], error=final["error"])
    except Exception as error:
        safe = str(error) if isinstance(error, (DriverError, SelectionError, ValueError)) else f"Worker failed: {type(error).__name__}."
        final = {"status": "error", "error": safe, "code": getattr(error, "code", "execution_failed")}
        if latest_result is not None:
            latest_result.status, latest_result.reason = "error", safe
            final["pipeline"] = latest_result.to_dict()
        report.append("run_error", **final)
        await event("execution_error", **final)
    finally:
        try:
            await driver.close()
        except Exception as error:
            final["cleanup_error"] = getattr(error, "code", type(error).__name__)
        report.append("run_finished", result=final)
        final["timing"] = await asyncio.to_thread(report.close)
        final["report_path"] = str(report.path)
    return final
