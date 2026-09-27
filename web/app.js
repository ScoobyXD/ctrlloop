const $ = (id) => document.getElementById(id);
const samplePrompt = "I want to see the latest posts on X";
const activeStatuses = new Set(["queued", "pending", "delivering", "delivered", "acknowledged", "running", "processing", "selecting", "executing", "verifying"]);
const errorStatuses = new Set(["error", "failed", "blocked", "rejected", "cancelled", "canceled", "expired", "interrupted"]);
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
const state = { session: null, loops: [], detail: null, selectedId: null, creating: false, running: false, changingMode: false, pollBusy: false, rfb: null, viewerGeneration: 0, viewerConnecting: false, fit: true, lastDetailSignature: "", lastListSignature: "" };
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
function hasActiveRun() { return Boolean(state.session?.worker?.active_run) || getRuns().some((run) => activeStatuses.has(run.status)); }
function getModel() {
  const raw = state.session?.model;
  const id = typeof raw === "string" ? raw : raw?.id || raw?.model || state.session?.llm_model;
  const effort = (typeof raw === "object" && raw?.reasoning_effort) || state.session?.reasoning_effort;
  return { id, effort, label: id ? `${id}${effort ? ` · ${effort} reasoning` : ""}` : "Model not reported" };
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) } });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    const detail = body?.detail ?? body?.error ?? body?.message;
    const message = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `Request failed (${response.status}).`;
    const error = new Error(message); error.status = response.status; throw error;
  }
  return body;
}
function notice(message, isError = false) { $("page-notice-text").textContent = message; $("page-notice").hidden = false; $("page-notice").classList.toggle("error", isError); }
function setConnection(connected) { $("connection-state").innerHTML = `<i class="status-dot ${connected ? "live" : "warn"}"></i>${connected ? "Workspace connected" : "Connection interrupted"}`; }

function showLogin() {
  const wasAuthenticated = state.session?.authenticated;
  state.session = null;
  disconnectViewer();
  $("app").hidden = true; $("login-screen").hidden = false;
  if (wasAuthenticated) { $("login-error").textContent = "Your session ended. Sign in to continue."; $("login-error").hidden = false; }
}
async function loadSession() {
  const session = await api("/api/session");
  if (!session?.authenticated) { showLogin(); return false; }
  state.session = session; $("login-screen").hidden = true; $("app").hidden = false;
  renderSession(); setConnection(true); return true;
}
function renderSession() {
  const worker = state.session?.worker;
  const model = getModel();
  $("model-label").textContent = model.label;
  $("model-detail-id").textContent = model.label;
  $("jev-detail-id").textContent = state.session?.jev_model || worker?.jev_model || "Not reported";
  $("worker-identity").textContent = worker?.id || "No worker assigned";
  $("worker-detail-id").textContent = worker?.id || "No worker assigned";
  $("worker-status").textContent = worker ? `Worker · ${humanize(worker.status)}` : "No worker assigned";
  $("worker-last-seen").textContent = worker?.last_seen ? `Last seen ${formatTime(worker.last_seen)}` : "";
  $("worker-last-seen").title = worker?.last_seen ? formatDate(worker.last_seen, { second: "2-digit", timeZoneName: "short" }) : "";
  renderControl();
}
function renderControl() {
  const human = state.session?.worker?.mode === "human";
  const canControl = state.session?.worker?.can_control === true;
  const active = hasActiveRun();
  const disabled = !state.session?.worker?.id || state.changingMode || active || (human && !canControl);
  $("control-mode-label").textContent = canControl ? "You have control" : "View only";
  $("control-mode-description").textContent = canControl ? "Return control when you're ready to run a loop." : human ? "Another signed-in session controls this desktop." : active ? "The agent is running. Takeover is locked." : "The agent controls this desktop.";
  $("control-mode-icon").textContent = canControl ? "↖" : "◉";
  $("control-mode-icon").parentElement.classList.toggle("human", canControl);
  $("toggle-control").textContent = state.changingMode ? "Switching…" : canControl ? "Return to agent ↗" : human ? "Control unavailable" : "Take control ↗";
  $("toggle-control").disabled = disabled;
  if (state.rfb) { state.rfb.viewOnly = !canControl || active; state.rfb.focusOnClick = canControl && !active; }
  const runButton = $("run-instruction");
  if (runButton) {
    runButton.disabled = state.running || active || human || !state.detail?.instruction || state.detail?.status !== "ready" || state.session?.worker?.status !== "online";
    runButton.textContent = state.running ? "Starting…" : active ? "Run in progress" : getRuns().length ? "Run again ↗" : "Run instruction ↗";
  }
  const hint = $("run-control-hint");
  if (hint) { hint.textContent = human ? canControl ? "Return control to the agent to run this instruction." : "Another session must return desktop control before a run can start." : !state.session?.worker?.id ? "An assigned worker is required to run this instruction." : state.session?.worker?.status !== "online" ? "Waiting for the assigned worker to come online." : state.detail?.status === "generating" ? "Waiting for the model to generate and save an instruction." : "The saved instruction is sent to your assigned worker."; }
}

