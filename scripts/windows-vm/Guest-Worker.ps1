[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Bootstrap', 'StartDriver', 'Run', 'StopDriver')]
    [string]$Action,
    [string]$RequestFile,
    [string]$TextFile,
    [ValidateRange(1, 10000)]
    [int]$MaxIterations = 10
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This script is deliberately guest-only. Host provisioning/copying is separate.
$guestRoot = 'C:\ctrlloop'
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
if ($computer.Manufacturer -ne 'Microsoft Corporation' -or $computer.Model -ne 'Virtual Machine') {
    throw 'Run Guest-Worker.ps1 only inside the provisioned Hyper-V Windows guest.'
}
$scriptRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
if ($scriptRoot -ne $guestRoot) {
    throw 'Deploy the allowlisted package to C:\ctrlloop before running this script.'
}

$driverExe = Join-Path $guestRoot '.local\cua-driver\bin\cua-driver.exe'
$driverPipe = '\\.\pipe\ctrlloop-direct-accessibility'
$runtimeRoot = Join-Path $guestRoot '.local\guest-worker'
$receiptPath = Join-Path $runtimeRoot 'driver-process.json'
$expectedDriverSha256 = '8DAAC87AE75FFDC39E92459147083B05A50EFE8E4ACCF67B23F0CF375679F5E4'
if (-not (Test-Path -LiteralPath $driverExe -PathType Leaf)) {
    throw 'The packaged pinned driver is missing.'
}
if ((Get-FileHash -LiteralPath $driverExe -Algorithm SHA256).Hash -ne $expectedDriverSha256) {
    throw 'The driver does not match the final patched binary recorded in direct-accessibility-validation.json.'
}
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

function Assert-NativeSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed (exit code $LASTEXITCODE)." }
}

function Assert-InteractiveSession {
    if (-not [Environment]::UserInteractive -or (Get-Process -Id $PID).SessionId -eq 0) {
        throw 'Start and run the UI worker from the logged-in guest desktop, not PowerShell Direct or a service.'
    }
}

function Get-OwnedDriver {
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { return $null }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $ownedProcess = Get-Process -Id $receipt.pid -ErrorAction SilentlyContinue
    if ($null -eq $ownedProcess) { return $null }
    if ($ownedProcess.Path -ne $driverExe -or
        $ownedProcess.StartTime.ToUniversalTime().Ticks.ToString() -ne $receipt.startedUtcTicks -or
        $ownedProcess.SessionId -ne (Get-Process -Id $PID).SessionId) {
        throw 'The recorded driver is not this session''s original process; inspect it before reuse or stopping.'
    }
    return $ownedProcess
}

