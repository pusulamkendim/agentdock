let state={agents:[],plans:[],workspaces:[],engines:{},quota:{}};
let live={plan:null,tasks:[],orchestrator:null,doctor:null,quota:{},mission_usage:{},server_time:0};
let selectedPlanId=null, selectedWorkspaceId=null, planning=false;
let missionAttachments=[], manualAttachments=[], inspectorTaskId=null;
let logTimer=null, stateTimer=null, liveTimer=null, inspectorTimer=null;
let missionConfigOpen=false;
let activityScrollState={stickToBottom:true};
const planReviewDisclosure=new Map();
const DEFAULT_ORCHESTRATOR='gpt-5.6-sol',DEFAULT_WORKER='gpt-5.6-luna',DEFAULT_ORCHESTRATOR_EFFORT='high',DEFAULT_WORKER_EFFORT='medium';
const $=id=>document.getElementById(id);
const decisionLabels={already_satisfied:'No work needed',answer_only:'Answer only',needs_user_input:'Needs your decision',blocked:'Blocked for safety or authority',execute:'Work required'};
const statusLabels={planning:'Planning',planned:'Plan ready',approved:'Approved',preflight:'Checking workspace',running:'Running',integrating:'Integrating',pausing:'Pausing',paused:'Paused',resuming:'Resuming',awaiting_apply:'Review changes',ready:'Complete',completed:'Complete',queued:'Queued',idle:'Ready',done:'Complete',attention:'Supervisor resolving issue',waiting_for_user:'Permission needed',waiting_for_permission:'Permission needed',blocked:'Permission needed',cancelled:'Cancelled',failed:'Supervisor resolving issue',paused_by_user:'Paused by you',waiting_for_orchestrator:'Waiting for orchestrator'};
function gitSetupNeeded(p){return p?.decision==='blocked'&&(p.workspace_snapshot||{}).classification==='NOT_GIT'}
function missionStatusLabel(p){return gitSetupNeeded(p)?'Workspace setup needed':statusLabel(p?.status)}
function missionStatusClass(p){return gitSetupNeeded(p)?'status-waiting-for-user':statusClass(p?.status)}
function missionDecisionLabel(p){return gitSetupNeeded(p)?'Git setup required':decisionLabel(p?.decision)}

