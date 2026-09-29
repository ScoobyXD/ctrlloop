const $ = (id) => document.getElementById(id);
const activeStatuses = new Set(["queued", "pending", "delivering", "delivered", "acknowledged", "running", "processing", "selecting", "executing", "verifying"]);
const errorStatuses = new Set(["error", "failed", "blocked", "unverified", "rejected", "cancelled", "canceled", "expired", "interrupted"]);
const stageLabels = {
  user_request: "User request", request: "User request", llm_generation: "LLM generation",
  llm_started: "LLM generation started", llm_completed: "Instruction generated", llm_generated: "Instruction generated", instruction_generated: "Instruction generated",
  db_saved: "Saved to PostgreSQL", saved: "Saved to PostgreSQL", queued: "Delivery queued", delivery_queued: "Delivery queued", delivery_acknowledged: "Worker acknowledgment",
  delivery: "Instruction delivery", delivered: "Instruction delivered", acknowledgment: "Worker acknowledgment",
  acknowledged: "Worker acknowledgment", worker_ack: "Worker acknowledgment", jev_selection: "Jev selection",
  selection: "Jev selection", action_dispatch: "Action dispatched", action: "Native action",
  observation: "Desktop observation", observed_result: "Observed result", verification: "Result verification",
  completed: "Run completed", error: "Error", failed: "Run failed",
};
const viewerRetryDelays = [1000, 2000, 4000, 8000, 15000];
const state = { session: null, sessionGeneration: 0, signingOut: false, loops: [], detail: null, selectedId: null, creating: false, running: false, changingMode: false, pollBusy: false, rfb: null, viewerGeneration: 0, viewerConnecting: false, viewerWanted: false, viewerRetryCount: 0, viewerRetryTimer: null, viewerConnectTimer: null, viewerStableTimer: null, viewerSessionController: null, viewerFailure: "", lastDetailSignature: "", lastListSignature: "" };
Object.assign(state, { view: "home", controlUnconfirmed: false, switchingCompany: false, members: [], savingMember: false, savingCompany: false, authBusy: false, authView: "login", resetToken: null, companies: [], companyContext: null, userContext: null });
const emptyDetail = $("loop-detail").innerHTML;
let viewerModule;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}
function humanize(value) { return String(value || "unknown").replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase()); }
function valueText(value) { return typeof value === "string" ? value : JSON.stringify(value ?? ""); }
function validDate(value) { const date = value ? new Date(value) : null; return date && Number.isFinite(date.getTime()) ? date : null; }
function formatDate(value, options = {}) { return validDate(value)?.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...options }) || "Time unavailable"; }
function formatTime(value) { return validDate(value)?.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) || "—"; }
function formatDuration(ms) { if (!Number.isFinite(ms) || ms < 0) return "—"; if (ms < 1000) return `${Math.round(ms)} ms`; if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`; return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`; }
function statusClass(status) { return errorStatuses.has(status) ? "error" : activeStatuses.has(status) || status === "generating" ? "active" : ["completed", "succeeded", "ready", "saved"].includes(status) ? "" : "neutral"; }
function badge(status) { return `<span class="badge ${statusClass(status)}">${escapeHtml(humanize(status))}</span>`; }
function getRuns() { return Array.isArray(state.detail?.runs) ? state.detail.runs : []; }
function hasActiveRun() { return Boolean(state.session?.worker?.active_run || state.session?.worker?.queued_run) || getRuns().some((run) => run.worker_id === state.session?.worker?.id && activeStatuses.has(run.status)); }
function desktopIsVisible() { return ["loop", "desktop"].includes(state.view); }
function autoStartPending() { return getRuns().some(run => run.status === "queued" && run.worker_id === state.session?.worker?.id); }
function personName(person) { return person?.display_name || person?.email || "Earlier company member"; }
function canManageCompany() { return state.session?.user?.is_devadmin === true || state.session?.company?.role === "company_admin"; }
function contextHeaders() {
  if (!state.session?.user?.is_devadmin) return {};
  return { ...(state.companyContext ? { "X-Company-ID": state.companyContext } : {}), ...(state.userContext ? { "X-User-ID": state.userContext } : {}) };
}
function scopedURL(path) {
  const url = new URL(path, location.origin);
  if (state.session?.user?.is_devadmin) {
    if (state.companyContext) url.searchParams.set("company_id", state.companyContext);
    if (state.userContext) url.searchParams.set("user_id", state.userContext);
  }
  return url.pathname + url.search;
}
function resetCompanyView() {
  disconnectViewer(); state.loops = []; state.detail = null; state.selectedId = null; state.members = [];
  state.creating = false; state.running = false; state.changingMode = false; state.savingMember = false; state.savingCompany = false; $("save-company").disabled = false; state.viewerFailure = "";
  $("create-loop").disabled = state.switchingCompany; $("create-loop").innerHTML = 'Start CtrlLoop <span aria-hidden="true">↗</span>'; $("prompt").disabled = false;
  state.lastListSignature = ""; state.lastDetailSignature = ""; state.view = "home";
  renderNavigation(false);
  $("loop-detail").innerHTML = emptyDetail; $("member-list").replaceChildren(); $("member-count").textContent = "";
  $("member-management").open = false; $("member-error").hidden = true; $("page-notice").hidden = true;
  $("company-form").reset(); delete $("company-form").dataset.companyId; $("company-error").hidden = true; renderLoops();
}
function getModel() {
  const raw = state.session?.model;
  const id = typeof raw === "string" ? raw : raw?.id || raw?.model || state.session?.llm_model;
  const effort = (typeof raw === "object" && raw?.reasoning_effort) || state.session?.reasoning_effort;
  return { id, effort, label: id ? `${id}${effort ? ` · ${effort} reasoning` : ""}` : "Model not reported" };
}

async function api(path, options = {}) {
  const sessionGeneration = state.sessionGeneration;
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...contextHeaders(), ...(options.headers || {}) } });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    if (response.status === 401 && !["/api/login", "/api/signup", "/api/verify-email", "/api/resend-verification", "/api/forgot-password", "/api/reset-password"].includes(path) && sessionGeneration === state.sessionGeneration) showLogin();
    const detail = body?.detail ?? body?.error ?? body?.message;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(issue => issue.msg || "Invalid input").join("; ") : `Request failed (${response.status}).`;
    const error = new Error(message); error.status = response.status; throw error;
  }
  return body;
}
function notice(message, isError = false) { $("page-notice-text").textContent = message; $("page-notice").hidden = false; $("page-notice").classList.toggle("error", isError); }
function setConnection(connected) { $("connection-state").innerHTML = `<i class="status-dot ${connected ? "live" : "warn"}"></i>${connected ? "Connected" : "Connection interrupted"}`; }

