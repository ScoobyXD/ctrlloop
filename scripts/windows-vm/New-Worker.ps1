#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# One local prototype worker, not a VM pool manager. No existing disk is reused.
# Microsoft references: docs/research/windows-vm-workers/platform-and-images.md
# https://learn.microsoft.com/windows-hardware/manufacture/desktop/capture-and-apply-windows-system-and-recovery-partitions
# https://learn.microsoft.com/windows-hardware/manufacture/desktop/bcdboot-command-line-options-techref-di
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
$stateRoot = Join-Path $repoRoot '.local\windows-vm'
$workerName = 'ctrlloop-worker-01'
$guestComputerName = 'CTRLLOOP-01'
$workerRoot = Join-Path $stateRoot $workerName
$vhdPath = Join-Path $workerRoot 'windows.vhdx'
$isoPath = Join-Path $stateRoot 'media\Win11_25H2_English_x64_v2.iso'
$payloadPath = Join-Path $stateRoot 'payload\ctrlloop'
$expectedIsoHash = '768984706B909479417B2368438909440F2967FF05C6A9195ED2667254E465E3'
$credentialPath = Join-Path $workerRoot 'private\guest-credential.clixml'
$resultPath = Join-Path $workerRoot 'provisioning.json'
$logPath = Join-Path $workerRoot 'provisioning.log'
$mountedIsoHere = $false
$mountedVhdHere = $false
$createdVhdHere = $false
$passwordText = $null
$securePassword = $null
$unattendXml = $null

function Assert-ProjectPath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($repoRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A provisioning path escaped the project directory.'
    }
    # Reject junctions/symlinks in all existing ancestors, including the repo.
    $candidate = $absolute
    while ($candidate -and $candidate.Length -ge $repoRoot.Length) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Provisioning paths must not contain reparse points.'
            }
        }
        if ($candidate -eq $repoRoot) { break }
        $candidate = [IO.Path]::GetDirectoryName($candidate)
    }
}

function Write-Stage([string]$Message) {
    $entry = '{0} {1}' -f [DateTime]::UtcNow.ToString('o'), $Message
    Write-Host $entry
    Add-Content -LiteralPath $logPath -Value $entry -Encoding UTF8
}

function Assert-OwnedDisk([uint32]$DiskNumber) {
    if (-not $createdVhdHere -or -not $mountedVhdHere) {
        throw 'Disk mutation requires the virtual disk created and mounted by this invocation.'
    }
    $vhd = Get-VHD -Path $vhdPath
    if (-not $vhd.Attached -or $vhd.DiskNumber -ne $DiskNumber -or
        [IO.Path]::GetFullPath($vhd.Path) -ne $vhdPath) {
        throw 'The mounted virtual disk identity changed.'
    }
    $disk = Get-Disk -Number $DiskNumber
    if ($disk.IsBoot -or $disk.IsSystem -or $disk.Size -ne 128GB) {
        throw 'Refusing to modify a boot/system disk or a disk with an unexpected size.'
    }
    return $disk
}

foreach ($path in @($workerRoot, $vhdPath, $isoPath, $payloadPath, $credentialPath)) {
    Assert-ProjectPath $path
}
Import-Module Hyper-V -ErrorAction Stop
Import-Module Dism -ErrorAction Stop
Import-Module Storage -ErrorAction Stop
if ((Get-Service -Name vmms).Status -ne 'Running') {
    throw 'Hyper-V VM Management is not running. Complete host feature installation/restart first.'
}
if (@(Get-VM | Where-Object Name -eq $workerName).Count -gt 0) {
    throw "The VM $workerName already exists. This script never replaces or resumes existing VMs."
}
if (Test-Path -LiteralPath $workerRoot) {
    throw "The worker directory already exists: $workerRoot. Inspect it; this script never wipes or reuses it."
}
$networkSwitch = @(Get-VMSwitch | Where-Object Name -eq 'Default Switch')
if ($networkSwitch.Count -ne 1) { throw 'Exactly one existing Hyper-V Default Switch is required.' }
if (-not (Test-Path -LiteralPath $isoPath -PathType Leaf)) { throw 'The verified Windows 11 ISO is missing.' }
foreach ($required in @('pyproject.toml', 'uv.lock', 'src\main.py', 'scripts\windows-vm\Guest-Worker.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $payloadPath $required) -PathType Leaf)) {
        throw "The allowlisted payload is incomplete: $required."
    }
}
if (@(Get-ChildItem -LiteralPath $payloadPath -Recurse -Force | Where-Object {
    ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}).Count -gt 0) { throw 'The payload contains a reparse point.' }
