# Main workflow

This is the authoritative description of the Jev-driven control loop in
`src/pipeline.py`, its selector, reporter, and CLI. Discover and select a window
until the first real window is chosen, then retain its native identity for the
run. Each iteration reads fresh controls in that window, followed by
selection, a click or targeted text entry, and settling. The synthetic Windows
Search destination requires a prepared query, then sends Win+S and immediately
types it. The next decision
uses the original request, the previous action, and the current observation.
[Reading run timings](timing.md) maps these operations to report fields.

The workflow keeps its authored question definitions in
[`src/jev_questions.py`](../src/jev_questions.py):

- `QUESTION` selects the next candidate at every window and control round,
  including the final round.
- `ACTION` selects `click` or `type_text` for each final control candidate.
- `TIMER` estimates the additional settling interval for a possible action.
  Separate hypothetical click and text timers accompany each finalist in the
  final control-selection round, before request sizing and packing.

Question definitions are reused as needed. A round with 40 groups still
contains 40 instances of the selection question, sent in one API call if the
complete body fits the 90,000-byte budget, otherwise in parallel batches.
There is no evidence-selection stage, action-confirmation question, or automatic
completion judgment. Reaching the iteration limit does not assert that the
user's request has been fulfilled.

## Jev request budget

**The application budget is 90,000 bytes per Jev API call, inclusive.** This is
decimal **90 KB**, not 90 KiB. A prepared body of 90,000 bytes fits; 90,001 bytes
exceeds the budget. `REQUEST_BUDGET_BYTES` in `src/selection.py` fixes this value;
there is no request-budget CLI setting.

Prepare the complete HTTP body locally through the pinned SDK's existing
request builder, then count its encoded bytes. This is the same body measured
as `len(request.content)` before transport. Count shared state, the model and
JSON envelope, question IDs, instructions, candidate criteria, every final
control action/timer. Each call repeats
its state and envelope, so count those bytes
again for each call. HTTP headers and transport framing are outside the budget.
Indented report-file sizes differ from the outgoing HTTP body sizes. Local sizing
sends no API calls and creates no HTTP request events.

Window/control selection reuse the same packing and parallel dispatch helpers.
Form selection groups of at most 50 candidates, reducing a
group to the largest contiguous prefix whose complete single-question body fits.
Complete all grouping and packing before dispatching any batch. If shared state
plus even one candidate's selection question, or a whole action/timer
question, exceeds the budget, fail that round before any API calls are sent.
Once formed, a question is atomic during packing: do not split its group or
trim the original request. If all groups in a multi-candidate round
would be singletons, fail before dispatch because the round cannot reduce them.

The budget comes from a study of 780 successful calls in 38 saved runs:
7,707,537 reported input tokens across 14,902,182 prepared HTTP body bytes, or
approximately 1.93346 bytes per input token. Applying that observed ratio to a
64,000-token reference gives approximately 123,741 bytes; reducing that by 25%
gives approximately 92,806 bytes, rounded down to **90,000 bytes**. This is an
application byte budget, not an exact tokenizer or a guarantee of passing Jev's
token limits. The model remains pinned to `jev-1.13.0`.

## Vocabulary

| Term | Meaning |
| --- | --- |
| Candidate | One observed or synthetic window/control option. |
| Group | Up to 50 candidates considered together, reduced when needed to fit the complete single-question request within the byte budget. |
| Group question | One `QUESTION` selecting a window/control candidate from a group. |
| Round | Evaluate all questions, then advance selected candidates after every batch succeeds. |
| API call | One request with a batch of whole questions and a complete body of at most 90,000 bytes; questions can be group selections or final action/timers. |
| Stage | Window discovery/selection, activation, control acquisition/selection, or action dispatch. |
| Iteration | One decision cycle with fresh control observation, selection, and action when available. Window discovery/selection occurs only until a real window is retained. All calls in the cycle share its iteration number. |
| Loop boundary for a timeline | Starts at current-state acquisition and ends when the last CUA command is sent: the selected control's click/text action or Windows Search text entry. Native return and any settling appear afterward, before the next loop starts. |

