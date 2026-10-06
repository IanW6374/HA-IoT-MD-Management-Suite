// Embedded into the portal before initialization; no additional network request.
let releaseSearch='',profileSearch='',releasePromotionError='';
const releasePromotionPending=new Set();
const healthStaleAfter=__HEALTH_STALE_AFTER__;

function apiConnectionStatus(device,now=Date.now()/1000){
  if(device.enabled===false)return {status:'disabled',tone:'unknown',label:'API: management disabled'};
  if(device.last_error)return {status:'unavailable',tone:'bad',label:'API unavailable: '+device.last_error};
  if(!device.last_seen)return {status:'unknown',tone:'unknown',label:'API: not yet connected'};
  if(now-Number(device.last_seen)>healthStaleAfter)return {status:'stale',tone:'warn',label:'API: connection status is stale; last checked '+when(device.last_seen)};
  return {status:'healthy',tone:'good',label:'API connected; last checked '+when(device.last_seen)};
}
function healthLED(status){return `<span class="device-health-led ${esc(status.tone)}" role="img" aria-label="${esc(status.label)}" title="${esc(status.label)}" tabindex="0"></span>`}
function deviceHealthStatus(device,now=Date.now()/1000){
  const apiStatus=apiConnectionStatus(device,now);
  if(apiStatus.tone!=='good')return {tone:'unknown',label:device.last_seen?'Device health unknown: last report is stale ('+when(device.last_seen)+'); refresh when API is available':'Device health unknown: no report received'};
  const inventory=device.inventory?.device||{},runtime=inventory.runtime||{},lifecycle=runtime.lifecycle||{},boot=inventory.boot||{},observation=inventory.qualification_observation||{},tasks=runtime.tasks||{},services=runtime.state||{},issues=[],warnings=[];
  const deviceState=lifecycle.device_state||services.device_state||boot.device_state;
  const state=observation.health_state;
  if(state==='failed'||lifecycle.state==='failed'||deviceState==='safe')issues.push(lifecycle.error||lifecycle.device_state_reason||boot.reason||'Device reports a runtime failure');
  else if(state==='degraded'||deviceState==='degraded')warnings.push(lifecycle.device_state_reason||'Device reports degraded operation');
  for(const [name,task] of Object.entries(tasks)){
    if(task.status==='failed'||task.state==='failed')(task.critical?issues:warnings).push(name+': '+(task.error||'task failed'));
    else if(task.status==='degraded'||task.state==='degraded')warnings.push(name+': '+(task.error||'degraded'));
  }
  for(const name of ['network','portal','api','mqtt','ntp','watchdog'])if(['failed','degraded','offline','unavailable','error'].includes(services[name]))warnings.push(name+': '+services[name]);
  if(issues.length)return {tone:'bad',label:'Device health failed: '+[...new Set(issues)].join('; ')};
  if(warnings.length)return {tone:'warn',label:'Device health degraded: '+[...new Set(warnings)].join('; ')};
  if(deviceState&&deviceState!=='running')return {tone:'warn',label:'Device health: '+deviceState+(lifecycle.device_state_reason?'; '+lifecycle.device_state_reason:'')};
  if(state==='healthy'||deviceState==='running')return {tone:'good',label:'Device reports healthy operation; last checked '+when(device.last_seen)};
  return {tone:'unknown',label:'Device health unknown: no current runtime health state reported'};
}
function deviceConnectionBadges(device){
  return healthLED(apiConnectionStatus(device));
}
function deviceRefreshButton(device){
  if(device.enabled===false)return '';
  const retry=deviceRetries.get(device.id),pending=Boolean(retry?.pending),title=retry?.error||'Refresh connection and device health';
  return `<button type="button" class="badge device-retry ${retry?.error?'bad':''}" data-device-id="${esc(device.id)}" onclick="pollDevice(this.dataset.deviceId,this)" title="${esc(title)}" aria-label="${esc(title)}" aria-busy="${pending}" ${pending?'disabled':''}><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20 7v5h-5M4 17v-5h5M6.1 8a7 7 0 0 1 11.6-2L20 9M4 15l2.3 3A7 7 0 0 0 17.9 16"/></svg></button>`;
}
function fleetHealthStatus(devices,now=Date.now()/1000){
  const enabled=devices.filter(device=>device.enabled!==false),counts={healthy:0,failed:0,degraded:0,unknown:0},details=[];
  for(const device of enabled){
    const api=apiConnectionStatus(device,now),health=deviceHealthStatus(device,now);
    const category=api.tone==='bad'||health.tone==='bad'?'failed':api.tone==='warn'||health.tone==='warn'?'degraded':api.tone==='good'&&health.tone==='good'?'healthy':'unknown';
    counts[category]++;
  }
  for(const name of ['healthy','failed','degraded','unknown'])if(counts[name])details.push(`${counts[name]} ${name}`);
  return {tone:counts.failed?'bad':counts.degraded||counts.unknown?'warn':enabled.length?'good':'unknown',label:enabled.length?`Fleet health (API and device): ${details.join(', ')}. Disabled devices excluded.`:'Fleet health: no enabled devices'};
}
function renderFleetHealth(){
  const indicator=document.getElementById('devices-menu-health');if(!indicator)return;
  const status=fleetHealthStatus(state.devices);
  indicator.className='device-health-led '+status.tone;
  indicator.title=status.label;indicator.setAttribute('aria-label',status.label);
  const menu=indicator.closest('button');if(menu)menu.title=status.label;
}
function matchesCatalogSearch(values,query){const searchable=values.join(' ').toLocaleLowerCase();return query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean).every(word=>searchable.includes(word))}
function filteredReleases(){return (releaseFilter==='all'?state.releases:state.releases.filter(item=>(item.channels||[]).length)).filter(item=>matchesCatalogSearch([item.version,item.tag,...(item.channels||[]),item.release_sequence,item.source_revision,item.prerelease?'pre-release':'production'],releaseSearch))}
function filteredProfiles(){return state.profiles.filter(item=>matchesCatalogSearch([item.name,item.description,...Object.keys(item.settings||{}),...Object.keys(item.secrets||{})],profileSearch))}
function setCatalogSearch(kind,value){if(kind==='releases'){releaseSearch=value;renderReleases(releaseSyncState)}else{profileSearch=value;renderProfiles()}}
function renderReleases(sync,force=false){
  releaseSyncState=sync||releaseSyncState;sync=releaseSyncState;
  const inventory=state.releaseInventory,box=document.getElementById('releases'),items=filteredReleases(),focused=box.contains(document.activeElement)?document.activeElement.dataset.tag:null;
  if(!releasePromotionPending.size&&(!focused||force)){
    box.innerHTML=items.length?`<div class="catalog-table-wrap"><table class="catalog-table release-table"><caption class="visually-hidden">Verified releases and promotion channels</caption><thead><tr><th scope="col">Version</th><th scope="col">Channel</th><th scope="col">Sequence</th><th scope="col">Release type</th><th scope="col">Source revision</th></tr></thead><tbody>${items.map(item=>{
      const assigned=(item.channels||[])[0]||'none',revision=String(item.source_revision||'');
      return `<tr><td><strong>${esc(item.version||item.tag)}</strong><span class="visually-hidden">Verified</span></td><td><select aria-label="Release channel for ${esc(item.version||item.tag)}" class="release-channel" data-tag="${esc(item.tag)}" onchange="setReleaseChannel(this)"><option value="none" ${assigned==='none'?'selected':''}>Not promoted</option><option value="stable" ${assigned==='stable'?'selected':''}>Stable</option><option value="beta" ${assigned==='beta'?'selected':''}>Beta</option><option value="alpha" ${assigned==='alpha'?'selected':''}>Alpha</option></select></td><td>${esc(item.release_sequence)}</td><td>${item.prerelease?'Pre-release':'Production'}</td><td><code class="release-fingerprint" title="${esc(revision)}">${esc(revision.slice(0,12)||'—')}</code></td></tr>`;
    }).join('')}</tbody></table></div>`:`<p class="muted catalog-empty">${releaseSearch?'No releases match your search.':releaseFilter==='promoted'?'No releases are promoted. Select All to assign a channel.':'No verified releases imported.'}</p>`;
    if(focused)[...box.querySelectorAll('.release-channel')].find(control=>control.dataset.tag===focused)?.focus({preventScroll:true});
  }
  for(const view of ['promoted','all']){const button=document.getElementById('release-filter-'+view);button?.classList.toggle('active',view===releaseFilter);button?.setAttribute('aria-pressed',String(view===releaseFilter))}
  document.getElementById('release-list-count').textContent=`${items.length} of ${state.releases.length} releases`;
  const last=inventory.last_sync?when(inventory.last_sync):'Never';
  document.getElementById('release-status').textContent=releasePromotionError||(sync.running?'Synchronization in progress':inventory.last_error||`Last synchronized: ${last}`);
  document.getElementById('release-sync').disabled=!sync.enabled||sync.running;
  document.getElementById('setting-sync-enabled').textContent=sync.enabled?'Yes':'No';
}
async function setReleaseChannel(control){
  const tag=control.dataset.tag;releasePromotionPending.add(tag);releasePromotionError='';control.disabled=true;
  try{await api('api/releases/promote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag,channel:control.value})});await refreshAll()}
  catch(error){releasePromotionError=error.message}
  finally{releasePromotionPending.delete(tag);renderReleases(releaseSyncState,true)}
}
function renderProfiles(){
  const box=document.getElementById('profiles'),items=filteredProfiles();
  replacePreservingDetails(box,items.length?`<div class="catalog-table-wrap"><table class="catalog-table profile-table"><caption class="visually-hidden">Saved configuration profiles. Expand a profile to review its included settings.</caption><thead><tr><th scope="col">Name / included settings</th><th scope="col">Description</th><th scope="col">Items</th><th scope="col">Updated</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead><tbody>${items.map(item=>`<tr><td><details data-disclosure-key="profile-${esc(item.name)}"><summary><strong>${esc(item.name)}</strong></summary><dl class="profile-settings-list">${Object.entries(item.settings||{}).map(([name,value])=>`<dt>${esc(profileFieldLabel(name))}</dt><dd>${esc(Array.isArray(value)?value.join(', '):String(value))}</dd>`).join('')}${Object.keys(item.secrets||{}).map(name=>`<dt>${esc(profileFieldLabel(name))}</dt><dd><code>********</code></dd>`).join('')}</dl></details></td><td><span class="catalog-description" title="${esc(item.description||'')}">${esc(item.description||'—')}</span></td><td>${Object.keys(item.settings||{}).length+Object.keys(item.secrets||{}).length}</td><td>${esc(when(item.updated_at))}</td><td><button class="danger compact" type="button" data-profile-name="${esc(item.name)}" onclick="deleteProfile(encodeURIComponent(this.dataset.profileName))">Delete</button></td></tr>`).join('')}</tbody></table></div>`:`<p class="muted catalog-empty">${profileSearch?'No profiles match your search.':'No profiles saved.'}</p>`);
  document.getElementById('profile-list-count').textContent=`${items.length} of ${state.profiles.length} profiles`;
}
function fleetSubview(page){const requested=new URLSearchParams(location.search).get('view');return requested===(page==='devices'?'enrol':'new')?requested:'list'}
function initFleetViews(){
  const page=document.body.dataset.page;
  if(['devices','profiles'].includes(page)){
    const view=fleetSubview(page);document.body.dataset.fleetView=view;
    for(const section of document.querySelectorAll(`[data-page-section="${page}"] [data-fleet-view]`))section.classList.toggle('view-hidden',section.dataset.fleetView!==view);
    if(page==='devices'&&view==='enrol'){
      document.querySelector('[data-page-section="devices"] h1').textContent='Enrol device';
      document.querySelector('[data-page-section="devices"] .hero p:not(.eyebrow)').textContent='Connect a device by hostname using your configured mutual-TLS credentials.';
    }
    if(page==='profiles'&&view==='new')document.querySelector('[data-page-section="profiles"] h1').textContent='New profile';
  }
}
function initFleetNavigation(){
  const nav=document.querySelector('nav[aria-label="Primary"]'),toggle=document.getElementById('nav-toggle'),groups=[...nav.querySelectorAll('.nav-group')];
  const close=except=>{for(const group of groups)if(group!==except){clearTimeout(group.openTimer);clearTimeout(group.closeTimer);group.classList.remove('open');group.querySelector('.nav-menu-trigger').setAttribute('aria-expanded','false')}};
  const open=group=>{close(group);clearTimeout(group.openTimer);clearTimeout(group.closeTimer);group.classList.add('open');group.querySelector('.nav-menu-trigger').setAttribute('aria-expanded','true')};
  toggle.addEventListener('click',()=>{const expanded=nav.classList.toggle('open');toggle.setAttribute('aria-expanded',String(expanded));if(!expanded)close()});
  for(const group of groups){
    const trigger=group.querySelector('.nav-menu-trigger'),links=[...group.querySelectorAll('.nav-dropdown a')],page=document.body.dataset.page,active=trigger.dataset.pageLink===page;
    trigger.classList.toggle('active',active);
    for(const link of links)if(active&&link.dataset.fleetNav===fleetSubview(page)){link.classList.add('active');link.setAttribute('aria-current','page')}
    trigger.addEventListener('click',()=>{if(group.classList.contains('open'))close();else open(group)});
    group.addEventListener('mouseenter',()=>{if(!matchMedia('(hover:hover)').matches)return;clearTimeout(group.closeTimer);group.openTimer=setTimeout(()=>open(group),260)});
    group.addEventListener('mouseleave',()=>{clearTimeout(group.openTimer);group.closeTimer=setTimeout(()=>{if(!group.contains(document.activeElement)){group.classList.remove('open');trigger.setAttribute('aria-expanded','false')}},180)});
    group.addEventListener('focusout',()=>setTimeout(()=>{if(!group.contains(document.activeElement)){group.classList.remove('open');trigger.setAttribute('aria-expanded','false')}},0));
    group.addEventListener('keydown',event=>{
      const index=links.indexOf(document.activeElement);
      if(event.key==='Escape'){event.preventDefault();close();trigger.focus()}
      else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();open(group);const next=event.key==='Home'?0:event.key==='End'?links.length-1:index<0?(event.key==='ArrowUp'?links.length-1:0):(index+(event.key==='ArrowDown'?1:-1)+links.length)%links.length;links[next]?.focus()}
    });
  }
  document.addEventListener('click',event=>{if(!nav.contains(event.target)&&event.target!==toggle){close();nav.classList.remove('open');toggle.setAttribute('aria-expanded','false')}});
}
initFleetViews();
initFleetNavigation();
