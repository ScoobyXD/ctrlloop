"""Browser Use observation -> existing Jev tournament -> one browser action."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from typing import Any

if __package__:
    from .driver import BrowserDriver, DriverError
    from .reporting import RunReport
    from .selection import Candidate, ChoiceSelector, SelectionError
else:
    from driver import BrowserDriver, DriverError
    from reporting import RunReport
    from selection import Candidate, ChoiceSelector, SelectionError

from browser_use.dom.views import EnhancedDOMTreeNode, NodeType


DEFAULT_MAX_ITERATIONS = 10
_NONCONTENT_TAGS = frozenset({"script", "style", "head", "noscript", "template", "meta", "link", "title"})


@dataclass
class PipelineResult:
    status: str
    page: dict[str, Any] | None = None
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


def _text(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _descendant_text(node: EnhancedDOMTreeNode, limit: int = 240) -> str | None:
    """Read bounded DOM text without including scripts/styles or giant subtrees."""
    pieces: list[str] = []
    length = 0
    pending = [node]
    visited = 0
    while pending and visited < 128 and length < limit:
        current = pending.pop()
        visited += 1
        if current.tag_name in _NONCONTENT_TAGS:
            continue
        if current.node_type == NodeType.TEXT_NODE:
            if current.is_visible is False:
                continue
            value = _text(current.node_value, limit - length)
            if value:
                pieces.append(value)
                length += len(value) + 1
            continue
        pending.extend(reversed(current.children_and_shadow_roots[:128]))
    return _text(" ".join(pieces), limit)


def _description(node: EnhancedDOMTreeNode, *, ancestors: bool = True) -> dict[str, Any]:
    attrs = node.attributes or {}
    ax = node.ax_node
    description: dict[str, Any] = {"tag": node.tag_name}
    ax_role = _text(ax.role if ax else None)
    role = (ax_role if ax_role not in {None, "none", "generic"} else None) or _text(attrs.get("role")) or node.tag_name
    description["role"] = role
    label = (
        _text(ax.name if ax else None) or _text(attrs.get("aria-label"))
        or _text(attrs.get("placeholder")) or _text(attrs.get("title"))
        or _text(attrs.get("alt")) or _descendant_text(node)
    )
    if label:
        description["label"] = label
    for name in ("id", "name", "type", "placeholder", "href", "contenteditable", "aria-expanded", "aria-checked"):
        if value := _text(attrs.get(name)):
            if name != "placeholder" or value != label:
                description[name] = value
    # Follow Browser Use's protection for password fields rather than emitting a value.
    if attrs.get("type", "").lower() != "password":
        if value := _text(attrs.get("value")):
            description["value"] = value
    if ax:
        for prop in ax.properties or []:
            if prop.name in {"disabled", "readonly", "checked", "selected", "expanded", "editable", "required"}:
                if prop.value is not None:
                    description[prop.name] = prop.value
    if "disabled" in attrs or attrs.get("aria-disabled") == "true":
        description["disabled"] = True
    if node.is_actually_scrollable:
        description["scrollable"] = True
    if ancestors:
        contexts = []
        parent = node.parent_node
        seen = {id(node)}
        # Preserve two actual meaningful ancestors; do not invent indexed ancestry.
        while parent is not None and id(parent) not in seen and len(seen) <= 32 and len(contexts) < 2:
            seen.add(id(parent))
            parent_attrs = parent.attributes or {}
            parent_ax = parent.ax_node
            parent_role = _text(parent_ax.role if parent_ax else None, 80) or _text(parent_attrs.get("role"), 80)
            parent_label = _text(parent_ax.name if parent_ax else None, 120) or _text(parent_attrs.get("aria-label"), 120)
            if parent_role not in {None, "none", "generic"} or parent_label:
                context = {"tag": parent.tag_name}
                if parent_role:
                    context["role"] = parent_role
                if parent_label:
                    context["label"] = parent_label
                contexts.append(context)
            parent = parent.parent_node
        if contexts:
            description["ancestors_nearest_first"] = contexts
    return description


def control_candidates(selector_map: dict[int, EnhancedDOMTreeNode]) -> list[Candidate]:
    """Offer every native filtered-map entry, in order, using the existing model."""
    return [
        Candidate(
            f"control_{index}", _description(node),
            {"index": index, "backend_node_id": node.backend_node_id,
             "target_id": node.target_id, "frame_id": node.frame_id, "session_id": node.session_id},
        )
        for index, node in selector_map.items()
    ]


def _span(report: RunReport | None, operation: str, **details):
    return report.span(operation, **details) if report is not None else nullcontext({})


def _route(trace: dict[str, Any], route: str, report: RunReport | None) -> None:
    trace["route"] = route
    if report:
        report.append("loop_route", iteration=trace["iteration"], route=route)


async def run_pipeline(
    original_request: str, driver: BrowserDriver, selector: ChoiceSelector,
    *, report: RunReport | None = None, max_iterations: int = DEFAULT_MAX_ITERATIONS,
    text: str | None = None,
) -> PipelineResult:
    """Execute bounded loops; external text is used once, completion is not inferred."""
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer.")
    if not isinstance(original_request, str) or not original_request.strip():
        raise ValueError("The original request must be nonempty.")
    if text is not None and not isinstance(text, str):
        raise ValueError("text must be a string or None.")
    result = PipelineResult(status="no_controls")
    pending_text = text or None
    state: dict[str, Any] = {"original_request": original_request, "iteration": 0, "current_page": None, "last_control": None}
    for iteration in range(1, max_iterations + 1):
        state["iteration"] = iteration
        result.control = None
        result.snapshot_id = None
        result.action_result = None
        result.observation = {}
        trace: dict[str, Any] = {"iteration": iteration}
        result.iterations.append(trace)
        settling_ms = None
        with _span(report, "loop.iteration", iteration=iteration):
            with _span(report, "control_acquisition", stage="controls", tool="get_browser_state_summary"):
                try:
                    observed = await driver.get_state()
                except DriverError as error:
                    error.context.update(stage="get_browser_state_summary", selection=result.to_dict())
                    raise
            with _span(report, "observation_processing", stage="controls"):
                page = {"url": observed.url, "title": observed.title, "target_id": driver.browser.agent_focus_target_id}
                result.page = page
                result.snapshot_id = driver.snapshot_id
                state["current_page"] = {"url": observed.url, "title": observed.title}
                result.observation = {
                    "source": "browser_use.selector_map",
                    "observed_control_count": len(observed.dom_state.selector_map),
                    "coverage": "Browser Use filtered candidates; full page coverage and actionability are not established",
                    "state_error": observed.state_error,
                    "browser_errors": observed.browser_errors,
                    "page_info": observed.page_info.model_dump(mode="json") if observed.page_info else None,
                    "tabs": [tab.model_dump(mode="json") for tab in observed.tabs],
                }
                state["observation"] = {"coverage": result.observation["coverage"]}
            if report:
                report.append("page_observed", page=page, snapshot_id=result.snapshot_id, observation=result.observation)
            if observed.state_error or (not observed.dom_state.selector_map and observed.browser_errors):
                result.status = "observation_unavailable"
                result.reason = observed.state_error or "Browser Use could not capture page state; see browser_errors in the observation."
                _route(trace, result.status, report)
                return result
            with _span(report, "candidate_preparation", stage="controls"):
                candidates = control_candidates(observed.dom_state.selector_map)
            if not candidates:
                result.status = "no_controls"
                result.reason = "Browser Use returned no indexed candidates; this does not establish that the page has no controls."
                _route(trace, result.status, report)
                return result
            if report:
                report.append("controls_observed", candidates=[{"key": c.key, "description": c.description, "record": c.record} for c in candidates])
            with _span(report, "control_selection", stage="controls"):
                selection = await asyncio.to_thread(
                    selector.select, state, candidates, stage="controls", report=report,
                    settling_context=state["current_page"],
                )
            result.selections.setdefault("controls", []).extend({**round_, "iteration": iteration} for round_ in selection.rounds)
            if selection.winner is None:
                result.status = "no_controls"
                result.reason = "Jev returned no candidate."
                _route(trace, result.status, report)
                return result
            if selection.action not in {"click", "type_text"}:
                raise SelectionError("The final control selection did not provide a supported action.")
            winner = selection.winner
            tool = selection.action
            result.control = winner.record
            if report:
                report.append("control_selected", control=winner.record, description=winner.description, action=tool, selections=selection.rounds)
            if tool == "type_text" and pending_text is None:
                result.status = "no_action"
                result.reason = "No prepared text is available for this typing operation (missing, empty, or already used)."
                _route(trace, result.status, report)
                if report:
                    report.append("action_skipped", reason=result.reason)
                return result
            arguments: dict[str, Any] = {"index": winner.record["index"]}
            if tool == "type_text":
                arguments["text"] = pending_text
            result.last_control = {"page": state["current_page"], "control": winner.description, "action": tool}
            state["last_control"] = result.last_control
            with _span(report, "action_dispatch", tool=tool):
                if report:
                    report.append("action_requested", tool=tool, arguments=arguments, snapshot_id=result.snapshot_id,
                                  boundary="before_browser_use_action_call")
                with _span(report, "browser_action", tool=tool, completion_scope="call_return"):
                    try:
                        native_result = await driver.act(tool, **arguments)
                    except DriverError as error:
                        error.context.update(stage=tool, selection=result.to_dict())
                        raise
            result.action_result = native_result.model_dump(mode="json", exclude_none=True)
            state["last_action"] = {"tool": tool, "result": result.action_result}
            if tool == "type_text":
                state["last_action"]["text"] = pending_text
                pending_text = None
            trace["last_action"] = state["last_action"]
            settling_ms = selection.settling_ms
            trace["settling_ms"] = settling_ms
            if report:
                report.append("action_returned", tool=tool, action_result=result.action_result)
                report.append("loop_action_finished", iteration=iteration, boundary="browser_use_action_call_return")
            if native_result.error:
                result.status = "action_error"
                result.reason = native_result.error
                _route(trace, result.status, report)
                return result
            result.status = "action_returned"
            if iteration == max_iterations:
                result.status = "iteration_limit"
                result.reason = "The configured iteration limit was reached; task completion was not assessed."
                _route(trace, result.status, report)
                return result
            _route(trace, "continue", report)
        # Settling is separate from the observation/selection/action loop interval.
        if settling_ms is not None:
            with _span(report, "settling_wait", iteration=iteration, requested_ms=settling_ms,
                       kind="after_action", start_boundary="after_browser_action_return"):
                await asyncio.sleep(settling_ms / 1000)
    return result