async function loadLoops() {
  const response = await api("/api/loops");
  state.loops = Array.isArray(response) ? response : response?.loops || [];
  renderLoops();
}
function renderLoops() {
  const signature = JSON.stringify([state.loops, state.selectedId]);
  if (signature === state.lastListSignature) return;
  state.lastListSignature = signature;
  $("loop-count").textContent = String(state.loops.length);
  if (!state.loops.length) { $("loop-list").innerHTML = '<p class="list-empty">Your saved loops live here.<br>Create one to get started.</p>'; return; }
  $("loop-list").innerHTML = state.loops.map((loop) => `<button class="loop-list-item ${String(loop.id) === String(state.selectedId) ? "selected" : ""}" data-loop-id="${escapeHtml(loop.id)}" ${String(loop.id) === String(state.selectedId) ? 'aria-current="true"' : ""}><span class="list-title">${escapeHtml(loop.prompt || "Untitled loop")}</span><span class="list-item-meta"><i class="status-dot ${errorStatuses.has(loop.status) ? "error" : activeStatuses.has(loop.status) ? "warn" : ""}"></i>${escapeHtml(humanize(loop.status))}<time title="${escapeHtml(formatDate(loop.created_at, { second: "2-digit" }))}">${escapeHtml(validDate(loop.created_at)?.toLocaleDateString(undefined, { month: "short", day: "numeric" }) || "—")}</time></span></button>`).join("");
}
async function selectLoop(id) {
  state.selectedId = id; state.lastDetailSignature = ""; renderLoops();
  try { await loadDetail(id); } catch (error) { if (error.status !== 401) notice(`Couldn't load this loop: ${error.message}`, true); }
}
async function loadDetail(id = state.selectedId) {
  if (!id) return;
  const detail = await api(`/api/loops/${encodeURIComponent(id)}`);
  if (String(state.selectedId) !== String(id) || !state.session) return;
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
  return `<section class="run-group"><div class="run-summary"><div><span>Run ${total - index}</span><span class="run-id" title="${escapeHtml(run.id)}">${escapeHtml(String(run.id || "").slice(0, 8))}</span>${badge(run.status)}</div><span class="run-duration" data-run-start="${escapeHtml(run.started_at || run.created_at || "")}" data-run-end="${escapeHtml(run.finished_at || "")}" data-run-active="${activeStatuses.has(run.status)}">—</span></div>${events.length ? `<ol class="timeline">${events.map((event, eventIndex) => renderEvent(event, eventIndex, run.id)).join("")}</ol>` : '<p class="no-events">No worker events recorded yet.</p>'}${run.error ? `<div class="inline-error run-error">${escapeHtml(valueText(run.error))}</div>` : ""}</section>`;
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
function disconnectViewer() {
  state.viewerGeneration += 1; state.viewerConnecting = false;
  const rfb = state.rfb; state.rfb = null;
  if (rfb) { try { rfb.disconnect(); } catch { /* The connection may already be closed. */ } }
  $("desktop-canvas").replaceChildren(); setViewerStatus("Disconnected");
}
async function connectViewer() {
  if (!state.session?.authenticated || state.viewerConnecting) return;
  disconnectViewer(); state.viewerConnecting = true;
  const generation = state.viewerGeneration;
  setViewerStatus("Connecting", "warn"); viewerPlaceholder("Connecting to your desktop", "Opening the authenticated desktop connection…", false);
  try {
    viewerModule ||= import("/vendor/novnc/core/rfb.js");
    const { default: RFB } = await viewerModule;
    if (generation !== state.viewerGeneration || !state.session?.authenticated) return;
    const url = new URL("/api/desktop", window.location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const rfb = new RFB($("desktop-canvas"), url.toString(), { shared: true });
    state.rfb = rfb; rfb.viewOnly = state.session?.worker?.can_control !== true || hasActiveRun(); rfb.focusOnClick = !rfb.viewOnly; rfb.scaleViewport = state.fit; rfb.resizeSession = false; rfb.background = "#0c100b";
    rfb.addEventListener("connect", () => { if (state.rfb !== rfb) return; state.viewerConnecting = false; $("viewer-placeholder").hidden = true; setViewerStatus("Live", "live"); renderControl(); });
    rfb.addEventListener("disconnect", (event) => { if (state.rfb !== rfb) return; state.rfb = null; state.viewerConnecting = false; setViewerStatus("Disconnected", "warn"); viewerPlaceholder("Desktop connection closed", event.detail?.clean ? "Reconnect to resume watching your desktop." : "The viewer connection was interrupted. Your saved loops are still available."); });
    rfb.addEventListener("securityfailure", (event) => { if (state.rfb !== rfb) return; state.viewerConnecting = false; setViewerStatus("Access error", "error"); viewerPlaceholder("Desktop access failed", event.detail?.reason || "The desktop gateway could not authenticate this connection."); });
    rfb.addEventListener("credentialsrequired", () => { if (state.rfb !== rfb) return; disconnectViewer(); setViewerStatus("Configuration needed", "error"); viewerPlaceholder("Desktop authentication required", "The desktop gateway needs server-side credentials. Workspace credentials are never sent to the desktop."); });
  } catch (error) {
    if (generation !== state.viewerGeneration) return;
    state.viewerConnecting = false; viewerModule = null; setViewerStatus("Unavailable", "error"); viewerPlaceholder("Viewer unavailable", `The desktop viewer could not load: ${error.message}`);
  }
}

async function refresh() {
  if (state.pollBusy || state.creating || document.hidden || !state.session?.authenticated) return;
  state.pollBusy = true;
  try {
    if (!await loadSession()) return;
    await Promise.all([loadLoops(), state.selectedId ? loadDetail() : Promise.resolve()]);
  } catch (error) { if (error.status !== 401) setConnection(false); }
  finally { state.pollBusy = false; }
}
async function boot() {
  try {
    if (!await loadSession()) return;
    await loadLoops();
    if (state.loops.length) await selectLoop(state.loops[0].id);
    await connectViewer();
  } catch (error) { if (!state.session) { $("login-error").textContent = `Unable to connect: ${error.message}`; $("login-error").hidden = false; } else notice(error.message, true); }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("login-submit").disabled = true; $("login-submit").textContent = "Opening workspace…"; $("login-error").hidden = true;
  try { await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("password").value }) }); $("password").value = ""; await boot(); }
  catch (error) { $("login-error").textContent = error.message; $("login-error").hidden = false; }
  finally { $("login-submit").disabled = false; $("login-submit").innerHTML = '<span>Open workspace</span><span aria-hidden="true">↗</span>'; }
});
$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); state.session = null; state.detail = null; state.loops = []; state.selectedId = null; state.lastListSignature = ""; state.lastDetailSignature = ""; showLogin(); $("login-error").hidden = true; }
  catch (error) { notice(`Sign out failed: ${error.message}`, true); }
});
$("compose-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const prompt = $("prompt").value.trim(); if (!prompt || state.creating) return;
  state.creating = true; $("create-loop").disabled = true; $("create-loop").textContent = "Generating…"; $("prompt").disabled = true; $("page-notice").hidden = true;
  const previousDetail = state.detail; state.detail = null; state.selectedId = null; state.lastDetailSignature = ""; renderLoops();
  $("loop-detail").innerHTML = '<div class="pending-panel" role="status"><span class="spinner" aria-hidden="true"></span><h2>Turning intent into instruction</h2><p>Waiting for the language model. The returned instruction will be saved to your workspace.</p></div>';
  try {
    const loop = await api("/api/loops", { method: "POST", body: JSON.stringify({ prompt }) });
    state.selectedId = loop.id; state.detail = loop; renderDetail();
    await loadLoops(); await loadDetail(loop.id);
  } catch (error) { if (error.status !== 401) { state.detail = previousDetail; state.selectedId = previousDetail?.id || null; state.lastDetailSignature = ""; if (previousDetail) renderDetail(); else $("loop-detail").innerHTML = `<div class="pending-panel"><h2>Instruction could not be created</h2><p>${escapeHtml(error.message)}</p><p>Your request is still in the editor. Try again when the issue is resolved.</p></div>`; renderLoops(); notice(error.message, true); } }
  finally { state.creating = false; $("create-loop").disabled = false; $("create-loop").innerHTML = '<span>Create loop</span><span aria-hidden="true">↗</span>'; $("prompt").disabled = false; }
});
$("loop-list").addEventListener("click", (event) => { const item = event.target.closest("[data-loop-id]"); if (item && !state.creating) selectLoop(item.dataset.loopId); });
$("loop-detail").addEventListener("click", async (event) => {
  if (event.target.closest("#reuse-request")) { $("prompt").value = state.detail?.prompt || ""; $("prompt").focus(); $("compose-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); return; }
  if (!event.target.closest("#run-instruction") || state.running || hasActiveRun() || !state.detail?.instruction) return;
  const id = state.detail.id; state.running = true; renderControl(); $("page-notice").hidden = true;
  try { await api(`/api/loops/${encodeURIComponent(id)}/runs`, { method: "POST" }); await Promise.all([loadDetail(id), loadLoops(), loadSession()]); }
  catch (error) { if (error.status !== 401) notice(`Couldn't start this run: ${error.message}`, true); }
  finally { state.running = false; renderControl(); }
});
$("new-loop").addEventListener("click", () => { $("prompt").value = samplePrompt; $("prompt").focus(); $("prompt").select(); $("compose-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); });
$("toggle-control").addEventListener("click", async () => {
  if (state.changingMode) return;
  state.changingMode = true; renderControl();
  try { await api("/api/worker/mode", { method: "POST", body: JSON.stringify({ mode: state.session?.worker?.mode === "human" ? "agent" : "human" }) }); await loadSession(); }
  catch (error) { if (error.status !== 401) notice(error.message, true); }
  finally { state.changingMode = false; renderControl(); }
});
$("fit-view").addEventListener("click", () => { state.fit = !state.fit; if (state.rfb) state.rfb.scaleViewport = state.fit; $("fit-view").setAttribute("aria-pressed", String(state.fit)); $("fit-view").classList.toggle("active", state.fit); $("fit-view").textContent = state.fit ? "Fit" : "1:1"; $("fit-view").title = state.fit ? "Show desktop at actual size" : "Fit desktop to viewer"; });
$("fullscreen").addEventListener("click", async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await $("desktop-panel").requestFullscreen(); } catch (error) { notice(`Fullscreen is unavailable: ${error.message}`); } });
document.addEventListener("fullscreenchange", () => { const text = document.fullscreenElement ? "Exit fullscreen" : "Enter fullscreen"; $("fullscreen").title = text; $("fullscreen").setAttribute("aria-label", text); });
$("reconnect-viewer").addEventListener("click", connectViewer);
$("dismiss-notice").addEventListener("click", () => { $("page-notice").hidden = true; });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
document.addEventListener("keydown", (event) => { if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.closest("input, textarea, [contenteditable], #desktop-canvas") && state.session?.authenticated) { event.preventDefault(); $("new-loop").click(); } });
window.addEventListener("beforeunload", disconnectViewer);
setInterval(refresh, 2000);
setInterval(updateDurations, 1000);
boot();
