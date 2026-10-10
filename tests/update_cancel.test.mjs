import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url);
const {JSDOM}=require(process.env.IOT_UI_NODE_MODULES ? `${process.env.IOT_UI_NODE_MODULES}/jsdom` : 'jsdom');
const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/update_cancel.js',import.meta.url),'utf8');
function fixture(status='staging'){
  const deployment={id:'deploy',update:{release_sequence:123},targets:['one'],results:{one:{status}}};
  const context=vm.createContext({state:{deployments:[deployment],devices:[{id:'one',host:'one.local'}]},esc:value=>String(value).replaceAll('<','&lt;'),confirm:()=>true,renderDeployments(){},async refreshDeploymentProgress(){},showWorkspaceError(){}});
  vm.runInContext(source,context);return context;
}
test('queued and staged updates can cancel; installation and cancelling states disable controls',()=>{
  for(const status of ['queued','staging','staged','scheduled']){
    assert.doesNotMatch(vm.runInContext("updateCancelDeviceBadge(state.deployments[0],'one')",fixture(status)),/ disabled/);
  }
  for(const status of ['installing','cancelling']){
    assert.match(vm.runInContext("updateCancelDeviceBadge(state.deployments[0],'one')",fixture(status)),/ disabled/);
  }
  for(const status of ['complete','failed','cancelled'])assert.equal(vm.runInContext("updateCancelDeviceBadge(state.deployments[0],'one')",fixture(status)),'');
});
test('confirmation is required and a failed cancellation remains visible without automatic mutation replay',async()=>{
  const context=fixture();let calls=0;
  context.api=async(path,options)=>{calls++;assert.equal(path,'api/deployments/cancel');assert.deepEqual(JSON.parse(options.body),{deployment_id:'deploy',device_id:'one'});throw Error('Cancellation not confirmed')};
  context.button={dataset:{deployment:'deploy',device:'one'}};context.confirm=()=>false;
  await vm.runInContext('cancelDeploymentUpdate(button)',context);assert.equal(calls,0);
  context.confirm=()=>true;
  await vm.runInContext('cancelDeploymentUpdate(button)',context);assert.equal(calls,1);
  assert.match(vm.runInContext("updateCancelError(state.deployments[0],'one')",context),/role="alert">Cancellation not confirmed/);
  assert.equal(context.state.deployments[0].results.one.status,'staging');
});
test('accepted cancellation reflects acknowledgement, not an invented successful completion',async()=>{
  const context=fixture();context.button={dataset:{deployment:'deploy',device:'one'}};
  context.api=async()=>({...context.state.deployments[0],results:{one:{status:'cancelling'}}});
  await vm.runInContext('cancelDeploymentUpdate(button)',context);
  assert.equal(context.state.deployments[0].results.one.status,'cancelling');
  assert.match(vm.runInContext("updateCancelDeviceBadge(state.deployments[0],'one')",context),/Cancelling…/);
});

