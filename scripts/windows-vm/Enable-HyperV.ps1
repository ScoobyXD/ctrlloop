[CmdletBinding()]
param(
    [string]$ResultPath = (Join-Path $PSScriptRoot '..\..\.local\windows-vm\hyperv-feature-result.json')
)

$ErrorActionPreference = 'Stop'
$result = [ordered]@{ startedUtc = [DateTime]::UtcNow.ToString('o'); status = 'starting' }
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script as an administrator to enable Hyper-V.'
    }
    $feature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All
    if ($feature.State -eq 'Enabled') {
        $result.status = 'already_enabled'
        $result.restartNeeded = $false
    } else {
        $enabled = Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All -NoRestart
        $result.status = 'enabled'
        $result.restartNeeded = [bool]$enabled.RestartNeeded
    }
    $result.featureState = [string](Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All).State
} catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
} finally {
    $result.finishedUtc = [DateTime]::UtcNow.ToString('o')
    $resultFullPath = [IO.Path]::GetFullPath($ResultPath)
    New-Item -ItemType Directory -Path (Split-Path -Parent $resultFullPath) -Force | Out-Null
    $result | ConvertTo-Json | Set-Content -LiteralPath $resultFullPath -Encoding UTF8
}
if ($result.status -eq 'failed') { exit 1 }
