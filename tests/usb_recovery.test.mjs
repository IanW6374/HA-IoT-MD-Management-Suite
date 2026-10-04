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
async function bundles() {
  const payload = Buffer.alloc(4096, 255); payload[0] = 0xe9;
  const coreBytes = pack('iotcore', {...base, size:payload.length, sha256:await sha(payload)}, payload);
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
function fakeREPL(core, fail = '') {
  const calls = [];
  return {calls, async enter(){calls.push('enter');}, async reboot(){calls.push('reboot');}, async exec(source){
    calls.push(source);
    if (fail && source.includes(fail)) throw new Error('device rejected step');
    if (source === recoveryPreflight) return JSON.stringify({device:'test-board',capacity:8192});
    if (source.includes('_hash=uhashlib')) return sha(core.payload);
    return '';
  }};
}
test('security, signatures and partition verification precede state erasure', async () => {
  const [core,app] = await bundles(), repl = fakeREPL(core), stages=[];
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x', (stage,percent,detail)=>stages.push({stage,percent,detail}));
  const index = text => repl.calls.findIndex(source => source.includes(text));
  assert.ok(index('secure_boot') < index('writeblocks'));
  assert.ok(index('verify_manifest_signature') < index('writeblocks'));
  assert.ok(index('_hash=uhashlib') < index('_remove_tree'));
  assert.ok(index('_target.set_boot()') < index('_remove_tree'));
  assert.ok(index('mark_app_valid_cancel_rollback') < index('stage_bundle'));
  assert.equal(stages.at(-1).stage,7);
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
  const [core,app] = await bundles(), repl=fakeREPL(core,'stage_bundle'), stages=[];
  await assert.rejects(recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',stage=>stages.push(stage)));
  assert.ok(!stages.includes(7));
});
test('device Python snippets compile without execution', async () => {
  const [core,app] = await bundles(), repl=fakeREPL(core);
  await recoverSecuredDevice(repl,core,app,'StrongSetup7Key!x',()=>{});
  const sources=repl.calls.filter(source=>!['enter','reboot'].includes(source));
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
