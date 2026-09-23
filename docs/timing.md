# Reading run timings

The [main workflow](main-workflow.md) defines the selection and timing contract.
A candidate is one window or control. A group contains up to ten candidates and
advances one selected candidate. A control round sends batches of at most 400
candidates, preserving groups of ten within each API call. All batch winners
recombine before the next round starts. Window rounds send all their group
questions in one API call. Every window and control group uses the same
`QUESTION` from [`src/jev_questions.py`](../src/jev_questions.py). The final control
round's request also includes `ACTION` for every finalist and a hypothetical
`TIMER` for each of `click` and `type_text`. Only the chosen control's action and
matching timer are used. Text resolution adds one call containing a `TEXT_WORD`
Noul for every source word. Windows Search bypasses control selection and timers:
it resolves its query before activation, then sends Win+S and immediately types
the exact resolved string in the same iteration.
There is no intervening observation, confirmation, sleep, or Jev call, and no
settling wait after typing. Each subsequent iteration starts with fresh window
discovery. The candidate cap bounds request size; it is
not a token-limit guarantee for unusually long descriptions or shared state.

The CLI collects events, timestamps, and actual Jev payloads in memory. At the end
one worker creates a unique `reports/run-<timestamp>-<id>/` folder, writes the
event array once to `total_run.json`, and saves separate Jev input and output
files directly alongside it. Payload filenames combine a shared chronological
sequence number, direction, loop number, singular workflow stage, and round number.
Rounds with multiple batches also include the one-based batch number:

```text
reports/run-<timestamp>-<id>/
  total_run.json
  0001_jev_input_loop_1_window_round_1.json
  0002_jev_output_loop_1_window_round_1.json
  0003_jev_input_loop_1_window_round_2.json
  0004_jev_output_loop_1_window_round_2.json
  0005_jev_input_loop_1_control_round_1_batch_1.json
  0006_jev_output_loop_1_control_round_1_batch_1.json
  0007_jev_input_loop_1_control_round_1_batch_2.json
  0008_jev_output_loop_1_control_round_1_batch_2.json
  0009_jev_input_loop_1_text_round_1.json
  0010_jev_output_loop_1_text_round_1.json
```

The prefix uses at least four digits, expanding uniformly for longer runs to
preserve chronological order across all inputs and outputs in the run. This file
sequence is separate from `request_id`, which still pairs request/response events
and counts API calls. Input files
contain the entire `model`, `state`, and `questions` payload, including failed
calls. They use two-space indentation and a blank line between top-level fields,
with `state` first, `questions` second, and remaining fields afterward. This
changes only whitespace and field order, preserving all JSON field names and
values. JSON output files preserve every returned field and value captured before
SDK validation, with no wrapper or added metadata. They use the same formatter
as inputs: two-space indentation, expanded nested objects and arrays, and a blank
line between top-level fields. JSON HTTP error bodies receive the same formatting;
non-JSON responses retain their original bytes even with the `.json` extension.
Transport failures and incomplete body reads have no complete response body and
produce no output file.

Each `jev_request.request_file` and `jev_response.response_file` links its filename
relative to the run folder. Direct selector calls without iteration metadata omit
the loop, such as `0001_jev_input_control_round_1.json`; an unscoped direct HTTP
call uses `0001_jev_input.json` and `0002_jev_output.json`. The report path and run
folder are printed to stderr.
The result on stdout retains selection and native action facts and adds completed
report-generation timing. A run with no Jev calls still has its `total_run.json`.

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
| `batch`, `batch_count`, `batch_candidate_count` | One-based batch position, number of calls in the round, and candidates in this batch. Inherited by nested SDK and HTTP events. |

Use `duration_ms` or the explicit work boundaries, rather than subtracting event
creation timestamps. A start event can be recorded before its measured work
begins. Intervals marked `recorded_after_completion: true` were recorded after
both boundaries were captured, as with startup and client cleanup.

