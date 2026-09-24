# Current ctrlloop stack inside one Windows guest

This is the guest portion of the single persistent Hyper-V pilot. The host
provisioning script owns VM creation and payload transfer. All application
files below land under `C:\ctrlloop`. No headless browser, new Jev questions,
driver source changes, or background scheduling service are introduced.

## Package allowlist

Copy the current working-tree versions, including intended uncommitted runtime
edits. Record the copied files' SHA256 values in the host deployment manifest;
the Git commit alone does not identify this snapshot.

| Source | Destination relative to `C:\ctrlloop` |
| --- | --- |
| `src/*.py`, files directly inside `src` only | `src/<same filename>` |
| `pyproject.toml` | `pyproject.toml` |
| `uv.lock` | `uv.lock` |
| `scripts/windows-vm/Guest-Worker.ps1` | `scripts/windows-vm/Guest-Worker.ps1` |
| This document | `scripts/windows-vm/GUEST-DEPLOYMENT.md` |
| Existing patched `cua-driver.exe` | `.local/cua-driver/bin/cua-driver.exe` |
| The same release's `cua-driver-uia.exe` and `cua-cursor-theme.exe` | `.local/cua-driver/bin/<same filename>` |
| Pinned CUA checkout's root `LICENSE.md` | `.local/cua-driver/LICENSE.md` |
| Inspected `uv.exe` | `.local/tools/uv.exe` |

Do not recursively copy the repository or `.local`. Exclude `.git`, `.venv`,
`__pycache__`, build/source/toolchain caches, `.env*`, keys, credentials, host
browser profiles, reports, screenshots, and previous run artifacts. The guest
downloads its own managed Python and installs dependencies from `uv.lock`.

Inspected local inputs on September 24, 2026:

- Driver release directory:
  `C:\Users\Jonat\.cua-driver\packages\releases\0.28.2-direct-accessibility-only-x86_64-pc-windows-msvc`.
- Driver version `0.28.2`; upstream commit
  `fc188250b4ca8549b8e61f937fdb1fb560770e86`, with the tracked Windows-key and
  direct-accessibility patches. The staged executable's SHA256 is
  `8DAAC87AE75FFDC39E92459147083B05A50EFE8E4ACCF67B23F0CF375679F5E4`, matching
  `docs/cua-driver/direct-accessibility-validation.json`.
- CUA notice:
  `C:\Users\Jonat\ctrlloop\.local\cua-driver\source\LICENSE.md`.
- uv: `C:\Users\Jonat\AppData\Local\hermes\bin\uv.exe`, version `0.11.26`,
  SHA256 `DEEAA21AAC3E3E40B3FA00788208AA9A319CEFBB3C2AA598CF580565A82EBC34`.
- Python preference `3.11.15`, Jev `jev-1.13.0`, and direct dependency pins come
  from the copied `pyproject.toml`; transitive versions/hashes come from
  `uv.lock`. Bootstrap reads the Python preference from that project file.

The companion binaries are retained as part of the existing release bundle.
Their presence is not a claim that unsigned UIAccess/elevated-app automation
is enabled. The pilot uses ordinary guest applications at compatible privilege
levels. No driver rebuild or installed host runtime replacement is needed.

## Bootstrap

