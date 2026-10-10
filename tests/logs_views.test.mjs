import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/logs_views.js',import.meta.url),'utf8').split('initLogsViews();')[0];
function context(extra={}){const context=vm.createContext({URLSearchParams,location:{search:''},esc:value=>String(value).replaceAll('<','&lt;').replaceAll('"','&quot;'),when:String,...extra});vm.runInContext(source,context);return context}
test('log dots use outcome colours and accessible hover/focus descriptions',()=>{
  const ctx=context();
  for(const [status,tone] of [['complete','good'],['failed','bad'],['interrupted','bad'],['staged','warn'],['cancelled','warn'],['observed','neutral'],['Recovery prepared','good']]){
    const html=vm.runInContext(`logStatusDot('${status}')`,ctx);
    assert.match(html,new RegExp('log-dot '+tone));assert.match(html,/tabindex="0"/);assert.match(html,/aria-label=/);assert.match(html,/title=/);
  }
  assert.match(vm.runInContext('logStatusDot("failed", "<script>")',ctx),/&lt;script>/);
});
test('mixed intentional cancellations are amber, while genuine failures remain red with outcome counts',()=>{
  const ctx=context();ctx.deployment={status:'partial',targets:['one','two','three'],results:{one:{status:'complete'},two:{status:'cancelled'},three:{status:'cancelled'}}};
  let html=vm.runInContext('deploymentOutcomeDot(deployment)',ctx);
  assert.match(html,/log-dot warn/);assert.match(html,/partly cancelled — 1 complete, 2 cancelled/);
  ctx.deployment.results.one.status='failed';
  html=vm.runInContext('deploymentOutcomeDot(deployment)',ctx);
  assert.match(html,/log-dot bad/);assert.match(html,/1 failed, 2 cancelled/);
  ctx.deployment.results.one.status='cancelled';ctx.deployment.status='cancelled';
  assert.match(vm.runInContext('deploymentOutcomeDot(deployment)',ctx),/cancelled — 3 cancelled/);
});
test('logs default to activity, audit is a distinct explicit view',()=>{
  const ctx=context();assert.equal(vm.runInContext('logView()',ctx),'activity');ctx.location.search='?view=audit';assert.equal(vm.runInContext('logView()',ctx),'audit');
});
test('activity search filters deployments, backups and USB outcomes without losing disclosure state',()=>{
  const controls={'action-history':{},'action-history-filter':{value:'all'},'action-history-search':{value:'BOILER'}};
  const state={devices:[{id:'one.local',name:'Boiler'}],deployments:[{id:'d1',created_at:3,targets:['one.local'],results:{},status:'complete',update:{version:'alpha.102'}}],backups:[{id:1,created_at:2,device_name:'Boiler'}],seedJobs:[{id:'s1',created_at:1,status:'failed',image:'factory.bin',detail:'Port closed'}]};
  const ctx=context({state,backupState:{},document:{getElementById:id=>controls[id]},matchesCatalogSearch:(values,query)=>values.join(' ').toLowerCase().includes(query.toLowerCase()),deploymentHistoryItem:()=>'<deployment>',backupHistoryItem:()=>'<backup>',seedHistoryItem:()=>'<usb>',replacePreservingDetails:(box,html)=>box.innerHTML=html,revealRequestedBackup:()=>{}});
  vm.runInContext('renderActionHistory()',ctx);assert.equal(controls['action-history'].innerHTML,'<deployment><backup>');
  controls['action-history-search'].value='Port closed';vm.runInContext('renderActionHistory()',ctx);assert.equal(controls['action-history'].innerHTML,'<usb>');
});
