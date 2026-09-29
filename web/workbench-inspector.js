// The workbench reads the existing report payload. It does not translate or
// rewrite recorded Jev questions, desktop observations, or native commands.
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
const finite = (value) => Number.isFinite(value) && value >= 0;
const count = (value) => finite(value) ? value.toLocaleString() : "Unavailable";
const ms = (value) => finite(value) ? `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ms` : "Unavailable";
const usd = (value) => finite(value) ? `$${value.toFixed(8)}` : "Unavailable";
const cost = (tokens) => finite(tokens) ? tokens * 0.042 / 1000000 : null;
const human = (value) => String(value ?? "Unavailable").replaceAll("_", " ");
const date = (value) => value && !Number.isNaN(new Date(value).getTime()) ? new Date(value).toISOString().replace("T", " ") : "Unavailable";
const asArray = (value) => Array.isArray(value) ? value : [];
const note = (text) => `<p class="wb-muted">${esc(text)}</p>`;
const pendingContent = new Map();
const boundContainers = new WeakSet();
let nextContentId = 0;

function disclosure(label, render, key = "") {
  const id = String(++nextContentId);
  pendingContent.set(id, render);
  return `<details class="wb-disclosure" data-wb-detail="${id}"${key ? ` data-disclosure="${esc(key)}"` : ""}><summary>${esc(label)}</summary><div class="wb-detail-content"></div></details>`;
}

function raw(label, value) {
  return disclosure(label, () => value == null ? note("Unavailable in the captured record.") : `<pre class="wb-output">${esc(JSON.stringify(value, null, 2))}</pre>`);
}

function facts(values) {
  return `<dl class="wb-facts">${values.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>`;
}

