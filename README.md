# ctrlloop

Headless Browser Use observation → existing Jev selection rounds → one Browser Use action → fresh observation.

This checkout runs the browser implementation. The Hermes/CUA desktop demo is preserved separately at `C:\Users\Jonat\ctrlloop-hermes-cua-demo-2026-09-25`. See [demo preservation](docs/hermes-plugin.md) before changing its compatibility launcher.

## Run

Python 3.11+, `uv`, and installed Chrome/Chromium are required. The existing `browser-use/` checkout is the local dependency, pinned to version 0.13.10 and commit `d8110c5ff87ccba887aaa726cdb780f2f84bef8d`. For a fresh clone:

```powershell
git clone https://github.com/browser-use/browser-use.git browser-use
git -C browser-use checkout d8110c5ff87ccba887aaa726cdb780f2f84bef8d
uv sync --locked --no-dev
```

Set `TYPESAFE_API_KEY` in the environment or existing local `.env`, then:

```powershell
uv run --locked python src/main.py "Open the World section." --url https://www.cnn.com/ --max-iterations 1
uv run --locked python src/main.py "Enter the provided text into Email or phone." --url https://mail.google.com/ --text "demo@example.com" --max-iterations 1
```

The starting URL is explicit. The browser is always headless and local. Every invocation owns an independent browser session and fresh profile. Gmail starts signed out; CNN may show a consent dialog. Jev receives candidates from that actual current state.

Use `--executable-path` for a specific Chrome/Chromium executable. `--request-file` and `--text-file` read exact UTF-8 contents. `--env-file` references existing credentials without copying them. Run `python src/main.py --help` for all arguments.

## Behavior

- Use every entry in Browser Use's already-filtered `BrowserStateSummary.dom_state.selector_map`; no additional candidate eligibility filter.
- Reuse `Candidate`, `ChoiceSelector`, authored questions and response validation. Jev remains **jev-1.13.0**.
- Groups contain at most **50 candidates**, subject to byte sizing. Round questions are batched into requests of at most **90,000 bytes**, then winners advance together until one remains.
- The final round includes existing speculative click/type and settling questions. No new question was introduced.
- Browser Use's existing registry executes `click(index)` or `input(index, text, clear=False)`. Typing appends. Its executor retains its own focus, scrolling and click mechanisms.
- Prepared text is caller-supplied and consumed once when typing is dispatched. Jev does not generate text. Typing without available text returns `no_action`.
- Each iteration captures fresh state and dispatches at most one chosen action. The map is not refreshed between selection and execution.
- Jev's synchronous selector runs in a worker thread while the browser's async event loop remains responsive.
- The existing timer controls settling before the next observation. There is no wait after the final permitted action and no automatic action replay.
- `--max-iterations` defaults to 10. There is no completion question: `iteration_limit` is not a success claim.

The current decision surface is **click and type**. Scrolling, dropdown choice, tab selection, generated text and completion are not new model decisions in this migration; expanding those decisions requires auditing their Jev questions.

## Interfaces and reports

Final result JSON goes to stdout; diagnostics go to stderr. Ignored `reports/` contains observations, exact Jev questions/answers, probabilities, API counts, timings and action results. Native browser nodes remain in the live selector map; reports contain identifiers rather than recursive node objects.

Exit code 1 indicates an error. Exit code 2 is a bounded run without established completion. Ctrl+C interrupts the run and closes its browser.

```python
import asyncio
from typesafe_sdk import RetryPolicy, TypeSafeClient
from src.driver import BrowserDriver
from src.pipeline import run_pipeline
from src.selection import ChoiceSelector, PINNED_MODEL

async def main():
    driver = BrowserDriver()
    try:
        await driver.start("https://example.com/")
        with TypeSafeClient(model=PINNED_MODEL, retry=RetryPolicy(max_retries=0)) as client:
            result = await run_pipeline(
                "Open the More information link.", driver, ChoiceSelector(client),
                max_iterations=1,
            )
            print(result.to_dict())
    finally:
        await driver.close()

asyncio.run(main())
```

`src/main.py` is the entry point and narrow old-Hermes compatibility dispatcher; `browser_cli.py` owns configuration and reporting; `driver.py` owns Browser Use; `pipeline.py` owns the loop. Existing `selection.py`, `jev_questions.py` and `reporting.py` retain shared selection/reporting behavior. `plugins/hermes/` continues launching the preserved desktop demo.

See [workflow](docs/main-workflow.md), [demo preservation](docs/hermes-plugin.md), and the saved [Browser Use investigation](reports/browser-state-investigation-2026-09-25/REPORT.md). Older CUA research/schema documents are historical references; runnable desktop code is in the separate demo directory.

Migration verification: two simultaneous local headless workers each selected from 80 controls in two rounds (80 → 2 → 1), clicked the chosen button and typed their distinct prepared text. Six Jev calls covered the four loops. Browser processes, profiles and storage were separate, and temporary profiles were removed on close. The actual CLI also passed a local one-action check. These are local smoke results, not a claim of general website coverage; saved details are in `reports/migration-2026-09-25/SMOKE.json` and `CLI_SMOKE.json`.
