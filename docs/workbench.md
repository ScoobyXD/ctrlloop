# Manual action workbench

The workbench runs explicitly authored desktop actions through the existing worker, Jev selection rounds, Cua Driver, and run evidence APIs. It does not invoke an instruction planner. The normal CtrlLoop flow and the separate Hermes/CUA compatibility launch remain available.

## Open and use

Run the application with the existing [website setup](mvp.md) and [environment configuration](environments.md). After signing in, choose **Action workbench** in the sidebar, or open `/#workbench` on that application origin. The desktop viewer remains beside the workbench. Saved workbenches appear in the normal loop history. Platform devadmins choose the company and person from the application header; that person's assigned desktop is used for new runs.

1. Give the sequence a name and choose an action for the first step: **Click**, **Type**, **Press key**, or **Read text**.
2. For Click and Read text, enter an **Instruction**, such as `Browser address bar` or `Text of the first post`. Jev resolves that target against a fresh observation. Type sends the exact supplied text. Press key sends an explicit key such as `Enter`.
3. Type and Press key use current focus by default. Expand **Use current focus** to set an optional target that should be resolved first.
4. Use **Add step**, the up/down buttons, and the remove button to edit the sequence. The selected step's editor is indented beneath the list. Changes save automatically, including unfinished steps; **Save** is available to save immediately or retry a failed save.
5. Click the marker beside a step to place the starting arrow there. Selecting a row for editing or inspection does not move the starting arrow. Adding a step selects it and moves the arrow to that new step.
6. With your assigned desktop online and in agent mode, choose **Run from this point** to execute the marked step and every following step, or **Run only this step** to execute the marked step and finish the run. Both save pending edits before starting. Execution uses the desktop's current state; moving the marker does not restore an earlier desktop state.
7. **Pause** and **Stop** take effect at step boundaries; an already dispatched native command finishes first. While paused, **Next step** executes one more step and pauses again if more remain, while **Continue** executes the rest of the selected range.

For example, author Click `Browser address bar`, Type `x.com`, and Press key `Enter` as three separate steps. Typing uses the focus established by the click. Add additional targeted Click or Read text steps explicitly as needed.

For a six-step sequence, placing the arrow at step 3 and choosing **Run from this point** executes steps 3–6. Placing it at step 5 and choosing **Run only this step** executes step 5, then releases the workbench for editing when completion and evidence are saved. You can add another step and run just that new step against the desktop left by the preceding run.

Editing is locked during a queued, running, or paused run. Stop the run before editing its draft. Completed steps are not replayed when a paused run continues. **Recorded runs** in the workbench header lets you inspect the exact full step snapshot from any recorded run, including the latest. Steps outside that run's selected range are labeled **not run**. Choose **Current draft** there to return to editing. Edited steps are labeled separately from their earlier outcomes; their changes apply to the next run.

## Saving and scope

Edits are saved to the existing company-scoped loop record after a short typing pause. Empty instruction, typing-text, or key fields can be saved as drafts. Starting a run validates every step it will execute; an unfinished step outside the selected range does not prevent execution. Saving never sends a desktop action.

The editor also keeps a recovery copy in browser storage, scoped to the signed-in account, selected company, selected person, and workbench. The header distinguishes **SAVING…**, **SAVED**, **DRAFT ON THIS DEVICE**, and **UNSAVED**. A failed account save keeps the local copy when browser storage is available and displays the failure with a retry through **Save**. Reloading or returning to the same workbench and scope recovers pending edits. A version conflict preserves those edits rather than silently overwriting a newer account copy.

Run controls and the automatically displayed latest run follow the selected person's assigned desktop. **Recorded runs** provides explicit access to the workbench's saved history within the current company. Company members can execute a saved shared sequence on their own desktop and inspect its recorded runs; editing the shared sequence requires its author or a platform devadmin.

## Results and inspection

A successful native step means its command was sent and returned an acceptable native delivery outcome. The active row shows a spinner and **running**, then **Complete** after success. **Effect unverified** remains visible when the driver could not verify the application's resulting effect. Sequence completion does not establish that a broad user goal was achieved. Driver refusals, incomplete input, unavailable observations, and transport or execution failures stop the sequence at that step.