function table(headings, rows) {
  return `<div class="wb-table-wrap"><table class="wb-detail-table"><thead><tr>${headings.map((heading) => `<th>${esc(heading)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function stepResult(run, stepId) {
  return asArray(run?.step_results).find((item) => item.step_id === stepId)
    ?? asArray(run?.result?.steps).find((item) => item.step_id === stepId)
    ?? null;
}

function scope(report, run, stepId) {
  const currentRun = run ?? report?.run;
  const result = stepId ? stepResult(currentRun, stepId) ?? stepResult(report?.run, stepId) : null;
  return {
    stepId,
    run: currentRun,
    result,
    events: asArray(report?.worker_events).filter((event) => !stepId || event.step_id === stepId),
    serverEvents: asArray(report?.events).filter((event) => !stepId || event.data?.step_id === stepId),
    pipeline: result?.result?.pipeline ?? (!stepId ? currentRun?.result?.pipeline : null),
  };
}

function selectionRounds(record) {
  const rounds = record.events.filter((event) => ["window_selected", "control_selected"].includes(event.event))
    .flatMap((event) => asArray(event.selections).map((round) => ({ stage: event.event === "window_selected" ? "windows" : "controls", iteration: event.iteration, round })));
  // A selected step may finish before its worker archive reaches the server.
  // Saved selection results retain the same questions and returned judgments.
  const pipelines = record.pipeline ? [record.pipeline] : record.stepId ? [] : asArray(record.run?.step_results).map((item) => item.result?.pipeline).filter(Boolean);
  const saved = pipelines.flatMap((pipeline) => Object.entries(pipeline.selections ?? {}).flatMap(([stage, items]) => asArray(items).map((round) => ({ stage, iteration: round.iteration, round }))));
  if (!saved.length) {
    saved.push(...record.serverEvents.filter((event) => ["jev_selection", "jev_window_selection", "selection"].includes(event.stage))
      .flatMap((event) => asArray(event.data?.rounds).map((round) => ({ stage: event.stage === "jev_window_selection" ? "windows" : "controls", iteration: round.iteration ?? event.data.iteration, round }))));
  }
  for (const entry of saved) {
    if (!rounds.some((item) => item.stage === entry.stage && item.iteration === entry.iteration && item.round.round === entry.round.round)) rounds.push(entry);
  }
  for (const event of record.events.filter((item) => item.operation === "selection.round" && item.event.startsWith("timing_"))) {
    if (!rounds.some((item) => item.stage === event.stage && item.iteration === event.iteration && item.round.round === event.round)) {
      rounds.push({ stage: event.stage, iteration: event.iteration, round: { round: event.round, span_id: event.span_id, candidate_count: event.candidate_count } });
    }
  }
  return rounds;
}

function jevCalls(record, rounds) {
  const calls = record.events.filter((event) => event.event === "jev_request").map((request) => {
    const response = record.events.find((event) => ["jev_response", "jev_response_error", "jev_request_unfinished"].includes(event.event) && event.request_id === request.request_id);
    return { request, response, stage: request.stage, iteration: request.iteration, round: request.round, batch: request.batch, tokens: response?.input_tokens ?? response?.body?.usage?.input_tokens, duration: response?.duration_ms };
  });
  for (const entry of rounds) {
    for (const saved of asArray(entry.round.api_calls)) {
      const captured = calls.find((call) => (saved.sdk_span_id != null && call.request?.span_id === saved.sdk_span_id)
        || (call.stage === entry.stage && call.iteration === entry.iteration && call.round === entry.round.round && (call.batch ?? 1) === (saved.batch ?? 1)));
      if (captured) {
        captured.saved = saved;
        captured.tokens ??= saved.input_tokens;
      } else {
        calls.push({ saved, stage: entry.stage, iteration: entry.iteration, round: entry.round.round, batch: saved.batch, tokens: saved.input_tokens, duration: null });
      }
    }
  }
  return calls;
}

function accounting(record, rounds, calls, stepId) {
  const declared = stepId ? record.result?.result?.timing?.jev_api_call_count : record.run?.result?.timing?.jev_api_call_count;
  const started = record.events.some((event) => event.event === "step_started" || (event.event === "timing_started" && event.operation === "workbench.step"));
  const finished = record.events.some((event) => event.event === "step_finished" || (event.event === "timing_finished" && event.operation === "workbench.step"));
  const hasCaptureGap = record.events.some((event) => event.event === "capture_gap");
  const knownCount = finite(declared) && declared >= calls.length ? declared : stepId && started && finished && !hasCaptureGap ? calls.length : null;
  const tokens = calls.filter((call) => finite(call.tokens)).reduce((sum, call) => sum + call.tokens, 0);
  const known = calls.filter((call) => finite(call.tokens)).length;
  const partial = knownCount == null || known < knownCount;
  const hasUsage = known > 0 || knownCount === 0;
  const duration = record.result?.duration_ms ?? (stepId
    ? record.events.find((event) => event.event === "timing_finished" && event.operation === "workbench.step")?.duration_ms
    : record.run?.result?.timing?.duration_ms);
  return {
    callCount: knownCount,
    capturedCalls: calls.length,
    inputTokens: hasUsage ? tokens : null,
    inputCost: hasUsage ? cost(tokens) : null,
    partial,
    durationMs: finite(duration) ? duration : null,
    callsLabel: knownCount != null ? count(knownCount) : calls.length ? `${count(calls.length)} captured` : "Unavailable",
    tokensLabel: hasUsage ? `${count(tokens)}${partial ? " + unavailable" : ""}` : "Unavailable",
    costLabel: hasUsage ? `${usd(cost(tokens))}${partial ? " + unavailable" : ""}` : "Unavailable",
    durationLabel: ms(duration),
    windowRounds: rounds.filter((entry) => entry.stage === "windows").length,
    controlRounds: rounds.filter((entry) => entry.stage === "controls").length,
  };
}

/** Compact accounting from the existing report, scoped to one authored step. */
export function stepAccounting(report, run = null, stepId = null) {
  const record = scope(report, run, stepId);
  const rounds = selectionRounds(record);
  return accounting(record, rounds, jevCalls(record, rounds), stepId);
}

function timingStage(span) {
  const stages = {
    window_discovery: ["CUA/UIA get windows", "#789cbe", 2, "Windows"],
    window_selection: ["Window selection outside captured rounds", "#747aaa", 1, "Window other"],
    foreground_activation: ["CUA focus", "#d5b28c", 2, "Focus"],
    control_acquisition: ["CUA/UIA get controls", "#a7b4bf", 2, "Controls"],
    control_selection: ["Control selection outside captured rounds", "#786b82", 1, "Control other"],
    action_dispatch: ["CUA action", "#ff620f", 2, "Action"],
    native_action: ["CUA action", "#ff620f", 3, "Action"],
    read_text: ["Read observed text", "#70ad98", 2, "Read text"],
    settling_wait: ["Settling wait", "#d2a450", 2, "Wait"],
  };
  if (span.operation === "selection.round") {
    return { name: `Jev ${span.stage === "windows" ? "window" : "control"} round ${span.round}`, label: `${span.stage === "windows" ? "Window" : "Control"} R${span.round}`, color: span.stage === "windows" ? "#8f94d7" : ["#ac86c3", "#d197b7", "#8984cf", "#c18fc6"][(Math.max(1, span.round) - 1) % 4], rank: 4, jev: true };
  }
  const value = stages[span.operation];
  return value ? { name: value[0], color: value[1], rank: value[2], label: value[3], jev: false } : null;
}

function timingPieces(record) {
  const spans = record.events.filter(event => event.event === "timing_finished" && finite(event.started_elapsed_ms) && finite(event.finished_elapsed_ms));
  const stepSpan = spans.find(event => event.operation === "workbench.step");
  const stepStart = record.events.find(event => event.event === "step_started");
  const stepFinish = record.events.find(event => event.event === "step_finished");
  const start = stepSpan?.started_elapsed_ms ?? stepStart?.elapsed_ms;
  const end = stepSpan?.finished_elapsed_ms ?? stepFinish?.elapsed_ms;
  if (!finite(start) || !finite(end) || end <= start) return null;
  const stages = spans.map(span => ({ span, category: timingStage(span) })).filter(item => item.category);
  // The round spans sit inside selection spans, and native actions sit inside
  // dispatch spans. Partition their boundaries so every millisecond appears once.
  const points = [...new Set([start, end, ...stages.flatMap(({ span }) => [span.started_elapsed_ms, span.finished_elapsed_ms])].filter(point => point >= start && point <= end))].sort((a, b) => a - b);
  const pieces = [];
  for (let index = 1; index < points.length; index++) {
    const left = points[index - 1], right = points[index];
    if (right <= left) continue;
    const chosen = stages.filter(({ span }) => span.started_elapsed_ms <= left && span.finished_elapsed_ms >= right).sort((a, b) => b.category.rank - a.category.rank)[0];
    const category = chosen?.category ?? { name: "Overhead / unaccounted time", label: "Overhead", color: "#62605c", jev: false };
    const key = category.jev ? `round:${chosen.span.span_id}` : `${category.name}:${chosen?.span.iteration ?? ""}`;
    const previous = pieces.at(-1);
    if (previous?.key === key) previous.end = right;
    else pieces.push({ ...category, key, start: left, end: right, span: chosen?.span });
  }
  return { start, end, duration: end - start, pieces };
}

function timingUsage(piece, calls, rounds, events, totals) {
  if (piece.total) return { tokens: totals.tokensLabel, dollars: totals.costLabel, note: "Aggregate step usage; individual process timing was not captured." };
  if (!piece.jev) {
    const selection = ["window_selection", "control_selection"].includes(piece.span?.operation);
    const direct = piece.span && !selection;
    const stage = piece.span?.stage ?? (piece.span?.operation === "window_selection" ? "windows" : "controls");
    const relevant = selection ? calls.filter(call => call.stage === stage && call.iteration === piece.span.iteration) : calls;
    const roundSpans = events.filter(event => event.event === "timing_finished" && event.operation === "selection.round" && finite(event.started_elapsed_ms) && finite(event.finished_elapsed_ms));
    const accounted = !totals.partial && totals.callCount === calls.length && !events.some(event => event.event === "capture_gap") && relevant.every(call => roundSpans.some(span => span.stage === call.stage && span.iteration === call.iteration && span.round === call.round));
    return direct || accounted
      ? { tokens: "0", dollars: "$0.00000000", note: "No Jev input tokens for this process." }
      : { tokens: "Unavailable", dollars: "Unavailable", note: "Missing round timing or usage prevents allocating Jev cost to this interval." };
  }
  const span = piece.span;
  const matches = calls.filter(call => call.stage === span.stage && call.iteration === span.iteration && call.round === span.round);
  const round = rounds.find(entry => entry.stage === span.stage && entry.iteration === span.iteration && entry.round.round === span.round)?.round;
  const known = matches.filter(call => finite(call.tokens));
  const total = known.reduce((sum, call) => sum + call.tokens, 0);
  const gap = events.some(event => event.event === "capture_gap");
  const complete = known.length > 0 && known.length === matches.length && !gap;
  const suffix = complete ? "" : " + unavailable";
  const reductions = round && finite(round.candidate_count) && finite(round.winner_count)
    ? `${count(round.candidate_count)} → ${count(round.winner_count)} candidates${finite(round.group_count) ? ` · ${count(round.group_count)} groups` : ""}` : "";
  return {
    tokens: known.length ? `${count(total)}${suffix}` : "Unavailable",
    dollars: known.length ? `${usd(cost(total))}${suffix}` : "Unavailable",
    note: [`${matches.length || "Unavailable"} captured API calls`, reductions].filter(Boolean).join(" · "),
  };
}

/** A compact, initially expanded timing disclosure for an executed step. */
export function renderStepTiming({ step, stepIndex = 0, run = null, report = null, loading = false, error = "" } = {}) {
  if (!step) return "";
  const record = scope(report, run, step.id);
  if (!record.result && !record.events.some(event => event.event === "step_started" || event.operation === "workbench.step")) return "";
  const rounds = selectionRounds(record), calls = jevCalls(record, rounds);
  const totals = accounting(record, rounds, calls, step.id);
  const capturedChart = timingPieces(record);
  const chart = capturedChart ?? (finite(totals.durationMs) ? {
    duration: totals.durationMs,
    pieces: [{ key: "step-total", name: "Step total · breakdown unavailable", label: "Step total", color: "#62605c", total: true, start: 0, end: totals.durationMs }],
  } : null);
  const key = `${record.run?.id ?? "run"}:${step.id}`;
  const timingId = `wb-timing-${record.run?.id ?? "run"}-${step.id}`.replace(/[^a-zA-Z0-9_-]/g, "-");
  const infoId = `${timingId}-info`;
  const startedAt = record.result?.started_at ?? record.run?.created_at;
  const timestamp = startedAt && !Number.isNaN(new Date(startedAt).getTime())
    ? `<p class="wb-timing-note wb-timing-timestamp">Ran <time datetime="${esc(startedAt)}" title="${esc(date(startedAt))}">${esc(new Date(startedAt).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" }))}</time></p>` : "";
  const stepLabel = `Step ${stepIndex + 1} · ${human(step.action?.tool)}${step.action?.target ? ` ${step.action.target}` : ""}`;
  const describe = piece => {
    const usage = timingUsage(piece, calls, rounds, record.events, totals);
    return `${stepLabel}\n${piece.name}${piece.span?.iteration != null ? ` · loop ${piece.span.iteration}` : ""}\nDuration: ${ms(piece.end - piece.start)}\nJev input tokens: ${usage.tokens}\nInput cost: ${usage.dollars}\n${usage.note}`;
  };
  const details = piece => `data-timing-info="${esc(describe(piece))}" aria-describedby="${esc(infoId)}" aria-label="${esc(describe(piece).replaceAll("\n", ". "))}"`;
  const legend = new Map();
  for (const piece of chart?.pieces ?? []) {
    const existing = legend.get(piece.key);
    if (existing) existing.end += piece.end - piece.start;
    else legend.set(piece.key, { ...piece, start: 0, end: piece.end - piece.start });
  }
  return `<details class="wb-step-timing" data-timing-key="${esc(key)}" open>
    <summary class="wb-timing-summary" id="${esc(timingId)}-summary">Timing · ${esc(chart ? ms(chart.duration) : totals.durationLabel)}</summary>
    <div class="wb-timing-chart">
      ${timestamp}
      ${chart ? `<div class="wb-timing-track" role="group" aria-label="${esc(stepLabel)} process durations">
        ${chart.pieces.map((piece, index) => `<button type="button" class="wb-timing-segment" id="${esc(timingId)}-segment-${index}" style="width:${piece.total ? 100 : (piece.end - piece.start) / chart.duration * 100}%;--segment-color:${piece.color}" ${details(piece)}></button>`).join("")}
      </div><div class="wb-timing-scale"><span>0 ms</span><span>${esc(ms(chart.duration))}</span></div>
      <div class="wb-timing-legend">${[...legend.values()].map((piece, index) => `<button type="button" id="${esc(timingId)}-legend-${index}" ${details(piece)}><i class="wb-timing-key" style="--segment-color:${piece.color}" aria-hidden="true"></i><span>${esc(piece.label)} · ${esc(ms(piece.end))}</span></button>`).join("")}</div>
      <div class="wb-timing-info" id="${esc(infoId)}" role="tooltip" hidden></div>` : !loading && !error ? '<p class="wb-timing-note">Process timing details are unavailable in this captured run.</p>' : ""}
      ${chart && !capturedChart && !loading ? '<p class="wb-timing-note">Step total shown; process breakdown unavailable.</p>' : ""}
      ${loading ? `<p class="wb-timing-note" role="status">${capturedChart ? "Refreshing captured timings…" : "Loading process timings…"}</p>` : ""}
      ${error ? `<p class="wb-timing-note wb-timing-error" role="status">${esc(error)}</p><button type="button" class="button quiet small" id="${esc(timingId)}-retry" data-wb="retry-evidence" data-run-id="${esc(record.run?.id)}">Retry evidence</button>` : ""}
      <p class="wb-timing-note">${esc(totals.callsLabel)} Jev calls · ${esc(totals.tokensLabel)} input tokens · ${esc(totals.costLabel)}</p>
      ${chart ? '<p class="wb-timing-note">Hover or focus a segment or label. Input cost: $0.042 per million tokens; output is free.</p>' : ""}
    </div>
  </details>`;
}

const timingContainers = new WeakSet();

/** Float details beside the pointer without changing the step's layout. */
export function bindStepTimings(container) {
  if (!container || timingContainers.has(container)) return;
  timingContainers.add(container);
  let tooltip = null, activeButton = null, dismissed = null;
  const segment = target => target?.closest?.("[data-timing-info]");
  const hide = () => { if (tooltip) tooltip.hidden = true; tooltip = null; };
  const show = (target, point) => {
    const button = segment(target);
    if (!button || !container.contains(button) || dismissed === button) return;
    const info = button.closest(".wb-step-timing")?.querySelector(".wb-timing-info");
    if (!info) return;
    if (tooltip !== info) hide();
    tooltip = info;
    activeButton = button;
    if (info.textContent !== button.dataset.timingInfo) info.textContent = button.dataset.timingInfo;
    info.hidden = false;
    const anchor = button.getBoundingClientRect();
    const x = point?.clientX ?? anchor.right, y = point?.clientY ?? anchor.top;
    const {width,height} = info.getBoundingClientRect();
    const margin = 12, viewport = document.documentElement;
    const left = x + margin + width <= viewport.clientWidth - margin ? x + margin : x - width - margin;
    const top = y - height - margin >= margin ? y - height - margin : y + margin;
    info.style.left = `${Math.max(margin, Math.min(left, viewport.clientWidth - width - margin))}px`;
    info.style.top = `${Math.max(margin, Math.min(top, viewport.clientHeight - height - margin))}px`;
  };
  container.addEventListener("pointerover", event => show(event.target, event));
  container.addEventListener("pointermove", event => show(event.target, event));
  container.addEventListener("pointerout", event => {
    if (!segment(event.target)) return;
    const next = segment(event.relatedTarget);
    if (next === segment(event.target)) return;
    dismissed = null;
    hide();
    if (next) show(next, event);
  });
  container.addEventListener("focusin", event => {
    if (segment(event.target)?.matches(":focus-visible")) show(event.target);
  });
  container.addEventListener("focusout", hide);
  window.addEventListener("keydown", event => {
    if (event.key === "Escape") { dismissed = activeButton; hide(); }
    else dismissed = null;
  });
  container.addEventListener("toggle", event => { if (!event.target.open) hide(); }, true);
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
  window.addEventListener("blur", hide);
}

function questions(call) {
  const inputs = call.request?.body?.questions ?? call.saved?.questions ?? {};
  const answers = call.response?.body?.answers ?? call.saved?.answers ?? {};
  if (!Object.keys(inputs).length) return note("Exact question input was not captured.");
  return Object.entries(inputs).map(([key, question]) => {
    const answer = answers[key];
    const instructions = question.instructions;
    const wording = typeof instructions === "string" ? instructions : instructions?.question;
    const criteria = question.criteria ?? {};
    const probabilities = answer?.probabilities ?? {};
    return disclosure(`${key} · chose ${answer?.choice ?? "Unavailable"}`, () =>
      `${wording ? `<p class="wb-question">${esc(wording)}</p>` : note("Question text is only available in the exact input below.")}
      ${facts([["Chosen option", answer?.choice ?? "Unavailable"], ["Confidence", finite(answer?.confidence) ? answer.confidence.toFixed(4) : "Unavailable"]])}
      ${Object.keys(probabilities).length ? table(["Option", "Probability", "Chosen"], Object.entries(probabilities).map(([option, probability]) => [option, finite(probability) ? `${(probability * 100).toFixed(2)}%` : "Unavailable", option === answer?.choice ? "✓" : ""])) : note("Probabilities unavailable.")}
      ${raw("Candidate descriptions / option criteria", criteria)}${raw("Exact question and answer", { question, answer: answer ?? null })}`);
  }).join("");
}

function renderCall(call, index) {
  const model = call.request?.body?.model ?? call.saved?.requested_model;
  const stage = call.stage === "windows" ? "window" : "control";
  return disclosure(`API call ${index + 1} · ${stage} round ${call.round ?? "?"} · ${ms(call.duration)} · ${count(call.tokens)} input tokens · ${usd(cost(call.tokens))}`, () =>
    `${facts([["Loop", call.iteration ?? "Unavailable"], ["Round", `${stage} ${call.round ?? "?"}`], ["Batch", call.batch ?? "Unavailable"], ["Pinned model", model ?? "Unavailable"], ["Request UTC", date(call.request?.timestamp)], ["Response UTC", date(call.response?.timestamp)]])}
    ${!call.request ? note("Recovered from saved selection results. Exact HTTP envelopes and transport timing are unavailable.") : ""}
    ${raw("Minimal state actually sent to Jev", call.request?.body?.state ?? call.saved?.state)}
    ${questions(call)}
    ${raw("Exact call inputs", call.request?.body ?? (call.saved ? { state: call.saved.state, questions: call.saved.questions, model: call.saved.requested_model } : null))}
    ${raw("Prepared request and transport timing", call.request)}
    ${raw("Response, usage and transport timing", call.response ?? call.saved)}`);
}

function renderRound(entry, events) {
  const round = entry.round;
  const stage = entry.stage === "windows" ? "Window" : "Control";
  const duration = events.find((event) => event.event === "timing_finished" && event.span_id === round.span_id)?.duration_ms;
  return disclosure(`${stage} round ${round.round} · ${count(round.candidate_count)} → ${count(round.winner_count)} candidates · ${ms(duration)}`, () =>
    `${facts([["Loop", entry.iteration ?? "Unavailable"], ["Groups", count(round.group_count)], ["API calls", Array.isArray(round.api_calls) ? count(round.api_calls.length) : "Unavailable"]])}
    ${asArray(round.groups).length ? table(["Group question", "Candidates", "Chosen"], round.groups.map((group) => [group.question_key, asArray(group.options).join(", "), group.winner ?? "Unavailable"])) : note("Group evidence unavailable.")}
    ${round.actions ? raw("Jev action choices for finalists", round.actions) : ""}
    ${round.settling ? raw("Jev settling choices for finalists", round.settling) : ""}
    ${raw("Complete recorded round", round)}`);
}

function renderNativeCalls(events) {
  const callIds = [...new Set(events.filter((event) => ["native_call_intent", "native_call_sent", "native_call_returned", "native_call_error"].includes(event.event)).map((event) => event.call_id))];
  return callIds.map((id) => {
    const evidence = events.filter((event) => event.call_id === id && event.event.startsWith("native_call_"));
    const sent = evidence.find((event) => event.event === "native_call_sent");
    const returned = evidence.find((event) => event.event === "native_call_returned");
    const error = evidence.find((event) => event.event === "native_call_error");
    const first = evidence[0];
    return disclosure(`CUA ${first.tool ?? first.method ?? "call"} · ${id} · ${error ? "error" : returned ? "returned" : sent ? "return unavailable" : "dispatch uncertain"}`, () =>
      `${facts([["Command sent UTC", date(sent?.timestamp)], ["Duration", ms(error?.duration_ms ?? returned?.duration_ms)], ["Loop", first.iteration ?? "Unavailable"]])}${raw("Arguments, dispatch and returned result", evidence)}`);
  }).join("") || note("No native command evidence is available yet.");
}

function renderObservations(record) {
  const observations = record.events.filter((event) => ["windows_discovered", "window_observed", "controls_observed", "desktop_observed"].includes(event.event));
  const selected = record.events.filter((event) => ["window_selected", "control_selected"].includes(event.event));
  return `${observations.map((event) => raw(`${human(event.event)} · loop ${event.iteration ?? "?"}`, event)).join("") || note("Raw observations are unavailable until the worker evidence archive is uploaded.")}
    ${selected.map((event) => raw(`${human(event.event)} · loop ${event.iteration ?? "?"}`, { ...event, selections: undefined })).join("")}
    ${record.pipeline ? raw("Saved selected window, control and observation coverage", { window: record.pipeline.window, control: record.pipeline.control, snapshot_id: record.pipeline.snapshot_id, observation: record.pipeline.observation }) : ""}`;
}

function operation(event) {
  const known = { window_discovery: "CUA/UIA get windows", window_selection: "Jev window selection", foreground_activation: "CUA focus", control_acquisition: "CUA/UIA get controls", control_selection: "Jev control selection", action_dispatch: "CUA action dispatch", native_action: "CUA action return", settling_wait: "Settling wait", "workbench.step": "Authored step total", "loop.cycle": "Loop cycle including return / capture" };
  return event.operation === "selection.round" ? `Jev ${event.stage === "windows" ? "window" : "control"} round ${event.round}` : known[event.operation] ?? human(event.operation);
}

function renderTiming(record) {
  const events = record.events;
  const spans = events.filter((event) => event.event === "timing_finished");
  const visible = spans.filter((event) => ["workbench.step", "loop.cycle", "window_discovery", "window_selection", "foreground_activation", "control_acquisition", "control_selection", "selection.round", "action_dispatch", "settling_wait"].includes(event.operation));
  const boundaries = events.filter((event) => ["loop_started", "loop_finished", "step_started", "step_finished", "native_call_sent"].includes(event.event));
  const rows = visible.map((event) => {
    const startedAt = event.started_at ?? (event.timestamp && finite(event.elapsed_ms) && finite(event.started_elapsed_ms)
      ? new Date(new Date(event.timestamp).getTime() - event.elapsed_ms + event.started_elapsed_ms).toISOString() : null);
    return { at: event.started_elapsed_ms, cells: [date(startedAt), operation(event), ms(event.duration_ms), ms(event.started_elapsed_ms)] };
  });
  for (const event of boundaries) {
    if (event.event === "native_call_sent" && !["click", "type_text", "press_key", "hotkey"].includes(event.tool)) continue;
    const boundary = event.event === "loop_started" ? `Loop ${event.iteration} starts · current state`
      : event.event === "loop_finished" ? `Loop ${event.iteration} ${event.completed_action_loop === false ? "attempt stopped" : "ends"}`
      : event.event === "native_call_sent" ? `CUA ${human(event.tool)} sent · loop ${event.iteration ?? "?"}`
      : human(event.event);
    rows.push({ at: event.elapsed_ms, cells: [date(event.timestamp), boundary, event.event === "loop_finished" ? ms(event.duration_ms) : "—", ms(event.elapsed_ms)] });
  }
  rows.sort((left, right) => (left.at ?? Infinity) - (right.at ?? Infinity));
  return `${facts([["Step start · worker UTC", date(record.result?.started_at)], ["Step finish · worker UTC", date(record.result?.finished_at)], ["Step total", ms(record.result?.duration_ms)], ["Run start · server UTC", date(record.run?.started_at)], ["Run finish · server UTC", date(record.run?.finished_at)], ["Run worker duration", ms(record.run?.result?.timing?.duration_ms)]])}
    ${note("Step identity and loop numbers are separate. A loop starts at current-state acquisition and ends when the final CUA input command is sent. Nested spans overlap; do not add their durations. Server and worker timestamps use separate clocks.")}
    ${rows.length ? table(["Worker UTC", "Operation / boundary", "Duration", "Run elapsed"], rows.map((row) => row.cells)) : note("Precise stage and loop timing is not available yet.")}
    ${raw("Every captured timing span and boundary", events.filter((event) => event.event.startsWith("timing_") || ["step_started", "step_finished", "loop_started", "loop_finished", "native_call_sent"].includes(event.event)))}`;
}

/** Return compact HTML; call bindStepInspector after mounting it. */
export function renderStepInspector({ step, run = null, report = null, loading = false, error = "", reportURL = "", downloadURL = "" } = {}) {
  pendingContent.clear();
  if (!step) return note("Select a step to inspect its result.");
  const record = scope(report, run, step.id);
  const result = record.result;
  const stepIndex = asArray(record.run?.steps).findIndex(item => item.id === step.id);
  const excluded = Array.isArray(record.run?.steps) && (stepIndex < (record.run.start_step ?? 0) || stepIndex >= (record.run.end_step ?? record.run.steps.length));
  const pipeline = record.pipeline;
  const rounds = selectionRounds(record);
  const calls = jevCalls(record, rounds);
  const totals = accounting(record, rounds, calls, step.id);
  const output = pipeline?.action_result;
  const reason = result?.error ?? result?.result?.error ?? result?.result?.reason ?? pipeline?.reason;
  const resultLabel = result?.status === "succeeded" ? step.action?.tool === "read" ? "Read completed" : output?.effect === "confirmed" ? "Command sent" : "Sent · unverified" : human(result?.status);
  const actionLabel = step.action ? `${human(step.action.tool)} ${step.action.target ?? (step.action.tool === "press_key" ? step.action.key : "current focus")}` : "Recorded step";
  const requested = record.events.filter((event) => event.event === "action_requested");
  const links = `${reportURL ? `<a class="button quiet small" href="${esc(reportURL)}" target="_blank" rel="noopener">Full timeline & report ↗</a>` : ""}${downloadURL ? `<a class="button quiet small" href="${esc(downloadURL)}">Evidence ZIP ↓</a>` : ""}`;
  return `<div class="wb-inspector">
    <p class="wb-muted">Recorded action: ${esc(actionLabel)}</p>
    ${result ? `<p class="wb-outcome"><strong>${esc(resultLabel)}</strong>${reason ? ` · ${esc(reason)}` : ""}</p>` : note(excluded ? "This step was not included in this run." : "This step has no completed result yet.")}
    ${typeof output?.text === "string" ? `<div class="wb-read-result"><strong>Read text</strong><pre class="wb-output">${esc(output.text)}</pre><p class="wb-muted">${esc(output.source ?? "Recorded desktop text")}</p></div>` : ""}
    ${facts([["Step time", totals.durationLabel], ["Jev calls", totals.callsLabel], ["Input tokens", totals.tokensLabel], ["Input cost", totals.costLabel]])}
    ${loading ? '<p class="wb-muted" role="status">Loading captured evidence…</p>' : ""}
    ${error ? `<p class="inline-error" role="alert">${esc(error)}</p>` : ""}
    ${disclosure("Action & result", () => `${raw("Authored step", step)}${requested.map((event) => raw(`Action requested · loop ${event.iteration ?? "?"}`, event)).join("")}${raw("Returned action / read result", output)}${raw("Step outcome", result)}${renderNativeCalls(record.events)}`, `step-${step.id}-action`)}
    ${disclosure(`Jev selection · ${totals.windowRounds} captured window rounds · ${totals.controlRounds} captured control rounds`, () => `${note("Candidates are windows or controls. Groups contain up to 10 candidates. Each round batches group questions into one API call where the request limit permits; split batches remain explicit. Input costs use $0.042 per million tokens; output is free.")}${rounds.map((entry) => renderRound(entry, record.events)).join("") || note("No completed selection round is available.")}${calls.map(renderCall).join("") || note("No captured API calls. Missing evidence does not establish zero calls, tokens or cost.")}`, `step-${step.id}-jev`)}
    ${disclosure("Observed state & candidates", () => renderObservations(record), `step-${step.id}-state`)}
    ${disclosure("Timings & evidence", () => `${renderTiming(record)}${asArray(report?.missing_capture).length ? `<p class="wb-muted">Run capture gaps</p><ul>${report.missing_capture.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}${raw("Complete events for this step", record.events)}${raw("Server progress for this step", record.serverEvents)}`, `step-${step.id}-timing`)}
    ${links ? `<div class="instruction-actions">${links}</div>` : ""}
  </div>`;
}

/** Hydrate disclosure content only when opened, keeping large traces off the DOM. */
export function bindStepInspector(container) {
  if (boundContainers.has(container)) return;
  boundContainers.add(container);
  container.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!details.matches?.("details[data-wb-detail]")) return;
    const slot = details.querySelector(":scope > .wb-detail-content");
    if (!details.open) {
      for (const child of slot.querySelectorAll("[data-wb-detail]")) pendingContent.delete(child.dataset.wbDetail);
      slot.replaceChildren();
      return;
    }
    if (!slot.childNodes.length) slot.innerHTML = pendingContent.get(details.dataset.wbDetail)?.() ?? "";
  }, true);
}
