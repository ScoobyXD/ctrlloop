[CmdletBinding()]
param(
    # Optional Git object cache; unavailable objects are fetched from upstream.
    [string]$SourceMirror,
    [string]$CargoHome,
    [string]$RustupHome
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$localRoot = Join-Path $repoRoot '.local\cua-driver'
$sourceRoot = Join-Path $localRoot 'source'
$targetRoot = Join-Path $localRoot 'target'
$binRoot = Join-Path $localRoot 'bin'
$sourceCommit = 'fc188250b4ca8549b8e61f937fdb1fb560770e86'
$sourceTag = 'cua-driver-rs-v0.28.2'
$targetTriple = 'x86_64-pc-windows-msvc'
$toolchain = '1.97.1'
$patches = @(
    (Join-Path $PSScriptRoot 'patches\cua-driver-0.28.2-left-windows-key.patch'),
    (Join-Path $PSScriptRoot 'patches\cua-driver-0.28.2-direct-accessibility-only.patch')
)

function Assert-NativeSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed (exit code $LASTEXITCODE)."
    }
}

foreach ($patch in $patches) {
    if (-not (Test-Path -LiteralPath $patch -PathType Leaf)) {
        throw "Required patch is missing: $patch"
    }
}

New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
# Git for Windows may check tracked patch files out as CRLF, while an existing
# upstream checkout uses LF. Apply canonical LF copies in either case.
$patchRoot = Join-Path $localRoot 'patches'
New-Item -ItemType Directory -Path $patchRoot -Force | Out-Null
$patches = @($patches | ForEach-Object {
    $normalizedPatch = Join-Path $patchRoot (Split-Path -Leaf $_)
    $patchText = [IO.File]::ReadAllText($_).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($normalizedPatch, $patchText, [Text.UTF8Encoding]::new($false))
    $normalizedPatch
})
if (-not (Test-Path -LiteralPath $sourceRoot)) {
    $cloneArguments = @('clone', '--config', 'core.autocrlf=false', '--filter=blob:none', '--sparse', '--branch', $sourceTag)
    if ($SourceMirror) {
        $mirrorRoot = (Resolve-Path -LiteralPath $SourceMirror).Path
        $cloneArguments += @('--reference-if-able', $mirrorRoot, '--dissociate')
    }
    & git @cloneArguments -- https://github.com/trycua/cua.git $sourceRoot
    Assert-NativeSuccess 'Cloning the pinned CUA source'
    & git -C $sourceRoot sparse-checkout set libs/cua-driver
    Assert-NativeSuccess 'Checking out the CUA Driver source subtree'
    & git -C $sourceRoot checkout --detach $sourceCommit
    Assert-NativeSuccess 'Checking out the pinned CUA commit'
}

$actualCommit = & git -C $sourceRoot rev-parse HEAD
Assert-NativeSuccess 'Reading the CUA source commit'
if ($actualCommit.Trim() -ne $sourceCommit) {
    throw "CUA source must be at $sourceCommit; found $actualCommit. Existing source was not reset."
}

foreach ($patch in $patches) {
    & git -C $sourceRoot apply --reverse --check -- $patch 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Already applied: $(Split-Path -Leaf $patch)"
        continue
    }
    & git -C $sourceRoot apply --check -- $patch
    Assert-NativeSuccess "Checking patch $(Split-Path -Leaf $patch)"
    & git -C $sourceRoot apply -- $patch
    Assert-NativeSuccess "Applying patch $(Split-Path -Leaf $patch)"
}

# Compare against a temporary index containing exactly the pinned commit and
# the two tracked patches. This does not alter the source repository's index.
$expectedIndex = Join-Path $localRoot "expected-index-$([guid]::NewGuid().ToString('N'))"
$previousIndex = $env:GIT_INDEX_FILE
try {
    $env:GIT_INDEX_FILE = $expectedIndex
    & git -C $sourceRoot read-tree $sourceCommit
    Assert-NativeSuccess 'Preparing the expected source index'
    foreach ($patch in $patches) {
        & git -C $sourceRoot apply --cached -- $patch
        Assert-NativeSuccess "Preparing expected patch $(Split-Path -Leaf $patch)"
    }
    # Sparse partial clones need not contain blobs outside the driver subtree.
    $expectedTree = & git -C $sourceRoot write-tree --missing-ok
    Assert-NativeSuccess 'Recording the expected source tree'
} finally {
    $env:GIT_INDEX_FILE = $previousIndex
    if (Test-Path -LiteralPath $expectedIndex) {
        Remove-Item -LiteralPath $expectedIndex
    }
}
& git -C $sourceRoot diff --quiet --exit-code $expectedTree
Assert-NativeSuccess 'Verifying the source contains exactly the tracked patches'

if (-not $CargoHome) { $CargoHome = Join-Path $localRoot 'cargo' }
if (-not $RustupHome) { $RustupHome = Join-Path $localRoot 'rustup' }
$CargoHome = [IO.Path]::GetFullPath($CargoHome)
$RustupHome = [IO.Path]::GetFullPath($RustupHome)
$cargoPath = Join-Path $CargoHome 'bin\cargo.exe'
if (-not (Test-Path -LiteralPath $cargoPath -PathType Leaf)) {
    throw "Cargo was not found at $cargoPath. Install Rust $toolchain there or supply -CargoHome and -RustupHome."
}

$savedEnvironment = @{}
foreach ($name in @('CARGO_HOME', 'RUSTUP_HOME', 'CARGO_TARGET_DIR', 'CUA_DRIVER_SOURCE_SHA')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
Push-Location (Join-Path $sourceRoot 'libs\cua-driver\rust')
try {
    $env:CARGO_HOME = $CargoHome
    $env:RUSTUP_HOME = $RustupHome
    $env:CARGO_TARGET_DIR = $targetRoot
    $env:CUA_DRIVER_SOURCE_SHA = "$sourceCommit-dirty"
    & $cargoPath "+$toolchain" build --locked --release --target $targetTriple -p cua-driver --bin cua-driver
    Assert-NativeSuccess 'Building CUA Driver'
} finally {
    Pop-Location
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}

$builtExe = Join-Path $targetRoot "$targetTriple\release\cua-driver.exe"
$stagedExe = Join-Path $binRoot 'cua-driver.exe'
New-Item -ItemType Directory -Path $binRoot -Force | Out-Null
Copy-Item -LiteralPath $builtExe -Destination $stagedExe -Force
& $stagedExe --version
Assert-NativeSuccess 'Inspecting the staged CUA Driver'
Get-FileHash -Algorithm SHA256 -LiteralPath $stagedExe
Write-Host "Staged worktree-local driver: $stagedExe"
Write-Host 'The installed driver and shared running service were not changed.'
