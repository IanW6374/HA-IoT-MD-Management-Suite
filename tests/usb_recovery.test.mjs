import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {readRecoveryBundle, recoverSecuredDevice, recoveryPreflight, recoveryResultProbe, confirmRecoveryAfterReset, eraseRecoveryState, verifyRecoveryManifests, RecoveryREPL, configureRecoveryMode, RECOVERY_WATCHDOG_MS, RecoveryActivityGuard, recoveryFailure} from '../iot_md_management/rootfs/app/assets/usb_recovery.js';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const sha = async bytes => Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');
test('normal recovery hides resume, while contextual retry explains its non-erasing operation', () => {
  const nodes = new Map(['h2','.section-head p','#usb-recovery-resume-notice','#usb-recovery-approval'].map(selector => [selector, {
    textContent:'', classList:{toggle(name, hidden) { this.hidden = hidden; }}
  }]));
  const button = {textContent:''};
  const form = {elements:{resume:{value:''}}, closest(){return {querySelector:selector=>nodes.get(selector)};}, querySelector(){return button;}};
  for (const resume of [false, true, false]) {
    configureRecoveryMode(form, resume);
    assert.equal(form.elements.resume.value, resume ? '1' : '0');
    assert.equal(nodes.get('#usb-recovery-resume-notice').classList.hidden, !resume);
    assert.equal(nodes.get('h2').textContent, resume ? 'Resume interrupted recovery' : 'Clean USB recovery');
    assert.equal(button.textContent, resume ? 'Resume application staging' : 'Recover device');
    assert.match(nodes.get('#usb-recovery-approval').textContent, resume ? /without a core write or configuration erase/ : /erases all user configuration/);
    assert.match(nodes.get('.section-head p').textContent, resume ? /preserving the running core/ : /first-run setup/);
  }
});
const base = {format_version:6, target_board:'esp32-s3', signature:'a'.repeat(128), signature_scheme:'ecdsa-p256-sha256', version:'3.0.0-alpha.96'};
function pack(type, manifest, payload) {
  const header = Buffer.from(JSON.stringify(manifest)), size = Buffer.alloc(4);
  size.writeUInt32BE(header.length);
  return new Uint8Array(Buffer.concat([Buffer.from(type === 'iotcore' ? 'IOTC1\n' : 'IOTA1\n'), size, header, payload]));
}
async function bundles(legacy = false) {
  const payload = Buffer.alloc(4096, 255); payload[0] = 0xe9;
  const coreBytes = pack('iotcore', {...base, release_sequence:legacy?2801:2802, size:payload.length, sha256:await sha(payload)}, payload);
  const appPayload = Buffer.from('appsettings');
  const appBytes = pack('iotapp', {...base, files:[{path:'iotmd.py',size:3,sha256:await sha(appPayload.subarray(0,3))},{path:'app_settings.json',size:8,sha256:await sha(appPayload.subarray(3))}]}, appPayload);
  return [await readRecoveryBundle(coreBytes,'core.iotcore','iotcore'), await readRecoveryBundle(appBytes,'application.iotapp','iotapp')];
}
test('only complete correctly hashed signed-format bundles are accepted', async () => {
  const [core, app] = await bundles();
  assert.equal(core.payload.length,4096);
  assert.equal(app.manifest.files.length,2);
  await assert.rejects(readRecoveryBundle(core.bytes,'core.factory.bin','iotcore'),/signed/);
  const damaged = core.bytes.slice(); damaged[damaged.length-1] ^= 1;
  await assert.rejects(readRecoveryBundle(damaged,'core.iotcore','iotcore'),/digest/);
  const bad = {...app.manifest,files:[{path:'../secret',size:11,sha256:await sha(app.payload)}]};
  await assert.rejects(readRecoveryBundle(pack('iotapp',bad,app.payload),'app.iotapp','iotapp'),/invalid/);
  await assert.rejects(readRecoveryBundle(pack('iotapp',{...app.manifest,files:[]},app.payload),'app.iotapp','iotapp'),/complete/);
});
function fakeREPL(core, fail = '', app = null) {
  const calls = [];
  return {calls, async until(marker){calls.push('wait:'+marker); if(fail==='receipt') throw new Error('device rejected step'); if(marker==='\r\n') return JSON.stringify({status:'ready',core_version:core.manifest.version,application_version:app.manifest.version,application_sha256:app.sha256});}, async enter(){calls.push('enter');}, async reboot(){calls.push('reboot');}, async reconnect(){calls.push('reconnect');}, async exec(source){
    calls.push(source);
    if (fail && source.includes(fail)) throw new Error('device rejected step');
    if (source === recoveryPreflight) return JSON.stringify({device:'test-board',capacity:8192,target:'ota_1'});
    if (source.includes("open('.app-update.bundle.upload','rb')")) return app.sha256;
    if (source.includes('_hash=uhashlib')) return sha(core.payload);
    if (source.includes("print('handoff-ready')")) return 'handoff-ready';
    if (source.includes("open('.usb-recovery-result.json','r')")) return JSON.stringify({status:'ready',core_version:core.manifest.version,application_version:app.manifest.version,application_sha256:app.sha256});
    if (source === eraseRecoveryState) return 'recovery-ready';
    return '';
  }};
}
test('security, signatures and partition verification precede state erasure', async () => {
  const [core,app] = await bundles(), repl = fakeREPL(core,'',app), stages=[];
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x', (stage,percent,detail)=>stages.push({stage,percent,detail}));
  const index = text => repl.calls.findIndex(source => source.includes(text));
  assert.ok(index('secure_boot') < index('writeblocks'));
  assert.ok(index('verify_manifest_signature') < index('writeblocks'));
  assert.ok(index('_hash=uhashlib') < index('_remove_tree'));
  assert.ok(index('_target.set_boot()') < index('_remove_tree'));
  assert.ok(index('.usb-recovery.json.tmp') < index('reboot'));
  assert.ok(index("open('.app-update.bundle.upload','wb')") < index('reboot'));
  assert.equal(repl.calls.filter(source=>source==='reboot').length,1);
  assert.ok(!repl.calls.some(source=>source.includes('stage_bundle')));
  assert.equal(stages.at(-1).stage,8);
  assert.match(stages.at(-1).detail,/not independently confirmed/);
  assert.doesNotMatch(eraseRecoveryState,/erase_key\('verifykey'\)|erase_flash|flash_erase/);
});
test('unsecured board, signature and secure-boot rejection stop before erasure', async () => {
  const [core,app] = await bundles();
  for (const fail of ['secure_boot','verify_manifest_signature','_target.set_boot()']) {
    const repl=fakeREPL(core,fail);
    await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{}));
    assert.ok(!repl.calls.some(source=>source.includes('_remove_tree')));
    if (fail !== '_target.set_boot()') assert.ok(!repl.calls.some(source=>source.includes('writeblocks')));
  }
});
test('readback mismatch never erases user state or requests reboot', async () => {
  const [core,app] = await bundles(), repl=fakeREPL(core);
  const original=repl.exec;
  repl.exec=async source=>source.includes('_hash=uhashlib')?'wrong':original(source);
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{}),/configuration has not been erased/);
  assert.ok(!repl.calls.some(source=>source.includes('_remove_tree')));
  assert.ok(!repl.calls.includes('reboot'));
});
test('application rejection cannot be reported as successful recovery', async () => {
  const [core,app] = await bundles(), repl=fakeREPL(core,'receipt',app), stages=[];
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',stage=>stages.push(stage)));
  assert.ok(!stages.includes(8));
});
test('device Python snippets compile without execution', async () => {
  const [core,app] = await bundles(), repl=fakeREPL(core,'',app);
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{});
  const sources=repl.calls.filter(source=>!['enter','reboot','reconnect'].includes(source)&&!source.startsWith('wait:'));
  sources.push(verifyRecoveryManifests(core.manifest,app.manifest));
  sources.push(recoveryResultProbe);
  const result=spawnSync('python3',['-c','import json,sys\nfor s in json.load(sys.stdin): compile(s,"recovery","exec")'],{input:JSON.stringify(sources),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
});
async function flowControlledREPL({windowSize=128,output='result',deviceError='',splitReads=false,abortAfter=Infinity}={}) {
  let controller, receiving=false, available=0, consumed=0, aborted=false;
  const chunks=[], calls=[];
  const emit = bytes => {
    if (splitReads) for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    else controller.enqueue(bytes);
  };
  const port = {
    async open(){}, async setSignals(){}, async close(){},
    readable:new ReadableStream({start(value){controller=value;}}),
    writable:new WritableStream({write(chunk){
      calls.push(new Uint8Array(chunk));
      if (!receiving) {
        assert.deepEqual([...chunk],[5,65,1]);
        receiving=true; available=2*windowSize; consumed=0;
        emit(Uint8Array.of(82,1,windowSize&255,windowSize>>8,1));
      } else if (chunk.length===1 && chunk[0]===4) {
        receiving=false;
        if (!aborted) emit(Buffer.concat([Buffer.from([4]),Buffer.from(output),Buffer.from([4]),Buffer.from(deviceError),Buffer.from([4,62])]));
      } else {
        assert.ok(chunk.length<=available,'host exceeded negotiated receive window');
        available-=chunk.length; chunks.push(new Uint8Array(chunk));
        const before=Math.floor(consumed/windowSize); consumed+=chunk.length;
        if (consumed>=abortAfter) { aborted=true; emit(Uint8Array.of(4)); }
        else for (let count=before;count<Math.floor(consumed/windowSize);count++) {available+=windowSize;emit(Uint8Array.of(1));}
      }
    }}),
  };
  const repl=new RecoveryREPL(port); await repl.open();
  return {repl,chunks,calls};
}

test('raw-paste framing preserves binary window bytes, split UTF-8 output and sanitized exceptions', async () => {
  for (const options of [{output:'résult ✓',splitReads:true},{deviceError:'Traceback secret-password',splitReads:true}]) {
    const {repl}=await flowControlledREPL(options);
    try {
      if (options.deviceError) await assert.rejects(repl.exec('bad()'),error=>!error.message.includes('secret-password') && /rejected/.test(error.message));
      else assert.equal(await repl.exec('print("result")'),'résult ✓');
    } finally {await repl.close();}
  }
});

test('large core commands use receiver credits without background-sensitive pacing or polling', async () => {
  const source='_target.writeblocks(0,ubinascii.unhexlify("'+'ff'.repeat(4096)+'"))\n_wdt.feed()';
  const {repl,chunks}=await flowControlledREPL({splitReads:true});
  const original=setTimeout, scheduled=[];
  globalThis.setTimeout=(callback,milliseconds,...args)=>{scheduled.push(milliseconds);return original(callback,Math.max(1000,milliseconds),...args);};
  try {
    assert.equal(await repl.exec(source),'result');
    assert.equal(Buffer.concat(chunks).toString(),source);
    assert.ok(chunks.length>30);
    assert.ok(scheduled.every(milliseconds=>milliseconds>=15000),'transfer must not depend on short timers');
    assert.equal(repl.waiters.size,0);
    assert.equal(await repl.exec('print("next command")'),'result');
  } finally {globalThis.setTimeout=original;await repl.close();}
});

test('unsupported or malformed flow control stops without sending source or falling back', async () => {
  for (const response of ['R\x00','ra','R\x01\x00\x00']) {
    const repl=new RecoveryREPL({}), calls=[];
    repl.writer={async write(bytes){calls.push([...bytes]);}};
    repl.buffer=response;
    await assert.rejects(repl.exec('dangerous()'),/support|window/);
    assert.deepEqual(calls,[[5,65,1]]);
  }
});

test('receiver abort prevents further source transfer and never replays the command', async () => {
  const {repl,chunks,calls}=await flowControlledREPL({abortAfter:128,splitReads:true});
  try {
    await assert.rejects(repl.exec('x'.repeat(8192)),/before transfer completed/);
    assert.ok(Buffer.concat(chunks).length<=256);
    assert.deepEqual([...calls.at(-1)],[4]);
    assert.equal(calls.filter(value=>value.length===3&&value[0]===5).length,1);
  } finally {await repl.close();}
});

test('closed serial writes are classified without exposing native exception contents or replaying', async () => {
  const repl=new RecoveryREPL({}); let writes=0;
  repl.writer={async write(){writes++;throw new Error('Port has been closed: secret-password');}};
  await assert.rejects(repl.exec('source'),error=>error.code==='UART_DISCONNECTED'&&!error.message.includes('secret-password'));
  assert.equal(writes,1);
  await assert.rejects(repl.write('more source'),error=>error.code==='UART_DISCONNECTED');
  assert.equal(writes,1);
});

test('close wakes pending reads immediately and releases timeout waiters', async () => {
  const port={async close(){}}, repl=new RecoveryREPL(port);
  const pending=repl.read(1).catch(error=>error);
  await repl.close();
  assert.equal((await pending).code,'UART_DISCONNECTED');
  assert.equal(repl.waiters.size,0);
});

test('recovery watchdog stays enabled with an explicit policy-sized timeout in both paths', async () => {
  assert.equal(RECOVERY_WATCHDOG_MS,60000);
  for (const legacy of [false,true]) {
    const [core,app]=await bundles(legacy), repl=fakeREPL(core,'',app);
    await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{},legacy);
    const watchdog=repl.calls.filter(source=>source.includes('machine.WDT('));
    assert.ok(watchdog.length);
    for (const source of watchdog) {
      assert.match(source,/machine\.WDT\(0,timeout=60000\)/);
      assert.doesNotMatch(source,/machine\.WDT\(0\)/);
    }
  }
});

