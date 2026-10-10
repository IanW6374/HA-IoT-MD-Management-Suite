function logView(){return new URLSearchParams(location.search).get('view')==='audit'?'audit':'activity'}
function logStatusDot(status,description=''){
  const value=String(status||'observed'),normalized=value.toLowerCase();
  const tone=['failed','partial','error','interrupted','rolled_back','unavailable','rejected','denied','startup_failed'].includes(normalized)?'bad':
    ['queued','active','running','checking','staging','scheduled','installing','staged'].includes(normalized)?'warn':
    ['complete','completed','success','healthy','confirmed','recovery prepared','image verified'].includes(normalized)?'good':'neutral';
  const label=description?`${value.replaceAll('_',' ')} — ${description}`:value.replaceAll('_',' ');
  return `<span class="event-dot log-dot ${tone}" role="img" tabindex="0" aria-label="${esc(label)}" data-tooltip="${esc(label)}" title="${esc(label)}"></span>`;
}
function timeline(items){return items.map(item=>`<article class="event"><time class="event-time">${esc(when(item.time))}</time>${logStatusDot(item.status)}<div><strong>${esc(item.title)}</strong><p>${esc([item.subject,item.target,item.detail].filter(Boolean).join(' · '))}</p></div></article>`).join('')||'<p class="muted">No matching log entries.</p>'}
function renderActionHistory(){
  const box=document.getElementById('action-history'),control=document.getElementById('action-history-filter');if(!box||!control)return;
  backupState.items=state.backups||[];
  const requested=new URLSearchParams(location.search).get('history'),filter=['deployments','backups','seed'].includes(requested)?requested:control.value,query=document.getElementById('action-history-search').value;
  control.value=filter;
  const records=[],matches=values=>matchesCatalogSearch(values,query);
  if(['all','deployments'].includes(filter))for(const item of state.deployments.filter(value=>['complete','failed','partial','staged','cancelled'].includes(value.status))){
    const devices=item.targets.map(id=>{const device=state.devices.find(value=>value.id===id);return [id,device?.host,device?.name,device?.description,item.results?.[id]?.status,item.results?.[id]?.detail].join(' ')});
    if(matches([item.id,item.update?.version,item.update?.release_type,item.profile_name,item.status,item.activation,...devices]))records.push({time:Number(item.created_at)||0,html:deploymentHistoryItem(item)});
  }
  if(['all','backups'].includes(filter))for(const item of backupState.items)if(matches([item.device_id,item.device_name,item.application_version,item.firmware_version,item.source,'complete encrypted backup']))records.push({time:Number(item.created_at)||0,html:backupHistoryItem(item)});
  if(['all','seed'].includes(filter))for(const item of (state.seedJobs||[]).filter(job=>['complete','failed','interrupted'].includes(job.status)))if(matches([item.image,item.kind,item.detail,item.status,item.status==='complete'?'Recovery prepared Image verified':'']))records.push({time:Number(item.created_at)||0,html:seedHistoryItem(item)});
  records.sort((a,b)=>b.time-a.time);
  replacePreservingDetails(box,records.map(item=>item.html).join('')||'<p class="muted">No matching completed actions.</p>');revealRequestedBackup();
}
function setActionHistoryFilter(){
  const query=new URLSearchParams(location.search),value=document.getElementById('action-history-filter').value;
  if(value==='all')query.delete('history');else query.set('history',value);
  query.set('view','activity');history.replaceState({},'',`logs?${query}`);renderActionHistory();
}
function renderActivity(){
  const control=document.getElementById('activity-filter'),requested=new URLSearchParams(location.search).get('filter'),filter=control.value!=='all'?control.value:(requested||'all');
  if([...control.options].some(option=>option.value===filter))control.value=filter;
  let items=activityItems();
  if(filter==='attention')items=(state.attention.items||[]).filter(item=>!item.acknowledged).map(item=>({time:item.time,title:item.title,status:item.status,subject:item.kind,target:'',detail:item.detail}));
  else if(filter==='failures')items=items.filter(item=>['failed','partial','error','interrupted'].includes(item.status));
  else if(filter==='devices')items=items.filter(item=>item.kind==='device');
  else if(filter==='deployments')items=items.filter(item=>item.kind==='deployment');
  else if(filter==='backups')items=items.filter(item=>item.kind==='backup');
  const search=document.getElementById('audit-search').value;
  items=items.filter(item=>matchesCatalogSearch([item.title,item.action,item.status,item.subject,item.target,item.detail],search));
  document.getElementById('activity-timeline').innerHTML=timeline(items);renderActionHistory();renderAttention();
}
function auditEndpoint(name){const query=document.getElementById('audit-search')?.value||'';return `api/${name}${['audit','events'].includes(name)&&query?'?q='+encodeURIComponent(query):''}`}
let auditSearchTimer=0,auditSearchGeneration=0;
function searchAudit(){
  renderActivity();clearTimeout(auditSearchTimer);const generation=++auditSearchGeneration;
  auditSearchTimer=setTimeout(async()=>{try{
    const query=document.getElementById('audit-search').value,[audit,events]=await Promise.all(['audit','events'].map(name=>api(auditEndpoint(name))));
    if(generation!==auditSearchGeneration||query!==document.getElementById('audit-search').value)return;
    state.audit=audit.audit||[];state.events=events.events||[];renderActivity();
  }catch(error){if(generation===auditSearchGeneration)document.getElementById('activity-timeline').textContent='Log search failed: '+error.message}},250);
}
function initLogsViews(){
  if(document.body.dataset.page!=='activity')return;
  const params=new URLSearchParams(location.search),view=logView(),attention=params.get('filter')==='attention';
  if(params.get('filter')==='seed')document.getElementById('action-history-filter').value='seed';
  document.getElementById('logs-title').textContent=view==='audit'?'Audit log':'Activity log';
  for(const section of document.querySelectorAll('[data-log-view]'))section.classList.toggle('view-hidden',section.dataset.logView!==view&&!(attention&&section.querySelector('#attention-list')));
  for(const link of document.querySelectorAll('[data-log-nav]')){const active=link.dataset.logNav===view;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page')}
}
initLogsViews();
