// Shared action presentation. Views are local UI state, never device commands.
const operationViews=new Map();
const universalStages=[['queued','Queued'],['inspect','Inspect release'],['core_write','Download & write core'],['core_verify','Verify core'],['application_download','Download application'],['application_verify','Verify & stage application'],['pair','Pair components'],['install','Restart & install'],['complete','Confirm healthy']];
function operationView(id){if(!operationViews.has(id))operationViews.set(id,{query:'',outcome:'all',stage:'all',page:0,size:25,selected:''});return operationViews.get(id);}
function operationOutcome(status){return status==='complete'?'complete':['failed','interrupted'].includes(status)?'failed':status==='cancelled'?'cancelled':status==='queued'?'queued':['staged','scheduled'].includes(status)?'waiting':'running';}
function deploymentStage(result,type){
 if(['complete','cancelled'].includes(result.status))return result.status;
 if(result.status==='queued')return 'queued';
 if(result.status==='installing')return 'install';
 if(type!=='universal')return ['staged','scheduled'].includes(result.status)?'ready':'stage';
 const passed=new Set(result.update_milestones||[]);passed.add('queued');
 if(result.status==='staged'||result.status==='scheduled')return 'ready';
 if(result.update_phase&&universalStages.some(([name])=>name===result.update_phase)&&!passed.has(result.update_phase))return result.update_phase;
 if(Number(result.milestone_rank)>=2)return 'install';
 return universalStages.find(([name])=>!passed.has(name))?.[0]||'complete';
}
function deploymentRows(deployment){
 const devices=new Map(state.devices.map(device=>[device.id,device])),type=deployment.update?.release_type;
 return deployment.targets.map(id=>{const result=deployment.results?.[id]||{status:'queued',detail:'Waiting to start'},device=devices.get(id),stage=deploymentStage(result,type);
  return {id,host:device?.host||id,description:device?.description||'',status:result.status,detail:result.detail||'',stage,
   percent:typeof result.update_percent==='number'&&Number.isFinite(result.update_percent)&&result.update_percent>=0&&result.update_percent<=100&&stage===result.update_phase&&!['install','complete','cancelled','ready'].includes(stage)?result.update_percent:null,
   updated:result.updated_at,freshness:result.progress_poll,completed:result.update_milestones||[],controls:updateCancelDeviceBadge(deployment,id,device),error:updateCancelError(deployment,id)};
 });
}
function profileFlow(deployment){
 const rows=deploymentRows(deployment),done=rows.filter(row=>row.status==='complete').length,running=rows.filter(row=>operationOutcome(row.status)==='running').length,failed=rows.filter(row=>row.status==='failed').length;
 return milestoneFlow(['Queued','Apply configuration','Confirm applied'],[rows.length,done,done],rows.length,failed>0,[0,running,0],[0,failed,0],{id:deployment.id,names:['queued','stage','complete']},[],[0,0,failed]);
}
function progressReadPresentation(poll,status,now=Date.now()/1000){
 if(!poll||['complete','failed','cancelled'].includes(status))return null;
 const received=Number(poll.last_received)||0,age=Math.max(0,Math.floor(now-received)),stale=received&&age>Number(poll.stale_after_s||15);
 const scheduling=poll.scheduling||{},retry=Number(scheduling.next_retry_s)||0,
  reason=poll.error?`${poll.workflow_stage||'Status'} read failed: ${poll.error}${retry?`; next attempt in ${Math.ceil(retry)}s`:''}`:
   scheduling.waiting_for&&scheduling.waiting_for!=='progress'?`Waiting for ${scheduling.waiting_for}`:'';
 return {text:received?`Updated ${age}s ago${poll.error?' — retrying':stale?' — stale':''}`:'Awaiting live progress',
  tone:poll.error||stale?'warn':'muted',title:[reason,`Last live device response: ${when(received)}; read took ${Number(poll.duration_ms)||0}ms`,
   ...Array.from(poll.reads||[],read=>`${read.path}: ${read.stage}, ${read.duration_ms}ms, ${read.response_bytes} bytes`)].filter(Boolean).join('; ')};
}
function operationFreshness(row){
 const value=progressReadPresentation(row.freshness,row.status);
 return value?`<small class="operation-freshness ${value.tone}" data-progress-freshness="${esc(JSON.stringify(row.freshness))}" data-progress-status="${esc(row.status)}" title="${esc(value.title)}">${esc(value.text)}</small>`:'';
}
function refreshProgressFreshness(){
 for(const node of document.querySelectorAll('[data-progress-freshness]')){
  const value=progressReadPresentation(JSON.parse(node.dataset.progressFreshness),node.dataset.progressStatus);
  if(value){node.textContent=value.text;node.className='operation-freshness '+value.tone;node.title=value.title;}
 }
}
function operationTable(id,rows,stages){
 const view=operationView(id),words=view.query.toLowerCase().trim().split(/\s+/).filter(Boolean),labels=new Map([...stages,['ready','Ready / scheduled'],['cancelled','Cancelled']]);
 const filtered=rows.filter(row=>words.every(word=>[row.host,row.description,row.id,row.detail,labels.get(row.stage)||row.stage,row.status].join(' ').toLowerCase().includes(word))&&(view.outcome==='all'||operationOutcome(row.status)===view.outcome)&&(view.stage==='all'||row.stage===view.stage));
 const pages=Math.max(1,Math.ceil(filtered.length/view.size));view.page=Math.min(view.page,pages-1);
 const shown=filtered.slice(view.page*view.size,(view.page+1)*view.size);
 const options=[['all','All outcomes'],['running','In progress'],['queued','Queued'],['waiting','Waiting for activation'],['complete','Completed'],['failed','Failed'],['cancelled','Cancelled']];
 return `<div class="operation-browser" data-operation="${esc(id)}"><div class="operation-filters"><label>Search devices<input type="search" data-operation-field="query" value="${esc(view.query)}" placeholder="Hostname, description, stage or error" oninput="changeOperationView(this)"></label><label>Outcome<select data-operation-field="outcome" onchange="changeOperationView(this)">${options.map(([key,label])=>`<option value="${key}" ${view.outcome===key?'selected':''}>${label}</option>`).join('')}</select></label><label>Stage<select data-operation-field="stage" onchange="changeOperationView(this)"><option value="all">All stages</option>${[...labels].map(([key,label])=>`<option value="${key}" ${view.stage===key?'selected':''}>${esc(label)}</option>`).join('')}</select></label><button class="secondary" type="button" onclick="resetOperationView(this)">Clear filters</button></div><div class="operation-table-wrap"><table class="operation-table"><thead><tr><th>Hostname</th><th>Current stage</th><th>Stage progress</th><th>Outcome</th></tr></thead><tbody>${shown.map(row=>`<tr class="target-result operation-device-row"><td><button class="operation-select" type="button" data-device="${esc(row.id)}" onclick="selectOperationDevice(this)" aria-expanded="${view.selected===row.id}"><strong>${esc(row.host)}</strong><small>${esc(row.description)}</small></button></td><td>${esc(labels.get(row.stage)||row.stage)}<small class="operation-stage-detail" title="${esc(row.detail)}">${esc(row.detail)}</small>${operationFreshness(row)}</td><td>${row.percent===null?`<small>${row.status==='complete'?'Confirmed':row.status==='failed'?'Stopped':row.status==='cancelled'?'Cancelled':['queued','scheduled','staged'].includes(row.status)?'Waiting':'In progress'}</small>`:`<progress value="${row.percent}" max="100" aria-label="${esc(row.host)}: ${esc(labels.get(row.stage)||row.stage)}">${row.percent}%</progress><small>${row.percent}% of this stage</small>`}</td><td><div class="deployment-status-actions">${row.controls||''}${statusBadge(row.status)}</div>${row.error||''}</td></tr>${view.selected===row.id?`<tr class="operation-detail-row"><td colspan="4"><strong>${esc(row.host)}</strong><p class="${row.status==='failed'?'status error':row.status==='cancelled'?'status warning':'muted'}">${esc(row.detail||'Waiting for device progress')}</p><small>Last update: ${esc(when(row.updated))}</small>${row.completed.length?`<p class="muted">Completed milestones: ${row.completed.map(name=>esc(labels.get(name)||name)).join(' · ')}</p>`:''}</td></tr>`:''}`).join('')||'<tr><td colspan="4">No devices match these filters.</td></tr>'}</tbody></table></div><div class="operation-pager"><small>${filtered.length?view.page*view.size+1:0}–${Math.min((view.page+1)*view.size,filtered.length)} of ${filtered.length} matching · ${rows.length} total</small><label>Rows<select data-operation-field="size" onchange="changeOperationView(this)">${[10,25,50].map(size=>`<option value="${size}" ${view.size===size?'selected':''}>${size}</option>`).join('')}</select></label><button type="button" class="secondary" data-direction="-1" onclick="pageOperation(this)" ${view.page===0?'disabled':''}>Previous</button><button type="button" class="secondary" data-direction="1" onclick="pageOperation(this)" ${view.page>=pages-1?'disabled':''}>Next</button></div></div>`;
}
function activeDeploymentCard(deployment){const update=deployment.update||{},content=[update.version&&`${update.version} · ${update.release_type}`,deployment.profile_name&&`Profile: ${deployment.profile_name}`].filter(Boolean).join(' + '),activation=deployment.activation==='now'?'Install now':deployment.activation==='schedule'?'Device schedule':'Stage only',rows=deploymentRows(deployment),stages=!update.version?[['queued','Queued'],['stage','Apply configuration'],['complete','Confirm applied']]:update.release_type==='universal'?universalStages:[['queued','Queued'],['stage','Verify & stage'],['install','Restart & install'],['complete','Confirm healthy']],summary=deployment.targets.length===1?`${rows[0].host} · ${activation}`:`${deployment.targets.length} devices · ${activation}`;return `<article class="card deployment" title="${esc(deployment.id)}"><div class="title-row"><h2>${esc(content||'Configuration deployment')}</h2><div class="deployment-status-actions">${updateCancelAllBadge(deployment)}${statusBadge(deployment.status)}</div></div><p class="muted">${esc(summary)}</p>${update.version?flowFor(deployment):profileFlow(deployment)}<details data-default-open data-disclosure-key="deployment-progress-${esc(deployment.id)}" open><summary>Device progress</summary>${operationTable(deployment.id,rows,stages)}</details></article>`;}
function renderOperationViews(){renderDeployments();if(typeof renderBackupProgress==='function')renderBackupProgress();if(typeof renderUSBProgress==='function')renderUSBProgress();}
function changeOperationView(control){const id=control.closest('[data-operation]').dataset.operation,view=operationView(id),field=control.dataset.operationField;view[field]=field==='size'?[10,25,50].includes(Number(control.value))?Number(control.value):25:control.value;view.page=0;renderOperationViews();}
function filterOperationStage(button){const view=operationView(button.dataset.operation);view.stage=button.dataset.stage;view.outcome='all';view.page=0;renderOperationViews();}
function resetOperationView(button){const id=button.closest('[data-operation]').dataset.operation;operationViews.delete(id);renderOperationViews();}
function pageOperation(button){operationView(button.closest('[data-operation]').dataset.operation).page=Math.max(0,operationView(button.closest('[data-operation]').dataset.operation).page+Number(button.dataset.direction));renderOperationViews();}
function selectOperationDevice(button){const view=operationView(button.closest('[data-operation]').dataset.operation);view.selected=view.selected===button.dataset.device?'':button.dataset.device;renderOperationViews();}
function preserveOperationFocus(render){
 const focus=document.activeElement,owner=focus?.closest?.('.operation-browser'),field=focus?.dataset?.operationField,id=owner?.dataset.operation,start=focus?.selectionStart,end=focus?.selectionEnd;
 const scroll=new Map([...document.querySelectorAll('.operation-browser')].map(node=>[node.dataset.operation,node.querySelector('.operation-table-wrap')?.scrollLeft||0]));
 render();
 for(const node of document.querySelectorAll('.operation-browser')){
  const wrap=node.querySelector('.operation-table-wrap');if(wrap)wrap.scrollLeft=scroll.get(node.dataset.operation)||0;
  if(id===node.dataset.operation&&field){const fresh=node.querySelector(`[data-operation-field="${field}"]`);if(fresh){fresh.focus({preventScroll:true});if(typeof start==='number'&&fresh.setSelectionRange)fresh.setSelectionRange(start,end);}}
 }
}
if(typeof renderDeployments==='function'){
 const actionRenderDeployments=renderDeployments;
 renderDeployments=function(){preserveOperationFocus(actionRenderDeployments);};
}

