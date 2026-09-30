import { renderStepInspector, bindStepInspector, stepAccounting, renderStepTiming, bindStepTimings } from './workbench-inspector.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const active = run => ['queued', 'running', 'paused'].includes(run?.status);
const label = {click: 'Click', type_text: 'Type', press_key: 'Press key', read: 'Read text'};
const freshStep = () => ({id: crypto.randomUUID(), action: {tool: 'click', target: ''}});
const summary = step => `${label[step.action.tool] || step.action.tool} ${step.action.tool === 'type_text' ? step.action.text || '…' : step.action.tool === 'press_key' ? step.action.key || '…' : step.action.target || '…'}`;
const duration = ms => Number.isFinite(ms) ? `${Math.round(ms).toLocaleString()} ms` : 'Timing unavailable';

// The editor owns drafts only. The existing run API owns execution and its cursor.
export function createWorkbench(host, {api, session, scopedURL, onSaved, onRefresh, onArchive}) {
  let detail = null, draft = null, selected = null, busy = false, dirty = false, inspector = false;
  let error = '', requestId = null;
  let displayedRun = null, signature = '', epoch = 0, startStep = null, record = null, saveTimer = null;
  const drafts = new Map();
  const reports = new Map();
  const timingDisclosure = new Map();

  function personalRuns() {
    const value = session();
    return (detail?.runs || []).filter(run => run.worker_id === value?.worker?.id && run.initiated_by === value?.target_user_id)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  }
  function latestRun() { return personalRuns()[0] || null; }
  function currentRun() { return (detail?.runs || []).find(run => run.id === displayedRun) || latestRun(); }
  function resultFor(run, id) { return run?.step_results?.find(result => result.step_id === id) || run?.result?.steps?.find(result => result.step_id === id); }
  function runForStep(step) {
    if (historical()) return currentRun();
    // Single-step runs must not erase evidence for the other executed rows.
    return personalRuns().find(run => {
      const index = run.steps?.findIndex(item => item.id === step.id) ?? -1;
      return resultFor(run, step.id) || index >= 0 && (run.step_in_flight === index || active(run) && index >= (run.start_step ?? 0) && index < (run.end_step ?? run.steps.length));
    }) || currentRun();
  }
  function reportState(run) { return reports.get(run?.id) || {}; }
  function historical() { return Boolean(displayedRun); }
  function shownSteps() { return historical() ? currentRun()?.steps || [] : draft?.steps || []; }
  function selectedStep() { return shownSteps().find(step => step.id === selected); }
  function owner() {
    const value = session();
    return JSON.stringify([value?.authenticated,value?.user?.id,value?.company?.id,value?.target_user_id,value?.worker?.id,scopedURL('/api/loops')]);
  }
  function context() { return {epoch,owner:owner()}; }
  function current(context) { return epoch === context.epoch && owner() === context.owner && !host.hidden; }
  function storageScope() {
    const value = session();
    return `ctrlloop.workbench.v1:${JSON.stringify([value?.user?.id,value?.company?.id,value?.target_user_id])}:`;
  }
  function persist(value) {
    try {
      localStorage.setItem(value.storageKey, JSON.stringify({draft:value.draft,dirty:value.dirty,selected:value.selected,startStep:value.startStep,requestId:value.requestId,loop:value.loop}));
      value.local = true;
    } catch { value.local = false; }
  }
  function stash() {
    if (!draft || !record) return;
    Object.assign(record, {draft,dirty,selected,startStep,requestId});
    persist(record);
  }
  function scheduleSave() {
    clearTimeout(saveTimer); saveTimer = null;
    if (!dirty || !editable() || record?.error) return;
    saveTimer = setTimeout(() => { saveTimer = null; void autosave(); }, 600);
  }
  function changed() { dirty = true; requestId = null; record.error = ''; stash(); scheduleSave(); }
  function deactivate() { clearTimeout(saveTimer); saveTimer = null; stash(); epoch++; busy = false; reports.clear(); signature = ''; }
  function reset() { deactivate(); drafts.clear(); timingDisclosure.clear(); record = null; draft = null; detail = null; signature = ''; requestId = null; busy = false; inspector = false; displayedRun = null; host.replaceChildren(); }
  function open(next = null) {
    const key = next?.id || 'new';
    if (draft && (detail?.id || 'new') === key) {
      if (host.hidden) { epoch++; busy = false; reports.clear(); }
      detail = next; render(); scheduleSave(); return;
    }
    clearTimeout(saveTimer); saveTimer = null; stash(); epoch++; detail = next; displayedRun = null; inspector = false; reports.clear(); timingDisclosure.clear(); error = ''; requestId = null; busy = false;
    const storageKey = storageScope() + key;
    let saved = drafts.get(key);
    if (!saved) {
      try {
        let recovered = JSON.parse(localStorage.getItem(storageKey));
        if (key === 'new' && recovered?.loop?.id) recovered = JSON.parse(localStorage.getItem(storageScope() + recovered.loop.id)) || recovered;
        if (typeof recovered?.draft?.prompt === 'string' && Array.isArray(recovered.draft.steps) && recovered.draft.steps.every(step => typeof step.id === 'string' && label[step.action?.tool])) saved = recovered;
      } catch { /* A damaged local copy must not hide the saved sequence. */ }
    }
    if (saved && !saved.dirty && next && next.version > saved.draft.version) saved = null;
    record = saved || {};
    record.storageKey = record.loop?.id ? storageScope() + record.loop.id : storageKey; drafts.set(key, record);
    draft = saved?.draft || {prompt: next?.prompt || 'Untitled sequence', steps: structuredClone(next?.steps || [freshStep()]), version: next?.version ?? 0};
    if (!next && record.loop) detail = {...record.loop,steps:structuredClone(draft.steps),runs:[]};
    requestId = saved?.requestId ?? null;
    dirty = saved?.dirty ?? !next; selected = draft.steps.some(step => step.id === saved?.selected) ? saved.selected : draft.steps[0]?.id;
    startStep = draft.steps.some(step => step.id === saved?.startStep) ? saved.startStep : draft.steps[0]?.id;
    render(true);
    if (detail && !next) { onSaved(detail); void onRefresh(detail.id).catch(() => {}); }
    if (dirty && saved) scheduleSave();
  }
  function update(next) {
    if (!draft || detail?.id !== next?.id) return open(next);
    if (next.version < draft.version) return;
    detail = next;
    if (requestId && next.runs?.some(run => run.request_id === requestId)) requestId = null;
    if (!dirty && draft.version < next.version) {
      draft = {prompt: next.prompt, steps: structuredClone(next.steps), version: next.version};
      if (!selectedStep()) selected = shownSteps()[0]?.id;
      if (!draft.steps.some(step => step.id === startStep)) startStep = draft.steps[0]?.id;
    }
    render();
    if (dirty && !saveTimer) scheduleSave();
  }
  function editable() {
    const value = session();
    const author = !detail || detail.created_by === value?.target_user_id || value?.user?.is_devadmin;
    return !busy && !historical() && author && !detail?.archived_at && !detail?.deleted_at && !(detail?.runs || []).some(active);
  }
  function render(force = false) {
    if (!draft) return;
    const run = currentRun(), live = latestRun(), locked = !editable(), worker = session()?.worker, history = historical(), steps = shownSteps();
    const workerState = worker ? [worker.id,worker.mode,worker.status,worker.active_run,worker.queued_run] : null;
    const stateKey = JSON.stringify([draft,selected,startStep,displayedRun,dirty,busy,Boolean(record?.saving),record?.error,locked,inspector,detail?.archived_at,detail?.runs,workerState,error,[...reports].map(([id,value]) => [id,value.key,value.loading,value.error])]);
    if (!force && signature === stateKey) return;
    signature = stateKey;
    const focused = host.contains(document.activeElement) ? document.activeElement : null;
    const focusId = focused?.id, selection = focused && 'selectionStart' in focused ? [focused.selectionStart, focused.selectionEnd] : null;
    const openDetails = new Set([...host.querySelectorAll('details[open]')].map(el => el.dataset.disclosure || el.id).filter(Boolean));
    for (const element of host.querySelectorAll('[data-timing-key]')) timingDisclosure.set(element.dataset.timingKey, element.open);
    const step = selectedStep();
    const inspectedRun = step ? runForStep(step) : run;
    const inspection = reportState(inspectedRun);
    const current = live?.step_in_flight ?? live?.next_step ?? 0;
    const awaiting = active(live), paused = live?.status === 'paused';
    const runnable = !busy && !history && !detail?.archived_at && !detail?.deleted_at && worker?.mode === 'agent' && worker?.status === 'online' && (!worker?.active_run || worker.active_run === live?.id) && (!worker?.queued_run || worker.queued_run === live?.id);
    const canStart = runnable && !awaiting && (!dirty || !locked) && draft.steps.length > 0;
    const canResume = runnable && paused;
    const startIndex = Math.max(0, draft.steps.findIndex(item => item.id === startStep));
    const edited = item => {
      const recorded = runForStep(item)?.steps?.find(saved => saved.id === item.id);
      return Boolean(recorded && JSON.stringify(recorded.action) !== JSON.stringify(item.action));
    };
    const stepStatus = item => {
      if (edited(item)) return 'edited';
      const source = runForStep(item);
      const index = source?.steps?.findIndex(saved => saved.id === item.id) ?? -1;
      if (!resultFor(source,item.id) && source?.status === 'running' && source.control === 'run' && index >= (source.start_step ?? 0) && index < (source.end_step ?? source.steps.length) && index === (source.step_in_flight ?? source.next_step)) return 'running';
      return resultFor(source,item.id)?.status || (source?.steps?.[source.step_in_flight]?.id === item.id ? source.status === 'running' ? 'running' : active(source) ? source.status : 'interrupted' : index >= 0 && (index < (source.start_step ?? 0) || index >= (source.end_step ?? source.steps.length)) ? 'not run' : 'pending');
    };
    const statusLabel = item => {
      if (edited(item)) return 'edited';
      const source = runForStep(item), outcome = resultFor(source,item.id);
      if (outcome?.status !== 'succeeded') return stepStatus(item) === 'interrupted' ? 'delivery unknown' : stepStatus(item);
      return 'Complete';
    };
    const finishing = awaiting && live?.step_in_flight == null && live?.next_step >= (live?.end_step ?? live?.steps?.length);
    const runReason = history ? 'Recorded snapshot · choose Current draft in Recorded runs to edit.' : !worker ? 'Assign a desktop before executing.' : worker.mode !== 'agent' ? 'Return desktop control to the agent to execute.' : worker.status !== 'online' ? 'Waiting for your desktop to reconnect.' : finishing ? 'Finishing run and saving evidence…' : live?.control === 'stop' && awaiting ? 'Stopping at the next step boundary…' : paused ? `Paused before step ${current + 1}. Continue, or stop to edit and choose a new starting point.` : live?.control === 'pause' && awaiting ? 'Pausing after the current step…' : live?.status === 'queued' ? 'Queued; waiting for the assigned worker.' : awaiting ? `Executing step ${current + 1} of ${live.steps.length}.` : `Start at step ${startIndex + 1} · uses the desktop as it is now.`;
    const ownReport = reportState(run).data || null;
    const totals = run ? stepAccounting(ownReport,run) : null;
    const completed = run?.step_results || [];
    const finishedDuration = run?.started_at && run?.finished_at ? Date.parse(run.finished_at) - Date.parse(run.started_at) : null;
    const capturedDuration = Number.isFinite(run?.result?.timing?.duration_ms) ? run.result.timing.duration_ms : null;
    const completedDuration = completed.length && completed.every(result => Number.isFinite(result.duration_ms)) ? completed.reduce((total,result) => total + result.duration_ms,0) : null;
    const runTiming = capturedDuration !== null ? `${duration(capturedDuration)} worker` : Number.isFinite(finishedDuration) ? `${duration(finishedDuration)} elapsed` : completedDuration !== null ? `${duration(completedDuration)} completed steps` : 'Timing unavailable';
    host.innerHTML = `<section class="wb panel" aria-label="Manual action workbench">
      <header class="wb-header"><div class="wb-heading"><h2>Action workbench</h2><span class="technical-label" role="status">${history ? 'RUN SNAPSHOT' : record?.saving ? 'SAVING…' : dirty ? record?.local ? 'DRAFT ON THIS DEVICE' : 'UNSAVED' : 'SAVED'}</span></div>
      <div class="wb-header-tools">${run && step ? `<button class="button quiet small" data-wb="inspect" aria-expanded="${inspector}" aria-controls="wb-inspector">${inspector ? '▾' : '▸'} Inspect selected step</button>` : ''}
      ${(detail?.runs?.length || 0) ? `<details class="wb-history" data-disclosure="history"><summary>Recorded runs (${detail.runs.length})</summary><div class="wb-history-menu"><label for="wb-run-select">Inspect saved steps</label><select id="wb-run-select"><option value="" ${!displayedRun ? 'selected' : ''}>Current draft</option>${[...detail.runs].sort((a,b) => String(b.created_at).localeCompare(String(a.created_at))).map((item,index) => `<option value="${esc(item.id)}" ${displayedRun === item.id ? 'selected' : ''}>${index === 0 ? 'Most recent · ' : ''}${esc(new Date(item.created_at).toLocaleString())} · ${esc(item.status)}</option>`).join('')}</select></div></details>` : ''}</div>
      ${run ? `<div class="wb-run-meta"><span>${esc(run.status)} · ${completed.filter(item => item.status === 'succeeded').length}/${(run.end_step ?? run.steps?.length ?? steps.length) - (run.start_step ?? 0)} steps</span><span>${esc(runTiming)}</span></div><p class="wb-muted wb-run-accounting">Jev: ${esc(totals.callsLabel)} calls · ${esc(totals.tokensLabel)} input tokens · ${esc(totals.costLabel)}</p>` : ''}</header>
      <div id="wb-inspector" ${inspector ? '' : 'hidden'}>${inspector && step && inspectedRun ? renderStepInspector({step: inspectedRun.steps?.find(s => s.id === step.id) || step,run:inspectedRun,report:inspection.data,loading:inspection.loading,error:inspection.error,reportURL: scopedURL(`/reports/runs/${encodeURIComponent(inspectedRun.id)}`),downloadURL: scopedURL(`/api/runs/${encodeURIComponent(inspectedRun.id)}/download`)}) : ''}${inspector && inspection.error ? `<button class="button quiet small" data-wb="retry-evidence" data-run-id="${esc(inspectedRun.id)}">Retry evidence</button>` : ''}</div>
      <label class="sr-only" for="wb-name">Sequence name</label><input id="wb-name" class="wb-name" maxlength="2000" value="${esc(draft.prompt)}" ${locked ? 'disabled' : ''}>
      <div class="wb-controls">${paused ? `<button class="button primary small" data-wb="run" ${canResume ? '' : 'disabled'}>Continue ▷</button><button class="button secondary small" data-wb="step" ${canResume ? '' : 'disabled'}>Next step ↦</button>` : `<button class="button primary small" data-wb="run" ${canStart ? '' : 'disabled'}>Run from this point ▷</button><button class="button secondary small" data-wb="single" ${canStart ? '' : 'disabled'}>Run only this step ↦</button>`}<button class="button quiet small" data-wb="pause" ${awaiting && !paused && !finishing ? '' : 'hidden'} ${!busy && !history && awaiting && !paused && live.control === 'run' ? '' : 'disabled'}>Pause</button><button class="button quiet small" data-wb="stop" ${awaiting && !finishing ? '' : 'hidden'} ${!busy && !history && awaiting && live.control !== 'stop' ? '' : 'disabled'}>Stop</button></div>
      <p class="wb-status" role="status">${esc(busy ? 'Saving command…' : runReason)}</p>
      ${detail?.archived_at ? '<p class="wb-muted" role="status">This CtrlLoop is archived. Recover it to edit or start new runs.</p>' : ''}
      ${error ? `<p class="inline-error" role="alert">${esc(error)}</p>` : ''}
      ${record?.error ? `<p class="inline-error" role="alert">Couldn’t save to your account. ${esc(record.error)} ${record.local ? 'Your draft is kept on this device.' : 'Keep this page open until you can save.'}</p>` : ''}
      ${run?.error ? `<p class="inline-error" role="status">${esc(run.error)}</p>` : ''}
      <ol class="wb-steps" aria-label="Sequence steps; use the arrow to set the starting point">${steps.map((item,index) => {
        const source = runForStep(item), evidence = reportState(source), status = stepStatus(item);
        const recorded = source?.steps?.find(saved => saved.id === item.id) || item;
        const unverified = resultFor(source,item.id)?.status === 'succeeded' && recorded.action.tool !== 'read' && resultFor(source,item.id)?.result?.pipeline?.action_result?.effect !== 'confirmed';
        return `<li data-step-id="${esc(item.id)}" aria-busy="${status === 'running'}" class="${item.id === selected ? 'selected' : ''} ${status === 'running' ? 'is-running' : ''} ${!history && item.id === startStep ? 'start-step' : ''}">
          <div class="wb-step-row">${!history ? `<button class="wb-start" data-start-step="${esc(item.id)}" aria-label="Start at step ${index + 1}" aria-pressed="${item.id === startStep}" title="Start at step ${index + 1}" ${busy || awaiting ? 'disabled' : ''}>${item.id === startStep ? '➜' : '·'}</button>` : ''}<button class="wb-select" data-select-step="${esc(item.id)}" aria-current="${item.id === selected ? 'step' : 'false'}"><span class="wb-step-number">${String(index + 1).padStart(2,'0')}</span><span class="wb-step-label">${esc(summary(item))}</span><span class="wb-step-state ${esc(status)}">${status === 'running' ? '<span class="wb-step-spinner" aria-hidden="true"></span>' : status === 'succeeded' ? '<span aria-hidden="true">✓</span>' : ''}${esc(statusLabel(item))}${unverified && !edited(item) ? '<span class="wb-step-unverified" title="Command completed; the application effect was not verified">effect unverified</span>' : ''}</span></button></div>
          ${renderStepTiming({step:recorded,stepIndex:index,run:source,report:evidence.data,loading:evidence.loading,error:evidence.error})}
        </li>`;
      }).join('')}</ol>
      ${!history && steps.some(edited) ? '<p class="wb-muted wb-empty">Edited steps apply to the next run. Inspection shows the action recorded in this run.</p>' : ''}
      <div class="wb-list-actions"><button class="button quiet small" data-wb="add" ${locked || draft.steps.length >= 100 ? 'disabled' : ''}>＋ Add step</button><button class="button quiet small" data-wb="save" ${locked || !dirty ? 'disabled' : ''}>Save</button></div>
      ${step ? `<div class="wb-editor"><div class="wb-editor-heading"><h3>Step ${steps.indexOf(step) + 1}</h3><div><button class="icon-button" data-wb="up" aria-label="Move step up" ${locked || steps.indexOf(step) === 0 ? 'disabled' : ''}>↑</button><button class="icon-button" data-wb="down" aria-label="Move step down" ${locked || steps.indexOf(step) === steps.length - 1 ? 'disabled' : ''}>↓</button><button class="icon-button" data-wb="remove" aria-label="Remove step" ${locked ? 'disabled' : ''}>×</button></div></div>
        <label for="wb-tool">Action</label><select id="wb-tool" ${locked ? 'disabled' : ''}>${Object.entries(label).map(([key,value]) => `<option value="${key}" ${step.action.tool === key ? 'selected' : ''}>${value}</option>`).join('')}</select>
        ${step.action.tool === 'type_text' ? `<label for="wb-value">Exact text to type</label><textarea id="wb-value" rows="2" maxlength="20000" ${locked ? 'disabled' : ''}>${esc(step.action.text || '')}</textarea>` : `<label for="wb-value">${step.action.tool === 'press_key' ? 'Key' : 'Instruction'}</label><input id="wb-value" maxlength="${step.action.tool === 'press_key' ? 100 : 2000}" ${step.action.tool === 'press_key' ? 'list="wb-keys"' : ''} placeholder="${step.action.tool === 'click' ? 'Browser address bar' : step.action.tool === 'read' ? 'Text of the first post' : 'Enter'}" value="${esc(step.action.tool === 'press_key' ? step.action.key || '' : step.action.target || '')}" ${locked ? 'disabled' : ''}>`}
        ${['type_text','press_key'].includes(step.action.tool) ? `<details class="wb-target" data-disclosure="target"><summary>${step.action.target ? 'Target: ' + esc(step.action.target) : 'Use current focus'}</summary><label for="wb-target">Target (optional)</label><input id="wb-target" maxlength="2000" value="${esc(step.action.target || '')}" placeholder="Leave empty to use current focus" ${locked ? 'disabled' : ''}></details>` : ''}
        <datalist id="wb-keys"><option value="Enter"><option value="Tab"><option value="Escape"><option value="Backspace"></datalist>
      </div>` : '<p class="wb-muted wb-empty">Add a step to begin.</p>'}
    </section>
    <div class="loop-configuration-actions">${detail?.id ? `<button class="button quiet small" data-wb="${detail.archived_at ? 'restore' : 'archive'}" ${busy || record?.saving || (detail.runs || []).some(active) ? 'disabled' : ''}>${detail.archived_at ? 'Recover' : 'Archive'}</button>${detail.archived_at ? `<button class="button quiet small" data-wb="delete" ${busy ? 'disabled' : ''}>Delete permanently</button>` : ''}` : ''}</div>`;
    for (const element of host.querySelectorAll('details')) if ((element.dataset.disclosure || element.id) && openDetails.has(element.dataset.disclosure || element.id)) element.open = true;
    for (const element of host.querySelectorAll('[data-timing-key]')) if (timingDisclosure.has(element.dataset.timingKey)) element.open = timingDisclosure.get(element.dataset.timingKey);
    bindStepInspector(host.querySelector('#wb-inspector'));
    bindStepTimings(host);
    if (focusId) { const replacement = host.querySelector(`#${CSS.escape(focusId)}`); replacement?.focus({preventScroll:true}); if (selection && replacement?.setSelectionRange && !['number','select-one'].includes(replacement.type)) replacement.setSelectionRange(...selection); }
    const neededReports = new Map(steps.map(item => runForStep(item)).filter(source => source && (source.step_results?.length || source.result?.steps?.length || source.step_in_flight != null && !active(source))).map(source => [source.id,source]));
    if (inspector && inspectedRun) neededReports.set(inspectedRun.id,inspectedRun);
    for (const source of neededReports.values()) void loadReport(source);
    stash();
  }
  async function loadReport(run) {
    const key = `${run.id}:${run.status}:${run.step_results?.length || 0}:${run.result?.capture?.complete ?? ''}:${Boolean(run.result?.artifacts)}`;
    if (reportState(run).key === key) return;
    const started = context(), entry = {key,loading:true,error:'',data:reportState(run).data};
    reports.set(run.id,entry); render();
    try { const data = await api(`/api/runs/${encodeURIComponent(run.id)}/report`); if (current(started) && reports.get(run.id) === entry) entry.data = data; }
    catch (failure) { if (current(started) && reports.get(run.id) === entry) entry.error = `Evidence unavailable: ${failure.message}`; }
    finally { if (current(started) && reports.get(run.id) === entry) { entry.loading = false; render(true); } }
  }
  function validate(mode) {
    if (!draft.prompt.trim()) throw new Error('Give the sequence a name.');
    if (!draft.steps.length) throw new Error('Add at least one step.');
    const start = draft.steps.findIndex(step => step.id === startStep);
    if (start < 0) throw new Error('Choose a starting step.');
    for (const [index,step] of draft.steps.entries()) {
      if (index < start || (mode === 'single' && index !== start)) continue;
      const action = step.action;
      if (['click','read'].includes(action.tool) && !action.target?.trim()) throw new Error(`Step ${index + 1} needs an instruction.`);
      if (action.tool === 'type_text' && !action.text) throw new Error(`Step ${index + 1} needs exact text to type.`);
      if (action.tool === 'press_key' && !action.key?.trim()) throw new Error(`Step ${index + 1} needs a key.`);
    }
  }
  async function save(started) {
    const savingRecord = record;
    while (savingRecord.saving) await savingRecord.saving;
    if (!current(started)) return null;
    if (!dirty && detail?.id) return detail;
    const source = JSON.stringify({prompt:draft.prompt,steps:draft.steps});
    const body = {prompt:draft.prompt.trim() || 'Untitled sequence',steps:structuredClone(draft.steps)};
    const previous = detail;
    const oldStorageKey = savingRecord.storageKey;
    savingRecord.error = '';
    savingRecord.saving = (async () => {
      try {
        const saved = await api(previous?.id ? `/api/loops/${encodeURIComponent(previous.id)}` : '/api/loops', {method:previous?.id ? 'PATCH':'POST',body:JSON.stringify(previous?.id ? {...body,version:draft.version} : {...body,start:false})});
        savingRecord.loop = {id:saved.id,created_by:saved.created_by ?? previous?.created_by,status:saved.status,version:saved.version,prompt:saved.prompt};
        savingRecord.draft.version = saved.version;
        savingRecord.dirty = JSON.stringify({prompt:savingRecord.draft.prompt,steps:savingRecord.draft.steps}) !== source;
        if (!savingRecord.dirty) { savingRecord.draft.prompt = saved.prompt; savingRecord.draft.steps = structuredClone(saved.steps); }
        savingRecord.storageKey = oldStorageKey.slice(0, oldStorageKey.lastIndexOf(':') + 1) + saved.id;
        persist(savingRecord);
        const showingRecord = record === savingRecord && owner() === started.owner && !host.hidden;
        // Preserve the server ID even if the user left while the initial save was pending.
        if (!previous?.id) {
          try {
            if (showingRecord) localStorage.removeItem(oldStorageKey);
            else localStorage.setItem(oldStorageKey, JSON.stringify({draft:savingRecord.draft,dirty:savingRecord.dirty,selected:savingRecord.selected,startStep:savingRecord.startStep,loop:savingRecord.loop}));
          } catch { /* The account copy is already saved. */ }
          if (showingRecord) drafts.delete('new');
        }
        if (owner() === started.owner) drafts.set(saved.id, savingRecord);
        if (record === savingRecord && owner() === started.owner) {
          detail = {...detail,...saved}; dirty = savingRecord.dirty;
          if (showingRecord) onSaved(detail);
        }
        return saved;
      } catch (failure) {
        savingRecord.error = failure.message;
        throw failure;
      } finally { savingRecord.saving = null; }
    })();
    render(true);
    await savingRecord.saving;
    return current(started) ? detail : null;
  }
  async function autosave() {
    if (!dirty || !editable() || host.hidden || record?.error) return;
    const started = context();
    try {
      const saved = await save(started);
      if (saved && current(started)) await onRefresh(saved.id);
    } catch { /* Keep the draft and expose the save failure beside the controls. */ }
    finally { if (current(started)) { render(true); scheduleSave(); } }
  }
  async function command(name) {
    if (busy || historical()) return;
    if (detail?.archived_at || detail?.deleted_at) return;
    clearTimeout(saveTimer); saveTimer = null;
    const started = context(); busy = true; error = ''; render();
    const acceptRun = next => {
      const previous = detail.runs?.find(run => run.id === next.id);
      detail.runs = [{...previous,...next},...(detail.runs || []).filter(run => run.id !== next.id)];
    };
    try {
      let live = latestRun();
      if (name === 'save') await save(started);
      else if (['run','step','single'].includes(name) && !active(live)) {
        validate(name);
        const saved = await save(started); if (!saved || !current(started)) return;
        requestId ||= crypto.randomUUID(); stash();
        const created = await api(`/api/loops/${encodeURIComponent(saved.id)}/runs`,{method:'POST',body:JSON.stringify({mode:name === 'run' ? 'sequence':name,start_step:draft.steps.findIndex(step => step.id === startStep),request_id:requestId})});
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
  async function archiveCommand(action) {
    if (busy || !detail?.id) return;
    clearTimeout(saveTimer); saveTimer = null;
    const started = context(); busy = true; error = ''; render();
    try {
      // Preserve authored changes before making the saved loop read-only.
      if (action === 'archive' && dirty) {
        const saved = await save(started);
        if (!saved || !current(started)) return;
      }
      await onArchive(detail.id, action);
    } catch (failure) { if (current(started)) error = failure.message; }
    finally { if (current(started)) { busy = false; render(true); scheduleSave(); } }
  }
  host.addEventListener('input', event => {
    if (!editable()) return;
    const step = selectedStep(), id = event.target.id;
    if (id === 'wb-name') draft.prompt = event.target.value;
    else if (id === 'wb-value' && step) step.action[step.action.tool === 'type_text' ? 'text' : step.action.tool === 'press_key' ? 'key' : 'target'] = event.target.value;
    else if (id === 'wb-target' && step) { if (event.target.value.trim()) step.action.target = event.target.value; else delete step.action.target; }
    else return;
    changed();
    // Keep the cursor stable while reflecting the edited step title and save state.
    const row = host.querySelector(`[data-select-step="${selected}"] .wb-step-label`);
    if (row && step) row.textContent = summary(step);
    host.querySelector('.wb-heading .technical-label').textContent = record.local ? 'DRAFT ON THIS DEVICE' : 'UNSAVED';
    host.querySelector('[data-wb="save"]').disabled = false;
  });
  host.addEventListener('change', event => {
    if (event.target.id === 'wb-run-select') { displayedRun = event.target.value; if (!selectedStep()) selected = shownSteps()[0]?.id; inspector = true; render(true); scheduleSave(); return; }
    if (event.target.id !== 'wb-tool' || !editable()) return;
    const tool = event.target.value; selectedStep().action = {tool, ...(tool === 'press_key' ? {key:'Enter'} : tool === 'type_text' ? {text:''} : {target:''})};
    changed(); render(true); host.querySelector('#wb-value')?.focus();
  });
  host.addEventListener('click', event => {
    const start = event.target.closest('[data-start-step]');
    if (start && !start.disabled) { startStep = start.dataset.startStep; selected = startStep; requestId = null; render(true); return; }
    const select = event.target.closest('[data-select-step]');
    if (select) { selected = select.dataset.selectStep; render(true); return; }
    const button = event.target.closest('[data-wb]'); if (!button || button.disabled) return;
    const name = button.dataset.wb;
    if (['archive','restore','delete'].includes(name)) { void archiveCommand(name); return; }
    if (['run','step','single','pause','stop','save'].includes(name)) { void command(name); return; }
    if (name === 'inspect') { inspector = !inspector; render(true); return; }
    if (name === 'retry-evidence') { reports.delete(button.dataset.runId || currentRun()?.id); render(true); return; }
    if (!editable()) return;
    const index = draft.steps.findIndex(step => step.id === selected);
    if (name === 'add') { const step = freshStep(); draft.steps.push(step); selected = step.id; startStep = step.id; }
    if (name === 'remove') { draft.steps.splice(index,1); if (startStep === selected) startStep = draft.steps[Math.min(index,draft.steps.length - 1)]?.id; selected = draft.steps[Math.min(index,draft.steps.length - 1)]?.id; }
    if (name === 'up' || name === 'down') { const next = index + (name === 'up' ? -1 : 1); [draft.steps[index],draft.steps[next]] = [draft.steps[next],draft.steps[index]]; }
    changed(); render(true);
  });
  document.addEventListener('click', event => {
    const menu = host.querySelector('.wb-history[open]');
    if (menu && !menu.contains(event.target)) menu.open = false;
  });
  host.addEventListener('keydown', event => {
    const menu = host.querySelector('.wb-history[open]');
    if (event.key === 'Escape' && menu) { menu.open = false; menu.querySelector('summary').focus(); }
  });
  return {open, update, render, reset, deactivate};
}