test('transport failures preserve stage-specific uncertainty instead of blaming a closed tab', () => {
  const error={code:'UART_DISCONNECTED',message:'native error'};
  for (let stage=1;stage<=7;stage++) {
    const result=recoveryFailure(error,{stage},true);
    assert.equal(result.status,'interrupted');
    assert.match(result.detail,/backgrounded/);
    assert.match(result.detail,/Do not factory-flash/);
    if (stage<=2) assert.match(result.detail,/has not erased user configuration/);
    if (stage===3) assert.match(result.detail,/boot target may have changed/);
    if (stage===4) assert.match(result.detail,/partially complete/);
    if (stage>=7) assert.match(result.detail,/may already be complete/);
    assert.doesNotMatch(result.detail,/native error/);
  }
  assert.equal(recoveryFailure({message:'signature invalid'},{stage:1}).status,'failed');
});

function guardPage() {
  const listeners=new Map();
  return {hidden:false,listeners,addEventListener(name,callback){listeners.set(name,callback);},removeEventListener(name){listeners.delete(name);}};
}
test('sleep guard warns on backgrounding, reacquires on foreground and releases on completion', async () => {
  const page=guardPage(), messages=[], locks=[];
  const browser={wakeLock:{async request(type){assert.equal(type,'screen');const lock={released:false,async release(){this.released=true;}};locks.push(lock);return lock;}}};
  const guard=new RecoveryActivityGuard(browser,page,message=>messages.push(message));
  guard.start(); await Promise.resolve(); await Promise.resolve();
  assert.equal(guard.wakeLock,locks[0]);
  page.hidden=true; await guard.update();
  assert.equal(guard.backgrounded,true); assert.equal(locks[0].released,true);
  assert.match(messages.at(-1),/background/);
  page.hidden=false; await guard.update();
  assert.equal(locks.length,2);
  await guard.stop(); assert.equal(locks[1].released,true);assert.equal(page.listeners.size,0);
});

