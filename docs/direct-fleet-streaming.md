# Direct Fleet desktop streaming

The viewer receives desktop frames over **browser → Cua Fleet gateway → assigned desktop**. Frames and RFB input do not traverse the CtrlLoop Python application, AWS relay, or CloudFront. The website and its authenticated API still use their normal hosting path. A small policy connection from the guest to CtrlLoop enforces session access and human/agent ownership.

## Connection and renewal

Each viewer connection checks the CtrlLoop session, then calls `POST /api/desktop/connection`. The backend resolves the user's existing assigned Fleet claim, checks its guarded `/health` endpoint, and uses the pinned SDK's `sandbox.services.create_signed_url('vnc', expires_in_seconds=120, ...)`. It returns `{url, expires_at}`. The browser passes `url` directly as the second argument to the existing noVNC `RFB` constructor.

The returned WebSocket URL targets `wss://run.cua.ai/api/signed-svc/<credential>/websockify`. Its query passes the same Fleet credential to the guest for session binding; CtrlLoop does not mint a second signing system. Broad Fleet secrets and worker credentials remain on their existing server/guest paths. Signed URLs must not be copied into logs, reports, analytics, screenshots, or issue descriptions. The database stores only a credential hash and its session/assignment binding in `desktop_connections`.

A disconnected viewer obtains a new URL for every attempt. Retries use delays of 1, 2, 4, 8, then at most 15 seconds and continue while the session and visible desktop remain eligible. An interrupted hidden-tab attempt is cancelled; retries resume when the tab returns. Logout, context changes, and desktop reassignment cancel pending requests and stale connections. Only a real application session failure requires CtrlLoop sign-in; the viewer never asks the user to sign into Cua. There is no pixel-relay fallback: the old `/api/desktop` WebSocket endpoint refuses connections.

The 120-second expiry limits **new connection admission**. Fleet expiry or revocation does not by itself terminate an established WebSocket. An otherwise authorized stream can stay open beyond the URL's expiry, but never beyond its CtrlLoop session deadline; it fetches a fresh URL whenever it must reconnect.

Worker deployment now applies a **1280 × 720** source-resolution experiment through `src/desktop_display.py`, under the existing maintenance guard. It changes the guest's existing Hyprland monitor rule before WayVNC captures the screen, preserving 60 Hz and scale 1. Browser fit/fullscreen remain local scaling; image quality, compression and capture frame-rate settings are unchanged. The source pixel count falls from 2,073,600 to 921,600 (55.6% fewer); this is not a measured frame-rate or bandwidth improvement.

The helper requires the known single `Virtual-1` desktop and monitor rule, backs up `~/.config/hypr/monitors.lua` under `~/.local/state/ctrlloop-worker/display/`, and restores the prior configuration if live geometry verification fails. Deployment and maintenance recovery for releases containing this helper verify both the persistent rule and live dimensions; recovery of an older installed release retains its previous policy. Stage-only uploads do not resize the desktop. To end the experiment, revise the deployment's display policy before restoring the saved monitor configuration and reloading Hyprland, so the next deployment does not reapply 720p.

An explicit human/agent mode change closes the old viewer before the request and reconnects after the resulting session state is checked, including recovery from an uncertain response. A successful takeover restores keyboard focus when the user has not focused another control. Other viewer reconnect attempts pause during the mode change.

## Enforced ownership and revocation

`src/desktop_stream.py` runs inside the assigned `src.cloud_worker` process. It owns `0.0.0.0:5900` and connects to WayVNC only at `127.0.0.1:5902`. The previous unguarded websockify service is stopped, disabled, and persistently masked. The gate requires the configured application Origin and verifies the credential through the existing worker authentication and session/assignment records before opening WayVNC.

The shared RFB parser separates viewing traffic from keyboard, pointer, and clipboard input. The guest obtains session access and human ownership at connection admission, then schedules another authorization check every 24 hours if the session remains valid. It enforces the session expiry as a hard local deadline; a delayed reply cannot extend that deadline. The daily exchange has a seven-second deadline. Losing the policy connection or missing authorization renewal closes the viewer.

Keyboard, pointer, and clipboard batches use the guest's current permission and local action lock: **no per-input policy request or database query**. Idle worker polling does not hold that lock. Before agent execution, the guest revokes human input and releases tracked key/button presses under the same lock. It also sends release events during viewer teardown; closing a socket alone does not establish that input was released. Browser `viewOnly` remains a UI setting; the guest enforces permission independently.

Revocation is event-driven. PostgreSQL notifications identify committed changes to the affected session, user, company, worker, or desktop connection. Each backend process closes the matching policy sockets; the guest closes those viewers, which reconnect through fresh authorization when still eligible. Logout, ownership changes, account revocation, reassignment, and active-run changes therefore do not wait for the daily check. A failed database notification listener invalidates every policy connection it protects and refuses new admission until listening resumes. Routine heartbeat and revision updates do not invalidate viewers. These notifications reuse the existing authority records rather than introducing another ownership model.

## Rollout and recovery

Deploy matching **`ctrlloop-direct-v2` backend and guest code as a coordinated release**, together with the updated browser. The v2 authorization exchange is incompatible with v1. Normal backend startup installs the `desktop_connections` table, index, and notification triggers through the existing schema initializer; this is not another account migration. The browser CSP permits the precise `wss://run.cua.ai` destination. Until the assigned guest reports v2 and the expected identity, the backend refuses to issue a viewer URL; admission also requires a healthy notification listener.

Use the existing [assigned-worker deployment procedure](developer-onboarding.md#deploy-only-your-worker), with its explicit environment, email, target, and reviewed source revision. `scripts/deploy_worker.py`:

1. Rejects a source revision without the compatible gate before modifying the guest.
2. Takes the existing deployment lock and enters authorized worker maintenance, preserving current assignment and credential checks.
3. Stops and masks `omarchy-websockify.service`, and records that this desktop requires guarded streaming.
4. Installs and starts the worker, then verifies gate protocol, revision, worker, company, user, and environment before releasing maintenance.

A failed install or health check leaves maintenance enabled and the old bridge masked. `--resume` performs the same gate/assignment/revision check before release; it cannot bypass a missing gate. Roll back only to another compatible v2 revision. The bootstrap `setup-omarchy-novnc.sh` remains available for fresh, unconfigured desktops but refuses an existing worker configuration or retirement marker and shares the deployment lock. Do not unmask the old bridge to recover a configured desktop.

If input release fails, the gate records `desktop-input-recovery-required` in `CTRLLOOP_WORKER_STATE` and refuses healthy operation. An operator must inspect and reset the desktop's input state, resolve the release failure, and explicitly clear that marker before restarting the worker. Deployment and `--resume` do not clear it. If the marker cannot be persisted, the worker exits with status 78; `RestartPreventExitStatus=78` prevents automatic restart from discarding that unresolved state.

This describes source behavior, not a verified v2 deployment or measured latency improvement. After coordinated rollout, verify the actual browser stream, takeover focus, automatic reconnect, agent exclusion, session expiry, and event-driven revocation on the assigned desktop. Measure interactive and passive viewing latency before claiming a speed improvement.