Nested operations overlap. A selection includes its rounds; a round includes
forming groups, each batch's request preparation, API call, and response
processing, and advancing all group winners. Do not add parent and child
durations to reconstruct the total.

## Following a run

Filter measured operations in PowerShell:

```powershell
$events = Get-Content -Raw -LiteralPath 'reports/run-REPLACE-WITH-YOUR-RUN/total_run.json' | ConvertFrom-Json
$events | Where-Object event -eq 'timing_finished' |
    Select-Object operation, iteration, stage, round, batch, duration_ms, span_id, parent_span_id |
    Format-Table -AutoSize
```

`startup.imports`, `startup.cli`, and `startup.report_initialization` cover the
recorded startup work. `initialization.request_read`,
`initialization.configuration`, `initialization.driver`,
`initialization.http_client`, `initialization.typesafe_client`, and
`initialization.selector` cover setup before `pipeline`. Within the pipeline:

| Operation | Measured work |
| --- | --- |
| `request_word_preparation` | Splitting the original request into ordered source words and offsets once, before the first loop. |
| `window_discovery` | `driver.list_windows()` through return/error. |
| `candidate_preparation` | Building window or control candidate descriptions. |
| `window_selection`, `control_selection` | Complete window/control selection, including final action Choices and hypothetical timers where applicable. |
| `text_selection` | Complete source-word selection, including the one batched Noul call and local contiguous-substring resolution. |
| `synthetic_resolution` | Looking up controls for a synthetic selection. |
| `foreground_preparation` | Preparing native activation arguments. |
| `foreground_activation` | `driver.bring_to_front()` for a real window or `driver.hotkey()` for Windows Search, through return/error. |
| `control_acquisition` | `driver.get_window_state()` through return/error. |
| `observation_processing` | Extracting snapshot metadata and quality information. |
| `action_dispatch` | Native control action preparation through click/text return, or Windows Search activation through text-entry return. Text resolution precedes this span. |
| `action_preparation` | Building native click/text arguments from the selected snapshot target. Synthetic target checks precede this span. |
| `native_action` | `driver.click()` or `driver.type_text()` through return/error. |
| `loop.iteration` | The containing cycle, including observation, selection, native action return, and settling when another observation follows. |
| `settling_wait` | The requested app/browser settling wait, starting after CUA returns for the action. |

Within selection, inspect `selection.validation`, `selection.round`,
`selection.grouping`, `selection.batch`, `selection.sdk_call`,
`selection.response_validation`, and `selection.advancement`. A round starts
before forming its groups and ends after all its winners are ready. A batch
contains one SDK call and local validation/collection of its answers; batches
execute sequentially. Only a singleton window can bypass selection. A singleton
control is still selected, with its action Choice and both timers in the same request when
execution context is available. A trailing one-candidate group stays in the
round's final batch.

Foreground activation precedes the newly selected real window's control snapshot.
Windows Search uses `foreground_activation` for desktop Win+S, then directly
dispatches desktop `type_text`. It has no live UIA read, control candidates,
control-selection rounds, or settling timer. Both native requests and results
are recorded separately under the same iteration. Its text-selection call occurs
before `foreground_activation`, and is absent when the exact `--text` override
is supplied. Unresolved text has no `native_action` or Search activation.
Absent stages mean the work did not occur.

Selection traces retain every group question, answer, and probability distribution
in each round's `api_calls` list. The final control round also retains every
finalist's action and both timing answers. Text traces retain every Noul
probability, inclusion threshold, selected positions/offsets, and the exact
resolved substring or unresolved reason. The `text_selected` event links these
traces to the destination. `request_words_prepared` records the source spans once.
`loop_route` records whether the pipeline continues or stops. The result's
`iterations` array retains the route and selected `settling_ms`. Use
`(iteration, round_id)` when correlating repeated names such as `controls_r1`;
`round_id` alone can identify different rounds in different passes. The existing
span and HTTP request IDs remain unique within the run.

