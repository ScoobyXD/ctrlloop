"""Definitions and lookup for application-defined windows and their controls.

Add destinations to DEFAULT_REGISTRY here. Both levels use the same Candidate
as observed UI; the registry owns copies and marks their records synthetic.
These are selection targets, not live CUA handles or action implementations.
"""

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

if __package__:
    from .selection import Candidate
else:
    from selection import Candidate


@dataclass(frozen=True)
class SyntheticWindow:
    window: Candidate
    controls: tuple[Candidate, ...]
    matches_observed: Callable[[Mapping[str, Any]], bool] | None = None


class SyntheticRegistry:
    """Ordered definitions with isolated candidate records for each selection.

    Window keys must be unique and cannot use the observed ``window_`` prefix.
    Control keys are scoped to their owning window. An empty registry disables
    synthetic destinations; an empty control list is a valid no-controls result.
    """

    def __init__(self, windows: Sequence[SyntheticWindow]):
        self._definitions: dict[str, SyntheticWindow] = {}
        for definition in windows:
            definition = SyntheticWindow(
                window=deepcopy(definition.window),
                controls=tuple(deepcopy(definition.controls)),
                matches_observed=definition.matches_observed,
            )
            key = definition.window.key
            if not isinstance(key, str) or not key.strip() or key.startswith("window_"):
                raise ValueError("Synthetic window keys must be nonempty and cannot start with 'window_'.")
            if key in self._definitions:
                raise ValueError(f"Duplicate synthetic window key: {key}")
            control_keys = set()
            for control in definition.controls:
                if not isinstance(control.key, str) or not control.key.strip():
                    raise ValueError("Synthetic control keys must be nonempty strings.")
                if control.key in control_keys:
                    raise ValueError(f"Duplicate synthetic control key in {key}: {control.key}")
                control_keys.add(control.key)
            for candidate in (definition.window, *definition.controls):
                candidate.record["synthetic"] = True
            self._definitions[key] = definition

    def windows(self) -> list[Candidate]:
        return [deepcopy(definition.window) for definition in self._definitions.values()]

    def controls_for(self, window_key: str) -> list[Candidate] | None:
        definition = self._definitions.get(window_key)
        return None if definition is None else list(deepcopy(definition.controls))

    def replaces(self, window: Mapping[str, Any]) -> bool:
        return any(
            definition.matches_observed is not None and definition.matches_observed(window)
            for definition in self._definitions.values()
        )


WINDOWS_SEARCH_KEY = "windows_search"


WINDOWS_SEARCH = SyntheticWindow(
    window=Candidate(
        WINDOWS_SEARCH_KEY,
        {
            "title": "Windows Search",
            "purpose": "Open Windows Search and type the supplied text to find apps, settings, and documents. Select a real Search window to use existing results.",
        },
        {
            "title": "Windows Search",
            "target": WINDOWS_SEARCH_KEY,
        },
    ),
    controls=(),
)

DEFAULT_REGISTRY = SyntheticRegistry((WINDOWS_SEARCH,))
