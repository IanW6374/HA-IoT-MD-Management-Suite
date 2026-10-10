import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import vm from 'node:vm';
const require=createRequire(import.meta.url);
const {JSDOM}=require(process.env.IOT_UI_NODE_MODULES ? `${process.env.IOT_UI_NODE_MODULES}/jsdom` : 'jsdom');
const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/management_portal.py',import.meta.url),'utf8');
const status=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/device_status.js',import.meta.url),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));

test('device status updates even when an unrelated workspace request never resolves',async()=>{
  const html=execFileSync(process.env.PYTHON||'python3',['-c',"import sys;sys.path.insert(0,'iot_md_management/rootfs/app');import management_portal;print(management_portal.HTML.replace('__PAGE__','devices').replace('__HEALTH_STALE_AFTER__','180'))"],{cwd:new URL('..',import.meta.url),encoding:'utf8'});
  const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'https://management.local/devices?view=list'});
  const w=dom.window,intervals=[];let generation=0;
  w.matchMedia=()=>({matches:false});
  w.HTMLElement.prototype.scrollIntoView=()=>{};
  w.setInterval=(callback,delay)=>{intervals.push({callback,delay});return intervals.length;};
  w.fetch=async path=>{
    if(path==='api/releases')return new Promise(()=>{});
    return {ok:true,text:async()=>JSON.stringify({devices:[{id:'one.local',host:'one.local',name:'One',enabled:true,cohort:'default',last_seen:generation?Math.floor(Date.now()/1000):1,inventory:{device:{runtime:{lifecycle:{device_state:'running'}}}}}],groups:[],inventory:{releases:[]},profiles:[],deployments:[],backups:[],audit:[],events:[],active_jobs:[],count:0,items:[]})};
  };
  try{
    for(const script of w.document.querySelectorAll('script:not([src])'))w.eval(script.textContent);
    await tick();await tick();
    const led=()=>w.document.querySelector('.device-table .device-health-led');
    assert.match(led().title,/stale/i);
    w.eval("selectDevice('one.local')");
    const settings=w.document.querySelector('[data-device-detail] details');settings.open=true;
    const description=w.document.querySelector('[data-device-detail] input[name=description]');
    description.value='Unsaved description';description.dispatchEvent(new w.Event('input',{bubbles:true}));description.focus();
    generation=1;
    intervals.find(item=>item.delay===10000).callback();
    await tick();await tick();
    assert.match(led().title,/API connected/i);
    assert.equal(w.document.querySelector('[data-device-detail] details').open,true);
    assert.equal(w.document.querySelector('[data-device-detail] input[name=description]').value,'Unsaved description');
    assert.equal(w.document.activeElement.name,'description');
  }finally{w.close();}
});

test('overlapping refreshes coalesce; failures remain visible and allow the next refresh',async()=>{
  let resolve,calls=0,renders=0,errors=[],cleared=[];
  const context=vm.createContext({state:{devices:[]},api:()=>{calls++;return new Promise(done=>resolve=done)},renderDevices:()=>renders++,renderMetrics:()=>{},showWorkspaceError:(...args)=>errors.push(args),clearWorkspaceError:message=>cleared.push(message),document:{hidden:false,addEventListener(){}},window:{addEventListener(){}}});
  vm.runInContext(status,context);
  const first=vm.runInContext('refreshDeviceStatus()',context),second=vm.runInContext('refreshDeviceStatus()',context);
  assert.equal(calls,1);resolve({devices:[{id:'one'}]});await Promise.all([first,second]);
  assert.equal(renders,1);assert.equal(cleared.length,1);
  context.api=async()=>{throw Error('Offline')};
  await assert.rejects(vm.runInContext('refreshDeviceStatus()',context),/Offline/);
  assert.equal(errors[0][0],'Device status could not be refreshed');
  assert.equal(context.state.devices[0].id,'one');
  context.api=async()=>({devices:[{id:'two'}]});
  await vm.runInContext('refreshDeviceStatus()',context);
  assert.equal(context.state.devices[0].id,'two');
});

test('returning to a visible tab refreshes immediately without making background requests',async()=>{
  const listeners={};let calls=0;
  const context=vm.createContext({state:{devices:[]},api:async()=>{calls++;return {devices:[]}},renderDevices(){},renderMetrics(){},showWorkspaceError(){},clearWorkspaceError(){},document:{hidden:true,addEventListener:(name,fn)=>listeners[name]=fn},window:{addEventListener:(name,fn)=>listeners[name]=fn}});
  vm.runInContext(status,context);
  listeners.visibilitychange();assert.equal(calls,0);
  context.document.hidden=false;listeners.visibilitychange();await tick();
  listeners.focus();await tick();listeners.pageshow();await tick();assert.equal(calls,3);
});

test('GETs are uncached and bounded; mutations are not aborted or replayed',async()=>{
  const line=source.split('\n').find(line=>line.startsWith('async function api('));
  let timer,cleared=0,requests=[];
  const context=vm.createContext({AbortController,setTimeout:(fn,delay)=>{assert.equal(delay,20000);timer=fn;return 1},clearTimeout:()=>cleared++,fetch:async(path,options)=>{requests.push(options);return {ok:true,text:async()=>'{"ok":true}'}}});
  vm.runInContext(line,context);
  await vm.runInContext("api('api/devices')",context);
  assert.equal(requests[0].cache,'no-store');assert.equal(cleared,1);
  context.fetch=(path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('Aborted'))));
  const request=vm.runInContext("api('api/devices')",context);timer();
  await assert.rejects(request,/timed out; retrying on the next refresh/);
  context.fetch=async(path,options)=>{requests.push(options);return {ok:true,text:async()=>'{"ok":true}'}};
  await vm.runInContext("api('api/poll',{method:'POST',body:'{}'})",context);
  assert.equal(requests[1].signal,undefined);assert.equal(requests[1].cache,undefined);
});
