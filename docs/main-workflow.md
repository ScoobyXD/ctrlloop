# Main workflow

This is the authoritative explanation of the basic Jev-driven control loop in
`src/pipeline.py`, its selector, reporter, and CLI. [Reading run timings](timing.md)
maps the workflow to report fields. Every run executes the loop: select a control,
deliver its CUA click, wait for settling, observe, and judge the next step. The loop
has no iteration cap. Selecting a real window also brings it to the foreground.

The two central requirements are:

- **Each selection round makes exactly one Jev API call.**
- **Collect timestamps and report data in memory during the run, then generate
  and write the report once at the end.**

## Vocabulary

| Term | Meaning |
| --- | --- |
| Candidate | One available option: a window during window selection, or a control during control selection. |
| Group | Up to 10 candidates considered together. |
| Group question | One question asking which candidate in that group best matches the original user request. |
| Round | Evaluate the current groups together, select one winner per group, and advance those winners. |
| API call | One request to Jev containing all group questions for that round. |
| Stage | A larger part of the workflow, such as window selection, control acquisition, or control selection. |
| Iteration / pass | One observation pass in the loop; it may contain several selection rounds, or wait without selecting or executing another control. |

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
7. Execute the winning control's click, wait for CUA to return, then wait the winning
   candidate's selected settling time and capture fresh state.
8. Continue with the later-pass questions and routing below. At the end, generate
   and write the report once from the records collected in memory.

**Do not ask the three loop questions on this first pass.** There is no previous
action and observation pair to compare yet. Window/control selection runs using
the original user request and the observed candidate descriptions.

Existing synthetic destinations retain their existing behavior. Start Menu offers
the synthetic Search field; neither has a native execution target. Selecting it
returns `no_action`. Opening Start and typing are deferred.

## Second and subsequent passes

After the selected wait has finished, discover the available windows and observe
the current window again. Supply Jev the original goal, concise previous and
current observations, and the relevant last-action facts. The comparison must
include both observations; a current snapshot alone cannot establish change.
Retain only this local comparison context, without a new memory framework.

Ask these three independent yes/no Choice questions in **one Jev API call**:

1. Did anything meaningfully change?
2. Are we done with the user's requested task?
3. Do we need another window to continue?

The questions judge the observed result in the context of the goal. Code then
routes the answers in this order:

| Result | Next step |
| --- | --- |
| Done | Stop and return the result and report, regardless of the other answers. |
| Unfinished, changed, another window needed | Repeat window selection using the fresh discovery, bring the selected window forward, capture its controls, and select a control. |
| Unfinished, changed, current window appropriate | Reuse the current window and select from its freshly observed controls. |
| Unfinished, unchanged | Ask whether to wait longer or report an error, as below. |

Fresh control candidates always come from the current snapshot. Never replay an
old snapshot's target. Each later control selection uses the final-round timing
batch before executing another click. A normal CUA response alone never marks the
user's goal complete. Native failures and unknown delivery keep the existing
error/uncertainty handling; this loop does not add automatic action retries.

### Unchanged state: wait longer or error

Send a Choice question with `wait` and `error` options, plus an independent
conditional additional-wait Choice question, in one request. The timing question
assumes that waiting is chosen and uses the same six options below. Retain both
answers, then consume the timing only for `wait`.

For `wait`, sleep for the selected interval and capture another fresh observation,
then ask the same three loop questions. **Do not repeat the previous action.**
For `error`, stop and report that no meaningful progress was observed. Repeated
waits continue through the same observation and judgment steps.

The last-action context includes code-computed `requested_wait_ms_so_far`, the
cumulative scheduled settling time already allowed after that action. It
includes additional waits, excludes CUA duration, and is not a measurement of
actual elapsed wall time. The measured wait durations remain in the report.

## Selection rounds: one API call per round

Apply the same procedure to window candidates and control candidates:

1. Divide the current candidates into groups of up to 10, preserving their order.
2. Create exactly one group question for every group. Its options are that group's
   candidates.
3. Put **all group questions for the round into one request** and call Jev once.
4. Read the selected candidate and probability distribution for every group.
5. Advance one winner from each group, preserving group order.
6. If multiple candidates remain, regroup the winners and start the next round.
   If only one remains, selection is complete; do not start another round or call.

If the entire selection starts with only one candidate, it is already the winner
and needs no selection question. An executable singleton still needs
its conditional timing question through the same selector. Its trace contains
one timing-only round and a deterministic group with `question_key: null`; no
redundant selection question is invented. A round otherwise
covers all current candidates; do not turn subsets into extra "rounds" to bypass
the one-call rule.

The question means:

