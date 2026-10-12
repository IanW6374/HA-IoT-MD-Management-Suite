import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {JSDOM}=require(process.env.IOT_UI_NODE_MODULES?`${process.env.IOT_UI_NODE_MODULES}/jsdom`:'jsdom');
const portal=fs.readFileSync(new URL('../iot_md_management/rootfs/app/management_portal.py',import.meta.url),'utf8');
const actions=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/action_progress.js',import.meta.url),'utf8');
const progressCSS=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/action_progress.css',import.meta.url),'utf8');
const flows=portal.slice(portal.indexOf('function milestoneFlow('),portal.indexOf('function deploymentTargets('));
function fixture(count=100,type='universal'){
 const dom=new JSDOM('<main id="deployments"></main><section id="backup-operations"><div id="backup-progress"></div><div id="backup-active-list"></div></section><div id="seed-progress"></div>',{runScripts:'outside-only'}),w=dom.window;
 w.state={devices:Array.from({length:count},(_,i)=>({id:'device-'+i,host:`iot-md-${String(i).padStart(3,'0')}.local`,description:'Device '+i}))};
 w.deployment={id:'batch',status:'active',targets:w.state.devices.map(device=>device.id),results:{},update:type==='profile'?{}:{version:'3.0.0-alpha.115',release_type:type}};
 for(const [i,id] of w.deployment.targets.entries())w.deployment.results[id]={status:i<10?'complete':i<20?'failed':i<30?'cancelled':i<40?'queued':'staging',detail:'Detail '+i,update_phase:'core_write',update_percent:42,update_milestones:['queued','inspect']};
 w.esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');w.when=value=>String(value??'');
 w.statusBadge=status=>`<span class="badge">${status}</span>`;
 w.updateCancelDeviceBadge=()=>'<button class="cancel-badge">Cancel</button>';w.updateCancelAllBadge=()=>'';w.updateCancelError=()=>'';
 w.backupState={pending:null};w.renderDeployments=()=>{w.document.getElementById('deployments').innerHTML=w.activeDeploymentCard(w.deployment)};
 w.eval(flows+'\n'+actions);w.renderDeployments();return {dom,w,doc:w.document};
}
test('diagnostic download remains available for completed deployment and URL-encodes its identity',()=>{
 const {dom,w}=fixture(3);try{
  const link=new JSDOM(w.progressTraceLink({id:'batch?<script>&',status:'complete'}));
  const anchor=link.window.document.querySelector('a');
  assert.equal(anchor.textContent,'Download diagnostics');
  assert.equal(anchor.getAttribute('download'),'iot-md-progress-traces.json');
  assert.equal(anchor.getAttribute('href'),'api/progress-traces?deployment=batch%3F%3Cscript%3E%26');
  assert.equal(link.window.document.querySelector('script'),null);
  link.window.close();
 }finally{dom.window.close();}
});
test('100-target batch has bounded rows and separate outcomes, not a made-up overall percentage',()=>{
 const {dom,doc}=fixture();try{
  assert.equal(doc.querySelectorAll('.operation-device-row').length,25);
  assert.equal(doc.querySelector('.batch-summary'),null);
  assert.match(doc.querySelector('.operation-pager').textContent,/1–25 of 100/);
  assert.ok(doc.querySelectorAll('[data-milestone-state="Failed"]').length);
  assert.equal(doc.querySelectorAll('.milestone-counts,.milestone-running,.milestone-errors').length,0);
  assert.match(doc.querySelector('[data-milestone-state="Failed"] .flow-ring').getAttribute('aria-label'),/60 in progress.*10 failed here/);
  const ring=doc.querySelector('[data-milestone-state="Failed"] .flow-ring');
  assert.equal(ring.style.getPropertyValue('--ring-complete-end'),'10%');
  assert.equal(ring.style.getPropertyValue('--ring-active-end'),'70%');
  assert.equal(ring.style.getPropertyValue('--ring-failed-end'),'80%');
 }finally{dom.window.close();}
});
test('live-read freshness reports stale/retrying data without inventing completion',()=>{
 const {w,doc}=fixture(3);try{
  const now=Math.floor(Date.now()/1000);
  const target=w.deployment.results['device-1'];target.status='staging';target.progress_poll={last_received:now-20,stale_after_s:15,duration_ms:81,error:''};
  w.renderDeployments();
  const node=doc.querySelector('.operation-freshness');assert.ok(node);
  assert.match(node.textContent,/Updated 20s ago — stale/);assert.ok(node.classList.contains('warn'));
  target.progress_poll.error='TLS handshake timed out <img src=x>';
  w.renderDeployments();
  assert.match(doc.querySelector('.operation-freshness').textContent,/retrying/);
  assert.match(doc.querySelector('.operation-freshness').title,/Status read failed: TLS handshake timed out <img src=x>/);
  assert.equal(doc.querySelector('img'),null);
  target.status='complete';w.renderDeployments();assert.equal(doc.querySelector('.operation-freshness'),null);
 }finally{w.close();}
});
test('freshness ages in place without replacing the focused device filter',()=>{
 const {w,doc}=fixture(3);try{
  const now=Math.floor(Date.now()/1000);w.Date.now=()=>now*1000;
  const target=w.deployment.results['device-1'];target.status='staging';target.progress_poll={last_received:now-2,stale_after_s:15};
  w.renderDeployments();
  const node=doc.querySelector('.operation-freshness'),input=doc.querySelector('[data-operation-field="query"]');input.focus();
  w.Date.now=()=>(now+30)*1000;w.refreshProgressFreshness();
  assert.equal(doc.querySelector('.operation-freshness'),node);assert.equal(doc.activeElement,input);
  assert.match(node.textContent,/Updated 32s ago — stale/);
 }finally{w.close();}
});
test('shared ring styles use proportional state colours without extra label rows',()=>{
 const {w,doc}=fixture(3);try{
  w.deployment.results['device-1']={status:'staging',update_phase:'core_write',update_milestones:['queued','inspect']};
  w.deployment.results['device-2']={status:'failed',update_phase:'application_verify',update_milestones:['queued','inspect']};
  w.renderDeployments();
  const style=doc.createElement('style');style.textContent=progressCSS;doc.head.append(style);
  assert.match(progressCSS,/background:conic-gradient\(var\(--good\).*var\(--warn\).*var\(--bad\).*var\(--line\)/);
  for(const [state,tone] of [['Not running','line'],['Active','warn'],['Failed','bad'],['Complete','good']]){
   const step=doc.querySelector(`[data-milestone-state="${state}"]`);assert.ok(step,state);
   assert.equal(w.getComputedStyle(step).getPropertyValue('--ring-tone'),`var(--${tone})`);
   assert.equal(w.getComputedStyle(step.querySelector('strong')).minHeight,'44.8px');
   assert.match(step.title,new RegExp(`^${state}:`));
  }
 }finally{w.close();}
});
test('milestone filters current work rather than devices which already passed it',()=>{
 const {dom,w,doc}=fixture();try{
  w.filterOperationStage(doc.querySelector('[data-stage="core_write"]'));
  assert.match(doc.querySelector('.operation-pager').textContent,/70 matching/);
  assert.ok([...doc.querySelectorAll('.operation-device-row')].every(row=>row.textContent.includes('42% of this stage')));
  w.filterOperationStage(doc.querySelector('[data-stage="complete"]'));
  assert.equal(doc.querySelectorAll('.operation-device-row').length,10);
  assert.ok([...doc.querySelectorAll('.operation-device-row')].every(row=>row.textContent.includes('Confirmed')));
 }finally{w.close();}
});
test('search, focused text/caret, stage, outcome, selection and page survive refresh',()=>{
 const {w,doc}=fixture();try{
  const search=doc.querySelector('[data-operation-field="query"]');search.value='iot-md';search.focus();search.setSelectionRange(3,5);w.changeOperationView(search);
  const outcome=doc.querySelector('[data-operation-field="outcome"]');outcome.value='running';w.changeOperationView(outcome);
  w.pageOperation(doc.querySelector('[data-direction="1"]'));
  w.selectOperationDevice(doc.querySelector('.operation-select'));
  const selected=doc.querySelector('.operation-detail-row').textContent;
  const fresh=doc.querySelector('[data-operation-field="query"]');fresh.focus();fresh.setSelectionRange(2,4);
  w.renderDeployments();
  assert.equal(doc.activeElement.dataset.operationField,'query');assert.equal(doc.activeElement.selectionStart,2);assert.equal(doc.activeElement.selectionEnd,4);
  assert.match(doc.querySelector('.operation-pager').textContent,/26–50 of 60/);
  assert.equal(doc.querySelector('[data-operation-field="outcome"]').value,'running');assert.equal(doc.querySelector('.operation-detail-row').textContent,selected);
 }finally{w.close();}
});
test('application, core and profile actions omit duplicate summaries and share rings/table',()=>{
 for(const type of ['application','firmware','profile']){
  const {w,doc}=fixture(3,type);try{
   assert.equal(doc.querySelectorAll('.operation-device-row').length,3);
   assert.equal(doc.querySelectorAll('progress').length,0);
   assert.equal(doc.querySelector('.batch-summary'),null);
   assert.ok([...doc.querySelectorAll('.flow-ring')].every(ring=>ring.style.getPropertyValue('--ring-complete-end')==='100%'));
   assert.equal(doc.querySelectorAll('.flow-step').length,type==='profile'?3:4);
   if(type==='profile')assert.doesNotMatch(doc.getElementById('deployments').textContent,/Restart & install|Confirm healthy/);
  }finally{w.close();}
 }
});
test('profile failures keep confirmation grey and explain the blocked share',()=>{
 const {w,doc}=fixture(3,'profile');try{
  w.deployment.results['device-1']={status:'running'};
  w.deployment.results['device-2']={status:'failed'};
  w.renderDeployments();
  const rings=doc.querySelectorAll('.flow-ring');
  assert.match(rings[1].getAttribute('aria-label'),/1 in progress; 1 failed here/);
  assert.match(rings[2].getAttribute('aria-label'),/1 blocked by an earlier failure/);
  assert.equal(rings[2].style.getPropertyValue('--ring-active-end'),rings[2].style.getPropertyValue('--ring-failed-end'));
 }finally{w.close();}
});
test('restart and missing/invalid measurements have no numeric progress',()=>{
 const {w,doc}=fixture(3);try{
  w.deployment.results['device-0']={status:'installing',update_phase:'install',update_percent:100};
  w.deployment.results['device-1']={status:'staging',update_phase:'core_write',update_percent:NaN};
  w.deployment.results['device-2']={status:'staging',update_phase:'core_write',update_percent:101};
  w.renderDeployments();assert.equal(doc.querySelectorAll('progress').length,0);
  assert.ok([...doc.querySelectorAll('.operation-device-row')].every(row=>!row.textContent.includes('%')));
 }finally{w.close();}
});
test('backup rings retain completed and failed jobs when active jobs shrink; stale reads cannot rewind terminal state',async()=>{
 const {w,doc}=fixture(3);try{
  const jobs=[{id:1,kind:'backup',target:'device-0',status:'complete',updated_at:5},{id:2,kind:'backup',target:'device-1',status:'running',updated_at:5},{id:3,kind:'backup',target:'device-2',status:'failed',updated_at:5,last_error:'Transport failed'}];
  w.renderBackupProgress(jobs);
  w.api=async path=>{assert.equal(path,'api/jobs?ids=1,2,3');return {jobs};};
  w.renderBackupProgress(await w.trackedBackupJobs({active_jobs:[jobs[1]]}));
  const backingUp=doc.querySelectorAll('#backup-progress .flow-ring')[1];
  assert.match(backingUp.getAttribute('aria-label'),/1 of 3.*1 in progress.*1 failed here/);
  assert.ok(Math.abs(parseFloat(backingUp.style.getPropertyValue('--ring-complete-end'))-100/3)<0.000001);
  assert.ok(Math.abs(parseFloat(backingUp.style.getPropertyValue('--ring-active-end'))-200/3)<0.000001);
  assert.equal(backingUp.style.getPropertyValue('--ring-failed-end'),'100%');
  assert.match(doc.querySelectorAll('#backup-progress .flow-ring')[2].getAttribute('aria-label'),/1 blocked by an earlier failure/);
  assert.equal(doc.querySelector('.batch-summary'),null);
  w.renderBackupProgress(jobs.map(job=>({...job,status:'queued',updated_at:4})));
  assert.match(doc.querySelectorAll('#backup-progress .flow-ring')[1].getAttribute('aria-label'),/1 of 3/);
  assert.equal(doc.querySelectorAll('#backup-active-list .operation-device-row').length,3);
  assert.equal(doc.querySelectorAll('#backup-active-list progress').length,0);
  w.selectOperationDevice(doc.querySelector('#backup-active-list .operation-select'));
  assert.equal(doc.querySelectorAll('#backup-active-list .operation-detail-row').length,1);
 }finally{w.close();}
});
test('backup batch validates read responses and chunks large fleets into bounded requests',async()=>{
 const {w}=fixture(3);try{
  let calls=0;w.api=async path=>{calls++;const ids=path.split('=')[1].split(',');return {jobs:ids.map(id=>({id:Number(id),kind:'backup'}))};};
  assert.equal((await w.readBackupJobs(Array.from({length:1100},(_,i)=>i+1))).length,1100);assert.equal(calls,3);
  w.api=async()=>({jobs:[]});await assert.rejects(w.readBackupJobs([1]),/Invalid backup/);
 }finally{w.close();}
});
test('USB/reset and restore flow use completion counts and never claim boot success or a reboot percent',()=>{
 const {w}=fixture(3);try{
  const html=w.singleActionFlow(['Write image','Verify image','Request reboot'],{stage:2,status:'running',percent:50});
  const doc=new JSDOM(html).window.document;
  assert.equal(doc.querySelectorAll('b').length,3);assert.equal(doc.querySelectorAll('b')[2].textContent,'0/1');assert.doesNotMatch(doc.body.textContent,/%/);
  const failed=w.singleActionFlow(['Preview configuration','Apply configuration','Restart required'],{stage:1,status:'failed'});
  assert.match(failed,/1 failed here/);assert.doesNotMatch(failed,/1 running/);
  assert.match(failed,/blocked by an earlier failure/);
 }finally{w.close();}
});
test('restore acknowledges configuration without claiming restart; read failure never repeats a write',async()=>{
 const {w,doc}=fixture(3);try{
  doc.body.insertAdjacentHTML('beforeend','<form data-backup-id="1"><input name="target_device_id" value="device-0"><div class="backup-preview"></div><button>Apply</button></form>');
  w.prompt=()=> 'restore device-0';w.restoreSections=()=>['network'];let writes=0;
  w.api=async()=>{writes++;return {result:{accepted:true}};};w.refreshAll=async()=>{throw Error('Read timed out');};w.showWorkspaceError=()=>{};
  await w.restoreBackup(doc.querySelector('form button'));
  assert.equal(writes,1);assert.match(doc.querySelector('.backup-preview').textContent,/restart has not been confirmed/);
 }finally{w.close();}
});
