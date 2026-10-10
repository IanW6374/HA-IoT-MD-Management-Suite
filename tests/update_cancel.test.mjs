import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/update_cancel.js',import.meta.url),'utf8');
function fixture(status='staging'){
  const deployment={id:'deploy',update:{release_sequence:123},targets:['one'],results:{one:{status}}};
  const context=vm.createContext({state:{deployments:[deployment],devices:[{id:'one',host:'one.local'}]},esc:value=>String(value).replaceAll('<','&lt;'),confirm:()=>true,renderDeployments(){},refreshDeploymentProgress(){}});
  vm.runInContext(source,context);return context;
}
test('queued and staged updates can cancel; installation and cancelling states disable controls',()=>{
  for(const status of ['queued','staging','staged','scheduled']){
    assert.doesNotMatch(vm.runInContext('updateCancelControls(state.deployments[0])',fixture(status)),/ disabled/);
  }
  for(const status of ['installing','cancelling']){
    assert.match(vm.runInContext('updateCancelControls(state.deployments[0])',fixture(status)),/ disabled/);
  }
  assert.equal(vm.runInContext('updateCancelControls(state.deployments[0])',fixture('cancelled')),'<div class="actions"></div>');
});
test('confirmation is required and a failed cancellation remains visible without automatic mutation replay',async()=>{
  const context=fixture();let calls=0;
  context.api=async(path,options)=>{calls++;assert.equal(path,'api/deployments/cancel');assert.deepEqual(JSON.parse(options.body),{deployment_id:'deploy',device_id:'one'});throw Error('Cancellation not confirmed')};
  context.button={dataset:{deployment:'deploy',device:'one'}};context.confirm=()=>false;
  await vm.runInContext('cancelDeploymentUpdate(button)',context);assert.equal(calls,0);
  context.confirm=()=>true;
  await vm.runInContext('cancelDeploymentUpdate(button)',context);assert.equal(calls,1);
  assert.match(vm.runInContext('updateCancelControls(state.deployments[0])',context),/role="alert">Cancellation not confirmed/);
  assert.equal(context.state.deployments[0].results.one.status,'staging');
});
test('accepted cancellation reflects acknowledgement, not an invented successful completion',async()=>{
  const context=fixture();context.button={dataset:{deployment:'deploy',device:'one'}};
  context.api=async()=>({...context.state.deployments[0],results:{one:{status:'cancelling'}}});
  await vm.runInContext('cancelDeploymentUpdate(button)',context);
  assert.equal(context.state.deployments[0].results.one.status,'cancelling');
  assert.match(vm.runInContext('updateCancelControls(state.deployments[0])',context),/Cancelling…/);
});
