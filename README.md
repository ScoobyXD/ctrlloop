# ctrlloop

The Python pipeline offers discovered desktop windows plus a synthetic
**Start Menu** option. Jev chooses a window; a real window is inspected through
Windows UI Automation, then its observed controls are offered for selection.
Choosing Start Menu instead offers exactly one synthetic **Search** text field.
The pipeline executes the selected control's CUA click, waits for the app to
settle, observes again, and lets Jev judge completion and route the next step.
Every run follows this loop with no iteration cap. The result is JSON containing
selected records, observations, actions, judgments, and selection rounds. The
[main workflow](docs/main-workflow.md) is the authoritative description. Every selected real window
is brought to the foreground using CUA `bring_to_front({pid, window_id})` before
taking the control snapshot, regardless of its minimized state.
Activation failures stop the run; native results are retained in the report.

Selection retains identities in Python. Click execution uses the control's
existing snapshot-bound target and CUA's default background delivery. The loop
captures fresh controls after each action and
uses batched Jev judgments to stop, reuse the current window, select another
window, or wait again. It does not add automatic click escalation,
coordinate fallback, typing, or action retries.

The Start Menu route is available even when the real menu is closed. An observed
Start window is represented by this same route, so it does not add a duplicate
choice with pinned apps and other controls. Other real windows retain their order
and identity. Successful empty window discovery still leaves Start Menu available;
driver errors remain errors.

Both Start Menu and Search records have `synthetic: true`. Search has role `Edit`,
`input_type: "text"`, and semantic target `windows_start_menu_search`. It has no
invented PID, window handle, element index/token, geometry, or snapshot identity.
The existing singleton rule selects Search without an extra Jev request.

A synthetic selection returns `no_action`: it has no live click
target. Opening Start, resolving its search field, and typing are deferred.

## Defining synthetic windows and controls

[src/synthetic.py](src/synthetic.py) is the single place for built-in synthetic
options. `START_MENU` defines the existing window, Search control, and rule that
replaces an observed Start window. `DEFAULT_REGISTRY` lists enabled definitions
in their selection order. To add another built-in option, define it beside
`START_MENU` and include it in that registry; no pipeline or selector edits are
needed.

A `SyntheticWindow` groups existing `Candidate` objects:

| Field | Purpose |
| --- | --- |
| `window` | Stable key, concise Jev description, and local window record |
| `controls` | Ordered tuple of candidates offered when this window wins |
| `matches_observed` | Optional pure predicate identifying real windows this definition replaces |

The registry adds `synthetic: true` to records and returns independent copies so
caller edits and earlier results cannot change later selections. Use semantic
`target` names and advertised actions in local records; these definitions contain
no invented HWNDs, element tokens, or snapshots. Actions describe intent only;
they do not implement execution. Matchers must not mutate observed records.

For an application-specific registry, reuse the same definitions and inject it:

```python
from src.pipeline import run_pipeline
from src.selection import Candidate
from src.synthetic import START_MENU, SyntheticRegistry, SyntheticWindow

launcher = SyntheticWindow(
    window=Candidate(
        "app_launcher",
        {"title": "App Launcher", "purpose": "Find an installed application."},
        {"title": "App Launcher", "target": "app_launcher"},
    ),
    controls=(Candidate(
        "search",
        {"role": "Edit", "label": "Search", "input_type": "text"},
        {"role": "Edit", "label": "Search", "input_type": "text",
         "actions": ["type_text"], "target": "app_launcher_search"},
    ),),
)
registry = SyntheticRegistry((START_MENU, launcher))
result = run_pipeline(original_request, driver, selector, synthetic=registry)
```

This example is not enabled by default. Window keys must be nonempty and unique;
`window_` is reserved for discovered windows. Control keys must be nonempty and
unique within their window. Duplicate or reserved keys fail at registry creation.
A single control advances without Jev; multiple controls use the same selection
rounds, with one API call per round. A definition with no controls returns
`no_controls`. Passing
`SyntheticRegistry(())` disables synthetic options and their replacement rules.
Observed windows otherwise retain their original identity, order, and keys.

## Run

From the repository root, with Python 3.11+ and `uv`:

```powershell
uv sync --locked
# Set TYPESAFE_API_KEY in your process environment or local .env (not committed).
uv run --locked python src/main.py 'Click the Test button in my test window.'
```

The loop continues until Jev judges completion, an error or unsupported/unavailable
observation stops progress, or the user interrupts it. There is no iteration limit.

The dependencies remain pinned to `typesafe-sdk==0.7.1`,
`python-dotenv==1.2.3`, and `httpx2==2.13.0`. The existing `pyproject.toml` pin is `jev-1.13.0`;
the pipeline requires that exact model and validates every response model.
Requested and returned model identities and the run-report path are logged to
stderr; results are JSON on stdout. Reports include the request and observed
UI metadata described below. Authentication headers and API keys are not recorded.

