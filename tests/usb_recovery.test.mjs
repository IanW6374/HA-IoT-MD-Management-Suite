import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {readRecoveryBundle, recoverSecuredDevice, recoveryPreflight, eraseRecoveryState, verifyRecoveryManifests, RecoveryREPL} from '../iot_md_management/rootfs/app/assets/usb_recovery.js';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const sha = async bytes => Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');
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
  const result=spawnSync('python3',['-c','import json,sys\nfor s in json.load(sys.stdin): compile(s,"recovery","exec")'],{input:JSON.stringify(sources),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
});
test('raw REPL framing consumes split output and masks device exceptions', async () => {
  const repl=new RecoveryREPL({}); repl.write=async()=>{};
  repl.buffer='OKresult\x04\x04>';
  assert.equal(await repl.exec('print("result")'),'result');
  repl.buffer='OK\x04Traceback secret-password\x04>';
  await assert.rejects(repl.exec('bad()'),error=>!error.message.includes('secret-password') && /rejected/.test(error.message));
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
