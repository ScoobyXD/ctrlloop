import { renderStepInspector, bindStepInspector, stepAccounting } from './workbench-inspector.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const active = run => ['queued', 'running', 'paused'].includes(run?.status);
const label = {click: 'Click', type_text: 'Type', press_key: 'Press key', read: 'Read text'};
const freshStep = () => ({id: crypto.randomUUID(), action: {tool: 'click', target: ''}});
const summary = step => `${label[step.action.tool] || step.action.tool} ${step.action.tool === 'type_text' ? step.action.text || '…' : step.action.tool === 'press_key' ? step.action.key || '…' : step.action.target || '…'}`;
const duration = ms => Number.isFinite(ms) ? `${Math.round(ms).toLocaleString()} ms` : 'Timing unavailable';

// The editor owns drafts only. The existing run API owns execution and its cursor.
export function createWorkbench(host, {api, session, scopedURL, onSaved, onRefresh}) {
  let detail = null, draft = null, selected = null, busy = false, dirty = false, inspector = false;
  let error = '', report = null, reportError = '', reportLoading = false, reportKey = '', requestId = null;
  let displayedRun = null, signature = '', epoch = 0;
  const drafts = new Map();

  function latestRun() {
    const value = session();
    return (detail?.runs || []).filter(run => run.worker_id === value?.worker?.id && run.initiated_by === value?.target_user_id)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] || null;
  }
  function currentRun() { return (detail?.runs || []).find(run => run.id === displayedRun) || latestRun(); }
  function historical() { return Boolean(displayedRun); }
  function shownSteps() { return historical() ? currentRun()?.steps || [] : draft?.steps || []; }
  function selectedStep() { return shownSteps().find(step => step.id === selected); }
  function owner() {
    const value = session();
    return JSON.stringify([value?.authenticated,value?.user?.id,value?.company?.id,value?.target_user_id,value?.worker?.id,scopedURL('/api/loops')]);
  }
  function context() { return {epoch,owner:owner()}; }
  function current(context) { return epoch === context.epoch && owner() === context.owner && !host.hidden; }
  function stash() { if (draft) drafts.set(detail?.id || 'new', {draft, dirty, selected, requestId}); }
  function deactivate() { stash(); epoch++; busy = false; reportLoading = false; reportKey = ''; signature = ''; }
  function reset() { epoch++; drafts.clear(); draft = null; detail = null; report = null; reportKey = ''; reportError = ''; reportLoading = false; signature = ''; requestId = null; busy = false; inspector = false; displayedRun = null; host.replaceChildren(); }
  function open(next = null) {
    const key = next?.id || 'new';
    if (draft && (detail?.id || 'new') === key) {
      if (host.hidden) { epoch++; busy = false; reportLoading = false; reportKey = ''; }
      detail = next; render(); return;
    }
    stash(); epoch++; detail = next; displayedRun = null; inspector = false; report = null; reportKey = ''; reportError = ''; reportLoading = false; error = ''; requestId = null; busy = false;
    const saved = drafts.get(key);
    draft = saved?.draft || {prompt: next?.prompt || 'Untitled sequence', steps: structuredClone(next?.steps || [freshStep()]), version: next?.version ?? 0};
    requestId = saved?.requestId ?? null;
    dirty = saved?.dirty ?? !next; selected = draft.steps.some(step => step.id === saved?.selected) ? saved.selected : draft.steps[0]?.id;
    render(true);
  }
  function update(next) {
    if (!draft || detail?.id !== next?.id) return open(next);
    detail = next;
    if (!dirty && draft.version !== next.version) {
      draft = {prompt: next.prompt, steps: structuredClone(next.steps), version: next.version};
      if (!selectedStep()) selected = shownSteps()[0]?.id;
    }
    render();
  }
  function editable() {
    const value = session();
    const author = !detail || detail.created_by === value?.target_user_id || value?.user?.is_devadmin;
    return !busy && !historical() && author && !(detail?.runs || []).some(active);
  }
  function render(force = false) {
    if (!draft) return;
    const run = currentRun(), live = latestRun(), locked = !editable(), worker = session()?.worker, history = historical(), steps = shownSteps();
    const workerState = worker ? [worker.id,worker.mode,worker.status,worker.active_run,worker.queued_run] : null;
    const stateKey = JSON.stringify([draft,selected,displayedRun,dirty,busy,locked,inspector,detail?.runs,workerState,error,reportKey,reportLoading,reportError]);
    if (!force && signature === stateKey) return;
    signature = stateKey;
    const focused = host.contains(document.activeElement) ? document.activeElement : null;
    const focusId = focused?.id, selection = focused && 'selectionStart' in focused ? [focused.selectionStart, focused.selectionEnd] : null;
    const openDetails = new Set([...host.querySelectorAll('details[open]')].map(el => el.dataset.disclosure || el.id).filter(Boolean));
    const step = selectedStep();
    const current = live?.step_in_flight ?? live?.next_step ?? 0;
    const awaiting = active(live), paused = live?.status === 'paused';
    const runnable = !busy && !history && worker?.mode === 'agent' && worker?.status === 'online' && (!worker?.active_run || worker.active_run === live?.id) && (!worker?.queued_run || worker.queued_run === live?.id);
    const canStart = runnable && (!awaiting || paused) && (!dirty || !locked) && draft.steps.length > 0;
    const resultFor = id => run?.step_results?.find(result => result.step_id === id);
    const edited = item => {
      const recorded = run?.steps?.find(saved => saved.id === item.id);
      return Boolean(recorded && JSON.stringify(recorded.action) !== JSON.stringify(item.action));
    };
    const stepStatus = item => edited(item) ? 'edited' : resultFor(item.id)?.status || (run?.steps?.[run.step_in_flight]?.id === item.id ? active(run) ? 'running' : 'interrupted' : 'pending');
    const statusLabel = item => {
      if (edited(item)) return 'edited';
      const outcome = resultFor(item.id);
      if (outcome?.status !== 'succeeded') return stepStatus(item) === 'interrupted' ? 'delivery unknown' : stepStatus(item);
      if ((run?.steps?.find(saved => saved.id === item.id) || item).action.tool === 'read') return 'read';
      return outcome.result?.pipeline?.action_result?.effect === 'confirmed' ? 'sent' : 'sent · unverified';
    };
    const finishing = awaiting && live?.step_in_flight == null && live?.next_step >= live?.steps?.length;
    const runReason = history ? 'Inspecting a recorded run. Select Current draft below to edit or execute.' : live?.error && !awaiting ? 'Sequence stopped. Inspect the result before starting a new run.' : !worker ? 'Assign a desktop before executing.' : worker.mode !== 'agent' ? 'Return desktop control to the agent to execute.' : worker.status !== 'online' ? 'Waiting for your desktop to reconnect.' : finishing ? 'Finishing run and saving evidence…' : live?.control === 'stop' && awaiting ? 'Stopping at the next step boundary…' : paused ? `Paused before step ${current + 1}. Inspect a result, then continue.` : live?.control === 'pause' && awaiting ? 'Pausing after the current step…' : live?.status === 'queued' ? 'Queued; waiting for the assigned worker.' : awaiting ? `Executing step ${current + 1} of ${live.steps.length}.` : ['error','interrupted','blocked','unverified'].includes(live?.status) ? 'Sequence stopped. Inspect the result before starting a new run.' : live?.status === 'succeeded' ? 'Sequence finished. A new run starts again at step 1.' : 'Run one step, or execute the sequence.';
    const ownReport = report?.run?.id === run?.id ? report : null;
    const totals = run ? stepAccounting(ownReport,run) : null;
    const completed = run?.step_results || [];
    const finishedDuration = run?.started_at && run?.finished_at ? Date.parse(run.finished_at) - Date.parse(run.started_at) : null;
    const capturedDuration = Number.isFinite(run?.result?.timing?.duration_ms) ? run.result.timing.duration_ms : null;
    const completedDuration = completed.length && completed.every(result => Number.isFinite(result.duration_ms)) ? completed.reduce((total,result) => total + result.duration_ms,0) : null;
    const runTiming = capturedDuration !== null ? `${duration(capturedDuration)} worker` : Number.isFinite(finishedDuration) ? `${duration(finishedDuration)} elapsed` : completedDuration !== null ? `${duration(completedDuration)} completed steps` : 'Timing unavailable';
    host.innerHTML = `<section class="wb panel" aria-label="Manual action workbench">
      <div class="wb-heading"><h2>Action workbench</h2><span class="technical-label">${history ? 'RUN SNAPSHOT' : dirty ? 'UNSAVED' : 'SAVED'}</span></div>
      <label class="sr-only" for="wb-name">Sequence name</label><input id="wb-name" class="wb-name" maxlength="2000" value="${esc(draft.prompt)}" ${locked ? 'disabled' : ''}>
      <div class="wb-controls"><button class="button primary small" data-wb="run" ${canStart ? '' : 'disabled'}>${paused ? 'Continue' : live && !awaiting ? 'New run' : 'Run sequence'} ▷</button><button class="button secondary small" data-wb="step" ${canStart ? '' : 'disabled'}>${paused ? 'Next step' : live && !awaiting ? 'New run · step' : 'Step'} ↦</button><button class="button quiet small" data-wb="pause" ${awaiting && !paused && !finishing ? '' : 'hidden'} ${!busy && !history && awaiting && !paused && live.control === 'run' ? '' : 'disabled'}>Pause</button><button class="button quiet small" data-wb="stop" ${awaiting && !finishing ? '' : 'hidden'} ${!busy && !history && awaiting && live.control !== 'stop' ? '' : 'disabled'}>Stop</button></div>
      <p class="wb-status" role="status">${esc(busy ? 'Saving command…' : runReason)}</p>
      ${error ? `<p class="inline-error" role="alert">${esc(error)}</p>` : ''}
      ${run?.error ? `<p class="inline-error" role="status">${esc(run.error)}</p>` : ''}
      <ol class="wb-steps">${steps.map((item,index) => `<li class="${item.id === selected ? 'selected' : ''}"><button class="wb-select" data-select-step="${esc(item.id)}" aria-current="${item.id === selected ? 'step' : 'false'}"><span class="wb-step-number">${String(index + 1).padStart(2,'0')}</span><span class="wb-step-label">${esc(summary(item))}</span><span class="wb-step-state ${esc(stepStatus(item,index))}">${esc(statusLabel(item))}</span></button></li>`).join('')}</ol>
      ${!history && steps.some(edited) ? '<p class="wb-muted wb-empty">Edited steps apply to the next run. Inspection shows the action recorded in this run.</p>' : ''}
      <div class="wb-list-actions"><button class="button quiet small" data-wb="add" ${locked || draft.steps.length >= 100 ? 'disabled' : ''}>＋ Add step</button><button class="button quiet small" data-wb="save" ${locked || !dirty ? 'disabled' : ''}>Save</button></div>
      ${step ? `<div class="wb-editor"><div class="wb-editor-heading"><h3>Step ${steps.indexOf(step) + 1}</h3><div><button class="icon-button" data-wb="up" aria-label="Move step up" ${locked || steps.indexOf(step) === 0 ? 'disabled' : ''}>↑</button><button class="icon-button" data-wb="down" aria-label="Move step down" ${locked || steps.indexOf(step) === steps.length - 1 ? 'disabled' : ''}>↓</button><button class="icon-button" data-wb="remove" aria-label="Remove step" ${locked ? 'disabled' : ''}>×</button></div></div>
        <label for="wb-tool">Action</label><select id="wb-tool" ${locked ? 'disabled' : ''}>${Object.entries(label).map(([key,value]) => `<option value="${key}" ${step.action.tool === key ? 'selected' : ''}>${value}</option>`).join('')}</select>
        ${step.action.tool === 'type_text' ? `<label for="wb-value">Exact text to type</label><textarea id="wb-value" rows="2" maxlength="20000" ${locked ? 'disabled' : ''}>${esc(step.action.text || '')}</textarea>` : `<label for="wb-value">${step.action.tool === 'press_key' ? 'Key' : 'Find this target'}</label><input id="wb-value" maxlength="${step.action.tool === 'press_key' ? 100 : 2000}" ${step.action.tool === 'press_key' ? 'list="wb-keys"' : ''} placeholder="${step.action.tool === 'click' ? 'Browser address bar' : step.action.tool === 'read' ? 'Text of the first post' : 'Enter'}" value="${esc(step.action.tool === 'press_key' ? step.action.key || '' : step.action.target || '')}" ${locked ? 'disabled' : ''}>`}
        ${['type_text','press_key'].includes(step.action.tool) ? `<details class="wb-target" data-disclosure="target"><summary>${step.action.target ? 'Target: ' + esc(step.action.target) : 'Use current focus'}</summary><label for="wb-target">Target (optional)</label><input id="wb-target" maxlength="2000" value="${esc(step.action.target || '')}" placeholder="Leave empty to use current focus" ${locked ? 'disabled' : ''}></details>` : ''}
        <datalist id="wb-keys"><option value="Enter"><option value="Tab"><option value="Escape"><option value="Backspace"></datalist>
      </div>` : '<p class="wb-muted wb-empty">Add a step to begin.</p>'}
      ${run ? `<div class="wb-run-meta"><span>${esc(run.status)} · ${completed.length}/${run.steps?.length || steps.length} steps</span><span>${esc(runTiming)}</span></div><p class="wb-muted wb-run-accounting">Jev: ${esc(totals.callsLabel)} calls · ${esc(totals.tokensLabel)} input tokens · ${esc(totals.costLabel)}</p>` : ''}
      ${run && step ? `<button class="wb-inspect-toggle" data-wb="inspect" aria-expanded="${inspector}">${inspector ? '▾' : '▸'} Inspect selected step</button>` : ''}
      <div id="wb-inspector" ${inspector ? '' : 'hidden'}>${inspector && step && run ? renderStepInspector({step: run.steps?.find(s => s.id === step.id) || step,run,report:ownReport,loading:reportLoading,error:reportError,reportURL: scopedURL(`/reports/runs/${encodeURIComponent(run.id)}`),downloadURL: scopedURL(`/api/runs/${encodeURIComponent(run.id)}/download`)}) : ''}${inspector && run && reportError ? '<button class="button quiet small" data-wb="retry-evidence">Retry evidence</button>' : ''}</div>
      ${(detail?.runs?.length || 0) ? `<details class="wb-history" data-disclosure="history"><summary>Recorded runs (${detail.runs.length})</summary><label for="wb-run-select">Inspect saved steps</label><select id="wb-run-select"><option value="" ${!displayedRun ? 'selected' : ''}>Current draft</option>${[...detail.runs].sort((a,b) => String(b.created_at).localeCompare(String(a.created_at))).map((item,index) => `<option value="${esc(item.id)}" ${displayedRun === item.id ? 'selected' : ''}>${index === 0 ? 'Most recent · ' : ''}${esc(new Date(item.created_at).toLocaleString())} · ${esc(item.status)}</option>`).join('')}</select></details>` : ''}
    </section>`;
    for (const element of host.querySelectorAll('details')) if ((element.dataset.disclosure || element.id) && openDetails.has(element.dataset.disclosure || element.id)) element.open = true;
    bindStepInspector(host.querySelector('#wb-inspector'));
    if (focusId) { const replacement = host.querySelector(`#${focusId}`); replacement?.focus({preventScroll:true}); if (selection && replacement?.setSelectionRange && !['number','select-one'].includes(replacement.type)) replacement.setSelectionRange(...selection); }
    if (inspector && run) void loadReport(run);
    stash();
  }
  async function loadReport(run) {
    const key = `${run.id}:${run.status}:${run.step_results?.length || 0}:${run.result?.capture?.complete ?? ''}:${Boolean(run.result?.artifacts)}`;
    if (reportKey === key) return;
    const started = context(); reportKey = key; reportLoading = true; reportError = ''; if (report?.run?.id !== run.id) report = null; render();
    try { const data = await api(`/api/runs/${encodeURIComponent(run.id)}/report`); if (current(started) && reportKey === key) report = data; }
    catch (failure) { if (current(started) && reportKey === key) reportError = `Evidence unavailable: ${failure.message}`; }
    finally { if (current(started) && reportKey === key) { reportLoading = false; render(true); } }
  }
  function validate() {
    if (!draft.prompt.trim()) throw new Error('Give the sequence a name.');
    if (!draft.steps.length) throw new Error('Add at least one step.');
    for (const [index,step] of draft.steps.entries()) {
      const action = step.action;
      if (['click','read'].includes(action.tool) && !action.target?.trim()) throw new Error(`Step ${index + 1} needs a target.`);
      if (action.tool === 'type_text' && !action.text) throw new Error(`Step ${index + 1} needs exact text to type.`);
      if (action.tool === 'press_key' && !action.key?.trim()) throw new Error(`Step ${index + 1} needs a key.`);
    }
  }
  async function save(started) {
    validate();
    if (!dirty && detail?.id) return detail;
    const body = {prompt:draft.prompt.trim(),steps:draft.steps};
    const saved = await api(detail?.id ? `/api/loops/${encodeURIComponent(detail.id)}` : '/api/loops', {method:detail?.id ? 'PATCH':'POST',body:JSON.stringify(detail?.id ? {...body,version:draft.version} : {...body,start:false})});
    if (!current(started)) return null;
    detail = {...detail,...saved}; draft.version = saved.version; dirty = false; drafts.delete('new'); stash();
    onSaved(detail); return detail;
  }
  async function command(name) {
    if (busy || historical()) return;
    const started = context(); busy = true; error = ''; render();
    const acceptRun = next => {
      const previous = detail.runs?.find(run => run.id === next.id);
      detail.runs = [{...previous,...next},...(detail.runs || []).filter(run => run.id !== next.id)];
    };
    try {
      let live = latestRun();
      if (name === 'save') await save(started);
      else if (['run','step'].includes(name) && !active(live)) {
        const saved = await save(started); if (!saved || !current(started)) return;
        requestId ||= crypto.randomUUID();
        const created = await api(`/api/loops/${encodeURIComponent(saved.id)}/runs`,{method:'POST',body:JSON.stringify({mode:name === 'run' ? 'sequence':'step',request_id:requestId})});
        if (!current(started)) return;
        acceptRun(created);
        requestId = null;
      } else if (live) {
        const updated = await api(`/api/runs/${encodeURIComponent(live.id)}`,{method:'PATCH',body:JSON.stringify({command:name,version:live.control_version})});
        if (!current(started)) return;
        acceptRun(updated);
      }
      if (current(started)) await onRefresh(detail?.id);
    } catch (failure) { if (current(started)) error = failure.message; }
    finally { if (current(started)) { busy = false; render(true); } }
  }
  host.addEventListener('input', event => {
    if (!editable()) return;
    const step = selectedStep(), id = event.target.id;
    if (id === 'wb-name') draft.prompt = event.target.value;
    else if (id === 'wb-value' && step) step.action[step.action.tool === 'type_text' ? 'text' : step.action.tool === 'press_key' ? 'key' : 'target'] = event.target.value;
    else if (id === 'wb-target' && step) { if (event.target.value.trim()) step.action.target = event.target.value; else delete step.action.target; }
    else return;
    dirty = true; requestId = null; stash();
    // Keep the cursor stable while reflecting the edited step title and save state.
    const row = host.querySelector(`[data-select-step="${selected}"] .wb-step-label`);
    if (row && step) row.textContent = summary(step);
    host.querySelector('.wb-heading .technical-label').textContent = 'UNSAVED';
    host.querySelector('[data-wb="save"]').disabled = false;
  });
  host.addEventListener('change', event => {
    if (event.target.id === 'wb-run-select') { displayedRun = event.target.value; if (!selectedStep()) selected = shownSteps()[0]?.id; reportKey = ''; report = null; inspector = true; render(true); return; }
    if (event.target.id !== 'wb-tool' || !editable()) return;
    const tool = event.target.value; selectedStep().action = {tool, ...(tool === 'press_key' ? {key:'Enter'} : tool === 'type_text' ? {text:''} : {target:''})};
    dirty = true; requestId = null; render(true); host.querySelector('#wb-value')?.focus();
  });
  host.addEventListener('click', event => {
    const select = event.target.closest('[data-select-step]');
    if (select) { selected = select.dataset.selectStep; render(true); return; }
    const button = event.target.closest('[data-wb]'); if (!button || button.disabled) return;
    const name = button.dataset.wb;
    if (['run','step','pause','stop','save'].includes(name)) { void command(name); return; }
    if (name === 'inspect') { inspector = !inspector; render(true); return; }
    if (name === 'retry-evidence') { reportKey = ''; render(true); return; }
    if (!editable()) return;
    const index = draft.steps.findIndex(step => step.id === selected);
    if (name === 'add') { const step = freshStep(); draft.steps.push(step); selected = step.id; }
    if (name === 'remove') { draft.steps.splice(index,1); selected = draft.steps[Math.min(index,draft.steps.length - 1)]?.id; }
    if (name === 'up' || name === 'down') { const next = index + (name === 'up' ? -1 : 1); [draft.steps[index],draft.steps[next]] = [draft.steps[next],draft.steps[index]]; }
    dirty = true; requestId = null; render(true);
  });
  return {open, update, render, reset, deactivate};
}