function singleActionPercent(labels,job){
 const measured=job.percent;
 return job.status==='running'&&/write|transfer|verify|stage application/i.test(labels[job.stage]||'')&&typeof measured==='number'&&Number.isFinite(measured)&&measured>=0&&measured<=100?measured:null;
}
function singleActionFlow(labels,job){
 const failed=['failed','interrupted'].includes(job.status),counts=labels.map((label,index)=>job.stage>index?1:0),active=labels.map((label,index)=>index===job.stage&&!failed&&job.status!=='complete'?1:0),failures=labels.map((label,index)=>index===job.stage&&failed?1:0);
 const measured=singleActionPercent(labels,job),label=labels[job.stage]||'';
 const blocked=labels.map((label,index)=>failed&&index>job.stage?1:0);
 return milestoneFlow(labels,counts,1,failed,active,failures,null,[],blocked)+(measured!==null?`<p class="muted">${esc(label)}: ${measured}% of this stage</p>`:'');
}
let displayedUSBJobs=[];
function renderUSBProgress(jobs){
 if(jobs!==undefined)displayedUSBJobs=jobs;
 const box=document.getElementById('seed-progress');if(!box)return;
 const stages=new Map(),rows=displayedUSBJobs.map(job=>{const labels=usbActionLabels(job),label=labels[job.stage]||'Prepared',stage='usb:'+label;stages.set(stage,label);return {id:String(job.id),host:job.image||'Browser USB',description:job.kind==='recovery'?'Clean USB recovery':'USB seed',status:job.status,stage,percent:singleActionPercent(labels,job),detail:job.detail||'',updated:job.updated_at,completed:labels.slice(0,job.stage)};});
 const selected=operationView('usb-batch').selected,job=displayedUSBJobs.find(item=>String(item.id)===selected)||displayedUSBJobs[0];
 preserveOperationFocus(()=>{box.innerHTML=rows.length?`<article class="card deployment">${job?`<h3>${esc(job.image)}</h3>${seedFlow(job)}<p class="muted">${esc(job.detail)}</p>`:''}${operationTable('usb-batch',rows,[...stages])}</article>`:'';});
}