async function api(path,opts={}){
  const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
  const raw=await r.text();
  let j={};
  try{j=raw?JSON.parse(raw):{}}catch{j={message:raw.slice(0,240)}}
  if(!r.ok)throw new Error(j.error||j.message||`${r.status} ${r.statusText||'Request failed'}`);
  return j;
}
function showDialog(id){const dialog=$(id);if(dialog&&!dialog.open)dialog.showModal()}
function pathId(id){return encodeURIComponent(String(id||'')).replace(/%3A/gi,':')}
function esc(s=''){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function short(s='',n=90){s=String(s);return s.length>n?s.slice(0,n-1)+'…':s}
function displayLogLine(line){return String(line||'').replace(/\bresume=[A-Za-z0-9._:-]{8,}/gi,'same conversation').replace(/\b(?:thread|turn)(?:Id)?(?:[=:]\s*|\s+)[A-Za-z0-9._:-]{8,}/gi,'same conversation')}
function statusClass(s){return `status-${String(s||'pending').replace(/_/g,'-').replace(/[^a-z-]/g,'')}`}
function decisionLabel(s){return decisionLabels[s]||s||'Not decided'}
function statusLabel(s){return statusLabels[s]||s||'Unknown'}
function missionTitle(p){return p?.title||deterministicTitle(p?.goal||'New mission')}
function deterministicTitle(text){const clean=String(text||'').replace(/\s+/g,' ').trim();if(!clean)return 'New mission';let first=clean.split(/(?<=[.!?])\s+/,1)[0].replace(/^(öncelikle|lütfen|please|şunu|bunu)\s+/i,'').trim();if(first.length>78)first=first.slice(0,78).replace(/\s+\S*$/,'').replace(/[ ,;:-]+$/,'');return first||'New mission'}
function jsonArray(v){if(Array.isArray(v))return v;try{const x=JSON.parse(v||'[]');return Array.isArray(x)?x:[]}catch{return []}}
function elapsed(start,finish,server){if(!start)return '—';const end=finish||server||Math.floor(Date.now()/1000);let sec=Math.max(0,end-start);if(sec<60)return `${sec}s`;const m=Math.floor(sec/60),ss=sec%60;if(m<60)return `${m}m ${ss}s`;return `${Math.floor(m/60)}h ${m%60}m`}
function deps(task){try{return JSON.parse(task.depends_json||'[]')}catch{return []}}
function pct(v){return v==null?'—':`${Math.round(v)}%`}
function resetLabel(ts){if(!ts)return 'reset —';const d=new Date(ts*1000);return `reset ${d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`}
function quotaBucket(label,b){if(!b)return `<div class="quota-item missing"><span>${label}</span><b>not reported</b></div>`;const rem=b.remaining_percent;return `<div class="quota-item"><span>${label}</span><div class="quota-meter"><i style="width:${Math.max(0,Math.min(100,rem??0))}%"></i></div><b>${pct(rem)} left</b><small>${resetLabel(b.resets_at)}</small></div>`}
const effortLabels={none:'None',low:'Low',medium:'Medium',high:'High',xhigh:'Extra High',max:'Max',ultra:'Ultra'};
function effortsFor(model){const dynamic=(state.engines?.codex?.models||[]).find(x=>x.slug===model)?.reasoning_levels;if(dynamic?.length)return dynamic;return model==='gpt-6-astra'||model==='auto-best'?['low','medium','high','xhigh','max']:['none','low','medium','high','xhigh','max']}
function syncEffortSelect(modelId,effortId,preferred){const model=$(modelId).value,sel=$(effortId),allowed=effortsFor(model),current=preferred||sel.value||'medium';sel.innerHTML=allowed.map(x=>`<option value="${x}">${effortLabels[x]}</option>`).join('');sel.value=allowed.includes(current)?current:(allowed.includes('high')?'high':allowed[0])}
function speedLabel(tier){return tier==='fast'?'Fast':'Standard'}
function configLabel(model,effort,tier){return `${model||'default'} · ${effortLabels[effort]||effort||'default'} · ${speedLabel(tier)}`}
function initModelControls(){syncEffortSelect('orchestratorModel','orchestratorEffort','high');syncEffortSelect('workerModel','workerEffort','medium');$('orchestratorModel').addEventListener('change',()=>syncEffortSelect('orchestratorModel','orchestratorEffort'));$('workerModel').addEventListener('change',()=>syncEffortSelect('workerModel','workerEffort'))}
function renderModelCatalog(){const models=(state.engines?.codex?.models||[]).filter(x=>x.slug&&/^gpt-/i.test(x.slug));if(!models.length)return;function fill(id,extra=[]){const sel=$(id),current=sel.value,items=[...extra,...models.filter(x=>!extra.some(e=>e.value===x.slug))];sel.innerHTML=items.map(x=>`<option value="${esc(x.value||x.slug)}">${esc(x.label||x.display_name||x.slug)}</option>`).join('');if(items.some(x=>(x.value||x.slug)===current))sel.value=current}fill('orchestratorModel',[{value:'auto-best',label:'Astra → Sol fallback'}]);fill('workerModel');syncEffortSelect('orchestratorModel','orchestratorEffort');syncEffortSelect('workerModel','workerEffort')}

function switchView(id){
  document.querySelectorAll('.nav,.view').forEach(x=>x.classList.remove('active'));
  const navId=id==='missionDetail'?'missions':id;
  document.querySelector(`.nav[data-view="${navId}"]`)?.classList.add('active');
  $(id)?.classList.add('active');
  const meta={
    overview:['CONTROL CENTER','Everything in one place.','Workspaces, missions and live agents without losing manual control.'],
    missions:['MISSIONS','All work, across every workspace.','Filter active, blocked and completed missions from one place.'],
    mission:['NEW MISSION','Delegate the outcome, keep the controls.','Choose a workspace, attach context and let the orchestrator build the execution graph.'],
    missionDetail:['MISSION','Live execution, fully inspectable.','Watch the swarm, open any agent, review diffs and take over manually when needed.'],
    agents:['AGENT PROFILES','Reusable worker roles.','Define specialized worker defaults without cluttering the mission flow.']
  }[id]||['AGENTDOCK','Mission control.',''];
  $('pageEyebrow').textContent=meta[0];$('pageTitle').textContent=meta[1];$('pageSubtitle').textContent=meta[2];
}

async function refreshState(){
  try{
    state=await api('/api/state');
    renderModelCatalog();
    if(!selectedWorkspaceId||!state.workspaces.some(w=>w.id===selectedWorkspaceId)) selectedWorkspaceId=state.workspaces[0]?.id||null;
    if(!selectedPlanId||!state.plans.some(p=>p.id===selectedPlanId)){
      const inWs=state.plans.filter(p=>!selectedWorkspaceId||p.workspace_id===selectedWorkspaceId);
      const active=inWs.find(p=>['running','planning','preflight','awaiting_apply','waiting_for_user','waiting_for_permission','attention'].includes(p.status))||inWs[0]||state.plans.find(p=>['running','planning','preflight','awaiting_apply','waiting_for_user','waiting_for_permission','attention'].includes(p.status))||state.plans[0];
      selectedPlanId=active?.id||null;if(active?.workspace_id)selectedWorkspaceId=active.workspace_id;
    }
    renderChrome();renderWorkspaceNav();renderWorkspaceSelect();renderWorkspaceBoard();renderOverview();renderGlobalMissions();renderAgents();renderHistory();
    if(selectedPlanId)await refreshLive();else renderNoMission();
  }catch(e){$('planMsg').textContent=e.message}
}
async function refreshLive(){if(!selectedPlanId)return;try{live=await api('/api/live/'+selectedPlanId);renderLive();if(inspectorTaskId)refreshInspector(false)}catch(e){if(String(e.message).includes('not found')){selectedPlanId=null;renderNoMission()}}}

function renderChrome(){const c=state.engines.codex||{},g=state.engines.git||{},q=state.quota||{};$('engineStatus').innerHTML=`<div class="engine-pill"><span>codex</span><span class="${c.installed?'on':'off'}">${c.installed?'online':'missing'}</span></div>${c.version?`<div class="tiny">${esc(c.version)}</div>`:''}<div class="engine-pill"><span>git</span><span class="${g.installed?'on':'off'}">${g.installed?'online':'missing'}</span></div>`;$('quotaStrip').innerHTML=q.status==='error'?`<div class="quota-error">quota unavailable</div>`:`${quotaBucket('5H',q.five_hour)}${quotaBucket('WEEK',q.weekly)}`}

function renderWorkspaceNav(){
  $('workspaceTree').innerHTML=(state.workspaces||[]).map(w=>{
    const plans=(state.plans||[]).filter(p=>p.workspace_id===w.id), running=plans.filter(p=>['running','planning','preflight','awaiting_apply','waiting_for_user','waiting_for_permission','pausing','resuming'].includes(p.status)).length, attention=plans.filter(p=>['attention','blocked','paused'].includes(p.status)).length;
    const count=running||attention||plans.length;
    return `<div class="workspace-node ${w.id===selectedWorkspaceId?'active':''}"><button class="workspace-button" onclick="selectWorkspace('${w.id}')"><span class="ws-dot ${running?'live':''}"></span><span><b>${esc(w.name)}</b><small>${running?running+' live':attention?attention+' attention':plans.length+' missions'}</small></span><em class="workspace-count">${count}</em></button></div>`
  }).join('')||'<div class="workspace-empty">No workspaces yet.</div>';
}
function renderWorkspaceSelect(){
  const sel=$('workspaceSelect'),old=selectedWorkspaceId||sel.value;
  sel.innerHTML=(state.workspaces||[]).map(w=>`<option value="${w.id}">${esc(w.name)}</option>`).join('');
  if(old&&state.workspaces.some(w=>w.id===old))sel.value=old;
  selectedWorkspaceId=sel.value||selectedWorkspaceId;
  const w=state.workspaces.find(x=>x.id===selectedWorkspaceId);
  $('workspacePath').textContent=w?.repo_path||'Add a workspace first';
  const filter=$('missionFilterWorkspace');
  if(filter){const current=filter.value;filter.innerHTML='<option value="">All workspaces</option>'+(state.workspaces||[]).map(w=>`<option value="${w.id}">${esc(w.name)}</option>`).join('');if(current&&state.workspaces.some(w=>w.id===current))filter.value=current;}
}
function renderOverview(){
  const plans=state.plans||[], workspaces=state.workspaces||[];
  const active=plans.filter(p=>['running','planning','preflight','planned','approved','awaiting_apply','waiting_for_user','waiting_for_permission','pausing','paused','resuming'].includes(p.status));
  const attention=plans.filter(p=>['attention','failed','blocked'].includes(p.status));
  const taskCount=active.reduce((n,p)=>n+(p.tasks||[]).filter(t=>['running','integrating'].includes(t.status)).length,0);
  $('overviewSummary').innerHTML=[
    ['ACTIVE MISSIONS',active.length,'across all workspaces',''],
    ['RUNNING AGENTS',taskCount,'workers currently executing',''],
    ['NEEDS ATTENTION',attention.length,'missions waiting for you','attention'],
    ['WORKSPACES',workspaces.length,'registered repositories','']
  ].map(([label,value,sub,cls])=>`<div class="summary-card ${cls}"><span>${label}</span><b>${value}</b><small>${sub}</small></div>`).join('');
  const visible=[...active,...attention.filter(x=>!active.some(a=>a.id===x.id))].slice(0,8);
  $('overviewMissions').innerHTML=visible.length?visible.map(p=>overviewMissionCard(p)).join(''):'<div class="empty-soft">No missions are active. Start a new mission when you are ready.</div>';
  $('overviewWorkspaces').innerHTML=workspaces.length?workspaces.map(w=>overviewWorkspaceCard(w)).join(''):'<div class="empty-soft">Add a repository workspace to begin.</div>';
}
function overviewMissionCard(p){
  const w=(state.workspaces||[]).find(x=>x.id===p.workspace_id),tasks=p.tasks||[],running=tasks.filter(t=>['running','integrating'].includes(t.status)).length,done=tasks.filter(t=>['done','executed'].includes(t.status)).length;
  return `<button class="overview-mission-card" onclick="selectPlan('${p.id}')"><span class="mission-state ${statusClass(p.status)}"></span><span class="mission-main"><b>${p.demo_mode?'<span class="demo-badge">DEMO</span> ':''}${esc(short(missionTitle(p),72))}</b><small>${esc(w?.name||'workspace')} · ${esc(p.decision?decisionLabel(p.decision):p.orchestrator_model)} · ${running} running · ${done}/${tasks.length||0} done</small></span><span class="mission-meta ${statusClass(p.status)}">${esc(statusLabel(p.status))}</span></button>`
}
function overviewWorkspaceCard(w){
  const plans=(state.plans||[]).filter(p=>p.workspace_id===w.id),active=plans.filter(p=>['running','planning','preflight','waiting_for_user','waiting_for_permission','pausing','resuming'].includes(p.status)).length,attention=plans.filter(p=>['attention','blocked','paused'].includes(p.status)).length;
  return `<button class="overview-workspace-card" onclick="selectWorkspace('${w.id}')"><h4>${esc(w.name)}</h4><code>${esc(w.repo_path)}</code><div class="workspace-card-stats"><span><b>${active}</b> live</span><span><b>${attention}</b> attention</span><span><b>${plans.length}</b> total</span></div></button>`
}
function renderGlobalMissions(){
  const wsFilter=$('missionFilterWorkspace')?.value||'',statusFilter=$('missionFilterStatus')?.value||'';
  let plans=[...(state.plans||[])];if(wsFilter)plans=plans.filter(p=>p.workspace_id===wsFilter);
  if(statusFilter==='active')plans=plans.filter(p=>['running','planning','preflight','planned','approved','awaiting_apply','waiting_for_user','waiting_for_permission','pausing','paused','resuming'].includes(p.status));
  else if(statusFilter==='attention')plans=plans.filter(p=>['attention','failed','blocked','waiting_for_user','waiting_for_permission','paused'].includes(p.status));
  else if(statusFilter==='waiting_for_user')plans=plans.filter(p=>['waiting_for_user','waiting_for_permission'].includes(p.status));
  else if(statusFilter==='blocked')plans=plans.filter(p=>['blocked','failed','attention'].includes(p.status));
  else if(statusFilter==='done')plans=plans.filter(p=>p.status==='done');
  plans.sort((a,b)=>(b.created_at||0)-(a.created_at||0));
  $('globalMissionBoard').innerHTML=plans.length?plans.map(p=>globalMissionRow(p)).join(''):'<div class="empty-soft">No missions match this filter.</div>';
}
function globalMissionRow(p){
  const w=(state.workspaces||[]).find(x=>x.id===p.workspace_id),tasks=p.tasks||[],running=tasks.filter(t=>['running','integrating'].includes(t.status)).length,queued=tasks.filter(t=>t.status==='pending').length,done=tasks.filter(t=>['done','executed'].includes(t.status)).length;
  return `<div class="global-mission-row" onclick="selectPlan('${p.id}')"><span class="mission-state ${statusClass(p.status)}"></span><div class="global-mission-title"><b>${p.demo_mode?'<span class="demo-badge">DEMO</span> ':''}${esc(short(missionTitle(p),110))}</b><small>${running} running · ${queued} queued · ${done}/${tasks.length||0} done${p.decision?` · ${esc(decisionLabel(p.decision))}`:''}</small></div><div class="global-mission-workspace">${esc(w?.name||'—')}</div><div class="global-mission-runtime">${esc(p.orchestrator_model)} → ${esc(p.worker_model)}</div><div class="global-mission-time">${elapsed(p.started_at,p.finished_at)}</div><div class="global-mission-status ${statusClass(p.status)}">${esc(statusLabel(p.status))}</div></div>`
}

function renderWorkspaceBoard(){
  $('workspaceBoard').innerHTML=(state.workspaces||[]).map(w=>{const plans=(state.plans||[]).filter(p=>p.workspace_id===w.id);return `<section class="workspace-card"><div class="workspace-card-head"><div><span class="workspace-kicker">${esc(w.default_branch||'git')}</span><h3>${esc(w.name)}</h3><code>${esc(w.repo_path)}</code></div><button class="ghost compact" onclick="selectWorkspace('${w.id}',true)">New mission</button></div><div class="workspace-stats"><span><b>${plans.filter(p=>['running','planning','preflight','approved','awaiting_apply','waiting_for_user','waiting_for_permission','pausing','paused','resuming'].includes(p.status)).length}</b>live</span><span><b>${plans.filter(p=>['attention','blocked','failed'].includes(p.status)).length}</b>attention</span><span><b>${plans.filter(p=>p.status==='done').length}</b>done</span></div><div class="mission-list">${plans.map(p=>missionListCard(p)).join('')||'<div class="empty-state">No missions in this workspace.</div>'}</div></section>`}).join('')||'<div class="empty-terminal"><span>agentdock@local:~$</span> add a workspace to begin_</div>';
}
function missionListCard(p){const tasks=p.tasks||[],run=tasks.filter(t=>['running','integrating'].includes(t.status)).length,queue=tasks.filter(t=>t.status==='pending').length,done=tasks.filter(t=>t.status==='done').length;return `<button class="mission-list-card" onclick="selectPlan('${p.id}')"><span class="mission-state ${missionStatusClass(p)}"></span><span class="mission-list-main"><b>${p.demo_mode?'<span class="demo-badge">DEMO</span> ':''}${esc(short(missionTitle(p),80))}</b><small>${esc(p.decision?missionDecisionLabel(p):p.orchestrator_model)} · ${run} running · ${queue} queued · ${done} done</small></span><span class="mission-list-status ${missionStatusClass(p)}">${esc(missionStatusLabel(p))}</span></button>`}

function renderNoMission(){$('liveTitle').textContent=planning?'Orchestrator is planning…':'No mission selected';$('runStats').innerHTML='';$('missionDetails').hidden=true;$('missionDetailsBody').innerHTML='';$('missionBar').className='mission-bar empty';$('missionBar').innerHTML=planning?'<span>Sol/Astra is analyzing the mission and workspace…</span>':'<span>Select or create a mission to see the swarm.</span>';$('supervisorDock').innerHTML=planning?planningTerminal():'';$('liveGrid').innerHTML=planning?'':`<div class="empty-terminal"><span>agentdock@local:~$</span> waiting for mission_</div>`;$('usagePanel').innerHTML='';$('decisionPanel').hidden=true;$('decisionPanel').innerHTML='';$('planReviewPanel').hidden=true;$('planReviewPanel').innerHTML=''}
function planningTerminal(){return `<article class="agent-terminal running"><div class="agent-titlebar"><span class="status-led"></span><span class="agent-name">orchestrator</span><span class="agent-index">planning</span><span class="agent-model">${esc(configLabel($('orchestratorModel').value,$('orchestratorEffort').value,$('orchestratorTier').value))}</span></div><div class="agent-task"><div class="task-path">mission / decomposition</div><strong>${esc(short($('goal').value,120))}</strong><p>Finding independent work, dependencies and safe parallel write boundaries.</p></div><div class="mini-terminal"><div class="term-line system">$ inspect goal and workspace</div><div class="term-line system">$ build dependency graph</div><div class="term-line"><span class="term-caret">▋</span> orchestrating...</div></div><div class="agent-footer"><span>read-only</span><span class="footer-spacer"></span><span>Sol/Astra</span></div></article>`}

function renderDecisionPanel(p){
  const box=$('decisionPanel');if(!box)return;
  // Mission classification is control-plane detail. The user-facing surface
  // is the persistent supervisor conversation and, when present, Plan Review.
  box.hidden=true;box.innerHTML='';return;
  const decision=p.decision||'',tasks=live.tasks||[],evidence=jsonArray(p.evidence||p.evidence_json),questions=jsonArray(p.questions||p.questions_json);
  if(!decision||decision==='execute'){box.hidden=true;box.innerHTML='';return}
  const noExecution=!tasks.length&&decision!=='execute';
  const missingGit=(p.workspace_snapshot||{}).classification==='NOT_GIT';
  const setupNeeded=decision==='blocked'&&missingGit;
  const response=setupNeeded?'Initialize a local Git repository to enable isolated write tasks. AgentDock will create only Git metadata and an empty base commit; existing workspace files will not be staged or committed. Planning then continues in the same orchestrator conversation.':p.final_response||p.summary||'';
  const canForceExecute=noExecution&&!['needs_user_input','blocked'].includes(decision);
  const initializeGit=missingGit?`<button class="primary compact" onclick="preflightAction('${p.id}','initialize_git',false)">Initialize Git repository</button>`:'';
  const actionButtons=noExecution?`<div class="decision-actions">${initializeGit}${canForceExecute?`<button class="secondary compact" onclick="reconsiderPlan('${p.id}','force_execute')">Create execution plan anyway</button>`:''}<button class="secondary compact" onclick="reconsiderPlan('${p.id}','reconsider')">Ask orchestrator to reconsider</button><button class="secondary compact" onclick="reconsiderPlan('${p.id}','verify')">Run verification again</button></div>`:'';
  const resultTitle=setupNeeded?'Workspace setup needed':decision==='blocked'?'Execution blocked':decision==='needs_user_input'?'Waiting for your decision':noExecution?'No execution needed':decisionLabel(decision);
  const displayedReason=setupNeeded?'This mission needs to write repository files, so AgentDock needs a local Git base for isolated execution.':p.decision_reason||'The orchestrator has classified this mission.';
  const badgeClass=setupNeeded?'status-needs-user-input':statusClass(decision),badgeLabel=setupNeeded?'Git setup required':decisionLabel(decision);
  const questionHtml=questions.length?`<div class="decision-questions"><span>YOUR DECISION</span>${questions.map(x=>`<p>${esc(x)}</p>`).join('')}</div>`:'';
  const evidenceHtml=evidence.length?`<div class="decision-evidence"><span>VERIFIED WITH</span><ul>${evidence.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:'';
  box.hidden=false;box.innerHTML=`<div class="decision-head"><div><span class="eyebrow">${setupNeeded?'WORKSPACE PREPARATION':'MISSION DECISION'}</span><h3>${esc(resultTitle)}</h3><p>${esc(displayedReason)}</p></div><span class="decision-badge ${badgeClass}">${esc(badgeLabel)}</span></div><div class="decision-body">${response?`<div class="decision-response">${esc(response).replace(/\n/g,'<br>')}</div>`:''}${evidenceHtml}${questionHtml}</div>${actionButtons}`;
}
function renderConsultationPanel(p){
  const box=$('decisionPanel'),pending=p.pending_question||{};
  if(!box||!['waiting_for_user','waiting_for_permission'].includes(p.status)||!['execution_question','permission_request'].includes(pending.kind))return;
  if(pending.kind==='permission_request'){
    box.hidden=false;
    box.innerHTML=`<section class="consultation-card permission-card"><div class="consultation-head"><div><span class="eyebrow">PERMISSION REQUEST</span><h3>${esc(pending.question||'The supervisor needs additional authority.')}</h3><p>${esc(pending.reason||'Open the supervisor and answer naturally to continue the same mission.')}</p></div><button class="primary compact" onclick="openOrchestratorInspector()">Talk to supervisor ↵</button></div></section>`;
    return;
  }
  const evidence=Array.isArray(pending.evidence)?pending.evidence:[],opts=Array.isArray(pending.options)?pending.options:[];
  const options=opts.length?'<label class="consultation-option"><span>OPTION</span><select id="consultationOption"><option value="">Choose an option or write your own</option>'+opts.map(x=>'<option value="'+esc(x)+'">'+esc(x)+'</option>').join('')+'</select></label>':'';
  const html='<section class="consultation-card"><div class="consultation-head"><div><span class="eyebrow">INFORMATION NEEDED</span><h3>'+esc(pending.question||'The worker needs a decision.')+'</h3><p>'+esc(pending.reason||'The worker stopped before making an unsafe assumption.')+'</p></div><span class="decision-badge status-needs-user-input">Waiting for your answer</span></div><div class="consultation-evidence"><span>WHY THIS WAS ASKED</span>'+(evidence.length?'<ul>'+evidence.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'<p>No additional evidence was reported.</p>')+'</div>'+options+'<textarea id="consultationAnswer" class="consultation-answer" placeholder="Add the missing product or factual information…"></textarea><input id="consultationFiles" class="consultation-files" type="file" accept="image/*" multiple><div class="consultation-actions"><button class="primary compact" onclick="submitConsultationAnswer(\''+p.id+'\',\''+(pending.consultation_id||'')+'\')">Use this information</button><button class="secondary compact" onclick="leaveConsultationField(\''+p.id+'\',\''+(pending.consultation_id||'')+'\')">Leave this field out</button><button class="text-button danger" onclick="cancelConsultationMission(\''+p.id+'\')">Cancel mission</button></div></section>';
  box.hidden=false;box.insertAdjacentHTML('beforeend',html);
}
function modelChoices(selected,orchestrator=false){
  const models=(state.engines?.codex?.models||[]).filter(x=>x.slug&&/^gpt-/i.test(x.slug)).map(x=>({value:x.slug,label:x.display_name||x.slug}));
  if(orchestrator)models.unshift({value:'auto-best',label:'Astra → Sol fallback'});
  const fallback=[orchestrator?DEFAULT_ORCHESTRATOR:DEFAULT_WORKER,selected].filter(Boolean).map(value=>({value,label:value}));
  const merged=[...fallback,...models.filter(x=>!fallback.some(y=>y.value===x.value))];
  return merged.map(x=>`<option value="${esc(x.value)}" ${x.value===selected?'selected':''}>${esc(x.label)}</option>`).join('');
}
function missionConfigMarkup(p){
  const configOpen=missionConfigOpen?' open':'';
  return `<details class="mission-config"${configOpen} ontoggle="setMissionConfigOpen(this)"><summary>Runtime settings</summary><div class="mission-config-panel"><div class="mission-config-group"><span class="mission-config-label">ORCHESTRATOR</span><div class="mission-config-fields"><select id="missionOrchestratorModel" class="premium-select" onchange="syncEffortSelect('missionOrchestratorModel','missionOrchestratorEffort')">${modelChoices(p.orchestrator_model||DEFAULT_ORCHESTRATOR,true)}</select><select id="missionOrchestratorEffort" class="premium-select">${effortsFor(p.orchestrator_model||DEFAULT_ORCHESTRATOR).map(x=>`<option value="${x}" ${(p.orchestrator_effort||DEFAULT_ORCHESTRATOR_EFFORT)===x?'selected':''}>${effortLabels[x]||x}</option>`).join('')}</select><select id="missionOrchestratorTier" class="premium-select"><option value="default" ${p.orchestrator_tier!=='fast'?'selected':''}>Standard</option><option value="fast" ${p.orchestrator_tier==='fast'?'selected':''}>Fast</option></select></div></div><div class="mission-config-group"><span class="mission-config-label">WORKERS</span><div class="mission-config-fields"><select id="missionWorkerModel" class="premium-select" onchange="syncEffortSelect('missionWorkerModel','missionWorkerEffort')">${modelChoices(p.worker_model||DEFAULT_WORKER)}</select><select id="missionWorkerEffort" class="premium-select">${effortsFor(p.worker_model||DEFAULT_WORKER).map(x=>`<option value="${x}" ${(p.worker_effort||DEFAULT_WORKER_EFFORT)===x?'selected':''}>${effortLabels[x]||x}</option>`).join('')}</select><select id="missionWorkerTier" class="premium-select"><option value="default" ${p.worker_tier!=='fast'?'selected':''}>Standard</option><option value="fast" ${p.worker_tier==='fast'?'selected':''}>Fast</option></select></div></div><div class="mission-config-bottom"><label>PARALLEL<select id="missionMaxParallel" class="premium-select">${[1,2,3,4,5,6,7,8].map(x=>`<option value="${x}" ${Number(p.max_parallel||4)===x?'selected':''}>${x}</option>`).join('')}</select></label><button class="secondary compact" onclick="saveMissionConfig('${p.id}')">Apply to remaining work</button></div></div></details>`;
}
function setMissionConfigOpen(el){missionConfigOpen=!!el.open}
function missionActions(p,tasks){
  const hasTasks=tasks.length>0;
  let primary='';
  if(p.status==='pausing')primary='<button class="secondary compact" disabled>Pausing…</button>';
  else if(p.status==='planning'||p.status==='running'||p.status==='preflight'||p.status==='resuming')primary=`<button class="secondary compact" onclick="event.stopPropagation();pausePlan('${p.id}')">Pause mission</button>`;
  else if(p.status==='paused')primary=`<button class="primary compact start-btn" onclick="event.stopPropagation();resumePlan('${p.id}')">Resume mission ↵</button>`;
  else if(p.status==='attention'&&p.apply_status==='failed')primary=`<button class="primary compact start-btn" onclick="event.stopPropagation();applyPlan('${p.id}')">Supervisor retry ↵</button>`;
  else if(p.status==='attention'||p.status==='failed')primary=`<button class="primary compact start-btn" onclick="event.stopPropagation();resumePlan('${p.id}')">Supervisor continue ↵</button>`;
  else if(p.status==='waiting_for_permission')primary=`<button class="primary compact start-btn" onclick="event.stopPropagation();openOrchestratorInspector()">Talk to supervisor ↵</button>`;
  else if(p.status==='done')primary=`<button class="secondary compact" onclick="event.stopPropagation();reopenPlan('${p.id}')">Reopen mission</button>`;
  else if(p.status==='approved'&&hasTasks)primary=`<button class="primary compact start-btn" onclick="event.stopPropagation();runPlan('${p.id}')">Start mission ↵</button>`;
  else if(p.status==='awaiting_apply')primary=`<button class="primary compact start-btn" onclick="event.stopPropagation();applyPlan('${p.id}')">Apply changes ↵</button>`;
  const review=p.status==='awaiting_apply'?`<button class="secondary compact" onclick="event.stopPropagation();showPlanDiff('${p.id}')">Review diff</button>`:'';
  return `${review}${primary}<details class="mission-more" onclick="event.stopPropagation()"><summary>More</summary><div><button class="text-button" onclick="restartAsNew('${p.id}')">Restart as new mission</button></div></details>`;
}
function renderLive(){
  const p=live.plan,tasks=live.tasks||[],counts={running:0,pending:0,paused:0,done:0,failed:0};
  tasks.forEach(t=>{if(['running','resuming','integrating'].includes(t.status))counts.running++;else if(t.status==='pending')counts.pending++;else if(['paused_by_user','pausing'].includes(t.status))counts.paused++;else if(['done','executed'].includes(t.status))counts.done++;else if(['failed','blocked','cancelled','attention','waiting_for_permission'].includes(t.status))counts.failed++});
  $('liveTitle').textContent=missionTitle(p);
  const pausedForPreflight=['waiting_for_user','waiting_for_permission','blocked'].includes(p.status)&&['waiting_for_user','waiting_for_permission','blocked'].includes(p.preflight_status);
  $('runStats').innerHTML=`<span class="stat"><strong>${counts.running}</strong>running</span><span class="stat"><strong>${counts.pending}</strong>${pausedForPreflight||counts.paused?'paused':'queued'}</span><span class="stat"><strong>${counts.done}</strong>done</span>${counts.failed?`<span class="stat status-failed"><strong>${counts.failed}</strong>issues</span>`:''}`;
  $('missionBar').className='mission-bar';
  const ws=(state.workspaces||[]).find(w=>w.id===p.workspace_id);
  const stageLabel=p.status==='planned'?'Plan ready for review':p.status==='approved'?'Approved · waiting to start':p.status==='awaiting_apply'?'Review changes':missionStatusLabel(p);
  $('missionBar').innerHTML=`<span class="mission-state ${missionStatusClass(p)}"></span><span class="mission-name">${esc(ws?.name||'workspace')} · ${esc(stageLabel)}</span>${p.error&&!['waiting_for_user','waiting_for_permission'].includes(p.status)&&!gitSetupNeeded(p)?`<span class="mission-chip status-attention" title="${esc(p.error)}">⚠ ${esc(short(p.error,80))}</span>`:''}${p.demo_mode?'<span class="demo-badge">DEMO · NO QUOTA</span>':''}<span class="mission-spacer"></span>${(p.attachments||[]).length?`<span class="mission-chip">${p.attachments.length} attachments</span>`:''}<button class="text-button" onclick="showDocs('${p.id}')">docs</button>${missionConfigMarkup(p)}<span class="mission-actions">${missionActions(p,tasks)}</span>`;
  const details=$('missionDetails');details.hidden=false;$('missionDetailsBody').innerHTML=`<div><span>ORIGINAL PROMPT</span><p>${esc(p.goal||'')}</p></div><div><span>WORKSPACE</span><p>${esc(ws?.name||'workspace')} · ${esc(p.workspace||'')}</p></div>`;
  renderUsage();
  renderDecisionPanel(p);
  renderConsultationPanel(p);
  renderPlanReview(p,tasks);
  const sorted=[...tasks].sort((a,b)=>(a.seq??0)-(b.seq??0));
  const orch=renderOrchestrator(live.orchestrator||{});
  const showWorkers=tasks.length>0&&!['planned','approved'].includes(p.status);
  $('supervisorDock').innerHTML=orch;
  $('liveGrid').innerHTML=showWorkers&&sorted.length?sorted.map(renderTerminal).join(''):(['planning','preflight'].includes(p.status)?'':showWorkers?'<div class="empty-terminal"><span>agentdock@local:~$</span> no tasks_':'');
}
function renderUsage(){const p=live.plan||{},q=live.quota||state.quota||{},mu=live.mission_usage||{};if(p.demo_mode){$('usagePanel').innerHTML=`<div class="usage-card"><span>CODEX QUOTA</span><div>${quotaBucket('5H',q.five_hour)}${quotaBucket('WEEK',q.weekly)}</div></div><div class="usage-card mission-usage demo-usage"><span>MISSION USAGE · DEMO</span><b>0% quota used</b><small>Local simulation only. No Codex model call is made.</small></div>`;return}function delta(d){if(!d)return '—';if(d.reset_during_mission)return 'window reset';const v=d.used_percent_delta;return `${v>0?'+':''}${v}% used`}$('usagePanel').innerHTML=`<div class="usage-card"><span>CODEX QUOTA</span><div>${quotaBucket('5H',q.five_hour)}${quotaBucket('WEEK',q.weekly)}</div></div><div class="usage-card mission-usage"><span>MISSION USAGE${mu.live?' · LIVE':''}</span><b>5H ${delta(mu.five_hour_delta)}</b><b>WEEK ${delta(mu.weekly_delta)}</b><small>Official snapshots; task attribution is not guessed.</small></div>`}
function renderDoctor(d){
  const p=live.plan||{},r=d.report||{},logs=d.recent_logs||[],status=d.status||p.preflight_status||'idle',repairs=(r.repairs||[]).length,blockers=(r.blockers||[]).length,paths=r.affected_paths||[],options=r.action_options||[];
  const warnings=(r.warnings||[]).length;
  if((status==='ready'||status==='idle')&&!repairs&&!blockers&&!warnings)return '';
  if(status==='ready'){
    const summary=[repairs?`${repairs} automatic change${repairs===1?'':'s'}`:'',warnings?`${warnings} warning${warnings===1?'':'s'}`:'',blockers?`${blockers} blocker${blockers===1?'':'s'}`:''].filter(Boolean).join(' · ')||'No issues';
    const detail=(r.warnings||[]).map(x=>`<div class="term-line doctor-warning">! ${esc(short(x,240))}</div>`).join('')||'<div class="term-line system">$ read-only checks completed without changing files</div>';
    return `<details class="preflight-compact"><summary><span class="status-led"></span><span class="preflight-compact-title"><b>Workspace check complete</b><small>Read-only · ${esc(summary)} · no files changed</small></span><span class="preflight-expand">Details</span></summary><div class="preflight-compact-body">${detail}<button class="text-button" onclick="event.preventDefault();logs('doctor:${p.id}','preflight')">Raw log</button></div></details>`;
  }
  const logLines=logs.length?logs.slice(-8).map(l=>`<div class="term-line ${l.stream==='stderr'?'doctor-blocker':l.stream==='supervisor'?'doctor-repair':esc(l.stream)}">${l.stream==='stderr'?'! ':l.stream==='supervisor'?'> ':'$ '}${esc(short(displayLogLine(l.line),240))}</div>`).join(''):'<div class="term-line system">$ waiting for preflight_</div>';
  const warningLines=(r.warnings||[]).map(x=>`<div class="term-line doctor-warning">! ${esc(short(x,240))}</div>`).join('');
  const lines=logLines+warningLines;
  const title=status==='running'?'Checking workspace':status==='ready'?'Workspace checks complete':status==='waiting_for_user'?'Waiting for your workspace choice':status==='blocked'?'Preflight stopped for safety':'Read-only workspace check';
  const pathPicker=paths.length?`<div class="preflight-paths"><div class="preflight-path-label">AFFECTED FILES · select paths for an action</div>${paths.map((x,i)=>`<label class="preflight-path"><input type="checkbox" value="${esc(x)}" checked><span>${esc(x)}</span></label>`).join('')}</div>`:'';
  const actionButtons=options.length&&['waiting_for_user','blocked'].includes(status)?`<div class="preflight-actions">${options.map(o=>`<button class="${o.id==='cancel'?'text-button danger':'secondary compact'}" ${o.disabled?'disabled':''} onclick="preflightAction('${p.id}','${esc(o.id)}',${o.requires_paths?'true':'false'})" title="${esc(o.description||'')}">${esc(o.label)}</button>`).join('')}</div>`:'';
  return `<article class="agent-terminal doctor-terminal ${esc(status)}"><div class="agent-titlebar"><span class="status-led"></span><span class="agent-name">PREFLIGHT / WORKSPACE CHECK</span><span class="agent-index">read-only</span><span class="agent-model">no files changed</span></div><div class="agent-task"><div class="task-path">workspace / safety</div><strong>${esc(title)}</strong><p>Preflight only reports state. No file, Git index, ignore rule or lock is changed automatically.</p></div><div class="mini-terminal">${lines}</div>${pathPicker}${actionButtons}<div class="agent-footer"><span>${repairs} automatic changes</span><span>${warnings} warnings</span><span class="${blockers?'status-failed':''}">${blockers} blockers</span><span class="footer-spacer"></span><button class="text-button" onclick="logs('doctor:${p.id}','preflight')">Raw log</button></div></article>`
}
function renderOrchestrator(o){
  const p=live.plan||{},logs=o.recent_logs||[],turnStatus=o.turn_status||p.orchestrator_turn_status||'',activeTurn=['queued','running'].includes(turnStatus),status=activeTurn?'running':(['attention','paused'].includes(turnStatus)?turnStatus:'idle');
  const isWorking=activeTurn;
  const recent=logs.length?logs.slice(-8).map(l=>`<div class="term-line ${esc(l.stream)}">${l.stream==='stderr'?'! ':l.stream==='supervisor'?'> ':'$ '}${esc(short(displayLogLine(l.line),260))}</div>`).join(''):`<div class="term-line system">$ starting orchestrator...</div>`;
  const noTask=p.decision&&!(live.tasks||[]).length;
  const permission=['waiting_for_user','waiting_for_permission','attention','failed'].includes(p.status);
  const waiting=noTask?`${decisionLabel(p.decision)} · ${short(p.decision_reason||'',180)}`:p.status==='planned'?'Plan ready. You can discuss or revise it here before approval.':p.status==='approved'?'Plan approved. Ready to start.':permission?'I am available now. Ask me to diagnose, revise permissions or continue the mission.':p.status==='running'&&!activeTurn?'Workers are running. Message me at any time to inspect or change the mission.':'';
  const working=isWorking?`<div class="classic-working"><i></i><b>Working</b><span>(${esc(orchestratorPurposeLabels[latestOrchestratorPurpose(o)]||'orchestrator turn')} · click to inspect)</span></div>`:'';
  const title=noTask?'Mission supervisor':activeTurn?(orchestratorPurposeLabels[latestOrchestratorPurpose(o)]||'Working on mission coordination'):permission?'Ready to resolve the mission issue':p.status==='planning'?'Preparing mission context':p.status==='planned'?'Plan ready for review':p.status==='approved'?'Waiting for execution':p.status==='running'?'Monitoring task graph':'Mission supervisor ready';
  return `<article class="agent-terminal orchestrator-terminal clickable ${esc(status)}" onclick="openOrchestratorInspector()"><div class="agent-titlebar"><span class="status-led"></span><span class="agent-name">SUPERVISOR / ORCHESTRATOR</span><span class="agent-index">root · ${activeTurn?'working':'idle'}</span><span class="agent-model">${esc(configLabel(o.model||p.orchestrator_model||'',o.reasoning_effort||p.orchestrator_effort,o.service_tier||p.orchestrator_tier))}</span></div><div class="agent-task"><div class="task-path">mission / control-plane</div><strong>${esc(title)}</strong><p>${esc(waiting||'Owns the disposition, architecture, scope, dependencies, escalation decisions and final synthesis.')}</p></div><div class="mini-terminal orchestrator-log">${working}${recent}</div><div class="agent-footer"><span>control</span><span>${esc(effortLabels[o.reasoning_effort||p.orchestrator_effort]||'')}</span><span class="footer-spacer"></span><button class="text-button" onclick="event.stopPropagation();openOrchestratorInspector(true)">message</button><button class="text-button" onclick="event.stopPropagation();logs('orchestrator:${p.id}','orchestrator')">Raw log</button></div></article>`
}
function renderTerminal(t){
  const d=deps(t),logs=['waiting_for_orchestrator','waiting_for_user','waiting_for_permission'].includes(t.status)?[]:(t.recent_logs||[]),readVerifying=t.mode==='read'&&t.status==='integrating',displayStatus=readVerifying?'Verifying result':statusLabel(t.status);let lines='';
  const pausedForPreflight=['waiting_for_user','waiting_for_permission','blocked'].includes(live.plan?.status)&&['waiting_for_user','waiting_for_permission','blocked'].includes(live.plan?.preflight_status);
  const working=t.status==='running'?`<div class="classic-working"><i></i><b>Working</b><span>(${elapsed(t.started_at,null,live.server_time)} · pause to interrupt)</span></div>`:t.status==='integrating'?`<div class="classic-working"><i></i><b>Integrating</b><span>protecting the worker result</span></div>`:t.status==='resuming'?`<div class="classic-working"><i></i><b>Continuing</b><span>same conversation</span></div>`:t.status==='pausing'?`<div class="classic-working"><i></i><b>Pausing</b><span>saving checkpoint</span></div>`:'';
  if(logs.length)lines=working+logs.slice(-7).map(l=>`<div class="term-line ${esc(l.stream)}">${l.stream==='stderr'?'! ':l.stream==='manual'?'> ':'$ '}${esc(short(displayLogLine(l.line),220))}</div>`).join('');
  else if(t.status==='running')lines=working+`<div class="term-line"><span class="term-caret">▋</span></div>`;
  else if(t.status==='waiting_for_orchestrator')lines='<div class="term-line system">> waiting for orchestrator</div><div class="term-line">'+esc(short(t.waiting_reason||'A worker decision is being resolved.',360))+'</div>';
  else if(t.status==='waiting_for_user')lines='<div class="term-line system">> waiting for your answer</div><div class="term-line">'+esc(short(t.waiting_reason||'The mission needs information before continuing.',360))+'</div>';
  else if(t.status==='waiting_for_permission')lines='<div class="term-line system">> permission requested</div><div class="term-line">'+esc(short(t.waiting_reason||'Talk with the supervisor to grant or decline the requested authority.',360))+'</div>';
  else if(t.status==='pending'&&pausedForPreflight)lines=`<div class="term-line system">$ paused · waiting for your workspace decision</div><div class="term-line"><span class="term-caret">_</span> preflight resolution required</div>`;
  else if(t.status==='pending')lines=`<div class="term-line system">$ queued${d.length?` · waiting for task ${d.map(x=>x+1).join(', ')}`:' · ready'}</div><div class="term-line"><span class="term-caret">_</span></div>`;
  else if(t.status==='integrating')lines=working+`<div class="term-line system">$ integration in progress · follow-ups are queued</div><div class="term-line"><span class="term-caret">▋</span></div>`;
  else if(['done','executed'].includes(t.status))lines=`<div class="term-line system">$ task complete</div><div class="term-line">${esc(short(t.output||'No textual output.',300))}</div>`;
  else if(t.status==='pausing')lines=`<div class="term-line system">$ pausing · saving checkpoint</div><div class="term-line"><span class="term-caret">_</span> same conversation will resume</div>`;
  else if(t.status==='resuming')lines=`<div class="term-line system">$ continuing the same conversation</div><div class="term-line"><span class="term-caret">▋</span></div>`;
  else if(t.status==='paused_by_user')lines=`<div class="term-line system">$ paused by you</div><div class="term-line">${esc(short(t.error||'The worker conversation is preserved and ready to resume.',420))}</div>`;
  else if(['failed','blocked','attention'].includes(t.status))lines=`<div class="term-line stderr">! ${esc(short(t.error||t.output||'worker stopped',420))}</div>`;
  else lines=`<div class="term-line system">$ ${esc(t.status)}</div>`;
  const control=t.status==='running'?`<button class="text-button stop" onclick="event.stopPropagation();pauseTask('${t.id}')">pause</button>`:(['paused_by_user','failed','attention'].includes(t.status)?`<button class="text-button resume" onclick="event.stopPropagation();resumeTask('${t.id}')">resume</button>`:t.status==='done'||t.status==='executed'?`<button class="text-button resume" onclick="event.stopPropagation();messageAgent('${t.id}')">continue</button>`:t.status==='resuming'?'<span class="agent-control-state">resuming…</span>':t.status==='pausing'?'<span class="agent-control-state">pausing…</span>':'');
  return `<article class="agent-terminal ${esc(t.status)} ${readVerifying?'read-verifying':''} clickable" onclick="openInspector('${t.id}')"><div class="agent-titlebar"><span class="status-led"></span><span class="agent-name">${esc(t.agent_name||t.agent_id||'worker')}</span><span class="agent-index">#${String(t.seq+1).padStart(2,'0')} · ${esc(displayStatus)}</span><span class="agent-model">${esc(configLabel(t.effective_model||'',t.effective_effort,t.effective_tier))}</span></div><div class="agent-task"><div class="task-path">${t.mode==='write'?'worktree':'workspace'} / task-${t.seq+1}</div><strong>${esc(t.title)}</strong><p>${esc((t.contract||{}).objective||t.instructions)}</p></div><div class="mini-terminal">${lines}</div><div class="agent-footer"><span>${esc(t.mode)}</span><span>${elapsed(t.started_at,t.finished_at,live.server_time)}</span><span class="${t.effective_effort==='max'?'reasoning-max':''}">${esc(effortLabels[t.effective_effort]||t.effective_effort||'')}</span>${t.retry_count?`<span>retry:${t.retry_count}</span>`:''}${d.length?`<span>deps:${d.map(x=>x+1).join(',')}</span>`:''}<span class="footer-spacer"></span><button class="text-button" onclick="event.stopPropagation();messageAgent('${t.id}')">message</button><button class="text-button" onclick="event.stopPropagation();openTaskTerminal('${t.id}')">terminal</button><button class="text-button" onclick="event.stopPropagation();openInspector('${t.id}')">inspect</button>${control}</div></article>`
}
function taskScopeSummary(t){const c=t.contract||{},sc=c.scope||{},ins=Array.isArray(sc.in_scope)?sc.in_scope:[],outs=Array.isArray(sc.out_of_scope)?sc.out_of_scope:[],paths=Array.isArray(c.allowed_paths)?c.allowed_paths:[];return `<div class="review-scope"><div><span>OBJECTIVE</span><p>${esc(c.objective||t.instructions||'—')}</p></div>${ins.length?`<div><span>IN SCOPE</span><p>${ins.slice(0,3).map(esc).join(' · ')}</p></div>`:''}${paths.length?`<div><span>PATHS</span><p>${paths.slice(0,4).map(esc).join(' · ')}</p></div>`:''}${outs.length?`<div><span>OUT OF SCOPE</span><p>${outs.slice(0,2).map(esc).join(' · ')}</p></div>`:''}</div>`}
function planReviewStateKey(p,tasks){
  if(['planned','approved','awaiting_apply','waiting_for_user','blocked','attention','failed','paused','pausing','resuming'].includes(p.status))return `mission:${p.status}`;
  const task=tasks.find(t=>['failed','blocked','attention','waiting_for_user','paused_by_user','resuming','integrating'].includes(t.status));
  return task?`task:${task.id}:${task.status}`:`quiet:${p.status}`;
}
function planReviewDefaultOpen(p,tasks){return !planReviewStateKey(p,tasks).startsWith('quiet:')}
function planReviewOpen(p,tasks){const key=planReviewStateKey(p,tasks),saved=planReviewDisclosure.get(p.id);if(saved&&saved.key===key)return saved.open;const open=planReviewDefaultOpen(p,tasks);planReviewDisclosure.set(p.id,{key,open});return open}
function togglePlanReview(id){const p=live.plan;if(!p||p.id!==id)return;const tasks=live.tasks||[],key=planReviewStateKey(p,tasks),open=planReviewOpen(p,tasks);planReviewDisclosure.set(id,{key,open:!open});renderPlanReview(p,tasks)}
function renderPlanReview(p,tasks){
  const box=$('planReviewPanel');if(!box)return;
  if(!tasks.length||p.status==='planning'){box.hidden=true;box.innerHTML='';return}
  box.hidden=false;
  const open=planReviewOpen(p,tasks);
  box.classList.toggle('collapsed',!open);
  const editable=['planned','approved'].includes(p.status);
  const agentOptions=t=>state.agents.map(a=>`<option value="${esc(a.id)}" ${a.id===t.agent_id?'selected':''}>${esc(a.name)}</option>`).join('');
  const cards=tasks.map(t=>{const d=deps(t);return `<article class="review-task-card ${statusClass(t.status)}"><div class="review-task-head"><span class="review-num">${String(t.seq+1).padStart(2,'0')}</span><div class="review-task-title"><b>${esc(t.title)}</b><small>${d.length?'after '+d.map(x=>'TASK-'+String(x+1).padStart(2,'0')).join(', '):'parallel-ready'}</small></div><span class="task-status ${statusClass(t.status)}">${esc(statusLabel(t.status))}</span></div>${taskScopeSummary(t)}<div class="review-controls"><label>AGENT<select id="assign-${t.id}" ${editable?'':'disabled'} onchange="updateTaskConfig('${t.id}')">${agentOptions(t)}</select></label><label>ACCESS<select id="mode-${t.id}" ${editable?'':'disabled'} onchange="updateTaskConfig('${t.id}')"><option value="read" ${t.mode==='read'?'selected':''}>Read-only</option><option value="write" ${t.mode==='write'?'selected':''}>Workspace write</option></select></label><button class="secondary compact" onclick="openInspector('${t.id}')">Full contract</button></div></article>`}).join('');
  const canRevise=['planned','approved'].includes(p.status)&&tasks.every(t=>t.status==='pending');
  const reviseAction=canRevise?`<button class="secondary" onclick="openPlanUpdate('${p.id}')">Update plan</button>`:'';
  const action=p.status==='planned'?`<button class="primary" onclick="approvePlan('${p.id}')">Approve plan</button>`:p.status==='approved'?`<button class="primary" onclick="runPlan('${p.id}')">Start mission ↵</button>`:'';
  const done=tasks.filter(t=>['done','executed'].includes(t.status)).length,issues=tasks.filter(t=>['failed','blocked','attention','waiting_for_user','paused_by_user'].includes(t.status)).length;
  const title=p.status==='planned'?'Check the task graph before anyone starts.':p.status==='approved'?'Approved and ready to start.':'Execution plan';
  const description=p.status==='planned'?'Review scope, dependencies and agent assignments. Changing an assignment keeps the plan unapproved until you confirm it again.':'Task ownership and scope stay visible throughout the mission.';
  const summary=`${tasks.length} tasks · ${done} complete${issues?` · ${issues} need attention`:''}`;
  box.innerHTML=`<div class="plan-review-head"><button class="plan-review-summary" onclick="togglePlanReview('${p.id}')" aria-expanded="${open?'true':'false'}"><span class="eyebrow">PLAN REVIEW</span><span class="plan-review-title-row"><h3>${esc(title)}</h3><i class="review-chevron">${open?'−':'+'}</i></span><p>${esc(description)}</p><small>${esc(summary)}</small></button><div class="review-actions">${reviseAction}${action}</div></div><div class="plan-review-body" ${open?'':'hidden'}><div class="review-task-grid">${cards}</div></div>`;
}
function openPlanUpdate(id){
  $('planUpdateId').value=id;
  $('planUpdatePrompt').value='';
  showDialog('planUpdateDialog');
  setTimeout(()=>$('planUpdatePrompt')?.focus(),50);
}
async function updateTaskConfig(id){try{await api('/api/task-config/'+id,{method:'POST',body:JSON.stringify({agent_id:$('assign-'+id).value,mode:$('mode-'+id).value})});await refreshState();await refreshLive()}catch(e){$('planMsg').textContent=e.message}}
async function approvePlan(id){try{await api('/api/approve-plan/'+id,{method:'POST',body:JSON.stringify({})});await refreshState();await refreshLive()}catch(e){$('planMsg').textContent=e.message}}
async function openTaskTerminal(id){try{await api('/api/open-terminal/'+id,{method:'POST',body:'{}'})}catch(e){$('planMsg').textContent=e.message}}
function messageAgent(id){return openInspector(id).then(()=>{setTimeout(()=>$('manualPrompt')?.focus(),50)})}
function renderHistory(){const target=$('planHistory');if(!target)return;const plans=(state.plans||[]).filter(p=>!selectedWorkspaceId||p.workspace_id===selectedWorkspaceId);target.innerHTML=plans.slice(0,8).map((p,i)=>`<button class="history-chip ${p.id===selectedPlanId?'active':''}" onclick="selectPlan('${p.id}')">${i===0?'latest · ':''}${esc(short(missionTitle(p),28))}</button>`).join('')}
function renderAgents(){$('agentGrid').innerHTML=(state.agents||[]).map(a=>`<article class="worker-card"><div class="worker-card-top"><strong>${esc(a.name)}</strong><button class="text-button" onclick="editAgent('${a.id}')">edit</button></div><p>${esc(a.role)}</p><div class="worker-meta"><span class="chip">${esc(a.mode)}</span><span class="chip">${esc(a.model||'plan model')}</span><span class="chip">${esc(a.reasoning_effort?effortLabels[a.reasoning_effort]:'inherit reasoning')}</span><span class="chip">${esc(a.service_tier?speedLabel(a.service_tier):'inherit speed')}</span></div></article>`).join('')}

async function selectWorkspace(id,newMission=false){selectedWorkspaceId=id;$('workspaceSelect').value=id;renderWorkspaceSelect();renderWorkspaceNav();if(newMission){selectedPlanId=null;switchView('mission');$('goal').focus()}else{if($('missionFilterWorkspace'))$('missionFilterWorkspace').value=id;renderGlobalMissions();switchView('missions')}}
async function selectPlan(id){selectedPlanId=id;const p=state.plans.find(x=>x.id===id);if(p?.workspace_id)selectedWorkspaceId=p.workspace_id;switchView('missionDetail');renderWorkspaceNav();renderHistory();await refreshLive()}
async function runPlan(id){try{await api('/api/run-plan/'+id,{method:'POST',body:'{}'});selectedPlanId=id;await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function pausePlan(id){try{await api('/api/pause-plan/'+id,{method:'POST',body:'{}'});await refreshState();await refreshLive()}catch(e){$('planMsg').textContent=e.message}}
async function resumePlan(id){try{await api('/api/resume-plan/'+id,{method:'POST',body:'{}'});await refreshState();await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function reopenPlan(id){try{await api('/api/reopen-plan/'+id,{method:'POST',body:'{}'});await refreshState();await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function restartAsNew(id){if(!window.confirm('Restart this mission as a new mission? Completed work will not be reused.'))return;try{const j=await api('/api/restart-plan/'+id,{method:'POST',body:'{}'});selectedPlanId=j.plan_id;await refreshState();switchView('missionDetail');scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function pauseTask(id){try{await api('/api/pause-task/'+id,{method:'POST',body:'{}'});await refreshLive();if(inspectorTaskId===id)refreshInspector()}catch(e){$('planMsg').textContent=e.message}}
async function resumeTask(id){try{await api('/api/resume-task/'+id,{method:'POST',body:'{}'});await refreshLive();if(inspectorTaskId===id)refreshInspector();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function saveMissionConfig(id){const button=document.querySelector('.mission-config-panel button');if(button)button.disabled=true;try{await api('/api/mission-config/'+id,{method:'POST',body:JSON.stringify({orchestrator_model:$('missionOrchestratorModel').value,orchestrator_effort:$('missionOrchestratorEffort').value,orchestrator_tier:$('missionOrchestratorTier').value,worker_model:$('missionWorkerModel').value,worker_effort:$('missionWorkerEffort').value,worker_tier:$('missionWorkerTier').value,max_parallel:Number($('missionMaxParallel').value),apply_remaining:true})});missionConfigOpen=false;await refreshState();await refreshLive()}catch(e){$('planMsg').textContent=e.message}finally{if(button)button.disabled=false}}
async function reconsiderPlan(id,mode){const labels={force_execute:'Create an execution plan anyway?',reconsider:'Ask the orchestrator to reconsider this decision?',verify:'Run a fresh read-only verification?'};if(!window.confirm(labels[mode]||'Ask the orchestrator to reconsider?'))return;try{await api('/api/reconsider-plan/'+id,{method:'POST',body:JSON.stringify({mode})});await refreshState();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
function selectedPreflightPaths(){return [...document.querySelectorAll('.preflight-path input:checked')].map(x=>x.value)}
async function preflightAction(id,action,requiresPaths){const paths=requiresPaths?selectedPreflightPaths():[];if(requiresPaths&&!paths.length){window.alert('Select at least one file first.');return}if(!window.confirm(action==='initialize_git'?'Initialize a local Git repository with an empty base commit? Existing files will not be added.':action==='move'?'Move the selected files to AgentDock safe area?':action==='stage'?'Stage the selected files without creating a commit?':action==='ignore'?'Add the selected paths to local ignore rules?':action==='verify_again'?'Run the read-only workspace verification again?':action==='cancel'?'Cancel this mission?':'Continue this mission in read-only mode?'))return;try{await api('/api/preflight-action/'+id,{method:'POST',body:JSON.stringify({action,paths})});await refreshState();await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function submitConsultationAnswer(planId,consultationId){
  const answer=($('consultationAnswer')?.value||'').trim(),option=($('consultationOption')?.value||'').trim(),files=[...($('consultationFiles')?.files||[])];
  if(!answer&&!option&&!files.length){window.alert('Add an answer, choose an option, or attach an image.');return}
  try{
    const attachments=[];for(const file of files)attachments.push(await fileToAttachment(file));
    await api('/api/consultation-answer/'+planId,{method:'POST',body:JSON.stringify({consultation_id:consultationId,answer,option,attachments})});
    await refreshState();await refreshLive();scheduleFastPolling();
  }catch(e){$('planMsg').textContent=e.message}
}
async function leaveConsultationField(planId,consultationId){if(!window.confirm('Tell the orchestrator to leave this field out?'))return;try{await api('/api/consultation-answer/'+planId,{method:'POST',body:JSON.stringify({consultation_id:consultationId,answer:'Leave this field out of the result.'})});await refreshState();await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function cancelConsultationMission(planId){if(!window.confirm('Cancel this mission?'))return;try{await api('/api/preflight-action/'+planId,{method:'POST',body:JSON.stringify({action:'cancel',paths:[]})});await refreshState();await refreshLive()}catch(e){$('planMsg').textContent=e.message}}
async function reconstructOrchestrator(planId){if(!window.confirm('Create a new orchestrator generation from the persisted mission history?'))return;try{await api('/api/reconstruct-orchestrator/'+planId,{method:'POST',body:'{}'});await refreshState();await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}}
async function cancelTask(id){try{await api('/api/cancel-task/'+id,{method:'POST',body:'{}'});await refreshLive();if(inspectorTaskId===id)refreshInspector()}catch(e){$('planMsg').textContent=e.message}}
function scheduleFastPolling(){clearInterval(liveTimer);liveTimer=setInterval(async()=>{if(selectedPlanId)await refreshLive()},1000)}
async function logs(id,title){$('logTitle').textContent=`${title}.log`;showDialog('logDialog');async function pull(){const j=await api('/api/logs/'+pathId(id));$('logText').textContent=j.logs.map(x=>`[${x.stream}] ${x.line}`).join('\n');$('logText').scrollTop=$('logText').scrollHeight}await pull();clearInterval(logTimer);logTimer=setInterval(()=>{if(!$('logDialog').open){clearInterval(logTimer);return}pull()},800)}
async function showDocs(id){try{const j=await api('/api/docs/'+id);$('planMsg').textContent=`Mission docs: ${j.mission_dir}`}catch(e){$('planMsg').textContent=e.message}}
async function showPlanDiff(id){try{const j=await api('/api/plan-diff/'+id);$('planDiffText').textContent=j.diff||'No pending integration diff.';showDialog('planDiffDialog')}catch(e){$('planMsg').textContent=e.message}}
async function applyPlan(id){const button=document.querySelector('.start-btn');if(button){button.disabled=true;button.textContent='Applying…'}try{await api('/api/apply-plan/'+id,{method:'POST',body:'{}'});await refreshState();await refreshLive()}catch(e){$('planMsg').textContent=e.message}finally{if(button){button.disabled=false}}}

function contractList(title,items){items=Array.isArray(items)?items:(items?[items]:[]);return `<section><h4>${esc(title)}</h4>${items.length?`<ul>${items.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<p>—</p>'}</section>`}
function contractHtml(t){const c=t.contract||{},sc=c.scope||{};return `<section><h4>Objective</h4><p>${esc(c.objective||t.instructions||'—')}</p></section><section><h4>Context</h4><p>${esc(c.context||'—')}</p></section>${contractList('In scope',sc.in_scope)}${contractList('Out of scope',sc.out_of_scope)}${contractList('Allowed paths',c.allowed_paths)}${contractList('Required inputs',c.required_inputs)}${contractList('Execution steps',c.implementation_steps)}${contractList('Acceptance criteria',c.acceptance_criteria)}${contractList('Verification',c.verification_commands)}${contractList('Expected output',c.expected_output)}${contractList('Escalate instead of deciding',c.escalation_conditions)}<section><h4>Decision policy</h4><p>${esc(c.decision_policy||'Architecture and scope decisions stay with the orchestrator.')}</p></section>`}

function eventItem(ev){const o=ev.payload||{},params=o.params||{};return o.item||params.item||{}}
function normalizeEvents(events){const out=[],positions=new Map();for(const ev of events||[]){const o=ev.payload||{},type=o.type||o.method||ev.event_type,item=eventItem(ev);let key=item.id?`item:${item.id}`:null;if(type==='agentdock.preflight')key='control:latest-preflight';if(key&&positions.has(key)){out[positions.get(key)]=ev}else{if(key)positions.set(key,out.length);out.push(ev)}}return out}
function mergeTimelineFallback(eventResult,logs=[],messages=[]){
  const items=[];
  const events=normalizeEvents(eventResult?.events||[]);
  events.forEach((ev,index)=>items.push({kind:'event',id:ev.id,session_id:ev.session_id,ts:ev.ts||ev.created_at||0,event_type:ev.event_type,item_type:ev.item_type,payload:ev.payload,order:Number(ev.id)||index}));
  const usableLogs=events.length?logs.filter(x=>x.stream!=='stdout'):logs;
  usableLogs.forEach((log,index)=>items.push({kind:'log',id:log.id,ts:log.ts||log.created_at||0,stream:log.stream,line:log.line,order:Number(log.id)||index}));
  messages.forEach((message,index)=>items.push({kind:'message',id:message.id,ts:message.ts||message.created_at||0,text:message.text,status:message.status,error:message.error,attachments:jsonArray(message.attachments_json),order:Number(message.id)||index}));
  items.sort((a,b)=>{const at=Number(a.ts)||0,bt=Number(b.ts)||0;if(at!==bt)return at-bt;const rank={event:1,message:2,log:3};return (rank[a.kind]||9)-(rank[b.kind]||9)||((Number(a.order)||0)-(Number(b.order)||0))});
  return {session:eventResult?.session||null,items:items.map((item,index)=>({...item,sequence:index+1}))};
}
async function loadTimeline(target){
  const encoded=pathId(target);
  try{
    const timeline=await api('/api/timeline/'+encoded);
    if(Array.isArray(timeline.items))return timeline;
  }catch(primaryError){
    try{
      const isOrchestrator=String(target||'').startsWith('orchestrator:');
      const [events,logsResult,messages]=await Promise.all([
        api('/api/events/'+encoded),
        api('/api/logs/'+encoded),
        isOrchestrator?Promise.resolve({messages:[]}):api('/api/messages/'+encoded)
      ]);
      return mergeTimelineFallback(events,logsResult.logs||[],messages.messages||[]);
    }catch(fallbackError){throw fallbackError||primaryError}
  }
  return {session:null,items:[]};
}
const orchestratorPurposeLabels={initial_disposition:'Analyzing the mission and workspace',user_answer:'Processing your answer',worker_consultation:'Resolving a worker question',failure_recovery:'Planning a safe recovery',contract_revision:'Revising the task contract',checkpoint_summary:'Summarizing mission progress',final_synthesis:'Preparing the final result',manual_message:'Responding to your message',reconstruct:'Reconstructing mission context'};
function latestOrchestratorPurpose(o){for(const entry of [...(o.recent_logs||[])].reverse()){const match=String(entry.line||'').match(/purpose=([a-z_]+)/);if(match)return match[1]}return ''}
function orchestratorInspectorProgress(p,o){
  const turnStatus=o.turn_status||p.orchestrator_turn_status||'';
  const activeTurn=['queued','running'].includes(turnStatus);
  const planningBeforeTurn=p.status==='planning'&&!['failed','attention','paused','cancelled'].includes(turnStatus);
  if(!activeTurn&&!planningBeforeTurn)return '';
  const purpose=latestOrchestratorPurpose(o),title=orchestratorPurposeLabels[purpose]||(p.status==='planning'?'Analyzing the mission and workspace':'Continuing the orchestrator turn');
  const label=turnStatus==='queued'?'ORCHESTRATOR IS STARTING':'ORCHESTRATOR IS WORKING';
  return `<div class="orchestrator-live"><i></i><div><span>${label}</span><b>${esc(title)}</b><small>Same mission conversation · live activity appears below</small></div></div>`;
}
function readableActivityError(value){
  let current=value;
  for(let i=0;i<5;i++){
    if(typeof current==='string'){
      try{current=JSON.parse(current);continue}catch{return current}
    }
    if(current&&typeof current==='object'){
      if(current.error){current=current.error;continue}
      if(current.message){current=current.message;continue}
    }
    break;
  }
  if(typeof current==='string')return current;
  if(current&&typeof current==='object')return current.code||current.type||'Turn failed';
  return 'Turn failed';
}
function activityEvent(ev){
  const o=ev.payload||{},params=o.params||{},type=o.type||o.method||ev.event_type,item=eventItem(ev),rawItemType=item.type||ev.item_type||'';
  const it=String(rawItemType).replace(/([A-Z])/g,'_$1').replace(/^_/,'').replace(/-/g,'_').toLowerCase();
  if(type==='agentdock.workspace')return `<div class="activity-control"><span>Workspace analysis</span><b>${esc(o.classification||'Workspace found')}</b><div>${esc(o.repo_root||'')} ${o.branch?`· branch ${esc(o.branch)}`:''} ${o.upstream?`· upstream ${esc(o.upstream)}`:''} ${o.remote_names?.length?`· remotes ${esc(o.remote_names.join(', '))}`:''}</div></div>`;
  if(type==='agentdock.disposition')return `<div class="activity-control disposition"><span>Mission decision</span><b>${esc(decisionLabel(o.decision))}</b><div>${esc(o.reason||'')}</div>${o.task_count!==undefined?`<small>${o.task_count} task(s) created</small>`:''}</div>`;
  if(type==='agentdock.task')return `<div class="activity-control task-event"><span>Proposed task ${String((o.seq??0)+1).padStart(2,'0')}</span><b>${esc(o.title||'Task')}</b><div>${esc(o.mode||'read-only')} · ${o.depends_on?.length?`after ${esc(o.depends_on.map(x=>x+1).join(', '))}`:'ready to run'}</div></div>`;
  if(type==='agentdock.preflight')return `<div class="activity-control preflight-event"><span>Workspace check</span><b>${esc(statusLabel(o.status||'ready'))}</b><div>${esc(o.message||'Read-only workspace checks complete.')}</div></div>`;
  if(type==='agentdock.mission_config'){const c=o.config||{};return `<div class="activity-control"><span>Runtime settings</span><b>Settings updated for the next turns</b><div>${esc(c.orchestrator_model||'default')} · ${esc(c.worker_model||'default')} · ${esc(o.max_parallel||'')} parallel${o.apply_remaining?' · applied to remaining work':''}</div></div>`}
  if(type==='agentdock.user_answer')return `<div class="activity-control"><span>Your answer</span><b>Information delivered to the orchestrator</b><div>${esc((o.answer||{}).text||'Attachment or option supplied')}</div></div>`;
  if(type==='agentdock.consultation')return `<div class="activity-control"><span>Worker question</span><b>Waiting for an orchestrator decision</b><div>${esc(o.question||'The worker asked for guidance.')}</div></div>`;
  if(type==='agentdock.orchestrator_turn'){const purpose=orchestratorPurposeLabels[o.purpose]||'Orchestrator turn';return `<div class="activity-milestone ${o.status==='failed'?'failed':''}"><span>Orchestrator</span><b>${esc(purpose)}</b><small>${esc(statusLabel(o.status||'completed'))} · same conversation</small></div>`}
  if(type==='agentdock.orchestrator_control')return `<div class="activity-control"><span>Supervisor action</span><b>${esc(String(o.action||'reply').replaceAll('_',' '))}</b><div>${esc(o.message||'Mission control updated.')}</div></div>`;
  if(type==='thread.started'||type==='thread/started'){const who=String(inspectorTaskId||'').startsWith('orchestrator:')?'Orchestrator':'Agent';return `<div class="activity-milestone"><span>Conversation</span><b>${who} conversation ready</b><small>The agent is ready; reasoning, commands and results will appear below.</small></div>`}
  if(type==='turn.started'||type==='turn/started'){const who=String(inspectorTaskId||'').startsWith('orchestrator:')?'Orchestrator':'Agent';return `<div class="activity-milestone active"><span>Working</span><b>${who} started a new turn</b><small>Reasoning, commands and results appear below as they arrive.</small></div>`}
  if(type==='turn.completed'||type==='turn/completed'){const u=o.usage||params.usage||params.turn?.usage||{};return `<div class="turn-usage"><b>Turn complete</b><span>${u.input_tokens||u.inputTokens||0} in</span><span>${u.cached_input_tokens||u.cachedInputTokens||0} cached</span><span>${u.output_tokens||u.outputTokens||0} out</span><span>${u.reasoning_output_tokens||u.reasoningOutputTokens||0} reasoning</span></div>`}
  if(type==='turn.failed'||type==='turn/failed'||type==='error')return `<div class="activity-error">${esc(readableActivityError(o.error||params.error||item.error||o.message||params.message||item.message||'Turn failed'))}</div>`;
  if(type==='turn/plan/updated'){const plan=Array.isArray(params.plan)?params.plan:[];return `<div class="todo-row"><b>Plan update</b>${plan.map(x=>`<div>${x.status==='completed'?'✓':x.status==='inProgress'?'◐':'○'} ${esc(x.step||x.text||'')}</div>`).join('')}</div>`}
  if(type==='item/agentMessage/delta')return `<div class="assistant-row"><span>agent</span><div>${esc(params.delta||'').replace(/\n/g,'<br>')}</div></div>`;
  if(it==='reasoning'){
    const summary=Array.isArray(item.summary)?item.summary.map(x=>typeof x==='string'?x:(x.text||'')).join(' '):(item.summary||item.text||'');
    return `<details class="reasoning-row" open><summary>Reasoning summary</summary><div>${esc(summary)}</div></details>`
  }
  if(it==='plan')return `<div class="todo-row"><b>Agent plan</b><div>${esc(item.text||'Plan updated')}</div></div>`;
  if(it==='agent_message'){
    const textValue=item.text||item.message||'';let structured=null;try{structured=JSON.parse(textValue)}catch{}
    if(structured&&typeof structured==='object'&&('decision' in structured||'tasks' in structured||'action' in structured))return '';
    return `<div class="assistant-row"><span>agent</span><div>${esc(textValue).replace(/\n/g,'<br>')}</div></div>`
  }
  if(it==='command_execution'){const status=item.status||'in_progress',output=item.aggregated_output||item.aggregatedOutput||'';return `<div class="tool-row command ${esc(status)}"><div class="tool-head"><span>terminal</span><b>${esc(short(item.command||item.cmd||'',150))}</b><em>${esc(status)}${item.exit_code!==undefined||item.exitCode!==undefined?` · exit ${item.exit_code??item.exitCode}`:''}</em></div>${output?`<pre>${esc(short(output,5000))}</pre>`:''}</div>`}
  if(it==='file_change'){return `<div class="tool-row file-change"><div class="tool-head"><span>files</span><b>File changes</b><em>${esc(item.status||'')}</em></div><div class="file-chips">${(item.changes||[]).map(c=>`<button class="file-chip ${esc(c.kind)}" data-path="${esc(c.path)}" onclick="openFilePreview(event,this.dataset.path)">${esc(c.kind)} ${esc(c.path)}</button>`).join('')}</div></div>`}
  if(it==='todo_list'){return `<div class="todo-row"><b>Plan</b>${(item.items||[]).map(x=>`<div>${x.completed?'✓':'○'} ${esc(x.text)}</div>`).join('')}</div>`}
  if(it==='web_search')return `<div class="tool-row"><div class="tool-head"><span>web</span><b>${esc(item.query||'search')}</b></div></div>`;
  if(it==='mcp_tool_call'||it==='collab_tool_call')return `<div class="tool-row"><div class="tool-head"><span>${esc(it)}</span><b>${esc(item.tool||item.server||'tool')}</b><em>${esc(item.status||'')}</em></div></div>`;
  if(it==='error')return `<div class="activity-error">${esc(readableActivityError(item.message||item.error||'Error'))}</div>`;
  if(type&&/^item[./]/.test(type)){
    const label=String(rawItemType||'agent activity').replace(/([A-Z])/g,' $1').replace(/[-_]/g,' ').trim();
    const detail=item.command||item.title||item.text||item.status||'Activity is in progress.';
    return `<div class="tool-row generic-event"><div class="tool-head"><span>agent</span><b>${esc(label)}</b><em>${esc(item.status||type)}</em></div><pre>${esc(short(detail,5000))}</pre></div>`;
  }
  if(type&&type!=='event')return `<div class="activity-system"><code>${esc(type)}${item.status?` · ${esc(item.status)}`:''}</code></div>`;
  return ''
}

function timelineMessage(item){const status=item.status||'delivered';return `<div class="timeline-message"><span>you</span><div>${esc(item.text||'[attachment]').replace(/\n/g,'<br>')}</div><em class="message-${esc(status)}">${esc(status)}</em></div>`}
function timelineLog(item){const line=displayLogLine(item.line||'');if(item.stream==='manual'&&line.includes('user → orchestrator: ')){return `<div class="timeline-message"><span>you</span><div>${esc(line.split('user → orchestrator: ',2)[1]||'')}</div><em class="message-delivered">delivered</em></div>`}const prefix=item.stream==='stderr'?'! ':item.stream==='supervisor'?'> ':'$ ';return `<div class="timeline-log ${esc(item.stream||'system')}"><span>${esc(item.stream||'terminal')}</span><code>${prefix}${esc(line)}</code></div>`}
function openFilePreview(event,path){if(event)event.stopPropagation();if(!live.plan||!path)return;const task=String(inspectorTaskId||'').startsWith('orchestrator:')?'':inspectorTaskId||'';window.open(`/preview.html?plan=${encodeURIComponent(live.plan.id)}&task=${encodeURIComponent(task)}&path=${encodeURIComponent(path)}`,'_blank','noopener')}
function renderTaskFiles(files){const box=$('taskFiles');if(!box)return;box.innerHTML=files?.length?`<div class="task-file-list"><div class="task-file-intro">Files from this task checkpoint. Open one in a separate AgentDock preview page.</div>${files.map(file=>`<button class="task-file-row" data-path="${esc(file.path)}" onclick="openFilePreview(event,this.dataset.path)"><span>▧</span><b>${esc(file.path)}</b><em>Open preview ↗</em></button>`).join('')}</div>`:'<div class="empty-state">No changed files are available for this task yet.</div>'}
function renderTimelineItem(item){if(item.kind==='message')return timelineMessage(item);if(item.kind==='log')return timelineLog(item);return activityEvent(item)}
function renderActivityTimeline(items,extra=''){
  const pane=$('activityPane'),feed=$('activityFeed');if(!pane||!feed)return;
  const nearBottom=pane.scrollHeight-pane.scrollTop-pane.clientHeight<96;
  const rendered=(items||[]).map(renderTimelineItem).filter(Boolean).join('');
  feed.innerHTML=extra+(rendered||'<div class="empty-state">No readable activity yet.</div>');
  const shouldStick=(activityScrollState.stickToBottom&&nearBottom)||activityScrollState.firstOpen;
  if(shouldStick){requestAnimationFrame(()=>{pane.scrollTop=pane.scrollHeight;$('activityLatest').hidden=true})}else{$('activityLatest').hidden=false}
  activityScrollState.stickToBottom=nearBottom||activityScrollState.firstOpen;activityScrollState.firstOpen=false;
}
async function openInspector(id){inspectorTaskId=id;manualAttachments=[];activityScrollState={stickToBottom:true,firstOpen:true};renderAttachmentStrip('manualAttachments',manualAttachments,'manual');$('manualPrompt').value='';showDialog('taskDialog');setInspectorTab('activity');await refreshInspector();clearInterval(inspectorTimer);inspectorTimer=setInterval(()=>{if(!$('taskDialog').open){clearInterval(inspectorTimer);return}refreshInspector(false)},1000)}
async function openOrchestratorInspector(){if(!live.plan)return;inspectorTaskId=`orchestrator:${live.plan.id}`;manualAttachments=[];activityScrollState={stickToBottom:true,firstOpen:true};showDialog('taskDialog');setInspectorTab('activity');await refreshInspector();clearInterval(inspectorTimer);inspectorTimer=setInterval(()=>{if(!$('taskDialog').open){clearInterval(inspectorTimer);return}refreshInspector(false)},1000)}
async function refreshInspector(fetchDiff=true){
  const isRoot=String(inspectorTaskId||'').startsWith('orchestrator:');
  const resumeButton=$('inspectResume'),stopButton=$('inspectStop');
  if(isRoot){
    const p=live.plan;if(!p)return;
    const evidence=jsonArray(p.evidence||p.evidence_json),questions=jsonArray(p.questions||p.questions_json);
    $('taskDialogTitle').textContent='SUPERVISOR / ORCHESTRATOR';
    $('taskRuntime').textContent=`${configLabel(p.orchestrator_used||p.orchestrator_model,p.orchestrator_effort,p.orchestrator_tier)} · ${statusLabel(p.status)}`;
    const legacyState=p.legacy_orchestrator_status||'';
    const legacyHtml=['reconstruct_required','reconciliation_required'].includes(legacyState)?`<section class="legacy-recovery"><h4>Conversation recovery</h4><p>This mission's previous orchestrator history needs to be reconciled before it can continue safely.</p><button class="secondary compact" onclick="reconstructOrchestrator('${p.id}')">Reconstruct context</button></section>`:'';
    $('taskContract').innerHTML=`<section><h4>Mission</h4><p>${esc(missionTitle(p))}</p><p>${esc(p.goal)}</p></section><section><h4>Decision</h4><p>${esc(decisionLabel(p.decision))}</p><p>${esc(p.decision_reason||'')}</p></section>${p.final_response?`<section><h4>Result</h4><p>${esc(p.final_response)}</p></section>`:''}${contractList('Evidence',evidence)}${contractList('Questions',questions)}<section><h4>Conversation</h4><p>Talk with the orchestrator in this mission's existing conversation.</p></section>${legacyHtml}`;
    stopButton.style.display='none';resumeButton.style.display='none';$('openTerminalBtn').style.display='none';document.querySelector('.manual-control').style.display='block';$('manualSend').disabled=false;$('manualSend').textContent='Send ↵';
    $('manualHint').textContent=['waiting_for_user','waiting_for_permission','attention'].includes(p.status)?'Tell the supervisor what you authorize or ask it to diagnose and continue.':'Talk with the orchestrator in the same mission conversation.';
    $('taskFiles').innerHTML='<div class="empty-state">Open a worker to inspect its changed files.</div>';
    try{const [timeline,raw]=await Promise.all([loadTimeline(inspectorTaskId),api('/api/logs/'+pathId(inspectorTaskId))]);renderActivityTimeline(timeline.items||[],orchestratorInspectorProgress(p,live.orchestrator||{}));$('inspectorRaw').textContent=raw.logs.map(x=>`[${x.stream}] ${x.line}`).join('\n');$('diffText').textContent='Orchestrator runs in the control plane; inspect worker diffs on their task cards.'}catch(err){renderActivityTimeline([] ,`<div class="activity-error">${esc(err.message)}</div>`)}
    return
  }
  document.querySelector('.manual-control').style.display='block';const t=(live.tasks||[]).find(x=>x.id===inspectorTaskId)||(state.plans.flatMap(p=>p.tasks||[]).find(x=>x.id===inspectorTaskId));if(!t)return;$('taskDialogTitle').textContent=`TASK-${String((t.seq??0)+1).padStart(3,'0')} · ${t.title}`;$('taskRuntime').textContent=`${t.agent_name||t.agent_id||'worker'} · ${configLabel(t.effective_model||live.plan?.worker_model||'',t.effective_effort||live.plan?.worker_effort,t.effective_tier||live.plan?.worker_tier)} · ${statusLabel(t.status)}`;$('taskContract').innerHTML=contractHtml(t);stopButton.style.display=t.status==='running'?'inline-flex':'none';resumeButton.style.display=['paused_by_user','failed','attention','waiting_for_permission'].includes(t.status)?'inline-flex':'none';$('openTerminalBtn').style.display='inline-flex';$('manualSend').disabled=t.status==='cancelled';$('manualSend').textContent=t.status==='running'?'Queue ↵':'Send ↵';$('manualHint').textContent=t.status==='running'?'Agent is working. Your message will automatically become the next turn on the same Codex thread.':t.status==='paused_by_user'?'Paused by you. Resume to continue this same conversation.':t.status==='resuming'?'Continuing the same conversation…':t.status==='pausing'?'Saving a checkpoint…':'Continue this agent conversation.';try{const [timeline,raw,d,files]=await Promise.all([loadTimeline(t.id),api('/api/logs/'+pathId(t.id)),fetchDiff?api('/api/diff/'+pathId(t.id)):Promise.resolve(null),api('/api/task-files/'+pathId(t.id))]);renderActivityTimeline(timeline.items||[]);renderTaskFiles(files.files||[]);$('inspectorRaw').textContent=raw.logs.map(x=>`[${x.stream}] ${x.line}`).join('\n');if(d)$('diffText').textContent=d.diff||'No diff.'}catch(err){renderActivityTimeline([],`<div class="activity-error">${esc(err.message)}</div>`)}
}
function setInspectorTab(name){document.querySelectorAll('.inspector-tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));document.querySelectorAll('.inspector-pane').forEach(p=>p.classList.toggle('active',p.id===`${name}Pane`))}

function fileToAttachment(file){return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>{const data=String(r.result||''),i=data.indexOf(',');resolve({name:file.name||`clipboard-${Date.now()}.png`,mime:file.type||'image/png',data_base64:i>=0?data.slice(i+1):data})};r.onerror=reject;r.readAsDataURL(file)})}
async function addFiles(files,target){for(const f of [...files]){if(!f.type.startsWith('image/'))continue;const a=await fileToAttachment(f);target.push(a)}renderAttachmentStrip(target===missionAttachments?'missionAttachments':'manualAttachments',target,target===missionAttachments?'mission':'manual')}
function renderAttachmentStrip(id,list,kind){$(id).innerHTML=list.map((a,i)=>`<span class="attachment-chip"><span class="attachment-thumb">▧</span><b>${esc(short(a.name,26))}</b><small>${esc(a.mime)}</small><button onclick="removeAttachment('${kind}',${i})">×</button></span>`).join('')}
function removeAttachment(kind,i){const list=kind==='mission'?missionAttachments:manualAttachments;list.splice(i,1);renderAttachmentStrip(kind==='mission'?'missionAttachments':'manualAttachments',list,kind)}
function pasteImagesHandler(target){return async e=>{const files=[...e.clipboardData.items].filter(x=>x.kind==='file').map(x=>x.getAsFile()).filter(Boolean);if(files.length){e.preventDefault();await addFiles(files,target)}}}

function setWorkspaceError(message=''){const el=$('workspaceError');el.textContent=message;el.hidden=!message}
function setWorkspaceDetected(message=''){const el=$('workspaceDetected');el.textContent=message;el.hidden=!message}
function openWorkspaceDialog(){$('workspaceForm').reset();setWorkspaceError();setWorkspaceDetected();showDialog('workspaceDialog');setTimeout(()=>$('workspaceRepo').focus(),50)}
function editAgent(id){const a=state.agents.find(x=>x.id===id);if(!a)return;$('agentId').value=a.id;$('agentName').value=a.name;$('agentModel').value=a.model||'';$('agentEffort').value=a.reasoning_effort||'';$('agentTier').value=a.service_tier||'';$('agentMode').value=a.mode;$('agentRole').value=a.role;showDialog('agentDialog')}

$('newWorkspace').onclick=$('newWorkspaceSide').onclick=$('newWorkspaceBoard').onclick=openWorkspaceDialog;
$('browseWorkspace').onclick=async()=>{const b=$('browseWorkspace');setWorkspaceError();setWorkspaceDetected();b.disabled=true;b.textContent='Opening…';try{const j=await api('/api/workspace/browse');if(j.cancelled)return;$('workspaceRepo').value=j.path||'';if(!$('workspaceName').value.trim())$('workspaceName').value=j.name||'';setWorkspaceDetected(`${j.is_git?'Git repository':'Folder'} · ${j.branch?`branch ${j.branch}`:'ready'} · ${j.path}`)}catch(err){setWorkspaceError(err.message)}finally{b.disabled=false;b.textContent='Choose folder…'}};
$('workspaceRepo').addEventListener('input',()=>{setWorkspaceError();setWorkspaceDetected()});
$('saveWorkspace').onclick=async e=>{e.preventDefault();const b=$('saveWorkspace');setWorkspaceError();setWorkspaceDetected();b.disabled=true;b.textContent='Adding…';try{const j=await api('/api/workspaces',{method:'POST',body:JSON.stringify({name:$('workspaceName').value,repo_path:$('workspaceRepo').value})});selectedWorkspaceId=j.workspace.id;$('workspaceDialog').close();await refreshState();$('workspaceSelect').value=selectedWorkspaceId;renderWorkspaceSelect()}catch(err){setWorkspaceError(err.message);$('workspaceRepo').focus()}finally{b.disabled=false;b.textContent='Add workspace'}};
$('workspaceSelect').onchange=()=>{selectedWorkspaceId=$('workspaceSelect').value;renderWorkspaceSelect();renderWorkspaceNav()};
$('newAgent').onclick=()=>{$('agentForm').reset();$('agentId').value='';showDialog('agentDialog')};
$('saveAgent').onclick=async e=>{e.preventDefault();await api('/api/agents',{method:'POST',body:JSON.stringify({id:$('agentId').value||undefined,name:$('agentName').value,model:$('agentModel').value,reasoning_effort:$('agentEffort').value,service_tier:$('agentTier').value,mode:$('agentMode').value,role:$('agentRole').value})});$('agentDialog').close();await refreshState()};

async function createMission(preview=false){const primary=$('planBtn'),demo=$('demoPlanBtn');if(!selectedWorkspaceId){openWorkspaceDialog();return}if(!$('goal').value.trim()){$('planMsg').textContent='Describe the mission outcome first.';$('goal').focus();return}primary.disabled=true;demo.disabled=true;planning=true;renderNoMission();$('planMsg').textContent=preview?'Starting local preview · no Codex quota…':'Orchestrator is inspecting the workspace and deciding whether execution is needed…';try{const recovery={auto_clean_generated:false,auto_repair_ignores:false,auto_retry_transient:$('autoRetryTransient').checked,auto_remove_stale_worktrees:false,auto_resolve_git_locks:false,unknown_local_changes:$('unknownChangesPolicy').value,merge_conflicts:$('mergeConflictPolicy').value,destructive_operations:$('destructivePolicy').value};const payload={goal:$('goal').value,workspace_id:selectedWorkspaceId,orchestrator_model:$('orchestratorModel').value,orchestrator_effort:$('orchestratorEffort').value,orchestrator_tier:$('orchestratorTier').value,worker_model:$('workerModel').value,worker_effort:$('workerEffort').value,worker_tier:$('workerTier').value,max_parallel:Number($('maxParallel').value),recovery,attachments:missionAttachments};const j=await api(preview?'/api/demo-plan':'/api/plan',{method:'POST',body:JSON.stringify(payload)});selectedPlanId=j.plan_id;missionAttachments=[];renderAttachmentStrip('missionAttachments',missionAttachments,'mission');$('planMsg').textContent=preview?'Demo mission is live · 0 quota used':'Orchestrator is live · workspace analysis and disposition are being recorded…';planning=false;switchView('missionDetail');await refreshState();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message;renderNoMission()}finally{planning=false;primary.disabled=false;demo.disabled=false}}
$('planBtn').onclick=()=>createMission(false);
$('demoPlanBtn').onclick=()=>createMission(true);

$('inspectStop').onclick=()=>inspectorTaskId&&!String(inspectorTaskId).startsWith('orchestrator:')&&pauseTask(inspectorTaskId);
$('inspectResume').onclick=()=>inspectorTaskId&&!String(inspectorTaskId).startsWith('orchestrator:')&&resumeTask(inspectorTaskId);
$('activityLatest').onclick=()=>{const pane=$('activityPane');activityScrollState={stickToBottom:true,firstOpen:false};pane.scrollTop=pane.scrollHeight;$('activityLatest').hidden=true};
$('openTerminalBtn').onclick=()=>{if(inspectorTaskId&&!String(inspectorTaskId).startsWith('orchestrator:'))openTaskTerminal(inspectorTaskId)};
$('manualSend').onclick=async()=>{if(!inspectorTaskId)return;const prompt=$('manualPrompt').value.trim();if(!prompt&&manualAttachments.length===0)return;try{if(String(inspectorTaskId).startsWith('orchestrator:')){const pid=inspectorTaskId.split(':')[1],p=live.plan||{};if(['waiting_for_user','waiting_for_permission'].includes(p.status)&&p.decision==='needs_user_input'&&!manualAttachments.length){await api('/api/reconsider-plan/'+pid,{method:'POST',body:JSON.stringify({mode:'answer',note:prompt})});$('manualHint').textContent='Answer received. Re-evaluating the mission…'}else{await api('/api/orchestrator-follow-up/'+pid,{method:'POST',body:JSON.stringify({prompt,attachments:manualAttachments})});$('manualHint').textContent='Message sent to orchestrator.'}}else{const r=await api('/api/follow-up/'+inspectorTaskId,{method:'POST',body:JSON.stringify({prompt,attachments:manualAttachments})});$('manualHint').textContent=r.queued?'Queued for the next turn on this agent.':'Message sent to the same Codex thread.'}$('manualPrompt').value='';manualAttachments=[];renderAttachmentStrip('manualAttachments',manualAttachments,'manual');await refreshLive();refreshInspector()}catch(e){$('manualHint').textContent=e.message}};
$('submitPlanUpdate').onclick=async()=>{const button=$('submitPlanUpdate'),pid=$('planUpdateId').value,prompt=$('planUpdatePrompt').value.trim();if(!prompt){$('planUpdatePrompt').focus();return}button.disabled=true;button.textContent='Updating…';try{await api('/api/reconsider-plan/'+pid,{method:'POST',body:JSON.stringify({mode:'reconsider',note:prompt})});$('planUpdateDialog').close();await refreshState();await refreshLive();scheduleFastPolling()}catch(e){$('planMsg').textContent=e.message}finally{button.disabled=false;button.textContent='Update plan ↵'}};
document.querySelectorAll('.inspector-tabs button').forEach(b=>b.onclick=()=>setInspectorTab(b.dataset.tab));
$('goal').addEventListener('paste',pasteImagesHandler(missionAttachments));$('manualPrompt').addEventListener('paste',pasteImagesHandler(manualAttachments));
$('missionDropZone').addEventListener('dragover',e=>{e.preventDefault();$('missionDropZone').classList.add('drag-over')});$('missionDropZone').addEventListener('dragleave',()=> $('missionDropZone').classList.remove('drag-over'));$('missionDropZone').addEventListener('drop',async e=>{e.preventDefault();$('missionDropZone').classList.remove('drag-over');await addFiles(e.dataTransfer.files,missionAttachments)});
$('refresh').onclick=refreshState;
$('overviewNewMission').onclick=$('missionsNewMission').onclick=()=>{selectedPlanId=null;switchView('mission');$('goal').focus()};
$('overviewAddWorkspace').onclick=openWorkspaceDialog;
$('backToMissions').onclick=()=>switchView('missions');
$('missionFilterWorkspace').onchange=renderGlobalMissions;$('missionFilterStatus').onchange=renderGlobalMissions;
document.querySelectorAll('[data-jump]').forEach(b=>b.onclick=()=>switchView(b.dataset.jump));
document.querySelectorAll('.nav').forEach(n=>n.onclick=()=>switchView(n.dataset.view));

initModelControls();switchView('overview');refreshState();scheduleFastPolling();stateTimer=setInterval(refreshState,6000);
