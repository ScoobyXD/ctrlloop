const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const lazyContent = new Map();
let lazySequence = 0;
function lazyDetails(label, render, className="raw-details") {
  const key=String(++lazySequence);
  lazyContent.set(key,render);
  return `<details class="${className}" data-lazy-key="${key}"><summary>${esc(label)}</summary><div class="lazy-content"></div></details>`;
}
function raw(label,value) {
  return lazyDetails(label,()=>{
    const pre=document.createElement("pre");
    pre.textContent=JSON.stringify(value,null,2);
    return pre;
  });
}
document.addEventListener("toggle",event=>{
  const details=event.target;
  if(!details.matches?.("details[data-lazy-key]"))return;
  const slot=details.querySelector(":scope > .lazy-content");
  if(!details.open){
    for(const child of slot.querySelectorAll("[data-lazy-key]"))lazyContent.delete(child.dataset.lazyKey);
    slot.replaceChildren();
    return;
  }
  if(slot.childNodes.length)return;
  const content=lazyContent.get(details.dataset.lazyKey)?.();
  if(typeof content==="string")slot.innerHTML=content;
  else if(content)slot.append(content);
},true);
const number = (value) => Number.isFinite(value);
const ms = (value) => number(value) ? `${value.toLocaleString(undefined,{maximumFractionDigits:2})} ms` : "Unavailable";
const usd = (value) => number(value) ? `$${value.toFixed(8)}` : "Unavailable";
const label = (value) => String(value || "unknown").replaceAll("_", " ");
const timestamp = (value) => value ? new Date(value).toISOString().replace("T"," ") : "Unavailable";
const parts = location.pathname.split("/").filter(Boolean);
const kind = parts[1], identity = parts[2];
const base = `/api/${kind}/${encodeURIComponent(identity)}`;
const requestedContext = new URLSearchParams(location.search);
function scopedURL(path) {
  const url = new URL(path, location.origin);
  for (const key of ["company_id", "user_id"]) if (requestedContext.get(key)) url.searchParams.set(key, requestedContext.get(key));
  return url.pathname + url.search;
}
let current;
let sessionGeneration = 0;
const personName = (person) => person?.display_name || person?.email || "Earlier company member";
function hideRecord() { current = null; lazyContent.clear(); $("report-content").replaceChildren(); $("report-content").hidden = true; }
function requireLogin() { sessionGeneration+=1; hideRecord(); $("report-login").hidden=false; $("report-loading").hidden=true; $("report-person").textContent="RUN EVIDENCE";  $("report-company-name").textContent=""; }

async function fetchJSON(path, options={}) {
  const generation=sessionGeneration;
  const response = await fetch(scopedURL(path), {credentials:"same-origin",cache:"no-store",...options});
  if(response.status===401){if(generation===sessionGeneration)requireLogin();return null;}
  const data = await response.json();
  if(!response.ok) throw new Error(typeof data.detail==="string" ? data.detail : `Request failed (${response.status})`);
  return data;
}
$("report-login").addEventListener("submit",async(event)=>{
  event.preventDefault();$("login-error").textContent="";$("report-login-submit").disabled=true;sessionGeneration+=1;
  try {
    const response=await fetch("/api/login",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("report-email").value.trim(),password:$("report-password").value})});
    if(!response.ok) { const detail=(await response.json()).detail; throw new Error(typeof detail==="string"?detail:Array.isArray(detail)?detail.map(item=>item.msg||"Invalid input").join("; "):"Sign in failed"); }
    $("report-password").value="";$("report-login").hidden=true;await load();
  } catch(error){$("login-error").textContent=error.message;}
  finally{$("report-login-submit").disabled=false;}
});
$("report-password-toggle").addEventListener("click", () => {
  const input = $("report-password"), button = $("report-password-toggle"), reveal = input.type === "password";
  input.type = reveal ? "text" : "password";
  button.setAttribute("aria-pressed", String(reveal));
  button.setAttribute("aria-label", reveal ? "Hide personal password" : "Show personal password");
});

