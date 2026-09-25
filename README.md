# ctrlloop

Headless Browser Use observation → existing Jev selection rounds → one Browser Use action → fresh observation.

This checkout runs the browser implementation. The Hermes/CUA desktop demo is preserved separately at `C:\Users\Jonat\ctrlloop-hermes-cua-demo-2026-09-25`. See [demo preservation](docs/hermes-plugin.md) before changing its compatibility launcher.

## Run

Python 3.11+, `uv`, and installed Chrome/Chromium are required. The existing `browser-use/` checkout is the local dependency, pinned to version 0.13.10 and commit `d8110c5ff87ccba887aaa726cdb780f2f84bef8d`. For a fresh clone:

```powershell
git clone https://github.com/browser-use/browser-use.git browser-use
git -C browser-use checkout d8110c5ff87ccba887aaa726cdb780f2f84bef8d
git -C browser-use apply ../patches/browser-use-profile-copy-cleanup.patch
git -C browser-use apply ../patches/browser-use-storage-state-privacy.patch
git -C browser-use apply ../patches/browser-use-attached-profile.patch
uv sync --locked --no-dev
```

The local cleanup patch makes Browser Use remove its temporary profile if copying fails, including while copying `Local State`. The privacy patch keeps storage-state contents out of profile representations and the configuration-conflict warning. The attachment patch prevents copying a profile when connecting to an already-running browser. All preserve the existing Browser Use APIs.

Set `TYPESAFE_API_KEY` in the environment or existing local `.env`, then:

```powershell
uv run --locked python src/main.py "Open the World section." --url https://www.cnn.com/ --max-iterations 1
uv run --locked python src/main.py "Enter the provided text into Email or phone." --url https://mail.google.com/ --text "demo@example.com" --max-iterations 1 --browser-profile demo --fresh-profile
```

**Browsers persist independently of commands.** Each named automation profile has its own headless Chrome process and durable directory under `.local/browsers/<name>/user-data`. Success, errors, iteration limits, and Ctrl+C detach the command; they do not close Chrome or delete its profile. Cookies, tabs, page JavaScript, and unsaved input remain available to subsequent commands while Chrome keeps running. Browser-owned cookies and site storage persist on disk across Windows restarts; live JavaScript memory cannot survive a reboot, and websites can expire their sessions.

The default profile is `default`. Use `--browser-profile work` for another account/workspace. One command at a time can control each profile; another command receives `browser_runtime_busy`. Different profiles run independently. `--url` explicitly navigates the current tab; omit it on follow-up commands to continue exactly where the browser is:

```powershell
uv run --locked python src/main.py "Click on history" --url https://x.com/home --max-iterations 5
uv run --locked python src/main.py "Open the first item" --max-iterations 1
```

Enable Windows user-logon startup once and inspect its status:

```powershell
.\.venv\Scripts\python.exe src/browser_runtime.py --install-startup
.\.venv\Scripts\python.exe src/browser_runtime.py --status
```

The hidden startup helper launches **every configured automation profile**, using the same directories and Chrome's session restoration. It does not run Jev tasks or import credentials. Browsers stay running when the helper exits and until Windows ends the user session/shuts down. Startup is tied to **your Windows sign-in**, not the pre-login system account. The absolute checkout/interpreter paths must remain available. `--start-all` runs the same helper manually. `--uninstall-startup` removes only the login entry, leaving running browsers and profiles intact. If Chrome exits unexpectedly, the next command restarts it with the same profile; there is no idle shutdown.

On **first use only**, the default behavior seeds the starting site's login from personal Chrome. Chrome 144+ must have **Allow remote debugging for this browser instance** enabled at `chrome://inspect/#remote-debugging`; approve its connection prompt if shown. Keep the starting website open to include localStorage/sessionStorage. Cookies matching the starting hostname can be imported without an open tab. `--profile-directory "Profile 2"` and `--user-data-dir "C:\path\to\User Data"` select the initial source. Startup verifies that profile through its own temporary diagnostic tab and never navigates/closes existing personal tabs.

