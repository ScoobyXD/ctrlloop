const $ = (id) => document.getElementById(id);
const samplePrompt = "Click the search field in the current application";
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
Object.assign(state, { switchingWorkspace: false, members: [], savingMember: false, inspectedToken: null, activationBusy: false });
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
function hasActiveRun() { return Boolean(state.session?.worker?.active_run) || getRuns().some((run) => run.worker_id === state.session?.worker?.id && activeStatuses.has(run.status)); }
function personName(person) { return person?.display_name || person?.username || "Earlier workspace member"; }
function clearActivationReceipt() { $("activation-receipt").hidden = true; $("member-activation-code").value = ""; $("activation-expiry").textContent = ""; $("create-member").disabled = state.savingMember; }
function resetWorkspaceView() {
  disconnectViewer(); state.loops = []; state.detail = null; state.selectedId = null; state.members = [];
  state.creating = false; state.running = false; state.changingMode = false; state.savingMember = false; state.viewerFailure = "";
  $("create-loop").disabled = state.switchingWorkspace; $("create-loop").innerHTML = '<span>Create loop</span><span aria-hidden="true">↗</span>'; $("prompt").disabled = false;
  state.lastListSignature = ""; state.lastDetailSignature = "";
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
function setConnection(connected) { $("connection-state").innerHTML = `<i class="status-dot ${connected ? "live" : "warn"}"></i>${connected ? "Workspace connected" : "Connection interrupted"}`; }

function showLogin() {
  const wasAuthenticated = state.session?.authenticated;
  state.sessionGeneration += 1;
  state.session = null;
  resetWorkspaceView();
  $("app").hidden = true; $("login-screen").hidden = false;
  if (wasAuthenticated) { $("login-error").textContent = "Your session ended. Sign in to continue."; $("login-error").hidden = false; }
}
function adoptSession(session) {
  if (!session?.authenticated) { showLogin(); return false; }
  const previous = state.session;
  if (previous?.authenticated && (previous.user?.id !== session.user?.id || previous.workspace?.id !== session.workspace?.id || previous.worker?.id !== session.worker?.id)) {
    // A session check from any caller must invalidate old workspace requests.
    state.sessionGeneration += 1; resetWorkspaceView();
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
  const workspace = state.session?.workspace;
  const memberships = state.session?.memberships || [];
  const model = getModel();
  $("user-name").textContent = personName(user);
  $("user-name").title = user?.username ? `@${user.username}` : "";
  $("user-role").textContent = workspace?.role ? humanize(workspace.role) : "";
  $("workspace-name").textContent = workspace?.name || "No workspace";
  $("workspace-name").hidden = memberships.length > 1;
  const picker = $("workspace-picker");
  const options = memberships.map(item => `<option value="${escapeHtml(item.workspace_id)}">${escapeHtml(item.name)}</option>`).join("");
  if (picker.dataset.options !== options) { picker.innerHTML = options; picker.dataset.options = options; }
  picker.value = workspace?.id || ""; picker.hidden = memberships.length <= 1;
  picker.disabled = state.switchingWorkspace || state.creating || state.running || state.changingMode || state.savingMember;
  $("member-management").hidden = workspace?.role !== "owner";
  if (workspace?.role !== "owner") { $("member-management").open = false; clearActivationReceipt(); }
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
    viewerPlaceholder("Your desktop is not assigned yet", "You can create and browse shared workflows now. Your workspace owner will arrange a desktop before you can run them.", false);
  }
  renderControl();
}
function renderControl() {
  const human = state.session?.worker?.mode === "human";
  const maintenance = state.session?.worker?.mode === "maintenance";
  const agent = state.session?.worker?.mode === "agent";
  const canControl = state.session?.worker?.can_control === true;
  const active = hasActiveRun();
  const disabled = !state.session?.worker?.id || (!human && !agent) || state.changingMode || state.switchingWorkspace || active || (human && !canControl);
  $("control-mode-label").textContent = maintenance ? "Deployment in progress" : canControl ? "You have control" : "View only";
  $("control-mode-description").textContent = !state.session?.worker ? "Waiting for a desktop to be assigned to you." : maintenance ? "Runs and control will resume after worker deployment." : canControl ? "Return control when you're ready to run a loop." : human ? "Another session of your account controls this desktop." : active ? "The agent is running. Takeover is locked." : "The agent controls your desktop.";
  $("control-mode-icon").textContent = canControl ? "↖" : "◉";
  $("control-mode-icon").parentElement.classList.toggle("human", canControl);
  $("toggle-control").textContent = state.changingMode ? "Switching…" : maintenance ? "Deployment in progress" : canControl ? "Return to agent ↗" : human ? "Control unavailable" : "Take control ↗";
  $("toggle-control").disabled = disabled;
  if (state.rfb) { state.rfb.viewOnly = !human || !canControl || active; state.rfb.focusOnClick = human && canControl && !active; }
  const runButton = $("run-instruction");
  if (runButton) {
    runButton.disabled = state.switchingWorkspace || state.running || active || !agent || !state.detail?.instruction || state.detail?.status !== "ready" || state.session?.worker?.status !== "online";
    runButton.textContent = state.running ? "Starting…" : active ? "Run in progress" : getRuns().length ? "Run again ↗" : "Run instruction ↗";
  }
  const hint = $("run-control-hint");
  if (hint) { hint.textContent = human ? canControl ? "Return control to the agent to run this instruction." : "Your other session must return desktop control before a run can start." : !state.session?.worker?.id ? "Desktop provisioning pending. You can run this shared workflow once a desktop is assigned to you." : state.session?.worker?.status !== "online" ? "Waiting for your assigned worker to come online." : state.detail?.status === "generating" ? "Waiting for the model to generate and save an instruction." : "This shared instruction will run on your own assigned desktop."; }
  if (hint && maintenance) hint.textContent = "Worker deployment in progress. Run and control access will resume when deployment completes.";
}

async function loadLoops() {
  const generation = state.sessionGeneration;
  const response = await api("/api/loops");
  if (generation !== state.sessionGeneration || !state.session) return;
  state.loops = Array.isArray(response) ? response : response?.loops || [];
  renderLoops();
}
function renderLoops() {
  const signature = JSON.stringify([state.loops, state.selectedId]);
  if (signature === state.lastListSignature) return;
  state.lastListSignature = signature;
  $("loop-count").textContent = String(state.loops.length);
  if (!state.loops.length) { $("loop-list").innerHTML = '<p class="list-empty">Shared workflows live here.<br>Create one for your workspace.</p>'; return; }
  $("loop-list").innerHTML = state.loops.map((loop) => `<button class="loop-list-item ${String(loop.id) === String(state.selectedId) ? "selected" : ""}" data-loop-id="${escapeHtml(loop.id)}" ${String(loop.id) === String(state.selectedId) ? 'aria-current="true"' : ""}><span class="list-title">${escapeHtml(loop.prompt || "Untitled loop")}</span><span class="list-item-meta"><i class="status-dot ${errorStatuses.has(loop.status) ? "error" : activeStatuses.has(loop.status) ? "warn" : ""}"></i>${escapeHtml(humanize(loop.status))}<time title="${escapeHtml(formatDate(loop.created_at, { second: "2-digit" }))}">${escapeHtml(validDate(loop.created_at)?.toLocaleDateString(undefined, { month: "short", day: "numeric" }) || "—")}</time></span></button>`).join("");
  [...$("loop-list").children].forEach((item, index) => {
    const author = document.createElement("span"); author.className = "list-author";
    author.textContent = `By ${personName(state.loops[index].creator)}`; item.append(author);
  });
}
async function selectLoop(id) {
  state.selectedId = id; state.lastDetailSignature = ""; renderLoops();
  try { await loadDetail(id); } catch (error) { if (error.status !== 401) notice(`Couldn't load this loop: ${error.message}`, true); }
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
  const openKeys = new Set([...$("loop-detail").querySelectorAll("details[open]")].map((item) => item.dataset.eventKey));
  const runs = [...getRuns()].sort((a, b) => (validDate(b.created_at)?.getTime() || 0) - (validDate(a.created_at)?.getTime() || 0));
  const loopEvents = Array.isArray(loop.events) ? loop.events : [];
  const model = typeof loop.model === "object" ? loop.model?.id || loop.model?.model : loop.model;
  $("loop-detail").innerHTML = `<div class="detail-heading"><h2>Saved loop</h2>${badge(loop.status)}</div><div class="request-block"><div class="content-label"><span>YOUR REQUEST</span><time datetime="${escapeHtml(loop.created_at || "")}" title="${escapeHtml(formatDate(loop.created_at, { second: "2-digit", timeZoneName: "short" }))}">${escapeHtml(formatDate(loop.created_at))}</time></div><p class="request-text">${escapeHtml(loop.prompt)}</p></div><div class="instruction-block"><div class="content-label"><span>GENERATED INSTRUCTION</span><span>${loop.instruction ? "SAVED" : "PENDING"}</span></div>${loop.instruction ? `<p class="instruction-text">${escapeHtml(loop.instruction)}</p>` : '<p class="instruction-missing">No generated instruction has been saved.</p>'}${model ? `<div class="instruction-meta">${escapeHtml(model)}</div>` : ""}${loop.error ? `<div class="inline-error">${escapeHtml(valueText(loop.error))}</div>` : ""}<div class="instruction-actions"><button id="run-instruction" class="button primary" ${!loop.instruction ? "disabled" : ""}>Run instruction ↗</button><button id="reuse-request" class="button quiet small">Edit request</button></div><p class="run-hint" id="run-control-hint"></p></div><div class="activity-header"><h3>Activity</h3><span>${runs.length} ${runs.length === 1 ? "run" : "runs"} · ${Intl.DateTimeFormat().resolvedOptions().timeZone}</span></div>${loopEvents.length ? `<div class="run-group"><div class="run-summary"><span>Request & instruction</span></div><ol class="timeline">${loopEvents.map((event, index) => renderEvent(event, index, `loop-${loop.id}`)).join("")}</ol></div>` : ""}${runs.map((run, index) => renderRun(run, index, runs.length)).join("")}${!runs.length ? '<p class="no-events">No runs yet. Run the saved instruction to follow delivery, Jev selection, native actions, and the observed result.</p>' : ""}`;
  for (const element of $("loop-detail").querySelectorAll("details[data-event-key]")) element.open = openKeys.has(element.dataset.eventKey);
  $("loop-detail").querySelector(".content-label > span").textContent = "SHARED REQUEST";
  const attribution = document.createElement("p"); attribution.className = "workflow-attribution";
  attribution.textContent = `Created by ${personName(loop.creator)}`;
  $("loop-detail").querySelector(".request-block").append(attribution);
  const requestLinks = document.createElement("div");
  requestLinks.className = "instruction-actions";
  requestLinks.innerHTML = `<a class="button quiet small" href="/reports/loops/${encodeURIComponent(loop.id)}" target="_blank" rel="noopener">Request & model report ↗</a><a class="button quiet small" href="/api/loops/${encodeURIComponent(loop.id)}/download">Request evidence ZIP ↓</a>`;
  $("loop-detail").querySelector(".instruction-block").append(requestLinks);
  updateDurations(); renderControl();
}
function updateDurations() {
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
  if (!state.viewerWanted || !state.session?.authenticated || !state.session?.worker?.id || state.switchingWorkspace || state.signingOut || state.rfb || state.viewerConnecting || state.viewerRetryTimer !== null) return;
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
  if (state.signingOut || state.switchingWorkspace || !state.session?.worker?.id) return;
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
      if (session.worker?.id) void connectViewer({ manual: true });
      return;
    }
    if (!session.worker?.id) return;
    viewerModule ||= import("/vendor/novnc/core/rfb.js");
    const { default: RFB } = await viewerModule;
    if (!current()) return;
    const url = new URL("/api/desktop", window.location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const rfb = new RFB($("desktop-canvas"), url.toString(), { shared: true });
    state.rfb = rfb; rfb.viewOnly = state.session?.worker?.mode !== "human" || state.session?.worker?.can_control !== true || hasActiveRun(); rfb.focusOnClick = !rfb.viewOnly; rfb.scaleViewport = state.fit; rfb.resizeSession = false; rfb.background = "#0c100b";
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
    rfb.addEventListener("credentialsrequired", () => { if (!current() || state.rfb !== rfb) return; viewerInterrupted("Desktop authentication required", "The desktop gateway needs server-side credentials. Workspace credentials are never sent to the desktop.", false); });
  } catch (error) {
    if (!current()) return;
    viewerModule = null;
    viewerInterrupted("Viewer unavailable", `The desktop connection could not open: ${error.message}`);
  }
}

