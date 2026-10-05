# Job to executable steps

Submitting the New CtrlLoop job uses the existing `/api/loops` endpoint and
pinned `gpt-6-luna` model with high reasoning. The prompt frame and strict JSON
output schema live in `src/planning.py`; no Jev questions are changed.

The planner returns a concise job summary and ordered actions in the same
`tool`, `target`, `text`, `key` representation as manual steps. The server
validates every action with `normalize_action` and assigns step UUIDs.
Generated typing requires explicit text and an immediately preceding click
with the identical target description. Generated actions never depend on
unspecified desktop focus. Unused fields from the model are null and removed
before existing action validation.

For “Search for hot snacks”, the intended plan is:

1. Click the browser webpage's search input.
2. Type the exact query `hot snacks` into that same input.
3. Click the webpage's search submit button.

The worker opens its configured browser; browser chrome/address bars and
targeted browser key input are unavailable through the typed page route.
The prompt uses page controls and submit buttons. Native application key
actions remain supported. The planner returns a blocked reason and no steps
when the complete job cannot be expressed with these capabilities; it does
not invent unknown credentials, conditional scripts, or future results.

Generation atomically stores the steps on both the saved loop and any initial
queued run. An initial Start executes those frozen steps through the existing
sequence executor after generation succeeds. The same workbench displays and
edits generated and manual steps. Run, pause, single-step execution, reports,
Jev selection, and the existing step cursor are reused. Generated sequences
require the same request ID as manual sequences for subsequent starts.

Every step is resolved against fresh observed controls at execution time.
A completed command sequence is not proof of task completion. CAPTCHA still
requires human control. This is a fixed plan and cannot dynamically branch on
unknown application content.