test('unavailable or denied sleep prevention never blocks USB, and late locks are released', async () => {
  for (const browser of [{},{wakeLock:{async request(){throw new Error('denied');}}}]) {
    const page=guardPage(), guard=new RecoveryActivityGuard(browser,page,()=>{});
    guard.start();await Promise.resolve();await guard.stop();
    assert.equal(guard.wakeLock,null);
  }
  let grant;
  const page=guardPage(), guard=new RecoveryActivityGuard({wakeLock:{request(){return new Promise(resolve=>{grant=resolve;});}}},page,()=>{});
  guard.start();await guard.stop();
  const lock={released:false,async release(){this.released=true;}};
  grant(lock);await Promise.resolve();await Promise.resolve();
  assert.equal(lock.released,true);assert.equal(guard.wakeLock,null);
});

test('recoverable Web Serial read faults release the reader and resume reception', async () => {
  let replacements = 0, controller;
  const replacement = new ReadableStream({start(value){controller=value;}});
  const port = {
    async open(){}, async setSignals(){},
    writable:new WritableStream(),
    readable:{getReader(){return {async read(){throw new Error('framing error');},releaseLock(){replacements++; port.readable=replacement;}};}},
    async close(){assert.equal(this.readable.locked,false);assert.equal(this.writable.locked,false);},
  };
  const repl = new RecoveryREPL(port);
  await repl.open();
  controller.enqueue(new TextEncoder().encode('raw REPL; CTRL-B to exit\r\n>'));
  await repl.until('raw REPL; CTRL-B to exit\r\n>',1000);
  assert.equal(replacements,1); assert.equal(repl.readFaults,1);
  assert.equal(repl.readError,null);
  await repl.close();
});

