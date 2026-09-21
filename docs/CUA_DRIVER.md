# CUA Driver on Windows

CUA Driver **0.28.2** is installed on this machine. The official `cua-driver-serve` scheduled task is enabled with `RunLevel=Highest` and `LogonType=Interactive`, and was started and checked successfully. Installation and documentation were completed in the existing `main` checkout.

This setup covers window discovery and native Windows accessibility reads. It does not add a GUI, browser automation, an action executor, or a Jev integration.

## Exact interface reference

Read the [versioned Windows input/output contract](cua-driver/0.28.2/README.md). It includes:

- The three exact input JSON Schemas exported from the installed binary.
- The actual MCP output JSON Schemas advertised for `list_windows` and `get_window_state`.
- Sanitized live request/response pairs, a readable accessibility tree, query behavior, and failure examples.
- The difference between CLI stdout and MCP responses, including a CLI error-handling limitation.

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

The installer checksum matched the release's published checksum before execution. The release was explicitly pinned to `0.28.2`. Version garbage collection was disabled for this installation only; no older release cleanup was needed. The driver also installed its official UIAccess worker. Telemetry was left at the official installer's default; its notice describes content-free product metadata.

The binary was initially installed with `-NoAutoStart`. The user then explicitly authorized administrator approval: `cua-driver autostart enable` completed through Windows UAC, followed by `cua-driver stop` and `cua-driver autostart kick`. **Automatic startup is enabled in the final configuration.**

The published guide says installation does not need administrator access. The version-matched installer distinguishes the user-profile binary installation from the scheduled task: registration of its `RunLevel=Highest` task requests UAC elevation. This is not a requirement to build a separate UI or configure a browser.

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

## Verification artifacts

- [Runtime verification record](cua-driver/0.28.2/runtime-verification.json)
- [Native tree from Windows Program Manager](cua-driver/0.28.2/examples/get_window_state.tree.md)
- [Corresponding structured result](cua-driver/0.28.2/examples/get_window_state.output.json)
- [Window discovery sample](cua-driver/0.28.2/examples/list_windows.output.json)

Samples preserve real data structures but replace process/window identifiers and omit unrelated windows/processes. They are documentation examples, not live reusable targets. The verification record describes the redactions and actual observed counts.

Official references: [installation](https://cua.ai/docs/how-to-guides/driver/install), [Windows tools](https://cua.ai/docs/reference/cua-driver/mcp-tools-windows), [CLI](https://cua.ai/docs/reference/cua-driver/cli-reference), [platform support](https://cua.ai/docs/reference/cua-driver/platform-support).
