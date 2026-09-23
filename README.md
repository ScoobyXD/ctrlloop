# ctrlloop

The Python pipeline offers discovered desktop windows plus a synthetic
**Windows Search** option. Jev chooses a window; a real window is inspected through
Windows UI Automation, then its observed controls are offered for selection.
For a real window, Jev chooses the control and whether to click or enter text.
The pipeline dispatches that native CUA action, waits for the app to settle,
and starts the next selection from fresh observations.
Choosing Windows Search instead sends desktop **Win+S**, then directly types the
query resolved before activation. An explicit `--text` supplies the exact Search
query; otherwise Jev selects words from the original request in one batched call.
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
existing snapshot-bound target and CUA's default background delivery. The loop
uses the same generic question for every window and control selection round;
every group selects a supplied candidate. It does not add automatic click escalation,
coordinate fallback, additional waits, or action retries. Targeted text entry
uses CUA's existing `type_text` operation. Its pinned Windows UIA path appends to
the existing value; it does not implement replacement or caret insertion.

The synthetic Windows Search opener is available when fresh discovery has no
visible `SearchHost.exe` window titled `Search`. While that destination is open,
only its real window is offered, preserving its observed order and native identity
for control selection. Closing Search makes the opener available again.
If the opener is selected immediately after the same query was typed and Search
was not observed, the run stops with `observation_unavailable` instead of sending
Win+S and typing again. An intervening control click or a new run permits a later
search; there is no permanent one-shot flag. Driver errors remain errors.

The Windows Search window record has `synthetic: true` and no invented PID,
window handle, control, geometry, or snapshot identity. It has no control
candidates: selecting it executes the two native desktop commands directly.
Without an explicit Search query, word selection happens before either native
command. An empty or noncontiguous selection returns `no_action` without typing.

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
use the same selection rounds, with up to 400 control candidates per API call. A definition with no controls returns
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
uv run --locked python src/main.py 'Open Slack and search for budget review'
uv run --locked python src/main.py 'Open Chrome using Windows Search.' --text 'Chrome'
```

Pass `--text` (or `run_pipeline(..., text=...)`) to supply the exact Windows Search
query. The string is preserved, including leading/trailing whitespace and line
endings; explicitly empty or whitespace-only values are rejected. This override
applies only to Windows Search and does not force Jev to select it. Other typing
actions, and Search without the override, use words selected from the original
request. No generative model rewrites or supplies missing text.

Each iteration executes one selected control click/text action or the Windows Search
activation-and-typing sequence. The next iteration discovers
windows and controls again and selects the next action using the original request
and previous action context. `--max-iterations` is a positive integer, default 10.
At the limit, the run returns `iteration_limit` without a further wait or observation.
An unsupported or unavailable observation, unavailable synthetic action,
unresolved text, a disabled text target, a native/API error, or user interruption
can stop the run earlier.

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
The CUA MCP client process receives `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, so the
adapter keeps telemetry off even if the saved setting or parent environment
enables it. This avoids the Windows telemetry worker delaying client shutdown.

At connection startup (and reconnect), the client sets its session cursor's
`glide_duration_ms` to **10 ms** with CUA's `set_agent_cursor_motion`. The cursor
stays visible. Windows CUA 0.28.2 otherwise uses a distance-dependent animation
and waits for the renderer before clicking, which can make later clicks take
seconds even when the first click is fast. The setting affects this connection
only; 10 ms bounds the glide wait, not the complete native action. Setting it
to zero would restore the slower default. Setup shares the first call's timeout
and must be acknowledged before the requested call is sent.

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
- [`jev_questions.py`](src/jev_questions.py) holds the authored selection, action, timer, and word-inclusion questions; edit the wording here.
- `selection.py` batches group questions into Jev calls capped at 400 control candidates, then advances the round's selected candidates together.
- `synthetic.py` defines synthetic windows, their controls, and duplicate-matching rules.
- `pipeline.py` repeats window selection and real control click/text actions or direct Windows Search actions within the iteration limit.
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
| `no_action` | Text selection empty/noncontiguous, text target disabled, or custom synthetic control lacks live CUA identifiers | 2 |
| `no_windows` | No first-stage candidate selected; normally unreachable with the Windows Search option | 2 |
| `no_controls` | A valid read returned no indexed controls, or a synthetic window defines no controls | 2 |
| `observation_degraded` | A degraded read returned no indexed controls | 2 |
| `observation_unavailable` | Structured controls/snapshot identity unavailable, repeated unobserved Search query, or targeted typing unconfirmed | 2 |
| `error` | Driver, configuration, or Jev request/response failure | 1 |

Nonempty degraded observations remain selectable and retain their quality flags.
Selecting a control and receiving its action result do not establish its success.
Inspect `action_result.effect`, `route`, and any `delivery`, `evidence`, or `escalation`.
For clicks, effects such as `unverifiable` and `suspected_noop` remain action facts, followed
by fresh observation and next-action selection when another iteration remains;
native refusals and failures return `error`/exit 1.
A targeted text action continues only after CUA reports a confirmed effect.
Otherwise the run stops with `observation_unavailable`, retaining the native facts
without automatically typing the same text again. This does not assess task
completion. Windows Search retains its desktop handoff and fresh-observation flow.
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
- `request_words_prepared`: ordered source words and their original character
  offsets, prepared once before the first loop.
