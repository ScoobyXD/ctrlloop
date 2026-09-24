# ctrlloop

[Minimal editable Hermes plugin: setup and exact input/output contract](docs/hermes-plugin.md).

The Python pipeline offers discovered desktop windows plus a synthetic
**Windows Search** option. Jev selects a destination; once it chooses a real window,
the pipeline retains that window's native identity for the rest of the run. Each iteration
inspects it through Windows UI Automation and offers its fresh controls for selection.
For a real window, Jev chooses the control and whether to click or enter text.
The pipeline dispatches that native CUA action, waits for the app to settle,
and starts the next selection from fresh observations.
Choosing Windows Search instead sends desktop **Win+S**, then directly types the
prepared query. A nonempty `--text` supplies the exact string for the first typing
operation, including Search. If typing is selected without pending prepared text,
the run returns `no_action`; Windows Search skips activation too.
The Win+S-to-typing handoff has no intervening observation,
confirmation, sleep, control selection, or Jev call. The next iteration observes
the real search results.
It repeats within an explicit iteration limit; task completion is not assessed.
The result is JSON containing selected records, observations, action facts,
iteration history, and selection rounds. The
[main workflow](docs/main-workflow.md) is the authoritative description. Every selected real window
is brought to the foreground using CUA `bring_to_front({pid, window_id})` before
taking the control snapshot, regardless of its minimized state.
Activation failures stop the run; native results are retained in the report.

Selection retains identities in Python. Click and targeted text execution use the control's
existing snapshot-bound target. Clicks require direct Windows accessibility
activation; synthetic mouse clicking is disabled. The patched driver uses UIA
Invoke, Toggle, SelectionItem.Select, ExpandCollapse.Expand, or an MSAA default
action when supported. Unsupported targets fail without a mouse or browser click
fallback. Accessibility providers can change focus, and dispatch alone does not
prove the requested effect. See [direct accessibility behavior and setup](docs/cua-driver/direct-accessibility-only.md).
The loop
uses the same generic question for every window and control selection round;
every group selects a supplied candidate. It does not add automatic click escalation,
coordinate fallback, additional waits, or action retries. Targeted text entry
uses CUA's existing `type_text` operation, explicitly choosing foreground delivery
for supported browsers from their observed application identity. Other targets
keep the native default unless a delivery mode is supplied. The pinned Windows
background UIA path appends to the existing value; the app adds no replacement
or caret-placement operation.

The synthetic Windows Search opener is available when fresh discovery has no
visible `SearchHost.exe` window titled `Search`. While that destination is open,
only its real window is offered, preserving its observed order and native identity
for control selection. This rule is evaluated whenever window discovery runs;
discovery stops after the first real window is selected. A selected real Search
window is retained just like any other real window.
If the opener is selected after the prepared text was consumed, the run stops
with `no_action` without sending Win+S or typing again. Prepared text belongs to
one run; a new run can supply another string. Driver errors remain errors.

The Windows Search window record has `synthetic: true` and no invented PID,
window handle, control, geometry, or snapshot identity. It has no control
candidates: selecting it with pending prepared text executes the two native
desktop commands directly. Missing, empty, or already consumed text returns
`no_action` before either native command.

## Defining synthetic windows and controls

