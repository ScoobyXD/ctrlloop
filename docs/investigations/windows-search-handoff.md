# Windows Search handoff

## Application change

Fresh window discovery determines whether the synthetic Search opener is offered.
A visible, non-minimized `SearchHost.exe` window titled `Search` keeps its native
candidate and suppresses the opener. Closing Search makes the opener available
again on the next discovery. Custom synthetic replacement hooks retain their
existing meaning; availability is computed after those hooks.

If the opener wins immediately after the same query was typed, the existing
`last_action` state stops the run with `observation_unavailable` and an
`action_skipped` event. Neither Win+S nor typing is repeated. A control click
replaces `last_action`; a new run starts fresh. No persistent one-shot flag or
additional Jev state is introduced.

The launch sequence remains one desktop Win+S immediately followed by the exact
supplied text, on the existing persistent transport. There are no observations,
extra hotkeys, sleeps, or model calls between those actions. Native activation,
snapshot identities, click contracts, model pin, and selection batching remain
unchanged.

## Live evidence, 2026-09-23

The installed CUA driver was used without rebuilding, reinstalling, restarting,
or reconfiguring the shared daemon. The original checkout was read only. Its
frozen implementation snapshot was imported after SHA-256 verification and
`git apply --check`; all five untracked files were verified byte for byte.
Existing unrelated changes and deleted tests were preserved.

An activation probe used the production `CuaDriver`, `_search_windows`, and
`_activate_window` on one transport. Evidence is retained locally at
`reports/search-activation-check/experiment.json`.

| Operation | Duration | Observed result |
| --- | ---: | --- |
| Initial window discovery | 210.67 ms | Search absent |
| Win+S and immediate exact `slack` typing | 106.75 ms combined | Both native calls returned |
| Next window discovery | 201.35 ms | Visible Search observed |
| Search control read | 311.87 ms | 27 controls; query was `rslacke` |
| Existing real-window activation | 6.14 ms | `landed_on_target: true`; same foreground HWND |
| Window discovery after activation | 126.39 ms | Search remained visible |
| Fresh Search control read | 195.84 ms | 51 controls; Search remained readable |

Counts: one Win+S, one typing call, one activation, zero clicks, zero Jev API
calls, zero window-selection rounds, and zero control-selection rounds. The
probe did not run application decision loops. Its combined pair timing does
not establish separate per-command durations.

This evidence supports keeping the current activation behavior: activating the
already-open Search window did not dismiss it. It does not prove behavior when
Search loses focus or disappears during model selection. Native failures still
stop without reusing a stale handle or token.

Applying the new candidate logic to these saved native observations yields
13 candidates before opening (12 native plus the opener), and 13 after opening
(13 native, including real Search, with no opener).

## Remaining live validation

A bounded two-loop application check was prepared with the pinned Jev model and
guards refusing any activation outside observed Search or any click except a
fresh Slack app result. Initial automatic approval review rejected the run;
after verifying the source user's authorization and narrowing those guards, the
revised run was approved.

Its setup stopped before any Jev call or application loop: Search contained
`hatev`, and a native accessibility request to clear that box returned
`effect: unverifiable`; the fresh read still showed the same value. No blind
input retry followed. Evidence is at
`reports/search-lifecycle-check/experiment.json`. A quiet keyboard interval was
requested before further live input.

Consequently, the full clean-query application route, Slack-result click,
result-driven Search dismissal, and subsequent fresh launcher availability
remain unproven live. The prior `rslacke` value also means the activation probe
cannot establish that the exact sent query became the whole Search-box value.
The required two-command sequence was preserved rather than inserting clearing
or readiness operations between its commands.

Python compilation and incremental whitespace checks passed. Independent review
found no remaining actionable source issues. No automated tests were added.