The report's `loop.iteration` span encloses the complete decision cycle, including
native return and any settling wait. Its duration therefore differs from the
user-facing timeline's dispatch-bounded loop duration. A terminal cycle without
an action is a stopped cycle, not a completed action.

## Each iteration

Before the first iteration, retain any supplied nonempty prepared text locally.
It remains pending through click-only iterations until the first typing dispatch.

1. Keep the exact request supplied to the pipeline in state and set this iteration number.
2. If no real window has been selected, discover windows and build their candidate
   descriptions. A visible, non-minimized
   `SearchHost.exe` window titled `Search` suppresses its synthetic opener, keeping
   the real window available for selection. Recompute this on every discovery.
3. On a discovery iteration, select a window with `QUESTION`. Retain the first
   real window's `pid`/`window_id` for the remainder of the run. Subsequent
   iterations skip discovery and window selection. A synthetic destination is
   not retained, so selecting Windows Search permits discovery again next time.
4. For Windows Search, require pending prepared text; otherwise return `no_action`
   before activation. Send desktop Win+S, then immediately send
   desktop `type_text` with the prepared string, and skip to the next iteration
   (or return `iteration_limit` at the configured bound). No control read,
   control selection, timer, observation, confirmation, or sleep intervenes.
   After typing consumes the prepared string, another selection of this opener
   returns `no_action` without replaying either command.
   For a real window, bring it forward and read its accessibility tree on every
   iteration, refreshing its app/title context. Preserve the observation
   identities needed to execute the selected control. If the retained window
   becomes unavailable, propagate the existing driver error; do not automatically
   select a replacement window.
5. Build control candidates once and narrow them with `QUESTION`. Only in the
   final control round, include an action Choice and a hypothetical timer for
   each of `click` and `type_text` per finalist. Consume the winning candidate's
   action and its matching timer.
6. For `type_text`, require pending prepared text; missing, empty, or already
   consumed text returns `no_action`. Consume prepared text only when typing is
   dispatched, retaining it through click-only iterations. Immediately before
   native dispatch, record the selected control,
   action, and source window as the latest attempted action. Execute the selected
   native CUA action using the original snapshot target and retain its result.
   Successful targeted typing with `confirmed` or `unverifiable` effect continues;
   explicit `partial` or `suspected_noop` effects stop with
   `observation_unavailable`. Unknown or malformed native effects fail adapter
   validation. Native errors fail without replaying typing.
7. At the iteration limit, return `iteration_limit` without another wait or
   observation. Otherwise, wait the winning control/action's timer interval after CUA
   returns, then begin the next iteration.

Empty, unavailable, or failed observations/actions and missing prepared text can stop earlier.
Old control targets are not reused for a new iteration; the window identity is.
There are no separate routing, verification, retry-action, or extra-wait questions.

The synthetic Windows Search route has one window candidate and no control
candidates. It uses the existing activation stage for
`hotkey({scope: "desktop", keys: ["win", "s"]})`, followed directly by
`type_text({scope: "desktop", text: <prepared text>})`. Both commands belong
to the same iteration. This route makes zero control-selection rounds and zero
timer requests, and adds no settling wait after typing. Without pending prepared
text, it returns `no_action` before Win+S. The next normal
iteration reads fresh state and can select real search results. Real Start and
Search windows remain observed window candidates; the synthetic route does not
replace them. A real Search window, once selected, is retained; if a result click
closes it, the next activation/read can fail instead of switching to the opened
application. Prepared text is available for only one typing operation in the run;
a new run can supply another string.

## Shared Jev state

Every window and control request carries the same run context:

| Field | Meaning |
| --- | --- |
| `original_request` | The exact command supplied to the pipeline, without rewriting, trimming, or shortening inside the loop. |
| `iteration` | Current decision cycle, starting at 1; it does not count completed clicks. |
| `current_window` | Observed app/title metadata; `null` before selection, then refreshed from each snapshot of the retained real window. Its native identity remains fixed for the run. |
| `last_control` | Previous dispatched control as `{window, control, action}`, or `null` before control dispatch. Update immediately before dispatch. |
| `last_action` | Previous tool and complete native response in `result`, including effect, route, delivery, and any evidence/escalation metadata, available after an action attempt. Text entry also retains the exact entered text. |

