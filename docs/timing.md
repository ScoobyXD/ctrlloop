# Reading run timings

The [main workflow](main-workflow.md) defines the selection and timing contract.
A candidate is one window or control; fresh controls also serve as verification
evidence. A group contains up to ten candidates. A control/evidence group question
adds `none`, allowing the group to advance no candidate. **Every selection round sends
all its group questions in exactly one Jev API call.** The final
control round's request also includes a conditional settling-time question for
every finalist. The final evidence round batches one independent action
confirmation question with its group question. The next round uses the previous
round's surviving candidates. An iteration is one observation pass, not a
selection round: pass 1 selects/acts, pass 2 verifies and stops.

The CLI collects events, timestamps, and actual Jev payloads in memory. At the end
it generates and writes the existing `reports/run-*.json` event array once. Use
the path printed to stderr after the run finishes. The result on stdout retains
selection and native action facts and adds completed report-generation timing.

## Clocks and boundaries

Elapsed times use Python's high-resolution monotonic `perf_counter_ns()` clock
and are reported in milliseconds. UTC `timestamp` values use one wall-clock
anchor plus monotonic elapsed time for correlation with external logs. Wall-clock
adjustments do not affect durations.

The CLI's earliest recorded boundary is immediately after importing `time` in
`src/main.py`. It excludes process creation, interpreter initialization, that
initial import, and anything before the boundary. Startup events are recorded
retrospectively once the reporter is available; an earlier import failure cannot
produce a report. `--help` and argument-parse errors also exit before reporting.

The `timing_origin` event and terminal event's `timing.origin` identify the origin:

| Origin | Meaning |
| --- | --- |
| `main_module_after_time_import` | CLI execution through `python src/main.py` or `python -m src.main`. |
| `main_call` | A call to an already imported `main()`; earlier imports are outside this run. |
| `report_construction` | A Python API caller constructing `RunReport` without an earlier origin. |

Every event has `elapsed_ms` from the recorded origin. Paired `timing_started`
and `timing_finished` events describe operations:

| Field | Meaning |
| --- | --- |
| `operation` | Local operation being measured. |
| `span_id`, `parent_span_id` | Join start/finish records and identify enclosing operations. |
| `duration_ms` | Monotonic work duration, on the finish record. |
| `started_elapsed_ms`, `finished_elapsed_ms` | Work boundaries relative to the origin. |
| `started_at`, `finished_at` | Corresponding anchored UTC work boundaries. |
| `status` | `ok`, `error`, or `interrupted`; failures retain `error_type`. |
| `iteration`, `stage`, `round`, `round_id`, `question_keys` | Loop, selection, and HTTP correlation where applicable. |

Use `duration_ms` or the explicit work boundaries, rather than subtracting event
creation timestamps. A start event can be recorded before its measured work
begins. Intervals marked `recorded_after_completion: true` were recorded after
both boundaries were captured, as with startup and client cleanup.

Nested operations overlap. A selection includes its rounds; a round includes
forming groups, preparing its request, waiting for the API call, and processing
its response. Do not add parent and child durations to reconstruct the total.

## Following a run

Filter measured operations in PowerShell:

```powershell
$events = Get-Content -Raw -LiteralPath 'reports/run-REPLACE-WITH-YOUR-FILE.json' | ConvertFrom-Json
$events | Where-Object event -eq 'timing_finished' |
    Select-Object operation, iteration, stage, round, duration_ms, span_id, parent_span_id |
    Format-Table -AutoSize
```

`startup.imports`, `startup.cli`, and `startup.report_initialization` cover the
recorded startup work. `initialization.request_read`,
`initialization.configuration`, `initialization.driver`,
`initialization.http_client`, `initialization.typesafe_client`, and
`initialization.selector` cover setup before `pipeline`. Within the pipeline:

