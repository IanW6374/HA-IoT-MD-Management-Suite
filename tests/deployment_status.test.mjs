import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url);
const {JSDOM}=require(process.env.IOT_UI_NODE_MODULES ? `${process.env.IOT_UI_NODE_MODULES}/jsdom` : 'jsdom');
const source=fs.readFileSync(new URL('../iot_md_management/rootfs/app/assets/deployment_status.js',import.meta.url),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const job=(revision,status)=>({id:'job',revision,status,targets:['one'],results:{one:{status}}});
function fixture(api,initial=[]) {
  const errors=[],cleared=[],listeners={};let renders=0;
  const context=vm.createContext({state:{deployments:initial},api,
    renderMetrics(){},renderDeployments(){renders++},renderActionHistory(){},reconcileUpdateCancellations(){},
    showWorkspaceError:(...args)=>errors.push(args),clearWorkspaceError:message=>cleared.push(message),
    document:{hidden:false,getElementById:()=>null,addEventListener:(name,fn)=>listeners[name]=fn},
    window:{addEventListener:(name,fn)=>listeners[name]=fn}});
  vm.runInContext(source,context);
  return {context,errors,cleared,listeners,renders:()=>renders};
}

test('a read started before cancellation cannot rewind its acknowledged result',async()=>{
  let resolve,calls=0;
  const f=fixture(()=>{calls++;return calls===1?new Promise(done=>resolve=done):Promise.resolve({deployments:[job(3,'cancelled')]})},[job(1,'queued')]);
  const reading=vm.runInContext('refreshDeploymentProgress()',f.context);
  f.context.ack=job(2,'cancelling');
  vm.runInContext('deploymentMutationEpoch++;applyDeploymentSnapshot(ack)',f.context);
  resolve({deployments:[job(1,'queued')]});await reading;
  assert.equal(calls,2);assert.equal(f.context.state.deployments[0].results.one.status,'cancelled');
  assert.equal(f.renders(),1);
});

test('revisions protect multiple target updates in the same second',async()=>{
  const f=fixture(async()=>({deployments:[job(2,'cancelling')]}),[job(3,'cancelled')]);
  await vm.runInContext('refreshDeploymentProgress()',f.context);
  f.context.old=job(1,'queued');vm.runInContext('applyDeploymentSnapshot(old)',f.context);
  assert.equal(f.context.state.deployments[0].revision,3);
  assert.equal(f.context.state.deployments[0].status,'cancelled');
});

test('reads coalesce; failed or malformed reads retain results and recover',async()=>{
  let resolve,calls=0;
  const f=fixture(()=>{calls++;return new Promise(done=>resolve=done)},[job(1,'queued')]);
  const a=vm.runInContext('refreshDeploymentProgress()',f.context),b=vm.runInContext('refreshDeploymentProgress()',f.context);
  assert.equal(calls,1);resolve({deployments:[job(2,'cancelling')]});await Promise.all([a,b]);
  f.context.api=async()=>({});await assert.rejects(vm.runInContext('refreshDeploymentProgress()',f.context),/Invalid deployment/);
  assert.equal(f.context.state.deployments[0].status,'cancelling');
  assert.equal(f.errors[0][0],'Update status could not be refreshed');
  f.context.api=async()=>({deployments:[job(3,'cancelled')]});
  await vm.runInContext('refreshDeploymentProgress()',f.context);
  assert.equal(f.context.state.deployments[0].status,'cancelled');assert.equal(f.cleared.length,2);
});

test('visibility and focus refresh immediately, but hidden tabs make no reads',async()=>{
  let calls=0;const f=fixture(async()=>{calls++;return {deployments:[]}});
  f.context.document.hidden=true;f.listeners.visibilitychange();assert.equal(calls,0);
  f.context.document.hidden=false;f.listeners.visibilitychange();await tick();
  f.listeners.focus();await tick();f.listeners.pageshow();await tick();assert.equal(calls,3);
});

test('the composed In-Flight page updates independently of a stalled catalog request',async()=>{
  const html=execFileSync(process.env.PYTHON||'python3',['-c',"import sys;sys.path.insert(0,'iot_md_management/rootfs/app');import management_portal;print(management_portal.HTML.replace('__PAGE__','actions').replace('__HEALTH_STALE_AFTER__','180'))"],{cwd:new URL('..',import.meta.url),encoding:'utf8'});
  const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'https://management.local/actions?view=inflight'});
  const w=dom.window,intervals=[];let cancelled=false;
  w.matchMedia=()=>({matches:false});w.HTMLElement.prototype.scrollIntoView=()=>{};
  w.setInterval=(callback,delay)=>{intervals.push({callback,delay});return intervals.length};
  w.fetch=async path=>{
    if(path==='api/releases')return new Promise(()=>{});
    const status=cancelled?'cancelled':'queued';
    return {ok:true,text:async()=>JSON.stringify({devices:[],groups:[],inventory:{releases:[]},profiles:[],deployments:[{...job(cancelled?2:1,status),created_at:1,activation:'now',update:{version:'3.0.0-alpha.113',release_type:'universal'},results:{one:{status,detail:cancelled?'Cancelled before dispatch':'Waiting to start'}}}],backups:[],audit:[],events:[],active_jobs:[],count:0,items:[]})};
  };
  try{
    for(const script of w.document.querySelectorAll('script:not([src])'))w.eval(script.textContent);
    await tick();await tick();
    assert.match(w.document.getElementById('deployment-active').textContent,/Waiting to start/);
    cancelled=true;
    const progressTimer=intervals.find(item=>item.delay===3000&&String(item.callback).includes('refreshVisibleDeploymentStatus'));
    assert.ok(progressTimer);progressTimer.callback();await tick();await tick();
    assert.doesNotMatch(w.document.getElementById('deployment-active').textContent,/Waiting to start/);
    assert.match(w.document.getElementById('action-history').textContent,/Cancelled before dispatch/);
  }finally{w.close()}
});

