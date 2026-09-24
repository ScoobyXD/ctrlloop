# One persistent Windows worker

This deploys the current ctrlloop Python/Jev/CUA stack to one Hyper-V Windows 11
guest. It adds no headless browser, new Jev questions, or VM scheduler.

## Current setup status — September 24, 2026

- Official Windows 11 25H2 English x64 ISO downloaded to
  `.local/windows-vm/media/Win11_25H2_English_x64_v2.iso` and SHA256 checked
  against Microsoft's download page. The source/hash receipt is
  `.local/windows-vm/media/windows-media.json`.
- The current working-tree application, locked dependency definitions, patched
  CUA 0.28.2 binaries, and uv are staged under
  `.local/windows-vm/payload/ctrlloop`. Its deployment manifest records hashes
  of all 18 copied files; no API credentials are included.
- Hyper-V remains disabled. Windows canceled the administrator consent prompt.
  No VM has been created or booted, and no guest runtime has been verified.
- The latest execution receipt is `.local/windows-vm/deployment-status.json`.

## Host steps

From an elevated Windows PowerShell in the repository:

```powershell
.\scripts\windows-vm\Enable-HyperV.ps1
```

Read `.local/windows-vm/hyperv-feature-result.json`. The script never restarts
Windows. If Windows requires a restart, save work and arrange that separately.
Do not proceed until Hyper-V's VM Management service and Default Switch are
available.

The already-staged payload can be reused. `Stage-Worker.ps1` creates a fresh
payload only and refuses an existing one, preserving its recorded snapshot.

```powershell
.\scripts\windows-vm\New-Worker.ps1
```

The creation script verifies the official ISO, applies its Windows 11 Pro image
to a newly created disk, copies the payload, and registers `ctrlloop-worker-01`:
generation 2, Secure Boot, virtual TPM, 4 vCPUs, 8 GiB RAM, and a 128 GiB dynamic
VHDX. It refuses existing worker directories/VMs and never formats an existing
host disk. The guest is left stopped. Its local account credential is stored
under the worker's private directory using current-user Windows DPAPI; it is
not printed or committed. No Windows activation or license purchase is made.

Start the VM, complete any visible first-boot Windows setup, then use
PowerShell Direct with that guest credential to run the guest bootstrap.
CUA must subsequently start in the guest's logged-in interactive desktop,
not in the PowerShell Direct session. See [guest deployment](GUEST-DEPLOYMENT.md)
for the exact bootstrap, driver, invocation, and separate API-key steps.

## Evidence still required

Verify guest dependency installation and the connected patched driver's native
observation/action capability. Run a bounded invocation against a harmless
guest application, then verify continued execution while the host viewer is
minimized. Finally check that the saved guest files survive an orderly
shutdown/start. Installation scripts and process liveness alone are not proof
that these operations work. No automated tests are introduced.