Prerequisites: a normally provisioned x64 Windows Hyper-V guest, the allowlisted
payload at `C:\ctrlloop`, a configured guest user, and working HTTPS/DNS access
to download managed Python and the locked Python packages. Use Windows
PowerShell 5.1 or newer. Bootstrap can run through PowerShell Direct:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ctrlloop\scripts\windows-vm\Guest-Worker.ps1 -Action Bootstrap
```

The process-local execution-policy flag does not change the guest's persistent
policy. Bootstrap uses uv's `--no-bin --no-registry` Python installation options,
stores Python/cache files under `.local`, and runs `uv sync --locked` into
`C:\ctrlloop\.venv`. It checks imports without contacting Jev or controlling UI.

`Guest-Worker.ps1` refuses to execute outside a Microsoft Hyper-V guest or from
any deployment location other than `C:\ctrlloop`. It verifies the driver hash
before every mode. All its child processes inherit
`CUA_DRIVER_RS_TELEMETRY_ENABLED=0`; the existing Python transport also enforces
this for its MCP client. No machine-wide environment settings are changed.

## Provision the Jev key separately

The payload intentionally contains no secret. The existing CLI accepts
`TYPESAFE_API_KEY` in its process environment or the existing default
`C:\ctrlloop\.env` file.

For this one persistent worker, the host provisioner can read only the existing
`TYPESAFE_API_KEY` through the existing dotenv loader, hold it in memory, and
transfer that value through the authenticated PowerShell Direct session. Do not
print it, place it in a command line, write it into the package/seed ISO, or copy
the entire host `.env`. Do not enable a transcript around the transfer.

Before writing the guest `.env`, create an empty file and replace inherited
permissions with access only for the intended guest worker account, SYSTEM,
and Administrators; then write the single key using an in-memory remote
argument. Keep it outside deployment manifests and collected report folders.
An alternative is to enter it directly in the guest during setup. If using a
process environment instead, it must be present in the process launching the
run, not merely in an earlier PowerShell Direct process.

No key has been read, copied, displayed, or generated by the guest script.

## Start in the guest's logged-in desktop

Sign into the intended guest user through VMConnect and run this **inside that
desktop**, or launch the same command using an interactive-token task for that
already logged-in account:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ctrlloop\scripts\windows-vm\Guest-Worker.ps1 -Action StartDriver
```

This starts the copied driver hidden, using the existing pipe
`\\.\pipe\ctrlloop-direct-accessibility`. It records only the owned process ID,
start time, and session ID under `.local\guest-worker`; it does not install a
service, enable auto-login, weaken UAC, or register a task. PowerShell Direct
alone does not establish the interactive desktop required by CUA.

Startup checks process identity/liveness. The first actual application call
still checks the connected daemon's direct-accessibility capability through
the existing MCP adapter. UI observation/action success and minimized-viewer
operation require their separate manual proof.

## Run one bounded invocation

Prepare a UTF-8 request file in the guest. If the workflow needs text, prepare
a separate UTF-8 file containing the exact first-typing value. Run in the same
guest user's interactive session:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ctrlloop\scripts\windows-vm\Guest-Worker.ps1 -Action Run -RequestFile C:\ctrlloop\request.txt -TextFile C:\ctrlloop\text.txt -MaxIterations 3
```

Omit `-TextFile` for click-only invocations. The wrapper calls the existing
`.venv\Scripts\python.exe -m src.main` with the pinned executable and dedicated
pipe. It holds an exclusive local file handle for the whole invocation, so
two instances of this wrapper cannot run native loops simultaneously. Other
manual driver clients must also stay idle while the workflow owns the guest.

Existing stdout/results and `C:\ctrlloop\reports` remain the evidence contract.
The loop uses prepared text once, retains one selected real window, and treats
the iteration limit as a bound rather than proof of task completion. Stop on
errors or ambiguous input outcomes; do not automatically rerun the request.

Keep target apps restored inside the guest. Minimize the host's VMConnect
viewer while the host user continues working, then inspect reports and guest
state to establish whether progress occurred during minimization. No action
is delivered by clicking or typing into the host's VMConnect window.

After active work ends, stop only the recorded guest daemon if needed:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ctrlloop\scripts\windows-vm\Guest-Worker.ps1 -Action StopDriver
```

The existing `stop --expected-pid` route checks the intended process. The
guest disk, applications, sign-ins, inputs, and reports remain intact. Viewer
minimization is separate from closing/disconnecting, guest locking, or stopping
the VM; qualify those transitions separately.
