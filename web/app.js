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
const state = { session: null, sessionGeneration: 0, signingOut: false, loops: [], detail: null, selectedId: null, creating: false, running: false, changingMode: false, pollBusy: false, rfb: null, viewerGeneration: 0, viewerConnecting: false, viewerWanted: false, viewerRetryCount: 0, viewerRetryTimer: null, viewerConnectTimer: null, viewerStableTimer: null, viewerSessionController: null, viewerFailure: "", fit: true, lastDetailSignature: "", lastListSignature: "" };
Object.assign(state, { view: "home", tab: "activity", switchingCompany: false, members: [], savingMember: false, inspectedToken: null, activationBusy: false });
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
function desktopIsVisible() { return ["loop", "desktop"].includes(state.view) && state.tab === "desktop"; }
function autoStartPending() { return getRuns().some(run => run.status === "queued" && run.worker_id === state.session?.worker?.id); }
function personName(person) { return person?.display_name || person?.username || "Earlier company member"; }
function clearActivationReceipt() { $("activation-receipt").hidden = true; $("member-activation-code").value = ""; $("activation-expiry").textContent = ""; $("create-member").disabled = state.savingMember; }
function resetCompanyView() {
  disconnectViewer(); state.loops = []; state.detail = null; state.selectedId = null; state.members = [];
  state.creating = false; state.running = false; state.changingMode = false; state.savingMember = false; state.viewerFailure = "";
  $("create-loop").disabled = state.switchingCompany; $("create-loop").innerHTML = 'Start CtrlLoop <span aria-hidden="true">↗</span>'; $("prompt").disabled = false;
  state.lastListSignature = ""; state.lastDetailSignature = ""; state.view = "home"; state.tab = "activity";
  renderNavigation(false);
  $("loop-detail").innerHTML = emptyDetail; $("member-list").replaceChildren(); $("member-count").textContent = "";
  $("member-management").open = false; $("member-error").hidden = true; $("page-notice").hidden = true;
  clearActivationReceipt(); renderLoops();
}
function getModel() {
  const raw = state.session?.model;
  const id = typeof raw === "string" ? raw : raw?.id || raw?.model || state.session?.llm_model;
  const effort = (typeof raw === "object" && raw?.reasoning_effort) || state.session?.reasoning_effort;
  return { id, effort, label: id ? `${id}${effort ? ` · ${effort} reasoning` : ""}` : "Model not reported" };
}

