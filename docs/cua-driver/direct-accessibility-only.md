# Direct Windows accessibility activation

ctrlloop's `click` always requests the native `accessibility` route. Before the
first click on each connection, its MCP client checks the connected daemon's
`click` schema for that sole supported/default route. An older daemon is refused
before the click is sent. Selecting a different client executable alone does
not select a different daemon; use `--driver-socket` for the isolated runtime.

The native patch replaces the Windows click implementation with direct
activation of a fresh snapshot element. It reuses UIA Invoke, Toggle,
SelectionItem.Select and ExpandCollapse.Expand, plus the MSAA default action.
The first available pattern is called once. A provider failure stops the action
because a delayed side effect may still occur. Missing patterns return
`direct_accessibility_unsupported`; provider failures return
`direct_accessibility_failed`. Neither result recommends a mouse fallback.

Both Chromium bypasses and the general mouse fallback are removed from this
click implementation. Native `right_click` and `double_click` are also refused.
Coordinate/desktop targets, foreground mouse delivery, middle/right buttons,
multiple clicks and modifier clicks are unsupported. An offscreen element can
be invoked directly without using its cached coordinates to deliver input.

This guarantee covers ctrlloop activation/click delivery. Its adapter exposes
no browser/DOM click tools. The general-purpose upstream binary still contains
other tools outside this workflow, including browser input, dragging and text
entry; this patch does not change their implementations. In particular, the
existing text-focus helper may use a mouse event as part of text entry, which
is outside this change's scope.

Dispatch success remains `effect: unverifiable`, `route: accessibility`.
Only a later observation can establish navigation, selection or another
application effect. Existing foreground-suppression guards are retained, but
an application can change focus or stacking in response to accessibility.
`delivery: background` describes the driver's delivery path, not a promise
that the application will preserve focus.

## Reproduce the isolated driver build

[`scripts/build-cua-driver.ps1`](../../scripts/build-cua-driver.ps1) builds CUA
Driver 0.28.2 at commit `fc188250b4ca8549b8e61f937fdb1fb560770e86` with Rust
`1.97.1-x86_64-pc-windows-msvc` and the locked dependency graph. It applies the
existing [left Windows-key patch](../../scripts/patches/cua-driver-0.28.2-left-windows-key.patch)
and the [direct accessibility patch](../../scripts/patches/cua-driver-0.28.2-direct-accessibility-only.patch).
Both patches are retained in Git; upstream source and build output are ignored.

Open an MSVC x64 developer shell with the Windows SDK and Spectre-mitigated C++
libraries installed. Install the pinned Rust toolchain in this worktree's
`.local\cua-driver\cargo` / `.local\cua-driver\rustup` homes, or pass existing
homes explicitly. Run from the ctrlloop repository root:

```powershell
& 'C:\BuildTools\Common7\Tools\Launch-VsDevShell.ps1' -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
.\scripts\build-cua-driver.ps1
```

To reuse the existing local source and toolchain downloads on this machine:

```powershell
.\scripts\build-cua-driver.ps1 `
    -SourceMirror 'C:\Users\Jonat\ctrlloop\.local\cua-driver\source' `
    -CargoHome 'C:\Users\Jonat\ctrlloop\.local\cua-driver\cargo' `
    -RustupHome 'C:\Users\Jonat\ctrlloop\.local\cua-driver\rustup'