function showLogin() {
  const wasAuthenticated = state.session?.authenticated;
  state.sessionGeneration += 1;
  state.session = null; state.companyContext = null; state.userContext = null; state.companies = [];
  resetCompanyView(); if (wasAuthenticated) showAuth("login");
  $("app").hidden = true; $("login-screen").hidden = false;
  if (wasAuthenticated) { $("login-error").textContent = "Your session ended. Sign in to continue."; $("login-error").hidden = false; }
}
function adoptSession(session) {
  if (!session?.authenticated) { showLogin(); return false; }
  const previous = state.session;
  if (previous?.authenticated && (previous.user?.id !== session.user?.id || previous.company?.id !== session.company?.id || previous.worker?.id !== session.worker?.id || previous.target_user_id !== session.target_user_id)) {
    // A session check from any caller must invalidate old company requests.
    state.sessionGeneration += 1; resetCompanyView(); history.replaceState(null, "", "#home");
  }
  state.session = session;
  state.controlUnconfirmed = false;
  state.companyContext = session.user?.is_devadmin ? session.company?.id || null : null;
  state.userContext = session.user?.is_devadmin ? session.target_user_id || null : null;
  $("login-screen").hidden = true; $("app").hidden = false;
  renderSession(); setConnection(true); return true;
}
async function loadSession() {
  const generation = state.sessionGeneration;
  const session = await api("/api/session");
  if (generation !== state.sessionGeneration) return false;
  return adoptSession(session);
}
function renderSession() {
  const worker = state.session?.worker;
  const user = state.session?.user;
  const company = state.session?.company;
  const model = getModel();
  $("user-name").textContent = personName(user);
  $("user-initial").textContent = personName(user).slice(0, 1).toUpperCase();
  $("breadcrumb-company").textContent = company?.name || "Company";
  $("company-nav").hidden = !canManageCompany();
  $("user-name").title = user?.email || "";
  $("user-role").textContent = user?.is_devadmin ? "Platform devadmin" : humanize(company?.role);
  $("company-name").textContent = company?.name || "No company";
  $("member-management").hidden = !canManageCompany();
  if (!canManageCompany()) $("member-management").open = false;
  if ($("company-form").dataset.companyId !== company?.id) { $("company-setting-name").value = company?.name || ""; $("company-form").dataset.companyId = company?.id || ""; }
  $("admin-context").hidden = !user?.is_devadmin;
  const contextBusy = state.switchingCompany || state.creating || state.running || state.changingMode || state.savingMember || state.savingCompany;
  $("admin-company").disabled = contextBusy;
  $("admin-user").disabled = contextBusy;
  $("admin-home").disabled = contextBusy;
  $("admin-company").value = company?.id || "";
  $("admin-user").value = state.session?.target_user_id || "";
  const target = state.members.find(member => String(member.user_id) === String(state.session?.target_user_id));
  const targetName = target ? personName(target) : state.session?.target_user_id === user?.id ? personName(user) : state.session?.target_user_id ? "Selected person" : "No person selected";
  $("admin-context-summary").textContent = `${company?.name || "Company"} · ${targetName}. Actions use this company and the selected person’s assigned desktop.`;
  $("desktop-owner").textContent = user?.is_devadmin ? `${targetName}’s desktop` : `${personName(user)}’s desktop`;
  $("desktop-scope-note").textContent = user?.is_devadmin ? `Current company: ${company?.name || "Company"}. This viewer and all new runs use ${targetName}’s assigned desktop.` : "This is your assigned desktop. Other company members run CtrlLoops on their own desktops.";
  if (state.view === "desktop") $("loop-title").textContent = user?.is_devadmin ? `${targetName}’s desktop` : "Your desktop";
  $("model-label").textContent = model.label;
  $("model-detail-id").textContent = model.label;
  $("jev-detail-id").textContent = state.session?.jev_model || worker?.jev_model || "Not reported";
  $("worker-identity").textContent = worker?.id || "No desktop assigned";
  $("worker-detail-id").textContent = worker?.id || "Not assigned yet";
  $("worker-status").textContent = worker ? `Your worker · ${humanize(worker.status)}${worker.mode === "maintenance" ? " · Worker deployment in progress" : ""}` : "No desktop assigned to the selected account";
  $("worker-last-seen").textContent = worker?.last_seen ? `Last seen ${formatTime(worker.last_seen)}` : "";
  $("worker-last-seen").title = worker?.last_seen ? formatDate(worker.last_seen, { second: "2-digit", timeZoneName: "short" }) : "";
  $("fullscreen").disabled = !worker;
  if (!worker) {
    if (state.rfb || state.viewerConnecting || state.viewerWanted) disconnectViewer();
    setViewerStatus("No desktop assigned", "warn");
    viewerPlaceholder(user?.is_devadmin && !state.session?.target_user_id ? "Choose a person to view their desktop" : "No desktop assigned to this account", "You can save and browse company CtrlLoops. Assign a desktop to this account before running them.", false);
  }
  renderControl(); renderHome();
  if (state.view === "company" && !canManageCompany()) showHome();
}
function renderControl() {
  const human = state.session?.worker?.mode === "human";
  const maintenance = state.session?.worker?.mode === "maintenance";
  const agent = state.session?.worker?.mode === "agent";
  const canControl = state.session?.worker?.can_control === true && !state.controlUnconfirmed;
  const active = hasActiveRun();
  const disabled = !state.session?.worker?.id || (!human && !agent) || state.controlUnconfirmed || state.changingMode || state.switchingCompany || state.running || active || (human && !canControl);
  $("control-mode-label").textContent = maintenance ? "Deployment in progress" : canControl ? "You have control" : "View only";
  $("control-mode-description").textContent = !state.session?.worker ? "Waiting for a desktop to be assigned to you." : maintenance ? "Runs and control will resume after worker deployment." : state.changingMode ? "Switching desktop control…" : canControl ? "Return control when you're ready to run a loop." : human ? "Another session of your account controls this desktop." : active ? "The agent is running. Control is available when it finishes." : "Click the desktop to take control.";
  $("control-mode-icon").textContent = canControl ? "↖" : "◉";
  if (state.controlUnconfirmed) {
    $("control-mode-label").textContent = "Checking control";
    $("control-mode-description").textContent = "Desktop input is paused until ownership can be confirmed.";
  }
  $("control-mode-icon").parentElement.classList.toggle("human", canControl);
  $("toggle-control").textContent = state.changingMode ? "Switching…" : "Return to agent ↗";
  $("toggle-control").hidden = !canControl;
  $("toggle-control").disabled = disabled;
  const inputEnabled = human && canControl && !disabled;
  const takeover = $("desktop-takeover");
  takeover.hidden = inputEnabled || !state.rfb || state.viewerConnecting;
  takeover.setAttribute("aria-disabled", String(disabled));
  if (state.rfb) { state.rfb.viewOnly = !inputEnabled; state.rfb.focusOnClick = inputEnabled; }
  const startReason = !state.session?.worker?.id ? "Save your CtrlLoop now. An assigned desktop is needed before it can run." : maintenance ? "Your desktop is being updated. Starting will be available when it’s ready." : human ? "Return desktop control to the agent before starting a CtrlLoop." : state.session?.worker?.status !== "online" ? "Your desktop is offline. Starting will be available when it reconnects." : active ? "Your desktop is running a CtrlLoop. You can follow its activity or start another when it finishes." : "Describe it once. We’ll prepare the instruction and start.";
  $("compose-hint").textContent = startReason;
  $("create-loop").disabled = state.creating || state.switchingCompany || state.running || state.changingMode || (Boolean(state.session?.worker?.id) && (active || !agent || state.session.worker.status !== "online"));
  if (!state.creating) $("create-loop").innerHTML = `${state.session?.worker?.id ? "Start" : "Save"} CtrlLoop <span aria-hidden="true">↗</span>`;
  $("compose-worker").textContent = !state.session?.worker ? "No desktop assigned" : active ? "Desktop in use" : human ? "Manual control" : state.session.worker.status === "online" ? "Desktop ready" : "Desktop offline";
  $("compose-worker-dot").className = `status-dot ${agent && state.session?.worker?.status === "online" && !active ? "live" : "warn"}`;
  const runButton = $("run-instruction");
  if (runButton) {
    runButton.hidden = state.detail?.status === "generating" || active;
    runButton.disabled = state.switchingCompany || state.running || active || !agent || !state.detail?.instruction || state.detail?.status !== "ready" || state.session?.worker?.status !== "online";
    runButton.textContent = state.running ? "Starting…" : active ? "Run in progress" : getRuns().length ? "Run again ↗" : "Start CtrlLoop ↗";
  }
  const hint = $("run-control-hint");
  if (hint) { hint.textContent = human ? canControl ? "Return control to the agent to run this instruction." : "Your other session must return desktop control before a run can start." : !state.session?.worker?.id ? "Desktop provisioning pending. You can run this CtrlLoop once a desktop is assigned to you." : state.session?.worker?.status !== "online" ? "Waiting for your assigned worker to come online." : state.detail?.status === "generating" ? autoStartPending() ? "Preparing the instruction. Execution will start automatically." : "Preparing the saved instruction. No execution is currently queued." : "Watch this run on the live desktop beside the instruction. Recorded steps appear below."; }
  if (hint && maintenance) hint.textContent = "Worker deployment in progress. Run and control access will resume when deployment completes.";
}


