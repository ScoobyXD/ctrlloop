"""Bounded executor using the existing Jev selection and native action pipeline.

Delivery and authorization live separately. No application-specific candidates,
typing content, navigation gestures, or completion claims are invented here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import httpx2
from typesafe_sdk import RetryPolicy, TypeSafeClient

from .driver import CuaDriver, DriverError
from .pipeline import run_pipeline
from .reporting import RunReport
from .selection import ChoiceSelector, PINNED_MODEL, SelectionError


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def execute_instruction(
    instruction: str, emit: Callable[[dict[str, Any]], Awaitable[None]],
    *, report_directory: Path | None = None, run_id: str | None = None,
    report: RunReport | None = None,
) -> dict[str, Any]:
    """Execute once; retain evidence and never retry uncertain desktop input."""
    report = report or RunReport(report_directory or Path(os.environ.get("CTRLLOOP_REPORT_DIR", "reports")), run_id=run_id)
    driver = None
    final: dict[str, Any] = {"status": "error"}

    async def event(stage: str, **details: Any) -> None:
        payload = {"stage": stage, "timestamp": _timestamp(), **details}
        report.append("worker_progress", **payload)
        await emit(payload)

    async def pipeline_event(name: str, details: dict[str, Any]) -> None:
        stages = {"windows_discovered": "desktop_observed", "window_selected": "jev_window_selection",
                  "control_selected": "jev_selection", "action_requested": "action_dispatch",
                  "action_returned": "action_returned"}
        messages = {"windows_discovered": "Observed desktop windows", "window_selected": "Selected the next window",
                    "control_selected": "Jev selected the next control and action", "action_requested": "Sending native input",
                    "action_returned": "Native input returned; task completion remains unverified"}
        if name == "window_selected" and not details.get("rounds"):
            stages[name] = "window_selection"
            messages[name] = "Selected the only observed window; no Jev window call was needed"
        await event(stages.get(name, name), message=messages.get(name, name), **details)

    try:
        report.append("run_started", run_id=run_id, request=instruction, backend="cua_driver",
                      worker="local_omarchy", jev_model=PINNED_MODEL, max_iterations=4,
                      capabilities={"actions": ["click", "type_text"], "generated_typing_content": False,
                                    "general_completion_verifier": False})
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 2000:
            raise ValueError("The persisted instruction must contain 1–2000 characters.")
        await event("execution_started", instruction=instruction, jev_model=PINNED_MODEL,
                    message="Executing the saved instruction inside the assigned desktop")
        api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not api_key:
            raise ValueError("The VM worker is missing TYPESAFE_API_KEY.")
        driver = CuaDriver(local_endpoint=os.environ.get("CUA_LOCAL_MCP_URL", "http://127.0.0.1:3000/mcp"),
                           local_token=os.environ.get("CUA_LOCAL_MCP_TOKEN"), report=report)
        with report.span("driver_initialization"):
            await driver.start()
        report.append("driver_connected", **driver.session_info)
        await event("driver_connected", driver=driver.server_info, transport="vm_loopback",
                    message="Connected to the desktop's native Cua Driver")
        with httpx2.Client(timeout=30.0, event_hooks={"request": [report.on_request], "response": [report.on_response]}) as http_client:
            with TypeSafeClient(api_key=api_key, base_url="https://api.typesafe.ai", model=PINNED_MODEL,
                                timeout=30.0, retry=RetryPolicy(max_retries=0), http_client=http_client) as client:
                result = await run_pipeline(instruction, driver, ChoiceSelector(client, model=PINNED_MODEL),
                                            report=report, max_iterations=4, screenshots=True,
                                            on_event=pipeline_event)
        reason = result.reason or "The bounded pipeline ended without a task-completion verifier."
        final = {"status": "unverified" if result.status == "iteration_limit" else "blocked", "pipeline": result.to_dict(), "error": reason,
                 "code": "typing_content_unavailable" if result.status == "no_action" else "outcome_not_verified",
                 "completion": {"verified": False, "source": "no_general_postcondition_verifier", "reason": reason}}
        await event("execution_" + final["status"], **final)
    except (asyncio.CancelledError, KeyboardInterrupt) as error:
        final = {"status": "interrupted", "error": "Worker execution was interrupted; desktop input was not replayed.",
                 "code": "execution_interrupted", "error_type": type(error).__name__}
        report.append("run_interrupted", **final)
        raise
    except Exception as error:
        safe = str(error) if isinstance(error, (DriverError, SelectionError, ValueError)) else f"Worker failed: {type(error).__name__}."
        final = {"status": "error", "error": safe, "code": getattr(error, "code", "execution_failed")}
        if isinstance(error, DriverError):
            final["details"] = error.details()
        report.append("run_error", **final)
        try:
            await event("execution_error", **final)
        except Exception as delivery_error:
            report.append("progress_delivery_error", error_type=type(delivery_error).__name__)
    finally:
        if driver is not None:
            try:
                with report.span("driver_cleanup"):
                    await driver.close()
            except Exception as error:
                final["cleanup_error"] = getattr(error, "code", type(error).__name__)
                report.append("driver_cleanup_error", code=final["cleanup_error"])
        report.append("run_finished", result=final)
        final["run_id"] = run_id
        final["report_path"] = str(report.path)
        final["report_directory"] = str(report.requests_path)
        try:
            final["timing"] = await asyncio.to_thread(report.close)
        except Exception as error:
            final["capture_error"] = f"Report rendering failed ({type(error).__name__}); available journal evidence is retained."
    return final