test('fatal UART failure reopens the same port once and clears stale failure', async () => {
  const repl = new RecoveryREPL({}); let opens=0, closes=0;
  repl.readError = new Error('stale read fault');
  repl.enter = async () => {if(repl.readError)throw repl.readError;};
  repl.close = async () => {closes++;};
  repl.open = async () => {opens++; repl.readError=null;};
  await repl.reconnect();
  assert.equal(opens,1); assert.equal(closes,1);
});

test('completed erasure and restart failure are separate milestones', async () => {
  const [core,app] = await bundles(), repl=fakeREPL(core,'',app), stages=[];
  repl.until=async()=>{throw new Error('boot receipt missing');};
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',(stage,percent)=>stages.push([stage,percent])));
  assert.ok(stages.some(([stage,percent])=>stage===4&&percent===100));
  assert.deepEqual(stages.at(-1),[7,0]);
  assert.ok(stages.some(([stage,percent])=>stage===5&&percent===100));
  assert.equal(repl.calls.filter(source=>source.includes('_remove_tree')).length,1);
});

test('same-version rollback cannot pass the selected core confirmation', async () => {
  const [core,app] = await bundles(), repl=fakeREPL(core,'',app);
  repl.until=async marker=>marker==='\r\n'?JSON.stringify({status:'failed'}):'';
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{}));
  assert.ok(!repl.calls.some(source=>source.includes("open('.app-update.bundle'")));
});