function renderNavigation(writeHistory = true) {
  $("home-view").hidden = state.view !== "home";
  $("loop-view").hidden = !["loop", "desktop"].includes(state.view);
  $("loop-workspace").classList.toggle("desktop-only", state.view === "desktop");
  $("loop-progress").hidden = state.view !== "loop";
  $("company-view").hidden = state.view !== "company";
  $("home-nav").classList.toggle("selected", state.view === "home");
  $("home-nav").toggleAttribute("aria-current", state.view === "home");
  if (state.view === "home") $("home-nav").setAttribute("aria-current", "page");
  $("company-nav").classList.toggle("selected", state.view === "company");
  $("breadcrumb-page").textContent = state.view === "loop" ? "CtrlLoop" : state.view === "desktop" ? "Desktop" : state.view === "company" ? "People & access" : "Home";
  $("activity-view").hidden = state.view !== "loop";
  const desktopVisible = desktopIsVisible();
  if (!desktopVisible) {
    setExpandedDesktop(false);
    if (document.fullscreenElement === $("desktop-panel")) void document.exitFullscreen().catch(() => {});
  }
  if (!desktopVisible && (state.viewerWanted || state.rfb || state.viewerConnecting)) disconnectViewer();
  if (desktopVisible && state.session?.worker?.id && !state.viewerWanted && !state.signingOut) void connectViewer({ manual: true });
  if (writeHistory) {
    const route = state.view === "loop" && state.selectedId ? `#loop/${encodeURIComponent(state.selectedId)}` : state.view === "desktop" ? "#desktop" : state.view === "company" ? "#company" : "#home";
    if (location.hash !== route) history.pushState(null, "", route);
  }
  setSidebarOpen(false);
}
function showHome({ focus = false, fromHistory = false } = {}) {
  state.view = "home"; state.selectedId = null; state.detail = null; state.lastDetailSignature = "";
  renderNavigation(!fromHistory); renderLoops(); renderControl();
  if (focus) { $("prompt").focus(); $("compose-panel").scrollIntoView({ behavior: "smooth", block: "center" }); }
}
function showDesktop(fromHistory = false) {
  state.view = "desktop"; state.selectedId = null; state.detail = null;
  $("loop-title").textContent = state.session?.user?.is_devadmin ? $("desktop-owner").textContent : "Your desktop"; $("loop-reference").textContent = "COMPANY / DESKTOP";
  $("loop-heading-status").replaceChildren(); renderNavigation(!fromHistory); renderLoops(); renderControl();
}
async function openRoute() {
  if (location.hash === "#desktop") { showDesktop(true); return; }
  const route = location.hash.match(/^#loop\/([^/]+)(\/desktop)?$/);
  if (route) { await selectLoop(decodeURIComponent(route[1]), { fromHistory: true }); return; }
  if (location.hash === "#company" && canManageCompany()) {
    state.view = "company"; state.selectedId = null; renderNavigation(false); renderLoops(); $("member-management").open = true; return;
  }
  showHome({ fromHistory: true });
}
function renderHome() {
  $("home-loop-count").textContent = `${state.loops.length} SAVED`;
  const recent = state.loops.slice(0, 3);
  const signature = JSON.stringify(recent);
  if ($("recent-loops").dataset.signature === signature) return;
  $("recent-loops").dataset.signature = signature;
  $("recent-loops").innerHTML = recent.length ? recent.map((loop, index) => `<button class="recent-loop" data-loop-id="${escapeHtml(loop.id)}"><span class="recent-loop-index">CTRLLOOP / ${String(index + 1).padStart(2, "0")}</span><span class="recent-loop-arrow" aria-hidden="true">↗</span><span class="recent-loop-title">${escapeHtml(loop.prompt || "Untitled CtrlLoop")}</span><span class="recent-loop-meta"><i class="status-dot ${loop.status === "generating" ? "warn" : errorStatuses.has(loop.status) ? "error" : ""}"></i>${escapeHtml(humanize(loop.status))}<span>·</span>${escapeHtml(validDate(loop.created_at)?.toLocaleDateString(undefined, { month: "short", day: "numeric" }) || "")}</span></button>`).join("") : '<div class="recent-empty"><span aria-hidden="true">[↳]</span><p>Your first CtrlLoop starts above.<small>Saved requests and their activity will appear here.</small></p></div>';
}

async function loadLoops() {
  const generation = state.sessionGeneration;
  const response = await api("/api/loops");
  if (generation !== state.sessionGeneration || !state.session) return;
  state.loops = Array.isArray(response) ? response : response?.loops || [];
  renderLoops();
}
function renderLoops() {
  renderHome();
  const signature = JSON.stringify([state.loops, state.selectedId]);
  if (signature === state.lastListSignature) return;
  state.lastListSignature = signature;
  $("loop-count").textContent = String(state.loops.length).padStart(2, "0");
  if (!state.loops.length) { $("loop-list").innerHTML = '<p class="list-empty">Your CtrlLoops live here.<br>Start with a new request.</p>'; return; }
  $("loop-list").innerHTML = state.loops.map(loop => `<button class="loop-list-item ${String(loop.id) === String(state.selectedId) ? "selected" : ""}" data-loop-id="${escapeHtml(loop.id)}" ${String(loop.id) === String(state.selectedId) ? 'aria-current="page"' : ""}><span class="loop-icon" aria-hidden="true">↳</span><span class="list-title">${escapeHtml(loop.prompt || "Untitled CtrlLoop")}</span><span class="list-item-meta"><i class="status-dot ${errorStatuses.has(loop.status) ? "error" : loop.status === "generating" ? "warn" : ""}"></i>${escapeHtml(humanize(loop.status))}<time title="${escapeHtml(formatDate(loop.created_at))}">${escapeHtml(validDate(loop.created_at)?.toLocaleDateString(undefined, { month: "short", day: "numeric" }) || "—")}</time></span></button>`).join("");
}
async function selectLoop(id, { fromHistory = false } = {}) {
  state.selectedId = id; state.detail = null; state.view = "loop"; state.lastDetailSignature = "";
  $("loop-progress").replaceChildren();
  $("loop-title").textContent = state.loops.find(loop => String(loop.id) === String(id))?.prompt || "Loading CtrlLoop…";
  $("loop-reference").textContent = `CTRLLOOP / ${String(id).slice(0, 8).toUpperCase()}`;
  $("loop-heading-status").replaceChildren(); $("loop-detail").innerHTML = '<p class="loading-message" role="status">Loading activity…</p>';
  renderLoops(); renderNavigation(!fromHistory);
  try { await loadDetail(id); }
  catch (error) { if (String(state.selectedId) === String(id) && error.status !== 401) { $("loop-title").textContent = "CtrlLoop unavailable"; $("loop-detail").innerHTML = `<p class="inline-error">${escapeHtml(error.message)}</p>`; notice(`Couldn’t load this CtrlLoop: ${error.message}`, true); } }
}

async function loadDetail(id = state.selectedId) {
  if (!id) return;
  const generation = state.sessionGeneration;
  const detail = await api(`/api/loops/${encodeURIComponent(id)}`);
  if (generation !== state.sessionGeneration || String(state.selectedId) !== String(id) || !state.session) return;
  state.detail = detail; renderDetail(); renderControl();
}
function renderEvent(event, index, groupKey) {
  const stage = event.stage || event.type || "event";
  const data = event.data;
  const failed = errorStatuses.has(stage) || errorStatuses.has(data?.status);
  const timestamp = event.created_at || event.timestamp;
  const duration = data?.duration_ms ?? data?.elapsed_ms;
  const label = stageLabels[stage] || humanize(stage);
  return `<li class="timeline-event ${failed ? "error" : ""}"><div class="event-head"><span class="event-title">${escapeHtml(label)}</span><time class="event-time" datetime="${escapeHtml(timestamp || "")}" title="${escapeHtml(formatDate(timestamp, { second: "2-digit", timeZoneName: "short" }))}">${escapeHtml(formatTime(timestamp))}</time></div>${event.message ? `<p class="event-message">${escapeHtml(valueText(event.message))}</p>` : ""}${Number.isFinite(duration) ? `<p class="event-message">${escapeHtml(formatDuration(duration))}</p>` : ""}${data != null && Object.keys(Object(data)).length ? `<details class="event-details" data-event-key="${escapeHtml(`${groupKey}-${event.id || index}`)}"><summary>Event data</summary><pre class="event-data">${escapeHtml(JSON.stringify(data, null, 2))}</pre></details>` : ""}</li>`;
}
function renderRun(run, index, total) {
  const events = Array.isArray(run.events) ? run.events : [];
  const links = `<p class="workflow-attribution">Started by ${escapeHtml(personName(run.initiator))}${run.worker_id ? ` · Desktop ${escapeHtml(run.worker_id)}` : ""}</p><div class="instruction-actions"><a class="button secondary small" href="${escapeHtml(scopedURL(`/reports/runs/${encodeURIComponent(run.id)}`))}" target="_blank" rel="noopener">Full-page report ↗</a><a class="button quiet small" href="${escapeHtml(scopedURL(`/api/runs/${encodeURIComponent(run.id)}/download`))}">Download evidence ZIP ↓</a></div>`;
  return `<section class="run-group"><div class="run-summary"><div><span>Run ${total - index}</span><span class="run-id" title="${escapeHtml(run.id)}">${escapeHtml(String(run.id || "").slice(0, 8))}</span>${badge(run.status)}</div><span class="run-duration" data-run-start="${escapeHtml(run.started_at || run.created_at || "")}" data-run-end="${escapeHtml(run.finished_at || "")}" data-run-active="${activeStatuses.has(run.status)}">—</span></div>${links}${events.length ? `<ol class="timeline">${events.map((event, eventIndex) => renderEvent(event, eventIndex, run.id)).join("")}</ol>` : '<p class="no-events">No worker events recorded yet.</p>'}${run.error ? `<div class="inline-error run-error">${escapeHtml(valueText(run.error))}</div>` : ""}</section>`;
}
function renderDetail() {
  if (!state.detail) return;
  const loop = state.detail;
  const signature = JSON.stringify(loop);
  if (signature === state.lastDetailSignature) return;
  state.lastDetailSignature = signature;
  const openKeys = new Set([...$("loop-detail").querySelectorAll("details[open]")].map(item => item.dataset.eventKey));
  const focusedId = $("loop-detail").contains(document.activeElement) ? document.activeElement.id : null;
  const runs = [...getRuns()].sort((a, b) => (validDate(b.created_at)?.getTime() || 0) - (validDate(a.created_at)?.getTime() || 0));
  const latest = runs[0];
  const loopEvents = Array.isArray(loop.events) ? loop.events : [];
  const model = typeof loop.model === "object" ? loop.model?.id || loop.model?.model : loop.model;
  const generating = loop.status === "generating";
  const currentStatus = loop.status === "error" ? "error" : generating ? "preparing" : latest?.status || loop.status;
  $("loop-title").textContent = loop.prompt || "Untitled CtrlLoop";
  $("loop-reference").textContent = `CTRLLOOP / ${String(loop.id).slice(0, 8).toUpperCase()}`;
  $("loop-heading-status").innerHTML = badge(currentStatus);
  const prepared = Boolean(loop.instruction);
  const executed = latest && !activeStatuses.has(latest.status) && !errorStatuses.has(latest.status);
  const progress = `<div class="progress-strip" aria-label="CtrlLoop progress"><div class="progress-step done"><span><i>✓</i></span><div><strong>Request</strong><small>Saved to company</small></div></div><div class="progress-step ${prepared ? "done" : generating ? "current" : ""}"><span><i>${prepared ? "✓" : "02"}</i></span><div><strong>Prepare</strong><small>${prepared ? "Instruction ready" : generating ? "Generating…" : "Needs attention"}</small></div></div><div class="progress-step ${executed ? "done" : prepared && latest && activeStatuses.has(latest.status) ? "current" : ""}"><span><i>${executed ? "✓" : "03"}</i></span><div><strong>Execute</strong><small>${generating && latest?.status === "queued" ? "Starts automatically" : latest ? escapeHtml(humanize(latest.status)) : "Not started"}</small></div></div></div>`;
  $("loop-progress").innerHTML = progress;
  $("loop-detail").innerHTML = `<aside class="request-summary"><div class="detail-heading"><h2>Request & instruction</h2><button id="reuse-request" class="button quiet small" title="Use this request in a new CtrlLoop">Reuse ↗</button></div><div class="request-block"><div class="content-label"><span>REQUEST</span></div><p class="request-text">${escapeHtml(loop.prompt)}</p><p class="workflow-attribution">Created by ${escapeHtml(personName(loop.creator))}<br>${escapeHtml(formatDate(loop.created_at))}</p></div><div class="instruction-block"><div class="content-label"><span>INSTRUCTION</span><span>${prepared ? "SAVED" : "PENDING"}</span></div>${prepared ? `<p class="instruction-text">${escapeHtml(loop.instruction)}</p>` : `<p class="instruction-missing">${generating ? autoStartPending() ? "Preparing your instruction. Execution will start automatically on your assigned desktop." : "Preparing the saved instruction. No execution is currently queued." : "An instruction could not be prepared. Your original request is saved."}</p>`}${model ? `<div class="instruction-meta">${escapeHtml(model)}</div>` : ""}${loop.error ? `<div class="inline-error">${escapeHtml(valueText(loop.error))}</div>` : ""}<div class="instruction-actions"><button id="run-instruction" class="button primary" ${!prepared ? "disabled" : ""}>Run again ↗</button></div><p class="run-hint" id="run-control-hint"></p><div class="instruction-actions"><a class="button quiet small" href="${escapeHtml(scopedURL(`/reports/loops/${encodeURIComponent(loop.id)}`))}" target="_blank" rel="noopener">Request report ↗</a><a class="button quiet small" href="${escapeHtml(scopedURL(`/api/loops/${encodeURIComponent(loop.id)}/download`))}">Evidence ↓</a></div></div></aside><div class="activity-column"><div class="activity-header"><h3>Execution activity</h3><span>${runs.length} ${runs.length === 1 ? "run" : "runs"} · ${escapeHtml(Intl.DateTimeFormat().resolvedOptions().timeZone)}</span></div>${loopEvents.length ? `<section class="run-group"><div class="run-summary"><div><span>Instruction preparation</span>${generating ? badge("preparing") : ""}</div></div><ol class="timeline">${loopEvents.map((event, index) => renderEvent(event, index, `loop-${loop.id}`)).join("")}</ol></section>` : ""}${runs.map((run, index) => renderRun(run, index, runs.length)).join("")}${!runs.length ? `<p class="no-events">${generating ? "Preparing the instruction…" : "This CtrlLoop has no recorded executions yet."}</p>` : ""}</div>`;
  for (const element of $("loop-detail").querySelectorAll("details[data-event-key]")) element.open = openKeys.has(element.dataset.eventKey);
  if (focusedId) $(focusedId)?.focus({ preventScroll: true });
  updateDurations(); renderControl();
}

function updateDurations() {
  $("system-clock").textContent = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  for (const element of document.querySelectorAll("[data-run-start]")) {
    const start = validDate(element.dataset.runStart);
    const end = validDate(element.dataset.runEnd);
    element.textContent = start && (end || element.dataset.runActive === "true") ? `${formatDuration((end || new Date()).getTime() - start.getTime())}${!end ? " elapsed" : ""}` : "Duration unavailable";
  }
}

function setViewerStatus(label, kind = "") { $("viewer-state").innerHTML = `<i class="status-dot ${kind}"></i>${escapeHtml(label)}`; }
function viewerPlaceholder(title, message, canReconnect = true) { $("viewer-placeholder").hidden = false; $("viewer-placeholder-title").textContent = title; $("viewer-placeholder-message").textContent = message; $("reconnect-viewer").hidden = !canReconnect; }
function clearViewerTimer(name) { clearTimeout(state[name]); state[name] = null; }
function closeViewerConnection() {
  state.viewerGeneration += 1; state.viewerConnecting = false;
  clearViewerTimer("viewerConnectTimer"); clearViewerTimer("viewerStableTimer");
  state.viewerSessionController?.abort(); state.viewerSessionController = null;
  const rfb = state.rfb; state.rfb = null;
  if (rfb) { try { rfb.disconnect(); } catch { /* The connection may already be closed. */ } }
}
function disconnectViewer() {
  state.viewerWanted = false; state.viewerRetryCount = 0;
  clearViewerTimer("viewerRetryTimer"); closeViewerConnection();
  $("desktop-canvas").replaceChildren(); setViewerStatus("Disconnected");
}
function scheduleViewerReconnect() {
  if (!state.viewerWanted || !state.session?.authenticated || !state.session?.worker?.id || state.switchingCompany || state.signingOut || state.rfb || state.viewerConnecting || state.viewerRetryTimer !== null) return;
  if (state.viewerRetryCount >= viewerRetryDelays.length) {
    setViewerStatus("Disconnected", "warn");
    viewerPlaceholder("Desktop connection unavailable", `${state.viewerFailure} Automatic reconnect stopped after ${viewerRetryDelays.length} attempts. Select Reconnect to try again.`);
    return;
  }
  if (document.hidden) {
    setViewerStatus("Reconnect paused", "warn");
    viewerPlaceholder("Desktop connection interrupted", "Automatic reconnect will resume when you return to this tab.");
    return;
  }
  const delay = viewerRetryDelays[state.viewerRetryCount];
  const generation = state.viewerGeneration;
  setViewerStatus("Reconnecting", "warn");
  viewerPlaceholder("Reconnecting to your desktop", `${state.viewerFailure} Retry ${state.viewerRetryCount + 1} of ${viewerRetryDelays.length} in ${delay / 1000} seconds. You can also select Reconnect now.`);
  state.viewerRetryTimer = setTimeout(() => {
    if (generation !== state.viewerGeneration) return;
    state.viewerRetryTimer = null;
    if (!state.viewerWanted || !state.session?.authenticated || document.hidden) return;
    state.viewerRetryCount += 1;
    void connectViewer();
  }, delay);
}
function viewerInterrupted(title, message, retry = true) {
  closeViewerConnection(); state.viewerFailure = message;
  if (!retry) state.viewerWanted = false;
  setViewerStatus(retry ? "Disconnected" : "Access error", retry ? "warn" : "error");
  viewerPlaceholder(title, message);
  if (retry) scheduleViewerReconnect();
}
async function connectViewer({ manual = false } = {}) {
  if (state.signingOut || state.switchingCompany || !state.session?.worker?.id) return;
  if (manual) { state.viewerWanted = true; state.viewerRetryCount = 0; state.viewerFailure = ""; clearViewerTimer("viewerRetryTimer"); }
  if (!state.viewerWanted || !state.session?.authenticated || state.viewerConnecting || state.rfb) return;
  if (document.hidden) { scheduleViewerReconnect(); return; }
  closeViewerConnection(); state.viewerConnecting = true;
  const generation = state.viewerGeneration;
  const current = () => generation === state.viewerGeneration && state.viewerWanted && state.session?.authenticated;
  const controller = new AbortController(); state.viewerSessionController = controller;
  const attempt = state.viewerRetryCount ? ` Retry ${state.viewerRetryCount} of ${viewerRetryDelays.length}.` : "";
  setViewerStatus(state.viewerRetryCount ? "Reconnecting" : "Connecting", "warn");
  viewerPlaceholder("Connecting to your desktop", `Checking your session and opening the desktop connection…${attempt}`, false);
  state.viewerConnectTimer = setTimeout(() => {
    if (current()) viewerInterrupted("Desktop connection timed out", "The desktop did not connect within 20 seconds.");
  }, 20000);
  try {
    // Every attempt checks the signed-in session; a failed WebSocket is not an authentication signal.
    const session = await api("/api/session", { signal: controller.signal });
    if (!current()) return;
    state.viewerSessionController = null;
    if (!adoptSession(session)) return;
    if (!current()) {
      // Scope changes cancel this attempt; reconnect only with the adopted session.
      if (desktopIsVisible() && session.worker?.id) void connectViewer({ manual: true });
      return;
    }
    if (!session.worker?.id) return;
    viewerModule ||= import("/vendor/novnc/core/rfb.js");
    const { default: RFB } = await viewerModule;
    if (!current()) return;
    const url = new URL(scopedURL("/api/desktop"), window.location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const rfb = new RFB($("desktop-canvas"), url.toString(), { shared: true });
    state.rfb = rfb; rfb.viewOnly = true; rfb.focusOnClick = false; rfb.scaleViewport = true; rfb.resizeSession = false; rfb.background = "#070707";
    rfb.addEventListener("connect", () => {
      if (!current() || state.rfb !== rfb) return;
      clearViewerTimer("viewerConnectTimer"); state.viewerConnecting = false;
      $("viewer-placeholder").hidden = true; setViewerStatus("Live", "live"); renderControl();
      // Brief connect/disconnect flapping must not reset the bounded retry budget.
      state.viewerStableTimer = setTimeout(() => {
        if (!current() || state.rfb !== rfb) return;
        state.viewerStableTimer = null;
        state.viewerRetryCount = 0; state.viewerFailure = "";
      }, 30000);
    });
    rfb.addEventListener("disconnect", () => { if (!current() || state.rfb !== rfb) return; viewerInterrupted("Desktop connection interrupted", "The viewer connection closed. Your saved loops are still available."); });
    rfb.addEventListener("securityfailure", (event) => { if (!current() || state.rfb !== rfb) return; viewerInterrupted("Desktop access failed", event.detail?.reason || "The desktop gateway could not authenticate this connection. Select Reconnect after access is restored.", false); });
    rfb.addEventListener("credentialsrequired", () => { if (!current() || state.rfb !== rfb) return; viewerInterrupted("Desktop authentication required", "The desktop gateway needs server-side credentials. Company credentials are never sent to the desktop.", false); });
  } catch (error) {
    if (!current()) return;
    viewerModule = null;
    viewerInterrupted("Viewer unavailable", `The desktop connection could not open: ${error.message}`);
  }
}

async function refresh() {
  if (state.pollBusy || state.creating || state.signingOut || state.switchingCompany || document.hidden || !state.session?.authenticated) return;
  state.pollBusy = true;
  try {
    if (!await loadSession()) return;
    await Promise.all([loadLoops(), state.selectedId ? loadDetail() : Promise.resolve()]);
    if (desktopIsVisible() && state.session?.worker?.id && !state.viewerWanted && !state.viewerFailure) void connectViewer({ manual: true });
  } catch (error) { if (error.status !== 401) setConnection(false); }
  finally { state.pollBusy = false; }
}
async function boot() {
  const generation = state.sessionGeneration;
  try {
    if (!await loadSession()) return;
    if (generation !== state.sessionGeneration) return;
    await Promise.all([loadLoops(), loadAdminCompanies()]);
    if (generation !== state.sessionGeneration) return;
    if (canManageCompany()) await loadMembers();
    if (generation !== state.sessionGeneration) return;
    await openRoute();
  } catch (error) { if (!state.session) { $("login-error").textContent = `Unable to connect: ${error.message}`; $("login-error").hidden = false; } else notice(error.message, true); }
}

const eyeIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
for (const button of document.querySelectorAll("[data-password-toggle]")) {
  button.innerHTML = eyeIcon;
  button.addEventListener("click", () => {
    const input = $(button.dataset.passwordToggle), reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    button.setAttribute("aria-pressed", String(reveal));
    button.setAttribute("aria-label", button.getAttribute("aria-label").replace(reveal ? "Show" : "Hide", reveal ? "Hide" : "Show"));
  });
}
function authMessage(message, isError = false) {
  $("auth-message").textContent = message; $("auth-message").hidden = !message; $("auth-message").classList.toggle("error", isError);
}
function deliveryMessage(result) {
  const message = result?.message || "Request received.";
  if (result?.email_delivery === "local" && !/captured locally/i.test(message)) return `${message} Email was captured locally for development; it was not sent to your inbox. Ask the operator for the local message.`;
  if (result?.email_delivery === "unavailable" && !/unavailable|not configured|not sent/i.test(message)) return `${message} Email delivery is unavailable. Contact your operator to configure the email provider, then request a new link.`;
  return message;
}
function showAuth(view, message = "") {
  state.authView = view; $("login-screen").dataset.authView = view;
  for (const button of document.querySelectorAll("#login-screen [data-password-toggle]")) {
    $(button.dataset.passwordToggle).type = "password";
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", button.getAttribute("aria-label").replace("Hide", "Show"));
  }
  for (const name of ["login", "signup", "forgot", "resend", "reset"]) {
    $(`${name}-form`).hidden = name !== view;
    $(`${name}-error`).hidden = true;
  }
  authMessage(message);
  if (view === "signup" && $("signup-role").value === "user") void loadPublicCompanies();
}
for (const button of document.querySelectorAll("[data-auth-view]")) button.addEventListener("click", () => {
  if (state.authBusy) return;
  const email = $("email").value.trim() || $("signup-email").value.trim();
  if (email && ["forgot", "resend"].includes(button.dataset.authView)) $(`${button.dataset.authView}-email`).value = email;
  showAuth(button.dataset.authView);
});
async function submitAuth(name, action) {
  if (state.authBusy) return;
  state.authBusy = true;
  const button = $(`${name}-submit`), label = button.innerHTML;
  button.disabled = true; button.textContent = "Please wait…"; $(`${name}-error`).hidden = true; authMessage("");
  try { await action(); }
  catch (error) { $(`${name}-error`).textContent = error.message; $(`${name}-error`).hidden = false; }
  finally { state.authBusy = false; button.disabled = false; button.innerHTML = label; }
}
async function loadPublicCompanies() {
  const picker = $("signup-company"), selected = picker.value;
  try {
    const result = await api("/api/companies");
    picker.innerHTML = '<option value="">Choose your company</option>' + (result.companies || []).map(company => `<option value="${escapeHtml(company.id)}">${escapeHtml(company.name)}</option>`).join("");
    if (selected) picker.value = selected;
    if (!(result.companies || []).length) picker.innerHTML = '<option value="">No companies available yet</option>';
  } catch (error) { if ($("signup-role").value !== "user") return; picker.innerHTML = '<option value="">Companies unavailable — try again</option>'; $("signup-error").textContent = `Could not load companies: ${error.message}`; $("signup-error").hidden = false; }
}
function updateSignupRole() {
  const admin = $("signup-role").value === "company_admin";
  $("join-company-fields").hidden = admin; $("new-company-fields").hidden = !admin;
  $("signup-company").disabled = admin; $("signup-company").required = !admin;
  $("signup-company-name").disabled = !admin; $("signup-company-name").required = admin;
  $("signup-company-password").minLength = admin ? 12 : 1;
  $("signup-company-password").autocomplete = admin ? "new-password" : "off";
  $("company-password-label").textContent = admin ? "Set a company password" : "Company password";
  $("company-password-help").textContent = admin ? "Use a separate password of at least 12 characters. Share it with people who should join your new company. Each person signs in with their own personal password." : "Ask your company admin for this password. It is only used when joining; you sign in with your personal password.";
}
$("signup-role").addEventListener("change", () => { updateSignupRole(); $("signup-error").hidden = true; if ($("signup-role").value === "user") void loadPublicCompanies(); });
$("login-form").addEventListener("submit", event => {
  event.preventDefault();
  void submitAuth("login", async () => {
    state.sessionGeneration += 1;
    await api("/api/login", { method: "POST", body: JSON.stringify({ email: $("email").value.trim(), password: $("password").value }) });
    $("password").value = ""; await boot();
  });
});
$("signup-form").addEventListener("submit", event => {
  event.preventDefault();
  void submitAuth("signup", async () => {
    const role = $("signup-role").value;
    const email = $("signup-email").value.trim();
    const body = { email, display_name: $("signup-name").value.trim(), password: $("signup-password").value, role, company_password: $("signup-company-password").value, ...(role === "company_admin" ? { company_name: $("signup-company-name").value.trim() } : { company_id: $("signup-company").value }) };
    const result = await api("/api/signup", { method: "POST", body: JSON.stringify(body) });
    $("signup-form").reset(); updateSignupRole(); $("email").value = email; $("resend-email").value = email;
    showAuth("login", deliveryMessage(result));
  });
});
for (const [name, path] of [["forgot", "/api/forgot-password"], ["resend", "/api/resend-verification"]]) {
  $(`${name}-form`).addEventListener("submit", event => {
    event.preventDefault();
    void submitAuth(name, async () => {
      const result = await api(path, { method: "POST", body: JSON.stringify({ email: $(`${name}-email`).value.trim() }) });
      authMessage(deliveryMessage(result));
    });
  });
}
$("reset-form").addEventListener("submit", event => {
  event.preventDefault();
  void submitAuth("reset", async () => {
    if (!state.resetToken) throw new Error("This reset link is missing or has already been used. Request another link.");
    const result = await api("/api/reset-password", { method: "POST", body: JSON.stringify({ token: state.resetToken, password: $("reset-password").value }) });
    state.resetToken = null; $("reset-form").reset(); showAuth("login", result.message || "Your password was updated. Sign in with your new password.");
  });
});
async function loadAdminCompanies() {
  if (!state.session?.user?.is_devadmin) return;
  const generation = state.sessionGeneration;
  const result = await api("/api/admin/companies");
  if (generation !== state.sessionGeneration) return;
  state.companies = result.companies || [];
  $("admin-company").innerHTML = state.companies.map(company => `<option value="${escapeHtml(company.id)}">${escapeHtml(company.name)}</option>`).join("");
  renderSession();
}
async function switchContext(companyId, userId) {
  if (!state.session?.user?.is_devadmin || state.switchingCompany || state.creating || state.running || state.changingMode || state.savingMember || state.savingCompany) { renderSession(); return; }
  const previous = { companyId: state.companyContext, userId: state.userContext };
  state.switchingCompany = true; state.sessionGeneration += 1; resetCompanyView(); history.replaceState(null, "", "#home");
  state.companyContext = companyId; state.userContext = userId || null;
  $("create-loop").disabled = true; renderSession();
  try {
    if (!await loadSession()) return;
    await Promise.all([loadLoops(), loadMembers()]);
  } catch (error) {
    state.companyContext = previous.companyId; state.userContext = previous.userId;
    if (state.session) {
      try { await loadSession(); await Promise.all([loadLoops(), loadMembers()]); } catch { /* Keep the desktop closed when recovery fails. */ }
      notice(`Could not change administration context: ${error.message}`, true);
    }
  } finally {
    state.switchingCompany = false; $("create-loop").disabled = false;
    if (state.session) { renderSession(); renderNavigation(); }
  }
}
$("admin-company").addEventListener("change", () => void switchContext($("admin-company").value, null));
$("admin-user").addEventListener("change", () => void switchContext(state.session?.company?.id, $("admin-user").value));
$("admin-home").addEventListener("click", () => void switchContext(state.session?.home_company_id, state.session?.user?.id));
async function loadMembers() {
  if (!canManageCompany()) return;
  const generation = state.sessionGeneration;
  try {
    const response = await api("/api/company/users");
    if (generation !== state.sessionGeneration) return;
    state.members = response.users || []; renderMembers();
    $("admin-user").innerHTML = '<option value="">Choose a person</option>' + state.members.map(member => `<option value="${escapeHtml(member.user_id)}">${escapeHtml(personName(member))} · ${escapeHtml(member.email)}${member.worker ? "" : " · no desktop"}</option>`).join("");
    renderSession();
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { $("member-error").textContent = error.message; $("member-error").hidden = false; } }
}
function renderMembers() {
  $("member-count").textContent = `${state.members.length} ${state.members.length === 1 ? "person" : "people"}`;
  $("member-list").innerHTML = state.members.length ? `<div class="member-table-wrap"><table class="member-table"><thead><tr><th>Person</th><th>Role / access</th><th>Personal desktop</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${state.members.map(member => {
    const self = String(member.user_id) === String(state.session?.user?.id), disabled = member.status === "disabled", frozen = state.savingMember || state.switchingCompany;
    return `<tr><td><strong>${escapeHtml(personName(member))}</strong><span>${escapeHtml(member.email)}${self ? " · You" : ""}</span></td><td>${self ? escapeHtml(humanize(member.role)) : `<select class="member-role" data-role-user="${escapeHtml(member.user_id)}" aria-label="Role for ${escapeHtml(personName(member))}" ${frozen ? "disabled" : ""}><option value="user" ${member.role === "user" ? "selected" : ""}>User</option><option value="company_admin" ${member.role === "company_admin" ? "selected" : ""}>Company admin</option></select>`}<span>${escapeHtml(member.status === "pending" ? "Email verification pending" : humanize(member.status))}</span></td><td>${member.worker ? `${escapeHtml(member.worker.id)}<span>${escapeHtml(humanize(member.worker.status))}</span>` : '<span>No desktop assigned</span>'}</td><td>${!self ? `<button type="button" class="button quiet small" data-member-user="${escapeHtml(member.user_id)}" data-member-active="${disabled}" ${frozen ? "disabled" : ""}>${disabled ? "Restore access" : "Disable access"}</button>` : ""}</td></tr>`;
  }).join("")}</tbody></table></div>` : '<p class="members-note">No people returned.</p>';
}
$("member-management").addEventListener("toggle", () => { if ($("member-management").open) void loadMembers(); });
async function updateMember(userId, body) {
  if (state.savingMember || state.switchingCompany || String(userId) === String(state.session?.user?.id)) return;
  state.savingMember = true; $("member-error").hidden = true; renderMembers(); renderSession();
  const generation = state.sessionGeneration;
  try {
    await api(`/api/company/users/${encodeURIComponent(userId)}`, { method: "PATCH", body: JSON.stringify(body) });
    if (generation === state.sessionGeneration) {
      if (body.active === false && String(userId) === String(state.userContext)) { state.userContext = null; disconnectViewer(); }
      await loadSession(); await loadMembers();
    }
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { $("member-error").textContent = error.message; $("member-error").hidden = false; } }
  finally { if (generation === state.sessionGeneration) { state.savingMember = false; if (state.session) { renderMembers(); renderSession(); } } }
}
$("member-list").addEventListener("click", event => {
  const button = event.target.closest("[data-member-user]");
  if (button) void updateMember(button.dataset.memberUser, { active: button.dataset.memberActive === "true" });
});
$("member-list").addEventListener("change", event => {
  const picker = event.target.closest("[data-role-user]");
  if (picker) void updateMember(picker.dataset.roleUser, { role: picker.value });
});
$("company-form").addEventListener("submit", async event => {
  event.preventDefault(); if (state.savingCompany || state.switchingCompany) return;
  state.savingCompany = true; $("save-company").disabled = true; $("company-error").hidden = true; renderSession();
  const generation = state.sessionGeneration;
  try {
    const body = { name: $("company-setting-name").value.trim(), current_password: $("company-current-password").value };
    if ($("company-setting-password").value) body.company_password = $("company-setting-password").value;
    await api("/api/company", { method: "PATCH", body: JSON.stringify(body) });
    if (generation !== state.sessionGeneration) return;
    $("company-current-password").value = ""; $("company-setting-password").value = ""; delete $("company-form").dataset.companyId;
    await loadSession(); await loadAdminCompanies(); notice("Company settings saved. Existing members and their personal passwords are unchanged.");
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { $("company-error").textContent = error.message; $("company-error").hidden = false; } }
  finally { if (generation === state.sessionGeneration) { state.savingCompany = false; $("save-company").disabled = false; if (state.session) renderSession(); } }
});
$("logout").addEventListener("click", async () => {
  if (state.signingOut) return;
  state.signingOut = true;
  state.sessionGeneration += 1; disconnectViewer();
  viewerPlaceholder("Desktop disconnected", "Signing out of this company…", false);
  try { await api("/api/logout", { method: "POST" }); state.session = null; state.detail = null; state.loops = []; state.selectedId = null; state.lastListSignature = ""; state.lastDetailSignature = ""; showLogin(); $("login-error").hidden = true; }
  catch (error) { notice(`Sign out failed: ${error.message}`, true); viewerPlaceholder("Desktop disconnected", "Select Reconnect to resume viewing if you stay signed in."); }
  finally { state.signingOut = false; }
});
$("compose-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const prompt = $("prompt").value.trim();
  if (!prompt || state.creating || state.switchingCompany || $("create-loop").disabled) return;
  const generation = state.sessionGeneration, start = Boolean(state.session?.worker?.id);
  state.creating = true; $("create-loop").disabled = true; $("create-loop").textContent = start ? "Starting…" : "Saving…"; $("prompt").disabled = true; $("page-notice").hidden = true;
  try {
    const loop = await api("/api/loops", { method: "POST", body: JSON.stringify({ prompt, start }) });
    if (generation !== state.sessionGeneration) return;
    state.selectedId = loop.id; state.detail = loop; state.view = "loop"; state.lastDetailSignature = "";
    state.loops = [loop, ...state.loops.filter(item => item.id !== loop.id)];
    $("prompt").value = ""; renderLoops(); renderNavigation(); renderDetail();
    await Promise.all([loadLoops(), loadDetail(loop.id), loadSession()]);
  } catch (error) {
    if (generation === state.sessionGeneration && error.status !== 401) notice(`Couldn’t ${start ? "start" : "save"} the CtrlLoop: ${error.message}`, true);
  } finally {
    if (generation === state.sessionGeneration) { state.creating = false; $("create-loop").innerHTML = 'Start CtrlLoop <span aria-hidden="true">↗</span>'; $("prompt").disabled = false; renderControl(); }
  }
});
for (const id of ["loop-list", "recent-loops"]) $(id).addEventListener("click", event => { const item = event.target.closest("[data-loop-id]"); if (item && !state.creating) void selectLoop(item.dataset.loopId); });
$("loop-detail").addEventListener("click", async (event) => {
  if (event.target.closest("#reuse-request")) { const prompt = state.detail?.prompt || ""; showHome({ focus: true }); $("prompt").value = prompt; return; }
  if (!event.target.closest("#run-instruction") || state.switchingCompany || state.running || hasActiveRun() || state.session?.worker?.mode !== "agent" || state.session?.worker?.status !== "online" || !state.detail?.instruction) return;
  const id = state.detail.id, generation = state.sessionGeneration; state.running = true; renderControl(); $("page-notice").hidden = true;
  try { await api(`/api/loops/${encodeURIComponent(id)}/runs`, { method: "POST" }); if (generation === state.sessionGeneration) await Promise.all([loadDetail(id), loadLoops(), loadSession()]); }
  catch (error) { if (generation === state.sessionGeneration && error.status !== 401) notice(`Couldn’t start this run: ${error.message}`, true); }
  finally { if (generation === state.sessionGeneration) { state.running = false; renderControl(); } }
});
$("home-desktop").addEventListener("click", () => { if (!state.creating) showDesktop(); });
$("new-loop").addEventListener("click", () => { if (!state.creating) showHome({ focus: true }); });
$("home-nav").addEventListener("click", () => { if (!state.creating) showHome(); });
$("company-nav").addEventListener("click", () => {
  if (!canManageCompany() || state.creating) return;
  state.view = "company"; state.selectedId = null; renderNavigation(); renderLoops(); $("member-management").open = true;
});
const mobileNavigation = matchMedia("(max-width: 600px)");
function setSidebarOpen(open) {
  $("app").classList.toggle("nav-open", open);
  $("toggle-sidebar").setAttribute("aria-expanded", String(open));
  document.querySelector(".sidebar").inert = mobileNavigation.matches && !open;
}
mobileNavigation.addEventListener("change", () => setSidebarOpen(false));
setSidebarOpen(false);
$("toggle-sidebar").addEventListener("click", () => setSidebarOpen(!$("app").classList.contains("nav-open")));
document.addEventListener("keydown", event => { if (event.key === "Escape" && $("app").classList.contains("nav-open")) { setSidebarOpen(false); $("toggle-sidebar").focus(); } });
$("prompt").addEventListener("keydown", event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("compose-form").requestSubmit(); } });
$("main-content").addEventListener("click", () => { setSidebarOpen(false); });
window.addEventListener("popstate", () => { if (state.session?.authenticated && !state.creating) void openRoute(); });
document.querySelector(".sidebar-brand").addEventListener("click", event => { event.preventDefault(); if (!state.creating) showHome(); });