async function api(path, options = {}) {
  const sessionGeneration = state.sessionGeneration;
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) } });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    if (response.status === 401 && !["/api/login", "/api/activate", "/api/activate/inspect"].includes(path) && sessionGeneration === state.sessionGeneration) showLogin();
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
  state.session = null;
  resetCompanyView();
  $("app").hidden = true; $("login-screen").hidden = false;
  if (wasAuthenticated) { $("login-error").textContent = "Your session ended. Sign in to continue."; $("login-error").hidden = false; }
}
function adoptSession(session) {
  if (!session?.authenticated) { showLogin(); return false; }
  const previous = state.session;
  if (previous?.authenticated && (previous.user?.id !== session.user?.id || previous.workspace?.id !== session.workspace?.id || previous.worker?.id !== session.worker?.id)) {
    // A session check from any caller must invalidate old company requests.
    state.sessionGeneration += 1; resetCompanyView(); history.replaceState(null, "", "#home");
  }
  state.session = session; $("login-screen").hidden = true; $("app").hidden = false;
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
  const company = state.session?.workspace;
  const memberships = state.session?.memberships || [];
  const model = getModel();
  $("user-name").textContent = personName(user);
  $("user-initial").textContent = personName(user).slice(0, 1).toUpperCase();
  $("breadcrumb-company").textContent = company?.name || "Company";
  $("company-nav").hidden = company?.role !== "owner";
  $("user-name").title = user?.username ? `@${user.username}` : "";
  $("user-role").textContent = company?.role === "owner" ? "Company admin" : company?.role === "member" ? "User" : company?.role ? humanize(company.role) : "";
  $("company-name").textContent = company?.name || "No company";
  $("company-name").hidden = memberships.length > 1;
  const picker = $("company-picker");
  const options = memberships.map(item => `<option value="${escapeHtml(item.workspace_id)}">${escapeHtml(item.name)}</option>`).join("");
  if (picker.dataset.options !== options) { picker.innerHTML = options; picker.dataset.options = options; }
  picker.value = company?.id || ""; picker.hidden = memberships.length <= 1;
  picker.disabled = state.switchingCompany || state.creating || state.running || state.changingMode || state.savingMember;
  $("member-management").hidden = company?.role !== "owner";
  if (company?.role !== "owner") { $("member-management").open = false; clearActivationReceipt(); }
  $("desktop-owner").textContent = `${personName(user)}’s desktop`;
  $("model-label").textContent = model.label;
  $("model-detail-id").textContent = model.label;
  $("jev-detail-id").textContent = state.session?.jev_model || worker?.jev_model || "Not reported";
  $("worker-identity").textContent = worker?.id || "Desktop provisioning pending";
  $("worker-detail-id").textContent = worker?.id || "Not assigned yet";
  $("worker-status").textContent = worker ? `Your worker · ${humanize(worker.status)}${worker.mode === "maintenance" ? " · Worker deployment in progress" : ""}` : "No desktop assigned to your account";
  $("worker-last-seen").textContent = worker?.last_seen ? `Last seen ${formatTime(worker.last_seen)}` : "";
  $("worker-last-seen").title = worker?.last_seen ? formatDate(worker.last_seen, { second: "2-digit", timeZoneName: "short" }) : "";
  $("fit-view").disabled = !worker; $("fullscreen").disabled = !worker;
  if (!worker) {
    if (state.rfb || state.viewerConnecting || state.viewerWanted) disconnectViewer();
    setViewerStatus("Provisioning pending", "warn");
    viewerPlaceholder("Your desktop is not assigned yet", "You can browse your company’s CtrlLoops. A company admin needs to assign your desktop before you can start one.", false);
  }
  renderControl(); renderHome();
}
function renderControl() {
  const human = state.session?.worker?.mode === "human";
  const maintenance = state.session?.worker?.mode === "maintenance";
  const agent = state.session?.worker?.mode === "agent";
  const canControl = state.session?.worker?.can_control === true;
  const active = hasActiveRun();
  const disabled = !state.session?.worker?.id || (!human && !agent) || state.changingMode || state.switchingCompany || active || (human && !canControl);
  $("control-mode-label").textContent = maintenance ? "Deployment in progress" : canControl ? "You have control" : "View only";
  $("control-mode-description").textContent = !state.session?.worker ? "Waiting for a desktop to be assigned to you." : maintenance ? "Runs and control will resume after worker deployment." : canControl ? "Return control when you're ready to run a loop." : human ? "Another session of your account controls this desktop." : active ? "The agent is running. Takeover is locked." : "The agent controls your desktop.";
  $("control-mode-icon").textContent = canControl ? "↖" : "◉";
  $("control-mode-icon").parentElement.classList.toggle("human", canControl);
  $("toggle-control").textContent = state.changingMode ? "Switching…" : maintenance ? "Deployment in progress" : canControl ? "Return to agent ↗" : human ? "Control unavailable" : "Take control ↗";
  $("toggle-control").disabled = disabled;
  if (state.rfb) { state.rfb.viewOnly = !human || !canControl || active; state.rfb.focusOnClick = human && canControl && !active; }
  const startReason = !state.session?.worker?.id ? "A company admin needs to assign your desktop before you can start." : maintenance ? "Your desktop is being updated. Starting will be available when it’s ready." : human ? "Return desktop control to the agent before starting a CtrlLoop." : state.session?.worker?.status !== "online" ? "Your desktop is offline. Starting will be available when it reconnects." : active ? "Your desktop is running a CtrlLoop. You can follow its activity or start another when it finishes." : "Describe it once. We’ll prepare the instruction and start.";
  $("compose-hint").textContent = startReason;
  $("create-loop").disabled = state.creating || state.switchingCompany || active || !agent || state.session?.worker?.status !== "online";
  $("compose-worker").textContent = !state.session?.worker ? "No desktop assigned" : active ? "Desktop in use" : human ? "Manual control" : state.session.worker.status === "online" ? "Desktop ready" : "Desktop offline";
  $("compose-worker-dot").className = `status-dot ${agent && state.session?.worker?.status === "online" && !active ? "live" : "warn"}`;
  const runButton = $("run-instruction");
  if (runButton) {
    runButton.hidden = state.detail?.status === "generating" || active;
    runButton.disabled = state.switchingCompany || state.running || active || !agent || !state.detail?.instruction || state.detail?.status !== "ready" || state.session?.worker?.status !== "online";
    runButton.textContent = state.running ? "Starting…" : active ? "Run in progress" : getRuns().length ? "Run again ↗" : "Start CtrlLoop ↗";
  }
  const hint = $("run-control-hint");
  if (hint) { hint.textContent = human ? canControl ? "Return control to the agent to run this instruction." : "Your other session must return desktop control before a run can start." : !state.session?.worker?.id ? "Desktop provisioning pending. You can run this CtrlLoop once a desktop is assigned to you." : state.session?.worker?.status !== "online" ? "Waiting for your assigned worker to come online." : state.detail?.status === "generating" ? autoStartPending() ? "Preparing the instruction. Execution will start automatically." : "Preparing the saved instruction. No execution is currently queued." : "Runs use your assigned desktop. Follow the recorded activity or open the Desktop tab."; }
  if (hint && maintenance) hint.textContent = "Worker deployment in progress. Run and control access will resume when deployment completes.";
}