CUA Driver 0.28.2 must already be installed with its daemon running on the
interactive Windows desktop. The adapter finds `cua-driver` on PATH or in its
default Windows install directory; `--driver <path>` overrides the executable.
Each user request owns one persistent MCP client connected explicitly to the
existing daemon. The client starts lazily on the first CUA call, is reused across
every loop iteration, and closes on completion, error, or interruption. A later
request gets its own client even if its wording is identical. It does not start
or stop the daemon. See [driver setup](docs/CUA_DRIVER.md).
Every CUA child process receives `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, so the
adapter keeps CUA CLI telemetry off even if the saved setting or parent
environment enables it. This avoids the Windows telemetry worker delaying
captured output after a command finishes.

There are no background health checks. EOF, transport errors and bounded request
timeouts detect a lost connection. Reads reconnect and retry once; native tool
refusals are not generally retried. A lost action response stops the run without
replaying the action, because delivery may already have occurred. After a
connection loss, only controls from a fresh snapshot may be clicked.

For a multiline request, `--request-file request.txt` reads UTF-8 bytes without
trimming or normalizing line endings. Use `--env-file <path>` to read an existing
credential file without copying it. Existing environment variables take priority.

```powershell
uv run --locked python src/main.py --request-file request.txt --max-elements 5000 --max-depth 25
uv run --locked python -m unittest discover -s tests -v
```

The source modules live directly in `src/` and feed into `src/main.py`:

- `driver.py` discovers and activates windows, reads accessibility state, and dispatches native clicks.
- `cua_mcp.py` owns the request's MCP connection, framing, timeouts and client cleanup.
- `selection.py` sends all group questions in one Jev call per round and advances one winner per group.
- `synthetic.py` defines synthetic windows, their controls, and duplicate-matching rules.
- `pipeline.py` runs window selection, activation, fresh observations, control selection, clicks, and later-pass judgments.
- `reporting.py` collects events and actual Jev HTTP bodies in memory, then writes the report once at the end.
- `main.py` provides the CLI, loads configuration, and prints the result.

From the repository root, `uv run --locked python -m src.main` also runs the CLI.
The modules are importable from the checkout:

```python
from typesafe_sdk import RetryPolicy, TypeSafeClient
from src.driver import CuaDriver
from src.pipeline import run_pipeline
from src.selection import ChoiceSelector, PINNED_MODEL

with CuaDriver() as driver, TypeSafeClient(model=PINNED_MODEL, retry=RetryPolicy(max_retries=0)) as client:
    result = run_pipeline(original_request, driver, ChoiceSelector(client))
    print(result.to_dict())