[src/synthetic.py](src/synthetic.py) is the single place for built-in synthetic
options. `WINDOWS_SEARCH` defines the existing window with no synthetic controls
or observed-window replacement rule. `DEFAULT_REGISTRY` lists enabled definitions
in their selection order. Add further candidate definitions beside
`WINDOWS_SEARCH` and include them in that registry. A new executable synthetic
route also needs an explicit pipeline action mapping; a definition alone does
not implement execution.

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
from src.synthetic import WINDOWS_SEARCH, SyntheticRegistry, SyntheticWindow

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
registry = SyntheticRegistry((WINDOWS_SEARCH, launcher))
result = run_pipeline(original_request, driver, selector, synthetic=registry)
```

This example is not enabled by default. Window keys must be nonempty and unique;
`window_` is reserved for discovered windows. Control keys must be nonempty and
unique within their window. Duplicate or reserved keys fail at registry creation.
A custom synthetic control still needs a Jev selection; multiple controls
use the same selection rounds, with whole questions packed into parallel calls
when the complete body exceeds 90,000 bytes. A definition with no controls returns
`no_controls` unless it has a direct route, as Windows Search does. Custom
synthetic controls without native targets return `no_action`. Passing
`SyntheticRegistry(())` disables synthetic options and their replacement rules.
Observed windows otherwise retain their original identity, order, and keys.

## Run

From the repository root, with Python 3.11+ and `uv`:

```powershell
uv sync --locked
# Set TYPESAFE_API_KEY in your process environment or local .env (not committed).
uv run --locked python src/main.py 'Click the Test button in my test window.' --max-iterations 1
uv run --locked python src/main.py 'Search for budget review in the open Slack window.' --text 'budget review'
uv run --locked python src/main.py 'Open Chrome using Windows Search.' --text 'Chrome'
uv run --locked python src/main.py 'In my browser on x.com, open the History tab, then open Liked posts.' --text 'x.com'
```

Pass `--text` (or `run_pipeline(..., text=...)`) to prepare the exact string for
the first typing operation, either a selected real control such as a browser
address bar or Windows Search. It stays pending through click-only iterations,
and is consumed when typing is dispatched. Later typing returns `no_action`;
errors do not cause the prepared text to be replayed. Jev still chooses the window, control,
and action, and the original request stays unchanged.

The string is preserved, including Unicode, leading/trailing whitespace, and line
endings. Omitted text, Python `None`, and an empty string leave no prepared text
available; a nonempty whitespace-only string is accepted verbatim. A typing
operation without pending text returns `no_action` with the reason
`No prepared text is available for this typing operation (missing, empty, or already used).` Use
`--text-file text.txt` for exact UTF-8 file contents without newline conversion;
it is mutually exclusive with `--text`. The experimental Hermes integration asks
Hermes to interpret user intent/context into a concise English `request` and
prepare any `text` in the same initial `ctrlloop_run` call. The engine preserves
that supplied command as its original request. See the
[Hermes input contract and trial prompt](docs/hermes-plugin.md).

Pass `--delivery-mode foreground` or `--delivery-mode background` (or
`run_pipeline(..., delivery_mode="foreground")` / `"background"`) to choose the
existing native delivery mode for targeted control typing. An explicit choice
wins over the browser default. With no override, the app sends `foreground` when
the freshest observed `current_window.app_name` is `chrome`, `chromium`, `msedge`,
`brave`, `opera`, `vivaldi`, or `firefox`, compared case-insensitively with an
optional `.exe` suffix. This small process-name policy does not infer a browser
from its title or window class. Unknown and non-browser apps omit the argument
and retain CUA's default. Clicks and Windows Search's desktop commands keep
their existing behavior even when an override is supplied. No delivery-mode
question or automatic background-to-foreground retry is added.

Each iteration executes one selected control click/text action or the Windows Search
activation-and-typing sequence. After a real window is selected, subsequent
iterations skip window discovery and selection, bring the same window forward,
and read fresh controls using the original request and previous action context.
Windows Search's synthetic opener does not pin a window: the next iteration
discovers and selects again until a real window is chosen. If that real window
later closes or becomes unavailable, the driver error stops the run; there is no
automatic switch to another window. `--max-iterations` is a positive integer, default 10.
At the limit, the run returns `iteration_limit` without a further wait or observation.
An unsupported or unavailable observation, unavailable synthetic action,
missing prepared text, a disabled text target, a native/API error, or user interruption
can stop the run earlier.

The dependencies remain pinned to `typesafe-sdk==0.7.1`,
`python-dotenv==1.2.3`, and `httpx2==2.13.0`. The existing `pyproject.toml` pin is `jev-1.13.0`;
the pipeline requires that exact model and validates every response model.
Requested and returned model identities and the run-report path are logged to
stderr; results are JSON on stdout. Reports include the request and observed
UI metadata described below. Authentication headers and API keys are not recorded.

CUA Driver 0.28.2 must already be installed with its daemon running on the
interactive Windows desktop. Clicks require the [ctrlloop direct-accessibility patch](docs/cua-driver/direct-accessibility-only.md)
in that daemon. Before the first click on each connection, the adapter checks the
daemon's advertised `click` schema for `route: "accessibility"` as its only route
and default, then sends that route explicitly. An unpatched daemon is rejected
before click dispatch. Reads, text entry, and hotkeys remain available.
The adapter finds `cua-driver` on PATH or in its
default Windows install directory; `--driver <path>` overrides the executable.
`--driver-socket <named-pipe>` selects an already-running isolated daemon;
omitting it retains `\\.\pipe\cua-driver`. Choosing a different executable alone
does not change which daemon handles the actions.
Each user request owns one persistent MCP client connected explicitly to the
existing daemon. The client starts lazily on the first CUA call, is reused across
every loop iteration, and closes on completion, error, or interruption. A later
request gets its own client even if its wording is identical. It does not start
or stop the daemon. See [driver setup](docs/CUA_DRIVER.md).

After starting the patched daemon on the dedicated pipe described in the
[isolated runtime instructions](docs/cua-driver/direct-accessibility-only.md), run:

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.' `
    --driver .\.local\cua-driver\bin\cua-driver.exe `
    --driver-socket '\\.\pipe\ctrlloop-direct-accessibility'
```

