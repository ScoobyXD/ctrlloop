# CUA Driver on Windows

CUA Driver **0.28.2** is installed on this machine. The official `cua-driver-serve` scheduled task is enabled with `RunLevel=Highest` and `LogonType=Interactive`, and was started and checked successfully. Installation and documentation were completed in the existing `main` checkout.

The installation provides window discovery, native Windows accessibility reads,
and native input tools. ctrlloop now connects Jev's existing selected control to
one optional CUA click through the same CLI adapter.

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
foreground activation, accessibility reads, and clicks through the same adapter.

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

## Select and execute one click

From the ctrlloop repository, with its dependencies and `TYPESAFE_API_KEY` configured:

```powershell
uv run --locked python src/main.py --click 'Click the Test button in my test window.'
```

Omit `--click` to select a control without clicking it. Every selected real
window is always brought forward using native `bring_to_front({pid, window_id})`
before the control snapshot is captured. A refusal stops execution; the report
records the activation request and native result as action events. This also
applies to runs without `--click` and windows that are not minimized.
With `--click`, the pipeline dispatches the
selected record's `pid`, `window_id`, and `element_token` (or the matching index
and snapshot pair), returns the native `action_result`, and stops. It does not
take a post-click snapshot or retry. Synthetic Start Menu/Search selections
return `no_action`; opening Start and typing are deferred.

`action_returned`/exit 0 means the driver returned action facts, including an
`unverifiable` or `suspected_noop` effect. It does not prove the application goal.
Native failures retain their diagnostic payload and return `error`/exit 1.
See [full CLI status and report details](../README.md#run).

The standard daemon supports routine native clicks. A shell denied access to
`\\.\pipe\cua-driver` cannot test them from that execution context; this is
separate from whether the installed daemon has click support. Do not restart or
reconfigure the working daemon to resolve a client access restriction. The
UIAccess helper is an internal worker, not a public alternative endpoint.

## Verification artifacts

- [Runtime verification record](cua-driver/0.28.2/runtime-verification.json)
- [Native tree from Windows Program Manager](cua-driver/0.28.2/examples/get_window_state.tree.md)
- [Corresponding structured result](cua-driver/0.28.2/examples/get_window_state.output.json)
- [Window discovery sample](cua-driver/0.28.2/examples/list_windows.output.json)

Samples preserve real data structures but replace process/window identifiers and omit unrelated windows/processes. They are documentation examples, not live reusable targets. The verification record describes the redactions and actual observed counts.

The single-click host smoke test used the production adapter with a disposable,
nonactivating WinForms Test button. One discovery, one snapshot, and one click
ran as separate CLI calls; the button received exactly one callback, there were
zero post-action driver reads, and the fixture exited. The native result was
`{"effect":"unverifiable","route":"accessibility","delivery":{"mode":"background"}}`.
Its local result is in the ignored `reports/isolated-click-result.json`; it is not
a replayable target. The sandbox's separate read failed with `pipe_access_denied`,
while authorized host execution reached the daemon.

Official references: [installation](https://cua.ai/docs/how-to-guides/driver/install), [Windows tools](https://cua.ai/docs/reference/cua-driver/mcp-tools-windows), [CLI](https://cua.ai/docs/reference/cua-driver/cli-reference), [platform support](https://cua.ai/docs/reference/cua-driver/platform-support).