One mutable dictionary is reused within the run. Each round freezes the current
state for sizing and parallel dispatch. Each request serializes that complete
snapshot; this is not a server-side patch protocol. Round traces and
report events retain historical snapshots so later updates cannot change earlier
requests. Refreshing the current window must not rewrite the source window
retained with the previous control.

Candidates live in group questions. Shared state contains no full previous/current
control list, accumulating action history, or local snapshot diff. Native target
identities remain local. The selector also supports a plain request string for
direct callers; the pipeline uses the existing shared dictionary.
Pending prepared text remains local; it is not added to window/control requests.
After typing, `last_action` retains the exact entered text.

## Selection rounds and API batches

Window and control selection follow these steps:

1. Divide candidates into groups of up to 50, preserving observed order. Size
   each complete single-group request through the SDK, including shared state.
   If a proposed group exceeds 90,000 bytes, use its largest fitting contiguous
   candidate prefix. Keep all remaining candidates for later groups.
2. Give every group one `QUESTION` with only its candidates as criteria. There is
   no synthetic `none` response option.
3. A round with one actual group is final; for controls, add its action/timer
   questions. In every round, prepare the complete HTTP request through the SDK.
   If it fits within **90,000 bytes**, send one call. Otherwise pack its
   ordered questions into the largest contiguous batches that fit, keeping every
   group question whole and counting shared state and the envelope in each body.
4. Send the batches in parallel. Validate and retain each selected option,
   confidence, and probability distribution by its existing question ID.
5. After every batch succeeds, combine one winner per group in group order;
   regroup until one winner remains.

An initial singleton window bypasses Jev. A singleton control still receives the
selection question and, for a real window, its action Choice and both timers.
Every round uses the same
`QUESTION` wording; observed window context is already in shared state.

| Control round | Candidates entering | Groups / selection questions | Winners leaving |
| --- | ---: | ---: | ---: |
| 1 | 300 | 6 (50 candidates each) | 6 |
| 2 | 6 | 1 (6 candidates) | 1 |

When these groups fit, this uses two control rounds. The final round adds six
action Choices and twelve hypothetical timers, making 19 questions to measure
before packing.
Each round uses one call if its complete body fits, or parallel calls if it needs
multiple batches. Typing adds no Jev round or API call. Window selection with 17 fitting
candidates takes one round.
Once a real window is retained, later iterations have zero window rounds and
start fresh control rounds from their new snapshots.

For 963 fitting controls, the rounds are `963 -> 20 -> 1` (19 groups of 50 and
one of 13 in round 1); for 1,000, they are `1000 -> 20 -> 1`.
The byte budget can require smaller groups and extra rounds. It also determines
API call boundaries after all round questions are formed. Multiple calls within
a round remain one round, including parallel action/timer calls in the final round.

The shared SDK client dispatches batches through a thread pool. All batches are
submitted before waiting; the pool bounds the number in flight. Answers may
arrive in any order, but question IDs preserve their original group membership.
Questions within a round are independent. Later rounds depend on all earlier
group winners and start only after every batch response is validated. If any
batch fails, selection stops before advancing or acting on partial results.
Candidates are not silently removed.

## Final control selection, action, and timer

Each finalist receives an action Choice between `click` and `type_text`, using
its capabilities and the user's intended next step. Each also receives two
instances of `TIMER`: one assuming a click, one assuming text entry. Structured
context identifies the candidate, observed description, hypothetical action, and
app/window. Include them all before byte sizing; they may be packed into
different parallel calls. Questions cannot rely on other answers from this
round. Retain all responses but consume only the winning candidate's action and
the corresponding timer after every batch validates. This follows the existing
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

## Resolving and entering text

