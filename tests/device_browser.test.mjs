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
