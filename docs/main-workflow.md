# Main workflow

This is the authoritative explanation of the basic Jev-driven control loop in
`src/pipeline.py`, its selector, reporter, and CLI. [Reading run timings](timing.md)
maps the workflow to report fields. Every run selects a control, delivers its CUA
click, waits for settling, and uses a fresh observation to verify that action's
intended result. It stops after verification. Overall task completion and
selection of the next window or action are outside this increment. Selecting a
real window also brings it to the foreground.

The two central requirements are:

- **Each selection round makes exactly one Jev API call.**
- **Collect timestamps and report data in memory during the run, then generate
  and write the report once at the end.**

## Vocabulary

| Term | Meaning |
| --- | --- |
| Candidate | One observed or synthetic window/control option. During verification, a fresh control is an evidence candidate, not an action target. |
| Group | Up to 10 candidates considered together. Control and evidence questions add a `none` option; it is not an observed candidate. |
| Group question | One question selecting a suitable control/window or the strongest evidence in that group. |
| Round | Evaluate all current groups together and advance each group's selected candidate. A group that chooses `none` advances no candidate. |
| API call | One request to Jev containing all group questions for that round. |
| Stage | A larger part of the workflow, such as window selection, control acquisition, control selection, or evidence selection. |
| Iteration / pass | One observation pass: pass 1 selects and executes a control; pass 2 verifies its result using fresh controls. Each pass may contain several selection rounds. |

Use these terms consistently in documentation, reports, and explanations. For
example, say "40 groups, each with one question, sent in one API call." Do not
describe this as 40 API calls or as 40 questions about an individual candidate.

## First pass

1. Receive the user's original request. Preserve its meaning and pass the original
   request unchanged to Jev during selection.
2. Discover the available windows and construct their candidate descriptions.
3. Select a window using the rounds described below.
4. Bring the selected real window to the foreground.
5. Read that window's Windows accessibility tree and construct control candidates
   from the returned observation. Preserve the observation's identities so that
   the selected control maps back to the correct native target.
6. Narrow controls using the same rounds described below. Add one
   conditional settling-time question for each finalist to the final control
   selection request.
7. If no suitable control remains, stop without clicking. Otherwise record the
   selected control in `last_control` with status `(unverified)`, then execute its
   click and wait for CUA to return.
8. Wait the winning candidate's selected settling time, then continue with the
   second-pass verification below. At the end, generate and write the report once
   from the records collected in memory.

Window/control selection uses the original user request and observed candidate
descriptions. This workflow has no changed/done/another-window routing questions.

Start Menu offers the synthetic Search field; neither has a native execution
target. Search is judged against `none`; selecting it
returns `no_action`. Opening Start and typing are deferred.

## Shared Jev state

Every pipeline request uses the same four shared context fields, including window,
control, and evidence selection:

| Field | Meaning |
| --- | --- |
| `original_request` | The user's exact original text, without rewriting or shortening. |
| `iteration` | The current observation pass: 1 for selection/action, 2 for verification. It stays the same across all calls within a pass. |
| `current_window` | Set when window selection finishes; app/title metadata refreshes if a later window observation changes it. `null` before the first window selection or when the current window is no longer observed. |
| `last_control` | `null` before control selection. Before dispatching a click, set `{window, control, status}` using the source window, selected control description, and status `(unverified)`. Change only the status to `(verified)` when fresh evidence confirms the action's intended result. The descriptive record remains after observation clears the old executable target. |

Create one mutable state dictionary per run and update these fields at those
events. All selection rounds reuse that object. Verification adds only
`last_action` delivery/wait facts and `observation` coverage information. Build
control candidates once per observation. Changing the current window's metadata
must not rewrite the source window retained in `last_control`.

Each HTTP request still serializes its complete state and questions for Jev;
reusing the local dictionary is not a server-side state or patch protocol. Request
traces and report entries retain independent historical snapshots so later field
updates cannot alter what an earlier call actually sent.

Control and evidence candidates live in their group questions. Shared state never
contains a full previous or current control list, and no local snapshot diff is
needed. The existing selector also accepts a plain request string for direct
callers, but the application loop always supplies this context. No separate state
API or data model is introduced.

## Second pass: verify the previous action

After the selected wait, rediscover windows to refresh the current window's
identity, then capture its fresh accessibility observation. Use its controls as
evidence candidates for the intended result of the
previous action, identified by `last_control` and `last_action`. The evidence
stage uses the same groups and rounds as control selection, with a `none` option
in every group. Choosing an evidence control never clicks it.

