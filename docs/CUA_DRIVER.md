# CUA Driver on Windows

CUA Driver **0.28.2** is installed on this machine. The official `cua-driver-serve` scheduled task is enabled with `RunLevel=Highest` and `LogonType=Interactive`, and was started and checked successfully. Installation and documentation were completed in the existing `main` checkout.

The installation provides window discovery, native Windows accessibility reads,
and native input tools. ctrlloop connects Jev's selected controls to native clicks
and its synthetic Windows Search destination to desktop Win+S followed directly
by text entry, through one persistent MCP adapter.

ctrlloop now requires [direct Windows accessibility activation for clicks](cua-driver/direct-accessibility-only.md).
The connected daemon must contain that patch; its `0.28.2` version string alone
is insufficient. The shared installation described below remains unchanged by
this work. The versioned contract and original verification samples document
that earlier runtime.

## Exact interface reference

Read the [versioned Windows input/output contract](cua-driver/0.28.2/README.md). It includes:

- The read-tool and click input JSON Schemas exported from the installed binary.
- The actual MCP output JSON Schemas advertised for `list_windows` and `get_window_state`.
- Sanitized live request/response pairs, a readable accessibility tree, query behavior, and failure examples.
- The difference between CLI stdout and MCP responses, including a CLI error-handling limitation.
- The click result/refusal and snapshot-cache contracts from the matching release.

## Installation record