| Operation | Measured work |
| --- | --- |
| `window_discovery` | `driver.list_windows()` through return/error. |
| `candidate_preparation` | Building window or control candidate descriptions. |
| `window_selection`, `control_selection` | Complete window/control selection, including final control timing questions where applicable. |
| `action_verification` | Complete evidence selection under stage `evidence`, including the final confirmation when candidates reach that round. |
| `synthetic_resolution` | Looking up controls for a synthetic selection. |
| `foreground_preparation` | Preparing activation arguments and recording `action_requested`. |
| `foreground_activation` | `driver.bring_to_front()` through return/error. |
| `control_acquisition` | `driver.get_window_state()` through return/error. |
| `observation_processing` | Extracting snapshot metadata and quality information. |
| `action_dispatch` | Selected-control recording through action preparation and return. |
| `action_preparation` | Checking synthetic targets, building click arguments, and recording `action_requested`. |
| `native_action` | `driver.click()` through return/error. |
| `loop.iteration` | The current pass, including observation, selection, action/wait, or verification as applicable. |
| `settling_wait` | The requested app/browser settling wait, starting after CUA returns for the action. |

Within selection, inspect `selection.validation`, `selection.round`,
`selection.grouping`, `selection.sdk_call`, `selection.response_validation`, and
`selection.advancement`. A round starts before forming its groups and ends after
its winners are ready. Only a singleton window can bypass selection. A singleton
control or evidence candidate is still judged against `none`, with final timing
or confirmation in the same request. A trailing one-candidate group within a
larger round still belongs to that round's single request.

Foreground activation precedes the newly selected real window's control snapshot.
Synthetic controls require no live UIA read or foreground activation. A skipped
synthetic action can have preparation records but has no `native_action`.
Absent stages mean the work did not occur.

Evidence selection uses the existing selection traces with stage `evidence`.
Its round `api_call` records retain all group and confirmation questions, answers,
and probabilities. A final round's `verification` trace identifies the confirmation
question, returned choice, and whether evidence plus confirmation verified the
action. The `action_verification`
event records `verified`, `last_control` including its status, selected evidence
or `null`, and the reason. `loop_route` records the terminal verification route:
`verified`, `no_evidence`, or `unverified`. The result's `iterations` array retains
the pass route and `settling_ms` when used. Use `(iteration, round_id)` when
correlating repeated names such as `controls_r1`; `round_id` alone can identify
different rounds in different passes. The existing span and HTTP request IDs
remain unique within the run.

`settling_wait` records `requested_ms` and its measured `duration_ms`, with `kind`
set to `after_action`;
the scheduler may return later than the request. It starts only after the CUA
action result returns, so its duration excludes `native_action`. The timing
Choice estimates additional app/browser readiness time. The exact options
`<50ms`, `100ms`, `200ms`, `300ms`, `400ms`, and `>500ms` map in code to 50, 100,
200, 300, 400, and 500 ms. The last option is a 500 ms first observation, not a
readiness guarantee. The workflow does not retry the click or add another wait
if verification fails.

On a successful pipeline return, `client_cleanup` measures closing the SDK and
HTTP clients. The total operational duration ends after the operational result
and normal cleanup, before generating the report. Stdout serialization and output
happen afterward.

## Jev API call duration

A question key such as `controls_r1_g0` identifies the stage, round, and group.
Rounds are numbered from one and groups from zero. `iteration`, `round_id`, and `question_keys`
link a round's group questions to its single API call. Each round trace contains
one `api_call` record with model identities, token usage, and SDK/validation span
IDs. Its groups retain their options and winners. HTTP events share `request_id`
and iteration/round context, linking `jev_request` with `jev_response`,
`jev_response_error`, or `jev_request_unfinished`.

The timing summary's `jev_api_call_count` counts actual prepared requests seen
by the HTTP hook. It is zero when no request occurs; Python callers must register
the report hooks to obtain this count. It can also be checked by counting
`jev_request` events, including the evidence-selection rounds in pass 2. The
per-finalist timing questions share the final control selection's call and
duration; action confirmation shares the final evidence round's call and
duration. There are no separate routing or recovery-wait calls. Inspect each
request/response pair to see its complete question and probability distributions;
do not count individual questions as requests.

The loop's last-action wait facts describe scheduled settling time and exclude
CUA duration. Use measured `settling_wait` spans for actual elapsed wait time
rather than treating the requested interval as a clock.

The primary API duration has these boundaries:

```text
Form groups and group questions; prepare and encode the HTTP request
Capture request data in memory
    START: prepared_request_before_transport
    Send request -> wait for Jev -> receive complete response body
    END: response_body_read
Decode and validate the response; advance selected candidates (none advances nothing)
```

