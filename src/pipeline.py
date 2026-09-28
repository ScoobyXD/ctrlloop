"""Remote Cua observation -> existing Jev rounds -> one native action."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

if __package__:
    from .driver import CuaDriver, DriverError
    from .reporting import RunReport
    from .selection import Candidate, ChoiceSelector, SelectionError
else:
    from driver import CuaDriver, DriverError
    from reporting import RunReport
    from selection import Candidate, ChoiceSelector, SelectionError

DEFAULT_MAX_ITERATIONS = 10


@dataclass
class PipelineResult:
    status: str
    window: dict[str, Any] | None = None
    control: dict[str, Any] | None = None
    snapshot_id: str | None = None
    observation: dict[str, Any] = field(default_factory=dict)
    selections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reason: str | None = None
    action_result: dict[str, Any] | None = None
    last_control: dict[str, Any] | None = None
    iterations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value, limit=240):
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= limit else value[:limit - 1] + "…"


def window_candidates(windows):
    """Keep native identities; send Jev only observed descriptions."""
    candidates = []
    for index, window in enumerate(windows):
        if not isinstance(window, dict) or any(type(window.get(k)) is not int or window[k] <= 0 for k in ("pid", "window_id")):
            raise DriverError("Driver returned a window without a usable identity.", code="invalid_window")
        description = {key: value for key in ("app_name", "title") if (value := _text(window.get(key)))}
        for key in ("is_on_screen", "minimized", "bounds"):
            if window.get(key) is not None:
                description[key] = window[key]
        candidates.append(Candidate(f"window_{index}", description, window))
    return candidates


def control_candidates(elements):
    """Reuse native element records and explicit indexed ancestry."""
    by_index = {element["element_index"]: element for element in elements}
    candidates = []
    for index, element in enumerate(elements):
        description = {"role": element["role"]}
        for key in ("label", "value", "value_description"):
            if key == "value" and (element.get("value") == element.get("label") or element.get("protected")):
                continue
            if value := _text(element.get(key)):
                description[key] = value
        for key in ("enabled", "selected", "actions", "frame"):
            if element.get(key) is not None:
                description[key] = element[key]
        ancestors, seen = [], {element["element_index"]}
        parent_index = element.get("parent_index")
        while parent_index in by_index and parent_index not in seen and len(ancestors) < 2:
            seen.add(parent_index)
            parent = by_index[parent_index]
            context = {"role": parent["role"]}
            if label := _text(parent.get("label"), 120):
                context["label"] = label
            ancestors.append(context)
            parent_index = parent.get("parent_index")
        if ancestors:
            description["indexed_ancestors_nearest_first"] = ancestors
        candidates.append(Candidate(f"control_{index}", description, element))
    return candidates


def _span(report, operation, **details):
    return report.span(operation, **details) if report else nullcontext({})


def _stop(result, trace, status, reason, report):
    result.status, result.reason = status, reason
    trace["route"] = status
    if report:
        report.append("loop_route", iteration=trace["iteration"], route=status)
    return result


def _retain_rounds(result, stage, selection, iteration):
    result.selections.setdefault(stage, []).extend({**r, "iteration": iteration} for r in selection.rounds)


async def run_pipeline(
    original_request: str, driver: CuaDriver, selector: ChoiceSelector,
    *, report: RunReport | None = None, max_iterations: int = DEFAULT_MAX_ITERATIONS,
    text: str | None = None, screenshots: bool = True,
    on_observation: Callable[[dict[str, Any], PipelineResult], Awaitable[bool]] | None = None,
    before_action: Callable[[PipelineResult, str, dict[str, Any]], Awaitable[None]] | None = None,
    after_action: Callable[[PipelineResult, str], Awaitable[bool]] | None = None,
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    candidate_filter: Callable[[str, list[Candidate]], list[Candidate]] | None = None,
) -> PipelineResult:
    """Run on the private worker desktop; never replay an uncertain mutation."""
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer.")
    if not isinstance(original_request, str) or not original_request.strip():
        raise ValueError("The original request must be nonempty.")
    if text is not None and not isinstance(text, str):
        raise ValueError("text must be a string or None.")
    result = PipelineResult(status="no_windows")
    if report is not None:
        driver.report = report
    pending_text = text or None
    state = {"original_request": original_request, "iteration": 0, "current_window": None, "last_control": None}
    async def notify(event, **details):
        if on_event:
            await on_event(event, details)
    for iteration in range(1, max_iterations + 1):
        state["iteration"] = iteration
        state["current_window"] = None
        state.pop("observation", None)
        result.window = result.control = result.snapshot_id = result.action_result = None
        result.observation = {}
        trace = {"iteration": iteration}
        result.iterations.append(trace)
        settling_ms = None
        # The encompassing cycle includes return/screenshot overhead. The report
        # separately closes loop_finished at the last input dispatch timestamp.
        with _span(report, "loop.cycle", iteration=iteration):
            with _span(report, "window_discovery", stage="windows", tool="list_windows"):
                discovered = await driver.call("list_windows", {"on_screen_only": True})
            windows = discovered.get("windows")
            if not isinstance(windows, list):
                raise DriverError("Driver did not return a window list.", code="invalid_window_observation")
            if report:
                report.append("windows_discovered", windows=windows)
            await notify("windows_discovered", iteration=iteration, candidate_count=len(windows))
            with _span(report, "candidate_preparation", stage="windows"):
                candidates = window_candidates(windows)
                if candidate_filter:
                    candidates = candidate_filter("windows", candidates)
            if not candidates:
                return _stop(result, trace, "no_windows", "Driver returned no visible application windows.", report)
            with _span(report, "window_selection", stage="windows"):
                selection = await asyncio.to_thread(selector.select, state, candidates, stage="windows", report=report)
            _retain_rounds(result, "windows", selection, iteration)
            if selection.winner is None:
                return _stop(result, trace, "no_windows", "Jev returned no window candidate.", report)
            result.window = selection.winner.record
            target = {key: result.window[key] for key in ("pid", "window_id")}
            state["current_window"] = selection.winner.description
            if report:
                report.append("window_selected", window=result.window, selections=selection.rounds)
            await notify("window_selected", iteration=iteration, window=result.window, rounds=selection.rounds)
            # Foreground activation stays inside the private VM, before snapshot.
            with _span(report, "foreground_activation", tool="bring_to_front"):
                activation = await driver.call("bring_to_front", target)
            if report:
                report.append("window_activated", result=activation)
            with _span(report, "control_acquisition", stage="controls", tool="get_window_state"):
                observed = await driver.call("get_window_state", {**target, "include_screenshot": False})
            result.snapshot_id = observed.get("snapshot_id")
            result.observation = {key: observed[key] for key in (
                "elements_complete", "degraded", "degraded_reason", "truncated", "truncation_reason",
                "element_count", "total_element_count", "returned_element_count", "filtered_element_count",
            ) if key in observed}
            result.observation.update(source="cua_driver.get_window_state", coverage="bounded native accessibility observation; full UI coverage is not established")
            elements = observed.get("elements")
            if isinstance(elements, list):
                result.observation["observed_control_count"] = len(elements)
            if report:
                report.append("window_observed", window=result.window, snapshot_id=result.snapshot_id, observation=result.observation)
            if on_observation and await on_observation(observed, result):
                return _stop(result, trace, "verified", "The caller verified the requested postcondition from fresh state.", report)
            if not isinstance(elements, list) or not result.snapshot_id:
                return _stop(result, trace, "observation_unavailable", "Driver returned no snapshot-bound control observation.", report)
            if observed.get("degraded") and all(e.get("role") in {"window", "application"} for e in elements):
                return _stop(result, trace, "observation_unavailable", "Driver recovered window metadata but no accessible app controls; inspect the desktop's accessibility support.", report)
            if not elements:
                return _stop(result, trace, "no_controls", "Driver returned no controls; this does not prove the app has none.", report)
            with _span(report, "candidate_preparation", stage="controls"):
                candidates = control_candidates(elements)
                if candidate_filter:
                    candidates = candidate_filter("controls", candidates)
            if report:
                report.append("controls_observed", candidates=[{"key": c.key, "description": c.description, "record": c.record} for c in candidates])
            state["observation"] = {key: result.observation[key] for key in ("coverage", "degraded", "truncated") if key in result.observation}
            with _span(report, "control_selection", stage="controls"):
                selection = await asyncio.to_thread(
                    selector.select, state, candidates, stage="controls", report=report,
                    settling_context=state["current_window"],
                )
            _retain_rounds(result, "controls", selection, iteration)
            if selection.winner is None:
                return _stop(result, trace, "no_controls", "Jev returned no control candidate.", report)
            tool = selection.action
            if tool not in {"click", "type_text"}:
                raise SelectionError("The final control selection did not provide a supported action.")
            winner = selection.winner
            result.control = winner.record
            if report:
                report.append("control_selected", control=winner.record, description=winner.description, action=tool, selections=selection.rounds)
            await notify("control_selected", iteration=iteration, control=winner.description, action=tool, rounds=selection.rounds)
            if tool == "type_text" and pending_text is None:
                if screenshots and report:
                    trace["screenshots"] = {"before": await report.capture_screenshot(driver, phase="before", iteration=iteration)}
                return _stop(result, trace, "no_action", "No prepared text is available for this typing operation (missing, empty, or already used).", report)
            arguments = {**target, "delivery_mode": "foreground"}
            if winner.record.get("element_token"):
                arguments["element_token"] = winner.record["element_token"]
            else:
                arguments.update(element_index=winner.record["element_index"], snapshot_id=result.snapshot_id)
            if tool == "type_text":
                arguments["text"] = pending_text
            if before_action:
                await before_action(result, tool, arguments)
            result.last_control = {"window": state["current_window"], "control": winner.description, "action": tool}
            state["last_control"] = result.last_control
            if screenshots and report:
                trace["screenshots"] = {"before": await report.capture_screenshot(driver, phase="before", iteration=iteration)}
            try:
                with _span(report, "action_dispatch", tool=tool):
                    if report:
                        report.append("action_requested", tool=tool, arguments=arguments, snapshot_id=result.snapshot_id, boundary="before_cua_driver_call")
                    await notify("action_requested", iteration=iteration, tool=tool, control=winner.description)
                    if tool == "type_text":
                        pending_text = None  # Consume before sending, including uncertain delivery.
                    with _span(report, "native_action", tool=tool, completion_scope="call_return"):
                        result.action_result = await driver.call(tool, arguments)
            except DriverError as error:
                if report:
                    report.append("loop_action_finished", iteration=iteration, boundary="cua_driver_call_error")
                    if screenshots:
                        trace["screenshots"]["after"] = await report.capture_screenshot(driver, phase="after", iteration=iteration)
                error.context.update(stage=tool, selection=result.to_dict())
                raise
            state["last_action"] = {"tool": tool, "result": result.action_result}
            trace["last_action"] = state["last_action"]
            settling_ms = selection.settling_ms
            trace["settling_ms"] = settling_ms
            verified = (
                await after_action(result, tool)
                if after_action and result.action_result.get("effect") in {"confirmed", "unverifiable"}
                else False
            )
            if report:
                report.append("action_returned", tool=tool, action_result=result.action_result)
                report.append("loop_action_finished", iteration=iteration, boundary="cua_driver_call_return")
                if screenshots:
                    trace["screenshots"]["after"] = await report.capture_screenshot(driver, phase="after", iteration=iteration)
            await notify("action_returned", iteration=iteration, tool=tool, result=result.action_result)
            if verified:
                return _stop(result, trace, "verified", "The caller verified the requested postcondition from fresh state.", report)
            effect = result.action_result.get("effect")
            # Unverifiable delivery is not task success. Take fresh state on the
            # next loop; never automatically resend the same native command.
            if effect not in {"confirmed", "unverifiable"}:
                return _stop(result, trace, "action_unconfirmed", f"Driver reported effect={effect or 'unknown'}; the action was not replayed.", report)
            if iteration == max_iterations:
                return _stop(result, trace, "iteration_limit", "The configured iteration limit was reached; task completion was not assessed.", report)
            trace["route"] = "continue"
            if report:
                report.append("loop_route", iteration=iteration, route="continue")
        if settling_ms is not None:
            with _span(report, "settling_wait", iteration=iteration, requested_ms=settling_ms, kind="after_action", start_boundary="after_cua_driver_call_return"):
                await asyncio.sleep(settling_ms / 1000)
    return result
