# Native Windows-key fix for CUA Driver 0.28.2

The native Windows input implementation omitted `VK_LWIN` from its extended-key
classification. CUA maps the `win` modifier to that left Windows key, so its
scan-code input must include the extended-key flag. The patch adds `VK_LWIN`
beside the existing `VK_RWIN` entry in `is_extended()`; the existing keyboard
event builder then applies the flag to both key-down and key-up.

This changes the native driver, not the application workflow. Windows Search
selection still sends **Win+S, then immediately types the exact supplied text**.
No readiness probe, focus check, observation, control-selection round, or wait
is inserted between those commands.

## Source and patch

| Item | Value |
| --- | --- |
| Upstream repository | `https://github.com/trycua/cua.git` |
| Version | `cua-driver-rs-v0.28.2` |
| Exact commit | `fc188250b4ca8549b8e61f937fdb1fb560770e86` |
| Local checkout | `C:\Users\Jonat\ctrlloop\.local\cua-driver\source` |
| Patch | [`scripts/patches/cua-driver-0.28.2-left-windows-key.patch`](../../scripts/patches/cua-driver-0.28.2-left-windows-key.patch) |
| Changed upstream file | `libs/cua-driver/rust/crates/platform-windows/src/input/keyboard.rs` |
| Rust toolchain | `1.97.1-x86_64-pc-windows-msvc` |
| Local Cargo / Rustup homes | `.local\cua-driver\cargo` / `.local\cua-driver\rustup` |
| MSVC Build Tools location | `C:\BuildTools` |

The checkout and build dependencies are ignored local working files. The patch
is the reviewable change retained in this repository. Reproduce from the exact
commit; do not apply it to an arbitrary newer release.

In PowerShell, from the repository root, verify the checkout and apply the patch
once. For a fresh checkout, fetch the pinned release and check out its exact
commit first.

```powershell
$fixRepo = 'C:\Users\Jonat\ctrlloop'
$fixSource = Join-Path $fixRepo '.local\cua-driver\source'
$fixCommit = 'fc188250b4ca8549b8e61f937fdb1fb560770e86'
$fixPatch = Join-Path $fixRepo 'scripts\patches\cua-driver-0.28.2-left-windows-key.patch'

if ((& git -C $fixSource rev-parse HEAD).Trim() -ne $fixCommit) {
    throw 'The CUA checkout is not at the pinned commit.'
}
& git -C $fixSource apply --check $fixPatch
if ($LASTEXITCODE -ne 0) { throw 'Patch check failed; inspect whether it is already applied.' }
& git -C $fixSource apply $fixPatch
if ($LASTEXITCODE -ne 0) { throw 'Patch application failed.' }
& git -C $fixSource diff --check
& git -C $fixSource diff --stat
```

For an already-patched checkout, `git apply --reverse --check` with the same
patch confirms that it is present without changing files.

## Build the main executable

After the C++ x64 tools, Spectre-mitigated C++ libraries
(`Microsoft.VisualStudio.Component.VC.Runtimes.x86.x64.Spectre`), and Windows SDK
finish installing, open the matching
developer shell and use the local Rust homes. Build only the main executable;
the desktop hotkey branch executes `send_key_synthesized()` in that daemon's
Windows backend. The 0.28.2 UIAccess helper forwarding path is not active.

```powershell
& 'C:\BuildTools\Common7\Tools\Launch-VsDevShell.ps1' -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
$env:CARGO_HOME = Join-Path $fixRepo '.local\cua-driver\cargo'
$env:RUSTUP_HOME = Join-Path $fixRepo '.local\cua-driver\rustup'
$env:CUA_DRIVER_SOURCE_SHA = "$fixCommit-dirty"
$fixCargo = Join-Path $env:CARGO_HOME 'bin\cargo.exe'
Push-Location (Join-Path $fixSource 'libs\cua-driver\rust')
try {
    & $fixCargo +1.97.1 build --locked --release --target x86_64-pc-windows-msvc -p cua-driver --bin cua-driver
    if ($LASTEXITCODE -ne 0) { throw 'CUA build failed; do not stage or activate it.' }
} finally { Pop-Location }
$fixBuiltExe = Join-Path $fixSource 'libs\cua-driver\rust\target\x86_64-pc-windows-msvc\release\cua-driver.exe'
& $fixBuiltExe --version
Get-FileHash -Algorithm SHA256 -LiteralPath $fixBuiltExe
```

Build from the Rust workspace so its `.cargo/config.toml` enables static CRT
linking. If `CARGO_TARGET_DIR` is explicitly configured, use its actual release output
path instead. The source provenance retains `-dirty` because this is a patched
build. A local build is unsigned; it does not retain the publisher's signature.
The main executable's manifest is `asInvoker` with `uiAccess=false`.

## Stage without overwriting the official release

The observed installation has this junction chain:

