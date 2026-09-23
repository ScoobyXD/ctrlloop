"""Run the Jev/CUA observe, select, act, and observe loop."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
import re
from time import sleep as system_sleep
from typing import Any, Protocol

if __package__:
    from .driver import DriverError
    from .reporting import RunReport
    from .selection import Candidate, ChoiceSelector, SelectionError
    from .synthetic import DEFAULT_REGISTRY, WINDOWS_SEARCH_KEY, SyntheticRegistry, is_visible_windows_search
else:
    from driver import DriverError
    from reporting import RunReport
    from selection import Candidate, ChoiceSelector, SelectionError
    from synthetic import DEFAULT_REGISTRY, WINDOWS_SEARCH_KEY, SyntheticRegistry, is_visible_windows_search


DEFAULT_MAX_ITERATIONS = 10


class Driver(Protocol):
    def list_windows(self) -> list[dict[str, Any]]: ...

    def get_window_state(self, window: Mapping[str, Any]) -> dict[str, Any]: ...

    def click(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...
    def bring_to_front(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...
    def hotkey(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...
    def type_text(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...


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


def _text(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # Only descriptions are shortened. Original request and source records stay exact.
    return value if len(value) <= limit else value[: limit - 1] + "…"


def window_candidates(
    windows: Sequence[dict[str, Any]], *, synthetic: SyntheticRegistry = DEFAULT_REGISTRY,
) -> list[Candidate]:
    # Search is transient: offer the opener only when fresh discovery has no
    # visible destination. Keep the real window and its native identifiers.
    observed = [
        (index, window) for index, window in enumerate(windows)
        if not synthetic.replaces(window)
    ]
    search_visible = any(is_visible_windows_search(window) for _, window in observed)
    candidates = [
        candidate for candidate in synthetic.windows()
        if not _is_windows_search(candidate.record) or not search_visible
    ]
    for index, window in observed:
        description = {}
        for name in ("app_name", "title"):
            if value := _text(window.get(name)):
                description[name] = value
        for name in ("is_on_screen", "minimized", "bounds"):
            if window.get(name) is not None:
                description[name] = window[name]
        candidates.append(Candidate(f"window_{index}", description, window))
    return candidates


def control_candidates(elements: Sequence[dict[str, Any]]) -> list[Candidate]:
    by_index = {element["element_index"]: element for element in elements}
    candidates = []
    for index, element in enumerate(elements):
        description: dict[str, Any] = {"role": element["role"]}
        for name in ("label", "value", "value_description"):
            if name == "value" and element.get("value") == element.get("label"):
                continue
            if value := _text(element.get(name)):
                description[name] = value
        for name in ("enabled", "selected", "actions", "frame"):
            if element.get(name) is not None:
                description[name] = element[name]

        # Only explicit links to observed indexed ancestors; depth is not ancestry.
        ancestors = []
        seen = {element["element_index"]}
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


def _retain_rounds(result, stage, rounds, iteration):
    if iteration is None:
        result.selections[stage] = rounds
    else:
        result.selections.setdefault(stage, []).extend(
            {**round_, "iteration": iteration} for round_ in rounds
        )


def _request_words(original_request):
    """Keep source positions locally; copy exact spans after Jev chooses words."""
    return [
        Candidate(f"word_{index}", {"text": match.group()},
                  {"start": match.start(), "end": match.end()})
        for index, match in enumerate(re.finditer(r"\S+", original_request))
    ]


def _resolve_text(state, words, target, selector, result, report, iteration):
    with report.span("text_selection", stage="text") if report else nullcontext({}):
        selection = selector.select_text(state, words, target=target, report=report)
    _retain_rounds(result, "text", selection.rounds, iteration)
    text = selection.winner.record["text"] if selection.winner else None
    if report:
        report.append("text_selected", target=target, text=text, selections=selection.rounds)
    if text is None:
        result.status = "no_action"
        result.reason = selection.rounds[-1].get("reason", "No usable request text was selected.")
        if report:
            report.append("action_skipped", reason=result.reason)
    return text


def _remember_action(state, result, tool, text=None):
    state["last_action"] = {
        "tool": tool,
        **({"text": text} if tool == "type_text" else {}),
        "result": {name: result.action_result[name] for name in ("effect", "route", "delivery")
                   if name in result.action_result},
    }


def _discover_windows(driver, report):
    with (
        report.span("window_discovery", stage="windows", tool="list_windows")
        if report is not None else nullcontext({})
    ):
        windows = driver.list_windows()
    if report is not None:
        report.append("windows_discovered", windows=windows)
    return windows


def _choose_window(state, windows, selector, result, report, synthetic, iteration=None):
    with report.span("candidate_preparation", stage="windows") if report is not None else nullcontext({}):
        candidates = window_candidates(windows, synthetic=synthetic)
    with report.span("window_selection", stage="windows") if report is not None else nullcontext({}):
        window_selection = selector.select(
            state, candidates, stage="windows",
            **({"report": report} if report is not None else {}),
        )
    _retain_rounds(result, "windows", window_selection.rounds, iteration)
    if window_selection.winner is None:
        result.reason = "Window discovery returned no candidates."
        return None
    result.window = window_selection.winner.record
    state["current_window"] = _app_context(result.window, {})
    if report is not None:
        report.append("window_selected", window=result.window, selections=window_selection.rounds)
    return window_selection.winner


def _is_windows_search(window):
    return bool(window and window.get("synthetic") and window.get("target") == WINDOWS_SEARCH_KEY)


def _invoke_action(result, driver, report, tool, arguments, operation="native_action"):
    """Dispatch once and retain native action facts through the shared report."""
    if report is not None:
        report.append("action_requested", tool=tool, arguments=arguments)
    try:
        with (
            report.span(operation, tool=tool, completion_scope="call_return")
            if report is not None else nullcontext({})
        ):
            action_result = getattr(driver, tool)(arguments)
    except DriverError as error:
        error.context.update(stage=tool, selection=result.to_dict())
        raise
    if report is not None:
        report.append("action_returned", tool=tool, action_result=action_result)
    return action_result


def _activate_window(result, driver, report):
    tool = "hotkey" if _is_windows_search(result.window) else "bring_to_front"
    with (
        report.span("foreground_preparation", tool=tool)
        if report is not None else nullcontext({})
    ):
        arguments = (
            {"scope": "desktop", "keys": ["win", "s"]}
            if tool == "hotkey" else
            {name: result.window.get(name) for name in ("pid", "window_id")}
        )
    return _invoke_action(result, driver, report, tool, arguments, "foreground_activation")


def _search_windows(state, result, driver, report, text, selector, words, iteration):
    """Resolve text before activation, then send Win+S and text consecutively."""
    previous_control = state.get("last_control")
    if text is None:
        text = _resolve_text(
            state, words,
            {"destination": "Windows Search", "purpose": "Search for the requested application, file, or setting."},
            selector, result, report, iteration,
        )
        if text is None:
            return
    last_action = state.get("last_action", {})
    if previous_control is None and last_action.get("tool") == "type_text" and last_action.get("text") == text:
        result.status = "observation_unavailable"
        result.reason = (
            "Windows Search was not observed after the previous identical query. "
            "The launch and text were not replayed; inspect the current state before trying again."
        )
        if report is not None:
            report.append("action_skipped", reason=result.reason)
        return
    result.status = "selected"
    state["last_control"] = None
    result.last_control = None
    # Prepare the text payload before activation so the next native call is typing.
    arguments = {"scope": "desktop", "text": text}
    with report.span("action_dispatch", tool="type_text") if report is not None else nullcontext({}):
        _activate_window(result, driver, report)
        result.action_result = _invoke_action(result, driver, report, "type_text", arguments)
    _remember_action(state, result, "type_text", text)
    result.status = "action_returned"


def _observe_window(state, result, driver, report):
    # Clear snapshot-bound targets before every read, including failed reads.
    result.control = None
    result.snapshot_id = None
    try:
        with (
            report.span("control_acquisition", stage="controls", tool="get_window_state")
            if report is not None else nullcontext({})
        ):
            snapshot = driver.get_window_state(result.window)
    except DriverError as error:
        error.context.update({
            "stage": "get_window_state",
            "window": {
                name: result.window[name]
                for name in (
                    "pid", "window_id", "app_name", "title", "bounds",
                    "is_on_screen", "minimized",
                )
                if name in result.window
            },
            "selections": result.selections,
        })
        raise
    with report.span("observation_processing", stage="controls") if report is not None else nullcontext({}):
        context = _app_context(result.window, snapshot)
        if context != state["current_window"]:
            # Replace metadata rather than editing the source window retained by
            # last_control. A changed title does not imply a new window selection.
            state["current_window"] = context
        result.snapshot_id = snapshot.get("snapshot_id")
        result.observation = {
            name: snapshot[name]
            for name in (
                "elements_complete", "degraded", "degraded_reason", "truncated",
                "truncation_reason", "element_count", "total_element_count",
                "returned_element_count", "filtered_element_count",
            )
            if name in snapshot
        }
        result.observation["coverage"] = "bounded observation; full UI coverage is not established"
        elements = snapshot.get("elements")
        if elements is not None:
            result.observation["observed_control_count"] = len(elements)
    if report is not None:
        report.append(
            "window_observed", window=result.window, snapshot_id=result.snapshot_id,
            observation=result.observation,
        )
    return snapshot


def _observed_controls(result, snapshot, report):
    elements = snapshot.get("elements")
    if elements is None:
        result.status = "observation_unavailable"
        result.reason = "The window read did not provide a structured control observation."
        return None
    if not elements:
        result.status = "observation_degraded" if snapshot.get("degraded") else "no_controls"
        result.reason = "The window read returned no indexed controls; this does not prove none exist."
        return None
    if not result.snapshot_id:
        result.status = "observation_unavailable"
        result.reason = "The control observation has no snapshot identity."
        return None
    with report.span("candidate_preparation", stage="controls") if report is not None else nullcontext({}):
        controls = control_candidates(elements)
    return controls


def _prepare_window(state, windows, driver, selector, result, report, synthetic, iteration=None):
    result.status = "no_windows"
    result.reason = None
    result.window = None
    result.control = None
    result.snapshot_id = None
    result.observation = {}
    result.action_result = None
    winner = _choose_window(state, windows, selector, result, report, synthetic, iteration)
    if winner is None:
        return None, {}
    if _is_windows_search(result.window):
        result.observation = {
            "source": "synthetic",
            "coverage": "application-defined destination; no live control snapshot was taken",
        }
        return [], {}
    with report.span("synthetic_resolution", stage="controls") if report else nullcontext({}):
        controls = synthetic.controls_for(winner.key)
    if controls is not None:
        result.observation = {
            "source": "synthetic",
            "coverage": "application-defined controls; no live control snapshot was taken",
        }
        return controls, {}
    _activate_window(result, driver, report)
    snapshot = _observe_window(state, result, driver, report)
    return _observed_controls(result, snapshot, report), snapshot


def _select_and_act(state, controls, driver, selector, result, report,
                    words, settling_context=None, iteration=None):
    with report.span("control_selection", stage="controls") if report is not None else nullcontext({}):
        control_selection = selector.select(
            state, controls, stage="controls",
            **({"settling_context": settling_context} if settling_context is not None else {}),
            **({"report": report} if report is not None else {}),
        )
    _retain_rounds(result, "controls", control_selection.rounds, iteration)
    if control_selection.winner is None:
        result.status = "no_controls"
        result.reason = "Control selection returned no candidate."
        return None
    tool = control_selection.action
    if tool not in {"click", "type_text"}:
        raise SelectionError("The final control selection did not provide a supported action.")
    result.control = control_selection.winner.record
    result.action_result = None
    pending_control = {
        "window": state["current_window"],
        "control": _control_context(control_selection.winner.description),
        "action": tool,
    }
    if report is not None:
        report.append("control_selected", control=result.control, action=tool, selections=control_selection.rounds)
    result.status = "selected"
    if result.window.get("synthetic") or result.control.get("synthetic"):
        result.status = "no_action"
        result.reason = "Synthetic controls have no live CUA action target."
        if report is not None:
            report.append("action_skipped", reason=result.reason)
        return None
    text = None
    if tool == "type_text":
        if result.control.get("enabled") is False:
            result.status = "no_action"
            result.reason = "The selected text-entry control is disabled."
            if report:
                report.append("action_skipped", reason=result.reason)
            return None
        text = _resolve_text(
            state, words, control_selection.winner.description, selector, result, report, iteration,
        )
        if text is None:
            return None
    # No new snapshot between choosing a target and dispatch: its token must
    # still refer to the observation from which the decision was made.
    state["last_control"] = pending_control
    result.last_control = pending_control
    with report.span("action_dispatch", tool=tool) if report else nullcontext({}):
        with report.span("action_preparation", tool=tool) if report else nullcontext({}):
            arguments = {name: result.window.get(name) for name in ("pid", "window_id")}
            token = result.control.get("element_token")
            if isinstance(token, str) and token.strip():
                arguments["element_token"] = token
            else:
                arguments.update(element_index=result.control.get("element_index"), snapshot_id=result.snapshot_id)
            if tool == "type_text":
                arguments["text"] = text
        result.action_result = _invoke_action(result, driver, report, tool, arguments)
    _remember_action(state, result, tool, text)
    result.status = "action_returned"
    if tool == "type_text" and result.action_result.get("effect") != "confirmed":
        # A returned input call can have changed the field without synchronous
        # evidence. Stop before another append rather than replay uncertain text.
        result.status = "observation_unavailable"
        result.reason = "CUA did not confirm the targeted text entry; inspect the field before further input."
    return control_selection.settling_ms


def _app_context(window, snapshot):
    """Use observed app/title identity, preferring the fresh window read."""
    context = {name: window[name] for name in ("app_name", "title") if name in window}
    for source, target in (("app_name", "app_name"), ("window_title", "title")):
        if snapshot.get(source):
            context[target] = snapshot[source]
    return context


def _jev_state(original_request, iteration, current_window, last_control):
    """Initialize the single mutable context owned by a pipeline run."""
    return {
        "original_request": original_request,
        "iteration": iteration,
        "current_window": current_window,
        "last_control": last_control,
    }


def _control_context(description):
    # Remember the selected control without retaining native targets or geometry.
    return {name: value for name, value in description.items() if name not in {"frame", "actions"}}


def _settle(milliseconds, sleep, report):
    with report.span(
        "settling_wait", requested_ms=milliseconds, kind="after_action",
        start_boundary="after_cua_call_return",
    ) if report else nullcontext({}):
        sleep(milliseconds / 1000)


def _route(trace, route, report):
    trace["route"] = route
    if report:
        report.append("loop_route", iteration=trace["iteration"], route=route)


def run_pipeline(
    original_request: str, driver: Driver, selector: ChoiceSelector,
    *, report: RunReport | None = None,
    synthetic: SyntheticRegistry = DEFAULT_REGISTRY,
    sleep: Callable[[float], None] = system_sleep,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    text: str | None = None,
) -> PipelineResult:
    """Repeat fresh selection and native actions within an explicit limit.

    Each call retains the original goal and the most recent decision/action.
    Windows Search resolves text before sending Win+S followed immediately by text.
    The iteration limit bounds execution; it does not establish task completion.
    Native delivery errors propagate without replaying the attempted action.
    """
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer.")
    if not isinstance(original_request, str) or not original_request.strip():
        raise ValueError("The original request must be nonempty.")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ValueError("text must be a string containing non-whitespace text.")
    result = PipelineResult(status="no_windows")
    state = _jev_state(original_request, 0, None, None)
    with report.span("request_word_preparation") if report else nullcontext({}):
        words = _request_words(original_request)
    if report:
        report.append("request_words_prepared", words=[
            {"key": word.key, **word.description, **word.record} for word in words
        ])
    for iteration in range(1, max_iterations + 1):
        state["iteration"] = iteration
        # Observation coverage belongs to the next fresh control snapshot.
        state.pop("observation", None)
        trace = {"iteration": iteration}
        result.iterations.append(trace)
        with report.span("loop.iteration", iteration=iteration) if report else nullcontext({}):
            windows = _discover_windows(driver, report)
            if result.window is not None:
                current_window = next((
                    window for window in windows
                    if all(window.get(name) == result.window.get(name) for name in ("pid", "window_id"))
                ), None)
                state["current_window"] = _app_context(current_window, {}) if current_window else None
            # Select again from fresh windows; the next step may be in another app.
            controls, _ = _prepare_window(
                state, windows, driver, selector, result, report, synthetic, iteration,
            )
            if controls is None:
                _route(trace, result.status, report)
                return result
            state["observation"] = {
                name: value for name, value in result.observation.items()
                if name not in {"element_count", "total_element_count", "returned_element_count",
                                "filtered_element_count", "observed_control_count"}
            }
            if _is_windows_search(result.window):
                _search_windows(state, result, driver, report, text, selector, words, iteration)
                settling_ms = None
            else:
                settling_ms = _select_and_act(
                    state, controls, driver, selector, result, report, words,
                    settling_context=None if result.window.get("synthetic") else state["current_window"],
                    iteration=iteration,
                )
            if result.action_result is not None:
                trace["last_action"] = state["last_action"]
            trace["settling_ms"] = settling_ms
            if result.status != "action_returned":
                _route(trace, result.status, report)
                return result
            if iteration == max_iterations:
                result.status = "iteration_limit"
                result.reason = "The configured iteration limit was reached; task completion was not assessed."
                _route(trace, result.status, report)
                return result
            # CUA has returned. Wait only when another observation will follow.
            if settling_ms is not None:
                _settle(settling_ms, sleep, report)
            _route(trace, "continue", report)
