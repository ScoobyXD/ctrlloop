# Browser controls in the assigned desktop

Deploy the browser route with the existing worker deploy command and
`--browser-start-url https://www.google.com`. The configured URL opens at run
start; all authored steps in a run share the same browser session.

This opt-in uses checksum-pinned Cua Driver 0.28.2 on a separate socket and MCP
port 3001. The original Driver 0.24.0 and port 3000 remain available. Omarchy
cannot currently attach an existing consumer Chrome profile through this
Driver's GNOME setup path. Instead, the worker launches/reuses the Driver's
persistent isolated profile named `ctrlloop`, without copying personal cookies.
Ending the session cleans up its browser process; the named profile remains.

The existing Candidate records, Jev questions, model pin, action API and reports
are reused. The Driver binds the exact native browser window and uniquely
selected tab, then observes DOM, accessibility and layout with `semantic_v2`.
Other desktop applications continue through native accessibility controls.
Unsupported consumer Chromium windows are excluded while the managed browser
is active, preventing clicks on a browser frame with no page controls.

Browser clicks explicitly use `dom_event` on Linux. Typing uses a fresh, focused
editable ref and the exact authored text with `insert_text` and `replace=true`.
Click the field first when it is not focused. Ref-targeted actions use fresh
snapshots and are never replayed after uncertain delivery. Browser targeted key
actions are unavailable; choose the page button or an authored current-focus
key action. Read actions copy the selected ref's exposed text.

Live verification on the assigned Omarchy VM found Google's Search combobox,
verified exact text insertion and submitted the search. Google displayed a
CAPTCHA afterward. Successful dispatch does not claim retrieved search results
or general task completion. No Playwright or new Jev questions are required.

To deploy the original native route, omit `--browser-start-url` on a subsequent
worker deployment. This removes the opt-in service environment overrides.
