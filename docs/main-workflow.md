# Main workflow

This is the authoritative description of the Jev-driven control loop in
`src/pipeline.py`, its selector, reporter, and CLI. Each iteration discovers
windows and selects a window. A real window goes through control reading,
selection, a click or targeted text entry, and settling. The synthetic Windows
Search destination resolves its query first, then sends Win+S and immediately
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
- `TEXT_WORD`, a Noul, asks whether each source word belongs in the text for the
  current destination. All words are evaluated in text round `text_r1` only when
  text needs to be resolved. Its whole questions use the same request budget and
  parallel batching as window/control selection.

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
control action/timer and every word Noul. Each call repeats
its state and envelope, so count those bytes
again for each call. HTTP headers and transport framing are outside the budget.
Indented report-file sizes differ from the outgoing HTTP body sizes. Local sizing
sends no API calls and creates no HTTP request events.

Window/control selection and text selection reuse the same packing and parallel
dispatch helpers. Complete all local packing before dispatching any batch. If
the shared state plus one whole question exceeds the budget, fail that round
before any of its API calls are sent. A question is atomic: do not
split a Choice group's candidates, remove words, or trim the original request.

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
| Candidate | One observed or synthetic window/control option, or one source word in the text round. |
| Group | Up to 10 candidates considered together. |
| Group question | One `QUESTION` selecting a window/control candidate from a group. |
| Round | Evaluate all questions, then advance selected candidates after every batch succeeds. The single text round evaluates all source-word Nouls. |
| API call | One request with a batch of whole questions and a complete body of at most 90,000 bytes; questions can be group selections, final action/timers, or source-word Nouls. |
| Stage | Window discovery/selection, activation, control acquisition/selection, text selection, or action dispatch. |
| Iteration | One decision cycle with fresh window discovery, selection, observation, and action when available. All calls in the cycle share its iteration number. |
| Loop boundary for a timeline | Starts at current-state acquisition and ends when the last CUA command is sent: the selected control's click/text action or Windows Search text entry. Native return and any settling appear afterward, before the next loop starts. |

The report's `loop.iteration` span encloses the complete decision cycle, including
native return and any settling wait. Its duration therefore differs from the
user-facing timeline's dispatch-bounded loop duration. A terminal cycle without
an action is a stopped cycle, not a completed action.

## Each iteration

Before the first iteration, split the original request into whitespace-delimited
words once, preserving their character offsets and order. Reuse these local
source spans throughout the run; do not regenerate or rewrite the request.

1. Keep the user's exact original request in state and set this iteration number.
2. Discover windows and build their candidate descriptions. Refresh the previous
   current window if still observed; otherwise clear it. A visible, non-minimized
   `SearchHost.exe` window titled `Search` suppresses its synthetic opener, keeping
   the real window available for selection. Recompute this on every discovery.
3. Select the next window with `QUESTION`. This happens again every iteration,
   allowing the next action to move to another window.
4. For Windows Search, use the exact explicit `--text` when supplied; otherwise
   assemble all words meeting the inclusion threshold. No selected words
   returns `no_action` before activation. Send desktop Win+S, then immediately send
   desktop `type_text` with the resolved string, and skip to the next iteration
   (or return `iteration_limit` at the configured bound). No control read,
   control selection, timer, observation, confirmation, or sleep intervenes.
   If the previous action was typing the identical Search query and Search was
   not observed, return `observation_unavailable` without replaying either command.
   For a real window, bring it forward and read its accessibility tree.
   Preserve the observation identities needed to execute the selected control.
5. Build control candidates once and narrow them with `QUESTION`. Only in the
   final control round, include an action Choice and a hypothetical timer for
   each of `click` and `type_text` per finalist. Consume the winning candidate's
   action and its matching timer.
6. For `type_text`, assemble all words meeting the inclusion threshold while
   preserving previous action/control history. No selected words returns
   `no_action`. Immediately before native dispatch, record the selected control,
   action, and source window as the latest attempted action. Execute the selected
   native CUA action using the original snapshot target and retain its result. Unconfirmed
   targeted typing stops with `observation_unavailable` to avoid automatic retyping.
7. At the iteration limit, return `iteration_limit` without another wait or
   observation. Otherwise, wait the winning control/action's timer interval after CUA
   returns, then begin the next iteration.

Empty, unavailable, or failed observations/actions and unresolved text can stop earlier.
Old executable targets are not reused for a new iteration. There are no separate
routing, verification, retry-action, or extra-wait questions.

The synthetic Windows Search route has one window candidate and no control
candidates. It uses the existing activation stage for
`hotkey({scope: "desktop", keys: ["win", "s"]})`, followed directly by
`type_text({scope: "desktop", text: <resolved text>})`. Both commands belong
to the same iteration. This route makes zero control-selection rounds and zero
timer requests, and adds no settling wait after typing. Without an explicit
Search override, its complete text-selection stage finishes before Win+S. The next normal
iteration reads fresh state and can select real search results. Real Start and
Search windows remain observed window candidates; the synthetic route does not
replace them. After a result click closes Search, the next discovery offers the
opener again. The existing `last_action` protects against consecutive identical
launch/type actions when the destination cannot be observed; a control click
replaces that state, and a new run starts with no previous action. This is an
observation failure stop, not a permanent ban on searching again.

