import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/management_portal.py',import.meta.url),'utf8');
const context=vm.createContext({});
const functions=source.slice(source.indexOf('function milestoneFlow('),source.indexOf('function deploymentTargets('));
vm.runInContext('function esc(value){return String(value)}\n'+functions,context);
function render(results,type='universal'){
  context.deployment={targets:Object.keys(results),results,update:{release_type:type}};
  return vm.runInContext('flowFor(deployment)',context);
}
function steps(html){return [...html.matchAll(/<div class="flow-step fleet-flow-step ([^"]*)" title="([^"]*)">.*?<strong>(.*?)<\/strong>/g)].map(([,tone,title,label])=>({tone,title,label}));}

test('reported three-device case highlights only inspection, not every partial milestone',()=>{
  const flow=steps(render({one:{status:'checking'},two:{status:'failed'},three:{status:'complete'}}));
  assert.equal(flow[0].tone,'done');
  assert.match(flow[1].tone,/current/);
  assert.match(flow[1].tone,/failed/);
  assert.match(flow[1].title,/1 of 3.*1 in progress.*1 failed here/);
  for(const step of flow.slice(2)){
    assert.equal(step.tone,'partial');
    assert.match(step.title,/1 of 3.*33%/);
    assert.doesNotMatch(step.title,/in progress|failed here/);
  }
});
test('different device phases can be active simultaneously without lighting future steps',()=>{
  const flow=steps(render({one:{status:'staging',update_phase:'core_write',update_milestones:['inspect']},
    two:{status:'staging',update_phase:'application_verify',update_milestones:['inspect','core_write','core_verify','application_download']},three:{status:'complete'}}));
  assert.deepEqual(flow.filter(step=>step.tone.includes('current')).map(step=>step.label),['Download & write core','Verify & stage application']);
  assert.equal(flow[8].tone,'partial');
});
test('stale completed telemetry phase does not mark an already completed milestone active',()=>{
  const flow=steps(render({one:{status:'installing',update_phase:'pair'},two:{status:'complete'}}));
  assert.deepEqual(flow.filter(step=>step.tone.includes('current')).map(step=>step.label),['Restart & install']);
});
test('queued and scheduled devices do not make future milestones active',()=>{
  for(const status of ['queued','scheduled','staged']){
    const flow=steps(render({one:{status},two:{status:'complete'}}));
    assert.ok(flow.every(step=>!step.tone.includes('current')));
  }
});
test('non-universal updates distinguish active installation from partially completed confirmation',()=>{
  const flow=steps(render({one:{status:'installing'},two:{status:'complete'}},'application'));
  assert.match(flow[2].tone,/current/);
  assert.equal(flow[3].tone,'partial');
});
test('backup progress keeps completed counts and highlights only the device actually backing up',()=>{
  const elements=Object.fromEntries(['backup-progress','backup-operations','backup-active-list'].map(id=>[id,{innerHTML:'',classList:{add(){},remove(){}}}]));
  context.document={getElementById:id=>elements[id]};context.state={devices:[]};
  const backup=source.slice(source.indexOf('function renderBackupProgress('),source.indexOf('function backupDeviceOptions('));
  vm.runInContext('function statusBadge(value){return value}\n'+backup,context);
  context.jobs=[{status:'running',target:'one'},{status:'complete',target:'two'},{status:'failed',target:'three'}];
  vm.runInContext('renderBackupProgress(jobs)',context);
  const flow=steps(elements['backup-progress'].innerHTML);
  assert.equal(flow[0].tone,'done');
  assert.match(flow[1].tone,/current/);
  assert.match(flow[1].tone,/failed/);
  assert.match(flow[1].title,/1 of 3.*1 in progress.*1 failed here/);
  assert.equal(flow[2].tone,'partial');
});

test('universal flow has detailed steps and no green installation tick during restart',()=>{
  const html=render({one:{status:'installing',milestone_rank:3,
    update_milestones:['queued','inspect','core_write','core_verify','application_download','application_verify','pair']}});
  assert.equal((html.match(/class="flow-step /g)||[]).length,9);
  assert.match(html,/Download & write core/);
  assert.match(html,/Verify & stage application/);
  assert.match(html,/milestone-progress:0%.*?Restart & install/);
  assert.equal((html.match(/<b>✓<\/b>/g)||[]).length,7);
});
test('each fleet milestone counts devices independently and keeps completed progress on failure',()=>{
  const html=render({one:{status:'complete'},two:{status:'failed',
    update_milestones:['queued','inspect','core_write']}});
  assert.match(html,/milestone-progress:50%.*?<b>1\/2<\/b>.*?Verify core/);
  assert.match(html,/milestone-progress:50%.*?<b>1\/2<\/b>.*?Confirm healthy/);
  assert.equal((html.match(/<b>✓<\/b>/g)||[]).length,3);
});
test('queued targets remain in the denominator even without a result entry',()=>{
  context.deployment={targets:['one','two'],results:{one:{status:'complete'}},update:{release_type:'universal'}};
  assert.match(vm.runInContext('flowFor(deployment)',context),/<b>1\/2<\/b>/);
});
test('staged devices complete all staging boundaries even when telemetry polls were missed',()=>{
  const html=render({one:{status:'staged'},two:{status:'installing'}});
  assert.equal((html.match(/<b>✓<\/b>/g)||[]).length,7);
  assert.match(html,/milestone-progress:0%.*?Restart & install/);
  assert.match(html,/milestone-progress:0%.*?Confirm healthy/);
});
test('all confirmed devices produce 100 percent and green ticks at every milestone',()=>{
  for(const type of ['universal','application','firmware']){
    const html=render({one:{status:'complete'},two:{status:'complete'},three:{status:'complete'}},type);
    const count=type==='universal'?9:4;
    assert.equal((html.match(/milestone-progress:100%/g)||[]).length,count);
    assert.equal((html.match(/<b>✓<\/b>/g)||[]).length,count);
  }
});
test('failed device retains prior staged milestones but does not count as confirmed',()=>{
  const html=render({one:{status:'complete'},two:{status:'failed',milestone_rank:2}});
  assert.equal((html.match(/<b>✓<\/b>/g)||[]).length,7);
  assert.match(html,/milestone-progress:50%.*?<b>1\/2<\/b>.*?Confirm healthy/);
});
