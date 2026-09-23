"""Run the Jev/CUA observe, select, act, and observe loop."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from itertools import count
from time import sleep as system_sleep
from typing import Any, Protocol

if __package__:
    from .driver import DriverError
    from .reporting import RunReport
    from .selection import Candidate, ChoiceSelector
    from .synthetic import DEFAULT_REGISTRY, SyntheticRegistry
else:
    from driver import DriverError
    from reporting import RunReport
    from selection import Candidate, ChoiceSelector
    from synthetic import DEFAULT_REGISTRY, SyntheticRegistry


class Driver(Protocol):
    def list_windows(self) -> list[dict[str, Any]]: ...

    def get_window_state(self, window: Mapping[str, Any]) -> dict[str, Any]: ...

    def click(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...
    def bring_to_front(self, arguments: Mapping[str, Any]) -> dict[str, Any]: ...


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
    candidates = synthetic.windows()
    for index, window in enumerate(windows):
        if synthetic.replaces(window):
            continue
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


def _activate_window(result, driver, report):
    with (
        report.span("foreground_preparation", tool="bring_to_front")
        if report is not None else nullcontext({})
    ):
        arguments = {name: result.window.get(name) for name in ("pid", "window_id")}
        if report is not None:
            report.append("action_requested", tool="bring_to_front", arguments=arguments)
    try:
        with (
            report.span("foreground_activation", tool="bring_to_front", completion_scope="call_return")
            if report is not None else nullcontext({})
        ):
            foreground_result = driver.bring_to_front(arguments)
    except DriverError as error:
        error.context.update({"stage": "bring_to_front", "selection": result.to_dict()})
        raise
    if report is not None:
        report.append("action_returned", tool="bring_to_front", action_result=foreground_result)


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


def _observed_controls(result, snapshot, report, *, stage="controls"):
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
    with report.span("candidate_preparation", stage=stage) if report is not None else nullcontext({}):
        controls = control_candidates(elements)
    return controls


def _prepare_window(state, windows, driver, selector, result, report, synthetic, iteration=None):
    result.status = "no_windows"
    result.reason = None
    result.control = None
    result.snapshot_id = None
    winner = _choose_window(state, windows, selector, result, report, synthetic, iteration)
    if winner is None:
        return None, {}
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
                    execute_click, settling_context=None, iteration=None):
    with report.span("control_selection", stage="controls") if report is not None else nullcontext({}):
        control_selection = selector.select(
            state, controls, stage="controls",
            **({"settling_context": settling_context} if settling_context is not None else {}),
            **({"report": report} if report is not None else {}),
        )
    _retain_rounds(result, "controls", control_selection.rounds, iteration)
    if control_selection.winner is None:
        result.status = "no_control_selected" if controls else "no_controls"
        result.reason = (
            "Jev found no suitable control for the requested action."
            if controls else "The selected synthetic window defines no controls."
        )
        return None
    # Include selection reporting and argument handoff in dispatch latency;
    # only native_action measures the click invocation through its return.
    with (
        report.span("action_dispatch", tool="click")
        if report is not None and execute_click else nullcontext({})
    ):
        result.control = control_selection.winner.record
        state["last_control"] = {
            "window": state["current_window"],
            "control": _control_context(control_selection.winner.description),
            "status": "(unverified)",
        }
        result.last_control = state["last_control"]
        if report is not None:
            report.append("control_selected", control=result.control, selections=control_selection.rounds)
        result.status = "selected"
        if execute_click:
            with report.span("action_preparation", tool="click") if report is not None else nullcontext({}):
                if result.window.get("synthetic") or result.control.get("synthetic"):
                    result.status = "no_action"
                    result.reason = "Synthetic targets have no live CUA click target; Start Menu search and typing are unsupported."
                    if report is not None:
                        report.append("action_skipped", reason=result.reason)
                    return None
                arguments = {name: result.window.get(name) for name in ("pid", "window_id")}
                token = result.control.get("element_token")
                if isinstance(token, str) and token.strip():
                    arguments["element_token"] = token
                else:
                    arguments.update(element_index=result.control.get("element_index"), snapshot_id=result.snapshot_id)
                if report is not None:
                    report.append("action_requested", tool="click", arguments=arguments)
            try:
                with (
                    report.span("native_action", tool="click", completion_scope="call_return")
                    if report is not None else nullcontext({})
                ):
                    result.action_result = driver.click(arguments)
            except DriverError as error:
                error.context.update(stage="click", selection=result.to_dict())
                raise
            result.status = "action_returned"
            if report is not None:
                report.append("action_returned", tool="click", action_result=result.action_result)
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


def _verify_action(state, controls, selector, result, report, iteration):
    with report.span("action_verification", stage="evidence") if report else nullcontext({}):
        evidence = selector.select(
            state, controls, stage="evidence",
            **({"report": report} if report is not None else {}),
        )
    _retain_rounds(result, "evidence", evidence.rounds, iteration)
    if evidence.winner is None:
        result.status = "no_evidence"
        result.reason = "No suitable observed evidence supports the attempted action."
    elif evidence.verification is not True:
        result.status = "action_unverified"
        result.reason = "Jev did not confirm that the action achieved its intended result."
    else:
        state["last_control"]["status"] = "(verified)"
        result.status = "action_verified"
        result.reason = None
    if report:
        report.append(
            "action_verification", iteration=iteration,
            verified=result.status == "action_verified", last_control=result.last_control,
            evidence=evidence.winner.description if evidence.winner is not None else None,
            reason=result.reason,
        )


def _settle(milliseconds, sleep, report):
    with report.span(
        "settling_wait", requested_ms=milliseconds, kind="after_action",
        start_boundary="after_cua_call_return",
    ) if report else nullcontext({}):
        sleep(milliseconds / 1000)


def _route(result, trace, route, report):
    trace["route"] = route
    if report:
        report.append("loop_route", iteration=trace["iteration"], route=route)


def run_pipeline(
    original_request: str, driver: Driver, selector: ChoiceSelector,
    *, report: RunReport | None = None,
    synthetic: SyntheticRegistry = DEFAULT_REGISTRY,
    sleep: Callable[[float], None] = system_sleep,
) -> PipelineResult:
    """Select and execute one action, then judge fresh evidence of its success.

    This version stops after verification; task-completion and new-window routing
    are deferred. Native delivery errors propagate without retrying an action.
    """
    result = PipelineResult(status="no_windows")
    state = _jev_state(original_request, 0, None, None)
    for iteration in count(1):
        state["iteration"] = iteration
        trace = {"iteration": iteration}
        result.iterations.append(trace)
        with report.span("loop.iteration", iteration=iteration) if report else nullcontext({}):
            windows = _discover_windows(driver, report)
            if iteration == 1:
                controls, snapshot = _prepare_window(
                    state,
                    windows, driver, selector, result, report, synthetic, iteration,
                )
                _route(result, trace, "first_pass", report)
                if controls is None:
                    return result
                settling_ms = _select_and_act(
                    state, controls, driver, selector, result, report, True,
                    settling_context=None if result.window.get("synthetic") else state["current_window"],
                    iteration=iteration,
                )
                if result.status != "action_returned":
                    return result
                state["last_action"] = {
                    "tool": "click",
                    "result": {name: result.action_result[name] for name in ("effect", "route", "delivery")
                               if name in result.action_result},
                    "requested_wait_ms_so_far": settling_ms,
                }
                trace["settling_ms"] = settling_ms
                # CUA has returned. Only now begin the app settling timer.
                _settle(settling_ms, sleep, report)
                continue

            # Discovery refreshes metadata without selecting a different window.
            # Never read an absent old handle or replay its executable target.
            current_window = next((
                window for window in windows
                if all(window.get(name) == result.window.get(name) for name in ("pid", "window_id"))
            ), None)
            if current_window is None:
                state["current_window"] = None
                result.control = None
                result.snapshot_id = None
                result.observation = {"window_missing": True, "coverage": "current window no longer observed"}
                controls = []
            else:
                result.window = current_window
                snapshot = _observe_window(state, result, driver, report)
                controls = _observed_controls(result, snapshot, report, stage="evidence")
                if controls is None and result.status not in {"no_controls", "observation_degraded"}:
                    _route(result, trace, "observation_unavailable", report)
                    return result
            state["observation"] = {
                name: value for name, value in result.observation.items()
                if name not in {"element_count", "total_element_count", "returned_element_count",
                                "filtered_element_count", "observed_control_count"}
            }
            _verify_action(state, controls or [], selector, result, report, iteration)
            _route(result, trace, {
                "action_verified": "verified", "no_evidence": "no_evidence",
                "action_unverified": "unverified",
            }[result.status], report)
            return result