function batchFixture(){
  const context=fixture();
  const statuses={one:'queued',two:'staging',done:'complete',failed:'failed',installing:'installing',cancelled:'cancelled',cancelling:'cancelling'};
  context.state.deployments[0].targets=Object.keys(statuses);
  context.state.deployments[0].results=Object.fromEntries(Object.entries(statuses).map(([id,status])=>[id,{status}]));
  context.button={dataset:{deployment:'deploy'}};
  return context;
}
test('Cancel all confirms once and cancels only eligible devices sequentially',async()=>{
  const context=batchFixture(),calls=[];let confirmations=0,active=0;
  context.confirm=()=>{confirmations++;return true;};
  context.api=async(path,options)=>{
    assert.equal(++active,1);
    const {device_id}=JSON.parse(options.body);calls.push(device_id);
    await Promise.resolve();active--;
    const deployment=context.state.deployments[0];
    return {...deployment,results:{...deployment.results,[device_id]:{status:'cancelling'}}};
  };
  await vm.runInContext('cancelAllDeploymentUpdates(button)',context);
  assert.equal(confirmations,1);assert.deepEqual(calls,['one','two']);
  assert.equal(context.state.deployments[0].results.installing.status,'installing');
  assert.equal(context.state.deployments[0].results.one.status,'cancelling');
  assert.match(vm.runInContext('updateCancelAllBadge(state.deployments[0])',context),/disabled/);
});
test('Cancel all continues after an unconfirmed device; no POST is replayed on refresh failure',async()=>{
  const context=batchFixture(),calls=[],errors=[];
  context.api=async(path,options)=>{
    const {device_id}=JSON.parse(options.body);calls.push(device_id);
    if(device_id==='one')throw Error('<Unconfirmed>');
    const deployment=context.state.deployments[0];
    return {...deployment,results:{...deployment.results,[device_id]:{status:'cancelled'}}};
  };
  context.refreshDeploymentProgress=async()=>{throw Error('Read failed');};
  context.showWorkspaceError=(...args)=>errors.push(args);
  await vm.runInContext('cancelAllDeploymentUpdates(button)',context);
  assert.deepEqual(calls,['one','two']);assert.equal(errors.length,1);
  assert.match(vm.runInContext("updateCancelError(state.deployments[0],'one')",context),/&lt;Unconfirmed>/);
  assert.equal(context.state.deployments[0].results.one.status,'queued');
});
test('Cancel all rechecks fresh state before each target and suppresses concurrent submissions',async()=>{
  const context=batchFixture(),calls=[];let resolve;
  context.api=async(path,options)=>{
    const {device_id}=JSON.parse(options.body);calls.push(device_id);
    await new Promise(done=>resolve=done);
    const deployment=context.state.deployments[0];
    return {...deployment,results:{...deployment.results,one:{status:'cancelling'},two:{status:'installing'}}};
  };
  const first=vm.runInContext('cancelAllDeploymentUpdates(button)',context);
  assert.match(vm.runInContext('updateCancelAllBadge(state.deployments[0])',context),/disabled/);
  await vm.runInContext('cancelAllDeploymentUpdates(button)',context);
  context.deviceButton={dataset:{deployment:'deploy',device:'two'}};
  await vm.runInContext('cancelDeploymentUpdate(deviceButton)',context);
  resolve();await first;assert.deepEqual(calls,['one']);
});
test('declined confirmation and terminal-only jobs make no cancellation requests',async()=>{
  const context=batchFixture();context.confirm=()=>false;
  context.api=async()=>assert.fail('No mutation should be sent');
  await vm.runInContext('cancelAllDeploymentUpdates(button)',context);
  for(const result of Object.values(context.state.deployments[0].results))result.status='complete';
  assert.equal(vm.runInContext('updateCancelAllBadge(state.deployments[0])',context),'');
  await vm.runInContext('cancelAllDeploymentUpdates(button)',context);
});
test('composed live cards place cancellation badges immediately before statuses, including single-device jobs',()=>{
  const html=execFileSync(process.env.PYTHON||'python3',['-c',"import sys;sys.path.insert(0,'iot_md_management/rootfs/app');import management_portal;print(management_portal.HTML)"],{cwd:new URL('..',import.meta.url),encoding:'utf8'});
  const renderer=html.split('\n').filter(line=>line.startsWith('function deploymentTargets(')||line.startsWith('function activeDeploymentCard(')).join('\n');
  const context=fixture();context.statusBadge=status=>`<span class="badge">${status}</span>`;
  context.activationLabel=()=> 'Install now';context.releaseFlowSummary=()=> '';context.flowFor=()=> '<div class="flow"></div>';
  vm.runInContext(renderer,context);
  for(const multiple of [false,true]){
    const deployment=context.state.deployments[0];deployment.status='active';
    if(multiple){deployment.targets.push('two');deployment.results.two={status:'failed'};}
    const dom=new JSDOM(vm.runInContext('activeDeploymentCard(state.deployments[0])',context));
    const overall=dom.window.document.querySelector('.title-row .deployment-status-actions');
    assert.equal(overall.firstElementChild.textContent,'Cancel all');
    assert.equal(overall.lastElementChild.textContent,'active');
    const individual=dom.window.document.querySelector('.target-result .deployment-status-actions');
    assert.equal(individual.firstElementChild.textContent,'Cancel');
    assert.equal(individual.lastElementChild.textContent,'staging');
    assert.equal(dom.window.document.querySelectorAll('button.cancel-badge').length,2);
    assert.equal(dom.window.document.querySelector('.deployment > .actions'),null);
    dom.window.close();
  }
  const history=html.split('\n').find(line=>line.startsWith('function deploymentHistoryEntry('));
  if(history)assert.doesNotMatch(history,/updateCancelDeviceBadge|updateCancelAllBadge/);
});