Executed rows have a compact **Timing** disclosure, expanded by default. Collapse it to hide the graph; that choice is retained as progress updates arrive. Each horizontal bar divides the recorded step duration into nonoverlapping processes, including window/control acquisition, individual Jev selection rounds, focus, action, settling, and overhead. Hover or keyboard-focus a segment or its legend label to see the step, process duration, input tokens, and dollar cost at **$0.042 per million input tokens**. Missing usage or timing remains explicitly unavailable. Rows that have never run have no timing disclosure.

Running individual steps retains the most recent execution evidence on the other rows for the current person and desktop. Timing includes the recorded run timestamp; editing an action labels its prior outcome **edited**. Selecting a recorded run restricts every row to that exact run. The spinner follows execution independently of the selected editor row.

Read text returns literal text from the selected accessible control and its observed descendants. It displays that text in the inspector with its source and retains the contributing accessibility fields. It does not invent text, run OCR, or assert that inaccessible, truncated, or off-screen content was read. Empty readable output stops the step with an explanation.

The workbench header contains run status, completed steps within the selected range, duration, Jev calls, input tokens, cost, **Inspect selected step**, and **Recorded runs**. The live desktop header contains the desktop owner's name, **View only** / **Return to agent**, viewer status, fullscreen, and a **Connection details** dropdown containing the current company, assigned worker, and models. The inspector progressively reveals:

- The authored action, actual native arguments and dispatch, return value, and failure details.
- Observed windows, controls, candidates, and accessibility coverage separately from the minimal state actually sent to Jev.
- Window and control rounds, candidate reductions, groups, exact existing question wording, call inputs, returned choices, probabilities, and available call durations.
- Stage, round, step, and run timing, timestamps, native-command markers, and loop boundaries. An authored step ID remains separate from its low-level loop number.
- Per-call input tokens and estimated input cost at **$0.042 per million input tokens**, with no output-token charge. Missing accounting is labeled unavailable; partial totals identify the unavailable remainder.

Step evidence is delivered at boundaries, including while a sequence is paused. Large live records may identify a capture gap until the final archive is uploaded. **Full timeline & report** retains the existing shared-scale loop chart and full report; **Evidence ZIP** downloads the existing authenticated evidence bundle. **Retry evidence** retries a failed read of the report without executing an action.

## Execution and validation limits

The backend owns the execution cursor, immutable run snapshot, versioned run controls, and an idempotent start request. The existing `POST /api/loops/{id}/runs` accepts a zero-based `start_step` and `mode: "sequence"` or `mode: "single"`, together with the existing unique `request_id`. Each run preserves the entire authored snapshot and original step IDs and indices, plus an inclusive `start_step` and exclusive `end_step`. Only the selected range may be dispatched or counted toward completion. Existing `mode: "step"` retains pause-after-step behavior for compatibility; `single` finishes the run after the one selected step.

Repeated controls cannot claim the same step twice. The worker journals the active step and its completion. Lost delivery acknowledgments, crashes, or uncertain native dispatch do not automatically replay desktop input. Existing workers with `manual_actions_v1` already follow the server-owned cursor and completion boundary; workers without that capability fail closed on manual sequences. Updated workers also record the selected range in report metadata.

This implementation uses the existing Jev model pin and authored questions. Explicit step actions determine the tool; Jev resolves the target. Current-focus Type and Press key require no Jev target-selection call. The compact existing `last_control` context carries the previous selected window/control into the next step, including after reads; this is not a variable-binding or general planning system. No additional Jev question wording or higher-level planner is introduced.

Local backend verification for selected-range execution included Python compilation/imports and isolated handler checks for starting at step 3, running only step 5, preserving full snapshots and original indices, rejecting incomplete selected steps, completing at the selected boundary, and rejecting completion without the required results. These checks did not use a live database, Jev calls, or assigned Omarchy actions. No automated test files were added.

Browser verification used a local preview with simulated API/run outcomes. It covered moving the start marker, starting at step 3, running only an added step, automatic saving of new and unfinished steps, reload recovery, leaving and returning during pending edits, and header inspection/history controls. No live desktop was connected. JavaScript syntax and diff whitespace checks passed.

The timing UI was also inspected in a local simulated preview with multiple selection rounds, known and missing token usage, direct actions without Jev, and unexecuted rows. Segment details, column containment, collapse persistence during rerendering, and the running-row indicator were checked in the browser. These checks do not establish live worker connectivity or deployment.