The CUA MCP client process receives `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, so the
adapter keeps telemetry off even if the saved setting or parent environment
enables it. This avoids the Windows telemetry worker delaying client shutdown.

At connection startup (and reconnect), the client sets its session cursor's
`glide_duration_ms` to **1 ms** with CUA's `set_agent_cursor_motion`. The cursor
stays visible. The setting affects this connection only and does not bound the
complete native action. Setting it to zero would restore CUA's distance-dependent
animation. Setup shares the first call's timeout and must be acknowledged before
the requested call is sent.

There are no background health checks. EOF, transport errors and bounded request
timeouts detect a lost connection. Each transport attempt has a 60-second default
deadline, including any lazy startup/handshake. Reads reconnect and retry once for
connection loss, timeout, or an unavailable daemon, with a new deadline; native
tool refusals are not generally retried. A lost action response stops the run
without replaying the action, because delivery may already have occurred. After
a connection loss, only controls from a fresh snapshot may be acted on.

For a multiline request, `--request-file request.txt` reads UTF-8 bytes without
trimming or normalizing line endings. Use `--env-file <path>` to read an existing
credential file without copying it. Existing environment variables take priority.

```powershell
uv run --locked python src/main.py --request-file request.txt --max-elements 5000 --max-depth 25
uv run --locked python -m unittest discover -s tests -v
```

The source modules live directly in `src/` and feed into `src/main.py`:

- `driver.py` discovers and activates windows, reads accessibility state, and dispatches native clicks, hotkeys, and text entry.
- `cua_mcp.py` owns the request's MCP connection, framing, timeouts and client cleanup.
- [`jev_questions.py`](src/jev_questions.py) holds the authored selection, action, and timer questions.
- `selection.py` forms groups of at most 50 candidates within the request budget, packs whole questions into HTTP bodies of at most 90,000 bytes, sends a round's calls in parallel, then advances its selected candidates together.
- `synthetic.py` defines synthetic windows, their controls, and duplicate-matching rules.
- `pipeline.py` retains the first selected real window, refreshes its controls for each click/text action, and supports direct Windows Search actions before a real window is selected.
- `reporting.py` buffers events and actual Jev HTTP bodies, then saves the report and separate input/output files on a worker at the end.
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

`run_pipeline` always runs the loop, with `max_iterations=10` by default.
It reuses the selected window/control
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
| `iteration_limit` | Configured action-cycle bound reached; task completion was not assessed | 2 |
| `no_action` | Prepared text missing, empty, or already used; text target disabled; or custom synthetic control lacks live CUA identifiers | 2 |
| `no_windows` | No first-stage candidate selected; normally unreachable with the Windows Search option | 2 |
| `no_controls` | A valid read returned no indexed controls, or a synthetic window defines no controls | 2 |
| `observation_degraded` | A degraded read returned no indexed controls | 2 |
| `observation_unavailable` | Structured controls/snapshot identity unavailable or targeted typing with `partial` or `suspected_noop` effect | 2 |
| `error` | Driver, configuration, or Jev request/response failure | 1 |

Nonempty degraded observations remain selectable and retain their quality flags.
Selecting a control and receiving its action result do not establish its success.
Inspect `action_result.effect`, `route`, and any `delivery`, `evidence`, or `escalation`.
For clicks, effects such as `unverifiable` and `suspected_noop` remain action facts, followed
by fresh observation and next-action selection when another iteration remains;
native refusals and failures return `error`/exit 1.
`direct_accessibility_unsupported` means the daemon lacks the required contract,
or the target/gesture has no supported direct activation. `direct_accessibility_failed`
means direct activation failed or the driver returned a prohibited route.
A successful targeted text action with `effect: "confirmed"` or `"unverifiable"`
continues through the existing settling wait and fresh observation when another
iteration remains. Reports and `last_action` preserve the actual effect and native
metadata; an unverified dispatch is not relabeled as confirmed. Explicit `partial`
or `suspected_noop` effects still stop with `observation_unavailable`; unknown or
malformed native effects fail adapter validation as `invalid_action_result`.
Native errors still fail. Typing is not automatically
repeated because it was unverified, and no verification stage is added. This
does not assess task completion. Windows Search retains its desktop handoff and
fresh-observation flow.
A timeout or unreadable response leaves delivery unknown and is never retried.

## Debug in VS Code

The CLI chooses a unique `reports/run-<timestamp>-<id>/` folder in this project
and saves its event report as `total_run.json`. Each outgoing Jev input and complete
Jev output is saved directly alongside it. The report path and run folder are printed to stderr.
Events and the original request/response HTTP bodies stay in memory during execution;
files are created at the end. The same behavior applies in VS Code without additional
launch arguments. The completed file is an indented JSON array of events with UTC
timestamps and event names. Nested objects and arrays expand onto separate lines.
The folder is ignored by Git.

Payload filenames share one chronological sequence and identify their direction,
loop, workflow stage, and round, for example:

```text
reports/run-<timestamp>-<id>/
  total_run.json
  0001_jev_input_loop_1_window_round_1.json
  0002_jev_output_loop_1_window_round_1.json
  0003_jev_input_loop_1_window_round_2.json
  0004_jev_output_loop_1_window_round_2.json
  0005_jev_input_loop_1_control_round_1.json
  0006_jev_output_loop_1_control_round_1.json