let displayedBackupJobs=[];
async function readBackupJobs(ids){
 const jobs=[];
 for(let offset=0;offset<ids.length;offset+=500){
  const chunk=ids.slice(offset,offset+500),data=await api('api/jobs?ids='+chunk.join(','));
  if(!Array.isArray(data.jobs)||data.jobs.length!==chunk.length||data.jobs.some((job,index)=>String(job.id)!==String(chunk[index])||job.kind!=='backup'))throw Error('Invalid backup progress response');
  jobs.push(...data.jobs);
 }
 return jobs;
}
async function trackedBackupJobs(data){
 const known=new Set(displayedBackupJobs.map(job=>String(job.id))),active=data.active_jobs||[];
 const ids=backupState.pending?.jobIds||(active.some(job=>!known.has(String(job.id)))?active.map(job=>job.id):displayedBackupJobs.map(job=>job.id));
 return ids.length?readBackupJobs(ids):data.active_jobs||[];
}
function renderBackupProgress(jobs){
 if(jobs!==undefined){const previous=new Map(displayedBackupJobs.map(job=>[String(job.id),job]));displayedBackupJobs=jobs.map(job=>{const old=previous.get(String(job.id));return old&&(Number(old.updated_at)>Number(job.updated_at)||['complete','failed','cancelled'].includes(old.status))?old:job;});}
 const box=document.getElementById('backup-progress'),section=document.getElementById('backup-operations'),list=document.getElementById('backup-active-list');if(!box||!section||!list)return;
 if(!displayedBackupJobs.length){section.classList.add('hidden');box.innerHTML='';list.innerHTML='';return;}
 const devices=new Map(state.devices.map(device=>[device.id,device])),rows=displayedBackupJobs.map(job=>({id:String(job.id),host:devices.get(job.target)?.host||job.target,description:devices.get(job.target)?.description||'',status:job.status,stage:job.status==='complete'?'stored':job.status==='queued'?'queued':'backup',percent:null,detail:job.last_error||`${job.payload?.source||'manual'} encrypted backup`,updated:job.updated_at,completed:job.status==='complete'?['queued','backup','stored']:['queued']})),total=rows.length,complete=rows.filter(row=>row.status==='complete').length,running=rows.filter(row=>operationOutcome(row.status)==='running').length,errors=rows.filter(row=>row.status==='failed').length;
 section.classList.remove('hidden');preserveOperationFocus(()=>{box.innerHTML=milestoneFlow(['Queued','Backing up','Stored'],[total,complete,complete],total,errors>0,[0,running,0],[0,errors,0],{id:'backup-batch',names:['queued','backup','stored']},[],[0,0,errors]);list.innerHTML=operationTable('backup-batch',rows,[['queued','Queued'],['backup','Backing up'],['stored','Stored']]);});
}
async function restoreBackup(button){
 const form=button.closest('form'),target=form.elements.target_device_id.value,confirmation=prompt(`This replaces the selected protected configuration and requires a restart. Type "restore ${target}" to continue.`);
 if(confirmation===null)return;
 const notice=form.querySelector('.backup-preview'),labels=['Preview configuration','Apply configuration','Restart required'];
 const show=(stage,status,detail)=>{notice.className='backup-preview';notice.innerHTML=singleActionFlow(labels,{stage,status})+`<p class="status ${status==='failed'?'error':status==='complete'?'success':''}" role="status">${esc(detail)}</p>`;};
 button.disabled=true;show(1,'running','Applying the selected configuration. Do not restart the device yet.');
 try{
  await api(`api/backups/${form.dataset.backupId}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_device_id:target,sections:restoreSections(form),confirmation})});
  show(3,'complete','Configuration restored and restart marked as required. Device restart has not been confirmed.');
  try{await refreshAll();}catch(error){showWorkspaceError('Restore succeeded; workspace refresh failed',error);}
 }catch(error){show(1,'failed',error.message);}finally{button.disabled=false;}
}