## Shared Jev state

Every window and control request carries the same run context:

| Field | Meaning |
| --- | --- |
| `original_request` | The user's exact original text, without rewriting, trimming, or shortening. |
| `iteration` | Current decision cycle, starting at 1; it does not count completed clicks. |
| `current_window` | Observed app/title metadata, refreshed from discovery and changed when a new window is selected; `null` before selection or when the old window is absent. |
| `last_control` | Previous dispatched control as `{window, control, action}`, or `null` before control dispatch. Retain it during text resolution; update immediately before dispatch. |
| `last_action` | Previous tool and filtered native delivery/result facts, available after an action attempt. Text entry also retains the exact entered text. |

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
Source words remain local until text resolution; they are not added to every
window/control request. Text requests add only their `text_target` context and
word questions to the existing state.

## Selection rounds and API batches

Window and control selection follow these steps:

1. Divide candidates into groups of up to 10, preserving observed order.
2. Give every group one `QUESTION` with only its candidates as criteria. There is
   no synthetic `none` response option.
3. Include final control action/timer questions and prepare the complete HTTP request through
   the SDK. If it fits within **90,000 bytes**, send one call. Otherwise pack its
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
| 1 | 392 | 40 | 40 |
| 2 | 40 | 4 | 4 |
| 3 | 4 | 1 | 1 |

This uses three control rounds. The final round adds four action Choices and
eight hypothetical timers, making 13 questions to measure before packing.
Each round uses one call if its complete body fits, or parallel calls if it needs
multiple batches. If text wins, its stage adds one word round packed under the
same budget. Window selection with 17 candidates adds two rounds: 17 become
two winners, then one. The next iteration begins new window/control rounds.

For 631 controls, the rounds are `631 -> 64 -> 7 -> 1`; for 647, they are
`647 -> 65 -> 7 -> 1`; for 1,000, they are `1000 -> 100 -> 10 -> 1`.
The prepared byte sizes determine API call boundaries, not a fixed candidate
count. Multiple calls within a round remain one round.

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

Text resolution reuses the original request's ordered word spans. In `text_r1`,
one `TEXT_WORD` Noul per word asks whether it belongs in the text for the selected
control or Windows Search destination, with the original request and current
action context. Evaluate **all source words in that one round**, using the same
complete-body byte sizing, whole-question packing, and parallel dispatch helpers
as selection. A fitting round uses one API call; a larger round uses parallel
calls, each counting the full shared state and envelope. No word is omitted or
converted into a ten-word Choice group. Collect and validate every batch before
deciding inclusion or dispatching text. No text call is needed after a click
choice or for an explicit Windows Search `--text` override.

Include every word whose Noul probability is **at least 0.80 (`>= 0.80`)**,
including exactly 0.80. Preserve source order even when selected words are
separated by omitted words. The threshold determines the final selected words;
there is no second Jev question, best-span choice, or span-length limit.
No selected words returns `no_action`. A request or response failure remains an
error and cannot dispatch partially resolved text.

Copy selected words verbatim using their source offsets, preserving punctuation,
URLs, and repeated-word identity. Preserve the original whitespace between
adjacent selected words. When omitted words separate two selected words, join
them with one space. For example, selecting `red` and `blue` from
`red and blue` produces `red blue`; selecting both from a tab-separated source
preserves the original tab. No quotes or punctuation are removed, no site name
becomes a URL, and no words absent from the request are generated. Local byte
packing happens before the round's requests are sent; a later provider rejection
remains an error, without automatic smaller retries or truncation.

For an observed control, the existing adapter sends native `type_text` with the
same snapshot-bound target used for clicking. CUA Driver 0.28.2's Windows
background UIA path appends to the existing value. The pipeline does not clear
the field, select all, infer caret placement, switch to foreground delivery, or
retry typing. Controls and editors unsupported by this path are not universal
text-entry coverage.

The native response's effect and readback facts remain in the report. Targeted
typing continues only with a confirmed effect; otherwise the pipeline stops
with `observation_unavailable`. A confirmed effect does not mean the overall
task is complete. A later iteration makes the existing fresh observation; no
extra Jev verification question or invented durable control identity is added.
Windows Search keeps its existing desktop action and observation behavior.

The selected native tool is retained as `Selection.action`. Final control traces
store `actions[candidate]` with its question key and chosen tool, and
`settling[candidate][action]` with its question key, Choice answer, and
milliseconds. The text stage uses the same `Selection` and `api_calls` records,
including packing, batch, and prepared `body_bytes` information. `text_r1` retains
every Noul probability, `threshold: 0.80`, `threshold_comparison: ">="`,
`selected_indices`, `selected_offsets`, and assembled `text` or an unresolved
`reason`. Its optional `source_span` records the exact source range only when the
selected words were contiguous; it is omitted for disjoint selections. Text
selection is one stage with one word round, separate from window/control rounds.