function renderNavigation(writeHistory = true) {
  $("home-view").hidden = state.view !== "home";
  $("loop-view").hidden = !["loop", "desktop"].includes(state.view);
  document.querySelector(".loop-tabs").hidden = state.view === "desktop";
  $("company-view").hidden = state.view !== "company";
  $("home-nav").classList.toggle("selected", state.view === "home");
  $("home-nav").toggleAttribute("aria-current", state.view === "home");
  if (state.view === "home") $("home-nav").setAttribute("aria-current", "page");
  $("company-nav").classList.toggle("selected", state.view === "company");
  $("breadcrumb-page").textContent = state.view === "loop" ? "CtrlLoop" : state.view === "desktop" ? "Desktop" : state.view === "company" ? "People & access" : "Home";
  $("activity-view").hidden = state.tab !== "activity";
  $("desktop-view").hidden = state.tab !== "desktop";
  for (const name of ["activity", "desktop"]) {
    $("tab-" + name).setAttribute("aria-selected", String(state.tab === name));
    $("tab-" + name).tabIndex = state.tab === name ? 0 : -1;
  }
  const desktopVisible = desktopIsVisible();
  if (!desktopVisible && (state.viewerWanted || state.rfb || state.viewerConnecting)) disconnectViewer();
  if (desktopVisible && state.session?.worker?.id && !state.viewerWanted && !state.signingOut) void connectViewer({ manual: true });
  if (writeHistory) {
    const route = state.view === "loop" && state.selectedId ? `#loop/${encodeURIComponent(state.selectedId)}${state.tab === "desktop" ? "/desktop" : ""}` : state.view === "desktop" ? "#desktop" : state.view === "company" ? "#company" : "#home";
    if (location.hash !== route) history.pushState(null, "", route);
  }
  setSidebarOpen(false);
}
function showHome({ focus = false, fromHistory = false } = {}) {
  state.view = "home"; state.selectedId = null; state.detail = null; state.lastDetailSignature = "";
  renderNavigation(!fromHistory); renderLoops(); renderControl();
  if (focus) { $("prompt").focus(); $("compose-panel").scrollIntoView({ behavior: "smooth", block: "center" }); }
}
function setLoopTab(tab, fromHistory = false) {
  state.tab = tab; renderNavigation(!fromHistory);
}
function showDesktop(fromHistory = false) {
  state.view = "desktop"; state.tab = "desktop"; state.selectedId = null; state.detail = null;
  $("loop-title").textContent = "Your desktop"; $("loop-reference").textContent = "COMPANY / DESKTOP";
  $("loop-heading-status").replaceChildren(); renderNavigation(!fromHistory); renderLoops(); renderControl();
}
async function openRoute() {
  if (location.hash === "#desktop") { showDesktop(true); return; }
  const route = location.hash.match(/^#loop\/([^/]+)(\/desktop)?$/);
  if (route) { await selectLoop(decodeURIComponent(route[1]), { fromHistory: true, tab: route[2] ? "desktop" : "activity" }); return; }
  if (location.hash === "#company" && state.session?.workspace?.role === "owner") {
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
async function selectLoop(id, { fromHistory = false, tab = "activity" } = {}) {
  state.selectedId = id; state.detail = null; state.view = "loop"; state.tab = tab; state.lastDetailSignature = "";
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
  const links = `<p class="workflow-attribution">Started by ${escapeHtml(personName(run.initiator))}${run.worker_id ? ` · Desktop ${escapeHtml(run.worker_id)}` : ""}</p><div class="instruction-actions"><a class="button secondary small" href="/reports/runs/${encodeURIComponent(run.id)}" target="_blank" rel="noopener">Full-page report ↗</a><a class="button quiet small" href="/api/runs/${encodeURIComponent(run.id)}/download">Download evidence ZIP ↓</a></div>`;
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
  $("loop-detail").innerHTML = `${progress}<div class="detail-grid"><aside class="request-summary"><div class="detail-heading"><h2>Request & instruction</h2><button id="reuse-request" class="button quiet small" title="Use this request in a new CtrlLoop">Reuse ↗</button></div><div class="request-block"><div class="content-label"><span>REQUEST</span></div><p class="request-text">${escapeHtml(loop.prompt)}</p><p class="workflow-attribution">Created by ${escapeHtml(personName(loop.creator))}<br>${escapeHtml(formatDate(loop.created_at))}</p></div><div class="instruction-block"><div class="content-label"><span>INSTRUCTION</span><span>${prepared ? "SAVED" : "PENDING"}</span></div>${prepared ? `<p class="instruction-text">${escapeHtml(loop.instruction)}</p>` : `<p class="instruction-missing">${generating ? autoStartPending() ? "Preparing your instruction. Execution will start automatically on your assigned desktop." : "Preparing the saved instruction. No execution is currently queued." : "An instruction could not be prepared. Your original request is saved."}</p>`}${model ? `<div class="instruction-meta">${escapeHtml(model)}</div>` : ""}${loop.error ? `<div class="inline-error">${escapeHtml(valueText(loop.error))}</div>` : ""}<div class="instruction-actions"><button id="run-instruction" class="button primary" ${!prepared ? "disabled" : ""}>Run again ↗</button><button id="open-desktop" class="button secondary small">View desktop ↗</button></div><p class="run-hint" id="run-control-hint"></p><div class="instruction-actions"><a class="button quiet small" href="/reports/loops/${encodeURIComponent(loop.id)}" target="_blank" rel="noopener">Request report ↗</a><a class="button quiet small" href="/api/loops/${encodeURIComponent(loop.id)}/download">Evidence ↓</a></div></div></aside><div class="activity-column"><div class="activity-header"><h3>Execution activity</h3><span>${runs.length} ${runs.length === 1 ? "run" : "runs"} · ${escapeHtml(Intl.DateTimeFormat().resolvedOptions().timeZone)}</span></div>${loopEvents.length ? `<section class="run-group"><div class="run-summary"><div><span>Instruction preparation</span>${generating ? badge("preparing") : ""}</div></div><ol class="timeline">${loopEvents.map((event, index) => renderEvent(event, index, `loop-${loop.id}`)).join("")}</ol></section>` : ""}${runs.map((run, index) => renderRun(run, index, runs.length)).join("")}${!runs.length ? `<p class="no-events">${generating ? "Preparing the instruction…" : "This CtrlLoop has no recorded executions yet."}</p>` : ""}</div></div>`;
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
    const url = new URL("/api/desktop", window.location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const rfb = new RFB($("desktop-canvas"), url.toString(), { shared: true });
    state.rfb = rfb; rfb.viewOnly = state.session?.worker?.mode !== "human" || state.session?.worker?.can_control !== true || hasActiveRun(); rfb.focusOnClick = !rfb.viewOnly; rfb.scaleViewport = state.fit; rfb.resizeSession = false; rfb.background = "#070707";
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
    await loadLoops();
    await openRoute();
  } catch (error) { if (!state.session) { $("login-error").textContent = `Unable to connect: ${error.message}`; $("login-error").hidden = false; } else notice(error.message, true); }
}

$("login-form").addEventListener("submit", async (event) => {
  // Discard any unauthenticated session response started before this sign-in.
  state.sessionGeneration += 1;
  event.preventDefault(); $("login-submit").disabled = true; $("login-submit").textContent = "Signing in…"; $("login-error").hidden = true;
  try { await api("/api/login", { method: "POST", body: JSON.stringify({ username: $("username").value.trim(), password: $("password").value }) }); $("password").value = ""; await boot(); }
  catch (error) { $("login-error").textContent = error.message; $("login-error").hidden = false; }
  finally { $("login-submit").disabled = false; $("login-submit").innerHTML = '<span>Sign in</span><span aria-hidden="true">↗</span>'; }
});
function showActivation(active) {
  $("login-form").hidden = active; $("activation-form").hidden = !active;
  $("toggle-activation").textContent = active ? "Already activated? Sign in" : "Have an activation code?";
  $("login-error").hidden = true; $("activation-error").hidden = true;
  if (!active) {
    $("activation-form").reset(); state.inspectedToken = null;
    $("activation-account").hidden = true; $("activation-submit").disabled = true;
  }
}
$("toggle-activation").addEventListener("click", () => { if (!state.activationBusy) showActivation($("activation-form").hidden); });
$("activation-token").addEventListener("input", () => {
  state.inspectedToken = null; $("activation-account").hidden = true; $("activation-submit").disabled = true;
});
async function inspectActivation() {
  const token = $("activation-token").value.trim();
  if (!token || state.activationBusy) return;
  state.activationBusy = true; $("inspect-activation").disabled = true; $("activation-error").hidden = true;
  state.inspectedToken = null; $("activation-submit").disabled = true;
  try {
    const result = await api("/api/activate/inspect", { method: "POST", body: JSON.stringify({ token }) });
    if (token !== $("activation-token").value.trim()) return;
    state.inspectedToken = token;
    $("activation-account").textContent = `Activate ${personName(result.user)} (@${result.user.username}) in ${(result.workspaces || []).map(item => item.name).join(", ")}. Code expires ${formatDate(result.expires_at)}.`;
    $("activation-account").hidden = false; $("activation-submit").disabled = false;
  } catch (error) { $("activation-error").textContent = error.message; $("activation-error").hidden = false; }
  finally { state.activationBusy = false; $("inspect-activation").disabled = false; }
}
$("inspect-activation").addEventListener("click", inspectActivation);
$("activation-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (state.activationBusy) return;
  const token = $("activation-token").value.trim(), password = $("activation-password").value;
  if (!token || token !== state.inspectedToken) { $("activation-error").textContent = "Review the account for this code before activating."; $("activation-error").hidden = false; return; }
  if (password !== $("activation-confirm").value) { $("activation-error").textContent = "The passwords do not match."; $("activation-error").hidden = false; return; }
  state.activationBusy = true; state.sessionGeneration += 1;
  $("activation-submit").disabled = true; $("activation-submit").textContent = "Activating…"; $("activation-error").hidden = true;
  try {
    await api("/api/activate", { method: "POST", body: JSON.stringify({ token, password }) });
    showActivation(false); await boot();
  } catch (error) { $("activation-error").textContent = error.message; $("activation-error").hidden = false; }
  finally { state.activationBusy = false; $("activation-submit").disabled = !state.inspectedToken; $("activation-submit").textContent = "Activate account ↗"; }
});
$("company-picker").addEventListener("change", async () => {
  const companyId = $("company-picker").value;
  if (state.switchingCompany || state.creating || state.running || state.changingMode || state.savingMember || companyId === state.session?.workspace?.id) { renderSession(); return; }
  state.switchingCompany = true; state.sessionGeneration += 1; resetCompanyView(); history.replaceState(null, "", "#home"); renderSession();
  $("create-loop").disabled = true;
  try {
    await api("/api/session/workspace", { method: "POST", body: JSON.stringify({ workspace_id: companyId }) });
    state.session = null; await boot();
  } catch (error) { if (error.status !== 401) notice(`Could not switch company: ${error.message}`, true); }
  finally {
    state.switchingCompany = false; $("create-loop").disabled = false;
    if (state.session) { renderSession(); renderNavigation(); }
  }
});
async function loadMembers() {
  if (state.session?.workspace?.role !== "owner") return;
  const generation = state.sessionGeneration;
  try {
    const response = await api("/api/memberships");
    if (generation !== state.sessionGeneration) return;
    state.members = response.members || []; renderMembers();
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { $("member-error").textContent = error.message; $("member-error").hidden = false; } }
}
function renderMembers() {
  $("member-count").textContent = `${state.members.length} ${state.members.length === 1 ? "person" : "people"}`;
  $("member-list").innerHTML = state.members.length ? `<div class="member-table-wrap"><table class="member-table"><thead><tr><th>Person</th><th>Membership</th><th>Personal desktop</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${state.members.map(member => `<tr><td><strong>${escapeHtml(personName(member))}</strong><span>@${escapeHtml(member.username)}${member.user_id === state.session?.user?.id ? " · You" : ""}</span></td><td>${escapeHtml(humanize(member.role))}<span>${escapeHtml(humanize(member.status))}</span></td><td>${member.worker ? `${escapeHtml(member.worker.id)}<span>${escapeHtml(humanize(member.worker.status))}</span>` : '<span>Provisioning pending</span>'}</td><td>${member.user_id !== state.session?.user?.id && member.role !== "owner" ? `<button type="button" class="button quiet small" data-membership-user="${escapeHtml(member.user_id)}" data-membership-active="${member.status === "revoked"}" ${state.savingMember ? "disabled" : ""}>${member.status === "revoked" ? "Restore access" : "Revoke access"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : '<p class="members-note">No members returned.</p>';
}
$("member-management").addEventListener("toggle", () => { if ($("member-management").open) void loadMembers(); });
$("member-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (state.savingMember || state.switchingCompany || !$("activation-receipt").hidden) return;
  state.savingMember = true; $("create-member").disabled = true; $("member-error").hidden = true; renderSession();
  const generation = state.sessionGeneration;
  try {
    const result = await api("/api/memberships", { method: "POST", body: JSON.stringify({ username: $("member-username").value.trim(), display_name: $("member-display-name").value.trim() }) });
    if (generation !== state.sessionGeneration) return;
    $("member-form").reset(); clearActivationReceipt();
    if (result.activation_token) {
      $("activation-receipt-title").textContent = `Activation for ${personName(result.member)}`;
      $("member-activation-code").value = result.activation_token;
      $("activation-expiry").textContent = `Expires ${formatDate(result.activation_expires_at)}. Open this site and choose “Have an activation code?” to use it.`;
      $("copy-activation").textContent = "Copy code"; $("activation-receipt").hidden = false;
    } else notice("Membership added. This person can sign in with their existing username and password.");
    await loadMembers();
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { $("member-error").textContent = error.message; $("member-error").hidden = false; } }
  finally { if (generation === state.sessionGeneration) { state.savingMember = false; $("create-member").disabled = !$("activation-receipt").hidden; if (state.session) { renderSession(); renderMembers(); } } }
});
$("member-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-membership-user]");
  if (!button || state.savingMember || state.switchingCompany) return;
  state.savingMember = true; $("member-error").hidden = true; renderMembers(); renderSession();
  const generation = state.sessionGeneration;
  try {
    await api(`/api/memberships/${encodeURIComponent(button.dataset.membershipUser)}`, { method: "PATCH", body: JSON.stringify({ active: button.dataset.membershipActive === "true" }) });
    if (generation === state.sessionGeneration) await loadMembers();
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { $("member-error").textContent = error.message; $("member-error").hidden = false; } }
  finally { if (generation === state.sessionGeneration) { state.savingMember = false; if (state.session) { renderMembers(); renderSession(); } } }
});
$("copy-activation").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("member-activation-code").value); $("copy-activation").textContent = "Copied"; }
  catch { $("member-activation-code").focus(); $("member-activation-code").select(); $("copy-activation").textContent = "Select and copy"; }
});
$("dismiss-activation").addEventListener("click", clearActivationReceipt);
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
  const generation = state.sessionGeneration;
  state.creating = true; $("create-loop").disabled = true; $("create-loop").textContent = "Starting…"; $("prompt").disabled = true; $("page-notice").hidden = true;
  try {
    const loop = await api("/api/loops", { method: "POST", body: JSON.stringify({ prompt, start: true }) });
    if (generation !== state.sessionGeneration) return;
    state.selectedId = loop.id; state.detail = loop; state.view = "loop"; state.tab = "activity"; state.lastDetailSignature = "";
    state.loops = [loop, ...state.loops.filter(item => item.id !== loop.id)];
    $("prompt").value = ""; renderLoops(); renderNavigation(); renderDetail();
    await Promise.all([loadLoops(), loadDetail(loop.id), loadSession()]);
  } catch (error) {
    if (generation === state.sessionGeneration && error.status !== 401) notice(`Couldn’t start the CtrlLoop: ${error.message}`, true);
  } finally {
    if (generation === state.sessionGeneration) { state.creating = false; $("create-loop").innerHTML = 'Start CtrlLoop <span aria-hidden="true">↗</span>'; $("prompt").disabled = false; renderControl(); }
  }
});
for (const id of ["loop-list", "recent-loops"]) $(id).addEventListener("click", event => { const item = event.target.closest("[data-loop-id]"); if (item && !state.creating) void selectLoop(item.dataset.loopId); });
$("loop-detail").addEventListener("click", async (event) => {
  if (event.target.closest("#open-desktop")) { setLoopTab("desktop"); return; }
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
  if (state.session?.workspace?.role !== "owner" || state.creating) return;
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
for (const name of ["activity", "desktop"]) $("tab-" + name).addEventListener("click", () => setLoopTab(name));
document.querySelector(".loop-tabs").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault(); const name = event.key === "Home" ? "activity" : event.key === "End" ? "desktop" : state.tab === "activity" ? "desktop" : "activity";
  setLoopTab(name); $("tab-" + name).focus();
});
window.addEventListener("popstate", () => { if (state.session?.authenticated && !state.creating) void openRoute(); });
document.querySelector(".sidebar-brand").addEventListener("click", event => { event.preventDefault(); if (!state.creating) showHome(); });

$("toggle-control").addEventListener("click", async () => {
  if (state.changingMode || state.switchingCompany || !["agent", "human"].includes(state.session?.worker?.mode)) return;
  const generation = state.sessionGeneration;
  state.changingMode = true; renderControl();
  try { await api("/api/worker/mode", { method: "POST", body: JSON.stringify({ mode: state.session?.worker?.mode === "human" ? "agent" : "human" }) }); if (generation === state.sessionGeneration) await loadSession(); }
  catch (error) { if (generation === state.sessionGeneration && error.status !== 401) notice(error.message, true); }
  finally { if (generation === state.sessionGeneration) { state.changingMode = false; renderControl(); } }
});
$("fit-view").addEventListener("click", () => { state.fit = !state.fit; if (state.rfb) state.rfb.scaleViewport = state.fit; $("fit-view").setAttribute("aria-pressed", String(state.fit)); $("fit-view").classList.toggle("active", state.fit); $("fit-view").textContent = state.fit ? "Fit" : "1:1"; $("fit-view").title = state.fit ? "Show desktop at actual size" : "Fit desktop to viewer"; });
$("fullscreen").addEventListener("click", async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await $("desktop-panel").requestFullscreen(); } catch (error) { notice(`Fullscreen is unavailable: ${error.message}`); } });
document.addEventListener("fullscreenchange", () => { const text = document.fullscreenElement ? "Exit fullscreen" : "Enter fullscreen"; $("fullscreen").title = text; $("fullscreen").setAttribute("aria-label", text); });
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
document.addEventListener("keydown", (event) => { if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.closest("input, textarea, [contenteditable], #desktop-canvas") && state.session?.authenticated) { event.preventDefault(); $("new-loop").click(); } });
window.addEventListener("beforeunload", disconnectViewer);
setInterval(refresh, 2000);
setInterval(updateDurations, 1000);
const activationFragment = new URLSearchParams(location.hash.slice(1)).get("activate");
if (activationFragment) {
  history.replaceState(null, "", location.pathname + location.search);
  showActivation(true); $("activation-token").value = activationFragment; void inspectActivation();
} else boot();
