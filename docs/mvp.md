# [CTRL] Loop MVP

The MVP has a typed-chat website, saved instructions and run history in PostgreSQL, a worker inside the existing Omarchy VM, and an embedded noVNC desktop viewer. The sample request is prefilled as **“I want to see the latest posts on X”**. OpenAI generates the instruction; the worker uses the existing Jev selection rounds and native Cua Driver to execute the supported navigation.

The live site is [CTRL Loop](https://d1t0sm9jhdinfi.cloudfront.net). The deployed service, authenticated viewer, and online worker have been verified. Sample instruction generation and execution are tracked separately in [acceptance evidence](#acceptance-evidence) below.

## Components and pins

| Component | Implementation |
| --- | --- |
| Website | [`web/`](../web/): static HTML, CSS, and JavaScript; no frontend build or external asset CDN |
| Application API | [`src/webapp.py`](../src/webapp.py): FastAPI, authentication, instruction generation, delivery, events, and worker ownership |
| Database | [`src/schema.sql`](../src/schema.sql): `sessions`, `workers`, `loops`, `runs`, and `events` |
| Worker service | [`src/cloud_worker.py`](../src/cloud_worker.py): outbound HTTPS polling, guest process lock, crash journal, heartbeats, and result delivery |
| Bounded executor | [`src/worker.py`](../src/worker.py): validates the saved instruction, narrows the permitted native actions, and observes the resulting page |
| Shared selection pipeline | Existing [`src/pipeline.py`](../src/pipeline.py), [`src/selection.py`](../src/selection.py), [`src/jev_questions.py`](../src/jev_questions.py), and [`src/reporting.py`](../src/reporting.py) |
| Native transport | Existing [`src/driver.py`](../src/driver.py), now supporting either authenticated Fleet transport or VM loopback using the same native schemas |
| Desktop gateway | [`src/desktop_gateway.py`](../src/desktop_gateway.py): authenticated same-origin WebSocket proxy for the exact configured sandbox, with server-side RFB input filtering |

The instruction model is **`gpt-6-luna`**, using the OpenAI Responses API with **`reasoning.effort = high`** and `store = false`. The server refuses a different `OPENAI_MODEL`. Model access was checked separately; [model-access.md](model-access.md) records that check and its scope.

Jev remains **`jev-1.13.0`** with **`typesafe-sdk==0.7.1`**. Existing authored questions are reused; the worker does not add another planner or a generated Jev question. Window and control rounds retain batched questions, groups of at most 10 candidates, recorded probabilities, API duration, input usage, and the configured **$0.042 per million input tokens** cost calculation. Output tokens have no Jev charge under this configured rate.

The native guest Driver is **`0.24.0`**; the adapter rejects a different reported version. Fleet dependencies are **`cua-sandbox==0.7.0`** and **`cua-fleet==0.1.17`**. Python supports `>=3.11,<3.14`; project preference is `3.11.15`, while the deployment container and guest installer use Python **3.12**. Runtime dependency resolution is committed in `uv.lock`. The application vendors noVNC **1.6.0**, including its licenses, under `vendor/novnc/`; this is separate from the VM's existing noVNC installation.

## Request and execution flow

1. Sign in and choose **Create loop**. The server stores the exact user prompt and starts a real LLM request. The response initially has `status: generating`.
2. The server saves the returned instruction string, response ID, usage, model, and generation timestamp. Only then does the loop become `ready`. Failed or interrupted generation produces an error; it does not substitute an instruction.
3. Choose **Run instruction**. The server requires a ready instruction, an online assigned worker, agent ownership, and no queued or running task for that worker. PostgreSQL also enforces one active run per worker.
4. The guest worker polls over HTTPS. The server dispatches the instruction read from PostgreSQL. The worker journals the run before acknowledging receipt and invoking the executor.
5. The executor validates the supported navigation, observes real Chrome windows and toolbar controls, and invokes the existing Jev rounds. Jev chooses among the permitted observed controls and selects click or typing. Only the address-field action is allowed to proceed.
6. Native Driver targets fresh snapshot identities. Before navigation, the worker reads back the typed URL and only then sends Enter. It observes fresh native state until the address and a loaded X document are present, or reports an error.
7. The worker persists progress and its result through the application API. A successful finish requires `verified_url`; a sent click, unverified input effect, or iteration limit cannot establish success.

The browser polls saved-loop detail and session state every two seconds while visible. Activity includes the user request, generation, database save, dispatch, acknowledgment, Jev selection, action dispatch, and observed result or error. Displayed event times are database timestamps in the viewer's local timezone. Worker event data also retains the worker timestamp; expand **Event data** for native evidence, selection rounds, and details.

## Access and desktop ownership

This is a **single-owner, single-workspace MVP**, not a multi-tenant account system. Anyone holding the workspace password can see its requests, runs, and assigned desktop. Keep that password private.

The deployment creates a random `APP_PASSWORD` in AWS Secrets Manager's `ctrlloop/mvp/app` secret. The private, ignored **`.local/mvp/access.md`** contains the site URL and workspace password for the operator. The ignored **`.local/mvp/deployment-secrets.json`** also contains deployment credentials; do not paste it into chat, logs, source, or an upload bundle. Non-secret deployment URL and resource identifiers are in `.local/mvp/aws.json`.

Login creates a hashed server-side session and a 12-hour `Secure`, `HttpOnly`, `SameSite=Strict` cookie. Mutating browser requests require the configured origin. Provider keys remain server-side. The guest worker receives its delivery token and Jev key; it does not receive the OpenAI or Fleet credentials.

The embedded viewer connects to `/api/desktop` on the application's own origin. The backend authenticates its cookie and origin, obtains the Fleet token server-side, and connects only to the configured namespace and sandbox. The browser does not need a Cua account or receive a broad provider bearer token.

The viewer starts in **View only**. **Take control** is available only when no run is queued or running. Human control belongs to the exact signed-in browser session; another signed-in session remains view-only. **Return to agent** releases ownership before a run can start. JavaScript uses `worker.can_control`, and the WebSocket gateway independently checks session ownership and active-run state before forwarding keyboard, pointer, or clipboard input. This restriction does not rely on the noVNC `viewOnly` flag alone.

Signing out releases that session's human ownership. Expired ownership is cleared when session state or ownership is next requested. Closing a browser tab does not automatically return ownership immediately; reopen the website with the same session and return control, or wait for session expiry. External Fleet viewers and separate controller tools are outside this application's ownership coordination; keep them from sending input during worker execution.

## Local development

From the repository root:

```powershell
uv sync --locked --no-dev
node --check web/app.js
```

Node is needed only for the optional JavaScript syntax check. There is no npm build step. Do not copy a Windows `.venv` into the Linux VM or container.

Provide a private, Git-ignored `.env.web` with these application settings. `uvicorn --env-file` loads it; `webapp.py` does not load `.env` implicitly.

| Setting | Value or source |
| --- | --- |
| `DATABASE_URL` | Dedicated PostgreSQL database and role; use TLS for remote database access |
| `APP_PASSWORD` | Private workspace password |
| `WORKER_TOKEN` | Dedicated token shared only with the assigned guest worker |
| `APP_ORIGIN` | Exact HTTPS browser origin, without a trailing slash |
| `OPENAI_API_KEY` | Authorized server-side inference key |
| `OPENAI_MODEL` | `gpt-6-luna` or omitted to use the pinned default |
| `CUA_CLIENT_ID`, `CUA_CLIENT_SECRET` | Authorized Fleet service credentials, kept server-side |
| `FLEET_NAMESPACE` | `ctrlloop-omarchy-20260927-1708` for the current desktop |
| `FLEET_SANDBOX`, `WORKER_ID` | `ctrlloop-omarchy-20260927-1708-e33048ae` for the current desktop |

Start the server behind a trusted local HTTPS reverse proxy whose origin matches `APP_ORIGIN`:

```powershell
uv run --locked uvicorn src.webapp:app --env-file .env.web --host 127.0.0.1 --port 8080 --no-access-log
```

Alternatively, supply a locally trusted certificate using uvicorn's `--ssl-keyfile` and `--ssl-certfile` options and set `APP_ORIGIN` to that HTTPS address. Keep keys under an ignored local directory. Secure cookies and same-origin enforcement remain enabled. A static HTTP server can preview the login assets but cannot provide an authenticated workspace.

Startup applies the idempotent schema in `src/schema.sql` and registers the configured worker. This requires an existing database owned by the application role. `/healthz` performs a database query. A local website alone does not make the cloud worker reachable: end-to-end delivery requires the guest's `CTRLLOOP_API_URL` to point to a reachable HTTPS application origin.

## AWS deployment

[`scripts/deploy_aws.py`](../scripts/deploy_aws.py) is an environment-specific deployment tool for the existing **`mangorts-dev`** account and **`us-west-1`** VPC. It checks the AWS account and contains explicit existing VPC, subnet, ALB, cluster, secret, and database identities. Review those constants before using it in another environment; this is not a generic infrastructure template.

It creates an isolated application service and database role while reusing the existing development ALB, ECS cluster, and Aurora PostgreSQL cluster. The app runs as one Fargate task with 0.25 vCPU and 1 GiB memory, without a public task IP. A new CloudFront distribution provides HTTPS, disables response caching, forwards session/WebSocket headers, and sends the origin header needed by the isolated ALB listener rule. No new domain is required.

Authenticate the permitted AWS SSO profile and create a separate deployment-tool environment. Runtime dependencies remain in the project lock; boto3 is used only by the deployment tool.

```powershell
aws sso login --profile mangorts-dev
uv venv --python 3.12 .local/aws-deploy-venv
uv pip install --python .local/aws-deploy-venv/Scripts/python.exe boto3==1.43.103
.local/aws-deploy-venv/Scripts/python.exe scripts/deploy_aws.py prepare --profile mangorts-dev --fleet-env C:/Users/Jonat/ctrlloop/.env.fleet
.local/aws-deploy-venv/Scripts/python.exe scripts/deploy_aws.py database --profile mangorts-dev
.local/aws-deploy-venv/Scripts/python.exe scripts/deploy_aws.py build --profile mangorts-dev --revision YOUR-UNIQUE-REVISION
.local/aws-deploy-venv/Scripts/python.exe scripts/deploy_aws.py status --profile mangorts-dev
```

On Linux, use `.local/aws-deploy-venv/bin/python` for that tool environment. `prepare` stores generated secrets separately from resource state. `database` launches a one-off initializer inside the VPC, using a temporary bootstrap role; `status` removes its extra secret-access policy once it observes a successful initializer exit. The web service uses only the dedicated database credentials.

`build` sends an explicit code/dependency/frontend allowlist to a private encrypted S3 source bucket and starts managed CodeBuild. It does not require local Docker Desktop. Source objects expire after seven days. Credentials, `.env*`, `.local`, `.git`, `.venv`, and reports are excluded. The Docker image installs native build headers needed by locked Linux dependencies, performs `uv sync --locked --no-dev --no-install-project`, and runs the server as a non-root user. ECR tags are immutable, so use a new revision for each build. Deployment resolves that revision to an image digest and pins the ECS task to it.

Wait until `status` reports database initialization success and a successful build before deploying:

```powershell
.local/aws-deploy-venv/Scripts/python.exe scripts/deploy_aws.py deploy --profile mangorts-dev
.local/aws-deploy-venv/Scripts/python.exe scripts/deploy_aws.py status --profile mangorts-dev
```

The deploy command starts an ECS rollout; its return is not proof of a healthy site. Check target health, running task count, CloudFront deployment state, HTTPS `/healthz`, and the login page. The deploy helper records its current image/revision and resource identifiers in `.local/mvp/aws.json`. Application logs are in CloudWatch `/ecs/ctrlloop-mvp` with 14-day retention. CodeBuild status includes its own log URL.

## Deploy the worker to the existing VM

[`scripts/deploy_worker.py`](../scripts/deploy_worker.py) deliberately checks the existing claim, namespace, and bound sandbox. It preserves the VM and its disk and does not provision, release, suspend, or recreate a desktop. It expects the local AWS state and deployment-secret files created above.

```powershell
uv run --locked python scripts/deploy_worker.py --fleet-env C:/Users/Jonat/ctrlloop/.env.fleet --jev-env C:/Users/Jonat/ctrlloop/.env --claim C:/Users/Jonat/ctrlloop/.local/omarchy-fleet/claim.json
uv run --locked python scripts/deploy_worker.py --fleet-env C:/Users/Jonat/ctrlloop/.env.fleet --jev-env C:/Users/Jonat/ctrlloop/.env --claim C:/Users/Jonat/ctrlloop/.local/omarchy-fleet/claim.json --status
```

The script creates a content-derived revision directory at `/home/omarchy/ctrlloop-dev/releases/<revision>/`, uploads only the source and locked dependency manifest, and installs a fresh Linux environment with `uv sync --locked --no-dev --python 3.12`. Credentials travel separately into `/home/omarchy/ctrlloop-dev/worker.env`, restricted to mode `0600`.

To prepare only code and dependencies, add **`--stage-only`** to the deployment invocation. This mode uses Fleet access to upload the allowlisted bundle but does not read or transfer the worker token/Jev key, modify the worker service, or start execution. Its asynchronous installation writes `stage.log` and `stage.status`; complete the normal deployment later to configure and start the worker.

The managed user service is **`ctrlloop-worker.service`**, running `python -m src.cloud_worker` independently of the laptop and browser connection. Its environment binds to the existing Omarchy graphical session: `/run/user/1000`, `wayland-1`, `:0`, and the user's D-Bus socket. Native commands use **`http://127.0.0.1:3000/mcp`** inside the VM. The installed local service currently needs no additional loopback token; the adapter can accept `CUA_LOCAL_MCP_TOKEN` if that deployment changes.

The worker records local state at `/home/omarchy/.local/state/ctrlloop-worker/`, detailed run reports at `/home/omarchy/ctrlloop-dev/reports/`, and installation output in the revision's `setup.log`/`setup.status`. Local deployment metadata is saved to `.local/mvp/worker.json`. The `--status` command reads the guest service status and recent journal output. Installation may still be running after the upload command returns; inspect its recorded status.

The VM's existing viewer repair must remain intact: websockify on service port 5900 forwards to WayVNC on loopback port 5902, with WayVNC using `--seat=Hyprland`. The website embeds its own client and proxies this existing stream; it does not replace the compositor or viewer bridge.

The same VM's effective expiry was extended to **September 30, 2026, 2:19:52 PM PDT / `2026-09-30T21:19:52Z`**. See the [lifetime readback and provider contract](vm-lifetime-20260927.md). Deployment and heartbeats do not extend that deadline. Expiry can delete the guest; its reports, Chrome state, and repair backup are not a durable cloud backup.

## Failure handling and current limits

- **Chrome must already be open** in the assigned graphical session. This executor selects an existing observed Chrome window; it does not launch Chrome, accept its first-run terms, dismiss arbitrary dialogs, or configure a browser profile.
- Only navigation to the HTTPS **X root** is supported. The instruction must actually describe that supported Chrome navigation. The executor rejects other destinations, paths, queries, fragments, ports, and unsupported actions. It does not post, purchase, sign in, or read an authenticated feed.
- A loaded X page and address must be observed through native Driver. The address field by itself is insufficient. Verification is bounded to 20 observations after committing the URL; failure to observe the page is an error, not completion.
- Jev sees compact native candidates and the saved instruction. The navigation scope retains Chrome toolbar candidates and excludes page/account/feed controls. Existing window/control selection and action questions remain the source of the decision.
- Native input is **not automatically replayed** after uncertain delivery or an exception. The guest journal can retry reporting a recorded result; it never repeats the GUI action. A restarted worker marks in-progress runs interrupted and releases stale server ownership after acquiring its guest process lock. Inspect the desktop before starting a new run.
- The guest service uses a per-user lock across revisions. The server accepts one queued/running task for its assigned worker. This is not a general distributed scheduler, multi-customer isolation system, or high-availability queue.
- Generation runs in the web process. A server restart marks incomplete generation as an error; create a new loop to retry. Keep the service at its configured single replica rather than scaling this in-process generation mechanism horizontally.
- The current viewer proxy supports the existing RFB 3.8 service configuration and rejects unknown protocol messages. Fit and fullscreen are display controls; guest-resolution changes are not requested.
- Full run reports and screenshots stay in the guest report directory. Run events/results in PostgreSQL provide the website's history. Export any needed guest artifacts before the VM expires. Existing Hermes compatibility remains a separate launch path.

## API surface

The website and worker share one application API and the existing PostgreSQL records; no second instruction model or delivery store is introduced.

| Method / path | Purpose |
| --- | --- |
| `GET /healthz` | Database-backed service health |
| `POST /api/login`, `POST /api/logout`, `GET /api/session` | Workspace session and assigned-worker state |
| `GET /api/loops`, `POST /api/loops` | List saved loops or begin real instruction generation |
| `GET /api/loops/{id}` | Instruction, generation events, runs, and run events |
| `POST /api/loops/{id}/runs` | Queue the persisted instruction |
| `POST /api/worker/mode` | Cookie-authenticated, origin-checked human/agent ownership transition |
| `WS /api/desktop` | Cookie-authenticated viewer and ownership-filtered input |
| `POST /api/worker/poll`, `/heartbeat`, `/recover` | Bearer-authenticated assigned worker delivery, liveness, and interrupted-run recovery |
| `POST /api/worker/runs/{id}/events`, `/finish` | Bearer-authenticated progress and observed outcome for the worker's active run |

## Acceptance evidence

Verified deployment checkpoint, **September 27, 2026**:

| Item | Recorded value |
| --- | --- |
| Live HTTPS site | [https://d1t0sm9jhdinfi.cloudfront.net](https://d1t0sm9jhdinfi.cloudfront.net) |
| AWS region / ECS service | `us-west-1` / `ctrlloop-mvp` |
| Application build revision | `20260927-mvp-002` |
| Deployed image digest | `sha256:e27702db2de53a1c9bbc541d619a7bb0341e986b21ea3df4ed8edae583fdc5e7` |
| ECS task definition | `ctrlloop-mvp:1` |
| CloudFront distribution | `EFZQEEWLIM0FZ` |
| PostgreSQL database | `ctrlloop_mvp`; bootstrap recorded ready |
| Application deployment requested | `2026-09-27T21:36:48.141226Z` / 2:36:48 PM PDT |
| Running guest worker revision | `3bc1f22bdb7c5d4c` |
| Guest worker active since | `2026-09-27T21:43:33Z` / 2:43:33 PM PDT |

The live `/healthz`, authentication, PostgreSQL connection, and authenticated read endpoints passed checks. Signing in through the deployed website displayed the real Chrome desktop inside its embedded viewer and **Worker · Online**. A separate read-only protocol check completed the RFB 3.8 handshake and read the **1920 × 1080** framebuffer through the application's gateway. This establishes the authenticated desktop viewing path; it does not establish that an instruction was executed.

The task's visual evidence is labeled **“Authenticated CTRL Loop workspace — live embedded Chrome desktop and online assigned worker.”** It shows the product interface and guest desktop, with no provider credentials or private access-file contents. No screenshot containing secrets is included in this document.

At this checkpoint, automatic approval review rejected the real sample **Create loop** action and also rejected re-review with the originating task's quoted user authorization, treating that transcript as untrusted. A direct approval question is pending in this task. The sample's actual generated instruction, persisted loop/run IDs, Jev decision, and verified navigation result are therefore **not yet recorded as completed**. Credential-transfer approval is resolved and is not the remaining blocker. The separate successful model-access check in [model-access.md](model-access.md) is not a substitute for this application run.

The remaining manual verification is to create the sample loop, wait for the real instruction to be saved, and run it while observing the stage events and desktop. Record loop/run IDs, the actual generated string, timestamps, Jev rounds/calls, and `observed_result` evidence. Verify persisted detail remains after reloading. Check takeover ownership between two signed-in sessions and disabled takeover during an active run. A successful run record must include the native snapshot's URL, loaded document title, snapshot ID, and observation time.

No repository test suite is added. Syntax/build checks, the live service/viewer checks above, and the authorized manual/integration demonstration are the validation path for this MVP. Append the final run evidence or unresolved blocker after observation.
