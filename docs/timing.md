# Reading run timings

Normal CLI runs write timing information into the existing `reports/run-*.json`
event array. Use the report path printed to stderr. No additional flag or live
desktop benchmark is needed to inspect a completed run. The JSON result on stdout
continues to describe selection and native action facts.

The report retains the original milestones, Jev request and response bodies,
question keys, options, probability distributions, tournament rounds, selected
identities, and native action results. Timing events give those records a common
elapsed-time axis; they do not change the questions or introduce extra requests.

## Clocks and boundaries

Elapsed times use Python's high-resolution monotonic `perf_counter_ns()` clock
and are reported in milliseconds. Use these values for durations and ordering.
UTC `timestamp` values let you correlate the report with external logs. They use
one wall-clock anchor plus monotonic elapsed time; they do not jump if the system
clock changes during the run. Wall-clock adjustments do not affect durations.

The CLI records its earliest measurable boundary in `src/main.py`, immediately
after importing the standard-library `time` module and before its other imports.
This measures the remaining module imports and subsequent CLI work. It does not
measure process creation, Python interpreter initialization, the initial `time`
import, or any work before execution reaches that boundary. `python -m src.main`
can also perform package/module setup before reaching it. Startup records are
saved retrospectively once the reporter is available, so they cannot preserve an
import failure or a process kill that occurs before reporting initialization.
`--help` and argument-parse errors also exit before a report is created.

The `timing_origin` event and terminal event's `timing.origin` identify the boundary:

| Origin | Meaning |
| --- | --- |
| `main_module_after_time_import` | CLI execution through `python src/main.py` or `python -m src.main`. |
| `main_call` | A call to an already imported `main()`; earlier imports are outside this run. |
| `report_construction` | The default for a Python API caller constructing `RunReport` without an explicit earlier origin. |

Every event has `elapsed_ms`, measured from the run's recorded origin. Paired
`timing_started` and `timing_finished` events describe an operation:

| Field | How to use it |
| --- | --- |
| `operation` | Identifies the local operation being measured. |
| `span_id` | Joins a start to its finish. |
| `parent_span_id` | Associates nested work with its enclosing operation. |
| `duration_ms` | On a finish, the monotonic duration of the operation. |
| `started_elapsed_ms`, `finished_elapsed_ms` | Actual work boundaries relative to the origin, on a finish. |
| `started_at`, `finished_at` | The corresponding anchored UTC work boundaries. |
| `status` | On a finish, `ok`, `error`, or `interrupted`; failures also retain `error_type`. |
| `stage`, `round`, `batch_id`, `question_keys` | Correlate selection and HTTP work where these fields apply. |

A normal span saves its start event before starting the measured operation. It
stops its clock when the operation exits, before writing the finish event.
Consequently, subtracting the two events' `elapsed_ms` values can include the
start-event write and need not equal `duration_ms`. Use the reported duration for
the operation itself. Intervals marked `recorded_after_completion: true` were
saved after measuring both boundaries. This applies to startup, HTTP/SDK client
initialization, client cleanup, and result serialization; their start records are
retrospective.

Nested operations overlap. A selection includes its rounds, and a round includes
its packing, batches, and response processing. Do not sum all durations to obtain
a run total. Compare siblings or inspect parent/child boundaries to understand
where the time went.

## Following a run

Read the event array in order, or filter the finish events in PowerShell:

```powershell
$events = Get-Content -Raw -LiteralPath 'reports/run-REPLACE-WITH-YOUR-FILE.json' | ConvertFrom-Json
$events | Where-Object event -eq 'timing_finished' |
    Select-Object operation, stage, round, batch_id, duration_ms, span_id, parent_span_id |
    Format-Table -AutoSize
```

Start with `startup.imports`, `startup.cli`, and `startup.report_initialization`.
The `initialization.request_read`, `initialization.configuration`,
`initialization.driver`, `initialization.http_client`,
`initialization.typesafe_client`, and `initialization.selector` operations cover
setup before `pipeline`. Within the pipeline, inspect:

| Operation | Measured work |
| --- | --- |
| `window_discovery` | The `driver.list_windows()` call. |
| `candidate_preparation` | Building candidate descriptions, identified by `stage: windows` or `controls`. |
| `window_selection`, `control_selection` | The complete selector call for that stage. |
| `synthetic_resolution` | Looking up controls for a synthetic selection. |
| `foreground_preparation` | Preparing activation arguments and saving `action_requested`. |
| `foreground_activation` | The `driver.bring_to_front()` call through return/error. |
| `control_acquisition` | The `driver.get_window_state()` call through return/error. |
| `observation_processing` | Extracting snapshot metadata and quality information. |
| `action_dispatch` | The path from the selected-control milestone through action preparation and return, including reporting. |
| `action_preparation` | Checking synthetic targets, building click arguments, and saving `action_requested`. |
| `native_action` | The `driver.click()` call through return/error. |

Foreground activation occurs before the control snapshot even when `--click` is
absent. `action_dispatch` and `action_preparation` can also appear for a skipped
synthetic target; `native_action` only appears when a click is invoked.

Within `selection`, inspect `selection.validation`, `selection.round`,
`selection.grouping` (including Choice-question construction), `selection.packing`,
`selection.batch`, `selection.sdk_call`, `selection.response_validation`, and
`selection.advancement`. Singleton candidates advance locally; they have no Jev
request or inference latency. Synthetic controls also require no live UIA read or
foreground activation. Missing operations on those routes mean the work did not
occur.