A nonempty prepared `text` string stays local and pending through click-only
iterations. The first actual typing dispatch uses it verbatim, whether Jev chose
an observed control such as a browser address bar or synthetic Windows Search.
The string is consumed at dispatch; errors do not cause retries or replay of the
prepared string. Typing makes no text-selection API call.
There is no field matching or multi-field text map. The original request, Jev
questions, and control/action selection remain unchanged.

Without pending prepared text, the selected typing operation returns `no_action`
with the reason `No prepared text is available for this typing operation (missing, empty, or already used).`
Windows Search checks this before activation, so it sends neither Win+S nor text.
The pipeline does not infer text from the request. Supplied Unicode, whitespace,
and line endings are preserved; a nonempty whitespace-only string is accepted.

For an observed control, the existing adapter sends native `type_text` with the
same snapshot-bound target used for clicking. A supplied `delivery_mode` of
`foreground` or `background` is passed directly to this existing CUA argument.
Without an override, the app explicitly sends `foreground` for these observed
process basenames: `chrome`, `chromium`, `msedge`, `brave`, `opera`, `vivaldi`, and
`firefox`. Matching uses the freshest existing `current_window.app_name`, is
case-insensitive, and permits an optional `.exe` suffix. Titles and window
classes are not used to infer a browser. Unknown and non-browser apps omit the
argument, preserving the native default. This policy applies only to targeted
typing; clicks and Windows Search desktop commands ignore the override.

CUA Driver 0.28.2's Windows background UIA path appends to the existing value.
The pipeline does not clear the field, select all, infer caret placement, submit, or
automatically retry typing with another delivery mode. Delivery uses the existing
native enum; no Jev delivery-mode question or native-driver default change is
needed. Unsupported controls still fail through the existing error path.

The native response's effect, delivery metadata, and readback facts remain in the
report and `last_action`. Successful targeted typing with `effect: "confirmed"`
or `"unverifiable"` continues through the normal settling wait and next fresh
observation when another iteration is allowed. The actual effect is never
relabeled. Explicit `partial` or `suspected_noop` effects still stop with
`observation_unavailable`; unknown or malformed native effects fail adapter
validation as `invalid_action_result`. Hard CUA errors still fail. Unverified delivery
does not automatically repeat typing or establish task completion. The next
iteration uses the existing observe/select/act loop, without an extra verification
stage, Jev question, or invented durable control identity.
Windows Search keeps its existing desktop action and observation behavior.

The selected native tool is retained as `Selection.action`. Final control traces
store `actions[candidate]` with its question key and chosen tool, and
`settling[candidate][action]` with its question key, Choice answer, and
milliseconds. Prepared text appears in the run inputs and native typing action
arguments. There is no text-selection round; missing text records an
`action_skipped` event with the reason above.

