import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/fleet_views.js',import.meta.url),'utf8').split('initFleetViews();')[0].replace('__HEALTH_STALE_AFTER__','180');
const now=1000;
const healthy={enabled:true,last_seen:990,last_error:'',inventory:{device:{qualification_observation:{health_state:'healthy'},runtime:{lifecycle:{device_state:'running'},state:{network:'online',mqtt:'disabled'},tasks:{}}}}};
function evaluate(code,extra={}){
  const context=vm.createContext({device:structuredClone(healthy),now,when:value=>String(value),esc:String,deviceRetries:new Map(),...extra});
  vm.runInContext(source,context);
  return vm.runInContext(code,context);
}
test('API and actual device health are independent',()=>{
  assert.equal(evaluate('apiConnectionStatus(device,now).tone'),'good');
  assert.equal(evaluate('deviceHealthStatus(device,now).tone'),'good');
  const device=structuredClone(healthy);device.inventory.device.qualification_observation.health_state='failed';
  assert.equal(evaluate('apiConnectionStatus(device,now).tone',{device}),'good');
  assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'bad');
});
test('baseline secrets are opt-in and include every supported password and certificate widget',()=>{
  const entries=[{dataset:{settingName:'mqtt_password',profileKind:'secret'}},{dataset:{settingName:'wifi_password',profileKind:'secret'}},{dataset:{settingName:'certificate_portal_key',profileKind:'file'}},{dataset:{settingName:'wifi_ssid',profileKind:'text'}}];
  assert.equal(evaluate('baselineProfileFields(entries).includes("mqtt_password")',{entries}),false);
  for(const name of ['mqtt_password','wifi_password','certificate_portal_key'])assert.equal(evaluate(`baselineProfileFields(entries,true).includes('${name}')`,{entries}),true);
  assert.equal(evaluate('baselineProfileFields(entries,true).includes("wifi_ssid")',{entries}),true);
  entries.push({dataset:{settingName:'mqtt_username',profileKind:'text'}},{dataset:{settingName:'device_name',profileKind:'text'}});
  assert.equal(evaluate('baselineProfileFields(entries).includes("mqtt_username")',{entries}),true);
  assert.equal(evaluate('baselineProfileFields(entries,true).includes("device_name")',{entries}),false);
});
test('blank baseline secrets are omitted, explicit secrets still require a value',()=>{
  const entry={dataset:{baselineOptional:'true',profileKind:'secret'}};
  assert.equal(evaluate('omitEmptyBaselineSecret(entry,control)',{entry,control:{value:''}}),true);
  assert.equal(evaluate('omitEmptyBaselineSecret(entry,control)',{entry,control:{value:'secret'}}),false);
  entry.dataset.baselineOptional='false';
  assert.equal(evaluate('omitEmptyBaselineSecret(entry,control)',{entry,control:{value:''}}),false);
  entry.dataset={baselineOptional:'true',profileKind:'file'};
  assert.equal(evaluate('omitEmptyBaselineSecret(entry,control)',{entry,control:{files:[]}}),true);
  assert.equal(evaluate('omitEmptyBaselineSecret(entry,control)',{entry,control:{files:[{}]}}),false);
});
test('unavailable, disabled, unpolled and stale reports cannot show healthy device LED',()=>{
  for(const changes of [{last_error:'TLS timeout'},{enabled:false},{last_seen:0},{last_seen:500}]){
    const device={...structuredClone(healthy),...changes};
    assert.notEqual(evaluate('apiConnectionStatus(device,now).tone',{device}),'good');
    assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'unknown');
  }
});
test('critical task failures are red and recoverable service failures amber',()=>{
  const device=structuredClone(healthy);
  device.inventory.device.runtime.tasks.network={status:'failed',critical:true,error:'Listener stopped'};
  assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'bad');
  device.inventory.device.runtime.tasks.network={status:'degraded',critical:false,error:'Connecting'};
  assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'warn');
  device.inventory.device.runtime.tasks={};device.inventory.device.runtime.state.mqtt='degraded';
  assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'warn');
});
test('historical error counters do not falsely mark recovered devices unhealthy',()=>{
  const device=structuredClone(healthy);
  device.health={health:{counters:{watchdog_resets:10,api_failures:15},observations:{last_startup_exception:'Old failure'}}};
  assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'good');
  device.inventory.device={};
  assert.equal(evaluate('deviceHealthStatus(device,now).tone',{device}),'unknown');
});
test('fleet menu health combines API and runtime reports, excluding disabled devices',()=>{
  const failed={...structuredClone(healthy),last_error:'TLS timeout'},degraded=structuredClone(healthy),unknown={...structuredClone(healthy),last_seen:0};
  degraded.inventory.device.runtime.state.mqtt='degraded';
  assert.equal(evaluate('fleetHealthStatus([],now).tone'),'unknown');
  assert.equal(evaluate('fleetHealthStatus(devices,now).tone',{devices:[healthy,{...failed,enabled:false}]}),'good');
  assert.equal(evaluate('fleetHealthStatus(devices,now).tone',{devices:[healthy,degraded]}),'warn');
  assert.match(evaluate('fleetHealthStatus(devices,now).label',{devices:[healthy,unknown]}),/1 healthy, 1 unknown/);
  assert.equal(evaluate('fleetHealthStatus(devices,now).tone',{devices:[healthy,degraded,failed]}),'bad');
  const runtimeFailure=structuredClone(healthy);runtimeFailure.inventory.device.qualification_observation.health_state='failed';
  assert.equal(evaluate('fleetHealthStatus(devices,now).tone',{devices:[runtimeFailure]}),'bad');
});
test('refresh is independent of API LEDs and remains available on healthy devices',()=>{
  assert.ok(!evaluate('deviceConnectionBadges(device)').includes('device-retry'));
  assert.match(evaluate('deviceRefreshButton(device)'),/Refresh connection and device health/);
  assert.equal(evaluate('deviceRefreshButton(device)',{device:{...healthy,enabled:false}}),'');
});
test('release search combines promoted/all filter with case-insensitive multiple words',()=>{
  const state={releases:[{version:'3.0.0-alpha.101',channels:['alpha'],release_sequence:2806},{version:'3.0.0-beta.1',channels:[],release_sequence:2807}],profiles:[]};
  assert.equal(evaluate("releaseSearch='ALPHA 2806';filteredReleases().length",{state,releaseFilter:'promoted'}),1);
  assert.equal(evaluate("releaseSearch='BETA';filteredReleases().length",{state,releaseFilter:'promoted'}),0);
  assert.equal(evaluate("releaseSearch='BETA';filteredReleases().length",{state,releaseFilter:'all'}),1);
});
test('profile search includes setting names but never secret values',()=>{
  const state={profiles:[{name:'Production',description:'Boiler',settings:{syslog_enabled:true},secrets:{mqtt_password:'do-not-search-me'}}]};
  assert.equal(evaluate("profileSearch='production syslog';filteredProfiles().length",{state}),1);
  assert.equal(evaluate("profileSearch='mqtt_password';filteredProfiles().length",{state}),1);
  assert.equal(evaluate("profileSearch='do-not-search-me';filteredProfiles().length",{state}),0);
});
test('profile table masks secrets defensively and retains disclosure keys',()=>{
  const box={innerHTML:''},count={};
  const document={getElementById:id=>id==='profiles'?box:count};
  const state={profiles:[{name:'Production',settings:{syslog_enabled:true},secrets:{mqtt_password:'must-not-leak'}}]};
  evaluate('renderProfiles()',{state,document,profileFieldLabel:String,replacePreservingDetails:(el,html)=>el.innerHTML=html});
  assert.match(box.innerHTML,/catalog-table profile-table/);
  assert.match(box.innerHTML,/data-disclosure-key="profile-Production"/);
  assert.match(box.innerHTML,/>\*{8}</);
  assert.ok(!box.innerHTML.includes('must-not-leak'));
});