```

Each input file preserves every JSON field and value of that outgoing API call,
including `model`, `state`, and the complete `questions` object with its criteria.
The file is formatted with two-space indentation and a blank line between
top-level fields. `state` comes first and `questions` second so the two inputs
are easy to distinguish; remaining fields follow. Only whitespace and field order
change. Failed requests are included.

Each JSON output file preserves every returned field and value captured before
SDK validation, with no wrapper or added metadata. It uses the same formatting
as inputs: two-space indentation, expanded nested objects and arrays, and a blank
line between top-level fields. This also applies to JSON HTTP error bodies.
Non-JSON responses retain their original bytes even though the filename ends
in `.json`. Transport failures and incomplete body reads do not produce an
output file.

The zero-padded file prefix uses at least four digits, expanding uniformly for
longer runs, and orders inputs and outputs together. It is separate from
`request_id`, which still pairs request/response events and counts API calls.
The events' `request_file` and `response_file` fields link filenames relative to
the run folder. Loop and round numbers distinguish repeated stages. Direct
selector calls without iteration metadata omit the loop, such as
`0001_jev_input_control_round_1.json`; an unscoped direct HTTP call uses
`0001_jev_input.json` and `0002_jev_output.json`. No extra payload parsing,
serialization, or file writes are added to the action loop.

Reports contain these milestones:

- `run_started`: the original request and exact text when supplied.
- `windows_discovered`: the complete observed window list, in discovery order;
  it does not include the synthetic Windows Search option. Discovery and window
  selection stop once the run retains a real window.
- `jev_request`: the actual JSON body captured by the HTTP client's request hook
  after SDK serialization and before network access, plus a request ID, URL, method,
  and byte count. Its `body` contains the exact JSON fields and values sent to Jev,
  including every group question and candidate description in that batch. JSON
  whitespace is
  normalized by the report writer; this is not a dump of Python object internals.
- `jev_response`: the response body before SDK/selection validation, associated
  with the same request ID. `body.answers` retains every chosen label, confidence,
  and complete probability distribution, alongside model and token usage data.
  Non-JSON responses use `body_text` instead. A response-body read failure records
  `jev_response_error` with HTTP status and the exception type.
- `window_selected`, `window_observed`, and `control_selected`: selected identities,
  completed selection rounds, and observation quality. A singleton window needs
  no selection question; a singleton control receives the selection question, with action and timing
  when executable. Windows Search is marked `synthetic: true` in
  `window_selected`; it has no `control_selected` or `window_observed` event.
  Reusing a real window emits a fresh `window_observed` but no new
  `window_selected`; that iteration has no window-selection rounds.
- `action_requested` and `action_returned`: native activation and input arguments
  and returned action facts. Windows Search records `hotkey` and `type_text`
  separately under the same iteration, including the exact prepared text. Missing
  prepared text or an unavailable custom synthetic action records `action_skipped`
  with its reason. A native action failure ends with `run_error`.
- `loop_route`: `continue` when another selection cycle will follow, or the
  terminal status such as `iteration_limit` or `no_action`.
- `run_finished`, `run_error`, or `run_interrupted`: the result or failure reached.

The same event array includes monotonic elapsed timings for startup, discovery,
candidate preparation, selection rounds, foreground activation, UIA reads,
action preparation/invocation, and settling waits.
Selection rounds retain iteration tags so repeated round names remain distinct.
For a timeline, a loop starts at current-state acquisition and ends when the
final CUA command is sent: the selected control's `click`/`type_text`, or Windows
Search's `type_text`. Native return and any settling follow that boundary. The `loop.iteration` span includes return and settling, so its
duration differs from this dispatch-bounded loop duration.
See [reading run timings](docs/timing.md)
for event correlation, bottleneck inspection, and measurement limits. Group
questions in each batch share one observed API call duration. Parallel durations
overlap, so their sum is not the round's elapsed time; native action return
latency does not establish a visible UI change or completion of the original request.

The reporter retains each event in memory without serializing or copying the
entire event history. Once the operational result and normal client cleanup are
complete, one worker serializes the full report and writes it once, then writes
the input and output files using the same JSON formatter. Non-JSON response
bodies are written as their original bytes. Parsing and formatting happen on that worker.
The CLI waits for that worker only at shutdown
so the files finish saving before the process exits.
In-memory capture still has a small cost; finalization adds shutdown time.
Report generation and writing are timed separately from the operational run; their completed duration
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

When no visible Search destination is observed, its synthetic opener is prepended
to the first-stage options. Real windows preserve
the driver's observed order, as do controls from real snapshots. Each group
contains at most 50 candidates and receives one Jev Choice question using
`QUESTION`. Before fixing the group, prepare its complete single-question body
through the SDK, including shared state and the envelope. If 50 candidates do not
fit within 90,000 bytes, use the largest contiguous prefix that fits and continue
with the remaining candidates. Each group selects one supplied candidate, which
advances in group order. Regroup survivors using the same rules for the next round.

In window/control selection, only a singleton window bypasses Jev. A singleton control still receives
the selection question, with action and timers when executable. For 1,000 control candidates,
the candidate counts are `1000 -> 20 -> 1` when the groups fit. The fixed group
maximum is 50; the byte budget can require smaller groups and additional rounds.
A round is final only when all remaining candidates fit into one actual group.

Authored questions `QUESTION`, `ACTION`, and `TIMER` live in
[`src/jev_questions.py`](src/jev_questions.py). `QUESTION` is reused for all window
and control narrowing rounds and final selections. The final control round also
includes an `ACTION` click-versus-`type_text` Choice
for every finalist and a separate hypothetical `TIMER` for each action per
finalist. Include all those questions before packing the round into one or more
calls. Consume only the winning control's action and matching timer after every
batch succeeds.

Typing uses only the prepared string supplied by the caller. It makes no text
selection API calls and does not extract words from the request. Click-only
iterations retain the string until the first typing dispatch consumes it. A
later typing selection, or one without supplied nonempty text, returns `no_action`.

Every loop request carries the unchanged `original_request`, current `iteration`,
`current_window` app/title context and `last_control`. The window is null before
selection; after a real window is retained, each fresh snapshot updates its
app/title context while preserving its native identity. The last control is null
until control action dispatch.
Before dispatch, set `last_control` to `{window, control, action}` with its source
window, descriptive control fields, and chosen tool. After CUA returns, `last_action` retains
the tool and complete native result in `last_action.result` for the next selection.
The Windows Search route has no selected control; its final `last_action` records `type_text`,
the exact text, and native result facts.
The pending control replaces `last_control` only immediately before its native
dispatch.

One mutable state dictionary is reused throughout the run. Every iteration
selects and acts from fresh observations, and all its calls share its iteration
number. Control candidates are prepared once per observation. State retains
`last_action` delivery facts and current `observation` coverage. Group questions
carry candidates, with no full current/previous control list in shared state and
no local snapshot diff or accumulating action history.
See [shared Jev state](docs/main-workflow.md#shared-jev-state).
Choice options contain concise descriptions of the observed or synthetic records, with stable
local labels mapped back to those records. Window descriptions use title, app,
visibility, minimization, and geometry. Controls use role, label/value, advertised
capabilities, available state, geometry, and up to two explicitly linked indexed
ancestors. Description text is capped at 240 characters (120 for ancestor labels);
the full observed records remain local and the original prompt is never shortened.
Selection does not send PID, HWND, snapshot token, screenshot, or a redundant full
tree as state.

**Every window and control round first prepares its complete HTTP body through the SDK.**
If it is at most **90,000 bytes**, send one call. Otherwise, preserve question
order and pack the largest contiguous batches of whole questions that fit,
counting shared state and the envelope in each body. Each batch repeats the
same frozen state for the round. A thread pool sends the batches concurrently
through the shared SDK client. Collect validated answers by their existing
question IDs, then advance winners in original group order after all batches
succeed. Packing drops no supplied candidates; multiple parallel calls remain
one round.

When groups fit, 300 controls narrow as `300 -> 6 -> 1`, and 963 narrow as
`963 -> 20 -> 1`. Oversized descriptions or shared state can reduce group sizes
and add rounds. Complete prepared body sizes determine the API call count within
each round; even a final round can need parallel calls for its action/timer questions.

Only the final control round adds the action Choice and two hypothetical
`TIMER` questions per finalist. The
exact choices are `<50ms`, `100ms`, `200ms`, `300ms`, `400ms`, and `>500ms`, mapped
in code to 50, 100, 200, 300, 400, and 500 ms. Include action and timer questions
before sizing the final round; they may occupy different parallel calls. Only
the winning candidate/action's interval is consumed. After CUA
returns, the loop waits that interval before the next iteration's fresh control
observation in the retained window. The final permitted action has no following wait.
Windows Search skips control selection and timers entirely and starts the next
iteration after typing returns, without a settling wait.
See the
[main workflow](docs/main-workflow.md) for details.

The fixed **90,000-byte** budget has no CLI configuration. It counts the complete
SDK-prepared HTTP body, equivalent to `len(request.content)` before transport;
indented report-file sizes differ. Local sizing sends no API requests. Service
token limits still apply. Automatic rejection retries, description compaction,
and splitting a formed question are outside this implementation. Group formation
reduces the candidate count before a question is fixed. If shared state plus one
candidate's selection question or one atomic action/timer question cannot fit,
that round fails before any of its calls are dispatched. A multi-candidate round
also fails before dispatch if only singleton groups fit, because it could not
eliminate any candidates.
Actual input/output token usage and prepared body bytes are retained for every
call in the round's `api_calls` list. See
[main workflow](docs/main-workflow.md) for the workflow and timing contract.

Returned model identity, question keys, answer type, option labels, probability
shape, and usage are validated. Choice confidence and probabilities never rank
winners across groups or gate advancement.

## Observation limits

Window discovery uses `list_windows({})` until the first real window is selected.
Every real-window iteration uses `get_window_state` with
the selected record's exact `pid`/`window_id`, accessibility enabled and screenshots
disabled. The adapter validates the MCP result envelope and native structured
payload, preserving tool errors before accepting an observation.
For a real control, the selected action is `click` or `type_text`, using the selected
window's `pid`/`window_id` and the original `element_token`, or the matching
`element_index`/`snapshot_id` pair when no token is present. After a click or
confirmed or unverifiable targeted text entry, the loop waits and observes again
for the next selection when another iteration remains. Text entry preserves native readback
facts and does not add a separate Jev verification call.
The patched click operation accepts a single left activation of a snapshot target.
Coordinate, desktop, foreground-delivery, modified, multi-click, right-click, and
middle-click requests are unsupported. It does not send posted mouse messages,
SendInput mouse clicks, browser mouse events, or JavaScript DOM clicks.

Each real control selection comes from one fresh snapshot. The synthetic Windows
Search branch takes no control snapshot; its synthetic destination metadata does
not represent observed controls. It sends `hotkey({scope: "desktop", keys:
["win", "s"]})`, then `type_text({scope: "desktop", text: <prepared text>})`
through the same persistent CUA connection, without checking or confirming focus
between the two commands.
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

A historical host smoke test, before the direct-only patch, used a disposable, nonactivating
WinForms Test button. Separate `list_windows`, `get_window_state`, and `click`
calls produced exactly one button callback and no post-action driver reads;
the fixture then exited. CUA returned `effect: "unverifiable"`,
`route: "accessibility"`, and background delivery. That result confirmed this
one button's click path; it did not establish direct-only behavior for Chromium
or other controls. See the [direct-only validation record](docs/cua-driver/direct-accessibility-only.md)
for the current change. The shared installed runtime was not changed by this work.

Implementation references: [TypeSafe Python usage](https://docs.typesafe.ai/sdk/python/usage),
[Choice](https://docs.typesafe.ai/primitives/choice),
[API](https://docs.typesafe.ai/api),
[verified CUA contracts](docs/cua-driver/0.28.2/README.md), and the
[CUA Driver skill](skills/cua-driver-skill-0.28.2/SKILL.md) with its
[Windows companion](skills/cua-driver-skill-0.28.2/WINDOWS.md).
