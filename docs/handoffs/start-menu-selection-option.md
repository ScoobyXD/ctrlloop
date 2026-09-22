# Add a synthetic Start menu option to Jev's window selection

Historical handoff for the synthetic-selection milestone. The current
[main workflow](../main-workflow.md) supersedes its descriptions of reporting,
request batching, and single-pass behavior; synthetic Start/Search execution
remains deferred.

Work in `C:\Users\Jonat\ctrlloop`. Read the repository's `AGENTS.md` instructions and inspect the existing code before editing. Preserve unrelated uncommitted work.

Implement a selection-only milestone: offer Jev an explicit option named **Open Start menu (Windows Search)** alongside the observed windows. The target is the Windows Start menu, with the search field across its top. Do not execute that action yet.

## Context

The current pipeline lists windows, runs a tournament of Jev Choice questions, reads the winning window, then selects one of its controls. Each group has at most ten options. Independent groups are batched into one request where budgets permit; group winners compete in subsequent rounds.

The seven real runs currently saved in `reports/` contain no Start menu or Windows Search entry among their observed windows or first-round window choices. An additional saved report is an offline fixture verification, and one run has both JSONL and formatted JSON copies. A Search menu item in the Notepad++ control choices is unrelated to Windows Search. These reports do not establish whether the driver can expose Start/Search when open. This task explicitly introduces a future route rather than claiming discovery of a real window.

A subsequent separate worktree investigation verified that the open Start menu is discoverable and its CUA snapshot contains 80 controls, including an enabled Search box with advertised text/value actions. The closed taskbar exposes a real Start button through direct UIA, but this driver's window enumeration excludes the taskbar's empty caption. The synthetic option therefore represents an opening route; once Start is actually open, its real window can be discovered normally.

## Required behavior

- Insert exactly one synthetic option into the initial window-selection candidate list, positioned within the first group. Count it toward the existing ten-option limit and preserve the relative order of real windows.
- Give it a stable, distinct key and a concise description explaining that opening the Start menu provides access to Windows Search for applications that may not already be open. Do not imply that it guarantees finding the requested application.
- Advance it through the same tournament as any other candidate. Do not insert another copy in later groups, requests, or rounds. Do not treat a preliminary group win as the final selection.
- Keep the original user request unchanged as Jev's shared state. Preserve the pinned model, question batching, request budgets, and selection rules.
- If a real window wins, preserve the existing window-read and control-selection behavior.
- If the synthetic option wins the final selection, return a clearly distinguished action selection and stop. Do not fabricate a PID, window ID, snapshot, or observed window record. Do not call `get_window_state` for this option or report that Search was opened or the user's goal was completed.
- Handle zero observed windows: the one synthetic candidate can use the existing singleton-forwarding behavior and produce an action selection without a Jev request.
- Reuse the existing `Candidate` and selection machinery. Extend the existing result structure minimally and consistently; do not introduce a second near-identical selector, API, or result model.
- Preserve the indented per-run JSON reports and their incremental saving. The reports must retain the actual observed window list, exact Jev request/response fields, probabilities when returned, and the final action selection. Never label the synthetic option as an observed window or an executed action.

## Scope and validation

No actual clicking, keyboard input, query extraction, searching, application launch, or general action loop is part of this change. Future execution will need a driver action dispatcher and appropriate transport lifetime; the current adapter only permits reads.

Inspect `src/pipeline.py`, `src/selection.py`, `src/main.py`, `src/reporting.py`, and their tests. Add focused coverage for first-group insertion, group size, normal tournament advancement without reinsertion, a final synthetic winner, a real-window winner, and no observed windows. Reuse existing tests where appropriate. Run the relevant suite and summarize the actual changes and validation.
