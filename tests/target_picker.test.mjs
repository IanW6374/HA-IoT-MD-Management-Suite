import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import vm from 'node:vm';
const require = createRequire(import.meta.url);
const {JSDOM} = require(process.env.IOT_UI_NODE_MODULES ? `${process.env.IOT_UI_NODE_MODULES}/jsdom` : 'jsdom');
const asset = name => fs.readFileSync(new URL(`../iot_md_management/rootfs/app/assets/${name}`, import.meta.url), 'utf8');
test('composed portal scripts parse without conflicting component declarations',()=>{
  const cwd=new URL('..',import.meta.url);
  const html=execFileSync(process.env.PYTHON || 'python3',['-c',"import sys;sys.path.insert(0,'iot_md_management/rootfs/app');import management_portal;print(management_portal.HTML)"],{cwd,encoding:'utf8'});
  const dom=new JSDOM(html);
  for(const script of dom.window.document.querySelectorAll('script:not([src])'))new vm.Script(script.textContent);
  assert.ok(html.includes('form_controls.css') || html.includes('Portfolio form rhythm'));
  dom.window.close();
});
function fixture() {
  const dom = new JSDOM('<main><form id="deploy"><div id="device-targets"></div><div id="cohort-targets"></div></form><form id="backup"><div id="backup-device-targets"></div><div id="backup-cohort-targets"></div></form></main>', {runScripts:'outside-only', pretendToBeVisual:true});
  dom.window.eval(`const state={devices:[{id:'one.local',name:'One',cohort:'Heating'},{id:'two.local',name:'Two',cohort:'Heating'},{id:'three.local',name:'Three',cohort:'Lighting'}]};
    function renderTargets(){const box=document.getElementById('device-targets'),selected=new Set([...box.querySelectorAll('input:checked')].map(input=>input.value));box.innerHTML=state.devices.map(device=>'<label class="check"><input type="checkbox" name="target_device" value="'+device.id+'" '+(selected.has(device.id)?'checked':'')+'><span>'+device.name+'</span></label>').join('');}
    function renderBackupTargets(){document.getElementById('backup-device-targets').innerHTML='<label><input type="checkbox" name="backup_target_device" value="one.local"><span>One</span></label>';}
    async function refreshBackups(){}
  ` + asset('target_picker.js'));
  dom.window.eval('renderTargets();renderBackupTargets();');
  return dom;
}
test('group and select-all controls have native all/some/none states without polluting payloads', () => {
  const dom=fixture(),doc=dom.window.document,box=doc.getElementById('device-targets');
  const group=box.querySelector('.profile-group-choice input'),all=box.querySelector('.profile-selection-tools input');
  const one=box.querySelector('[value="one.local"]'); let changes=0;
  one.addEventListener('change',()=>changes++); one.click();
  assert.equal(group.indeterminate,true); assert.equal(all.indeterminate,true);
  assert.equal(changes,1); group.click(); assert.equal(group.checked,true); assert.equal(group.indeterminate,false);
  assert.deepEqual([...new dom.window.FormData(doc.getElementById('deploy'))], [['target_device','one.local'],['target_device','two.local']]);
  all.click(); assert.equal(box.querySelectorAll('input[name]:checked').length,3);
  all.click(); assert.equal(group.checked,false); assert.equal(group.indeterminate,false);
  dom.window.close();
});
test('poll refresh preserves selected devices, query, expanded groups, open dropdown and focus', async () => {
  const dom=fixture(),doc=dom.window.document,box=doc.getElementById('device-targets');
  box.querySelector('[value="one.local"]').click();
  box.querySelector('details').open=true;
  await new Promise(resolve=>dom.window.setTimeout(resolve,0));
  box.querySelector('.profile-group-expand').click();
  let search=box.querySelector('input[type=search]');search.value='one.local';search.dispatchEvent(new dom.window.Event('input'));search.focus();
  dom.window.eval('renderTargets()');search=box.querySelector('input[type=search]');
  assert.equal(search.value,'one.local');assert.equal(doc.activeElement,search);assert.equal(box.querySelector('details').open,true);
  assert.equal(box.querySelector('[value="one.local"]').checked,true);assert.equal(box.querySelector('[value="two.local"]').closest('label').hidden,true);
  search.value='';search.dispatchEvent(new dom.window.Event('input'));assert.equal(box.querySelector('.profile-group-settings').hidden,false);
  search.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert.equal(box.querySelector('details').open,false);assert.equal(doc.activeElement,box.querySelector('summary'));
  dom.window.close();
});
test('disabled members cannot be selected, searching does not lose hidden selections, backup is independent', () => {
  const dom=fixture(),doc=dom.window.document,box=doc.getElementById('device-targets');
  box.querySelector('[value="two.local"]').disabled=true;
  box.querySelector('.profile-selection-tools input').click();
  assert.equal(box.querySelector('[value="two.local"]').checked,false);
  assert.equal(doc.querySelector('[name=backup_target_device]').checked,false);
  const search=box.querySelector('input[type=search]');search.value='no match';search.dispatchEvent(new dom.window.Event('input'));
  assert.equal(box.querySelectorAll('input[name]:checked').length,2);
  assert.equal([...box.querySelectorAll('p')].find(p=>p.textContent==='No matching options.').hidden,false);
  box.querySelector('.profile-selection-chip button').click();assert.equal(box.querySelector('[value="one.local"]').checked,false);
  dom.window.close();
});
test('required-field annotation keeps the checkbox before text and marks only required controls', async () => {
  const dom = new JSDOM('<label>Approval<input type="checkbox" required></label><label><input type="checkbox"><span>Optional</span></label><label>Port<input required></label>',{runScripts:'outside-only',pretendToBeVisual:true});
  dom.window.eval(asset('form_requirements.js'));
  const labels=[...dom.window.document.querySelectorAll('label')];
  assert.equal(labels[0].firstElementChild.tagName,'INPUT');assert.equal(labels[1].firstElementChild.tagName,'INPUT');
  assert.equal(labels[0].querySelector('.field-requirement').title,'Required');assert.equal(labels[1].querySelector('.field-requirement'),null);
  dom.window.eval(asset('form_requirements.js'));assert.equal(labels[0].querySelectorAll('.field-requirement').length,1);
  dom.window.close();
});
