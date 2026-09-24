[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$payloadRoot = Join-Path $projectRoot '.local\windows-vm\payload\ctrlloop'
if (Test-Path -LiteralPath $payloadRoot) {
    throw "The payload already exists at $payloadRoot. Keep the recorded snapshot or explicitly choose a new staging directory."
}
$releaseRoot = Join-Path $env:USERPROFILE '.cua-driver\packages\releases\0.28.2-direct-accessibility-only-x86_64-pc-windows-msvc'
$uvPath = Join-Path $env:LOCALAPPDATA 'hermes\bin\uv.exe'
$driverHash = '8DAAC87AE75FFDC39E92459147083B05A50EFE8E4ACCF67B23F0CF375679F5E4'
$uvHash = 'DEEAA21AAC3E3E40B3FA00788208AA9A319CEFBB3C2AA598CF580565A82EBC34'
if ((Get-FileHash -LiteralPath (Join-Path $releaseRoot 'cua-driver.exe') -Algorithm SHA256).Hash -ne $driverHash) {
    throw 'The source driver is not the inspected pinned patched binary.'
}
if ((Get-FileHash -LiteralPath $uvPath -Algorithm SHA256).Hash -ne $uvHash) {
    throw 'The source uv executable is not the inspected pinned binary.'
}
$copies = @()
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $projectRoot 'src') -File -Filter '*.py') {
    $copies += @{ source = $file.FullName; relativePath = "src\$($file.Name)" }
}
foreach ($relativePath in @('pyproject.toml', 'uv.lock', 'scripts\windows-vm\Guest-Worker.ps1', 'scripts\windows-vm\GUEST-DEPLOYMENT.md')) {
    $copies += @{ source = (Join-Path $projectRoot $relativePath); relativePath = $relativePath }
}
foreach ($binary in @('cua-driver.exe', 'cua-driver-uia.exe', 'cua-cursor-theme.exe')) {
    $copies += @{ source = (Join-Path $releaseRoot $binary); relativePath = ".local\cua-driver\bin\$binary" }
}
$copies += @{ source = (Join-Path $projectRoot '.local\cua-driver\source\LICENSE.md'); relativePath = '.local\cua-driver\LICENSE.md' }
$copies += @{ source = $uvPath; relativePath = '.local\tools\uv.exe' }
foreach ($copy in $copies) {
    if (-not (Test-Path -LiteralPath $copy.source -PathType Leaf)) { throw "Required payload file missing: $($copy.source)" }
}
New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null
$records = foreach ($copy in $copies) {
    $targetPath = Join-Path $payloadRoot $copy.relativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
    Copy-Item -LiteralPath $copy.source -Destination $targetPath
    $sourceHash = (Get-FileHash -LiteralPath $copy.source -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash
    if ($sourceHash -ne $targetHash) { throw "Payload integrity mismatch: $($copy.relativePath)" }
    [pscustomobject][ordered]@{ path = $copy.relativePath; sha256 = $targetHash; bytes = (Get-Item -LiteralPath $targetPath).Length }
}
$revision = (& git -C $projectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not record the source Git revision.' }
$manifest = [ordered]@{
    stagedUtc = [DateTime]::UtcNow.ToString('o')
    sourceRevision = $revision
    workingTreeSnapshot = $true
    guestRoot = 'C:\ctrlloop'
    credentialsIncluded = $false
    files = @($records)
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $payloadRoot 'deployment-manifest.json') -Encoding UTF8
[pscustomobject]@{ PayloadPath = $payloadRoot; FileCount = $records.Count; Bytes = ($records | Measure-Object -Property bytes -Sum).Sum; CredentialsIncluded = $false } | ConvertTo-Json
