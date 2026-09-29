# [CTRL] Loop MVP

The application has a shared development website and PostgreSQL database, personal sign-ins, companies, and a separate assigned Omarchy desktop for each member. Workflows and history are shared within a company; each run targets its initiator's own worker. OpenAI generates the saved instruction; the worker uses the existing Jev selection rounds and native Cua Driver to select observed windows and controls. Runs have a full-page report and an authenticated ZIP download of their available evidence.

Use [developer onboarding](developer-onboarding.md) for personal access, worker updates and shared-site releases, and [environment configuration](environments.md) for development resource bindings and the disabled production promotion path. Those guides describe the current implementation and outstanding access prerequisites; deployment checkpoints below record what was actually verified.

The configured development site is [CTRL Loop](https://dev.ctrlloop.io). The historical X-navigation demonstration **succeeded**: real OpenAI generation, PostgreSQL persistence, delivery to the existing VM, Jev selection, native Chrome input, and observation of the loaded X root page. The authenticated embedded viewer and ownership controls were also verified. [Historical acceptance evidence](#historical-acceptance-evidence) records that successful run and earlier errors at the previous origin; it does not establish that company accounts are deployed.

## Components and pins

| Component | Implementation |
| --- | --- |
| Website | [`web/`](../web/): static HTML, CSS, and JavaScript; no frontend build or external asset CDN |
| Application API | [`src/webapp.py`](../src/webapp.py): FastAPI, authentication, instruction generation, delivery, events, and worker ownership |
| Database | [`src/schema.sql`](../src/schema.sql): `companies`, `users`, `sessions`, `workers`, `loops`, `runs`, `events`, `auth_tokens`, and `company_migration_audit` |
| Worker service | [`src/cloud_worker.py`](../src/cloud_worker.py): outbound HTTPS polling, guest process lock, crash journal, heartbeats, and result delivery |
| Bounded executor | [`src/worker.py`](../src/worker.py): validates the saved instruction and invokes existing window/control rounds with explicit capability limits |
| Shared selection pipeline | Existing [`src/pipeline.py`](../src/pipeline.py), [`src/selection.py`](../src/selection.py), [`src/jev_questions.py`](../src/jev_questions.py), and [`src/reporting.py`](../src/reporting.py) |
| Native transport | Existing [`src/driver.py`](../src/driver.py), now supporting either authenticated Fleet transport or VM loopback using the same native schemas |
| Desktop gateway | [`src/desktop_gateway.py`](../src/desktop_gateway.py): authenticated same-origin WebSocket proxy for the exact configured sandbox, with server-side RFB input filtering |

The instruction model is **`gpt-6-luna`**, using the OpenAI Responses API with **`reasoning.effort = high`** and `store = false`. The server refuses a different `OPENAI_MODEL`. Model access was checked separately; [model-access.md](model-access.md) records that check and its scope.

Jev remains **`jev-1.13.0`** with **`typesafe-sdk==0.7.1`**. Existing authored questions are reused; the worker does not add another planner or a generated Jev question. Window and control rounds retain batched questions, groups of at most 10 candidates, recorded probabilities, API duration, input usage, and the configured **$0.042 per million input tokens** cost calculation. Output tokens have no Jev charge under this configured rate.

The native guest Driver is **`0.24.0`**; the adapter rejects a different reported version. Fleet dependencies are **`cua-sandbox==0.7.0`** and **`cua-fleet==0.1.17`**. Python supports `>=3.11,<3.14`; project preference is `3.11.15`, while the deployment container and guest installer use Python **3.12**. Runtime dependency resolution is committed in `uv.lock`. The application vendors noVNC **1.6.0**, including its licenses, under `vendor/novnc/`; this is separate from the VM's existing noVNC installation.

The live run exposed a Linux Driver omission in named entry-field values. The [native entry-value patch and reproducible build guide](native-driver-entry-value.md) records the exact upstream 0.24.0 commit and existing-schema fix. Chrome's 16 affected child processes were preserved through the user systemd manager, and daemon activation preserved the browser, compositor, and viewer/MCP service PIDs. The revised patch built successfully at `2026-09-28T04:45:37.026213Z`, activated at `04:49:56Z`, and passed fresh native address readback. The resumed worker subsequently verified the loaded X page at `04:55:44.038Z` in the unchanged Chrome window.

## Request and execution flow

1. Sign in with a personal email/password, open your company, and choose **Create loop**. The server stores the exact user prompt and creator and starts a real LLM request. The response initially has `status: generating`.
2. The server saves the returned instruction string, response ID, usage, model, and generation timestamp. Only then does the loop become `ready`. Failed or interrupted generation produces an error; it does not substitute an instruction.
3. Choose **Run instruction**. The server records the initiator and requires a ready instruction, that member's online assigned worker, agent ownership, and no queued or running task for that worker. PostgreSQL also enforces one active run per worker. Missing assignments and deployment maintenance prevent execution while shared workflows remain available.
4. The guest worker polls over HTTPS. The server dispatches the instruction read from PostgreSQL. The worker journals the run before acknowledging receipt and invoking the executor.
5. The executor observes all visible native windows and their controls, and invokes the existing Jev rounds. The X-only, Chrome-only, English-toolbar, and address-field filters have been removed. Jev still chooses only the existing click or typing action; no planner, action scheme, or new Jev question has been introduced.
6. Native Driver targets fresh snapshot identities. The web executor permits at most four iterations. It does not invent typing content or generate special navigation key sequences. If Jev chooses typing without prepared text, the run stops with `typing_content_unavailable`. The existing CLI can still receive explicit prepared text.
7. The worker persists progress and its result through the application API. There is currently no general task-completion verifier, so dispatching an action cannot produce success. Unsupported actions stop as `blocked`; reaching the bounded iteration limit is `unverified`; execution failures remain `error`. A future caller must supply explicit verification before `succeeded` can be accepted.
8. The worker closes its durable report, uploads the available evidence ZIP to authenticated private storage, and reports the terminal result. The full-page report combines that evidence with the server's saved user request, exact OpenAI request/response, delivery events, and result. If upload fails, the terminal result is still reported. Transient failures retry evidence delivery without replaying desktop actions; permanent archive rejection retains the evidence for repair and releases the worker for later runs.

The browser polls saved-loop detail and session state every two seconds while visible. Activity includes the user request, generation, database save, dispatch, acknowledgment, Jev selection, action dispatch, and observed result or error. Displayed event times are database timestamps in the viewer's local timezone. Worker event data also retains the worker timestamp; expand **Event data** for native evidence, selection rounds, and details.

## Run evidence and timing

**Full report** opens `/reports/runs/{run_id}` (or `/reports/loops/{loop_id}` for generation failures). It includes expandable raw JSON, native observations and command arguments/results, Jev questions/state/probabilities and candidate reductions, screenshots, API-call usage/cost, shared-scale loop charts, a chronological timeline, and downloads. The download joins the existing PostgreSQL loop/run/event records with the worker ZIP; it remains available when execution fails or stops before the pipeline.

The existing worker event array remains `total_run.json`, accompanied by readable per-call Jev input/output files and `screenshots/`. New `events.jsonl` records are flushed and synced as observations occur, rather than waiting for run completion. `delivery.jsonl` records the guest's event outbox attempts. `manifest.json` correlates the server run ID, lists file sizes and hashes, and explicitly identifies missing screenshots, model responses, native dispatch markers, or command responses. HTTP authentication headers and environment credentials are excluded. Observed application content is intentionally part of this private report.

A loop begins immediately before acquiring current state and ends at the last native input command's **transport-call start**. That timestamp is captured after durable intent journaling and is not proof that the remote application received or applied input. `loop_started` and `loop_finished` are authoritative loop boundaries; `loop.cycle` also contains return and reporting work and is not the user's loop duration. Command returns, screenshots, settling waits, report writing, and delivery overhead remain inspectable outside that boundary. Worker durations use one monotonic clock; server timestamps are shown separately and are never subtracted from worker timestamps to imply precise network latency.

A guest restart recovers the available JSONL records and creates a partial ZIP, then retries only event/result/artifact delivery. An intent without its later transport marker or response is labeled uncertain. The durable journal cannot recover bytes that were never captured, and complete loss of the guest before upload can still leave only server evidence. Transient storage failures remain visible as pending upload with the local evidence retained. Invalid or oversized archives remain local with a receipt in the worker state's `failed-evidence/` directory and an explicit repair-required result; they do not block future polling. Recorded screenshots are checked against actual files, so an unexpectedly missing image remains an explicit capture gap. The private report bucket is separate from the seven-day build-source staging bucket; website authentication gates report and file downloads.

## Access and desktop ownership

Each account belongs to exactly one company and signs in with its own verified email and personal password. Signup as **Company admin** creates a new company and a separate company join password; signup as **User** selects an existing company and verifies that join password. Company admins manage their users and future enrollment password. The site provides email verification, login/logout and forgotten-password reset. See [company account flows, security boundaries and email configuration](company-accounts.md).

Platform devadmin is a separate verified-account operator grant and is never a signup role. It permits explicit company/user target context across the product. Ordinary users cannot choose another company or another person's VM. Every company-owned table has its own non-null indexed `company_id`, including sessions, users, workers, loops, runs, events and tokens; composite foreign keys enforce same-company relationships.

Legacy account data needs the [explicit preflight and migration](company-accounts.md#migration-preflight). This source change does not deploy the new flow or migrate the live database. Existing personal credentials and historical evidence are preserved where ownership mappings are resolved; old sessions expire and migrated accounts verify their real email. There is no runtime shared password or default-worker fallback.

The embedded viewer connects to `/api/desktop` on the application's own origin. The backend authenticates its cookie, origin and verified company access, obtains the Fleet token server-side, and connects only to the signed-in member's assigned namespace and sandbox. The browser does not need a Cua account or receive a broad provider bearer token.

The live desktop opens automatically beside the request and instruction, with execution activity below the instruction. The viewer stays connected while activity refreshes and while switching between saved requests. On narrow screens it appears above the request. The desktop scales to fit, and the top-right button opens fullscreen (or fills the app viewport when native fullscreen is unavailable). Older `/desktop` request links still open the combined view.

The viewer starts in **View only**. Click the displayed desktop (or focus it and press Enter/Space) to acquire control when no run is queued or running and the worker is not in maintenance. That first click only acquires ownership and focuses the viewer; it is not forwarded to the guest. Input stays blocked until the server confirms ownership, including after a failed ownership refresh. Human control belongs to the exact signed-in browser session; another session of that account remains view-only. **Return to agent** releases ownership before a run can start. JavaScript uses `worker.can_control`, and the WebSocket gateway independently checks company access, assignment, session ownership and active-run state before forwarding keyboard, pointer, or clipboard input. This restriction does not rely on the noVNC `viewOnly` flag alone.

Signing out releases that session's human ownership. Expired ownership is cleared when session state or ownership is next requested. Closing a browser tab does not automatically return ownership immediately; reopen the website with the same session and return control, or wait for session expiry. External Fleet viewers and separate controller tools are outside this application's ownership coordination; keep them from sending input during worker execution.

## Viewer recovery and performance diagnosis

At the earlier viewer checkpoint, the changes were deployed as **`20260928-viewer-001`**, ECS task definition **`ctrlloop-mvp:2`**, image digest **`sha256:3562084c0092dc93581a708ae26a308c062ce254e3b6d34ef882c7478a05872c`**. That deployment was healthy by **`2026-09-28T17:17Z`**; subsequent readback confirmed one running task, none pending, and HTTP health 200. It reused the same VM, worker revision `7443318e84165bc5`, native Driver, and Jev questions. The later general-executor deployment is recorded under acceptance evidence below. No tests were added.

The application now reuses a PostgreSQL pool of **2–8 connections**. The gateway forwards RFB negotiation and framebuffer requests without an ownership query; only keyboard, pointer, and clipboard input needs the database ownership check. Adjacent input packets in one browser message share that check. The shared worker-row lock remains held until authorized input is forwarded, so ownership changes and run starts cannot race past it. Signed-in session validity is also checked independently every 15 seconds. The parser now consumes noVNC's **seven-byte extended PointerEvent** when its extension bit is present; treating it as six bytes left an extra byte that could corrupt the following message.

After an unexpected disconnect, the viewer checks `/api/session` before each new connection and makes at most **five automatic retries**, with delays of **1, 2, 4, 8, and 15 seconds**. Each attempt has a **20-second deadline**. The retry budget resets after **30 seconds continuously connected**, so short repeated connections cannot cause an unlimited retry loop. Recovery pauses while the tab is hidden; hiding the tab during setup cancels that attempt without consuming its retry. An established video connection remains open across tab visibility, run status, and human/agent control changes. Logout or an intentional disconnect cancels pending recovery. Expired sessions return to sign-in; explicit RFB security failures or credential requests stop automatic retries. **Reconnect** permits a fresh manual attempt.

Starting sign-in invalidates outstanding session reads from before that sign-in. A delayed unauthenticated response can no longer tear down the viewer opened by a later successful login.

Gateway logs now emit payload-free **`desktop_closed`** records with a connection ID, terminating task, classified cause, close codes, connection duration, input/protocol counts, byte counts, and ownership-check total/maximum duration. Use these records in CloudWatch `/ecs/ctrlloop-mvp` to distinguish protocol rejection, upstream closure, expired sessions, and operation timeouts. They omit packet contents, keyboard/clipboard text, tokens, and raw exception messages. Compare permission timing and close causes during a live reproduction before attributing the remaining delay to the browser, database, or upstream stream.

Comparable read-only probes before deployment (`2026-09-28T07:14:05Z`) and after deployment (`17:18:12Z`) recorded:

| Measurement | Before | Deployed revision |
| --- | ---: | ---: |
| Median framebuffer response; eight nonincremental 32 × 32 reads | 81.145 ms | 53.495 ms |
| Median `/api/session` response; three requests | 106.01 ms | 32.82 ms |
| WebSocket ping, approximately | 24 ms | 28 ms |

The new task's read-only probe connection **`9a545cc03b09`** recorded **12 protocol packets, 0 input packets, and 0 ownership checks**, confirming the display protocol avoids those queries. Through the actual embedded viewer, three dispatched characters were visible in the immediately following screenshots after **434, 416, and 463 ms**. These are upper bounds including automation and screenshot overhead, not precise input-to-frame latency measurements. The small framebuffer probes likewise do not measure interactive rendering of the full desktop.

Embedded viewer connection **`8a9c883fd053`** stayed open from **`17:18:14.583Z` until an intentional page reload at `17:30:02.235Z`**, with a logged lifetime of **11m 47.8s**, across five run attempts and human/agent ownership changes. Runs **`781f5316`**, **`ee2021cf`**, and **`068eab7d`** succeeded in approximately **6.60, 5.42, and 5.27 seconds** respectively (persisted run durations). Runs **`6c3dd98b`** and **`856aefde`** stopped before an action because Jev selected outside the bounded navigation scope while Chrome was already on X; this existing executor guard is separate from the viewer changes. No viewer disconnect was observed during these attempts. The connection forwarded **982 protocol packets** and checked **94 input packets**, forwarding 93 and dropping one. Ownership checks averaged **1.90 ms**, with a **59.24 ms** maximum; sampled ECS CPU during the runs stayed below **18%**.

**Controlled automatic recovery passed.** The operator disconnected only the current WayVNC client, **26**, at **`17:31:58.993Z`**; the WayVNC daemon remained active with PID **1876**. The gateway recorded upstream closure at **`17:31:59.015Z`** for connection `b597f8e157ee` (upstream code 1000, browser code 1011), then accepted new connection **`e08c507643d3` at `17:32:00.406Z`**: **1.391 seconds** from logged closure to transport recovery. New WayVNC client **27** was present by `17:32:01.993Z`. Fresh UI observation at **`17:32:43.835Z`** showed **Live** with the placeholder hidden, without a manual reconnect or reload after the interruption. The earlier UI wait expired before the interruption, so the transient Reconnecting state was not captured.

The exact cause of the historically reported loop-triggered disconnect remains **unconfirmed**. A pre-deployment reproduction also received 34 framebuffer replies through a bounded-error run without an unexpected close. The deployed checks establish improved measured response times, stable viewing across runs, and automatic recovery from the controlled interruption.

The ignored evidence is under `.local/mvp/`: `latency-live-baseline.json`, `latency-live-after.json`, `latency-rollout-readback.json`, `latency-desktop-metrics-after.json`, `viewer-input-checks.json`, `viewer-ui-runs-readback.json`, `viewer-controlled-recovery.json`, and `viewer-recovery-ui.json`. `old-app-viewer-during-run.json` records the pre-deployment reproduction. These artifacts contain the observed timings and sanitized diagnostics used here.

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
| `APP_ENV` | Explicit `dev` or `prod`; local development uses `dev` |
| `APP_ORIGIN` | Exact HTTPS browser origin, without a trailing slash |
| `OPENAI_API_KEY` | Authorized server-side inference key |
| `OPENAI_MODEL` | `gpt-6-luna` or omitted to use the pinned default |
| `CUA_CLIENT_ID`, `CUA_CLIENT_SECRET` | Authorized Fleet service credentials, kept server-side |
| `REPORT_BUCKET` | Selected environment's private evidence bucket, with authorized server-side AWS access |

Personal/company credentials and assigned worker routing come from PostgreSQL. Configure real SES/SMTP delivery, or explicitly use `EMAIL_PROVIDER=local` with `APP_ENV=dev` for local captured messages. The UI distinguishes capture from sent mail. See [email settings](company-accounts.md#email-delivery); never copy production state into development.

Start the server behind a trusted local HTTPS reverse proxy whose origin matches `APP_ORIGIN`:

```powershell
uv run --locked uvicorn src.webapp:app --env-file .env.web --host 127.0.0.1 --port 8080 --no-access-log
```

Alternatively, supply a locally trusted certificate using uvicorn's `--ssl-keyfile` and `--ssl-certfile` options and set `APP_ORIGIN` to that HTTPS address. Keep keys under an ignored local directory. Secure cookies and same-origin enforcement remain enabled. A static HTTP server can preview the login assets but cannot provide an authenticated company page.

Startup creates `src/schema.sql` for an empty database or validates the current company schema; legacy data requires the explicit administrator migration and mapped real email addresses. Signup creates or joins a company through the website; VM assignment uses the [administrator setup](company-accounts.md#per-user-vm-assignment). `/healthz` performs a database query. A local website alone does not make the cloud worker reachable: end-to-end delivery requires the guest's `CTRLLOOP_API_URL` to point to a reachable HTTPS application origin.

## AWS deployment

Use the [shared website release procedure](developer-onboarding.md#deploy-the-shared-website) and [environment configuration](environments.md). Every deployment explicitly selects `--environment dev` or `--environment prod`; contributor releases use `--release-only` against existing approved resources. Production remains disabled pending its separate configuration and review. The original single-owner commands without an environment selector are historical and have been superseded by those guides.

Development retains the existing `ctrlloop-mvp` service and database identities in `us-west-1`. The application uses one Fargate task, the existing development ALB/ECS/Aurora infrastructure, and a CloudFront HTTPS origin with caching disabled. Current resource state and private credentials live separately under `.local/environments/dev/`; historical checkpoints and evidence remain below and in `.local/mvp/`.

The build uploads an explicit code/dependency/frontend allowlist to private S3 and runs managed CodeBuild, without local Docker. Credentials, `.env*`, `.local`, `.git`, `.venv`, and reports are excluded. Build source expires after seven days; private report storage is separate. Locked dependencies run as a non-root container user, ECR tags are immutable, and deployment pins the selected image digest. A successful build or a returned deploy command is not proof of a healthy rollout; the linked procedure includes the required readback.

Coordinate shared-site releases while instruction generation and desktop runs are idle. Old and new application processes can overlap during an ECS rollout, and startup marks unfinished generation as interrupted. CloudWatch application logs remain under `/ecs/ctrlloop-mvp` with 14-day retention. Infrastructure/bootstrap changes stay under administrator control; `status` is read-only and `database-ready` explicitly completes database initialization.

## Deploy a user's worker

Use [Deploy only your worker](developer-onboarding.md#deploy-only-your-worker). Commands require an explicit environment and `--email`; no default worker is selected. Each user's target, claim, credential and deployment state live under `.local/environments/{environment}/workers/{sha256-of-normalized-email}/`. The assignment command prints the directory. Follow [explicit VM assignment](company-accounts.md#per-user-vm-assignment) to bind the company/user and preserve existing worker credentials.

[`scripts/deploy_worker.py`](../scripts/deploy_worker.py) verifies the saved assignment against the exact bound sandbox and server worker identity. It preserves that VM and disk. Normal updates reuse the VM's dedicated credentials; administrator-only initial configuration transfers the worker/Jev credentials separately into a mode-0600 `worker.env`. Deployment takes the guest lock, enters maintenance for that worker, refuses active/unsettled runs or human control, and resumes only after successful startup. A failed deployment remains in maintenance for inspection. `--stage-only` prepares code and dependencies without transferring credentials or starting a worker.

The content-derived release lives at `/home/omarchy/ctrlloop-dev/releases/<revision>/` with a fresh locked Python 3.12 environment. The user service **`ctrlloop-worker.service`** runs `python -m src.cloud_worker` independently of the laptop/browser. Its native endpoint is **`http://127.0.0.1:3000/mcp`** in the Omarchy graphical session. Worker state lives at `/home/omarchy/.local/state/ctrlloop-worker/`, and detailed reports at `/home/omarchy/ctrlloop-dev/reports/`; the per-member deployment record identifies its installation logs.

The existing viewer repair remains part of each prepared desktop: websockify port 5900 forwards to WayVNC loopback port 5902 with `--seat=Hyprland`. The website proxies that stream; worker updates do not replace the compositor or viewer bridge. The original owner's guest expiry checkpoint is recorded in the [lifetime readback](vm-lifetime-20260927.md); Kevin's separate pool deadline is in [onboarding](developer-onboarding.md#administrator-migration-and-assignment). Deployment and heartbeats do not extend either deadline. Guest-only reports, browser state and repair backups are not durable cloud backups.

## Failure handling and current limits

- The executor selects existing observed application windows; it does not include a general launch/planning system, generated typing content, or general completion verification. Removing demo filters does not establish support for every application or request.
- Jev receives compact native candidates and the saved instruction. Existing authored window/control/action/settling questions, batching, model pins, and request budgets remain unchanged. Native raw observations are retained locally in the report; they are not all sent to Jev.
- Fresh snapshot targeting, worker authentication, assigned-desktop authorization, and single-controller ownership remain enforced. Native input is **not automatically replayed** after uncertain delivery or exceptions. Recovery retries only evidence and recorded result delivery. Inspect the desktop before starting a new run after interruption.
- The worker remains bounded to four iterations with existing transport/request limits. Archives are limited to 100 MiB for upload. Evidence that cannot upload remains local and the result explicitly reports the upload problem; local storage and guest lifetime remain finite.
- The guest service uses a per-user lock across revisions. The server accepts one queued/running task per assigned worker; different members can run on their separate desktops concurrently. The application still uses one replica.
- Generation runs in the web process. A server restart marks incomplete generation as an error; create a new loop to retry. Failure reports preserve the available request, response, and error events and identify missing capture.
- The viewer proxy and its recovery/ownership behavior remain independent of execution. Automatic fit and fullscreen only change the display; guest-resolution changes are not requested.
- New run archives are uploaded to private durable storage. Earlier runs that lack worker uploads expose the available server record and identify their missing native/Jev/screenshot evidence. Existing Hermes compatibility remains a separate launch path.

## API surface

The website and worker share one application API and the existing PostgreSQL records; no second instruction model or delivery store is introduced.

| Method / path | Purpose |
| --- | --- |
| `GET /healthz` | Database-backed service health |
| `GET /api/companies`, `POST /api/signup` | Minimal company enrollment selector; atomic new-company or company-password signup |
| `POST /api/login`, `POST /api/logout`, `GET /api/session` | Verified email/password session, company and assigned-worker state |
| `POST /api/verify-email`, `POST /api/resend-verification` | Single-use email verification and resend |
| `POST /api/forgot-password`, `POST /api/reset-password` | Personal password reset, token consumption and session revocation |
| `PATCH /api/company` | Company-admin company name and enrollment-password management |
| `GET /api/company/users`, `PATCH /api/company/users/{user_id}` | Company-admin user listing and access/role changes |
| `GET /api/admin/companies` | Trusted devadmin cross-company context selection |
| `GET /api/loops`, `POST /api/loops` | List saved loops or begin real instruction generation |
| `GET /api/loops/{id}` | Instruction, generation events, runs, and run events |
| `POST /api/loops/{id}/runs` | Queue the persisted instruction |
| `POST /api/worker/mode` | Cookie-authenticated, origin-checked human/agent ownership transition |
| `WS /api/desktop` | Cookie-authenticated viewer and ownership-filtered input |
| `POST /api/worker/poll`, `/heartbeat`, `/recover` | Bearer-authenticated assigned worker delivery, liveness, and interrupted-run recovery |
| `POST /api/worker/deployment` | Bearer-authenticated maintenance transition for the exact worker/environment/company/user |
| `POST /api/worker/runs/{id}/events`, `/finish` | Bearer-authenticated progress and recorded outcome for the assigned worker's run |
| `POST /api/worker/runs/{id}/artifacts` | Bearer-authenticated, idempotent worker ZIP upload; usable after terminal result for recovery |
| `GET /api/runs/{id}/report`, `/download` | Authenticated combined run record and downloadable evidence ZIP |
| `GET /api/loops/{id}/report`, `/download` | Authenticated instruction/generation record, including failures before a run |
| `GET /api/runs/{id}/files/{path}` | Authenticated individual worker evidence file |
| `GET /reports/runs/{id}`, `/reports/loops/{id}` | Full-page report interface |

## Historical acceptance evidence

The dated checkpoints below predate the company-account change and do not establish that its signup, email integration, migration, or tenant isolation has been deployed.

### General executor and evidence archive — September 28, 2026

Final app revision **`20260928-run-evidence-004`**, ECS task definition **`ctrlloop-mvp:6`**, reached steady state at **23:45:23 UTC**. Readback at 23:46 UTC verified one running task, no pending tasks, a sole healthy target, HTTP health 200, and deployed report assets identical to the reviewed source. The image digest is `sha256:b78ce9ab5e3b357d2528aaffe256b0a78c53e68aa6b34c4331866b1cd239c2cb`.

The generic worker `c239319d3eaea74a` was verified with saved request `d926493e-fb4f-4055-9bca-806552117406`: “Click the Bold button in the blank LibreOffice Writer document.” Run [`0499cd53-ec33-4328-a441-9965414f1fb5`](https://d1t0sm9jhdinfi.cloudfront.net/reports/runs/0499cd53-ec33-4328-a441-9965414f1fb5) started at **23:30:01.277 UTC** and completed four bounded action loops. Jev selected a native Bold control each time. The outcome is **unverified**: the iteration limit is not a task-completion checker, and the pipeline can repeat an action.

The worker operational duration was **44,962.600 ms** and the complete record through report generation was **47,879.814 ms**. The four current-state-to-last-input-dispatch loop durations were **5,683.406**, **6,090.622**, **6,475.933**, and **7,547.629 ms**; their end timestamps exactly match the final native command dispatch in each loop. Action returns, settling, screenshots and report work outside these boundaries remain separately visible. Each loop used one window-selection round and four control-selection rounds. First control rounds needed four parallel API batches under the existing byte limit; later rounds each needed one call. No Jev question was changed.

The **32 Jev API calls** consumed **668,683 input tokens**, estimated at **$0.028084686** at $0.042/million, with free output. Instruction generation used **117 input / 55 output tokens**, took **3,346.58 ms**, and cost an estimated **$0.0000392**. These are usage estimates, not billed charges. The live report includes each call, group, question, probability and chosen action.

Authenticated readback verified **741 worker events**, exact OpenAI request/response bodies, **8 before/after screenshots**, every worker manifest file hash, and the downloaded ZIP's CRCs. The private worker archive is **8,715,978 bytes**; the combined server/worker download is **9,594,426 bytes**. Its manifest has no capture gaps. Signed-out report, file and download requests returned 401, and S3 uses AES256 encryption. Evidence completeness does not imply task success.

Four older surviving reports were also archived privately, with unavailable historical captures explicitly listed. The prior guest profile and guest-only files were already absent when inspected; restoring services on the same assigned guest did not recover those files. DBeaver was connected to the real `ctrlloop_mvp` database and its tables/data grid were visually verified over TLS. See the [infrastructure and cost audit](infrastructure-costs-20260928.md) for capacity, shared costs, billing limits and polling behavior.

Validation used Python compilation, JavaScript syntax checks, browser rendering, authenticated live integration and archive readback; no tests were added. Desktop and mobile rendering of the actual completed report had no overflow or JavaScript errors. Raw records and call details render on expansion: initial HTML was about 100 KB, and expansion, collapse and reopening preserved all 39 questions in the inspected call. The deployed page also visibly loaded the completed report and expanded that call's questions, probabilities, tokens and cost. The full report JSON still downloads before rendering (about 23.7 MB for this run), so initial load time depends on the connection. The preserved Hermes entrypoint and pinned archived runtime still exist, and its `--help` launch succeeds independently.

### Earlier X-navigation demonstration

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

The historical visual evidence shows the authenticated product interface, embedded Chrome desktop, and online assigned worker, with no provider credentials or private access-file contents. No screenshot containing secrets is included in this document.

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