```

`run_pipeline` always runs the loop. It reuses the selected window/control
dictionaries and keeps the driver's native dictionary as `action_result`.
The optional `sleep=` argument supports a fake sleeper in offline tests. The
existing selector, driver, result, and reporting contracts serve the loop.

The Python API raises `DriverError` or `SelectionError` for failures. The CLI
reports them as `{"status":"error","error":"..."}` with exit code 1.
Driver errors also include a safe diagnostic `code`, tool and process exit code
when available. A failed window read retains its stage, selected window metadata,
and completed window-selection rounds. A click failure retains `stage: "click"`,
the completed `selection`, and any parsed native `action_result`. Python callers
can inspect these in `DriverError.context`. Raw driver stdout/stderr and UI trees
are not copied into error messages. An unknown driver failure is reported as unknown;
it is not automatically attributed to daemon access.

| Status | Meaning | CLI exit |
| --- | --- | --- |
| `completed` | The loop's later-pass judgment found the user's goal complete from observed state | 0 |
| `unchanged_error` | The loop observed no meaningful progress and Jev chose error instead of waiting | 2 |
| `no_action` | Click requested for a synthetic target without live CUA identifiers | 2 |
| `no_windows` | No first-stage candidate selected; normally unreachable with the Start Menu option | 2 |
| `no_controls` | A valid read returned no indexed controls, or a synthetic window defines no controls | 2 |
| `observation_degraded` | A degraded read returned no indexed controls | 2 |
| `observation_unavailable` | Structured controls or their snapshot identity were unavailable | 2 |
| `error` | Driver, configuration, or Jev request/response failure | 1 |

Nonempty degraded observations remain selectable and retain their quality flags.
Selecting a control and receiving its action result do not establish that the
original request is complete. Inspect
`action_result.effect`, `route`, and any `delivery`, `evidence`, or `escalation`.
Effects such as `unverifiable` and `suspected_noop` remain action facts, followed
by fresh observation and judgment; native refusals and failures return `error`/exit 1.
A timeout or unreadable response leaves delivery unknown and is never retried.

## Debug in VS Code

The CLI chooses a unique `reports/run-*.json` path in this project and prints it
to stderr. Events and timestamps stay in memory during execution; the file is
created once at the end. The same behavior applies in VS Code without additional
launch arguments. The completed file is an indented JSON array of events with UTC
timestamps and event names. Nested objects and arrays expand onto separate lines.
The folder is ignored by Git.

Reports contain these milestones:

- `run_started`: the original request.
- `windows_discovered`: the complete observed window list, in discovery order;
  it does not include the synthetic Start Menu option.
- `jev_request`: the actual JSON body captured by the HTTP client's request hook
  after SDK serialization and before network access, plus a request ID, URL, method,
  and byte count. Its `body` contains the exact JSON fields and values sent to Jev,
  including every group question and candidate description for the round. JSON
  whitespace is
  normalized by the report writer; this is not a dump of Python object internals.
- `jev_response`: the response body before SDK/selection validation, associated
  with the same request ID. `body.answers` retains every chosen label, confidence,
  and complete probability distribution, alongside model and token usage data.
  Non-JSON responses use `body_text` instead. A response-body read failure records
  `jev_response_error` with HTTP status and the exception type.
- `window_selected`, `window_observed`, and `control_selected`: selected identities,
  completed selection rounds, and observation quality. Singleton selections appear
  here even when they need no selection question; executable loop singletons
  still request timing. The Start Menu and Search selections
  are marked `synthetic: true` in `window_selected` and `control_selected`. This
  route records no `window_observed` event because no live snapshot is taken.
- `action_requested` and `action_returned`: native click arguments and returned
  action facts. A synthetic selection instead
  records `action_skipped` with its reason. A click failure ends with `run_error`.
- `loop_judgment` and `loop_route`: the iteration's batched questions, validated
  answers and probabilities, and selected continuation or stop route.
- `run_finished`, `run_error`, or `run_interrupted`: the result or failure reached.

The same event array includes monotonic elapsed timings for startup, discovery,
candidate preparation, selection rounds, foreground activation, UIA reads,
optional action preparation/invocation, loop judgments, and settling waits.
Selection rounds retain iteration tags so repeated round names remain distinct.
See [reading run timings](docs/timing.md)
for event correlation, bottleneck inspection, and measurement limits. All group
questions in a round share one observed API call duration; native action return
latency does not establish a visible UI change or completion of the original request.

The reporter retains each event in memory without serializing or copying the
entire event history. Once the operational result and normal client cleanup are
complete, it serializes the full report and writes it once. Report generation and
writing are timed separately from the operational run; their completed duration
is returned in the JSON result on stdout. No second write is needed to insert the
duration of the report's own write.

Ordinary failures and interruptions produce a final report when the process can
finish normally. An abrupt process termination can lose the in-memory report.
Reports intentionally contain user requests, UI titles/values, and native action
diagnostics, but no HTTP headers or raw exception messages.

The reporter does not filter candidates, alter questions, change grouping, or
choose winners. It observes the existing pipeline. The JSON returned on stdout
retains selection and observation information alongside the optional action result.

Open this repository folder in VS Code and select **ctrlloop: step through main**
in Run and Debug. Press **F5**, enter the original request, and the debugger pauses
at the entry point. The checked-in launch configuration uses `.venv` and the
integrated terminal. Set a breakpoint on the first line inside `main()` to skip
imports; **F10** steps over a line and **F11** steps into a function. Use the
Variables panel or Debug Console to inspect values.

For a window-read failure, break on `driver.get_window_state(result.window)` in
`src/pipeline.py` and inspect `result.window`. Inside `src/driver.py`, `_payload`
receives the native MCP result envelope, including structured content and tool
error information. Inspect it locally when the safe diagnostic code is insufficient.

`python -m pdb src/main.py ...` starts the separate terminal debugger: `(Pdb)` is
a prompt waiting for debugger commands, not application output. It does not use
VS Code's graphical breakpoints. See the [VS Code Python debugger guide](https://code.visualstudio.com/docs/python/debugging).

## Selection contract

Start Menu is prepended to the first-stage options. Other real windows preserve
the driver's observed order, as do controls from real snapshots. Each group contains at most ten
candidates and receives one group question using Jev Choice. One winner from
every group advances in group order; winners are regrouped in tens until one
remains. If selection starts with only one candidate, no selection question is
needed; an executable singleton still gets its timing question.
For 1,000 candidates, the round counts are `1000 -> 100 -> 10 -> 1`. Group size ten is fixed for this experiment.

The selection question is:

> Which of these candidates is best suited to help fulfill the user’s original request, based on the provided candidate information?

Selection keeps the unchanged original request in shared `state`. Final
control questions also explicitly name the observed app/browser and include one
independent conditional settling-time Choice per finalist in that same call.
Later-pass requests add the minimal previous/current observations and last-action
facts needed for the comparison.
Choice options contain concise descriptions of the observed or synthetic records, with stable
local labels mapped back to those records. Window descriptions use title, app,
visibility, minimization, and geometry. Controls use role, label/value, advertised
capabilities, available state, geometry, and up to two explicitly linked indexed
ancestors. Description text is capped at 240 characters (120 for ancestor labels);
the full observed records remain local and the original prompt is never shortened.
Selection does not send PID, HWND, snapshot token, screenshot, or a redundant full
tree as state.

**Every selection round makes exactly one API call containing all its group
questions.** The next round starts after that response is processed, because it
needs the previous round's winners. For 392 control candidates, the rounds are
`392 -> 40 -> 4 -> 1`: 40 group questions in one call, then four in one call,
then one in one call.

Only the last of those control calls adds timing questions. The
exact choices are `<50ms`, `100ms`, `200ms`, `300ms`, `400ms`, and `>500ms`, mapped
in code to 50, 100, 200, 300, 400, and 500 ms. After CUA returns, the loop waits
the winning candidate's interval and captures fresh state. Every later pass
batches change, completion, and window-switch judgments in one call. Unchanged
state gets a separate wait/error plus conditional timing batch; waiting never
replays an action. See the [main workflow](docs/main-workflow.md) for details.

There is no byte-based request splitting or request-budget CLI configuration.
Actual service limits still apply; an oversized request fails explicitly rather
than being split into extra calls or having candidates silently removed. Actual
input/output token usage is retained for each round's API call. See
[main workflow](docs/main-workflow.md) for the workflow and timing contract.

Returned model identity, question keys, answer type, option labels, probability
shape, and usage are validated. Confidence and probabilities never rank winners
across groups or gate advancement. There are no scores, thresholds, beams, or
synthetic “none” options.

## Observation limits

Observation calls use `list_windows({})` and `get_window_state` with
the selected record's exact `pid`/`window_id`, accessibility enabled and screenshots
disabled. The adapter separates stdout/stderr and validates JSON and success
shape, because some driver failures print plain text with exit code zero.
The selected action is `click` using the selected
window's `pid`/`window_id` and the original `element_token`, or the matching
`element_index`/`snapshot_id` pair when no token is present. The loop then waits
and observes again before deciding whether to continue.

Each real control selection comes from one fresh snapshot. The synthetic Search branch takes no
control snapshot and marks its observation source as `synthetic`.
`element_index` is not an array offset;
parent context comes only from explicit links to observed records. The structured
`elements` array omits some containers visible in `tree_markdown`, and the default
walk is bounded at 5,000 nodes / depth 25. This pipeline does not infer missing
ancestry or claim full coverage. Windows 0.28.2 commonly reports
`elements_complete:false`, including successful reads; that alone is not failure.

Real window identities are live OS identities. Control indices/tokens are bound to
the recorded snapshot in the running daemon. Separate one-shot CLI calls use that
same daemon cache; ending the CLI process alone does not invalidate it. A newer
snapshot of the same window, cache eviction, a runtime restart, or a changed
window identity can invalidate the target. The pipeline takes no intervening
snapshot between selecting and executing a control and preserves CUA's
stale-target refusal if another client supersedes it. A later loop pass constructs
new candidates from its fresh observation.
Exported report handles are historical evidence, not permanent UI identifiers or
authorization to replay an action. See the [versioned cache contract](docs/cua-driver/0.28.2/README.md#7-click-execution-contract).

A host smoke test with the production adapter used a disposable, nonactivating
WinForms Test button. Separate `list_windows`, `get_window_state`, and `click`
calls produced exactly one button callback and no post-action driver reads;
the fixture then exited. CUA returned `effect: "unverifiable"`,
`route: "accessibility"`, and background delivery. This confirms the isolated
click path. The sandbox separately returned `pipe_access_denied`; host execution
could reach the existing daemon.

Implementation references: [TypeSafe Python usage](https://docs.typesafe.ai/sdk/python/usage),
[Choice](https://docs.typesafe.ai/primitives/choice),
[API](https://docs.typesafe.ai/api),
[verified CUA contracts](docs/cua-driver/0.28.2/README.md), and the
[CUA Driver skill](skills/cua-driver-skill-0.28.2/SKILL.md) with its
[Windows companion](skills/cua-driver-skill-0.28.2/WINDOWS.md).