async function refresh() {
  if (state.pollBusy || state.creating || state.signingOut || state.switchingWorkspace || document.hidden || !state.session?.authenticated) return;
  state.pollBusy = true;
  try {
    if (!await loadSession()) return;
    await Promise.all([loadLoops(), state.selectedId ? loadDetail() : Promise.resolve()]);
    if (state.session?.worker?.id && !state.viewerWanted && !state.viewerFailure) void connectViewer({ manual: true });
  } catch (error) { if (error.status !== 401) setConnection(false); }
  finally { state.pollBusy = false; }
}
async function boot() {
  const generation = state.sessionGeneration;
  try {
    if (!await loadSession()) return;
    if (generation !== state.sessionGeneration) return;
    void connectViewer({ manual: true });
    await loadLoops();
    if (state.loops.length) await selectLoop(state.loops[0].id);
  } catch (error) { if (!state.session) { $("login-error").textContent = `Unable to connect: ${error.message}`; $("login-error").hidden = false; } else notice(error.message, true); }
}

$("login-form").addEventListener("submit", async (event) => {
  // Discard any unauthenticated session response started before this sign-in.
  state.sessionGeneration += 1;
  event.preventDefault(); $("login-submit").disabled = true; $("login-submit").textContent = "Opening workspace…"; $("login-error").hidden = true;
  try { await api("/api/login", { method: "POST", body: JSON.stringify({ username: $("username").value.trim(), password: $("password").value }) }); $("password").value = ""; await boot(); }
  catch (error) { $("login-error").textContent = error.message; $("login-error").hidden = false; }
  finally { $("login-submit").disabled = false; $("login-submit").innerHTML = '<span>Open workspace</span><span aria-hidden="true">↗</span>'; }
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
$("workspace-picker").addEventListener("change", async () => {
  const workspaceId = $("workspace-picker").value;
  if (state.switchingWorkspace || state.creating || state.running || state.changingMode || state.savingMember || workspaceId === state.session?.workspace?.id) { renderSession(); return; }
  state.switchingWorkspace = true; state.sessionGeneration += 1; resetWorkspaceView(); renderSession();
  $("create-loop").disabled = true;
  try {
    await api("/api/session/workspace", { method: "POST", body: JSON.stringify({ workspace_id: workspaceId }) });
    state.session = null; await boot();
  } catch (error) { if (error.status !== 401) notice(`Could not switch workspace: ${error.message}`, true); }
  finally {
    state.switchingWorkspace = false; $("create-loop").disabled = false;
    if (state.session) { renderSession(); void connectViewer({ manual: true }); }
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
  event.preventDefault(); if (state.savingMember || state.switchingWorkspace || !$("activation-receipt").hidden) return;
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
  if (!button || state.savingMember || state.switchingWorkspace) return;
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
  viewerPlaceholder("Desktop disconnected", "Signing out of this workspace…", false);
  try { await api("/api/logout", { method: "POST" }); state.session = null; state.detail = null; state.loops = []; state.selectedId = null; state.lastListSignature = ""; state.lastDetailSignature = ""; showLogin(); $("login-error").hidden = true; }
  catch (error) { notice(`Sign out failed: ${error.message}`, true); viewerPlaceholder("Desktop disconnected", "Select Reconnect to resume viewing if you stay signed in."); }
  finally { state.signingOut = false; }
});
$("compose-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const prompt = $("prompt").value.trim(); if (!prompt || state.creating || state.switchingWorkspace) return;
  const generation = state.sessionGeneration;
  state.creating = true; $("create-loop").disabled = true; $("create-loop").textContent = "Generating…"; $("prompt").disabled = true; $("page-notice").hidden = true;
  const previousDetail = state.detail; state.detail = null; state.selectedId = null; state.lastDetailSignature = ""; renderLoops();
  $("loop-detail").innerHTML = '<div class="pending-panel" role="status"><span class="spinner" aria-hidden="true"></span><h2>Turning intent into instruction</h2><p>Waiting for the language model. The returned instruction will be saved to your workspace.</p></div>';
  try {
    const loop = await api("/api/loops", { method: "POST", body: JSON.stringify({ prompt }) });
    if (generation !== state.sessionGeneration) return;
    state.selectedId = loop.id; state.detail = loop; renderDetail();
    await loadLoops(); await loadDetail(loop.id);
  } catch (error) { if (generation === state.sessionGeneration && error.status !== 401) { state.detail = previousDetail; state.selectedId = previousDetail?.id || null; state.lastDetailSignature = ""; if (previousDetail) renderDetail(); else $("loop-detail").innerHTML = `<div class="pending-panel"><h2>Instruction could not be created</h2><p>${escapeHtml(error.message)}</p><p>Your request is still in the editor. Try again when the issue is resolved.</p></div>`; renderLoops(); notice(error.message, true); } }
  finally { if (generation === state.sessionGeneration) { state.creating = false; $("create-loop").disabled = false; $("create-loop").innerHTML = '<span>Create loop</span><span aria-hidden="true">↗</span>'; $("prompt").disabled = false; } }
});
$("loop-list").addEventListener("click", (event) => { const item = event.target.closest("[data-loop-id]"); if (item && !state.creating) selectLoop(item.dataset.loopId); });
$("loop-detail").addEventListener("click", async (event) => {
  if (event.target.closest("#reuse-request")) { $("prompt").value = state.detail?.prompt || ""; $("prompt").focus(); $("compose-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); return; }
  if (!event.target.closest("#run-instruction") || state.switchingWorkspace || state.running || hasActiveRun() || state.session?.worker?.mode !== "agent" || state.session?.worker?.status !== "online" || !state.detail?.instruction) return;
  const id = state.detail.id, generation = state.sessionGeneration; state.running = true; renderControl(); $("page-notice").hidden = true;
  try { await api(`/api/loops/${encodeURIComponent(id)}/runs`, { method: "POST" }); if (generation === state.sessionGeneration) await Promise.all([loadDetail(id), loadLoops(), loadSession()]); }
  catch (error) { if (generation === state.sessionGeneration && error.status !== 401) notice(`Couldn't start this run: ${error.message}`, true); }
  finally { if (generation === state.sessionGeneration) { state.running = false; renderControl(); } }
});
$("new-loop").addEventListener("click", () => { $("prompt").value = samplePrompt; $("prompt").focus(); $("prompt").select(); $("compose-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); });
$("toggle-control").addEventListener("click", async () => {
  if (state.changingMode || state.switchingWorkspace || !["agent", "human"].includes(state.session?.worker?.mode)) return;
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
