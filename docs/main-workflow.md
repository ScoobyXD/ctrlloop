# Main workflow

This document defines the workflow and timing behavior implemented by the selector,
reporter, and CLI. [Reading run timings](timing.md) maps these requirements to the
report fields. The selector sends one complete request per round, and the reporter
buffers observations until the operational run ends.

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

Use these terms consistently in documentation, reports, and explanations. For
example, say "40 groups, each with one question, sent in one API call." Do not
describe this as 40 API calls or as 40 questions about an individual candidate.

## Main sequence

1. Receive the user's original request. Preserve its meaning and pass the original
   request unchanged to Jev during selection.
2. Discover the available windows and construct their candidate descriptions.
3. Select a window using the rounds described below.
4. Bring the selected real window to the foreground.
5. Read that window's Windows accessibility tree and construct control candidates
   from the returned observation. Preserve the observation's identities so that
   the selected control maps back to the correct native target.
6. Select a control using the same rounds described below.
7. Map the selected control to the existing CUA action. In execution mode, invoke
   the action and record its returned result. In selection-only mode, return the
   selected control without invoking it.
8. Finish the operational run, then generate and write the report from the records
   collected in memory.

Existing synthetic destinations retain their existing behavior; they are not
invented live windows or controls. This specification changes selection request
organization and reporting, not the supported actions.

The current scope ends after selection or the existing single action returns.
It does not introduce a repeated action loop, polling, additional Jev questions,
50/100/150 ms estimates, adaptive waits, or new retry/recovery behavior. An action
return is not proof that the UI changed or the user's task completed. A future
observe-and-repeat workflow needs its own design.

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

If the entire selection starts with only one candidate, that candidate is already
the winner and no selection round is needed. A round otherwise covers all current
candidates; do not turn subsets into extra "rounds" to bypass the one-call rule.

The question means:

> Which of these candidates best matches the user's original request?

The existing equivalent question wording can remain. There are no additional
questions per candidate and no time-estimate questions. Keep the shared state and
candidate descriptions minimal while retaining the information needed to choose.
Keep the Jev model pinned to `jev-1.13.0`.

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
- The recorded start/finish boundaries connecting these durations and events.

Report generation and writing happen after the operational run and must be
measured separately. Their complete duration is only known after the file write
finishes; it can be returned through the existing final result/stdout. Do not
rewrite the report to insert the duration of its own completed write. A complete
through-report duration can likewise be reported there with its boundary stated.

An abrupt process termination can lose the in-memory records. This is a tradeoff
of end-of-run reporting, not a reason to reintroduce synchronous disk writes into
the measured workflow. This document does not request additional failure-handling
or recovery mechanisms.