The start is after request preparation and capture, immediately before the
prepared request proceeds to transport. The end is captured after the complete
body is read, before decoding or recording that response. There is no report
formatting, full-history copying, or file I/O inside this interval.

This is client-observed API call duration. It includes transport, connection setup
when needed, network travel, service waiting/processing, and response transfer.
It excludes group construction, request encoding, response decoding/validation,
and report generation. It is not isolated model inference time; only server
measurements could distinguish Jev's internal processing from the other costs.

`response_headers_elapsed_ms` marks entry to the response hook. Subtract the
request's `started_elapsed_ms` for the request-to-headers interval.
`response_body_read_duration_ms` measures reading the remaining body. Receiving
headers alone does not complete an API call.

All group questions in a round share that one API duration. Individual group
question durations are not observed. Dividing call time by the number of groups
does not measure per-question latency.

The enclosing `selection.sdk_call` includes SDK preparation and processing as
well as the HTTP call. It is labeled separately and must not substitute for API
call duration. Local selection response validation follows the SDK return.

The request event's `request_preparation_duration_ms` measures from SDK entry
through the prepared request's transport boundary, including lightweight request
capture. The finished `selection.round` event also contains
`response_processing_started_elapsed_ms`, `response_processing_finished_elapsed_ms`,
and `response_processing_duration_ms`: complete response receipt through winner
advancement. These fields include SDK decoding and local validation. They are
derived from recorded boundaries during final report generation, and are present
when a complete response and winner advancement were recorded.

The terminal summary's `time_to_first_jev_request_ms` is measured from the run
origin to the first prepared request's transport boundary, not its capture event.
It is `null` when no Jev call occurs.

If transport fails before a response hook runs, `jev_request_unfinished` closes
the pending interval at the SDK failure boundary with
`end_boundary: "sdk_call_exit_without_complete_response"`. It does not claim a
complete response or reveal when the remote operation stopped. Response-body
failures retain HTTP status, exception type, and observed read timing.

## Operational total and final report write

`run_finished`, `run_error`, and `run_interrupted` contain the operational `timing`
summary. `duration_ms` ends at
`end_boundary: "operational_end_before_report_generation"`. It excludes report
generation, the final write, stdout output, and process teardown. An imported
Python API caller cannot infer earlier application startup from a later reporter
origin.

The reporter records each event in memory without repeatedly serializing,
parsing, or copying the entire history. Raw HTTP bodies are retained for the
final report. At close it constructs and serializes the complete JSON document
and writes it once. The report file does not exist during normal execution.

The completed write duration cannot be inserted into that same file without
another write. Instead, the final JSON result on stdout includes these additional
fields in `timing`:

| Field | Meaning |
| --- | --- |
| `report_generation_duration_ms` | Constructing and serializing the report plus the final write. |
| `report_write_duration_ms` | The file-write portion of report generation. |
| `report_write_count` | One write for a completed report. |
| `through_report_duration_ms` | Recorded origin through completion of report generation and writing. |

The generation and write durations overlap; do not add them together. Neither is
part of the operational duration stored in the report. Lightweight event capture
still has a cost; the measurements do not claim zero instrumentation overhead.

Python API callers pass the same `RunReport` to
`run_pipeline(..., report=report)` and register its `on_request`/`on_response`
hooks on the HTTP client supplied to `TypeSafeClient`. Pipeline timing propagates
to the selector; the hooks provide HTTP timing. Direct selector callers can pass
`report=report` to `select()`. Finish by calling `report.close()` once to write the
collected records and receive the completed timing summary. The CLI handles this
lifecycle and the run-level terminal events.

Ordinary failures and interruptions retain records in memory until final
reporting can run. An abrupt process termination can lose the report; there is
no per-event durable copy. A missing finish record does not mean zero duration.

Native action duration ends when the adapter returns. Its result does not prove
that the screen changed or the user's task completed. The loop waits, observes
fresh state, and selects evidence with the final action confirmation described
in the [main workflow](main-workflow.md). Offline fixtures verify boundaries and correlation;
their durations are not production Jev benchmarks. The model remains
`jev-1.13.0` and the driver remains CUA Driver `0.28.2`.
