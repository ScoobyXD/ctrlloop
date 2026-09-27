# Omarchy Fleet trial

This standalone helper provisions and inspects the Omarchy Fleet trial on the existing `main` checkout. Verification uses the same async `src.driver.CuaDriver` adapter as the remote desktop control loop. The helper does not run Jev.

## Current status

Replacement trial restored and verified September 27, 2026, after the previous pool was confirmed absent with no remaining claims. The new worker's computer-server and Driver MCP respond, and the authenticated Chrome viewer visibly shows the normal Omarchy desktop and terminal without safe mode. Keyboard and mouse actions were not tested on this replacement. The installer includes the prior trial's seat and startup-order fixes described below. This trial already exists; use `status`, `verify`, and `viewer` rather than running `create` again.

[Open the live desktop](https://run.cua.ai/api/svc/ctrlloop-omarchy-20260927-1708/ctrlloop-omarchy-20260927-1708-e33048ae-vnc/vnc.html?autoconnect=1&resize=scale&reconnect=1) in the Chrome profile signed into Cua. The viewer is installed and its authenticated connection was visually verified.

- Pool and namespace: `ctrlloop-omarchy-20260927-1708`.
- Claim: `omarchy-trial`, `Bound` to sandbox `ctrlloop-omarchy-20260927-1708-e33048ae`; its SDK reconnect reference is saved in `.local/omarchy-fleet/claim.json`.
- Observed capacity: exactly `1` replica and one `Bound` claim, with `ready_replicas=0` available/unclaimed workers; autoscaling maximum remains `1`. Fleet's [status logic explicitly treats a fully claimed one-worker pool as healthy](https://github.com/trycua/cua/blob/0e75660ce4c2edda519e0c795fa3ad98abf4e76f/libs/fleet/sdk/src/status.rs#L97); `verify` checks the claimed guest's live services separately.
- Pool created: `2026-09-27T17:08:51Z` (September 27, 10:08:51 AM PDT).
- Pool creation-age TTL: `21600` seconds; configured expiry is `2026-09-27T23:08:51Z` (September 27, 4:08:51 PM PDT). Controller deletion is asynchronous.
- Claim created: `2026-09-27T17:08:53Z` (September 27, 10:08:53 AM PDT); actual shutdown deadline is `2026-09-27T23:08:53Z` (September 27, 4:08:53 PM PDT), with policy `Delete` and `auto_renew=false`. Its creation-age TTL is also `21600` seconds.
- Guest verification: `2026-09-27T17:11:54.829002+00:00`. Omarchy `4.0.2`, Cua Driver CLI `0.24.0`, and Hyprland PID `609` with `--watchdog-fd 4`, without `--safe-mode`.
- Driver MCP verification: `2026-09-27T17:11:56.409823+00:00`. MCP reported Driver `0.24.0`; read-only `list_windows` returned the terminal with PID `716` and window ID `94562714966032`. A [desktop screenshot](C:/Users/Jonat/ctrlloop/.local/omarchy-fleet/desktop.png) was saved; the authenticated browser viewer supplied the visual confirmation. These process/window identifiers describe this observation and may change later.
- The prior trial's evidence is archived in `.local/omarchy-fleet-expired-20260927-1708`. Its `2026-09-27T08:55:40Z` verification describes the expired worker. Current evidence is in `.local/omarchy-fleet/`.

## Configuration and cost

- One worker: 4 vCPU, 6144 MiB RAM, `replicas=1`, and autoscaling minimum/initial/maximum all set to `1`.
- Official published amd64 VM image: `public.ecr.aws/k5j5w0x5/cua-omarchy-workspace@sha256:dc448dcc986f443980dfb3586f1b91d8699091ad96a74b369016535f0dbb0342`.
- Python 3.11–3.13, `cua-sandbox==0.7.0`, `cua-fleet==0.1.17`. Dependencies install separately from the project's application environment.
- Guest services: computer-server `8000`, Driver MCP `3000`, noVNC `5900`. The image's upstream Driver is used as supplied; `verify` records the actual version.
- Viewer setup pins noVNC `1.7.0` and websockify `0.13.0`, with the image's WayVNC on loopback `5902`, explicitly using the `Hyprland` seat. WayVNC `0.10.1-ae53f07` was recorded in the prior trial; its version was not separately recorded on the replacement.
- Six-hour creation-age TTLs on pool and claim. Setup time consumes this allowance; `status` reports timestamps and configured expiry. No automatic renewal. Deletion is asynchronous, so check resource absence after expiry.
- Published CPU/RAM rates checked September 27, 2026: `$0.044625/vCPU/hour + $0.0223125/GB/hour`. The example allocation is approximately **$0.312375/hour**, **$1.87425 for six hours**, before any other account charges. Check the live resource count and usage page.

## Credentials

Sign in at [Cua Fleet](https://run.cua.ai). Add billing yourself under Settings if required. Under API keys, create a User key for this trial (or use an existing authorized key), and store the Client ID and Client Secret in the repository-root `.env.fleet` file. It is ignored by the existing `.gitignore`.

```dotenv
CUA_CLIENT_ID=<client ID>
CUA_CLIENT_SECRET=<client secret>
CUA_TOKEN_URL=https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token
```

Do not put credentials in commands, chat, tracked files, or logs. `FLEETS_TOKEN` is also supported but takes precedence over client credentials and cannot refresh itself; remove a stale token from the environment before using a User key. Browser sign-in and SDK credentials are separate.

## Provision and verify

Run from `C:\Users\Jonat\ctrlloop` in PowerShell. Replace the example pool suffix with a unique lowercase value. Only run `create` once. The helper records the dedicated pool before provisioning and refuses to create another while that record exists.

```powershell
uv --cache-dir .local/uv-cache run --env-file .env.fleet scripts/omarchy-fleet.py access
uv --cache-dir .local/uv-cache run --env-file .env.fleet scripts/omarchy-fleet.py create --pool ctrlloop-omarchy-<unique-suffix>
uv --cache-dir .local/uv-cache run --env-file .env.fleet scripts/omarchy-fleet.py status
uv --cache-dir .local/uv-cache run --env-file .env.fleet scripts/omarchy-fleet.py verify
uv --cache-dir .local/uv-cache run --env-file .env.fleet scripts/omarchy-fleet.py viewer
```

`verify` checks Hyprland and records OS/Driver version through computer-server. The shared `CuaDriver` adapter handles MCP initialization, schema/version validation, a read-only `list_windows` call, the desktop screenshot, and Driver session cleanup. The standalone script imports this adapter without loading the CLI or TypeSafe SDK. Evidence remains in `guest.json`, `desktop.png`, and `driver.json`; the latter records the adapter's server metadata, session metadata, and native window observation. A successful computer-server connection alone is insufficient. Open the saved screenshot to confirm the desktop visually. Check Fleet's pool inventory shows one computer and one claim.

`viewer` installs the documented bridge inside the guest and verifies its HTTP page through Fleet. Open the printed noVNC URL in the same browser profile signed into Cua, or use the claim's Desktop pane. Confirm the display connects. The URL uses the **bound sandbox name**, not the claim name:

```text
https://run.cua.ai/api/svc/<namespace>/<sandbox>-vnc/vnc.html?autoconnect=1&resize=scale&reconnect=1
```

The helper intentionally disconnects without releasing the claim. Closing its process or the browser viewer leaves the desktop available until explicit cleanup or expiry. Trial storage is disposable; download anything needed before cleanup.

### Prior trial: WayVNC seat compatibility and recovery

On the expired pool `ctrlloop-omarchy-20260927-01a0e1e6`, the first noVNC connection triggered a Hyprland `0.56.2` crash around `2026-09-27T08:35:04Z`, followed by a safe-mode desktop. The stack reached `CSignalBase::registerListenerInternal` through `CCursorshareSession::CCursorshareSession`. That guest had the upstream `cua-hyprland-plugin` `0.1.0` loaded. This history explains the installer fixes retained for the replacement trial.

Source inspection identifies the likely cause: [WayVNC selects the first unoccupied seat by default](https://github.com/any1/wayvnc/blob/ae53f07/src/main.c#L1882), which can select a Cua synthetic seat. [Cua's synthetic pointer resources](https://github.com/trycua/cua/blob/0e75660ce4c2edda519e0c795fa3ad98abf4e76f/libs/cua-driver/hyprland-plugin/src/input_experiment.cpp) do not contain Hyprland's native pointer data, while [Hyprland's cursor-capture protocol assumes a native pointer](https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/protocols/ImageCopyCapture.cpp#L471) and [dereferences it in the constructor](https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/managers/screenshare/CursorshareSession.cpp#L13).

`scripts/setup-omarchy-novnc.sh` now writes this user-service override, selecting Hyprland's [native seat name](https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/protocols/core/Seat.hpp#L21):

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/wayvnc --seat=Hyprland 127.0.0.1 5902
```

The override preserves manual mouse and keyboard input. It changes WayVNC configuration only; no Driver or plugin code was changed. `--render-cursor` alone still creates a separate cursor-capture session in this WayVNC version, so it does not address the seat mismatch.

In the prior trial, loading the configuration in Hyprland's recovery session did not restore a complete desktop. A guest `systemctl --no-ask-password reboot` restarted that same VM through its existing OS permissions and restored normal Omarchy. The installed viewer files survived. This was not SDK `Sandbox.restart()`, which would scale Fleet capacity down and up.

That reboot also exposed a systemd ordering cycle in the bridge unit. The corrected `After=graphical-session.target wayvnc.service` ordering passed `systemd-analyze --user verify`, and both viewer services became active. Final checks on the expired worker confirmed normal Omarchy `4.0.2`, Driver `0.24.0`, live noVNC display, typed-and-cleared keyboard input, workspace switching with the mouse, and a successful read-only Driver MCP call while the viewer remained connected. These prior results do not establish verification of the replacement worker.

## Reconnect to Driver

The replacement's verified [Driver MCP endpoint](https://run.cua.ai/api/svc/ctrlloop-omarchy-20260927-1708/ctrlloop-omarchy-20260927-1708-e33048ae-mcp/mcp) requires Fleet authentication. It accepts MCP requests rather than serving a browser page. Use the shared adapter from the repository root to connect through the held claim:

```python
from pathlib import Path
from src.driver import CuaDriver

driver = CuaDriver(Path(".local/omarchy-fleet/claim.json"))
try:
    await driver.start()
    windows = await driver.call("list_windows")
finally:
    await driver.close()  # Ends this Driver session and retains the Fleet claim.
```

The adapter uses `Sandbox.from_dict()` to reconnect to the existing reservation and disconnects on close; it does not save or restore a disk image or change the expiry. Do not use `async with pool.claim()` for retention: its exit releases the claim. The main Jev loop uses this same adapter and saved claim reference.

## Evidence and cleanup

The ignored `.local/omarchy-fleet/` directory records `pool.txt`, the SDK's unmodified `claim.json` reference, resource status, version/observation results, and `desktop.png`. No credential is intentionally saved in these artifacts. If provisioning is interrupted, inspect the recorded exact pool in Fleet before retrying; do not pick another name and accidentally leave paid capacity behind.

To delete this trial now, obtain its exact name from `.local/omarchy-fleet/pool.txt`, then run:

```powershell
$trialPool = (Get-Content .local/omarchy-fleet/pool.txt -Raw).Trim()
uv --cache-dir .local/uv-cache run --env-file .env.fleet scripts/omarchy-fleet.py cleanup --pool $trialPool
```

Cleanup deletes the dedicated Fleet namespace, including its templates, pool, claims, and computers, and checks namespace absence. It refuses a name that differs from the saved trial. If deletion is still pending, refresh Fleet until the namespace and its resources are absent and check Usage. Deleting only the pool is not the helper's complete namespace cleanup. Local evidence is retained. `Pool.get(...).delete()` alone does not remember template ownership.

After finishing the trial, revoke a trial-only API key in Fleet and remove its local secret references. Revoking a key does not delete running resources. A future trial requires deliberate removal/archiving of the old local state only after its cloud resources have been verified absent.

## Official references

- [Fleet credentials](https://cua.ai/docs/how-to-guides/sandbox/set-up-fleet-credentials)
- [Published Omarchy recipe](https://cua.ai/docs/how-to-guides/sandbox/run-omarchy-on-cloud-fleet)
- [noVNC recipe](https://cua.ai/docs/how-to-guides/sandbox/connect-to-omarchy-with-novnc)
- [Pool and claim lifecycle](https://cua.ai/docs/reference/sandbox-sdk/pool)
- [Expiry semantics](https://cua.ai/docs/how-to-guides/sandbox/expire-pools-and-claims)
- [Cleanup and recovery](https://cua.ai/docs/how-to-guides/sandbox/recover-first-cloud-fleet)
- [Autoscaling schema](https://github.com/trycua/cua/blob/0e75660ce4c2edda519e0c795fa3ad98abf4e76f/libs/fleet/sdk-schema/src/warmpool.rs)
- [Pricing](https://cua.ai/#pricing)