Narrow the evidence to a final group of at most ten candidates. In that final
round, batch the group-selection question with this independent yes/no question:

> Based on these evidence candidates and the action we attempted, did the action achieve its intended result?

The confirmation question includes all final evidence candidates. It cannot
depend on the winner of its group question, since questions within one call run
independently. Record both answers and their probabilities. There is no extra
confirmation API call after the final evidence round.

| Result | Outcome |
| --- | --- |
| Current window disappeared, or its fresh control list is empty | Stop with `no_evidence`, without a Jev evidence request. An unavailable structured observation retains `observation_unavailable`. |
| All groups choose `none`, or the final group chooses `none` | Stop with `no_evidence`; keep `(unverified)`. A simultaneous yes from the confirmation question does not override no selected evidence. |
| Evidence selected, confirmation is no | Stop with `action_unverified`; keep `(unverified)`. |
| Evidence selected, confirmation is yes | Set `last_control.status` to `(verified)` and stop with `action_verified`. |

This verifies one action's intended result, not completion of the user's whole
task. There are no completion, window-switch, wait/error, additional-wait, or
automatic action-retry calls. Missing or partial observations remain limitations
of the evidence; neither a control selection nor a CUA return proves success.
Native failures and unknown delivery retain the existing error handling.

## Selection rounds: one API call per round

Apply the same grouping procedure to windows, controls, and evidence candidates:

1. Divide the current candidates into groups of up to 10, preserving their order.
2. Create exactly one group question for every group. Window questions offer only
   that group's candidates. Control and evidence questions also offer `none`, so
   a full group has ten observed candidates and eleven response options.
3. Put **all group questions for the round into one request** and call Jev once.
4. Read the selected option and complete probability distribution for every group.
5. Advance each selected candidate, preserving group order. `none` advances no
   candidate. If all groups reject their candidates, stop the stage.
6. If a round began with more than ten candidates, regroup its surviving winners
   for the next round. Control and evidence stages still need their final round
   when pruning leaves only one candidate: it must be judged against `none`.
   Finish after the final group, with its timing or verification questions.

Only window selection can accept an initial singleton without a Jev selection
question. Control and evidence singletons require a group question against
`none`. The final control round adds one settling-time question per real finalist;
the final evidence round adds one confirmation question for the whole finalist
set. A round covers all current candidates; do not turn subsets into extra
"rounds" to bypass the one-call rule.

For window/control selection, the question means:

> Which of these candidates best matches the user's original request?

For evidence selection, ask which candidate most strongly supports that the
previous action achieved its intended result. `none` means no candidate in this
group is suitable for that stage; it is not a fabricated window/control.

Final control-selection questions explicitly include the observed
app/browser identity so the original request is interpreted in its actual window
context. Keep shared state and candidate descriptions minimal while retaining
the information needed to choose. Keep the Jev model pinned to `jev-1.13.0`.

All groups within a round are independent. Their questions belong in the same
API call. The next round depends on the previous round's winners and therefore
starts after that response has been processed.