For a timeline, label each loop from the `window_discovery` operation's
`started_elapsed_ms` to the final `native_action` operation's
`started_elapsed_ms` in that iteration. These boundaries represent current-state
acquisition through dispatch to the CUA adapter. The latter is entry to
`driver.click()` or `driver.type_text()`; the report does not measure the later transport-send instant
inside the adapter. Subtract those boundaries for the loop duration and use the
end boundary for grand total elapsed time through that loop. Mark each loop's
number in its start/end rows, with loop duration and grand total in the end row.
The corresponding `started_at` values provide UTC timeline times. Show native
call duration and settling afterward, before the next loop's acquisition. The enclosing
`loop.iteration` duration also includes that return and settling, so it is a
broader cycle measurement. Windows Search's hotkey duration occurs inside the
loop before the final typing dispatch; its text-entry call return occurs after
that boundary, with no settling wait. If a cycle stops without dispatching an action,
show its stop route and measured ending boundary instead of inventing an action
boundary.

`settling_wait` records `requested_ms` and its measured `duration_ms`, with `kind`
set to `after_action`;
the scheduler may return later than the request. It starts only after the CUA
action result returns, so its duration excludes `native_action`. The timing
Choice estimates additional app/browser readiness time. The exact options
`<50ms`, `100ms`, `200ms`, `300ms`, `400ms`, and `>500ms` map in code to 50, 100,
200, 300, 400, and 500 ms. The last option is a 500 ms first observation, not a
readiness guarantee. The wait runs after a returned action only when another
observation will follow. Its duration remains separate from the native action.
This wait applies to selected control clicks and confirmed targeted text entry;
Windows Search has no timer or wait. Unconfirmed targeted text entry stops with
its native facts and no automatic replay.

On a successful pipeline return, `client_cleanup` measures closing the SDK and
HTTP clients. The total operational duration ends after the operational result
and normal cleanup, before generating the report. Stdout serialization and output
happen afterward.

The CUA client is persistent for one user request. Its lazy MCP startup,
handshake, and one session cursor-motion configuration call are included in the
first CUA call's span. The client requests a 10 ms fixed glide before sending
the first requested tool call, and reapplies it after reconnecting. Windows CUA
0.28.2's default glide instead waits for a distance-dependent animation to
arrive; that wait is included in `native_action` and can dominate later clicks.
The 10 ms setting bounds only the glide wait, not OS delivery or transport time.
A read may reconnect and
retry once after connection loss, timeout, or an unavailable daemon; both attempts
remain inside that call's outer span. Each attempt has its own default 60-second
deadline, so an outer read span can exceed 60 seconds. Clicks, foreground
activations, hotkeys, and text entry are not retried. `driver_cleanup` measures closing this request's MCP client after
normal work, failure, or interruption, before the terminal report event. The
shared daemon remains running. No periodic health polling is performed.

## Jev API call duration

A question key such as `controls_r1_g0` identifies the stage, round, and group.
Rounds and batches are numbered from one; group numbers start at zero and remain
unique across the whole round. `iteration`, `round_id`, `batch`, and
`question_keys` link each group to its API call. Every newly produced round trace
uses one `api_calls` list, including rounds with only one call. Each call retains
its own model identities, token usage, questions, answers, probabilities, and
SDK/validation span IDs, plus its batch number, batch count, candidate count, and
batch span ID. The round's groups retain their options and winners. Historical
reports used a singular `api_call` record; consumers reading them should treat
it as a one-item list. New reports do not duplicate the singular field.

HTTP events share `request_id` and iteration/round/batch context, linking
`jev_request` with `jev_response`, `jev_response_error`, or
`jev_request_unfinished`. Sum token usage once per `api_calls` entry, or once per
HTTP response's usage object; those are two views of the same usage and must not
be added together.

