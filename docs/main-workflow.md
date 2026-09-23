# Main workflow

This is the authoritative description of the Jev-driven control loop in
`src/pipeline.py`, its selector, reporter, and CLI. Each iteration discovers
windows and selects a window. A real window goes through control reading,
selection, clicking, and settling. The synthetic Windows Search destination
instead sends Win+S and immediately types the supplied exact query. The next decision
uses the original request, the previous action, and the current observation.
[Reading run timings](timing.md) maps these operations to report fields.

The workflow uses exactly two question definitions in
[`src/jev_questions.py`](../src/jev_questions.py):

- `QUESTION` selects the next candidate at every window and control round,
  including the final round.
- `TIMER` estimates the additional settling interval for a possible control click.
  It is included once per finalist in the final control-selection request.

These are two question types, reused as needed. A round with 40 groups still
contains 40 instances of the selection question, all sent in one API call.
There is no evidence-selection stage, action-confirmation question, or automatic
completion judgment. Reaching the iteration limit does not assert that the
user's request has been fulfilled.

## Vocabulary

| Term | Meaning |
| --- | --- |
| Candidate | One observed or synthetic window/control option. |
| Group | Up to 10 candidates considered together. |
| Group question | One instance of `QUESTION` selecting a candidate from a group. |
| Round | Evaluate all groups, then advance each group's selected candidate after every batch succeeds. |
| API call | One request with a batch of group questions (up to 400 control candidates), plus any final control timers. |
| Stage | Window discovery/selection, activation, control acquisition/selection, or action dispatch. |
| Iteration | One decision cycle with fresh window discovery, selection, observation, and action when available. All calls in the cycle share its iteration number. |
| Loop boundary for a timeline | Starts at current-state acquisition and ends when the last CUA command is sent: a control click or Windows Search text entry. Native return and any settling appear afterward, before the next loop starts. |

The report's `loop.iteration` span encloses the complete decision cycle, including
native return and any settling wait. Its duration therefore differs from the
user-facing timeline's dispatch-bounded loop duration. A terminal cycle without
an action is a stopped cycle, not a completed click.

## Each iteration

1. Keep the user's exact original request in state and set this iteration number.
2. Discover windows and build their candidate descriptions. Refresh the previous
   current window if still observed; otherwise clear it.
3. Select the next window with `QUESTION`. This happens again every iteration,
   allowing the next action to move to another window.
4. For Windows Search, send desktop Win+S, then immediately send desktop
   `type_text` with the exact supplied string, and skip to the next iteration
   (or return `iteration_limit` at the configured bound). No control read,
   control selection, timer, observation, confirmation, or sleep intervenes.
   Without supplied text, return `no_action` before either native command.
   For a real window, bring it forward and read its accessibility tree.
   Preserve the observation identities needed to execute the selected control.
5. Build control candidates once and narrow them with `QUESTION`. Include one
   conditional `TIMER` per finalist only in the final control round.
6. Record the selected control and source window as the latest attempted action.
   Execute its CUA click and retain the native result.
7. At the iteration limit, return `iteration_limit` without another wait or
   observation. Otherwise, wait the winning control's timer interval after CUA
   returns, then begin the next iteration.

Empty, unavailable, or failed observations/actions and missing search text can stop earlier.
Old executable targets are not reused for a new iteration. There are no separate
routing, verification, retry-action, or extra-wait questions.

The synthetic Windows Search route has one window candidate and no control
candidates. It uses the existing activation stage for
`hotkey({scope: "desktop", keys: ["win", "s"]})`, followed directly by
`type_text({scope: "desktop", text: <exact supplied text>})`. Both commands belong
to the same iteration. This route makes zero control-selection rounds and zero
timer requests, and adds no settling wait after typing. The next normal
iteration reads fresh state and can select real search results. Real Start and
Search windows remain observed window candidates; the synthetic route does not
replace them.

## Shared Jev state

Every window and control request carries the same run context:

| Field | Meaning |
| --- | --- |
| `original_request` | The user's exact original text, without rewriting, trimming, or shortening. |
| `iteration` | Current decision cycle, starting at 1; it does not count completed clicks. |
| `current_window` | Observed app/title metadata, refreshed from discovery and changed when a new window is selected; `null` before selection or when the old window is absent. |
| `last_control` | `null` until action selection, then `{window, control}` with source window and descriptive control fields. |
| `last_action` | Previous tool and filtered native delivery/result facts, available after an action attempt. Windows Search text entry also retains the exact supplied text. |

