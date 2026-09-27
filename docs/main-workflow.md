# Omarchy desktop workflow

The caller supplies the original request, an existing Fleet claim reference, optional HTTP(S) URL, optional exact typing text, and an iteration bound. [`src/main.py`](../src/main.py) preserves cached Hermes launches and otherwise dispatches to [`src/cli.py`](../src/cli.py). The default claim reference is `.local/omarchy-fleet/claim.json`; TypeSafe and Fleet credentials default to `.env` and `.env.fleet` respectively.

## Connection and ownership

[`CuaDriver`](../src/driver.py) acquires a nonblocking OS lock for this checkout and claim, reconnects using `Sandbox.from_dict()`, and initializes the claim's native Driver MCP service. It requires upstream Driver `0.24.0`, reads its advertised tool schemas, and starts a new Driver session for this command. It uses the SDK's existing claim representation and native Driver dictionaries rather than a second worker or control model.

A second local command using the same claim fails with `desktop_in_use`. This lock does not coordinate other machines or a noVNC user. Manual viewer input must not overlap an agent run. Stop the agent before taking control; a subsequent command starts with a fresh observation. Coordinated website takeover and resume remain future work.

When supplied, `--url` calls Driver's `launch_app` with that URL in the remote default browser. Without it, the command continues the desktop's existing state. The loop neither launches a host browser nor imports its login state.

## One iteration

1. Call native `list_windows` for visible windows. Build existing `Candidate` objects from the returned identities and compact observed descriptions.
2. Run the existing Jev window-selection rounds through `ChoiceSelector`. Bring the chosen window forward inside the private VM with `bring_to_front`.
3. Call `get_window_state` for that exact window. Retain the returned snapshot ID and native element records. Missing snapshot-bound controls stop the run. A degraded result containing only window/application metadata returns `observation_unavailable`; an empty control list returns `no_controls`, without claiming that the application has no controls.
4. Build control candidates from observed role, label, state, value, frame, and indexed ancestry. Reuse the existing control-selection, speculative click/type, and settling questions. Native records retain action identity; Jev receives compact descriptions.
5. Save a before-action desktop PNG when enabled, then dispatch at most one `click` or `type_text`. Bind it to the selected window and native element token, or to the native element index plus snapshot ID. Use explicit `delivery_mode="foreground"` inside the guest. Do not replace the control snapshot between selection and dispatch.
6. Record the native action result and action-return boundary, then save an immediate after-action PNG. Driver effects `confirmed` and `unverifiable` permit continuation through fresh observation and selection; `unverifiable` does not establish that the intended effect occurred. Partial effects, suspected no-ops, malformed responses, and refusals stop through `action_unconfirmed` or a Driver error. Never automatically replay the native mutation or substitute a coordinate action.
7. If another iteration is allowed, wait the selected settling duration, then discover windows and controls again. Otherwise return `iteration_limit` without claiming completion. The final permitted action has no extra settling wait.

[`src/pipeline.py`](../src/pipeline.py) owns these steps. Jev selection runs in a worker thread so the async Driver connection remains responsive. Window discovery and selection repeat each iteration; native control identities are never carried into a later observation for execution.

## Selection contract

The model remains **`jev-1.13.0`**. A group contains at most **10 candidates**, shrinking when necessary to fit the **90,000-byte** request ceiling. Each round batches whole questions; a fitting round uses one call, while larger rounds use multiple batches whose winners recombine before the next round. The ceiling bounds serialized request bytes rather than token count.

All window and control groups reuse the existing authored selection question. The final control round includes existing action and settling questions. The executable decision surface is click and type; this migration does not add questions for completion, scrolling, generated text, application search, or other decisions.

Prepared typing text is supplied by the caller and is not generated or rewritten. Nonempty text survives click-only iterations and is consumed before its first typing dispatch, including uncertain delivery. Whitespace is preserved. Missing, empty, or already-consumed text returns `no_action` if typing is selected. Native Driver behavior determines insertion; the loop does not issue an implicit clear/replace action.

## Reporting and stop conditions

Each run writes result JSON to stdout and records details under ignored `reports/`. Reports include window and control candidates, native observations/results, selection rounds, exact Jev questions and responses, probabilities, per-call timings and token counts, and input-token cost at `$0.042` per million input tokens. Caller-supplied text and observed application text can appear in these local artifacts.

The CLI enables before/after desktop screenshots by default; `--no-screenshots` disables them. PNGs never enter Jev state. Screenshot capture/write timings are separate spans, and capture failures remain diagnostics rather than action failures. The `action_requested` event records the pre-call boundary; `loop_action_finished` records action return or error. The enclosing iteration span also contains reporting and screenshot work, so it must not be interpreted as only native input delivery time.

Transport uncertainty, stale identities, schema mismatches, and Driver refusals end the current run. Degraded metadata-only observations stop before control selection. A valid `unverifiable` effect can continue to a fresh observation and new selection when iterations remain, as can `confirmed`; neither triggers automatic action replay. Partial or suspected no-op effects stop without replay. `iteration_limit` and other bounded stops do not establish successful task completion. Exit code `1` denotes errors, `observation_unavailable`, or `action_unconfirmed`; exit code `2` denotes a stop without established completion. A native `unverifiable` effect alone is not an error or proof of completion.

## Cleanup and worker lifetime

Normal return, ordinary errors, and Ctrl+C end this command's Driver session, disconnect the transport, and release its local claim lock. They do not release, renew, delete, or resize the Fleet worker. The OS also releases the local lock after process death; an abrupt process termination may prevent remote session cleanup.

Provisioning, viewer setup, expiry, and deletion remain in the separate [Fleet trial helper](omarchy-fleet-trial.md). The CLI prints that worker's existing authenticated noVNC URL; it does not run a local preview server. The current image/Driver trial has a six-hour lifetime. Saved references do not extend it or preserve files after worker deletion.

Existing host `.local/browsers` profiles remain on disk but are unused by this runtime. The Browser Use startup entry and implementation were removed. The [preserved Hermes/CUA demo](hermes-plugin.md) continues through its separate runtime and narrow compatibility launch.