test('explicit resume verifies running core and password without erasure or core writes', async () => {
  const [core,app]=await bundles(true), repl=fakeREPL(core), stages=[];
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',stage=>stages.push(stage),true);
  assert.ok(repl.calls.some(source=>source.includes('_target=_running')));
  assert.ok(repl.calls.some(source=>source.includes('bootstrap_key()==')));
  assert.ok(repl.calls.some(source=>source.includes('_hash=uhashlib')));
  assert.ok(!repl.calls.some(source=>/writeblocks|_remove_tree|_target.set_boot/.test(source)));
  assert.equal(repl.calls.filter(source=>source==='reboot').length,1);
  assert.equal(stages.at(-1),8);
  assert.ok(repl.calls.some(source=>source.includes('clear_recovery_request()')));
});

test('resume refuses provisioned devices or mismatched credentials before upload', async () => {
  const [core,app]=await bundles(true), repl=fakeREPL(core,'bootstrap_key()==');
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{},true));
  assert.ok(!repl.calls.some(source=>source.includes("open('.app-update.bundle'")));
});

test('application readback mismatch leaves no committed handoff and never resets', async () => {
  const [core,app]=await bundles(), repl=fakeREPL(core,'',app), original=repl.exec;
  repl.exec=async source=>source.includes("open('.app-update.bundle.upload','rb')")?'wrong':original(source);
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{}),/readback/);
  assert.ok(!repl.calls.includes('reboot'));
  assert.ok(!repl.calls.some(source=>source.includes("print('handoff-ready')")));
});