The timing summary's `jev_api_call_count` counts actual prepared requests seen
by the HTTP hook. It is zero when no request occurs; Python callers must register
the report hooks to obtain this count. It can also be checked by counting
`jev_request` events across all iterations. The per-finalist action and timing
questions share the final control selection's call and duration. All word Nouls
share one additional text-selection call; they are not separate API calls. Inspect each
request/response pair to see its complete question and probability distributions;
do not count individual questions as requests.

The round and iteration traces retain the selected timer interval. Use measured
`settling_wait` spans for actual elapsed wait time rather than treating the
requested interval as a clock. `last_action` retains the tool and native result
facts used as context for the next selection, including the exact text after
either targeted control typing or Windows Search text entry.

The primary API duration has these boundaries:

```text
Form groups and group questions; prepare and encode the HTTP request
Capture request data in memory
    START: prepared_request_before_transport
    Send request -> wait for Jev -> receive complete response body
    END: response_body_read
Decode and validate the response; advance selected candidates
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

All questions in a batch share that API duration. Individual group question
durations are not observed. Dividing call time by the number of groups does not
measure per-question latency. Sum the round's HTTP request durations for its API
time; the enclosing round duration also includes local preparation and processing.

The enclosing `selection.sdk_call` includes SDK preparation and processing as
well as the HTTP call. It is labeled separately and must not substitute for API
call duration. Local selection response validation follows the SDK return.

The request event's `request_preparation_duration_ms` measures from SDK entry
through the prepared request's transport boundary, including lightweight request
capture. Each finished `selection.batch` with a complete response contains
`response_processing_started_elapsed_ms`, `response_processing_finished_elapsed_ms`,
and `response_processing_duration_ms`: complete response receipt through local
validation and collection of that batch's answers. These fields include SDK
decoding. The finished `selection.round` sums those disjoint processing durations
and adds its winner-advancement duration once. It exposes the summed
`response_processing_duration_ms` without a misleading continuous start/end
interval that would include later batches' HTTP waits. Do not add the round
aggregate to its batch durations. A failed round retains whichever complete
response-processing intervals were observed; a failed call without a complete
body supplies no such interval. Unbatched legacy span structures retain their
original continuous response-receipt-through-advancement fields. All these
derived fields are calculated during final report generation.

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
final report. At close a single worker constructs and serializes the complete
JSON document and writes it once, then parses and formats the buffered input and
output JSON bodies with the same formatter. Non-JSON response bodies are saved
as their original bytes. Each payload file is saved atomically. The action loop does no
payload-export parsing or disk writes. The worker starts after processing ends;
the caller waits once at shutdown
so files are complete before exiting. The main report is saved first and still
contains the captured payload fields or decoded body text if a later payload-file
write fails. Repeated `close()` calls
reuse the completed result or rethrow the original finalization failure.

The completed write duration cannot be inserted into that same file without
another write. Instead, the final JSON result on stdout includes these additional
fields in `timing`:

| Field | Meaning |
| --- | --- |
| `report_generation_duration_ms` | Worker startup, final document construction, serialization, all file writes, and shutdown wait. |
| `report_write_duration_ms` | The file-write portion of report generation. |
| `report_write_count` | One write for a completed report. |
| `jev_requests_directory` | Absolute path of the run folder containing `total_run.json` and all Jev input/output files. |
| `jev_request_file_count` | Number of outgoing input files successfully saved. |
| `jev_request_write_duration_ms` | Time spent saving outgoing input files, separate from the main report write. |
| `jev_response_file_count` | Number of complete output files successfully saved; failures without a complete response body have no output file. |
| `jev_response_write_duration_ms` | Time spent saving output files, separate from the main report and input writes. |
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
that the screen changed or the user's task completed. When continuing, the loop
observes fresh state and selects the next action, after a settling wait for
control clicks/confirmed targeted typing or directly after Windows Search typing,
as described in the
[main workflow](main-workflow.md). Offline fixtures verify boundaries and correlation;
their durations are not production Jev benchmarks. The model remains
`jev-1.13.0` and the driver remains CUA Driver `0.28.2`.