Verified on 2026-09-21 using the [official Windows installer](https://cua.ai/driver/install.ps1) and [official release](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.28.2).

| Item | Value |
| --- | --- |
| Driver | `cua-driver 0.28.2` |
| Target | `x86_64-pc-windows-msvc` |
| Command location | `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin\cua-driver.exe` |
| Installed release | `%USERPROFILE%\.cua-driver\packages\releases\0.28.2-x86_64-pc-windows-msvc` |
| Current-release junction | `%USERPROFILE%\.cua-driver\packages\current` |
| User PATH | Installer added the command directory |
| Runtime | Official scheduled task, interactive Windows desktop |
| Service pipe | `\\.\pipe\cua-driver` |
| Permission mode | `standard` |
| Installer SHA-256 | `3e770fa8c351b80db99ae6b080f696a22f844534498bf44d45816cbd05eb0c3f` |

The installer checksum matched the release's published checksum before execution. The release was explicitly pinned to `0.28.2`. Version garbage collection was disabled for this installation only; no older release cleanup was needed. The driver also installed its official UIAccess worker. Telemetry was initially left at the installer's default, then explicitly disabled with `cua-driver telemetry disable` on 2026-09-22. A separate status check confirmed `enabled: false` and `source: persisted`.

The binary was initially installed with `-NoAutoStart`. The user then explicitly authorized administrator approval: `cua-driver autostart enable` completed through Windows UAC, followed by `cua-driver stop` and `cua-driver autostart kick`. **Automatic startup is enabled in the final configuration.**

The published guide says installation does not need administrator access. The version-matched installer distinguishes the user-profile binary installation from the scheduled task: registration of its `RunLevel=Highest` task requests UAC elevation. This is not a requirement to build a separate UI or configure a browser.

## Telemetry stays off

The ctrlloop adapter passes `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` to every CUA CLI
child process. This overrides an inherited enable value and CUA's saved setting
without changing the parent environment. It covers window discovery,
foreground activation, accessibility reads, clicks, hotkeys, and text entry through the same adapter.

This also avoids a Windows 0.28.2 latency bug: the CLI's background telemetry
worker can inherit captured output handles, making the caller wait for that
worker to exit after the command has already returned its result.

For CUA used outside ctrlloop, apply its saved opt-out after installation:

```powershell
cua-driver telemetry disable
cua-driver telemetry status --json
```

The saved preference applies to future events in the existing service unless
that service has an explicit telemetry environment override. The adapter's
per-process override applies to its CLI children; it does not reconfigure an
already-running service. These are CUA 0.28.2 settings and should be checked
again when upgrading the pinned driver.

## Reproduce the official installation

From an interactive Windows desktop PowerShell session:

```powershell
$env:CUA_DRIVER_RS_VERSION = '0.28.2'
try {
    irm https://cua.ai/driver/install.ps1 | iex
} finally {
    Remove-Item Env:CUA_DRIVER_RS_VERSION -ErrorAction SilentlyContinue
}
```

Accept the Windows administrator prompt to register automatic startup. Open a new PowerShell window if the updated user PATH is not visible to an existing terminal. Then:

```powershell
cua-driver --version
cua-driver autostart kick
cua-driver autostart status
cua-driver status
cua-driver doctor --json
```

A version string proves installation, not access to the desktop. During verification, the sandbox shell was in Session 1 but attached to `CodexSandboxDesktop` and saw no normal windows. The authorized host execution context used the `Default` desktop and reached Windows UI Automation. The successful captures came through the official scheduled daemon. A correct session ID alone is therefore insufficient.

## Read a window without acting on it

Use the installed executable explicitly if this terminal has an old PATH:

```powershell
$cua = Join-Path $env:LOCALAPPDATA 'Programs\Cua\cua-driver\bin\cua-driver.exe'
'{}' | & $cua call list_windows
```

Choose a `pid` and `window_id` from the same returned window record. Choosing means copying identifiers into the next request; it does not activate or focus the window.

```powershell
$targetPid = [int](Read-Host 'pid from list_windows')
$targetWindowId = [long](Read-Host 'window_id from that same record')

@{
    pid = $targetPid
    window_id = $targetWindowId
    include_accessibility_tree = $true
    include_screenshot = $false
    max_elements = 500
    max_depth = 15
} | ConvertTo-Json -Compress | & $cua call get_window_state
```

These explicit limits bound the initial walk; they do not promise a complete tree. Omit `screenshot_out_file` for a tree-only request, because that argument forces capture even when `include_screenshot` is false.

The CLI's successful result has `elements` and `tree_markdown` **at the top level**. A real CLI tool failure may print plain text and still return exit code zero; inspect the output as described in the versioned reference. Do not treat an empty tree as proof that an app has no controls.

## Run the control loop

Each original user request now uses one persistent MCP client, connected with
`cua-driver mcp --socket \\.\pipe\cua-driver` to the existing daemon. All CUA
operations and loop iterations share that client. It closes on normal completion,
error or interruption; the daemon stays running. A new request receives a new
client. Python callers should scope `CuaDriver()` with a context manager.
The `--driver-socket` CLI option, or `CuaDriver(socket=...)` in Python, selects a
different already-running daemon pipe. The default remains `\\.\pipe\cua-driver`.
`--driver` selects the client executable; both options are needed to use an
isolated patched build without replacing the shared runtime:

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.' `
    --driver .\.local\cua-driver\bin\cua-driver.exe `
    --driver-socket '\\.\pipe\ctrlloop-direct-accessibility'
```

Start that dedicated daemon first using the [isolated runtime instructions](cua-driver/direct-accessibility-only.md).

Before the first click on each connection, the adapter reads `tools/list` and
requires the daemon's `click` schema to advertise `route: "accessibility"` as
the sole route and default. Each click sends it explicitly and only accepts an
accessibility result. A daemon without this contract returns
`direct_accessibility_unsupported` before any click is sent. This check does not
disable observations, text entry, or hotkeys. Use the [patched build and isolated
runtime instructions](cua-driver/direct-accessibility-only.md) to enable clicks.

No periodic health polling is used. EOF and request failures/timeouts trigger
connection disposal. Reads reconnect and retry once. A lost click, activation, hotkey, or text-entry
response stops without replaying the action. A fresh snapshot is required before
using controls after connection loss. MCP's native structured result/error
envelopes replace parsing one-shot CLI stdout in the application adapter; the
one-shot commands above remain useful for manual inspection.

From the ctrlloop repository, with its dependencies and `TYPESAFE_API_KEY` configured:

```powershell
uv run --locked python src/main.py 'Click the Test button in my test window.'
uv run --locked python src/main.py 'Open Chrome using Windows Search.' --text 'Chrome'
```

Every run executes the loop. Each selected real window is brought forward using
native `bring_to_front({pid, window_id})`
before the control snapshot is captured. A refusal stops execution; the report
records the activation request and native result as action events. This also
applies to windows that are not minimized.
The pipeline dispatches the
selected record's `pid`, `window_id`, and `element_token` (or the matching index
and snapshot pair), records the native `action_result`, waits the selected
settling interval, and captures fresh state. Jev then selects the next action.
The loop defaults to `--max-iterations 10`, returns `iteration_limit` at that
bound, and does not assess completion or retry failed actions.

Click dispatch is limited to a single left activation of that snapshot target,
using UIA Invoke, Toggle, SelectionItem.Select, ExpandCollapse.Expand, or an MSAA
default action. Targets without a supported action and coordinate, desktop,
foreground-delivery, modified, multi-click, right-click, or middle-click gestures
return a clear unsupported result. Activation failures return
`direct_accessibility_failed`. There is no fallback to posted mouse messages,
SendInput mouse clicks, browser mouse events, or JavaScript DOM clicks.
An accessibility provider may raise or focus its window; direct activation does
not guarantee background preservation. The pipeline's existing explicit
`bring_to_front` step also remains in place.

