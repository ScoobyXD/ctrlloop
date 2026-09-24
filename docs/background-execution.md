# Background execution requirement

Recorded on 2026-09-24. This is a product requirement and design direction;
background execution is not implemented by the current loop.

The [detailed research and architecture guide](research/windows-background/README.md)
compares one and multiple workers, desktop/session/VM isolation, minimizable
viewers, browser alternatives, and the capabilities of this PC.

The user must be able to continue using their computer while ctrlloop performs
a task. Automation must not take over the user's foreground window, keyboard,
mouse, or visible desktop. The user accepts either invisible execution or a
separate visible workspace, provided that minimizing its viewer leaves the
agent running in the background without taking over the main desktop.

## Required behavior

- Observation and actions must stay within the intended execution environment.
- Agent window activation, menus, typing, and cursor overlays must not appear on
  or redirect input from the user's working desktop.
- An action that cannot run under this constraint must return a clear reason
  through the existing result/error path; it must not fall back to foreground
  input on the user's desktop.
- A separate workspace viewer, if provided, must be minimizable while the agent
  continues observing and acting. Restoring the viewer must show current state
  without restarting the run. Execution must not depend on viewer visibility
  or focus, or require switching away from the user's desktop.
- Intended task results can still change shared application data. For example,
  an edit may sync into a document the user has open. The requirement concerns
  UI takeover, not preventing the requested work from changing data.

## Current gaps

| Existing behavior | Why it does not meet the requirement |
| --- | --- |
| `pipeline._prepare_window` calls `_activate_window` before each real-window observation | The selected app is brought to the foreground on every iteration. |
| Browser text entry defaults to `foreground` | Typing uses the interactive desktop. |
| Windows Search sends desktop Win+S followed by desktop text entry | The synthetic route acts on the user's current desktop. |
| Direct accessibility clicks invoke UIA/MSAA actions | Providers can still open menus, raise windows, or change focus. |
| The click patch calls `pin_overlay_above`, `overlay_glide_to`, and `ClickPulse` | Accessibility-only clicks still produce visible driver feedback. |

`--delivery-mode background` only changes targeted text delivery. It does not
disable window activation, desktop Search commands, or cursor overlays, and it
does not guarantee that application responses preserve focus. A separate daemon
pipe isolates the connection; it does not by itself isolate the desktop.

## Proposed execution boundary

Keep the existing `Driver` protocol, native window/control records,
snapshot-bound targets, selection loop, and report format. Place the driver and
the applications it controls in an agent-owned execution environment. Jev
continues to select from that environment's observed windows and controls.
This requirement needs no new Jev question or parallel set of action APIs.

There are two isolation options to evaluate:

| Option | Fit and limits |
| --- | --- |
| Hidden Windows desktop with the driver and app processes launched there | Candidate for direct UIA actions. Requires validation of discovery, text entry, provider behavior, and app startup. Ordinary foreground keyboard/mouse delivery must not be assumed to target an inactive desktop. |
| Separate Windows session or VM with its own interactive desktop | Candidate when applications need foreground input. The agent's UI changes happen inside that environment. Provisioning, application profiles, login state, and driver connectivity need explicit setup. |

These are architectural options, not existing ctrlloop capabilities. Windows
documents desktop objects separately from the currently visible input desktop;
only one desktop in the interactive window station is active at a time.
Processes can specify their desktop at creation using `STARTUPINFO.lpDesktop`.
See Microsoft's [desktop model](https://learn.microsoft.com/en-us/windows/win32/winstation/desktops)
and [process startup structure](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfow).

Microsoft's [Power Automate picture-in-picture modes](https://learn.microsoft.com/en-us/power-automate/desktop-flows/run-desktop-flows-pip)
provide a product precedent. The [research guide](research/windows-background/sessions-and-vms.md)
details the modes and their limits; those capabilities do not establish CUA compatibility.

For the accepted minimizable-workspace behavior, the recommended starting point
is a separate session or VM that keeps the agent's interactive desktop available
independently of its viewer. Minimize the viewer, not the target applications:
the current direct-click patch refuses minimized targets. The chosen session,
display, and viewer arrangement must be validated; merely minimizing an ordinary
remote desktop window is not evidence that automation will continue working.

Isolation also changes which application instances the agent operates. It
cannot be assumed to inherit the user's already-open windows, unsaved state,
or browser profile. Access to the exact existing app instance versus an
agent-owned instance is an implementation decision still to resolve.

## Next implementation checkpoint

Manually validate one agent-owned app and the pinned driver in the chosen
environment: discover it, read controls, activate a control, enter text, and
observe the result while the user continues working on the main desktop.
Repeat with the workspace viewer minimized for several loops, then restore it
and verify that execution continued and the current state is visible.
Check for transient focus changes, input interference, and overlays throughout
the operation, not only before and after it. Unsupported operations must stop
without switching desktops or retrying against the main desktop.

This checkpoint follows the repository's current no-new-tests policy. No driver
runtime or application configuration was changed while recording the requirement.