```text
%LOCALAPPDATA%\Programs\Cua\cua-driver\bin
  -> %USERPROFILE%\.cua-driver\packages\current
  -> %USERPROFILE%\.cua-driver\packages\releases\0.28.2-x86_64-pc-windows-msvc
```

Stage a separate directory, keeping the original signed main executable for
rollback. Copy the official signed `cua-driver-uia.exe` and existing
`cua-cursor-theme.exe` unchanged into the new directory. Do not replace the
UIAccess helper with an unsigned local build.

```powershell
$fixPackages = Join-Path $env:USERPROFILE '.cua-driver\packages'
$fixOriginal = Join-Path $fixPackages 'releases\0.28.2-x86_64-pc-windows-msvc'
$fixRelease = Join-Path $fixPackages 'releases\0.28.2-win-key-fix-x86_64-pc-windows-msvc'
$fixCurrent = Join-Path $fixPackages 'current'
$fixVisibleExe = Join-Path $env:LOCALAPPDATA 'Programs\Cua\cua-driver\bin\cua-driver.exe'

if (Test-Path -LiteralPath $fixRelease) {
    throw 'The patched release directory already exists; inspect it instead of overwriting it.'
}
New-Item -ItemType Directory -Path $fixRelease | Out-Null
Copy-Item -LiteralPath $fixBuiltExe -Destination (Join-Path $fixRelease 'cua-driver.exe')
foreach ($fixCompanion in @('cua-driver-uia.exe', 'cua-cursor-theme.exe')) {
    Copy-Item -LiteralPath (Join-Path $fixOriginal $fixCompanion) -Destination $fixRelease
    $fixBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $fixOriginal $fixCompanion)).Hash
    $fixAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $fixRelease $fixCompanion)).Hash
    if ($fixBefore -ne $fixAfter) { throw "Companion copy differs: $fixCompanion" }
}
Get-AuthenticodeSignature -FilePath (Join-Path $fixRelease 'cua-driver-uia.exe') |
    Select-Object Status, StatusMessage
```

Before this change, the official executable hashes were:

| File | SHA-256 |
| --- | --- |
| `cua-driver.exe` | `DBBD52D75759900155FBF3D5F0A13C759A12D06EF17338B88B3F2B8B9C1EF8DC` |
| `cua-driver-uia.exe` | `2AA7BDE5774C12771B994BA5500E1B948E90A3129FD1182A486E462606BDE7F8` |

Both signatures were valid and identified Cua AI, Inc. These hashes identify
the preserved local originals, not every distribution of this version.

## Atomic activation and rollback

The published [0.28.2 installer](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/scripts/install.ps1)
retargets the inner junction with `FSCTL_SET_REPARSE_POINT`. Reuse that exact
helper. Do not execute the entire installer: it also downloads releases,
changes installation state, and performs cleanup.

The following loads only its `Add-JunctionSupportType` function from the pinned
checkout. No junction is changed by loading this helper.

```powershell
$fixInstaller = Join-Path $fixSource 'libs\cua-driver\scripts\install.ps1'
$fixTokens = $null
$fixParseErrors = $null
$fixAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $fixInstaller, [ref]$fixTokens, [ref]$fixParseErrors
)
if ($fixParseErrors.Count) { throw 'Could not parse the pinned installer.' }
$fixHelper = @($fixAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Add-JunctionSupportType'
}, $false))
if ($fixHelper.Count -ne 1) { throw 'Expected exactly one installer junction helper.' }
. ([scriptblock]::Create($fixHelper[0].Extent.Text))
Add-JunctionSupportType
if ((Get-Item -LiteralPath $fixCurrent).LinkType -ne 'Junction') {
    throw 'Refusing to retarget a non-junction current directory.'
}
```

The existing `cua-driver-serve` scheduled task uses **Interactive / Highest**
and a hidden PowerShell wrapper launching the stable visible executable path.
Keep that task and the outer `bin` junction. Keep the patched executable named
`cua-driver.exe`: the `cua-driver-local.exe` name selects a different state
namespace and pipe, which the application's existing connection does not use.

After staging and validation, activate as follows. Read the current daemon PID
immediately before stopping it; the historical PID must not be reused.