One mutable dictionary is reused within the run. Each request serializes a
complete snapshot; this is not a server-side patch protocol. Round traces and
report events retain historical snapshots so later updates cannot change earlier
requests. Refreshing the current window must not rewrite the source window
retained with the previous control.

Candidates live in group questions. Shared state contains no full previous/current
control list, accumulating action history, or local snapshot diff. Native target
identities remain local. The selector also supports a plain request string for
direct callers; the pipeline uses the existing shared dictionary.

## Selection rounds and API batches

1. Divide candidates into groups of up to 10, preserving observed order.
2. Give every group one `QUESTION` with only its candidates as criteria. There is
   no synthetic `none` response option.
3. For controls, send consecutive batches of at most **400 candidates** (40 groups)
   per Jev API call. Keep all window groups in one call. Final control timers share
   the final round's single batch.
4. Retain each selected option, confidence, and probability distribution.
5. After every batch succeeds, combine one winner per group in group order;
   regroup until one winner remains.

An initial singleton window bypasses Jev. A singleton control still receives the
selection question and, for a real window, its timer. Every round uses the same
`QUESTION` wording; observed window context is already in shared state.

| Control round | Candidates entering | Groups / selection questions | API calls | Winners leaving |
| --- | ---: | ---: | ---: | ---: |
| 1 | 392 | 40 | 1 | 40 |
| 2 | 40 | 4 | 1 | 4 |
| 3 | 4 | 1 | 1 | 1 |

This uses three control calls in total. The final call adds four timers, for five questions
in that request. Window selection with 17 candidates adds two calls: 17 become
two winners, then one. The next iteration begins new window/control rounds.

For 631 controls, round 1 sends the first 400 candidates in 40 groups and the
remaining 231 in 24 groups, using two sequential API calls. All 64 winners enter
round 2 together, then seven enter the final round: four control calls in total.
For 647 controls, the first round sends 400 and 247 candidates in 40 and 25 groups.
The rounds are `647 -> 65 -> 7 -> 1`, also using four control calls in total.
For 1,000 controls, the first round sends 400, 400, and 200 candidates. The rounds
are `1000 -> 100 -> 10 -> 1`, using three calls, then one, then one: five control
calls in total. Larger rounds require further batches, each capped at 400 candidates.

Questions within one call are independent. Later rounds depend on all earlier
group winners and start only after every batch response is validated. If any
batch fails, selection stops before advancing or acting on partial results.
Candidates are not silently removed.

## Final control selection and timer