test('Cancel all updates the live page and history without reload or a catalog response',async()=>{
  const html=execFileSync(process.env.PYTHON||'python3',['-c',"import sys;sys.path.insert(0,'iot_md_management/rootfs/app');import management_portal;print(management_portal.HTML.replace('__PAGE__','actions').replace('__HEALTH_STALE_AFTER__','180'))"],{cwd:new URL('..',import.meta.url),encoding:'utf8'});
  const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'https://management.local/actions?view=inflight'});
  const w=dom.window,posts=[];let releaseLast;
  const deployment={id:'batch',revision:1,status:'active',targets:['one','two','three'],created_at:1,activation:'now',update:{version:'3.0.0-alpha.113',release_type:'universal'},results:{one:{status:'complete',detail:'Installed'},two:{status:'queued',detail:'Waiting to start'},three:{status:'queued',detail:'Waiting to start'}}};
  const response=value=>({ok:true,text:async()=>JSON.stringify(value)});
  w.matchMedia=()=>({matches:false});w.confirm=()=>true;w.HTMLElement.prototype.scrollIntoView=()=>{};w.setInterval=()=>1;
  w.fetch=async(path,options={})=>{
    if(path==='api/releases')return new Promise(()=>{});
    if(path==='api/deployments/cancel'){
      const {device_id}=JSON.parse(options.body);posts.push(device_id);
      if(device_id==='three')await new Promise(done=>releaseLast=done);
      deployment.results[device_id]={status:'cancelled',detail:'Cancelled before dispatch'};
      deployment.revision++;if(device_id==='three')deployment.status='partial';
      return response(structuredClone(deployment));
    }
    return response({devices:[],groups:[],inventory:{releases:[]},profiles:[],deployments:[structuredClone(deployment)],backups:[],audit:[],events:[],active_jobs:[],count:0,items:[]});
  };
  try{
    for(const script of w.document.querySelectorAll('script:not([src])'))w.eval(script.textContent);
    await tick();await tick();
    const details=w.document.querySelector('#deployment-active details');details.open=true;
    const cancellation=w.eval("cancelAllDeploymentUpdates({dataset:{deployment:'batch'}})");
    await tick();await tick();
    assert.deepEqual(posts,['two','three']);
    assert.match(w.document.getElementById('deployment-active').textContent,/Cancelled before dispatch/);
    assert.equal(w.document.querySelector('#deployment-active details').open,true);
    releaseLast();await cancellation;await tick();
    assert.equal(w.document.getElementById('deployment-active').textContent,'');
    const history=w.document.getElementById('action-history');
    assert.match(history.textContent,/Installed/);assert.match(history.textContent,/Cancelled before dispatch/);
    assert.match(history.querySelector('.event-dot').title,/partly cancelled.*1 complete, 2 cancelled/);
    assert.equal(history.querySelector('.event-dot').classList.contains('warn'),true);
    assert.deepEqual(posts,['two','three']);
  }finally{w.close()}
});
