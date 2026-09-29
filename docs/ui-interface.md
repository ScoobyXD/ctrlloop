# CtrlLoop interface and single-submit execution

The UI creates and starts a CtrlLoop with one submission. Generation and dispatch are coordinated by the backend; the browser does not send a second automatic run request.

## API contract

`POST /api/loops` accepts:

```json
{"prompt": "Open the search field in my browser", "start": true}
```

The response is the existing loop object, including `runs: [queuedRun]`. The loop initially has `status: "generating"`; the reserved run has `status: "queued"`. Omitting `start`, or sending `false`, preserves the earlier save/generate-only behavior and returns `runs: []`. Explicit reruns still use `POST /api/loops/{id}/runs`.

Creation and reservation commit in one database transaction. Starting requires an assigned, enabled worker under agent control, seen within the last 30 seconds, with no queued/running run or active-run pointer. Unassigned, offline, human-controlled, maintenance, and occupied desktops return HTTP 409 without leaving a new loop behind. The existing generation-capacity limit still returns HTTP 429. The existing unique active-run index and worker lock prevent double reservation.

Both entry points use the same reservation helper and existing `runs` table. Two additive columns hold the requesting session hash and dispatch deadline. Neither field is returned to the browser, worker, or report. Worker polling waits for a ready, nonempty instruction, then rechecks the requesting session, account privileges, target company/user and assignment before dispatch. Account, session, and worker/run locks preserve authorization through that transaction. Concurrent polls dispatch a reservation once.

## Lifetime and navigation

The five-minute deadline starts when the run is reserved and includes instruction generation time. It limits dispatch only; it does not stop an already running desktop task. If it expires, the pending run becomes `interrupted` and requires an explicit new start. A reconnect never silently requeues it.

Navigating Home, selecting another CtrlLoop, opening/closing the Desktop tab, refreshing, or closing the browser does not cancel a committed reservation. Generation and worker polling run on the server independently of UI polling. The active sign-in session must remain valid for dispatch. A lost create response should be reconciled through the loop list/detail; clients must not blindly replay the POST.

Signing out cancels runs still queued by that session. Session expiration, account/role revocation, and invalidated target assignments also prevent pending dispatch. Devadmin company/user selection changes only the viewing context; pending runs keep their original authorized target and continue across navigation. Generation failure/cancellation, server restart, or worker restart interrupts queued execution without replaying actions. Already dispatched runs retain the existing execution/recovery behavior; logout is not a stop-running command.

`GET /api/session` exposes `worker.queued_run` as the reserved run UUID or `null`, alongside the existing `worker.active_run`. It derives this field from the existing queued run after expiring invalid reservations in the same transaction, so browser navigation and reloads retain the authoritative busy state. The session read releases five-minute-expired, invalid-session, or reassigned-worker reservations even if the worker is offline.

Queued reservations block human takeover and additional starts while generation is in progress. UI availability and automatic-start wording should reflect the pending run even when another page is selected. A generating loop without a queued reservation is only preparing an instruction.

## Local verification performed

No test files were added. Verification used the existing Python environment and a disposable PostgreSQL 18 instance bound only to `127.0.0.1:59482`. The database was stopped afterward; no existing database, provider API, or desktop was used.

- Python AST parsing, backend module import, and `git diff --check` passed.
- The schema applied successfully to a fresh database and applied a second time unchanged.
- Direct async endpoint calls verified legacy generation-only creation, a queued start reservation, waiting through generation, rollback of duplicate submission, blocked human takeover, and exactly one dispatch from two simultaneous polls after readiness.
- Human-control, maintenance, offline, and unassigned worker starts returned HTTP 409.
- Deadline expiry, logout, expired session, and worker restart interrupted queued runs.
- The actual generation exception handler interrupted its queued run when an in-process unavailable client deliberately failed before any network call. Server-start interruption of queued runs was also exercised.
- Loop creation/detail, worker dispatch, and report responses excluded the private session hash and dispatch deadline.
- Repeated session reads returned the queued UUID without selected-loop state. A session read cleared an expired reservation with the worker offline, a reservation from an invalidated session, and a reservation belonging to a previous desktop assignee.

These initial checks preceded the combined company-account integration and did not exercise a real instruction provider or a live desktop action.

## Interface

The interface uses a black/charcoal background, orange accents, off-white type,
monospaced measurements, fine technical grids, and restrained engineering diagrams.
A persistent sidebar contains Home, New CtrlLoop, and the company's saved CtrlLoops.
Home has the request composer and recent CtrlLoops. Selecting a CtrlLoop opens its
request, generated instruction, progress, and recorded execution activity. The
Desktop tab opens the existing authenticated noVNC viewer on demand; leaving it
disconnects the viewer without stopping server-side execution. Home also exposes
the desktop, including when no CtrlLoop exists yet. There are no folders.

Mobile navigation collapses into a keyboard-accessible drawer. Activity/Desktop
tabs support arrow keys, Home and End. Ctrl/Cmd+Enter submits the composer, and N
opens a new request outside editable fields and the desktop canvas. Reuse copies
an existing request into a new composer; it does not overwrite history.

Browser verification used an explicitly labeled local sample-data preview, with
desktop and 390-pixel mobile viewports. There were no JavaScript page errors or
horizontal page overflow. One submission sent one POST with start:true and no
automatic second run POST. Preparation/execution state, queued busy state after
navigating Home, Home desktop access, drawer closing, report layout, and login
were inspected. The report preview used previously captured run evidence.

The combined release includes company email signup/login, company administration, and explicit devadmin target selection. The company backend is the canonical account model; ordinary users cannot switch companies. Report links and desktop connections retain the authorized target context.
