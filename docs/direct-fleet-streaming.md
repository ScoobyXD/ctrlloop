# Direct Fleet desktop streaming

The viewer receives desktop frames over **browser → Cua Fleet gateway → assigned desktop**. Frames and RFB input do not traverse the CtrlLoop Python application, AWS relay, or CloudFront. The website and its authenticated API still use their normal hosting path. A small policy connection from the guest to CtrlLoop enforces session access and human/agent ownership.

## Connection and renewal

Each viewer connection checks the CtrlLoop session, then calls `POST /api/desktop/connection`. The backend resolves the user's existing assigned Fleet claim, checks its guarded `/health` endpoint, and uses the pinned SDK's `sandbox.services.create_signed_url('vnc', expires_in_seconds=120, ...)`. It returns `{url, expires_at}`. The browser passes `url` directly as the second argument to the existing noVNC `RFB` constructor.

The returned WebSocket URL targets `wss://run.cua.ai/api/signed-svc/<credential>/websockify`. Its query passes the same Fleet credential to the guest for session binding; CtrlLoop does not mint a second signing system. Broad Fleet secrets and worker credentials remain on their existing server/guest paths. Signed URLs must not be copied into logs, reports, analytics, screenshots, or issue descriptions. The database stores only a credential hash and its session/assignment binding in `desktop_connections`.

A disconnected viewer obtains a new URL for every attempt. Retries use delays of 1, 2, 4, 8, then at most 15 seconds and continue while the session and visible desktop remain eligible. An interrupted hidden-tab attempt is cancelled; retries resume when the tab returns. Logout, context changes, and desktop reassignment cancel pending requests and stale connections. Only a real application session failure requires CtrlLoop sign-in; the viewer never asks the user to sign into Cua. There is no pixel-relay fallback: the old `/api/desktop` WebSocket endpoint refuses connections.

The 120-second expiry limits **new connection admission**. Fleet expiry or revocation does not by itself terminate an established WebSocket. An otherwise authorized stream can stay open beyond the URL's expiry; it fetches a fresh URL whenever it must reconnect.

## Enforced ownership and revocation

`src/desktop_stream.py` runs inside the assigned `src.cloud_worker` process. It owns `0.0.0.0:5900` and connects to WayVNC only at `127.0.0.1:5902`. The previous unguarded websockify service is stopped, disabled, and persistently masked. The gate requires the configured application Origin and verifies the credential through the existing worker authentication and session/assignment records before opening WayVNC.

The shared RFB parser separates viewing traffic from keyboard, pointer, and clipboard input. Each input batch requires current human ownership for that session and no active agent run. The backend retains the existing ownership locks until the guest acknowledges the batch. The guest's shorter input deadline and shared worker action lock prevent a delayed grant from overlapping agent execution. The gate tracks forwarded key/button presses and explicitly sends their release events during teardown and before worker input starts; closing a socket alone does not establish that input was released. Browser `viewOnly` remains a UI setting; the guest enforces permission independently.

The guest checks ongoing view access every 5 seconds with a 7-second total exchange deadline and closes established streams on logout, revoked access, company/context changes, or reassignment. Detection plus teardown is bounded to 15 seconds under a functioning guest event loop. Input authorization retains its separate 2-second deadline. A failed policy/API connection closes the stream and refuses further input. These checks, rather than URL expiry alone, enforce revocation.

## Rollout and recovery

Deploy matching backend and guest code as a coordinated release. On an existing company schema, normal backend startup adds the `desktop_connections` table and index through the current schema initializer; this is not another account migration. The browser CSP permits the precise `wss://run.cua.ai` destination. Until the assigned guest reports `ctrlloop-direct-v1` and the expected identity, the backend refuses to issue a viewer URL.

Use the existing [assigned-worker deployment procedure](developer-onboarding.md#deploy-only-your-worker), with its explicit environment, email, target, and reviewed source revision. `scripts/deploy_worker.py`:

1. Rejects a source revision without the compatible gate before modifying the guest.
2. Takes the existing deployment lock and enters authorized worker maintenance, preserving current assignment and credential checks.
3. Stops and masks `omarchy-websockify.service`, and records that this desktop requires guarded streaming.
4. Installs and starts the worker, then verifies gate protocol, revision, worker, company, user, and environment before releasing maintenance.

A failed install or health check leaves maintenance enabled and the old bridge masked. `--resume` performs the same gate/assignment/revision check before release; it cannot bypass a missing gate. Roll back only to another compatible direct-streaming revision. The bootstrap `setup-omarchy-novnc.sh` remains available for fresh, unconfigured desktops but refuses an existing worker configuration or retirement marker and shares the deployment lock. Do not unmask the old bridge to recover a configured desktop.

If input release fails, the gate records `desktop-input-recovery-required` in `CTRLLOOP_WORKER_STATE` and refuses healthy operation. An operator must inspect and reset the desktop's input state, resolve the release failure, and explicitly clear that marker before restarting the worker. Deployment and `--resume` do not clear it. If the marker cannot be persisted, the worker exits with status 78; `RestartPreventExitStatus=78` prevents automatic restart from discarding that unresolved state.

No shared application or guest deployment is implied by this source change. After rollout, verify the actual browser stream, automatic reconnect, manual ownership, agent exclusion, and established-stream revocation on the assigned desktop. Measure viewing latency before claiming a speed improvement.