> Which of these candidates best matches the user's original request?

Final control-selection questions explicitly include the observed
app/browser identity so the original request is interpreted in its actual window
context. Keep shared state and candidate descriptions minimal while retaining
the information needed to choose. Keep the Jev model pinned to `jev-1.13.0`.

All groups within a round are independent. Their questions belong in the same
API call. The next round depends on the previous round's winners and therefore
starts after that response has been processed.

Do not split a round into multiple calls, either sequentially or concurrently.
The former 48,000-byte packing heuristic has been removed. Actual service request
limits still apply: if a complete round cannot
fit, surface that limitation explicitly instead of silently splitting the round
or dropping candidates. Jev's published limits are 64k tokens for the state plus
all questions and 32k for the state plus the longest question. The service checks
the submitted request; the application does not invent a byte-to-token conversion.
See the [TypeSafe model documentation](https://docs.typesafe.ai/models).

### Example: 392 control candidates

| Round | Candidates entering | Groups | Group questions in the single API call | Winners leaving |
| --- | ---: | --- | ---: | ---: |
| 1 | 392 | 39 groups of 10, plus one group of 2 | 40 | 40 |
| 2 | 40 | 4 groups of 10 | 4 | 4 |
| 3 | 4 | 1 group of 4 | 1 | 1 |

That is **three control-selection rounds and three API calls**, not five calls.
For the example run with 17 window candidates, window selection adds two rounds
and two calls: 17 candidates become 2 winners, then 1 winner. The complete
selection therefore uses **five API calls** across both stages.

The last control request also contains four timing questions, one
for each finalist. That request has five questions in total and is still one API
call. The three later-pass judgments and any wait/error judgment are separate
batched calls; they are not additional window/control selection rounds.

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

The endpoint mapping is deliberate: `>500ms` schedules a first observation after
500 ms and does not assert that the app is ready. The unchanged-state branch can
wait again. Code performs this mapping and all elapsed-time arithmetic; Jev does
not calculate a delay or date.

The ordering is strictly sequential:

```text
Final control selection + every finalist's conditional timing (one Jev call)
Choose winning control and its timing
START native action duration
    Call CUA and wait for its action result
END native action duration
START additional app/browser settling wait
    Sleep the selected 50 / 100 / 200 / 300 / 400 / 500 ms
END settling wait
Capture fresh state
Ask the three later-pass questions together
```

The settling timer starts **after the CUA call returns**, never at dispatch.
It does not include CUA execution duration. Its value estimates additional app
settling time, not how long CUA needs to deliver the action. A new snapshot is
captured only after the wait; concurrent action/snapshot handling is unnecessary.

## Running the loop

From the repository root, with the configured TypeSafe credentials and CUA daemon:

```powershell
uv run --locked python src/main.py 'Complete the click-only task in my test app.'
```

The same existing Python API is `run_pipeline(request, driver, selector)`.
It always executes the loop; callers can supply `sleep=` for deterministic
testing. See the [README](../README.md#run) for setup and
client construction, and [report timing](timing.md) for report hooks.

There is **no maximum iteration count**. Each pass records its iteration number
and continues until Jev judges the task complete, an error or unsupported/unavailable
observation stops progress, or the user interrupts the run. `completed` means Jev
judged the goal complete; `unchanged_error` means the unchanged-state question
chose error. Existing observation, synthetic-target, and native error outcomes
remain available.

This increment supports the existing clicks and foreground activation. It does
not add typing, scrolling, keyboard shortcuts, Start Menu execution, action
retries, coordinate fallback, a general planner, durable memory, advanced loading
detection, or a recovery framework. Accessibility coverage and model judgments
remain limited by their observed evidence. Tests use fake Jev/CUA and a fake
clock/sleeper; they do not demonstrate success on arbitrary live desktop tasks.

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
| Each selection round | Before forming that round's groups/questions | Its single API response has been processed and winners are ready |
| Request preparation | Before building/encoding the round's API request | Complete request is ready for transport |
| Jev API call | Immediately before transport dispatch | Complete response body received |
| Response processing | After response receipt, before decoding/validation | Validated choices and advancing winners are ready |
| Action preparation | Before mapping the selected control to native arguments | Arguments are ready for dispatch |
| Native action | Immediately before invoking the action adapter | Adapter returns the action result |
| Settling wait | After the native action returns, or a wait-longer answer is consumed | The selected sleep returns, before fresh observation |
| Later-pass judgment | Before preparing the batched three questions or wait/error questions | Validated answers are available for routing |
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
- Every finalist's timing question and answer, the consumed winning timing,
  loop judgments, iteration number, route, and actual settling-wait duration.
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