```

`SourceMirror` is used as an optional object cache when creating a fresh sparse
clone from upstream; unavailable objects still require network access. Git may
ignore a shallow reference repository. It does not modify the supplied
repository, copy its uncommitted changes, or keep object dependencies on it.
Existing source must match the pinned commit and exactly the two tracked patches. The
script accepts patches already applied and refuses extra tracked source edits.
It builds from the upstream Rust workspace to preserve its static CRT settings.
All compiled output goes into this worktree's `.local\cua-driver\target`, even
when Cargo and Rustup homes are shared. The resulting executable is staged at
`.local\cua-driver\bin\cua-driver.exe` with source provenance ending in `-dirty`.

The script prints the resulting version and SHA256. It does not replace the
installed executable, register a scheduled task, start or stop a daemon, or
connect to the shared driver pipe. The locally built executable is unsigned.

## Use the isolated runtime

Start the built driver in its own hidden process and retain the returned process
handle. This does not change the installed scheduled task or default pipe.

```powershell
$directExe = Join-Path (Get-Location) '.local\cua-driver\bin\cua-driver.exe'
$directPipe = '\\.\pipe\ctrlloop-direct-accessibility'
$directProcess = Start-Process -FilePath $directExe `
    -ArgumentList @('serve', '--socket', $directPipe) -WindowStyle Hidden -PassThru

uv run --locked python -m src.main "your request" `
    --driver $directExe --driver-socket $directPipe

# After the request completes, stop only this runtime.
& $directExe stop --socket $directPipe --expected-pid $directProcess.Id
```

The application still defaults to the existing shared pipe. Until that daemon
is deliberately replaced with the patched build, its clicks fail closed. A
different `--driver` path alone does not bypass this capability check.

## Validation on 2026-09-23

Release compilation, Python source compilation, CLI help, patch reproduction,
and a second idempotent build passed. No automated tests were added or run.
The first Rust release build took 2m 01s. A final schema-description correction
was rebuilt in 41.36s; the subsequent idempotent build took 0.58s.

Live checks used the built daemon in interactive Windows session 1 on the
dedicated pipe above. A temporary isolated Chrome profile and local diagnostic
page supplied the controls. No DOM or CDP mouse click was used. Browser
preparation/navigation only set up the page; every activation below used the
native Windows snapshot element.

| Activation | Dispatch time | Separate observed result |
| --- | ---: | --- |
| UIA Invoke on button | 9.53 ms | Status changed from `Activation count 0` to `Activation count 1` |
| UIA Toggle on checkbox | 9.92 ms | `selected: true` and `Checkbox enabled` |
| UIA Invoke on offscreen link | 50.71 ms | Native URL and window title changed to the destination |
| UIA ExpandCollapse on Chrome menu | 4,332.38 ms | Menu items appeared in a fresh native tree |
| Invoke on Chrome's New tab menu item | 1,495.67 ms | Provider error `0x80040200`; clear failure, no fallback; no new tab observed |

The offscreen link was at screen y=28,323 while its browser window was only
566 pixels high and fully covered by another window. Before/after native
window lists showed the browser remained underneath that window, and the
foreground HWND did not change across the click. These samples do not rule
out transient focus changes or establish the same behavior for every app.
MSAA and SelectionItem-only activation were not exercised live.

Static text, coordinate clicks, right/double clicks, foreground delivery,
modifiers and repeated clicks all returned explicit unsupported results.
Against the existing shared daemon, observations succeeded but the app refused
the first click after reading `tools/list`; **zero click requests were sent**.

CNN's page had a title and DOM content but the native page was blank and its
accessibility tree exposed no Tech link during this check. No CNN Tech click
was attempted, so this validation does not establish that specific navigation.

The [sanitized validation record](direct-accessibility-validation.json) retains
native action results, separate observations, timings and binary hashes.
Detailed local replies are in `.local/validation`; build logs are in
`.local/cua-driver`. The live binary hash was
`95F7DCBA263146FA0EFF551CEC945DAC74EA4DAB0CFAF69A3407BD70EB13BAEA`.
The final binary, differing only in click schema descriptions, is
`8DAAC87AE75FFDC39E92459147083B05A50EFE8E4ACCF67B23F0CF375679F5E4`.

The diagnostic browser, daemon and local web server were closed. The shared
driver remained PID 41472 with executable hash
`26939A4382ECD63619C83BEEAA43FC76F9D4C823CC5CF0159A2FCBC6930BFB3C`.
The original source checkout still has only its preexisting keyboard patch.
