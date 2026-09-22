"""Select windows and controls, optionally dispatching one native click."""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
import logging
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


def run_pipeline(
    original_request: str, driver: Driver, selector: ChoiceSelector,
    *, report: RunReport | None = None,
    synthetic: SyntheticRegistry = DEFAULT_REGISTRY,
    execute_click: bool = False,
) -> PipelineResult:
    """Return a selection, optionally executing exactly one click, then stop.

    Real controls belong to their saved observations only. Synthetic controls
    are semantic destinations and cannot be clicked. Native click results report
    action facts, not completion of the original request. Every selected real
    window is brought forward before obtaining the control snapshot, including
    runs without a click. No post-click read, retry or extra Jev stage runs here.
    """
    with (
        report.span("window_discovery", stage="windows", tool="list_windows")
        if report is not None else nullcontext({})
    ):
        windows = driver.list_windows()
    if report is not None:
        report.append("windows_discovered", windows=windows)
    with report.span("candidate_preparation", stage="windows") if report is not None else nullcontext({}):
        candidates = window_candidates(windows, synthetic=synthetic)
    with report.span("window_selection", stage="windows") if report is not None else nullcontext({}):
        window_selection = selector.select(
            original_request, candidates, stage="windows",
            **({"report": report} if report is not None else {}),
        )
    result = PipelineResult(status="no_windows", selections={"windows": window_selection.rounds})
    if window_selection.winner is None:
        result.reason = "Window discovery returned no candidates."
        return result
    result.window = window_selection.winner.record
    if report is not None:
        report.append("window_selected", window=result.window, selections=window_selection.rounds)
    with report.span("synthetic_resolution", stage="controls") if report is not None else nullcontext({}):
        controls = synthetic.controls_for(window_selection.winner.key)
    if controls is not None:
        result.observation = {
            "source": "synthetic",
            "coverage": "application-defined controls; no live control snapshot was taken",
        }
    else:
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
            try:
                report.append("action_returned", tool="bring_to_front", action_result=foreground_result)
            except (OSError, ValueError):
                # Focus changed even if the report write failed. Preserve that
                # fact for callers and stop before taking any further action.
                error = DriverError("The window was brought forward, but its result could not be saved.",
                                    code="report_write_failed", tool="bring_to_front")
                error.context.update(stage="bring_to_front", selection=result.to_dict(),
                                     action_result=foreground_result)
                raise error from None
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
        if elements is None:
            result.status = "observation_unavailable"
            result.reason = "The window read did not provide a structured control observation."
            return result
        if not elements:
            result.status = "observation_degraded" if snapshot.get("degraded") else "no_controls"
            result.reason = "The window read returned no indexed controls; this does not prove none exist."
            return result
        if not result.snapshot_id:
            result.status = "observation_unavailable"
            result.reason = "The control observation has no snapshot identity."
            return result
        with report.span("candidate_preparation", stage="controls") if report is not None else nullcontext({}):
            controls = control_candidates(elements)
    with report.span("control_selection", stage="controls") if report is not None else nullcontext({}):
        control_selection = selector.select(
            original_request, controls, stage="controls",
            **({"report": report} if report is not None else {}),
        )
    result.selections["controls"] = control_selection.rounds
    if control_selection.winner is None:
        result.status = "no_controls"
        result.reason = "The selected synthetic window defines no controls."
        return result
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
                    return result
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
                try:
                    report.append("action_returned", tool="click", action_result=result.action_result)
                except (OSError, ValueError):
                    # The click has already happened. Return its facts even when
                    # the report sink fails, so callers do not retry blindly.
                    logging.getLogger(__name__).warning("Could not save the action result to the run report; returning it directly.")
    return result