Targeted text entry uses the existing native `delivery_mode` argument. The app
defaults supported observed browser process names to `foreground`; other apps
retain the native default. `--delivery-mode {foreground,background}` or Python's
`run_pipeline(..., delivery_mode=...)` overrides that choice for targeted typing
only. The [main workflow](main-workflow.md#resolving-and-entering-text) documents
the exact name policy. This changes no driver default, click delivery, or desktop
Windows Search command, and adds no automatic delivery-mode retry.

If the synthetic Windows Search window wins, its activation stage instead sends
`hotkey({scope: "desktop", keys: ["win", "s"]})` and directly follows it with
`type_text({scope: "desktop", text: <prepared text>})`. There is no
intervening observation, confirmation, sleep, control search, or Jev call. Both
commands use the same MCP client and iteration; the next iteration observes the
real search results without a settling wait. Real Start/Search windows remain
available to normal window and control selection. A freshly observed visible
Search destination suppresses the synthetic opener until it closes. If the opener
wins after prepared text was consumed, the pipeline returns `no_action` without
another hotkey or typing call. The adapter retains each native result, and
uncertain input is never replayed.

Supply exact prepared text with `--text`, `--text-file`, or
`run_pipeline(..., text=...)`. A nonempty string remains available through clicks
and is consumed by the first typing dispatch, either into an observed control or
Windows Search. Unicode, whitespace, and line endings are preserved, including
whitespace-only strings; `--text-file` reads UTF-8 bytes without newline conversion.
Missing, empty, or already used text returns `no_action` when typing is selected,
with the reason `No prepared text is available for this typing operation (missing, empty, or already used).`
Windows Search checks this before sending Win+S. There is no text-generation or
source-word selection call in the loop. Existing native insertion and caret behavior
remain unchanged; the app does not clear, replace, or submit the field automatically.

The `action_returned` event and `last_action.result` preserve native response
facts, including their actual effect and delivery metadata. Clicks continue to
fresh selection; successful targeted typing continues with `confirmed` or
`unverifiable` effect, using the normal settling wait when another iteration
remains. Targeted `partial` or `suspected_noop` effects still stop with
`observation_unavailable`; unknown or malformed native effects fail adapter
validation as `invalid_action_result`. No unverified typing is automatically replayed and no
verification stage is added. `iteration_limit`/exit 2 does not establish completion.
Native failures retain their diagnostic payload and return `error`/exit 1.
See [full CLI status and report details](../README.md#run).

The standard unpatched daemon can send synthetic mouse input for element-target
clicks, so ctrlloop refuses to use it for clicking. A shell denied access to
`\\.\pipe\cua-driver` also cannot reach it from that execution context; this is
a separate transport restriction. Do not restart or
reconfigure the working daemon to resolve a client access restriction. The
UIAccess helper is an internal worker, not a public alternative endpoint.

## Verification artifacts

- [Direct accessibility patch, isolated runtime, and validation](cua-driver/direct-accessibility-only.md)
- [Native Windows-key patch, build, and rollback](cua-driver/windows-key-fix.md)
- [Runtime verification record](cua-driver/0.28.2/runtime-verification.json)
- [Native tree from Windows Program Manager](cua-driver/0.28.2/examples/get_window_state.tree.md)
- [Corresponding structured result](cua-driver/0.28.2/examples/get_window_state.output.json)
- [Window discovery sample](cua-driver/0.28.2/examples/list_windows.output.json)

Samples preserve real data structures but replace process/window identifiers and omit unrelated windows/processes. They are documentation examples, not live reusable targets. The verification record describes the redactions and actual observed counts.

The historical single-click host smoke test, before the direct-only patch, used a disposable,
nonactivating WinForms Test button. One discovery, one snapshot, and one click
ran as separate CLI calls; the button received exactly one callback, there were
zero post-action driver reads, and the fixture exited. The native result was
`{"effect":"unverifiable","route":"accessibility","delivery":{"mode":"background"}}`.
That result established the one button's effect, not a direct-only guarantee for
every click target. Current patch validation is recorded separately above.
Its local result is in the ignored `reports/isolated-click-result.json`; it is not
a replayable target. The sandbox's separate read failed with `pipe_access_denied`,
while authorized host execution reached the daemon.

Official references: [installation](https://cua.ai/docs/how-to-guides/driver/install), [Windows tools](https://cua.ai/docs/reference/cua-driver/mcp-tools-windows), [CLI](https://cua.ai/docs/reference/cua-driver/cli-reference), [platform support](https://cua.ai/docs/reference/cua-driver/platform-support).
