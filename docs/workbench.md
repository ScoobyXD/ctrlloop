# Manual action workbench

The workbench runs explicitly authored desktop actions through the existing worker, Jev selection rounds, Cua Driver, and run evidence APIs. It does not invoke an instruction planner. The normal CtrlLoop flow and the separate Hermes/CUA compatibility launch remain available.

## Open and use

Run the application with the existing [website setup](mvp.md) and [environment configuration](environments.md). After signing in, choose **Action workbench** in the sidebar, or open `/#workbench` on that application origin. The existing desktop viewer remains beside the workbench. A saved workbench also appears in the normal loop history.

1. Give the sequence a name and choose an action for the first step: **Click**, **Type**, **Press key**, or **Read text**.
2. For Click and Read text, describe the target, such as `Browser address bar` or `Text of the first post`. Jev resolves the target against a fresh observation. Type sends the exact supplied text. Press key sends an explicit key such as `Enter`.
3. Type and Press key use current focus by default. Expand **Use current focus** to set an optional target that should be resolved first.
4. Use **Add step**, the up/down buttons, and the remove button to edit the sequence. **Save** stores it without executing.
5. With your assigned desktop online and in agent mode, choose **Step** to execute the next step and pause, or **Run sequence** to continue through the sequence. Both save pending edits before starting.
6. While paused, select a step and open **Inspect selected step**. Choose **Next step** or **Continue** when ready. **Pause** and **Stop** take effect at step boundaries; an already dispatched native command finishes first.

For example, author Click `Browser address bar`, Type `x.com`, and Press key `Enter` as three separate steps. Typing uses the focus established by the click. Add additional targeted Click or Read text steps explicitly as needed.

Editing is locked during a queued, running, or paused run. Stop the run before editing its draft. A new run starts at step 1; completed steps are not silently replayed when a paused run continues. **Recorded runs** lets you inspect the exact step snapshot from any run, including the latest. Choose **Current draft** there to return to editing. Edited steps are labeled separately from their earlier outcomes; their changes apply to the next run.

Run controls follow the selected person's assigned desktop. Company members can execute a saved shared sequence on their own desktop and inspect its recorded runs; editing the shared sequence requires its author or a platform devadmin.

## Results and inspection

A successful native step means its command was sent and returned an acceptable native delivery outcome. **Sent · unverified** means the driver could not verify the application's resulting effect. Sequence completion does not establish that a broad user goal was achieved. Driver refusals, incomplete input, unavailable observations, and transport or execution failures stop the sequence at that step.

Read text returns literal text from the selected accessible control and its observed descendants. It displays that text in the inspector with its source and retains the contributing accessibility fields. It does not invent text, run OCR, or assert that inaccessible, truncated, or off-screen content was read. Empty readable output stops the step with an explanation.

The default screen has a short step list, editor, run controls, status, and modest accounting. The inspector progressively reveals:

- The authored action, actual native arguments and dispatch, return value, and failure details.
- Observed windows, controls, candidates, and accessibility coverage separately from the minimal state actually sent to Jev.
- Window and control rounds, candidate reductions, groups, exact existing question wording, call inputs, returned choices, probabilities, and available call durations.
- Stage, round, step, and run timing, timestamps, native-command markers, and loop boundaries. An authored step ID remains separate from its low-level loop number.
- Per-call input tokens and estimated input cost at **$0.042 per million input tokens**, with no output-token charge. Missing accounting is labeled unavailable; partial totals identify the unavailable remainder.

Step evidence is delivered at boundaries, including while a sequence is paused. Large live records may identify a capture gap until the final archive is uploaded. **Full timeline & report** retains the existing shared-scale loop chart and full report; **Evidence ZIP** downloads the existing authenticated evidence bundle. **Retry evidence** retries a failed read of the report without executing an action.

## Execution and validation limits

The backend owns the execution cursor, immutable run snapshot, versioned run controls, and an idempotent start request. Repeated controls cannot claim the same step twice. The worker journals the active step and its completion. Lost delivery acknowledgments, crashes, or uncertain native dispatch do not automatically replay desktop input.

This implementation uses the existing Jev model pin and authored questions. Explicit step actions determine the tool; Jev resolves the target. Current-focus Type and Press key require no Jev target-selection call. The compact existing `last_control` context carries the previous selected window/control into the next step, including after reads; this is not a variable-binding or general planning system. No additional Jev question wording or higher-level planner is introduced.

The worktree is not deployed by this change. Local verification included JavaScript syntax, Python compilation/import/OpenAPI checks, and manual checks against an isolated PostgreSQL database for duplicate starts, version conflicts, immutable snapshots, one-time dispatch, pause/resume/stop, failures, and recovery. Browser checks covered authoring, saving, reordering, reloading, Step, Continue, and the inspector with explicitly labeled simulated worker outcomes. Ephemeral fake-driver checks covered exact read extraction, action dispatch, existing question batching, and report boundaries. No live Jev calls or assigned Omarchy actions were performed, and no new automated tests were added. Both the app and worker must be updated together to use manual execution; older workers fail closed on manual sequences.
