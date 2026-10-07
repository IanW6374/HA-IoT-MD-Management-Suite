import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/profile_designer.js',import.meta.url),'utf8');
function status(names,values){const context=vm.createContext({names,selected:new Set(values)});vm.runInContext(source,context);return JSON.parse(vm.runInContext('JSON.stringify(profileSelectionState(names,selected))',context))}
test('group selection distinguishes all, some and none',()=>{
  assert.deepEqual(status(['a','b'],[]),{count:0,total:2,checked:false,indeterminate:false});
  assert.deepEqual(status(['a','b'],['a']),{count:1,total:2,checked:false,indeterminate:true});
  assert.deepEqual(status(['a','b'],['a','b']),{count:2,total:2,checked:true,indeterminate:false});
});
test('selection counts ignore unrelated groups and empty groups never show all',()=>{
  assert.deepEqual(status(['a','b'],['c']),{count:0,total:2,checked:false,indeterminate:false});
  assert.deepEqual(status([],['c']),{count:0,total:0,checked:false,indeterminate:false});
});
test('native mixed state and keyboard-close controls are present',()=>{
  assert.ok(source.includes('control.indeterminate=state.indeterminate'));
  assert.ok(source.includes("event.key==='Escape'"));
  assert.ok(source.includes('summary.focus()'));
  assert.ok(source.includes("picker.querySelector('#profile-reset-baseline').click()"));
  assert.ok(!source.includes('profile-section-select'));
});