function loopData(events) {
  const spans=events.filter(e=>e.event==="timing_finished" && number(e.started_elapsed_ms) && number(e.finished_elapsed_ms));
  const starts=events.filter(e=>e.event==="loop_started");
  return starts.map((start,index)=>{
    const end=events.find(e=>e.event==="loop_finished" && e.iteration===start.iteration);
    const cycle=spans.find(e=>["loop.cycle","loop.iteration"].includes(e.operation) && e.iteration===start.iteration);
    const finish=end?.finished_elapsed_ms;
    const extent=starts[index+1]?.elapsed_ms ?? cycle?.finished_elapsed_ms ?? Math.max(start.elapsed_ms,...events.filter(e=>e.iteration===start.iteration).map(e=>e.elapsed_ms||0));
    return {iteration:start.iteration,start:start.elapsed_ms,end:finish,extent:Math.max(extent,finish||0),boundary:end,spans:spans.filter(e=>e.iteration===start.iteration),events:events.filter(e=>e.iteration===start.iteration)};
  });
}
const colors={windows:"#789cbe",window:"#8f94d7",focus:"#d5b28c",controls:"#a7b4bf",action:"#ff620f",wait:"#d2a450",overhead:"#62605c"};
function stage(span){
  const op=span.operation;
  if(op==="window_discovery")return {name:"CUA/UIA get windows",key:"windows",color:colors.windows,rank:2};
  if(op==="window_selection")return {name:"Jev window selection overhead",key:"window",color:colors.window,rank:1};
  if(op==="foreground_activation")return {name:"CUA focus",key:"focus",color:colors.focus,rank:2};
  if(op==="control_acquisition")return {name:"CUA/UIA get controls",key:"controls",color:colors.controls,rank:2};
  if(op==="control_selection")return {name:"Jev control selection overhead",key:"control-overhead",color:"#786b82",rank:1};
  if(op==="selection.round")return {name:`Jev ${span.stage==="windows"?"window":"control"} round ${span.round}`,key:`${span.stage}-${span.round}`,color:span.stage==="windows"?colors.window:["#ac86c3","#d197b7","#8984cf","#c18fc6"][(span.round-1)%4],rank:4};
  if(["action_dispatch","native_action"].includes(op))return {name:"CUA action",key:"action",color:colors.action,rank:3};
  if(op==="settling_wait")return {name:"Settling wait",key:"wait",color:colors.wait,rank:2};
  return null;
}
function segments(loop){
  const stages=loop.spans.map(span=>({...span,category:stage(span)})).filter(e=>e.category);
  const points=[loop.start,loop.extent,...stages.flatMap(e=>[e.started_elapsed_ms,e.finished_elapsed_ms])];
  if(number(loop.end))points.push(loop.end);
  const boundaries=[...new Set(points.filter(t=>t>=loop.start && t<=loop.extent))].sort((a,b)=>a-b);
  const result=[];
  for(let i=1;i<boundaries.length;i++){
    const start=boundaries[i-1],end=boundaries[i];
    if(end<=start)continue;
    const candidate=stages.filter(s=>s.started_elapsed_ms<=start && s.finished_elapsed_ms>=end).sort((a,b)=>b.category.rank-a.category.rank)[0];
    const category=candidate?.category || {name:"Overhead / other capture",key:"overhead",color:colors.overhead};
    const after=number(loop.end)&&start>=loop.end;
    const last=result.at(-1);
    if(last?.key===category.key && last.after===after)last.end=end;
    else result.push({...category,start,end,after});
  }
  return result;
}
function reductions(loop){
  return loop.events.filter(e=>["window_selected","control_selected"].includes(e.event)).flatMap(e=>(e.selections||[]).map(r=>`${e.event==="window_selected"?"Window":"Control"} round ${r.round}: ${r.candidate_count} → ${r.winner_count} candidates (${r.group_count} groups)`));
}
function charts(loops){
  if(!loops.length)return '<p>No precise loop boundaries were captured. Older aggregate timing is available in raw JSON.</p>';
  const scale=Math.max(1,...loops.map(loop=>loop.extent-loop.start));
  return `<p class="report-note">Every bar uses the same ${ms(scale)} scale. Solid segments end at the last CUA input command being sent. Hatched segments show action return, settling, and capture after that boundary; they are excluded from loop duration. Failed observations without an input command are labeled as attempts.</p>`+loops.map(loop=>{
    const pieces=segments(loop),totals=new Map();
    for(const p of pieces){const key=p.key+(p.after?"after":"");const value=totals.get(key)||{...p,duration:0};value.duration+=p.end-p.start;totals.set(key,value);}
    const clicks=loop.events.filter(e=>e.event==="native_call_sent"&&e.tool==="click");
    const done=number(loop.end);
    return `<div class="chart-loop"><div class="chart-loop-heading"><strong>Loop ${loop.iteration}${loop.boundary?.completed_action_loop===false?" · attempt stopped without input":""}</strong><span>${done?ms(loop.end-loop.start):"End boundary unavailable"} · ${loop.start.toFixed(2)} ms from worker start</span></div><div class="chart-track" role="img" aria-label="Loop ${loop.iteration} timing, ${done?ms(loop.end-loop.start):"incomplete"}">${pieces.map(p=>`<span class="chart-segment ${p.after?"after-boundary":""}" style="width:${(p.end-p.start)/scale*100}%;background:${p.color}" title="${esc(p.name)}: ${ms(p.end-p.start)}${p.after?" · after loop end":""}"></span>`).join("")}${clicks.map(e=>`<i class="chart-marker click-marker" style="left:${(e.elapsed_ms-loop.start)/scale*100}%" title="Click sent at ${ms(e.elapsed_ms)}"></i>`).join("")}${done?`<i class="chart-marker" style="left:${(loop.end-loop.start)/scale*100}%"><span class="chart-marker-label">${loop.boundary?.last_command_tool==="click"?"Click sent / ":""}${loop.boundary?.completed_action_loop===false?"attempt stopped":"loop end"}</span></i>`:""}</div><div class="chart-axis"><span>0 ms</span><span>${ms(scale/2)}</span><span>${ms(scale)}</span></div><div class="chart-legend">${[...totals.values()].map(p=>`<span><i style="background:${p.color}"></i>${esc(p.name)}: ${ms(p.duration)}${p.after?" (after end)":""}</span>`).join("")}${clicks.length?'<span>┆ Click sent</span>':""}<span>│ Loop end / stop</span></div><p class="report-note">${reductions(loop).map(esc).join(" · ")||"Candidate reduction trace unavailable."}</p></div>`;
  }).join("");
}
function workerTime(events,elapsed){
  const anchor=events.find(e=>e.timestamp&&number(e.elapsed_ms));
  return anchor?timestamp(new Date(new Date(anchor.timestamp).getTime()+elapsed-anchor.elapsed_ms).toISOString()):"Unavailable";
}
function timeline(events,loops,completion={}){
  if(!events.length)return '<p>No worker timing record is available.</p>';
  const rows=[];
  for(const loop of loops){
    rows.push({at:loop.start,title:`Loop ${loop.iteration} starts · get current state`,className:"boundary",duration:null});
    for(const segment of segments(loop))rows.push({at:segment.start,title:segment.name+(segment.after?" · after loop end":""),duration:segment.end-segment.start,finished:segment.end});
    for(const e of loop.events.filter(e=>e.event==="native_call_sent"&&["click","type_text","press_key","hotkey"].includes(e.tool)))rows.push({at:e.elapsed_ms,title:`CUA ${e.tool} sent · call ${e.call_id}`,duration:0});
    if(number(loop.end))rows.push({at:loop.end,title:`Loop ${loop.iteration} ${loop.boundary?.completed_action_loop===false?"attempt stopped":"ends"} · loop total ${ms(loop.end-loop.start)}`,className:"loop-end",duration:loop.end-loop.start});
  }
  // Preserve setup/teardown and other work outside the observed loops as explicit rows.
  for(const span of events.filter(e=>e.event==="timing_finished"&&number(e.started_elapsed_ms)&&!e.iteration&&!e.parent_span_id))rows.push({at:span.started_elapsed_ms,title:label(span.operation),duration:span.duration_ms,finished:span.finished_elapsed_ms});
  if(number(completion.through_report_duration_ms)&&number(completion.report_generation_duration_ms))rows.push({at:completion.through_report_duration_ms-completion.report_generation_duration_ms,title:"Report generation and final file writes · measured worker completion timing",duration:completion.report_generation_duration_ms});
  rows.sort((a,b)=>a.at-b.at||(a.className==="boundary"?-1:1));
  const total=Math.max(0,completion.through_report_duration_ms||0,...events.map(e=>e.elapsed_ms||0));
  return `<div class="report-table-wrap"><table class="report-table"><thead><tr><th>Worker UTC start / boundary</th><th>Operation / loop boundary</th><th>Operation duration</th><th>Grand elapsed at timestamp</th></tr></thead><tbody>${rows.map(r=>`<tr class="${r.className||""}"><td class="numeric">${esc(workerTime(events,r.at))}</td><td>${esc(r.title)}</td><td class="numeric">${r.duration===null?"—":ms(r.duration)}</td><td class="numeric">${ms(r.at)}</td></tr>`).join("")}<tr class="boundary"><td>${esc(workerTime(events,total))}</td><td>Worker captured record total</td><td class="numeric">${ms(total)}</td><td class="numeric">${ms(total)}</td></tr></tbody></table></div>`;
}
function jevCalls(events){
  return events.filter(e=>e.event==="jev_request").map(request=>({request,response:events.find(e=>["jev_response","jev_response_error","jev_request_unfinished"].includes(e.event)&&e.request_id===request.request_id)}));
}
function inputTokens(call){return call.response?.input_tokens??call.response?.body?.usage?.input_tokens;}
function savedRounds(run,serverEvents){
  const selections=run?.result?.pipeline?.selections;
  if(selections && Object.values(selections).some(value=>Array.isArray(value)&&value.length)){
    return Object.entries(selections).flatMap(([stage,rounds])=>(Array.isArray(rounds)?rounds:[]).map(round=>({stage,iteration:round.iteration,round})));
  }
  return serverEvents.filter(e=>Array.isArray(e.data?.rounds)&&["jev_selection","jev_window_selection","selection"].includes(e.stage)).flatMap(e=>e.data.rounds.map(round=>({stage:e.stage==="jev_window_selection"?"windows":"controls",iteration:round.iteration??e.data.iteration,round})));
}
function savedCallCards(saved){
  const cards=saved.flatMap(({stage,iteration,round})=>(round.api_calls||[]).map((call,index)=>{
    const tokens=call.input_tokens;
    return lazyDetails(`Saved API call · loop ${iteration??"?"} · ${stage==="windows"?"window":"control"} round ${round.round} · batch ${call.batch??index+1} · duration unavailable · ${number(tokens)?tokens.toLocaleString():"unknown"} input tokens · ${usd(number(tokens)?tokens*.042/1000000:null)}`,()=>`<p class="report-note">Recovered from the saved selection result. This contains the authored state/questions and returned judgments/usage. Exact HTTP envelopes and per-call transport timing are unavailable without the worker trace.</p>${raw("Saved Jev state",call.state)}${questionDetails(call.questions,call.answers)}${raw("Original stored API call trace",call)}`,"raw-details round-card");
  }));
  return cards.join("")||'<p>Jev API call evidence is unavailable. An absent archive does not establish zero calls, zero tokens, or zero cost.</p>';
}
function questionDetails(questions={},answers={}){
  return Object.entries(questions).map(([key,q])=>{
    const answer=answers[key];const probabilities=answer?.probabilities||{};
    return `<div class="question"><code>${esc(key)}</code><p>${esc(typeof q.instructions==="string"?q.instructions:JSON.stringify(q.instructions??q))}</p><div class="report-note">Chosen: <strong>${esc(answer?.choice??"Unavailable")}</strong> · confidence ${number(answer?.confidence)?answer.confidence.toFixed(4):"unavailable"}</div>${Object.entries(probabilities).map(([option,p])=>`<div class="probability ${option===answer?.choice?"winner":""}"><span title="${esc(JSON.stringify(q.criteria?.[option]??option))}">${esc(option)}${option===answer?.choice?" ✓":""}</span><div class="prob-track"><div class="prob-fill" style="width:${number(p)?Math.max(0,Math.min(1,p))*100:0}%"></div></div><strong>${number(p)?`${(p*100).toFixed(2)}%`:"—"}</strong></div>`).join("")}${raw("Question criteria, exact question and answer",{question:q,answer:answer??null})}</div>`;
  }).join("");
}
function callCards(calls){
  if(!calls.length)return '<p>No Jev API request was captured.</p>';
  return calls.map(({request:r,response:s},index)=>{
    const tokens=inputTokens({response:s}),cost=number(tokens)?tokens*.042/1000000:null;
    return lazyDetails(`API call ${index+1} · loop ${r.iteration??"?"} · ${r.stage==="windows"?"window":"control"} round ${r.round??"?"}${r.batch_count>1?` · batch ${r.batch}/${r.batch_count}`:""} · ${ms(s?.duration_ms)} · ${number(tokens)?tokens.toLocaleString():"unknown"} input tokens · ${usd(cost)}`,()=>`<div class="round-meta"><span>HTTP ${s?.status_code??"No complete response"}</span><span>${esc(r.body?.model||"Model unavailable")}</span><span>${Object.keys(r.body?.questions||{}).length} questions in this API call</span><span>Output token charge: $0</span></div>${raw("Exact Jev state",r.body?.state??null)}${questionDetails(r.body?.questions,s?.body?.answers)}${raw("Exact prepared request and transport timing",r)}${raw("Exact response / error and transport timing",s??{missing_capture:"No response captured"})}`,"raw-details round-card");
  }).join("");
}
function rounds(events,saved=[]){
  let selections=events.filter(e=>["window_selected","control_selected"].includes(e.event));
  const fallback=!selections.length;
  if(fallback)selections=saved.map(({stage,iteration,round})=>({event:stage==="windows"?"window_selected":"control_selected",iteration,selections:[round]}));
  const result=selections.flatMap(e=>(e.selections||[]).map(round=>{
    const duration=events.find(t=>t.event==="timing_finished"&&t.span_id===round.span_id)?.duration_ms;
    return `<div class="round-card"><h3>Loop ${e.iteration??"?"} · Jev ${e.event==="window_selected"?"window":"control"} round ${round.round}</h3><div class="round-meta"><span>${round.candidate_count} → ${round.winner_count} candidates</span><span>${round.group_count} groups</span><span>${round.api_calls?.length??"?"} API calls</span><span>${ms(duration)}</span></div>${raw("Round groups, chosen candidates, actions and all model judgments",round)}</div>`;
  }));
  return (fallback&&result.length?'<p class="report-note">These rounds come from saved server selection results. Timing is unavailable unless independently retained in the worker trace.</p>':"")+(result.join("")||'<p>No completed selection round was captured. Failed calls, if available, appear below.</p>');
}
function nativeCalls(events){
  const calls=events.filter(e=>e.event==="native_call_intent"||e.event==="native_call_sent");
  const seen=new Set();
  return calls.filter(e=>{if(seen.has(e.call_id))return false;seen.add(e.call_id);return true;}).map(e=>{
    const sent=events.find(x=>x.event==="native_call_sent"&&x.call_id===e.call_id);
    const returned=events.find(x=>x.event==="native_call_returned"&&x.call_id===e.call_id);
    const error=events.find(x=>x.event==="native_call_error"&&x.call_id===e.call_id);
    return `<details class="raw-details"><summary>Call ${e.call_id} · ${esc(e.tool||e.method)} · loop ${e.iteration??"—"} · ${ms(error?.duration_ms??returned?.duration_ms)} · ${error?"error":returned?"returned":sent?"return unavailable":"dispatch uncertain"}</summary>${raw("Command arguments and dispatch",{intent:e,dispatch:sent??null})}${raw("Native transport result and validation / execution error",{returned:returned??null,error:error??null,missing_capture:returned||error?undefined:"No native return captured; do not infer whether the action landed."})}</details>`;
  }).join("")||'<p>No native command capture is available.</p>';
}
function render(data){
  current=data;
  lazyContent.clear();
  const events=Array.isArray(data.worker_events)?data.worker_events:[];
  const loops=loopData(events),calls=jevCalls(events),run=data.run,request=data.loop;
  const serverEvents=data.events||[],saved=savedRounds(run,serverEvents);
  const savedCalls=saved.flatMap(item=>item.round.api_calls||[]);
  const tokenValues=savedCalls.length>calls.length?savedCalls.map(c=>c.input_tokens):calls.map(inputTokens);
  const knownTokens=tokenValues.filter(number),tokens=knownTokens.reduce((total,value)=>total+value,0);
  const declaredCallCount=run?.result?.timing?.jev_api_call_count??events.find(e=>e.event==="run_finished")?.timing?.jev_api_call_count;
  const observedCallCount=Math.max(calls.length,savedCalls.length);
  const countKnown=number(declaredCallCount)&&declaredCallCount>=observedCallCount;
  const callCountLabel=countKnown?String(declaredCallCount):observedCallCount?`${observedCallCount} captured`:"Unknown";
  const unknownTokens=!countKnown||declaredCallCount>tokenValues.length||knownTokens.length<tokenValues.length;
  const zeroVerified=countKnown&&declaredCallCount===0;
  const tokenLabel=knownTokens.length||zeroVerified?`${tokens.toLocaleString()}${unknownTokens?" + unknown":""}`:"Unknown";
  const costLabel=knownTokens.length||zeroVerified?`${usd(tokens*.042/1000000)}${unknownTokens?" + unknown":""}`:"Unknown";
  const total=Math.max(0,run?.result?.timing?.through_report_duration_ms||0,...events.map(e=>e.elapsed_ms||0));
  const status=run?.status||request.status,active=["running","queued","generating"].includes(status);
  const files=data.artifact?.files||[];
  const fileURL=(path)=>esc(scopedURL(`/api/runs/${encodeURIComponent(run?.id)}/files/${path.split("/").map(encodeURIComponent).join("/")}`));
  const modelResponse=serverEvents.find(e=>e.stage==="llm_response")?.data;
  const modelCost=modelResponse?.cost_estimate||data.openai_cost_estimate;
  const windowRounds=Math.max(events.filter(e=>e.event==="timing_finished"&&e.operation==="selection.round"&&e.stage==="windows").length,saved.filter(e=>e.stage==="windows").length);
  const controlRounds=Math.max(events.filter(e=>e.event==="timing_finished"&&e.operation==="selection.round"&&e.stage==="controls").length,saved.filter(e=>e.stage==="controls").length);
  const firstStep=run?.start_step??0,afterLastStep=run?.end_step??run?.steps?.length??0;
  const completedSteps=(run?.step_results||[]).filter(step=>step.status==="succeeded"&&step.step_index>=firstStep&&step.step_index<afterLastStep).length;
  const selectedSteps=afterLastStep-firstStep===1?`Step ${firstStep+1}`:`Steps ${firstStep+1}–${afterLastStep}`;
  $("report-content").innerHTML=`<div class="report-hero"><div><div class="eyebrow">${run?"DESKTOP RUN":"REQUEST & INSTRUCTION"} · ${esc(identity)}</div><h1>Every available step, in view.</h1><span class="report-badge ${["succeeded","ready"].includes(status)?"":"warning"}">${esc(label(status))}</span><p>${esc(request.prompt)}</p>${Array.isArray(run?.steps)&&run.steps.length?`<p class="report-note">${esc(selectedSteps)} of ${run.steps.length} authored steps · ${completedSteps}/${afterLastStep-firstStep} selected steps succeeded.</p>`:""}</div><div class="report-actions"><button id="refresh-report" class="report-refresh">Refresh record</button><a class="button primary" href="${esc(scopedURL(`${base}/download`))}">Download evidence ZIP ↓</a></div></div>
  ${active?'<div class="report-card warn"><p>This record is still in progress. Refresh to load new evidence. The ZIP contains everything available at download time.</p></div>':""}
  <div class="report-card ${data.missing_capture.length?"warn":""}"><h2>${data.missing_capture.length?"Capture gaps":"Available evidence captured"}</h2>${data.missing_capture.length?`<ul>${data.missing_capture.map(item=>`<li>${esc(item)}</li>`).join("")}</ul>`:'<p>The worker archive and server record are available. Capture completeness describes evidence availability; it does not establish task success.</p>'}<p class="report-note">${esc(data.timing_note)} Authentication headers and infrastructure credentials are intentionally excluded.</p></div>
  <div class="report-metrics"><div class="metric"><strong>${events.length||number(run?.result?.timing?.through_report_duration_ms)?ms(total):"Unavailable"}</strong><span>Worker captured duration</span></div><div class="metric"><strong>${loops.length||"Unavailable"}</strong><span>Precisely bounded loops / attempts</span></div><div class="metric"><strong>${callCountLabel}</strong><span>Jev API calls · ${windowRounds} captured window rounds · ${controlRounds} captured control rounds</span></div><div class="metric"><strong>${tokenLabel}</strong><span>Recorded Jev input tokens</span></div><div class="metric"><strong>${costLabel}</strong><span>Jev usage estimate · $0.042 / million input tokens</span></div></div>
  <div class="report-grid"><section class="report-card"><h2>Request and outcome</h2><dl><div><dt>Instruction</dt><dd>${esc(request.instruction||"No instruction returned")}</dd></div><div><dt>Created · server UTC</dt><dd>${esc(timestamp(request.created_at))}</dd></div>${run?`<div><dt>Run start · server UTC</dt><dd>${esc(timestamp(run.started_at))}</dd></div><div><dt>Run finish · server UTC</dt><dd>${esc(timestamp(run.finished_at))}</dd></div><div><dt>Worker revision</dt><dd>${esc(run.revision||"Unavailable")}</dd></div>`:""}<div><dt>Outcome</dt><dd>${esc(run?.error||run?.result?.reason||request.error||"Inspect recorded status and observations")}</dd></div></dl>${raw("Stored request and outcome",{loop:request,run})}</section><section class="report-card"><h2>Instruction model usage</h2><p>${esc(request.model)} · high reasoning</p><dl><div><dt>API duration</dt><dd>${ms(modelResponse?.duration_ms)}</dd></div><div><dt>Input tokens</dt><dd>${esc(modelResponse?.usage?.input_tokens??request.llm_usage?.input_tokens??"Unavailable")}</dd></div><div><dt>Output tokens</dt><dd>${esc(modelResponse?.usage?.output_tokens??request.llm_usage?.output_tokens??"Unavailable")}</dd></div><div><dt>Cost</dt><dd>${modelCost?`${usd(modelCost.cost_usd)} estimated`:"Unavailable; usage breakdown was not captured"}</dd></div></dl><p class="report-note">${esc(data.cost_note)}</p>${modelCost?`<p class="report-note">OpenAI estimate: $0.10/M uncached input, $0.01/M cached input, $0.50/M output. <a href="${esc(modelCost.source)}" target="_blank" rel="noopener">Official pricing</a>, checked ${esc(modelCost.checked_at)}. This is not a billed charge.</p>${raw("OpenAI usage and cost calculation",modelCost)}`:""}${serverEvents.filter(e=>["llm_request","llm_response"].includes(e.stage)).map(e=>raw(e.stage==="llm_request"?"Exact OpenAI request":"Exact OpenAI response",e)).join("")}</section></div>
  <section class="report-card"><h2>Dataflow and delivery</h2><div class="report-flow"><span>User request</span> → <span>OpenAI instruction</span> → <span>PostgreSQL saved job</span> → <span>Assigned worker</span> → <span>CUA observation</span> → <span>Jev selection rounds</span> → <span>CUA input</span> → <span>Observed outcome + evidence</span></div><p class="report-note">The worker checks for a queued job every two seconds while idle. This keeps delivery outbound from the desktop and makes reconnects simple, with an idle delivery delay of up to about two seconds plus request time. Server notifications could wake an authenticated persistent WebSocket or long-poll connection after the database commit; durable job claiming, ownership and reconnect recovery would still be required. This release preserves polling.</p>${serverEvents.map(e=>raw(`${timestamp(e.created_at)} · ${label(e.stage)} · ${e.message}`,e)).join("")}</section>
  <section class="report-card"><h2>Loop timing · shared scale</h2>${charts(loops)}</section>
  <section class="report-card"><h2>Worker timeline</h2><p class="report-note">UTC time and grand elapsed use the worker's monotonic clock anchor. Server delivery events above use the server clock. A loop ends at the final input command dispatch, before command return or the next state observation.</p>${timeline(events,loops,run?.result?.timing||{})}</section>
  <section class="report-card"><h2>Jev selection rounds</h2><p class="report-note">Candidates are available windows or controls. Up to 10 candidates form one group; each group has a question. All questions for a round are batched in one API call when they fit the existing request limit. Any split batches are shown explicitly.</p>${rounds(events,saved)}</section>
  <section class="report-card"><h2>Jev API calls · questions and probabilities</h2><p class="report-note">Each call shows available input tokens, estimated input charge and captured HTTP duration. Probability bars compare options within their question; they are not proof of task completion.</p>${calls.length?callCards(calls):savedCallCards(saved)}</section>
  <section class="report-card"><h2>Native CUA observations and commands</h2>${nativeCalls(events)}</section>
  <section class="report-card"><h2>Screenshots</h2><div class="screenshots">${files.filter(f=>f.path.toLowerCase().endsWith(".png")).map(f=>`<figure><a href="${fileURL(f.path)}" target="_blank" rel="noopener"><img src="${fileURL(f.path)}" loading="lazy" alt="${esc(f.path)}"></a><figcaption>${esc(f.path)}</figcaption></figure>`).join("")||'<p>No screenshots available in the uploaded evidence.</p>'}</div></section>
  <section class="report-card"><h2>Files and complete raw record</h2><ul class="report-files">${files.map(f=>`<li><a href="${fileURL(f.path)}">${esc(f.path)}</a> · ${f.size_bytes.toLocaleString()} bytes</li>`).join("")}</ul>${raw("Worker manifest",data.artifact?.manifest??null)}${raw("Complete worker chronological JSON",events)}${raw("Server record and capture metadata",{...data,worker_events:undefined})}</section>`;
  $("refresh-report").addEventListener("click",load);
  const attribution=document.createElement("p");attribution.className="report-attribution";
  attribution.textContent=`Created by ${personName(request.creator)}${run?` · Run started by ${personName(run.initiator)}${run.worker_id?` · Desktop ${run.worker_id}`:""}`:""}`;
  $("report-content").querySelector(".report-hero > div").append(attribution);
  $("report-content").hidden=false;$("report-loading").hidden=true;
}
async function load(){
  const generation=sessionGeneration;
  try{
    const session=await fetchJSON("/api/session");
    if(generation!==sessionGeneration)return;
    if(!session?.authenticated){requireLogin();return;}
    $("report-person").textContent=personName(session.user);
    $("report-company-name").textContent = `${session.company?.name || ""}${session.user?.is_devadmin ? " · Devadmin company context" : ""}`;
    const data=await fetchJSON(`${base}/report`);if(data&&generation===sessionGeneration)render(data);
  }
  catch(error){if(generation!==sessionGeneration)return;hideRecord();$("report-loading").hidden=false;$("report-loading").textContent=`Could not load this report: ${error.message} Open the report from the company page where you have access.`;}
}
void load();