$savedEnvironment = @{}
foreach ($name in @('CUA_DRIVER_RS_TELEMETRY_ENABLED', 'UV_PYTHON_INSTALL_DIR', 'UV_CACHE_DIR', 'UV_PROJECT_ENVIRONMENT', 'PYTHONUTF8')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:CUA_DRIVER_RS_TELEMETRY_ENABLED = '0'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $guestRoot '.local\python'
    $env:UV_CACHE_DIR = Join-Path $guestRoot '.local\uv-cache'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $guestRoot '.venv'
    $env:PYTHONUTF8 = '1'
    Push-Location $guestRoot
    try {
        switch ($Action) {
            'Bootstrap' {
                $uvExe = Join-Path $guestRoot '.local\tools\uv.exe'
                if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) {
                    throw 'The package must include the inspected uv.exe at .local\tools\uv.exe.'
                }
                $uvVersion = & $uvExe --version
                Assert-NativeSuccess 'Inspecting uv'
                if ($uvVersion -notmatch '^uv 0\.11\.26(?:\s|$)') {
                    throw 'This guest bootstrap uses the inspected uv 0.11.26; package that version.'
                }
                $projectText = Get-Content -LiteralPath (Join-Path $guestRoot 'pyproject.toml') -Raw
                $pythonMatch = [regex]::Match($projectText, '(?m)^preferred-python\s*=\s*"(?<version>\d+\.\d+\.\d+)"\s*$')
                if (-not $pythonMatch.Success) { throw 'pyproject.toml is missing the pinned preferred-python version.' }
                $pythonVersion = $pythonMatch.Groups['version'].Value
                & $uvExe python install $pythonVersion --no-bin --no-registry
                Assert-NativeSuccess 'Installing the project-pinned Python'
                & $uvExe sync --locked --python $pythonVersion --managed-python --no-dev
                Assert-NativeSuccess 'Installing the locked application dependencies'
                & (Join-Path $guestRoot '.venv\Scripts\python.exe') -c 'import sys, httpx2, dotenv, typesafe_sdk; print(sys.version)'
                Assert-NativeSuccess 'Checking the installed application imports'
                Write-Host 'Guest runtime installed. Provision TYPESAFE_API_KEY separately; no key was copied by this script.'
            }
            'StartDriver' {
                Assert-InteractiveSession
                $ownedProcess = Get-OwnedDriver
                if ($null -eq $ownedProcess) {
                    $untracked = @(Get-Process -Name 'cua-driver' -ErrorAction SilentlyContinue | Where-Object {
                        $_.Path -eq $driverExe -and $_.SessionId -eq (Get-Process -Id $PID).SessionId
                    })
                    if ($untracked.Count -gt 0) {
                        throw 'An untracked copy of this driver is already running; inspect its pipe before starting another.'
                    }
                    $ownedProcess = Start-Process -FilePath $driverExe -ArgumentList @('serve', '--socket', $driverPipe) `
                        -WorkingDirectory $guestRoot -WindowStyle Hidden -PassThru `
                        -RedirectStandardOutput (Join-Path $runtimeRoot 'driver.stdout.log') `
                        -RedirectStandardError (Join-Path $runtimeRoot 'driver.stderr.log')
                    Start-Sleep -Milliseconds 500
                    $ownedProcess.Refresh()
                    if ($ownedProcess.HasExited) {
                        throw 'The driver exited during startup; inspect .local\guest-worker\driver.stderr.log.'
                    }
                    @{
                        pid = $ownedProcess.Id
                        startedUtcTicks = $ownedProcess.StartTime.ToUniversalTime().Ticks.ToString()
                        sessionId = $ownedProcess.SessionId
                    } | ConvertTo-Json | Set-Content -LiteralPath $receiptPath -Encoding UTF8
                }
                Write-Host "Driver process $($ownedProcess.Id) is running in guest session $($ownedProcess.SessionId), pipe $driverPipe."
                Write-Host 'The first application call still validates the connected daemon; process liveness alone is not UI readiness.'
            }
            'Run' {
                Assert-InteractiveSession
                if ($null -eq (Get-OwnedDriver)) { throw 'Start the guest driver first with -Action StartDriver.' }
                if (-not $RequestFile -or -not (Test-Path -LiteralPath $RequestFile -PathType Leaf)) {
                    throw 'Run requires -RequestFile pointing to a UTF-8 request file.'
                }
                if ($TextFile -and -not (Test-Path -LiteralPath $TextFile -PathType Leaf)) {
                    throw '-TextFile must point to an existing UTF-8 file.'
                }
                $pythonExe = Join-Path $guestRoot '.venv\Scripts\python.exe'
                if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw 'Run -Action Bootstrap first.' }
                # Keep the wrapper serial for an entire invocation, including Jev waits.
                $runLock = [IO.File]::Open((Join-Path $runtimeRoot 'active-run.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
                try {
                    $arguments = @('-m', 'src.main', '--request-file', $RequestFile, '--driver', $driverExe,
                        '--driver-socket', $driverPipe, '--max-iterations', $MaxIterations.ToString())
                    if ($TextFile) { $arguments += @('--text-file', $TextFile) }
                    & $pythonExe @arguments
                    $runExitCode = $LASTEXITCODE
                } finally { $runLock.Dispose() }
                if ($runExitCode -ne 0) { throw "ctrlloop exited with code $runExitCode; retain its existing reports before retrying." }
            }
            'StopDriver' {
                Assert-InteractiveSession
                $ownedProcess = Get-OwnedDriver
                if ($null -ne $ownedProcess) {
                    & $driverExe stop --socket $driverPipe --expected-pid $ownedProcess.Id
                    Assert-NativeSuccess 'Stopping the recorded guest driver'
                }
                if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { Remove-Item -LiteralPath $receiptPath }
                Write-Host 'The recorded guest driver is stopped. Guest apps and reports were retained.'
            }
        }
    } finally { Pop-Location }
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}