After a successful pipeline return, `client_cleanup` records closing the SDK/HTTP
clients once both context managers exit successfully. Cleanup errors propagate
through the original CLI error path and do not produce a completed cleanup
interval. `result_serialization` records preparing the JSON result for stdout.
The final terminal boundary includes those operations on a successful run; the
actual stdout write follows it.

The first `jev_request` event's `elapsed_ms` gives the measured time from the
origin to the first HTTP request hook. It includes all earlier startup, discovery,
candidate preparation, selection preparation, SDK preparation, and reporting work.
The terminal event also exposes this as `timing.time_to_first_jev_request_ms`,
which is `null` if the run makes no Jev request.

## Associating Jev questions with time

The existing question key, such as `windows_r1_g0`, identifies its stage, round,
and candidate group. Rounds are numbered from one; groups and batches from zero.
`round_id` (`windows_r1`), `question_keys`, and `batch_id` (`windows_r1_b0`)
associate the groups packed into one SDK call. HTTP events retain their
`request_id` and batch context, so you can join `jev_request` to `jev_response`,
`jev_response_error`, or `jev_request_unfinished` and inspect the exact request
options and returned probabilities. Existing round and batch
traces carry span IDs linking them to measured operations. Groups also carry
their `batch_id`, or `null` when they were forwarded as singletons.

Every question in a batch shares the same observed request interval. The client
does not observe individual provider question start/finish times. Do not divide
the batch duration by its question count and label the result per-question
inference time. Batching and sequential round advancement are unchanged.

The SDK-call span covers the client call through its return or exception. The
HTTP request interval begins at entry to the request hook and ends when the
response body has been read. It includes writing the durable request event,
network transport, remote waiting, and body reading. `response_headers_elapsed_ms`
is the elapsed time from the run origin to entry to the response hook;
subtract `started_elapsed_ms` to obtain the request-to-headers interval.
`response_body_read_duration_ms` measures the subsequent body read. These are
client-observed boundaries, not provider-only inference times or independent
network/queue/compute measurements.

The enclosing SDK interval additionally includes SDK serialization and validation,
HTTP-hook processing, and reporting overhead. Local selection response validation
has its own interval after the SDK returns. Subtracting nested timings gives a
residual containing these costs; it does not reveal undocumented provider internals.

If no response hook runs because of a transport error, the pending HTTP interval
can only be closed at the enclosing SDK failure boundary. The report records
`jev_request_unfinished` with
`end_boundary: "sdk_call_exit_without_complete_response"`. That is an observed
failure boundary, not evidence of when the remote operation stopped. Response-body
failures retain the HTTP status, exception type, and observed read interval where
available.

## Action return, failures, and measurement limits

Action preparation is distinct from the native driver invocation. The invocation
duration measures the adapter call through its returned result or error, including
the CLI/IPC/native work exposed through that call. It does not isolate internal
driver stages. `action_returned` retains the native result; its latency does not
prove a visible UI change, task completion, or a successful click effect. There is
no post-action observation in this pipeline.

Exceptions and interruptions close active spans where execution and reporting
remain possible. Earlier events remain durable if later work fails. A missing
finish event can mean an abrupt kill or a report-write failure; do not interpret
it as zero elapsed time or a completed operation. No instrumentation can make a
final disk write survive every failure.

`run_finished`, `run_error`, and `run_interrupted` contain a `timing` summary.
Its `duration_ms` ends at
`end_boundary: "terminal_event_before_its_serialization_and_write"`. It excludes
serializing/writing that final event, subsequent output/cleanup, and process
teardown. An imported Python API caller has a different origin unless it
explicitly supplies an earlier one; it cannot infer application startup from a
reporter constructed later.

For Python API usage, pass the same `RunReport` as `run_pipeline(..., report=report)`
and register its `on_request`/`on_response` hooks on the HTTP client supplied to
`TypeSafeClient`. Pipeline timing automatically propagates to the selector; HTTP
timing requires the hooks. A caller using `ChoiceSelector` directly can pass
`report=report` to `select()`. The CLI already connects these pieces and writes
run-level terminal events.

Reporting has a cost: each appended event serializes the complete array, flushes
and syncs a temporary file, then replaces the previous report. The recorded
cumulative `timing.report_write_duration_ms` covers write attempts before the
terminal event, including flush/sync/replace and failures, with
`timing.report_write_count` counting those attempts (including the initial empty
document). `timing.report_append_duration_ms` additionally includes event JSON
serialization and snapshotting. These counters overlap; do not add them together.
Neither includes the terminal event's own append/write. Child event writes can
occur inside a parent duration. Treat instrumented runs as measurements of the
instrumented application, particularly when reports contain large UI or HTTP
payloads. Compare like-for-like runs on the same environment and retain model,
candidate, batch, observation, and native-result context.

This instrumentation supplies measurements of the existing single-pass pipeline.
It does not add questions, UIA polling, sleeps, adaptive waits, DXGI capture, or a
repeated action loop. Tests use fakes and local HTTP fixtures to verify
timing and correlation; those durations are not production benchmarks. The model
remains `jev-1.13.0` and the driver remains CUA Driver `0.28.2`.
