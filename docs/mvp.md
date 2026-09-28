# [CTRL] Loop MVP

The MVP has a typed-chat website, saved instructions and run history in PostgreSQL, a worker inside the existing Omarchy VM, and an embedded noVNC desktop viewer. The sample request is prefilled as **“I want to see the latest posts on X”**. OpenAI generates the instruction; the worker uses the existing Jev selection rounds and native Cua Driver to execute the supported navigation.

The live site is [CTRL Loop](https://d1t0sm9jhdinfi.cloudfront.net). The complete saved-instruction demonstration **succeeded**: real OpenAI generation, PostgreSQL persistence, delivery to the existing VM, Jev selection, native Chrome input, and observation of the loaded X root page. The authenticated embedded viewer and ownership controls were also verified. [Acceptance evidence](#acceptance-evidence) records the successful run and the earlier errors; authenticated feed contents were not evaluated.

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

The live run exposed a Linux Driver omission in named entry-field values. The [native entry-value patch and reproducible build guide](native-driver-entry-value.md) records the exact upstream 0.24.0 commit and existing-schema fix. Chrome's 16 affected child processes were preserved through the user systemd manager, and daemon activation preserved the browser, compositor, and viewer/MCP service PIDs. The revised patch built successfully at `2026-09-28T04:45:37.026213Z`, activated at `04:49:56Z`, and passed fresh native address readback. The resumed worker subsequently verified the loaded X page at `04:55:44.038Z` in the unchanged Chrome window.

## Request and execution flow

1. Sign in and choose **Create loop**. The server stores the exact user prompt and starts a real LLM request. The response initially has `status: generating`.
2. The server saves the returned instruction string, response ID, usage, model, and generation timestamp. Only then does the loop become `ready`. Failed or interrupted generation produces an error; it does not substitute an instruction.
3. Choose **Run instruction**. The server requires a ready instruction, an online assigned worker, agent ownership, and no queued or running task for that worker. PostgreSQL also enforces one active run per worker.
4. The guest worker polls over HTTPS. The server dispatches the instruction read from PostgreSQL. The worker journals the run before acknowledging receipt and invoking the executor.
5. The executor validates the supported navigation, observes real Chrome windows and toolbar controls, and invokes the existing Jev rounds. Jev chooses among the permitted observed controls and selects click or typing. Only the address-field action is allowed to proceed.
6. Native Driver targets fresh snapshot identities. After the selected address-field click or typing action, the worker reads back the exact saved destination and only then sends Enter. It observes fresh native state until the address and a loaded X document are present, or reports an error.
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

Deploy while instruction generation and desktop runs are idle. The configured ECS rolling deployment uses `minimumHealthyPercent=100` and `maximumPercent=200`, so the old and new app processes can overlap temporarily. New-process startup marks unfinished generation as interrupted; this mechanism does not support an uninterrupted rollout during active generation.

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
- Only navigation to the HTTPS **X root** is supported. The instruction must describe that Chrome navigation and contain exactly one valid destination. The executor rejects other destinations, paths, queries, fragments, and ports; it executes no additional requested action. It does not post, purchase, sign in, or read an authenticated feed. The current candidate filter expects the English Chrome accessibility labels.
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
| Guest worker revision at this deployment checkpoint | `3bc1f22bdb7c5d4c` |
| That guest revision active since | `2026-09-27T21:43:33Z` / 2:43:33 PM PDT |

The live `/healthz`, authentication, PostgreSQL connection, and authenticated read endpoints passed checks. Signing in through the deployed website displayed the real Chrome desktop inside its embedded viewer and **Worker · Online**. A separate read-only protocol check completed the RFB 3.8 handshake and read the **1920 × 1080** framebuffer through the application's gateway. This establishes the authenticated desktop viewing path; it does not establish that an instruction was executed.

The task's visual evidence is labeled **“Authenticated CTRL Loop workspace — live embedded Chrome desktop and online assigned worker.”** It shows the product interface and guest desktop, with no provider credentials or private access-file contents. No screenshot containing secrets is included in this document.

The user subsequently approved the complete live demonstration; generation/execution authorization and credential-transfer approval are resolved. The application created PostgreSQL loop **`1681e48a-e35c-4c0e-8779-457621abe348`** from the exact sample request. Its real saved instruction is:

> Open https://x.com/ in Google Chrome to view the latest posts on X.

The request was saved at **September 27, 2026, 8:36:43.020276 PM PDT** (`2026-09-28T03:36:43.020276Z`); the generated instruction was committed at **8:36:46.468914 PM PDT** (`03:36:46.468914Z`). The actual `gpt-6-luna` / high-reasoning API call took **3,429 ms**, used **125 input tokens** and **67 output tokens**, including **44 reasoning tokens**, and returned response ID **`resp_0aba74e14f04cae4016ab9e0cbd67487d0a2557ed39715abe1`**. The loop record, generation event, and worker acknowledgment retain the same instruction string. This is separate from the earlier minimal model-access check.

**Run `fd224a26-652c-4896-bb4b-64115b7f38e7` succeeded** on worker revision **`7443318e84165bc5`**, deployed at `2026-09-28T04:54:50Z`. On September 27 PDT, it was queued at **21:55:35.049555**, started at **21:55:35.949624**, and persisted as succeeded at **21:55:44.750116**. The worker's operational duration was **8,486.804 ms**, excluding delivery queue time and subsequent report generation.

The successful run used **1 iteration**, **0 Jev window-selection rounds** (one eligible Chrome window), and **1 control-selection round**. Its **3 candidates in 1 group** were Reload, Home, and Address and search bar; they reduced to **1 winner**. The sole **`jev-1.13.0`** API call took **150.798 ms** and used **3,809 input tokens**, **686 output tokens**, and **$0.000159978** input cost. The full control round took **153.297 ms**. Ten existing questions were batched in that call: one candidate choice, three action choices, and six hypothetical settling choices. Jev chose the address field with probability **0.98** (Home 0.02, Reload 0.00), then **click** with probability **0.84** (typing 0.16). Its selected settling label was `>500ms`, configured as 500 ms, with probability **0.43**.

The worker read back the already-entered destination from a fresh native snapshot after that selected click and sent Enter. At **`2026-09-28T04:55:44.038Z`**, native `cua_driver.get_window_state` snapshot **`s0000000d`** observed URL **`https://x.com`**, window title **“X. It’s what’s happening / X - Google Chrome”**, and document title **“X. It’s what’s happening / X”**. Chrome PID **22550** and window **94562712109632** remained unchanged. This observed URL and document—not the returned click effect—established `status: verified` and the persisted successful run. Its scope is opening the X root page; no authenticated feed was read.

Selected measured spans from that successful run are below. The Jev API duration is inside the control round; these spans are not an additive breakdown of the full run.

| Operation | Duration |
| --- | ---: |
| Native window discovery | 3.716 ms |
| Native foreground activation | 4.452 ms |
| Native control acquisition | 411.355 ms |
| Jev control-selection round 1 | 153.297 ms |
| Native selected-address click | 857.815 ms |
| Navigation commit (Enter) | 470.326 ms |
| Native loaded-page verification | 4,207.790 ms |
| Full worker execution | **8,486.804 ms** |

Three earlier attempts remain visible as errors:

| Run / guest revision | Recorded interval, PDT on September 27 | Worker execution duration | Result |
| --- | --- | --- | --- |
| `f2babdad-3ee0-482e-826b-ea9e2e3f9c16` / `3bc1f22bdb7c5d4c` | Queued 20:37:49.583684; started 20:37:51.610615; finished 20:37:52.853564 | **956.682 ms**; 0 Jev API calls | `invalid_driver_response`: native focus response was not accepted as a structured object; stopped before Jev control selection. |
| `c6de6614-be74-480f-b48b-ee9dd88b194c` / `d9efe4f7aa7d984c` | Queued 20:43:26.970683; started 20:43:28.566943; finished 20:43:33.708271 | **4,943.188 ms**; 1 Jev API call | `address_readback_failed`: URL typing was dispatched, but the exact address could not be read back; Enter was not sent and navigation success was not claimed. |
| `b50776a2-a041-4a0a-bbcc-01b9f6460a27` / `d9efe4f7aa7d984c` | Queued 21:50:50.940015; started 21:50:52.136155; finished 21:51:07.570919 | **14,946.652 ms**; 4 Jev API calls | `iteration_limit`: with native address readback fixed, Jev clicked the address field four times; this worker revision committed only after typing, so the already-entered URL remained uncommitted. |

Worker durations above are the reports' operational durations, excluding delivery queue time and subsequent report generation. The second run's real **`jev-1.13.0`** call took **160.703 ms**, with **3,761 input tokens**, **687 output tokens**, and **$0.000157962** input cost at $0.042 per million input tokens. It batched 10 questions in one API call: one candidate-selection question, three action questions, and six hypothetical settling questions.

That run had one observed Chrome window, so window selection needed **0 Jev rounds**. Control selection used **1 round**, **3 candidates in 1 group**, and reduced them to **1 winner** in **163.503 ms**. The candidates were Reload, Home, and Address and search bar. Jev selected the address field (`control_18`) with candidate probability **1.00**, then selected `type_text` with probability **0.74** versus **0.26** for click. The selected hypothetical settling label was `>500ms` (configured as 500 ms, probability 0.25); execution stopped before a settling wait. This proves a real instruction-related Jev choice and native typing attempt, **not a verified loaded X page**.

The third run had **0 window-selection rounds** and **4 control-selection rounds**, one per iteration; each round narrowed **3 candidates in 1 group to 1 winner**. Its four Jev calls took **122.243, 97.541, 68.628, and 88.901 ms**, using **3,809, 4,133, 4,133, and 4,133 input tokens** respectively. Their input costs were **$0.000159978, $0.000173586, $0.000173586, and $0.000173586**: **16,208 input tokens** and **$0.000680736** total. Each selected an address-field click. The corrected worker commits after that selected click only when fresh native readback exactly matches the saved destination; the successful fourth run exercised this path without changing a Jev question.

The live UI takeover check passed: **Take control** changed the session to human ownership, **Run again** became disabled, and **Return to agent** restored agent ownership. At approximately **9:11 PM PDT on September 27** (`2026-09-28T04:11Z`), a manual page reload and saved-loop selection showed the same exact generated instruction and both then-existing persisted error runs; the viewer reconnected as **Live** and the worker remained **Online**. During the third run, the live UI showed **Running**, disabled **Run in progress** and **Take control**, and displayed **“The agent is running. Takeover is locked.”** while the viewer remained **Live** and worker **Online**. This verifies the visible active-run lock; the optional negative ownership check from a second signed-in session was not run because automatic approval review rejected that check and is not counted as passed.

At **9:56:57 PM PDT on September 27** (`2026-09-28T04:56:57Z`), a further manual reload retained the exact instruction and **all four runs**, including the successful run displayed as **Succeeded · 8.8 s** with its observed-result events. The viewer reconnected **Live**, the worker was **Online**, and the embedded desktop showed the loaded X landing page in view-only mode without signing into an account. The UI duration uses the persisted start/finish interval; the report's 8,486.804 ms measures worker execution before report generation.

The private local evidence files are `.local/mvp/demo-1681e48a-e35c-4c0e-8779-457621abe348.json`, `successful-run-worker-report.json`, the three earlier `first-run-worker-report.json`, `second-run-worker-report.json`, and `third-run-worker-report.json` files, plus `demo-running-ax.txt`, `demo-success-ax.txt`, and `demo-success.png` in the same directory. They contain actual persisted records, detailed timing/selection traces, and the running website's accessibility state. The final screenshot is labeled **“Successful saved instruction — loaded X page in the live embedded desktop.”** It contains no credentials or private access-file contents.

No repository test suite is added. Syntax/build checks, the live service/viewer/ownership checks, the reproducible native build and readback, and the authorized successful integration run are the validation path for this MVP.