- `windows_discovered`: the complete observed window list, in discovery order;
  it does not include the synthetic Windows Search option.
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
  completed selection rounds, and observation quality. A singleton window needs
  no selection question; a singleton control receives the selection question, with action and timing
  when executable. Windows Search is marked `synthetic: true` in
  `window_selected`; it has no `control_selected` or `window_observed` event.
- `text_selected`: destination, exact resolved substring or unresolved result,
  and the word-selection trace. Every Noul probability is retained with its API call.
- `action_requested` and `action_returned`: native activation and input arguments
  and returned action facts. Windows Search records `hotkey` and `type_text`
  separately under the same iteration, including the exact resolved text. Unresolved
  text or an unavailable custom synthetic action records `action_skipped`
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
questions in each batch share one observed API call duration; native action return
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
contains at most ten candidates and receives one Jev Choice question using
`QUESTION`. Each group selects one supplied candidate, which advances in group
order. Regroup survivors in tens for the next round.

Only a singleton window bypasses selection. A singleton control still receives
the selection question, with action and timers when executable. For 1,000 control candidates,
the round counts are `1000 -> 100 -> 10 -> 1`: three calls, then one, then one,
for five control calls in total. Group size ten is fixed.

Authored questions `QUESTION`, `ACTION`, `TIMER`, and `TEXT_WORD` live in
[`src/jev_questions.py`](src/jev_questions.py). `QUESTION` is reused for all window
and control narrowing rounds and final selections. The final control call also
includes an `ACTION` click-versus-`type_text` Choice
for every finalist and a separate hypothetical `TIMER` for each action per
finalist. Consume only the winning control's action and matching timer.

The original request is split once into whitespace-delimited words with original
character offsets. When text is needed, one further API call asks one Noul per
word, including all words together and using the selected destination as context.
Words whose inclusion probability exceeds 0.5 are included. The chosen positions
must form one nonempty continuous phrase; code copies its exact original substring,
preserving punctuation and internal whitespace. Empty or scattered selections
stop the action. A name is not converted into a URL, and text absent from the
request cannot be generated. All Noul answers and probabilities remain in the
existing selection/reporting traces.

Every loop request carries the unchanged `original_request`, current `iteration`,
fresh `current_window` identity and `last_control`. The window is null before
selection or when missing; the last control is null until control action dispatch.
Before dispatch, set `last_control` to `{window, control, action}` with its source
window, descriptive control fields, and chosen tool. After CUA returns, `last_action` retains
the tool and filtered native result facts for the next selection. The Windows
Search route has no selected control; its final `last_action` records `type_text`,
the exact text, and native result facts.
Text resolution still sees the previous action/control history; the pending
control replaces `last_control` only immediately before its native dispatch.

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

**Control selection sends at most 400 candidates per API call, in groups of 10.**
Larger rounds send consecutive batches in observed order: the first 400 controls,
then the next 400, then any remainder. Window rounds use one call. The next round
starts after all batch responses are processed, because it needs every group's
winner. For 392 control candidates, the rounds are
`392 -> 40 -> 4 -> 1`: 40 group questions in one call, then four in one call,
then one in one call, for three control calls in total.

For 631 controls, round 1 sends 400 candidates in 40 groups, then 231 in 24 groups.
The 64 group winners combine for round 2; the rounds are `631 -> 64 -> 7 -> 1`,
using two calls, then one, then one: four control calls in total. No candidate
is dropped to meet the cap.

Only the last of those control calls adds the action Choice and two hypothetical
`TIMER` questions per finalist. The
exact choices are `<50ms`, `100ms`, `200ms`, `300ms`, `400ms`, and `>500ms`, mapped
in code to 50, 100, 200, 300, 400, and 500 ms. All timers share the final
selection API call; only the winning candidate/action's interval is consumed. After CUA
returns, the loop waits that interval before the next iteration's fresh window
and control observations. The final permitted action has no following wait.
Windows Search skips control selection and timers entirely and starts the next
iteration after typing returns, without a settling wait.
See the
[main workflow](docs/main-workflow.md) for details.

There is no byte-based request splitting or request-budget CLI configuration.
Actual service limits still apply; the 400-candidate cap is not a token guarantee.
An oversized batch fails explicitly without automatic smaller retries or removing
candidates. Actual input/output token usage is retained for every call in the
round's `api_calls` list. See
[main workflow](docs/main-workflow.md) for the workflow and timing contract.

Returned model identity, question keys, answer type, option labels, probability
shape, and usage are validated. Choice confidence and probabilities never rank
winners across groups or gate advancement. The word-inclusion Nouls use their
probabilities for inclusion decisions; they do not alter control elimination.

## Observation limits

Observation calls use `list_windows({})` and `get_window_state` with
the selected record's exact `pid`/`window_id`, accessibility enabled and screenshots
disabled. The adapter validates the MCP result envelope and native structured
payload, preserving tool errors before accepting an observation.
For a real control, the selected action is `click` or `type_text`, using the selected
window's `pid`/`window_id` and the original `element_token`, or the matching
`element_index`/`snapshot_id` pair when no token is present. After a click or
confirmed targeted text entry, the loop waits and observes again for the next
selection when another iteration remains. Text entry preserves native readback
facts and does not add a separate Jev verification call.

Each real control selection comes from one fresh snapshot. The synthetic Windows
Search branch takes no control snapshot; its synthetic destination metadata does
not represent observed controls. It sends `hotkey({scope: "desktop", keys:
["win", "s"]})`, then `type_text({scope: "desktop", text: <resolved text>})`
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
