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