## Running and stopping

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.' --max-iterations 1
uv run --locked python src/main.py 'Open Chrome using Windows Search.' --text 'Chrome'
```

The Python API is `run_pipeline(request, driver, selector, max_iterations=10, text=None)`.
`--max-iterations` and the Python argument must be positive integers, default 10.
`--text` and the Python `text` argument supply the exact query for the Windows
Search route; they do not force its selection. Explicitly empty or whitespace-only
strings are rejected. Valid override text is retained verbatim, without
rewriting, trimming, or line-ending normalization. Without that override, Search
uses the same source-word selection as observed control text entry. The override
does not replace the text selected for other fields.
Callers can supply `sleep=` for deterministic tests. The connection check in
`scripts/check_jev.py` uses the production selector with one fictional control:
one selection, one action Choice, and two timers in one call, with production response validation
and no desktop action.

| Status | Meaning | CLI exit |
| --- | --- | ---: |
| `iteration_limit` | Configured action-cycle bound reached; overall completion unknown. | 2 |
| `no_action` | No request words meet the text threshold, text target disabled, or custom synthetic control lacks a live CUA target. | 2 |
| `no_windows` | No window candidates; normally unreachable with Windows Search enabled. | 2 |
| `no_controls` | Valid observation/synthetic definition has no indexed controls. | 2 |
| `observation_degraded` | Degraded read has no indexed controls. | 2 |
| `observation_unavailable` | Structured controls/snapshot identity unavailable, repeated unobserved Search query, or targeted typing unconfirmed. | 2 |
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
| Batching | Up to 10 window/control candidates per group. All rounds pack whole questions into HTTP bodies of at most 90,000 bytes and dispatch parallel batches; action/timers included before sizing the final control round; one Noul per source word in `text_r1`. |
| Text assembly | All words meeting `>= 0.80` are typed in source order. Adjacent selected words retain their source whitespace; omitted-word gaps become one space. |
| Settling | One 50–500 ms scheduled wait after a control click or confirmed targeted text action when another iteration follows; none on the Windows Search route. |
| Iterations | Positive bound, default 10; no automatic completion judgment. |
| Model | Exact pin `jev-1.13.0`, validated on every response. |
| Action scope | Observed control clicks/targeted text entry and direct Windows Search activation/text entry; no coordinate fallback, automatic action retry, or general recovery planner. |
| Response validation | Exact question/option labels and expected Choice/Noul answer kinds; finite probabilities/confidence in [0, 1]; Choice totals 0.97–1.03; nonnegative integer token counts. |
| Confidence gating | Choices advance directly without ranking across groups; word Nouls use `>= 0.80` for inclusion. |

Controls require structured indexed observations and snapshot identities. Actions
use an observed element token or an element index with its snapshot ID. Old
targets are cleared before new observation; stale failures are not replayed.

There is no local token cap or total runtime cap.
Provider limits apply to shared state plus questions; see
[TypeSafe model documentation](https://docs.typesafe.ai/models). Observation and
text bounds and the 90,000-byte cap do not guarantee a batch fits those limits.
Automatic token-limit retries, description compaction, and splitting an
oversized individual question are outside this implementation.

## Timing and reporting

Capture monotonic boundaries for startup, discovery, candidate preparation, each
selection stage/round, text selection, foreground activation, control acquisition, native action,
settling, and cleanup. UTC timestamps add wall-clock context. Nested spans overlap;
do not add parent and child durations. The report identifies its timing origin.

API duration begins immediately before transport receives the prepared request
and ends when the complete response body arrives. It includes transport, network,
and service work; it excludes request construction and response decoding. It is
client-observed duration, not isolated inference time. All questions in a batch
share that call's duration. Each round retains an `api_calls` list, including
single-call rounds, with separate answers, usage, and timing references per call.
Each call also records its prepared `body_bytes`. `selection.packing` measures
local SDK preparation and sizing. Parallel call durations overlap, so their sum
is not the round's elapsed time.

Each cycle retains iteration, selections, native results, rounds, and route.
Windows Search records both `hotkey` and `type_text` action events and timings
under that iteration. The final action and exact query remain in `last_action`;
there are no control-selection rounds or fake control records for this route.
Correlate repeated round names with `(iteration, round_id)` and span/request IDs.
Retain the original request, question text, all probabilities, action/timer answers,
word-inclusion answers, assembled text, consumed timer, requested wait, and
measured durations. No evidence rounds or
confirmation traces are generated.

Collect data in memory, then construct and write the report once after the
operational result and cleanup. Do not copy/serialize accumulated history per
event. Measure report generation/writing separately and return completed write
duration on stdout rather than rewriting the report. Abrupt termination can lose
in-memory records. See [reading run timings](timing.md) for exact boundaries.