if (@(Get-ChildItem -LiteralPath $payloadPath -Recurse -Force -File | Where-Object {
    $_.Name -eq '.env' -or ($_.Name -like '.env.*' -and $_.Name -ne '.env.example')
}).Count -gt 0) { throw 'The payload contains an environment-secret file; provision API credentials separately.' }
$manifestRaw = Get-Content -LiteralPath (Join-Path $payloadPath 'deployment-manifest.json') -Raw
$payloadManifest = $manifestRaw | ConvertFrom-Json
if ($payloadManifest.credentialsIncluded -ne $false -or $payloadManifest.guestRoot -ne 'C:\ctrlloop' -or
    @($payloadManifest.files).Count -eq 0) { throw 'Unexpected application deployment manifest.' }
$manifestFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($entry in $payloadManifest.files) {
    $sourcePath = [IO.Path]::GetFullPath((Join-Path $payloadPath $entry.path))
    if ([IO.Path]::IsPathRooted($entry.path) -or $entry.path.Contains(':') -or
        -not $sourcePath.StartsWith($payloadPath + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not $manifestFiles.Add($sourcePath) -or $entry.sha256 -notmatch '^[0-9A-Fa-f]{64}$') {
        throw 'The deployment manifest contains an invalid or duplicate file path/hash.'
    }
    if ((Get-Item -LiteralPath $sourcePath).Length -ne $entry.bytes -or
        (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash -ne $entry.sha256) {
        throw "The staged payload does not match its manifest: $($entry.path)."
    }
}
if (@(Get-ChildItem -LiteralPath $payloadPath -Recurse -Force -File).Count -ne $manifestFiles.Count + 1) {
    throw 'The payload includes files outside its deployment manifest.'
}

New-Item -ItemType Directory -Path $workerRoot -ErrorAction Stop | Out-Null
$privateRoot = New-Item -ItemType Directory -Path (Join-Path $workerRoot 'private')
$privateAcl = New-Object System.Security.AccessControl.DirectorySecurity
$privateAcl.SetAccessRuleProtection($true, $false)
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$privateAcl.SetOwner($currentSid)
foreach ($sid in @($currentSid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl',
        'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $privateAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $privateRoot.FullName -AclObject $privateAcl

try {
    Write-Stage 'Verifying official Windows 11 English ISO SHA256.'
    if ((Get-FileHash -LiteralPath $isoPath -Algorithm SHA256).Hash -ne $expectedIsoHash) {
        throw 'The ISO SHA256 does not match the recorded official English 25H2 media.'
    }
    $iso = Get-DiskImage -ImagePath $isoPath
    if (-not $iso.Attached) {
        $iso = Mount-DiskImage -ImagePath $isoPath -StorageType ISO -PassThru
        $mountedIsoHere = $true
    }
    $isoVolumes = @($iso | Get-Volume | Where-Object DriveLetter)
    if ($isoVolumes.Count -ne 1) { throw 'Expected one mounted ISO volume with a drive letter.' }
    $isoRoot = '{0}:\' -f $isoVolumes[0].DriveLetter
    $imageFiles = @('sources\install.wim', 'sources\install.esd') | ForEach-Object {
        $candidate = Join-Path $isoRoot $_
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
    }
    $imageFiles = @($imageFiles)
    if ($imageFiles.Count -ne 1) { throw 'Expected exactly one Windows installation image on the ISO.' }
    $imagePath = $imageFiles[0]
    $proImages = @(Get-WindowsImage -ImagePath $imagePath | Where-Object ImageName -eq 'Windows 11 Pro')
    if ($proImages.Count -ne 1) { throw 'The ISO must contain exactly one image named Windows 11 Pro.' }
    $imageIndex = $proImages[0].ImageIndex

    Write-Stage "Creating a new 128-GiB dynamic VHDX; Windows 11 Pro image index $imageIndex."
    New-VHD -Path $vhdPath -Dynamic -SizeBytes 128GB -LogicalSectorSizeBytes 512 | Out-Null
    $createdVhdHere = $true
    $mountedVhd = Mount-VHD -Path $vhdPath -NoDriveLetter -PassThru
    $mountedVhdHere = $true
    $disks = @($mountedVhd | Get-Disk)
    if ($disks.Count -ne 1) { throw 'The new VHDX must resolve to exactly one disk.' }
    $diskNumber = [uint32]$disks[0].Number
    $disk = Assert-OwnedDisk $diskNumber
    if ($disk.PartitionStyle -ne 'RAW' -or $disk.NumberOfPartitions -ne 0) {
        throw 'Refusing to initialize a disk that is not new, unpartitioned RAW media.'
    }
    if ($disk.IsOffline) { Set-Disk -Number $diskNumber -IsOffline $false }
    if ($disk.IsReadOnly) { Set-Disk -Number $diskNumber -IsReadOnly $false }
    Initialize-Disk -Number $diskNumber -PartitionStyle GPT | Out-Null
    Assert-OwnedDisk $diskNumber | Out-Null
    # Some storage providers reserve the MSR during initialization. Inspect the
    # actual new layout rather than creating a duplicate reserved partition.
    $initialPartitions = @(Get-Partition -DiskNumber $diskNumber -ErrorAction SilentlyContinue)
    if ($initialPartitions.Count -gt 0 -and ($initialPartitions.Count -ne 1 -or
        $initialPartitions[0].GptType -ne '{E3C9E316-0B5C-4DB8-817D-F92DF00215AE}' -or
        $initialPartitions[0].Size -ne 16MB)) {
        throw 'Initialization produced an unexpected partition layout on the new VHDX.'
    }
    $efi = New-Partition -DiskNumber $diskNumber -Size 256MB -AssignDriveLetter `
        -GptType '{C12A7328-F81F-11D2-BA4B-00A0C93EC93B}'
    $efi | Format-Volume -FileSystem FAT32 -NewFileSystemLabel 'System' -Force -Confirm:$false | Out-Null
    if ($initialPartitions.Count -eq 0) {
        New-Partition -DiskNumber $diskNumber -Size 16MB `
            -GptType '{E3C9E316-0B5C-4DB8-817D-F92DF00215AE}' | Out-Null
    }
    $windows = New-Partition -DiskNumber $diskNumber -UseMaximumSize -AssignDriveLetter
    Assert-OwnedDisk $diskNumber | Out-Null
    $windows | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'Windows' -Force -Confirm:$false | Out-Null
    $windowsRoot = '{0}:\' -f $windows.DriveLetter
    $efiRoot = '{0}:' -f $efi.DriveLetter
    if (-not $windows.DriveLetter -or -not $efi.DriveLetter -or
        $windowsRoot -eq ($env:SystemDrive + '\') -or $windows.DiskNumber -ne $efi.DiskNumber) {
        throw 'Guest volume identity validation failed.'
    }

    Write-Stage 'Applying Windows to the new guest disk. DISM progress follows; this can take several minutes.'
    $scratch = New-Item -ItemType Directory -Path (Join-Path $workerRoot 'scratch')
    & "$env:SystemRoot\System32\dism.exe" /Apply-Image "/ImageFile:$imagePath" "/Index:$imageIndex" `
        "/ApplyDir:$windowsRoot" /CheckIntegrity /Verify "/ScratchDir:$($scratch.FullName)" `
        "/LogPath:$(Join-Path $workerRoot 'dism.log')"
    if ($LASTEXITCODE -ne 0) { throw "Applying the Windows image failed (exit $LASTEXITCODE)." }
    Assert-OwnedDisk $diskNumber | Out-Null
    Write-Stage 'Writing UEFI boot files only to the new guest EFI partition.'
    # /s is essential: it targets the guest ESP and prevents host NVRAM entries.
    & "$env:SystemRoot\System32\bcdboot.exe" (Join-Path $windowsRoot 'Windows') /s $efiRoot /f UEFI
    if ($LASTEXITCODE -ne 0) { throw "Configuring guest boot files failed (exit $LASTEXITCODE)." }
    if (-not (Test-Path -LiteralPath ($efiRoot + '\EFI\Boot\bootx64.efi'))) {
        throw 'The guest EFI default boot file is missing.'
    }

    Write-Stage 'Copying the allowlisted application payload to the guest.'
    $guestAppRoot = Join-Path $windowsRoot 'ctrlloop'
    New-Item -ItemType Directory -Path $guestAppRoot | Out-Null
    foreach ($entry in $payloadManifest.files) {
        $destinationPath = Join-Path $guestAppRoot $entry.path
        New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $payloadPath $entry.path) -Destination $destinationPath
        if ((Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256).Hash -ne $entry.sha256) {
            throw "The copied guest payload does not match its manifest: $($entry.path)."
        }
    }
    [IO.File]::WriteAllText((Join-Path $guestAppRoot 'deployment-manifest.json'), $manifestRaw,
        [Text.UTF8Encoding]::new($false))
    $guestRuntime = Join-Path $guestAppRoot '.local\guest-worker'
    New-Item -ItemType Directory -Path $guestRuntime -Force | Out-Null

    # Password appears only in process memory and the guest's setup data. The
    # host-side credential file uses Windows current-user DPAPI, not plaintext.
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $randomBytes = New-Object byte[] 32
        $rng.GetBytes($randomBytes)
        $passwordText = 'Ct1!' + [Convert]::ToBase64String($randomBytes)
        [Array]::Clear($randomBytes, 0, $randomBytes.Length)
    } finally { $rng.Dispose() }
    $securePassword = ConvertTo-SecureString $passwordText -AsPlainText -Force
    $credential = [Management.Automation.PSCredential]::new("$guestComputerName\ctrlloop", $securePassword)
    $credential | Export-Clixml -LiteralPath $credentialPath -Encoding UTF8

    # LogonCount has a documented +1 issue. One first-logon script disables it,
    # deletes the autologon secret and consumes the guest-only answer file.
    # https://learn.microsoft.com/windows-hardware/customize/desktop/unattend/microsoft-windows-shell-setup-autologon-logoncount
    $cleanupScript = @'
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'CTRLLOOP-01' -or $env:USERNAME -ne 'ctrlloop') {
    throw 'This first-logon script belongs only to the CTRLLOOP-01 guest account.'
}
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -LiteralPath $winlogon -Name AutoLogonCount -Type DWord -Value 0
Set-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon -Value '0'
Remove-ItemProperty -LiteralPath $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
foreach ($answer in @('C:\Windows\Panther\unattend.xml', 'C:\Windows\Panther\Unattend\unattend.xml')) {
    if (Test-Path -LiteralPath $answer -PathType Leaf) { Remove-Item -LiteralPath $answer -Force }
}
$workerUser = Get-LocalUser -Name 'ctrlloop'
& "$env:SystemRoot\System32\icacls.exe" 'C:\ctrlloop' /grant "*$($workerUser.SID.Value):(OI)(CI)M" /T /Q
if ($LASTEXITCODE -ne 0) { throw 'Setting the guest worker application ACL failed.' }
[DateTime]::UtcNow.ToString('o') | Set-Content -LiteralPath 'C:\ctrlloop\.local\guest-worker\first-logon-complete.txt' -Encoding ASCII
'@
    $cleanupScript | Set-Content -LiteralPath (Join-Path $guestRuntime 'Complete-FirstLogon.ps1') -Encoding UTF8
    $passwordXml = [Security.SecurityElement]::Escape($passwordText)
    $unattendXml = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>$guestComputerName</ComputerName>
      <TimeZone>Pacific Standard Time</TimeZone>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>0409:00000409</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE><HideEULAPage>false</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE></OOBE>
      <UserAccounts><LocalAccounts><LocalAccount wcm:action="add"><Name>ctrlloop</Name><DisplayName>ctrlloop</DisplayName><Group>Administrators</Group><Password><Value>$passwordXml</Value><PlainText>true</PlainText></Password></LocalAccount></LocalAccounts></UserAccounts>
      <AutoLogon><Username>ctrlloop</Username><Domain>$guestComputerName</Domain><Enabled>true</Enabled><LogonCount>1</LogonCount><Password><Value>$passwordXml</Value><PlainText>true</PlainText></Password></AutoLogon>
      <FirstLogonCommands><SynchronousCommand wcm:action="add"><Order>1</Order><Description>Finish one-time worker sign-in</Description><CommandLine>powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File C:\ctrlloop\.local\guest-worker\Complete-FirstLogon.ps1</CommandLine></SynchronousCommand></FirstLogonCommands>
    </component>
  </settings>
</unattend>
"@
    $panther = Join-Path $windowsRoot 'Windows\Panther'
    New-Item -ItemType Directory -Path $panther -Force | Out-Null
    $unattendXml | Set-Content -LiteralPath (Join-Path $panther 'unattend.xml') -Encoding UTF8
    $passwordText = $null
    $passwordXml = $null
    $unattendXml = $null
    Write-Stage 'Guest setup prepared; encrypted credential retained in the private host directory.'

    Dismount-VHD -Path $vhdPath
    $mountedVhdHere = $false
    Write-Stage 'Registering the stopped generation-2 VM with Secure Boot and virtual TPM.'
    $vm = New-VM -Name $workerName -Generation 2 -MemoryStartupBytes 8GB `
        -VHDPath $vhdPath -Path $workerRoot -SwitchName $networkSwitch[0].Name
    Set-VMProcessor -VM $vm -Count 4
    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
    Set-VMKeyProtector -VM $vm -NewLocalKeyProtector
    Enable-VMTPM -VM $vm
    Set-VM -VM $vm -AutomaticStartAction Nothing -AutomaticStopAction ShutDown -AutomaticCheckpointsEnabled $false
    Set-VMFirmware -VM $vm -FirstBootDevice (Get-VMHardDiskDrive -VM $vm)
    @{
        status = 'provisioned-not-started'
        vmName = $workerName
        vmId = $vm.Id.ToString()
        computerName = $guestComputerName
        vhdPath = $vhdPath
        isoSha256 = $expectedIsoHash
        imageName = 'Windows 11 Pro'
        imageIndex = $imageIndex
        credentialPath = $credentialPath
        completedUtc = [DateTime]::UtcNow.ToString('o')
        recoveryPartition = $false
        firstBootNeedsOobeReview = $true
    } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    Write-Stage 'Provisioned and left stopped. First boot may await OOBE/EULA review before the bounded autologon.'
    Write-Stage 'No host reboot, VM startup, API credential, or driver launch was performed.'
} catch {
    Write-Stage 'Provisioning failed. The new worker artifacts were retained for inspection; no existing VM/disk was removed.'
    throw
} finally {
    $passwordText = $null
    $passwordXml = $null
    $unattendXml = $null
    if ($null -ne $securePassword) { $securePassword.Dispose() }
    if ($mountedVhdHere) {
        try { Dismount-VHD -Path $vhdPath -ErrorAction Stop }
        catch { Write-Warning "Could not detach the newly created VHDX; inspect $vhdPath before further work." }
    }
    if ($mountedIsoHere) {
        try { Dismount-DiskImage -ImagePath $isoPath -ErrorAction Stop | Out-Null }
        catch { Write-Warning 'Could not detach the installation ISO mounted by this invocation.' }
    }
}