test('passive boot receipt leaves first-run setup running without console interruption', async () => {
  const [core,app]=await bundles(), repl=fakeREPL(core,'',app);
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{});
  const index=text=>repl.calls.findIndex(source=>source.includes(text));
  assert.ok(index('reboot')<index('wait:USB-RECOVERY-RESULT'));
  assert.ok(index('wait:USB-RECOVERY-RESULT')<index('wait:\r\n'));
  assert.ok(!repl.calls.includes('reconnect'));
  assert.equal(repl.calls.filter(source=>source==='enter').length,1);
});

test('new-core resume transfers before its single reset without erasing or writing core', async () => {
  const [core,app]=await bundles(), repl=fakeREPL(core,'',app);
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{},true);
  assert.ok(!repl.calls.some(source=>/writeblocks|_remove_tree|_target.set_boot/.test(source)));
  assert.equal(repl.calls.filter(source=>source==='reboot').length,1);
});

test('old signed core is refused before USB mutations unless explicitly resuming', async () => {
  const [core,app]=await bundles(true), repl=fakeREPL(core);
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{}),/Alpha 97/);
  assert.deepEqual(repl.calls,[]);
});

const expectedRecovery = {device:'original-board', partition:'ota_1', coreVersion:'alpha97', applicationVersion:'alpha97', applicationSHA256:'a'.repeat(64)};
function confirmationREPL(mode='passive', override={}) {
  const expected=expectedRecovery, calls=[];
  const receipt={status:'ready',core_version:expected.coreVersion,application_version:expected.applicationVersion,application_sha256:expected.applicationSHA256};
  const saved={device:expected.device,partition:expected.partition,provisioned:false,core_version:expected.coreVersion,application_status:'ready',application_version:expected.applicationVersion,has_application:true,selected_paths:['iotmd.py','app_settings.json'],receipt,...override};
  let reopens=0;
  return {calls,async until(marker){
    calls.push('wait:'+marker);
    if(mode==='lost'||(mode==='reconnect'&&!reopens)) throw Object.assign(new Error('USB console disconnected'), {code:reopens?'UART_TIMEOUT':'UART_DISCONNECTED'});
    return marker==='\r\n'?JSON.stringify(receipt):'';
  },async reopenAfterReset(){reopens++; calls.push('reopen');},async enter(){calls.push('enter');},async exec(source){calls.push(source);return JSON.stringify(saved);},async reboot(){calls.push('restart-setup');}};
}

test('reset disconnect is reopened passively without interrupting setup when receipt arrives', async () => {
  const repl=confirmationREPL('reconnect');
  await confirmRecoveryAfterReset(repl,expectedRecovery,()=>{},0);
  assert.equal(repl.calls.filter(call=>call==='reopen').length,1);
  assert.ok(!repl.calls.includes('enter'));
  assert.ok(!repl.calls.includes('restart-setup'));
});