Later commands use the persistent automation profile directly, without reconnecting to personal Chrome or re-importing its old snapshot. Logins and storage obtained by the automation browser are retained. The initial import still covers only the starting hostname/origin, not all personal Chrome profiles, IndexedDB, passkeys, or device-bound credentials; unrelated sites may need a login in the persistent browser. Missing initial import setup stops startup without silently selecting a signed-out session. Cookie values stay out of reports and Jev input.

`--fresh-profile` explicitly initializes a **new named persistent profile** signed out; it never erases/resets an existing profile. After its first command, omit the flag to reuse that profile. No profile deletion or browser shutdown is performed by ordinary commands.

Use `--executable-path` for a specific Chrome/Chromium executable. `--request-file` and `--text-file` read exact UTF-8 contents. `--env-file` references existing credentials without copying them. Run `python src/main.py --help` for all arguments.

Screenshots and live preview are enabled by default. Open the printed **Live page preview** localhost URL while the command runs to watch the current page. The viewer is read-only, refreshes about twice per second through the same Browser Use CDP connection, and stops with the run; Chrome itself remains running. Each invocation has its own viewer URL.

Viewport PNGs are saved to the printed report directory under `screenshots/`: `loop-0001-before.png`, `loop-0001-after.png`, and so on. The before image is captured just before dispatch, after selection; the after image is captured as soon as the action returns, including the last action and action errors. It can show a page still loading. Initial navigation failures save `navigation_error.png` when capture is possible. Images stay local and are never sent to Jev. Report events record image paths and capture/write timings. Use `--no-preview` or `--no-screenshots` to disable either feature and its overhead.

If startup returns `browser_navigation_failed`, no selection loop has run. Check the saved navigation image and the report's native navigation diagnostic, including `document_response.statusCode` when available. An HTTP response failure alone does not identify whether the cause is site access policy, authentication, or an upstream network response. Session reuse does not guarantee that a site accepts the automated browser. The page selector map also does not contain Chrome toolbar controls or Chrome's browsing history.

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
- Screenshots use native viewport capture without rebuilding the selector map. Preview capture runs in the background; action captures have separate timing spans. The `loop_action_finished` event marks action return before the after image, while the encompassing iteration span also includes artifact capture overhead.
- `--max-iterations` defaults to 10. There is no completion question: `iteration_limit` is not a success claim.

The current decision surface is **click and type**. Scrolling, dropdown choice, tab selection, generated text and completion are not new model decisions in this migration; expanding those decisions requires auditing their Jev questions.

## Interfaces and reports

Final result JSON goes to stdout; diagnostics go to stderr. Ignored `reports/` contains observations, exact Jev questions/answers, probabilities, API counts, timings and action results. Native browser nodes remain in the live selector map; reports contain identifiers rather than recursive node objects.

Exit code 1 indicates an error. Exit code 2 is a bounded run without established completion. Ctrl+C interrupts the run and disconnects its controller, leaving the browser running.

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

`src/main.py` is the entry point and narrow old-Hermes compatibility dispatcher; `browser_cli.py` owns configuration and reporting; `driver.py` attaches Browser Use; `browser_runtime.py` owns persistent browser launch, profile leases, and Windows startup; `browser_session.py` seeds the initial source session; `pipeline.py` owns the loop; `browser_preview.py` serves the local viewer. Existing `selection.py`, `jev_questions.py` and `reporting.py` retain shared selection/reporting behavior. `plugins/hermes/` continues launching the preserved desktop demo.

See [workflow](docs/main-workflow.md), [demo preservation](docs/hermes-plugin.md), and the saved [Browser Use investigation](reports/browser-state-investigation-2026-09-25/REPORT.md). Older CUA research/schema documents are historical references; runnable desktop code is in the separate demo directory.

Migration verification, before session reuse became the default: two simultaneous local headless workers with fresh profiles each selected from 80 controls in two rounds (80 → 2 → 1), clicked the chosen button and typed their distinct prepared text. Six Jev calls covered the four loops. Browser processes, profiles and storage were separate, and temporary profiles were removed on close. The actual CLI also passed a local one-action check. These are local smoke results, not a claim of general website coverage; saved details are in `reports/migration-2026-09-25/SMOKE.json` and `CLI_SMOKE.json`.