```powershell
$fixStatus = (& $fixVisibleExe status) -join "`n"
$fixPidMatch = [regex]::Match($fixStatus, '(?m)^\s*pid:\s*(\d+)\s*$')
if (-not $fixPidMatch.Success) { throw 'No daemon PID found; inspect status before activation.' }
$fixDaemonPid = [uint32]$fixPidMatch.Groups[1].Value
[CuaDriverInstaller.Junction]::SetTarget($fixCurrent, $fixRelease)
& (Join-Path $fixOriginal 'cua-driver.exe') stop --expected-pid $fixDaemonPid --socket '\\.\pipe\cua-driver'
if ($LASTEXITCODE -ne 0) { throw 'The old daemon did not stop cleanly; inspect before restarting.' }
& $fixVisibleExe autostart kick
if ($LASTEXITCODE -ne 0) { throw 'Autostart failed; inspect the task or roll back.' }
& $fixVisibleExe status
Get-Process -Name cua-driver | Select-Object Id, SessionId
Get-Item -LiteralPath $fixCurrent | Select-Object FullName, LinkType, Target
Get-FileHash -Algorithm SHA256 -LiteralPath $fixVisibleExe
```

Verify that the restarted daemon is in the interactive session, uses the new
build, and answers through `\\.\pipe\cua-driver`. A successful command return
alone does not establish that Win+S opens Search. Record a separately authorized
live observation of the result after typing; it does not add an intermediate
check to the application workflow.

To roll back, initialize the paths and junction helper above, then retarget the
same junction to the preserved official directory and restart the currently
running daemon. This leaves both release directories intact.

```powershell
$fixStatus = (& $fixVisibleExe status) -join "`n"
$fixPidMatch = [regex]::Match($fixStatus, '(?m)^\s*pid:\s*(\d+)\s*$')
if (-not $fixPidMatch.Success) { throw 'No daemon PID found; inspect status before rollback.' }
$fixDaemonPid = [uint32]$fixPidMatch.Groups[1].Value
if (-not (Test-Path -LiteralPath (Join-Path $fixOriginal 'cua-driver.exe'))) {
    throw 'The original executable is missing; do not retarget.'
}
[CuaDriverInstaller.Junction]::SetTarget($fixCurrent, $fixOriginal)
& $fixVisibleExe stop --expected-pid $fixDaemonPid --socket '\\.\pipe\cua-driver'
if ($LASTEXITCODE -ne 0) { throw 'The patched daemon did not stop cleanly; inspect before restarting.' }
& $fixVisibleExe autostart kick
if ($LASTEXITCODE -ne 0) { throw 'Autostart failed; inspect the existing scheduled task.' }
& $fixVisibleExe status
Get-FileHash -Algorithm SHA256 -LiteralPath $fixVisibleExe
```

Neither procedure deletes a directory or overwrites the official release.
Future CUA updates can retarget `current` again; confirm whether a later version
contains the fix before discarding this patch or its rollback copy.

## Completion record

- Release build succeeded on 2026-09-22 PDT with Rust 1.97.1, MSVC
  14.44.35207, and Windows SDK 10.0.26100.0. The successful incremental build
  took 2m 05s and emitted upstream unused-code warnings.
- Patched executable SHA-256:
  `26939A4382ECD63619C83BEEAA43FC76F9D4C823CC5CF0159A2FCBC6930BFB3C`.
- Activated `releases\0.28.2-win-key-fix-x86_64-pc-windows-msvc` through
  `packages\current`. The existing task restarted daemon PID 41472 in session 1.
  Its `get_config.source_sha` returned
  `fc188250b4ca8549b8e61f937fdb1fb560770e86-dirty`.
- Both companion copies matched their originals; the UIAccess helper's signature
  remained valid. The preserved official main executable still matched its
  recorded hash after activation.
- Live command:
  `uv run --locked python -m src.main "open up my slack" --text "slack" --max-iterations 1`.
  Jev selected Windows Search in two window rounds; the app sent Win+S then
  `type_text("slack")`. A subsequent desktop image visibly showed Search open
  with the exact query. A later observation showed the Slack app result.
  There was no observation or wait inserted between the two input commands.
- Verification report:
  `reports/run-20260923T063319131276Z-f51211e30ec14bdbad13fc30d8ad0d29/total_run.json`.
  Pipeline duration was 1,149.44 ms; exit 2 reflects the intentionally configured
  one-iteration limit. Native input still reports `effect: unverifiable`; the
  desktop observations establish the visible result.
- A separate second one-iteration invocation selected the synthetic launcher
  again. End-to-end Slack launch was not established by these checks.
- No automated tests were added or run. The native patch was independently
  reviewed and verified with `git apply --reverse --check`.

## Build tools installed for this repair

Microsoft Visual Studio 2022 Build Tools 17.14.41 installed the C++ toolchain,
MSBuild/prerequisites, Spectre libraries, and Windows SDK system-wide. Rust,
Cargo, rustfmt, the pinned CUA source, downloaded crates, installers, and native
build output are under the project's ignored `.local\cua-driver` directory.
The Rust installation did not modify the user's PATH.

Measured retained file sizes after the build (not network transfer sizes):

| Directory | Size |
| --- | --- |
| `C:\BuildTools` | 3.65 GiB |
| `C:\Program Files (x86)\Windows Kits\10` | 1.67 GiB |
| `C:\Users\Jonat\ctrlloop\.local\cua-driver` | 2.71 GiB |

These sum to about 8.03 GiB; shared Visual Studio installer files and other
system prerequisites are additional. Installer downloads came from Microsoft's
official bootstrapper and Rust's official distribution host. The original CUA
release remains installed alongside the patched release.
