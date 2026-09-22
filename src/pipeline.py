"""Select a real window/control or the synthetic Start Menu search target."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

if __package__:
    from .driver import DriverError
    from .reporting import RunReport
    from .selection import Candidate, ChoiceSelector
else:
    from driver import DriverError
    from reporting import RunReport
    from selection import Candidate, ChoiceSelector


START_MENU_KEY = "start_menu"
START_MENU_SEARCH_KEY = "start_menu_search"
START_MENU_APP = "StartMenuExperienceHost.exe"


class ReadOnlyDriver(Protocol):
    def list_windows(self) -> list[dict[str, Any]]: ...

    def get_window_state(self, window: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass
class PipelineResult:
    status: str
    window: dict[str, Any] | None = None
    control: dict[str, Any] | None = None
    snapshot_id: str | None = None
    observation: dict[str, Any] = field(default_factory=dict)
    selections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # Only descriptions are shortened. Original request and source records stay exact.
    return value if len(value) <= limit else value[: limit - 1] + "…"


def window_candidates(windows: Sequence[dict[str, Any]]) -> list[Candidate]:
    # This is an application-defined destination, not an observed HWND. Keep it
    # available whether Start is closed or open, and give it one consistent path.
    start = {
        "synthetic": True,
        "app_name": START_MENU_APP,
        "title": "Start Menu",
        "target": "windows_start_menu",
    }
    candidates = [Candidate(
        START_MENU_KEY,
        {
            "title": "Start Menu",
            "purpose": "Search Windows for apps, settings, and documents, including apps that are not open.",
        },
        start,
    )]
    for index, window in enumerate(windows):
        if (
            window.get("app_name", "").casefold() == START_MENU_APP.casefold()
            and window.get("title") == "Start"
        ):
            # An open real Start window should not create a competing route
            # that exposes all pinned apps instead of the requested Search field.
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


def start_menu_search_candidate() -> Candidate:
    """A semantic text-entry target for a future executor to resolve through CUA.

    It intentionally has no HWND, snapshot, element token, or pixel coordinates.
    Those must come from a fresh real observation after opening Start.
    """
    control = {
        "synthetic": True,
        "role": "Edit",
        "label": "Search",
        "input_type": "text",
        "actions": ["type_text"],
        "target": "windows_start_menu_search",
    }
    return Candidate(
        START_MENU_SEARCH_KEY,
        {
            "role": "Edit",
            "label": "Search",
            "input_type": "text",
            "purpose": "Type a query to search Windows for apps, settings, and documents.",
        },
        control,
    )


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
    original_request: str, driver: ReadOnlyDriver, selector: ChoiceSelector,
    *, report: RunReport | None = None,
) -> PipelineResult:
    """Return an observed or synthetic selection, without executing UI actions.

    Real controls belong to their saved observations only. Synthetic Start search
    is a semantic destination, never a fabricated driver handle. A later executor
    must resolve it to a live field before typing supplied text.
    """
    windows = driver.list_windows()
    if report is not None:
        report.append("windows_discovered", windows=windows)
    window_selection = selector.select(original_request, window_candidates(windows), stage="windows")
    result = PipelineResult(status="no_windows", selections={"windows": window_selection.rounds})
    if window_selection.winner is None:
        result.reason = "Window discovery returned no candidates."
        return result
    result.window = window_selection.winner.record
    if report is not None:
        report.append("window_selected", window=result.window, selections=window_selection.rounds)
    if window_selection.winner.key == START_MENU_KEY:
        control_selection = selector.select(
            original_request, [start_menu_search_candidate()], stage="controls"
        )
        result.control = control_selection.winner.record
        if report is not None:
            report.append("control_selected", control=result.control, selections=control_selection.rounds)
        result.selections["controls"] = control_selection.rounds
        result.observation = {
            "source": "synthetic",
            "coverage": "application-defined Start Menu search; no live control snapshot was taken",
        }
        result.status = "selected"
        return result
    try:
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
    control_selection = selector.select(original_request, control_candidates(elements), stage="controls")
    result.selections["controls"] = control_selection.rounds
    result.control = control_selection.winner.record
    if report is not None:
        report.append("control_selected", control=result.control, selections=control_selection.rounds)
    result.status = "selected"
    return result