async function changeDesktopMode(mode) {
  const worker = state.session?.worker;
  if (state.changingMode || state.controlUnconfirmed || state.switchingCompany || state.signingOut || state.running || hasActiveRun() || !worker?.id) return;
  if (mode === "human" && (worker.mode !== "agent" || !state.rfb || state.viewerConnecting || !desktopIsVisible())) return;
  if (mode === "agent" && (worker.mode !== "human" || !worker.can_control)) return;
  const generation = state.sessionGeneration, rfb = state.rfb;
  state.changingMode = true; renderControl();
  try {
    await api("/api/worker/mode", { method: "POST", body: JSON.stringify({ mode }) });
    if (generation === state.sessionGeneration) await loadSession();
  } catch (error) {
    if (generation === state.sessionGeneration && error.status !== 401) {
      notice(error.message, true);
      try { await loadSession(); } catch { if (generation === state.sessionGeneration) state.controlUnconfirmed = true; }
    }
  } finally {
    if (generation === state.sessionGeneration) {
      const focusDesktop = document.activeElement === $("desktop-takeover");
      state.changingMode = false; renderControl();
      if (mode === "human" && focusDesktop && rfb && state.rfb === rfb && !rfb.viewOnly && desktopIsVisible() && !document.hidden) rfb.focus({ preventScroll: true });
    }
  }
}
// The first completed click only acquires ownership. It never reaches the guest.
$("desktop-takeover").addEventListener("click", () => void changeDesktopMode("human"));
$("toggle-control").addEventListener("click", () => void changeDesktopMode("agent"));
function updateFullscreenLabel() {
  const text = document.fullscreenElement || $("desktop-panel").classList.contains("expanded") ? "Exit fullscreen" : "Enter fullscreen";
  $("fullscreen").title = text; $("fullscreen").setAttribute("aria-label", text);
}
function setExpandedDesktop(expanded) {
  $("desktop-panel").classList.toggle("expanded", expanded);
  document.body.classList.toggle("desktop-expanded", expanded);
  updateFullscreenLabel();
}
$("fullscreen").addEventListener("click", async () => {
  if ($("desktop-panel").classList.contains("expanded")) { setExpandedDesktop(false); return; }
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $("desktop-panel").requestFullscreen();
  } catch {
    // Embedded browsers may disallow native fullscreen; still fill the app viewport.
    if (desktopIsVisible()) setExpandedDesktop(true);
  }
});
document.addEventListener("fullscreenchange", updateFullscreenLabel);
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("desktop-panel").classList.contains("expanded")) {
    event.preventDefault(); event.stopPropagation(); setExpandedDesktop(false); $("fullscreen").focus();
  }
}, true);
$("reconnect-viewer").addEventListener("click", () => { void connectViewer({ manual: true }); });
$("dismiss-notice").addEventListener("click", () => { $("page-notice").hidden = true; });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearViewerTimer("viewerRetryTimer");
    if (state.viewerConnecting) {
      // Hiding the tab pauses an attempt; it must not exhaust the last retry.
      state.viewerRetryCount = Math.max(0, state.viewerRetryCount - 1);
      closeViewerConnection();
    }
  } else { void refresh(); }
  scheduleViewerReconnect();
});
document.addEventListener("keydown", (event) => { if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.closest("input, textarea, select, [contenteditable], .desktop-display") && state.session?.authenticated) { event.preventDefault(); $("new-loop").click(); } });
window.addEventListener("beforeunload", disconnectViewer);
setInterval(refresh, 2000);
setInterval(updateDurations, 1000);
function handleAuthFragment() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const verificationToken = fragment.get("verify"), resetToken = fragment.get("reset");
  if (!verificationToken && !resetToken) return false;
  history.replaceState(null, "", location.pathname + location.search);
  showLogin(); state.resetToken = resetToken;
  if (resetToken) { state.authBusy = false; showAuth("reset"); return true; }
  showAuth("login", "Verifying your email…");
  const generation = state.sessionGeneration;
  state.authBusy = true; $("login-submit").disabled = true;
  api("/api/verify-email", { method: "POST", body: JSON.stringify({ token: verificationToken }) })
    .then(result => { if (generation === state.sessionGeneration) showAuth("login", result.message || "Your email is verified. Sign in to continue."); })
    .catch(error => { if (generation === state.sessionGeneration) { showAuth("resend"); authMessage(error.message, true); } })
    .finally(() => { if (generation === state.sessionGeneration) { state.authBusy = false; $("login-submit").disabled = false; } });
  return true;
}
window.addEventListener("hashchange", handleAuthFragment);
if (!handleAuthFragment()) boot();