## Running and stopping

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.' --max-iterations 1
uv run --locked python src/main.py 'Open Chrome using Windows Search.' --text 'Chrome'
uv run --locked python src/main.py 'In my browser on x.com, open the History tab, then open Liked posts.' --text 'x.com'
uv run --locked python src/main.py --request-file request.txt --text-file text.txt
```

The Python API is `run_pipeline(request, driver, selector, max_iterations=10, text=None, delivery_mode=None)`.
`--max-iterations` and the Python argument must be positive integers, default 10.
`--text` and the Python `text` argument supply the exact string for the first
typing dispatch, whether targeted control typing or Windows Search. They do not
force a destination or action. Omitted text, Python `None`, and an empty string
leave no prepared text available for typing. Nonempty strings, including whitespace-only
strings, are retained verbatim with Unicode, whitespace, and line endings intact.
`--text-file` reads UTF-8 bytes without newline conversion and is mutually
exclusive with `--text`. Click-only iterations leave prepared text pending;
after one typing dispatch, later typing returns `no_action`.
Hermes interprets the user's intent/context into a concise English `request`
and may prepare `text` in the same initial `ctrlloop_run` call. The engine's
`original_request` is that supplied command, preserved throughout the run.
There is no additional text-generation call or callback; see the
[Hermes contract and trial prompt](hermes-plugin.md).
`--delivery-mode {foreground,background}` and Python's `delivery_mode` choose the
existing native mode for targeted control typing; omitting them uses the browser
policy above. The choice does not alter clicks or desktop Windows Search.
Callers can supply `sleep=` for deterministic tests. The connection check in
`scripts/check_jev.py` uses the production selector with one fictional control:
one selection, one action Choice, and two timers in one call, with production response validation
and no desktop action.

| Status | Meaning | CLI exit |
| --- | --- | ---: |
| `iteration_limit` | Configured action-cycle bound reached; overall completion unknown. | 2 |
| `no_action` | Prepared text missing, empty, or already used; text target disabled; or custom synthetic control lacks a live CUA target. | 2 |
| `no_windows` | No window candidates; normally unreachable with Windows Search enabled. | 2 |
| `no_controls` | Valid observation/synthetic definition has no indexed controls. | 2 |
| `observation_degraded` | Degraded read has no indexed controls. | 2 |
| `observation_unavailable` | Structured controls/snapshot identity unavailable or targeted typing with `partial` or `suspected_noop` effect. | 2 |
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
| Batching | Up to 50 window/control candidates per group, reduced when the complete single-group body would exceed 90,000 bytes. All rounds pack whole questions into bodies within that budget and dispatch parallel batches; action/timers included before sizing the final control round. |
| Window reuse | Retain the first selected real window for the run, with fresh control snapshots each iteration. Synthetic Windows Search permits another discovery/selection. No automatic replacement of a closed real window. |
| Prepared text | Used verbatim for the first typing dispatch, retained through clicks. Missing, empty, or already consumed text returns `no_action` when typing is selected. |
| Text delivery | Explicit native mode overrides the observed browser foreground default for targeted typing; unknown/non-browser targets retain the native default. No automatic mode retry. |
| Settling | One 50–500 ms scheduled wait after a control click or confirmed/unverifiable targeted text action when another iteration follows; none on the Windows Search route. |
| Iterations | Positive bound, default 10; no automatic completion judgment. |
| Model | Exact pin `jev-1.13.0`, validated on every response. |
| Action scope | Observed control clicks/targeted text entry and direct Windows Search activation/text entry; no coordinate fallback, automatic action retry, or general recovery planner. |
| Response validation | Exact question/option labels and Choice answer kinds; finite probabilities/confidence in [0, 1]; Choice totals 0.97–1.03; nonnegative integer token counts. |
| Confidence gating | Choices advance directly without ranking across groups. |

Controls require structured indexed observations and snapshot identities. Actions
use an observed element token or an element index with its snapshot ID. Old
targets are cleared before new observation; stale failures are not replayed.

There is no local token cap or total runtime cap.
Provider limits apply to shared state plus questions; see
[TypeSafe model documentation](https://docs.typesafe.ai/models). Observation and
description bounds and the 90,000-byte cap do not guarantee a batch fits those limits.
Automatic token-limit retries, description compaction, and splitting a
formed individual question are outside this implementation. Selection grouping
can reduce candidate counts before questions are fixed.

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
Each call also records its prepared `body_bytes`. `selection.grouping` includes
local sizing of proposed groups; `selection.packing` measures complete-round
preparation and batch sizing. Neither sends API requests. Parallel call durations
overlap, so their sum is not the round's elapsed time.

Each cycle retains iteration, selections, native results, rounds, and route.
Windows Search records both `hotkey` and `type_text` action events and timings
under that iteration. The final action and exact query remain in `last_action`;
there are no control-selection rounds or fake control records for this route.
Correlate repeated round names with `(iteration, round_id)` and span/request IDs.
Retain the original request, question text, all probabilities, action/timer answers,
prepared text, consumed timer, requested wait, and
measured durations. No evidence rounds or
confirmation traces are generated.

Collect data in memory, then construct and write the report once after the
operational result and cleanup. Do not copy/serialize accumulated history per
event. Measure report generation/writing separately and return completed write
duration on stdout rather than rewriting the report. Abrupt termination can lose
in-memory records. See [reading run timings](timing.md) for exact boundaries.