Do not split a round into multiple calls, either sequentially or concurrently.
If a complete round cannot fit the service's request limits, surface that
limitation explicitly instead of silently splitting the round or dropping
candidates. See [Restrictions and limits](#restrictions-and-limits) for the
provider limits and the application's own bounds.

### Example: 392 control candidates

| Round | Candidates entering | Groups | Group questions in the single API call | Winners leaving |
| --- | ---: | --- | ---: | ---: |
| 1 | 392 | 39 groups of 10, plus one group of 2 | 40 | 40 |
| 2 | 40 | 4 groups of 10 | 4 | 4 |
| 3 | 4 | 1 group of 4 | 1 | 1 |

Assuming every group advances a candidate, that is **three control-selection
rounds and three API calls**. Groups selecting `none` can reduce later counts or
stop selection earlier.
For the example run with 17 window candidates, window selection adds two rounds
and two calls: 17 candidates become 2 winners, then 1 winner. The complete
selection therefore uses **five API calls** across both stages.

The last control request also contains four timing questions, one
for each finalist. That request has five questions in total and is still one API
call. The later evidence stage has its own rounds and API calls, with one
confirmation question added only to its final round. Evidence rounds are distinct
from window/control-selection rounds.

## Final control selection and settling time

Only the **final** control-selection request includes a separate conditional
Choice timing question for **every finalist**, alongside the question choosing
the winning control. Intermediate narrowing rounds do not ask timing questions.
Each timing question identifies its candidate action and the observed app/browser
context explicitly. Its meaning is:

> After CUA finishes delivering this candidate action in [observed app/browser],
> how much additional time should this browser/app need for the resulting state
> to become ready for the next step?

Use identity actually present in the window/snapshot, such as the observed app
name and window title; do not guess a browser from the user's request. Keep the
original user request available as context. The premise is that **this particular
candidate's action is executed**. A timing answer cannot depend on the selection
answer in the same batch, because all questions are evaluated independently.
This follows TypeSafe's [speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
pattern. Retain all timing answers and their full probabilities, then use only
the winning candidate's timing.

Timing uses Choice, not Score. These are the exact options and code mappings:

| Choice option | Scheduled wait (milliseconds) |
| --- | ---: |
| `<50ms` | 50 |
| `100ms` | 100 |
| `200ms` | 200 |
| `300ms` | 300 |
| `400ms` | 400 |
| `>500ms` | 500 |

The endpoint mapping is deliberate: `>500ms` schedules the observation after
500 ms and does not assert that the app is ready. This increment has no
additional-wait branch. Code performs this mapping and all elapsed-time
arithmetic; Jev does not calculate a delay or date.

The ordering is strictly sequential:

```text
Final control selection + every finalist's conditional timing (one Jev call)
Choose winning control and its timing
Record last_control with status (unverified)
START native action duration
    Call CUA and wait for its action result
END native action duration
START additional app/browser settling wait
    Sleep the selected 50 / 100 / 200 / 300 / 400 / 500 ms
END settling wait
Capture fresh state
Narrow fresh evidence in rounds, with none in each group
Final evidence round + action confirmation (one Jev call)
Mark last_control (verified) only when evidence is selected and confirmation is yes
Stop and report the per-action result
```

The settling timer starts **after the CUA call returns**, never at dispatch.
It does not include CUA execution duration. Its value estimates additional app
settling time, not how long CUA needs to deliver the action. A new snapshot is
captured only after the wait; concurrent action/snapshot handling is unnecessary.

## Running the loop

From the repository root, with the configured TypeSafe credentials and CUA daemon:

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.'
```

The same existing Python API is `run_pipeline(request, driver, selector)`.
It always executes the loop; callers can supply `sleep=` for deterministic
testing. See the [README](../README.md#run) for setup and
client construction, and [report timing](timing.md) for report hooks.

The current scope is one action and its verification, recorded as passes 1 and 2.
It stops after `action_verified` (CLI exit 0), `action_unverified` or `no_evidence`
(exit 2). Initial control rejection returns `no_control_selected` (exit 2), while
an empty control observation retains `no_controls`. Existing observation,
synthetic-target, and native error outcomes remain available. This is a workflow
boundary, not a configurable maximum-iteration setting, and `action_verified`
does not assert overall task completion.

Accessibility coverage and model judgments remain limited by their observed
evidence. Tests use fake Jev/CUA and a fake clock/sleeper; they do not demonstrate
success on arbitrary live desktop tasks.

## Restrictions and limits

The following are application choices, configured bounds, and implementation
gaps. They are separate from Jev's provider limits.

| Application restriction | Current behavior | Where it is controlled |
| --- | --- | --- |
| Accessibility observation size | Request at most **5,000 elements**, with maximum depth **25**, by default. A bounded observation does not establish full UI coverage. | CLI `--max-elements` and `--max-depth`; also `CuaDriver` constructor arguments. Both must be positive integers. |
| Candidate description shortening | App/title and control label/value/value-description text is shortened to **240 characters**. Include at most **two explicitly indexed ancestors**, with labels shortened to **120 characters**. Original request and source records remain intact. | `_text`, `window_candidates`, and `control_candidates` in `src/pipeline.py`; no CLI setting. |
| Jev HTTP timeout and retries | **30-second timeout configuration**, with **zero automatic retries**. This is HTTP timeout configuration, not a total run deadline. | Client construction in `src/main.py`; no CLI setting. |
| HTTP connection lifetime | One HTTP client is reused throughout a CLI run, then closed. Separate CLI runs start a new client. The installed HTTP library defaults to **five seconds of idle connection retention**, so the same client can need a new connection after a longer idle period. | Client lifetime in `src/main.py`; inherited `httpx2` connection-pool defaults; no CLI setting. |
| CUA child timeout | **60 seconds per CLI child call**, by default. | `CuaDriver(timeout=...)`; no CLI setting. |
| Selection grouping and batching | Up to **ten candidates per group**; control/evidence questions also include `none` for at most eleven options. Exactly **one API call per selection round** contains all group questions plus any final-round timing/confirmation questions. | `GROUP_SIZE` and selection flow in `src/selection.py`; required by the workflow. |
| Settling-time choices | Only **50, 100, 200, 300, 400, or 500 ms** can be scheduled after the click. `<50ms` maps to 50 and `>500ms` maps to 500. This increment does not add another wait after verification fails. | `SETTLING_MS` in `src/selection.py`; no CLI setting. |
| Model pin | Require **`jev-1.13.0`**, including the model identity returned by the service. | `PINNED_MODEL` and selector validation; CLI verifies the project configuration matches. This is an explicit project requirement. |
| Available actions | Clicks and foreground activation are wired. Typing, scrolling, keyboard shortcuts, and Start Menu execution are not implemented. Synthetic Start Menu/Search targets return `no_action`. | Existing pipeline/driver integration; these are capability gaps, not Jev limits. |
| Continuation and recovery | Stop after verifying one action. No overall-completion/window-switch routing, additional waits, automatic action retry, or coordinate fallback; no general planner, durable memory, advanced loading detection, or recovery framework. | Current pipeline scope. |

The application also validates identities and response structure before using
them. These checks are safeguards, not confidence thresholds:

| Validation | Current behavior |
| --- | --- |
| Observation and target identity | Control selection needs structured indexed controls and a snapshot identity. Clear old targets before reading again. Native clicks require the observed window identity and either an element token or an element index with its snapshot ID. Stale-target failures are surfaced without retry. |
| Jev answer structure | Require exactly the requested question keys, Choice answers, known option labels, and matching probability labels. Probabilities and confidence must be finite numbers from 0 to 1; each question's probability total must be **0.97–1.03 inclusive**. Token usage must contain nonnegative integer counts. Invalid responses stop the run. |
| Confidence/probability gating | **None.** Structurally valid answers are accepted without a minimum confidence or winning-probability threshold; the application uses Jev's returned choice. |

There is **no local token cap, request-byte cap, configurable maximum iteration
count, or total runtime cap**. The workflow currently stops after one action's
verification and offers only the one settling wait described above. The former
**48,000-byte packing heuristic was removed**. The application neither estimates
tokens from bytes nor splits a selection round to fit a locally invented limit.

Jev itself limits a request to **64k tokens for shared state plus all questions**
and **32k tokens for shared state plus the longest question**. Those are service
limits, not restrictions added by this application; a request can fail either
check. They apply to control and evidence selection alike.
See the [TypeSafe model documentation](https://docs.typesafe.ai/models).

Verification distributes fresh control descriptions across evidence group
questions; shared state contains neither a previous nor current full control
list. The observation-size and text-shortening bounds above still do **not**
guarantee that every round fits Jev's limits. There is no local token-budget check
or arbitrary byte cutoff before sending it.

## Timing boundaries

Use one high-resolution monotonic clock, such as `perf_counter_ns()`, for elapsed
time. Capture the actual start and finish of each operation and calculate the
difference. Report durations in milliseconds. UTC timestamps provide date/time
context; they are not the clock used to calculate durations.

### Jev API call duration

This is the primary measurement of how long we wait for a Jev API call:

```text
Build groups and questions
Prepare and encode the complete HTTP request
    START API TIMER: immediately before handing the prepared request to transport
    Send request -> Jev processes request -> receive complete response body
    STOP API TIMER: immediately after the full response body has been received
Decode and validate the response; advance group winners
```

This duration includes client transport work, connection setup when needed,
network travel, service waiting/processing, and response transfer. It excludes
constructing groups, encoding the request, decoding/validating the response after
receipt, and generating or writing the report. Receiving response headers alone
does not end the timer.

Do not perform report formatting, full-history copying, or file I/O between these
boundaries. Capture the end timestamp before processing the received body for
reporting. Any necessary in-memory recording must remain lightweight; timing
instrumentation is not assumed to have literally zero cost.

This is **client-observed API call duration**, not isolated model inference time.
Only server-provided timing could separate Jev's internal processing from network
and other service overhead. If such timing becomes available, label it separately.
Do not infer it by subtracting unrelated stage durations.

All group questions in a round share this one API call duration. There is no
measured duration for each individual group question. Do not divide the call time
by the number of questions and present that as per-question latency.

### Other durations to record

| Measurement | Start | Finish |
| --- | --- | --- |
| Startup and initialization | Earliest recorded application boundary | Configuration, request, driver, and API client are ready |
| Window discovery | Before calling the window-discovery adapter | Discovery call returns |
| Candidate preparation | Before constructing candidates from the observation | Candidate descriptions are ready |
| Window selection | Before the first window round | Final window winner is available |
| Foreground activation | Before calling the activation adapter | Activation call returns |
| Control acquisition | Before calling the accessibility-observation adapter | Observation call returns |
| Control selection | Before the first control round | Final control winner is available |
| Evidence selection | Before the first evidence round | Final evidence selection and action confirmation are available, or no evidence remains |
| Each selection round | Before forming that round's groups/questions | Its single API response has been processed and winners are ready |
| Request preparation | Before building/encoding the round's API request | Complete request is ready for transport |
| Jev API call | Immediately before transport dispatch | Complete response body received |
| Response processing | After response receipt, before decoding/validation | Validated choices and advancing winners are ready |
| Action preparation | Before mapping the selected control to native arguments | Arguments are ready for dispatch |
| Native action | Immediately before invoking the action adapter | Adapter returns the action result |
| Settling wait | After the native action returns | The selected sleep returns, before fresh observation |
| Total operational run | Earliest recorded application boundary | Operational result and normal client cleanup are complete, before report generation |
| Report generation and writing | After the operational run, before constructing the final JSON | The single final report write completes |

The report must state its timing origin. The current earliest application boundary
is after importing the clock in the Python entry module; it excludes process
launch, interpreter initialization, and work before that boundary. An invocation
of an already-imported function must identify its different origin.

Record time to first Jev API call from that same origin to the first actual
transport-dispatch boundary. Record the total operational run directly from its
start and finish; do not reconstruct it by adding nested durations.

A round includes its request preparation, API call, and response processing. A
selection stage includes its rounds. These durations overlap: do not add parents
and children together. Optional SDK-call timings, if retained, must be labeled
as SDK time and must not replace the API measurement defined above.

Native action timing ends when the adapter returns. It does not measure when the
screen finishes changing or when the target application is ready for another
action. Stages that do not occur should be marked as skipped or absent, not as
completed work with a fabricated zero duration.

## Collect during the run; create the report at the end

During execution, retain timestamps and the data needed for the report in memory.
Preserve event data against later mutation without repeatedly copying or parsing
the accumulated history. Retain actual request/response payloads efficiently so
they can be included in the final report.

There must be no per-event report-file writes, repeated serialization of the
entire history, JSON round-trips of the entire history, or per-event flush/fsync
and file replacement. This applies inside HTTP hooks as well as stage timers.

After the operational end timestamp is captured, construct the report from the
collected records, serialize it, and write it once. Reuse the existing report
format and correlation identities rather than adding a parallel reporting API
or near-identical data model.

The completed report must make it easy to see:

- The original request, pinned model, final window/control selection, and action
  result where an action ran.
- Total operational duration, time to first API call, and each stage's duration.
- Every round's candidate count, group count, whole-round duration, and single
  API call duration.
- Every group's candidates, exact question, returned probabilities, selected
  winner, and separately reported confidence where available.
- Every control finalist's timing question and answer, the consumed winning
  timing, iteration number, route, and actual settling-wait duration.
- Evidence rounds, including `none` probabilities, the final confirmation question
  and answer, and the `action_verification` result with `last_control` status.
- The recorded start/finish boundaries connecting these durations and events.

Selection round names can repeat in later passes. Correlate them using
`(iteration, round_id)` and span/request identities, never `round_id` alone.

Report generation and writing happen after the operational run and must be
measured separately. Their complete duration is only known after the file write
finishes; it can be returned through the existing final result/stdout. Do not
rewrite the report to insert the duration of its own completed write. A complete
through-report duration can likewise be reported there with its boundary stated.

An abrupt process termination can lose the in-memory records. This is a tradeoff
of end-of-run reporting, not a reason to reintroduce synchronous disk writes into
the measured workflow. This document does not request additional failure-handling
or recovery mechanisms.