Each finalist receives the same `TIMER` wording plus structured context identifying
its candidate, observed description, CUA click, and app/window. Its timer cannot
rely on the selection answer from the same batch. Retain all responses but consume
only the winning candidate's interval. This follows the existing
[speculative fan-out pattern](https://docs.typesafe.ai/patterns/fan-out).

| Choice option | Scheduled wait (milliseconds) |
| --- | ---: |
| `<50ms` | 50 |
| `100ms` | 100 |
| `200ms` | 200 |
| `300ms` | 300 |
| `400ms` | 400 |
| `>500ms` | 500 |

The last option schedules observation after 500 ms; it does not guarantee
readiness. Jev chooses an option; code maps it to time. The timer starts **after
the CUA call returns**, excludes CUA execution, and runs only before another
iteration. No wait follows the last permitted action.

## Running and stopping

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.' --max-iterations 1
uv run --locked python src/main.py 'Open Chrome using Windows Search.' --text 'Chrome'
```

The Python API is `run_pipeline(request, driver, selector, max_iterations=10, text=None)`.
`--max-iterations` and the Python argument must be positive integers, default 10.
`--text` and the Python `text` argument supply the exact query for the Windows
Search route; they do not force its selection. Explicitly empty or whitespace-only
strings are rejected. Valid text is retained verbatim, without extraction from
the request, rewriting, trimming, or line-ending normalization. Selecting Windows
Search without text returns `no_action` with a reason identifying missing `--text`.
Callers can supply `sleep=` for deterministic tests. The connection check in
`scripts/check_jev.py` uses the production selector with one fictional control:
one selection and one timer in one call, with production response validation
and no desktop action.

| Status | Meaning | CLI exit |
| --- | --- | ---: |
| `iteration_limit` | Configured action-cycle bound reached; overall completion unknown. | 2 |
| `no_action` | Windows Search selected without text, or a custom synthetic control lacks a live CUA target. | 2 |
| `no_windows` | No window candidates; normally unreachable with Windows Search enabled. | 2 |
| `no_controls` | Valid observation/synthetic definition has no indexed controls. | 2 |
| `observation_degraded` | Degraded read has no indexed controls. | 2 |
| `observation_unavailable` | Structured controls or snapshot identity unavailable. | 2 |
| `error` | Driver, configuration, or Jev request/response failure. | 1 |

One persistent CUA MCP client belongs to each run. It opens lazily and closes on
return, failure, or interruption; the daemon stays running. Reads may reconnect
and retry once after connection loss, timeout, or unavailable daemon. Uncertain
clicks, foreground activations, hotkeys, and text entry are never replayed. CUA results describe
delivery, not screen readiness or completion.

## Restrictions and limits

| Restriction | Current behavior |
| --- | --- |
| Accessibility size | Default 5,000 elements and depth 25; positive `--max-elements` and `--max-depth` overrides. Coverage may be partial. |
| Description size | App/title/control text capped at 240 characters; up to two indexed ancestors with 120-character labels. Original request and source records remain intact. |
| Jev timeout/retries | 30-second HTTP timeout configuration, zero automatic retries; not a total run deadline. |
| CUA timeout/retries | 60-second deadline per transport attempt including lazy startup; reads may reconnect/retry once, actions are not replayed. |
| Batching | Up to 10 candidates per group; at most 400 control candidates per call; window rounds use one call; timers only in final control call. |
| Settling | One 50–500 ms scheduled wait after a control click when another iteration follows; no settling wait on the Windows Search route. |
| Iterations | Positive bound, default 10; no automatic completion judgment. |
| Model | Exact pin `jev-1.13.0`, validated on every response. |
| Action scope | Observed control clicks and direct Windows Search activation/text entry; no coordinate fallback, automatic action retry, or general recovery planner. |
| Response validation | Exact question/option labels; Choice answers; finite probabilities/confidence in [0, 1]; totals 0.97–1.03; nonnegative integer token counts. |
| Confidence gating | None; returned choices advance directly without ranking across groups or thresholds. |

Controls require structured indexed observations and snapshot identities. Clicks
use an observed element token or an element index with its snapshot ID. Old
targets are cleared before new observation; stale failures are not replayed.

There is no local token cap, byte-packing heuristic, or total runtime cap.
Provider limits apply to shared state plus questions; see
[TypeSafe model documentation](https://docs.typesafe.ai/models). Observation and
text bounds and the 400-candidate cap do not guarantee a batch fits those limits.

## Timing and reporting

Capture monotonic boundaries for startup, discovery, candidate preparation, each
selection stage/round, foreground activation, control acquisition, native action,
settling, and cleanup. UTC timestamps add wall-clock context. Nested spans overlap;
do not add parent and child durations. The report identifies its timing origin.

API duration begins immediately before transport receives the prepared request
and ends when the complete response body arrives. It includes transport, network,
and service work; it excludes request construction and response decoding. It is
client-observed duration, not isolated inference time. All questions in a batch
share that call's duration. Each round retains an `api_calls` list, including
single-call rounds, with separate answers, usage, and timing references per call.

Each cycle retains iteration, selections, native results, rounds, and route.
Windows Search records both `hotkey` and `type_text` action events and timings
under that iteration. The final action and exact query remain in `last_action`;
there are no control-selection rounds or fake control records for this route.
Correlate repeated round names with `(iteration, round_id)` and span/request IDs.
Retain the original request, question text, all probabilities, timer answers,
consumed timer, requested wait, and measured durations. No evidence rounds or
confirmation traces are generated.

Collect data in memory, then construct and write the report once after the
operational result and cleanup. Do not copy/serialize accumulated history per
event. Measure report generation/writing separately and return completed write
duration on stdout rather than rewriting the report. Abrupt termination can lose
in-memory records. See [reading run timings](timing.md) for exact boundaries.
