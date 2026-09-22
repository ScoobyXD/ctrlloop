"""Run the Jev/CUA observe, select, act, and observe loop."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from itertools import count
from time import sleep as system_sleep
from typing import Any, Protocol

from typesafe_sdk import Choice

if __package__:
    from .driver import DriverError
    from .reporting import RunReport
    from .selection import Candidate, ChoiceSelector, SETTLING_MS
    from .synthetic import DEFAULT_REGISTRY, SyntheticRegistry
else:
    from driver import DriverError
    from reporting import RunReport
    from selection import Candidate, ChoiceSelector, SETTLING_MS
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


def _choose_window(original_request, windows, selector, result, report, synthetic, iteration=None):
    with report.span("candidate_preparation", stage="windows") if report is not None else nullcontext({}):
        candidates = window_candidates(windows, synthetic=synthetic)
    with report.span("window_selection", stage="windows") if report is not None else nullcontext({}):
        window_selection = selector.select(
            original_request, candidates, stage="windows",
            **({"report": report} if report is not None else {}),
        )
    _retain_rounds(result, "windows", window_selection.rounds, iteration)
    if window_selection.winner is None:
        result.reason = "Window discovery returned no candidates."
        return None
    result.window = window_selection.winner.record
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


def _observe_window(result, driver, report):
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


def _prepare_window(original_request, windows, driver, selector, result, report, synthetic, iteration=None):
    result.status = "no_windows"
    result.reason = None
    result.control = None
    result.snapshot_id = None
    winner = _choose_window(original_request, windows, selector, result, report, synthetic, iteration)
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
    snapshot = _observe_window(result, driver, report)
    return _observed_controls(result, snapshot, report), snapshot


def _select_and_act(original_request, controls, driver, selector, result, report,
                    execute_click, settling_context=None, iteration=None):
    with report.span("control_selection", stage="controls") if report is not None else nullcontext({}):
        control_selection = selector.select(
            original_request, controls, stage="controls",
            **({"settling_context": settling_context} if settling_context is not None else {}),
            **({"report": report} if report is not None else {}),
        )
    _retain_rounds(result, "controls", control_selection.rounds, iteration)
    if control_selection.winner is None:
        result.status = "no_controls"
        result.reason = "The selected synthetic window defines no controls."
        return None
    # Include selection reporting and argument handoff in dispatch latency;
    # only native_action measures the click invocation through its return.
    with (
        report.span("action_dispatch", tool="click")
        if report is not None and execute_click else nullcontext({})
    ):
        result.control = control_selection.winner.record
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


def _comparison_observation(windows, result, snapshot):
    # Reuse candidate descriptions; exclude native target tokens and layout noise.
    controls = control_candidates(snapshot.get("elements") or [])
    return {
        "available_windows": [
            {name: window[name] for name in ("app_name", "title", "is_on_screen", "minimized")
             if name in window}
            for window in windows
        ],
        "current_window": _app_context(result.window, snapshot),
        "observation": result.observation,
        "controls": [
            {name: value for name, value in candidate.description.items() if name != "frame"}
            for candidate in controls
        ],
    }


def _loop_questions():
    return {
        "changed": Choice(
            instructions=(
                "Did anything meaningfully change between the previous and current observations "
                "for the user's original request? Compare the provided observations, considering "
                "the last action; a CUA action return alone is not evidence of UI change."
            ), criteria={"yes": None, "no": None},
        ),
        "done": Choice(
            instructions=(
                "Are we done with the user's original requested task? Judge from the current "
                "observed state. An action return alone or missing/partial observation is not "
                "proof of completion."
            ), criteria={"yes": None, "no": None},
        ),
        "another_window": Choice(
            instructions=(
                "If the user's task is unfinished, do we need another window to continue, "
                "instead of the currently observed window? Use the observed available windows "
                "and current controls. Answer independently of the other questions."
            ), criteria={"yes": None, "no": None},
        ),
    }


def _wait_questions(context):
    return {
        "recovery": Choice(
            instructions=(
                "The task is unfinished and nothing meaningfully changed. Should we wait "
                "longer for the last action's resulting app state, or report an error? "
                "Waiting only observes again; it never repeats the action."
            ), criteria={"wait": "Allow more app settling time", "error": "Stop: no useful progress"},
        ),
        "wait_timing": Choice(
            instructions={
                "question": (
                    "If we wait longer without repeating the last action, how much additional "
                    "time should this observed browser/app need to become ready for the next step? "
                    "Estimate app settling time after CUA has already returned, not CUA duration. "
                    "The >500ms option schedules a first observation after 500ms, not guaranteed readiness."
                ),
                "observed_app_browser": context,
            }, criteria={label: None for label in SETTLING_MS},
        ),
    }


def _judge_loop(selector, state, questions, report, iteration, kind):
    with report.span("loop.judgment", kind=kind) if report else nullcontext({}):
        answers, api_call = selector.judge(state, questions, report=report)
    if report:
        report.append("loop_judgment", iteration=iteration, kind=kind, api_call=api_call)
    return answers, api_call


def _settle(milliseconds, sleep, report, *, kind):
    with report.span(
        "settling_wait", requested_ms=milliseconds, kind=kind,
        start_boundary="after_cua_call_return" if kind == "after_action" else "after_wait_decision",
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
    """Observe and execute until completion, a stop condition, or interruption.

    There is no iteration cap. Native delivery errors propagate without retrying
    an action, and every new action uses a freshly observed control target.
    """
    result = PipelineResult(status="no_windows")
    previous = None
    last_action = None
    for iteration in count(1):
        trace = {"iteration": iteration}
        result.iterations.append(trace)
        with report.span("loop.iteration", iteration=iteration) if report else nullcontext({}):
            windows = _discover_windows(driver, report)
            if iteration == 1:
                controls, snapshot = _prepare_window(
                    original_request, windows, driver, selector, result, report, synthetic, iteration,
                )
                _route(result, trace, "first_pass", report)
                if controls is None:
                    return result
            else:
                # Discovery refreshes app/title metadata and tells us if the old
                # window vanished. Never query or act on an absent old handle.
                current_window = next((
                    window for window in windows
                    if all(window.get(name) == result.window.get(name) for name in ("pid", "window_id"))
                ), None)
                if current_window is None:
                    result.control = None
                    result.snapshot_id = None
                    result.observation = {"window_missing": True, "coverage": "current window no longer observed"}
                    snapshot = {}
                else:
                    result.window = current_window
                    snapshot = _observe_window(result, driver, report)
                current = _comparison_observation(windows, result, snapshot)
                state = {
                    "original_request": original_request, "previous": previous, "current": current,
                    "last_action": last_action,
                }
                answers, api_call = _judge_loop(selector, state, _loop_questions(), report, iteration, "routing")
                trace["routing_judgment"] = api_call
                if answers["done"] == "yes":
                    result.status = "completed"
                    _route(result, trace, "done", report)
                    return result
                if answers["changed"] == "no":
                    recovery, api_call = _judge_loop(
                        selector, state, _wait_questions(_app_context(result.window, snapshot)),
                        report, iteration, "unchanged",
                    )
                    trace["unchanged_judgment"] = api_call
                    if recovery["recovery"] == "error":
                        result.status = "unchanged_error"
                        result.reason = "Jev chose to report an error after no meaningful observed change."
                        _route(result, trace, "error", report)
                        return result
                    _route(result, trace, "wait", report)
                    trace["settling_ms"] = SETTLING_MS[recovery["wait_timing"]]
                    previous = current
                    _settle(trace["settling_ms"], sleep, report, kind="additional_wait")
                    last_action["requested_wait_ms_so_far"] += trace["settling_ms"]
                    continue
                if answers["another_window"] == "yes":
                    _route(result, trace, "select_window", report)
                    controls, snapshot = _prepare_window(
                        original_request, windows, driver, selector, result, report, synthetic, iteration,
                    )
                else:
                    _route(result, trace, "reuse_window", report)
                    controls = _observed_controls(result, snapshot, report)
                if controls is None:
                    return result
            previous = _comparison_observation(windows, result, snapshot)
            settling_ms = _select_and_act(
                original_request, controls, driver, selector, result, report, True,
                settling_context=None if result.window.get("synthetic") else _app_context(result.window, snapshot),
                iteration=iteration,
            )
            if result.status != "action_returned":
                return result
            last_action = {
                "tool": "click", "app_browser": _app_context(result.window, snapshot),
                "control": next(candidate.description for candidate in controls if candidate.record is result.control),
                "result": {name: result.action_result[name] for name in ("effect", "route", "delivery")
                           if name in result.action_result},
                "requested_wait_ms_so_far": settling_ms,
            }
            trace["settling_ms"] = settling_ms
            # The driver call above has returned. Only now begin the app timer.
            _settle(settling_ms, sleep, report, kind="after_action")
