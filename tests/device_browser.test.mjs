import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/management_portal.py',import.meta.url),'utf8');
const filter=source.split('\n').find(line=>line.startsWith('function filteredDevices('));
function filtered(search='',status='all',cohort='all'){
  const context=vm.createContext({deviceView:{search,status,cohort},state:{devices:[
    {id:'001',name:'Boiler',description:'Ground floor heating',host:'iot-md-001.local',cohort:'main',enabled:true,last_error:''},
    {id:'002',name:'HTW',description:'Hot water',host:'iot-md-002.local',cohort:'canary',enabled:true,last_error:'TLS timeout'},
    {id:'003',name:'Spare',host:'iot-md-003.local',cohort:'main',enabled:false,last_error:''}
  ]}});
  vm.runInContext(filter,context);
  return Array.from(vm.runInContext('filteredDevices().map(device=>device.id)',context));
}
test('device search matches all words across identity and description, case insensitive',()=>{
  assert.deepEqual(filtered(' GROUND  Boiler '),['001']);
  assert.deepEqual(filtered('IOT-MD-002.LOCAL'),['002']);
  assert.deepEqual(filtered('003'),['003']);
  assert.deepEqual(filtered('missing'),[]);
});
test('device search combines status and cohort filters without treating disabled as healthy',()=>{
  assert.deepEqual(filtered('','healthy'),['001']);
  assert.deepEqual(filtered('','unavailable','canary'),['002']);
  assert.deepEqual(filtered('','disabled'),['003']);
  assert.deepEqual(filtered('','healthy','canary'),[]);
});

const functions=names=>source.split('\n').filter(line=>names.some(name=>line.startsWith(`function ${name}(`)||line.startsWith(`async function ${name}(`))).join('\n');
test('successful save clears only submitted device values, not edits made during the request or backup settings',()=>{
  const make=(name,value,form='updateDevice(event,\'001\')')=>({name,value,form,checked:true,dataset:{deviceDirty:'true'}});
  const controls=[make('description','Newer unsaved'),make('name','Saved name'),make('port','8444')];
  const drafts=[...controls,make('retention','14','updateDeviceBackup(event,\'001\')')];
  const deviceView={drafts:new Map([['001',drafts]])};
  const card={dataset:{deviceDetail:'001'},querySelector:()=>({querySelectorAll:()=>controls})};
  const context=vm.createContext({deviceView,document:{querySelector:()=>card}});
  vm.runInContext(functions(['clearSavedDeviceDraft']),context);
  vm.runInContext("clearSavedDeviceDraft('001',{description:'Submitted',name:'Saved name',port:8444})",context);
  assert.equal(controls[0].dataset.deviceDirty,'true');
  assert.equal(controls[1].dataset.deviceDirty,undefined);
  assert.equal(controls[2].dataset.deviceDirty,undefined);
  assert.deepEqual(Array.from(deviceView.drafts.get('001'),item=>item.name),['description','retention']);
});
test('successful save for a different selected device does not clear that device edits',()=>{
  const dirty={name:'description',value:'002 draft',dataset:{deviceDirty:'true'}};
  const card={dataset:{deviceDetail:'002'},querySelector:()=>{throw new Error('Wrong device accessed')}};
  const deviceView={drafts:new Map([['001',[{name:'description',value:'Saved',form:'updateDevice(event)'}]],['002',[dirty]]])};
  const context=vm.createContext({deviceView,document:{querySelector:()=>card}});
  vm.runInContext(functions(['clearSavedDeviceDraft']),context);
  vm.runInContext("clearSavedDeviceDraft('001',{description:'Saved'})",context);
  assert.equal(deviceView.drafts.get('001').length,0);
  assert.equal(deviceView.drafts.get('002')[0],dirty);
  assert.equal(dirty.dataset.deviceDirty,'true');
});
test('description is sent only when edited; failed writes retain drafts and release save state',async()=>{
  const deviceView={saving:new Set(),errors:new Map(),drafts:new Map([['001',['unsaved']]])};
  let dirty=false,calls=[],renders=0;
  const form={elements:{enabled:{checked:true},description:{hasAttribute:()=>dirty}}};
  const context=vm.createContext({deviceView,FormData:class{*[Symbol.iterator](){yield ['port','8444'];yield ['description','Old description'];yield ['name','Boiler']}},renderDevices:()=>renders++,api:async(path,options)=>{calls.push(JSON.parse(options.body));throw new Error('API-write permission required')},document:{querySelector:()=>null}});
  vm.runInContext(functions(['updateDevice','clearSavedDeviceDraft']),context);
  context.event={preventDefault(){},target:form};
  await vm.runInContext("updateDevice(event,'001')",context);
  assert.equal('description' in calls[0],false);
  assert.equal(deviceView.saving.size,0);
  assert.equal(deviceView.errors.get('001'),'API-write permission required');
  assert.deepEqual(deviceView.drafts.get('001'),['unsaved']);
  dirty=true;
  await vm.runInContext("updateDevice(event,'001')",context);
  assert.equal(calls[1].description,'Old description');
  assert.equal(renders,4);
});
