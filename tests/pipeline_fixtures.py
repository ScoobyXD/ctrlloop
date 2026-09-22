"""Test-only composition of shared stages, stopped before the next loop pass.

These fixtures let stage/adapter regressions inspect one selection or one native
dispatch. They are not a public selection-only or single-click execution mode;
the application entry point always runs the full loop.
"""

from src.pipeline import PipelineResult, _discover_windows, _prepare_window, _select_and_act
from src.synthetic import DEFAULT_REGISTRY


def select_once(request, driver, selector, *, report=None,
                synthetic=DEFAULT_REGISTRY, deliver_click=False):
    result = PipelineResult(status="no_windows")
    windows = _discover_windows(driver, report)
    controls, _ = _prepare_window(request, windows, driver, selector, result, report, synthetic)
    if controls is not None:
        _select_and_act(request, controls, driver, selector, result, report, deliver_click)
    return result