test('missed receipt uses saved result and restores setup without upload or erase replay', async () => {
  const repl=confirmationREPL('lost');
  await confirmRecoveryAfterReset(repl,expectedRecovery,()=>{},0);
  assert.equal(repl.calls.filter(call=>call===recoveryResultProbe).length,1);
  assert.equal(repl.calls.filter(call=>call==='restart-setup').length,1);
  assert.ok(!repl.calls.some(call=>/writeblocks|_remove_tree|stage_bundle|\.upload/.test(call)));
});

test('wrong device or configured device is never restarted during confirmation', async () => {
  for (const override of [{device:'other-board'},{provisioned:true}]) {
    const repl=confirmationREPL('lost',override);
    await assert.rejects(confirmRecoveryAfterReset(repl,expectedRecovery,()=>{},0));
    assert.ok(!repl.calls.includes('restart-setup'));
  }
});

test('wrong slot, core, app state or digest cannot be reported as confirmed', async () => {
  for (const override of [{partition:'ota_0'},{core_version:'old'},{application_status:'idle'},{selected_paths:['app_settings.json']},{receipt:null},{receipt:{}},{receipt:{status:'failed'}},{receipt:{status:'ready',core_version:'alpha97',application_version:'alpha97',application_sha256:'b'.repeat(64)}}]) {
    const repl=confirmationREPL('lost',override);
    await assert.rejects(confirmRecoveryAfterReset(repl,expectedRecovery,()=>{},0));
    assert.equal(repl.calls.filter(call=>call==='restart-setup').length,1);
  }
});

test('explicit failed boot receipt does not trigger a recovery replay or console inspection', async () => {
  const repl=confirmationREPL();
  repl.until=async marker=>marker==='\r\n'?JSON.stringify({status:'failed'}):'';
  await assert.rejects(confirmRecoveryAfterReset(repl,expectedRecovery,()=>{},0));
  assert.deepEqual(repl.calls,[]);
});

test('same SerialPort can be reopened after transient open failure without selecting another board', async () => {
  const port={}, repl=new RecoveryREPL(port); let opens=0,closes=0;
  repl.close=async()=>{closes++;};
  repl.open=async()=>{opens++;if(opens===1)throw new Error('port not back yet');};
  await repl.reopenAfterReset(2000);
  assert.equal(repl.port,port);
  assert.equal(opens,2); assert.equal(closes,2);
});

test('UART timeout is classified without exposing device output', async () => {
  const repl=new RecoveryREPL({});
  await assert.rejects(repl.until('missing',0),error=>error.code==='UART_TIMEOUT');
});

test('complete recovery survives a missed reset receipt without replaying transfer milestones', async () => {
  const [core,app]=await bundles(), repl=fakeREPL(core,'',app), originalExec=repl.exec;
  const originalNow=Date.now; let clock=0;
  // Skip only the passive observation budget; the full transfer still runs.
  repl.until=async()=>{throw Object.assign(new Error('reset lost receipt'),{code:'UART_TIMEOUT'});};
  repl.exec=async source=>source===recoveryResultProbe?JSON.stringify({device:'test-board',core_version:core.manifest.version,partition:'ota_1',provisioned:false,application_status:'ready',application_version:app.manifest.version,has_application:true,selected_paths:['iotmd.py','app_settings.json'],receipt:{status:'ready',core_version:core.manifest.version,application_version:app.manifest.version,application_sha256:app.sha256}}):originalExec(source);
  Date.now=()=>{clock+=180001;return clock;};
  const milestones=[];
  try { await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',stage=>milestones.push(stage)); }
  finally { Date.now=originalNow; }
  assert.equal(milestones.at(-1),8);
  assert.equal(repl.calls.filter(source=>source.includes('_remove_tree')).length,1);
  assert.equal(repl.calls.filter(source=>source.includes('writeblocks')).length,1);
  assert.equal(repl.calls.filter(source=>source.includes("open('.app-update.bundle.upload','wb')")).length,1);
  assert.equal(repl.calls.filter(source=>source==='reboot').length,2);
});
