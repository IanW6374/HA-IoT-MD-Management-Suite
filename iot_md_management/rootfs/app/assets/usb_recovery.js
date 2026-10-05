import {usbAvailability} from './usb_seed.js';

const encoder = new TextEncoder(), decoder = new TextDecoder();
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const hex = bytes => [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
const digest = async bytes => hex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
const pyString = value => JSON.stringify(String(value));

export async function readRecoveryBundle(bytes, filename, type) {
  const magic = type === 'iotcore' ? 'IOTC1\n' : 'IOTA1\n';
  if (!filename.endsWith('.' + type) || bytes.length < 10 || bytes.length > 4 * 1024 * 1024 || decoder.decode(bytes.slice(0, 6)) !== magic) throw new Error('Choose a signed .' + type + ' bundle, not a factory image.');
  const length = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(6);
  if (length < 2 || length > (type === 'iotcore' ? 2048 : 65535) || 10 + length > bytes.length) throw new Error('Invalid bundle manifest length.');
  const manifest = JSON.parse(decoder.decode(bytes.slice(10, 10 + length)));
  if (manifest.format_version !== 6 || (manifest.target_board || manifest.platform) !== 'esp32-s3' || !/^[0-9a-f]{128}$/i.test(manifest.signature || '')) throw new Error('A signed format-6 ESP32-S3 bundle is required.');
  const payload = bytes.slice(10 + length);
  if (type === 'iotcore') {
    if (payload.length !== manifest.size || payload[0] !== 0xe9 || await digest(payload) !== manifest.sha256) throw new Error('Core bundle digest or size is invalid.');
  } else {
    if (bytes.length > 2 * 1024 * 1024) throw new Error('Application bundle exceeds the recovery limit.');
    if (!Array.isArray(manifest.files)) throw new Error('Application file list is missing.');
    let offset = 0;
    const paths = new Set();
    for (const file of manifest.files) {
      if (!Number.isSafeInteger(file.size) || file.size < 0 || paths.has(file.path) || typeof file.path !== 'string' || !file.path || file.path.startsWith('/') || file.path.includes('\\') || file.path.split('/').some(part => ['', '.', '..'].includes(part))) throw new Error('Application file list is invalid.');
      paths.add(file.path);
      const content = payload.slice(offset, offset + file.size);
      if (content.length !== file.size || await digest(content) !== file.sha256) throw new Error('Application content verification failed.');
      offset += file.size;
    }
    if (offset !== payload.length || !paths.has('iotmd.py') || !paths.has('app_settings.json')) throw new Error('A complete application bundle is required.');
  }
  return {manifest, payload, bytes, filename, sha256: await digest(bytes)};
}

// Raw REPL uses the running core and encrypted partition API, never ROM flash
// commands. No DTR/RTS reset sequence or soft reset is used to enter the REPL.
export class RecoveryREPL {
  constructor(port) { this.port = port; this.buffer = ''; this.closed = false; this.readFaults = 0; }
  async open() {
    this.closed = false; this.readError = null; this.buffer = '';
    this.decoder = new TextDecoder();
    await this.port.open({baudRate: 115200});
    await this.port.setSignals({dataTerminalReady: false, requestToSend: false});
    this.writer = this.port.writable.getWriter();
    this.reading = (async () => {
      // Web Serial replaces readable after recoverable framing/parity errors.
      // Release the failed reader and acquire the replacement stream instead
      // of retaining a permanent readError across every reconnect attempt.
      while (!this.closed && this.port.readable) {
        const reader = this.port.readable.getReader(); this.reader = reader;
        try {
          while (!this.closed) {
            const {value, done} = await reader.read();
            if (done) break;
            this.buffer += this.decoder.decode(value, {stream: true});
            if (this.buffer.length > 1024 * 1024) {
              this.readError = new Error('Unexpected UART output.'); return;
            }
          }
        } catch (_) {
          if (!this.closed) { this.readFaults++; this.decoder = new TextDecoder(); }
        } finally {
          reader.releaseLock(); if (this.reader === reader) this.reader = null;
        }
        if (!this.closed) await delay(25);
      }
      if (!this.closed) this.readError = new Error('UART disconnected during recovery.');
    })();
  }
  async write(value) { await this.writer.write(typeof value === 'string' ? encoder.encode(value) : value); }
  async until(marker, timeout = 15000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const index = this.buffer.indexOf(marker);
      if (index >= 0) { const value = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + marker.length); return value; }
      if (this.readError) throw this.readError;
      await delay(10);
    }
    throw new Error('UART response timed out. Select the UART interface with firmware running, not JTAG or ROM download mode.');
  }
  async enter(timeout = 15000) {
    this.buffer = '';
    await this.write('\r\x03\x03\x02');
    await delay(100);
    await this.write('\r\x01');
    await this.until('raw REPL; CTRL-B to exit\r\n>', timeout);
  }
  async exec(source, timeout = 30000) {
    for (let offset = 0; offset < source.length; offset += 128) {
      await this.write(source.slice(offset, offset + 128));
      await delay(2);
    }
    await this.write('\x04');
    const ack = await this.until('OK', timeout);
    if (ack) throw new Error('Unexpected raw REPL response.');
    const output = await this.until('\x04', timeout);
    const error = await this.until('\x04', timeout);
    await this.until('>', timeout);
    // Do not expose arbitrary UART exceptions (they can contain credentials).
    if (error) throw new Error('The device rejected a recovery step. User state may be partially changed; keep power connected and inspect the device.');
    return output.trim();
  }
  async reboot() {
    this.buffer = '';
    await this.write('import machine\nmachine.reset()\n\x04');
    await delay(2000);
  }
  async reconnect() {
    // Reopen at most once to recover a stale/fatal stream. Never reselect a
    // different device, assert BOOT, or replay erasure/partition writes.
    let reopened = false;
    for (let attempt = 0; attempt < 12; attempt++) {
      try { await this.enter(2500); return; }
      catch (_) {
        if (!reopened && (this.readError || attempt >= 2)) {
          reopened = true;
          await this.close(); await delay(500); await this.open();
        }
        await delay(250);
      }
    }
    throw new Error('UART reconnect failed after configuration reset. The core may already be in first-run setup. Use Resume application staging only after reconnecting UART; do not factory-flash this secured board.');
  }
  async close() {
    this.closed = true;
    try { await this.reader?.cancel(); } catch (_) {}
    await this.reading;
    this.reader = null; this.writer?.releaseLock(); this.writer = null;
    await this.port.close().catch(() => {});
  }
}

export const recoveryPreflight = `import _iotmd_platform_v3 as _platform, esp32, machine, ujson as json, ubinascii, uhashlib, update_security, credential_store
_caps=_platform.capabilities()
assert _caps['board']['target']=='esp32-s3'
assert _caps['security']['secure_boot'] and _caps['security']['flash_encryption'] and _caps['security']['encrypted_nvs']
_running=esp32.Partition(esp32.Partition.RUNNING)
_target=_running.get_next_update()
assert _target is not None
_wdt=machine.WDT(0)
_wdt.feed()
print(json.dumps({'device':ubinascii.hexlify(machine.unique_id()).decode(),'capacity':_target.info()[3],'target':_target.info()[4]}))`;

export function verifyRecoveryManifests(core, application) {
  return `_core=json.loads(${pyString(JSON.stringify(core))})\n_app=json.loads(${pyString(JSON.stringify(application))})\nupdate_security.validate_manifest('iotcore',_core)\n_key=update_security._public_key()\nassert _key is not None\n_verification=_key[0].to_bytes(32,'big')+_key[1].to_bytes(32,'big')\nassert _app['signature_scheme']==update_security.SIGNATURE_SCHEME\nassert update_security.verify_manifest_signature('iotapp',_app,_app['signature'],_key)\nimport app_update\nfor _entry in _app['files']:\n _path=app_update._safe_path(_entry['path'])\n assert not app_update.is_protected_path(_path)\n_wdt.feed()\nprint('signatures-verified')`;
}

export const eraseRecoveryState = `import uos as os
def _remove_tree(path):
 for item in list(os.ilistdir(path)):
  child=path.rstrip('/')+'/'+item[0]
  if item[1]&0x4000:
   _remove_tree(child)
   os.rmdir(child)
  else:
   os.remove(child)
  _wdt.feed()
_store=esp32.NVS('iotmd_config')
# Preserve verification identity and all flash/NVS encryption key partitions.
for _key in ('cfg0','cfg1','active','nettrial','factoryreset'):
 try:_store.erase_key(_key)
 except OSError:pass
_store.set_blob('bootkey',_password)
# Preserve the effective identity even if it was a filesystem override of NVS.
_store.set_blob('verifykey',_verification)
_store.commit()
_remove_tree('/')
print('recovery-ready')`;

export async function recoverSecuredDevice(repl, core, application, password, progress, resume = false) {
  await repl.enter();
  const board = JSON.parse(await repl.exec(recoveryPreflight));
  if (!Number.isSafeInteger(board.capacity) || board.capacity < Math.ceil(core.payload.length / 4096) * 4096) throw new Error('The signed core does not fit the inactive OTA partition.');
  await repl.exec(verifyRecoveryManifests(core.manifest, application.manifest), 120000);
  const padded = new Uint8Array(Math.ceil(core.payload.length / 4096) * 4096).fill(255);
  padded.set(core.payload);
  if (resume) {
    await repl.exec(`assert not credential_store.is_provisioned()\nassert credential_store.bootstrap_key()==ubinascii.unhexlify('${hex(encoder.encode(password))}')\n_target=_running`);
    progress(2, 100, 'Resume selected: retaining the existing core without writing it.');
  } else {
    progress(2, 0, 'Security and trusted bundle signatures verified. Writing the inactive core partition.');
    for (let offset = 0; offset < padded.length; offset += 4096) {
      await repl.exec(`_target.writeblocks(${offset / 4096},ubinascii.unhexlify('${hex(padded.slice(offset, offset + 4096))}'))\n_wdt.feed()`);
      progress(2, Math.floor((offset + 4096) / padded.length * 100), 'Writing the inactive core partition. Keep USB connected.');
    }
  }
  progress(3, 0, resume ? 'Verifying the running core matches the selected signed bundle.' : 'Verifying the installed core before erasing user configuration.');
  const actual = await repl.exec(`_hash=uhashlib.sha256()\n_buf=bytearray(4096)\nfor _block in range(${padded.length / 4096}):\n _target.readblocks(_block,_buf)\n _hash.update(_buf)\n _wdt.feed()\nprint(ubinascii.hexlify(_hash.digest()).decode())`, 120000);
  if (actual !== await digest(padded)) throw new Error('Installed core verification failed. User configuration has not been erased.');
  progress(3, 100, 'Core partition verified.');
  if (!resume) {
    // ESP-IDF validates the secure-boot image before configuration erasure.
    await repl.exec('_target.set_boot()\n_wdt.feed()');
    progress(4, 0, 'Core verified. Erasing user configuration.');
    await repl.exec(`_password=ubinascii.unhexlify('${hex(encoder.encode(password))}')\nimport credential_security\ncredential_security.validate_password_strength(_password.decode())`);
    if (await repl.exec(eraseRecoveryState, 120000) !== 'recovery-ready') throw new Error('Configuration reset acknowledgement was not received. Inspect the device before retrying.');
    progress(4, 100, 'Configuration erasure confirmed.');
    progress(5, 0, 'Restarting the core and reconnecting UART. No further erasure will be attempted.');
    await repl.reboot();
    await repl.reconnect();
    // Same-version rollback must not be mistaken for the selected core.
    await repl.exec(`import esp32\nassert esp32.Partition(esp32.Partition.RUNNING).info()[4]==${pyString(board.target)}`);
  } else {
    progress(4, 100, 'Resume selected: configuration is already unprovisioned; no erasure performed.');
    progress(5, 100, 'Existing recovery core is connected; no intermediate restart required.');
  }
  await repl.exec(`import esp32, credential_store, core_metadata, app_update, ubinascii, machine\nassert core_metadata.CORE_FIRMWARE_VERSION==${pyString(core.manifest.version)}\nassert not credential_store.is_provisioned()\nassert len(credential_store.bootstrap_key())==${password.length}\nesp32.Partition.mark_app_valid_cancel_rollback()\n_wdt=machine.WDT(0)\n_file=open('.app-update.bundle','wb')`);
  progress(6, 0, 'Recovery core running. Uploading the signed application.');
  for (let offset = 0; offset < application.bytes.length; offset += 512) {
    await repl.exec(`_file.write(ubinascii.unhexlify('${hex(application.bytes.slice(offset, offset + 512))}'))\n_wdt.feed()`);
    progress(6, Math.min(99, Math.floor((offset + 512) / application.bytes.length * 100)), 'Uploading the signed application.');
  }
  await repl.exec("_file.close()\n_result=app_update.stage_bundle('.app-update.bundle',False)\nassert _result.get('status')=='ready' and _result.get('has_application')\nassert 'app_settings.json' in _result.get('selected_paths',())\nimport recovery_boot\nrecovery_boot.clear_recovery_request()\n_wdt.feed()", 180000);
  progress(6, 100, 'Signed application verified and staged.');
  progress(7, 0, 'Signed application staged. Requesting first-run startup.');
  await repl.reboot();
  progress(8, 100, 'Clean USB recovery completed; signed core verified and application staged. Setup hotspot is not independently confirmed. Use the retained password file when IoT-MD-Setup appears.');
  return board.device;
}

function initializeRecovery() {
  const form = document.getElementById('usb-recovery-form');
  if (!form) return;
  const support = document.getElementById('usb-recovery-support'), status = document.getElementById('usb-recovery-status');
  const button = form.querySelector('button'), fieldset = form.querySelector('fieldset');
  const policy = document.permissionsPolicy || document.featurePolicy;
  const availability = usbAvailability(navigator, window.isSecureContext && location.protocol === 'https:', !policy || policy.allowsFeature('serial'), window.top !== window);
  support.textContent = availability.message.replaceAll('seeding', 'USB recovery');
  support.className = availability.ready ? 'status' : 'status error';
  fieldset.disabled = !availability.ready;
  const workspace = document.getElementById('usb-recovery-workspace');
  workspace.classList.toggle('hidden', !availability.workspace);
  form.elements.resume.checked = new URLSearchParams(location.search).get('resume') === '1';
  let running = false;
  window.addEventListener('beforeunload', event => { if (running) { event.preventDefault(); event.returnValue = ''; } });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (running || fieldset.disabled) return;
    let repl, job, timer, pending = Promise.resolve();
    const publish = () => { if (!job) return; const snapshot = {...job}; pending = pending.catch(() => {}).then(() => window.api('api/seed/' + job.id, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(snapshot)})); return pending; };
    const progress = (stage, percent, detail) => {
      const previousStage = job.stage;
      job = {...job, stage, percent, detail}; window.localSeedJob = job;
      window.applySeedState({jobs: [...(window.currentSeedJobs || []).filter(item => item.id !== job.id), job]});
      window.renderDeployments();
      if (stage !== previousStage || percent === 100) publish()?.catch(() => {});
    };
    running = true; button.disabled = true;
    status.textContent = 'Choose the device’s UART interface. Firmware must be running; do not hold BOOT.';
    try {
      const port = await navigator.serial.requestPort();
      const coreFile = form.elements.core.files[0], appFile = form.elements.application.files[0];
      const core = await readRecoveryBundle(new Uint8Array(await coreFile.arrayBuffer()), coreFile.name, 'iotcore');
      const application = await readRecoveryBundle(new Uint8Array(await appFile.arrayBuffer()), appFile.name, 'iotapp');
      const password = (await form.elements.password_file.files[0].text()).trim();
      if (!/^[\x21-\x7e]{16,63}$/.test(password) || !/[A-Z]/.test(password) || !/[a-z]/.test(password) || !/[0-9]/.test(password)) throw new Error('Choose the retained strong setup-password file (16–63 printable characters).');
      const resume = form.elements.resume.checked;
      job = await window.api('api/seed', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({kind:'recovery', milestone_count:8, resume, image:core.filename, application:application.filename, sha256:core.sha256, application_sha256:application.sha256, confirmation:form.elements.confirmation.value, credential_retained:form.elements.credential_retained.checked, erase_confirmed:form.elements.erase_confirmed.checked})});
      window.setActionView('inflight', true);
      const query = new URLSearchParams(location.search); query.set('usb_job', job.id);
      history.replaceState({}, '', `actions?${query.toString()}`);
      timer = setInterval(() => publish()?.catch(() => {}), 2000);
      repl = new RecoveryREPL(port); await repl.open();
      await recoverSecuredDevice(repl, core, application, password, progress, resume);
      job.status = 'complete';
      // Reuse the seed metadata retry channel. Never persist bundle bytes,
      // password contents or UART commands, including on a Management outage.
      try { sessionStorage.setItem('iot-md-seed-completion', JSON.stringify(job)); } catch (_) {}
      await publish().then(() => sessionStorage.removeItem('iot-md-seed-completion')).catch(() => {});
      const outcome = document.getElementById('seed-outcome'); outcome.classList.remove('hidden','success'); outcome.textContent = job.detail;
      form.reset();
    } catch (error) {
      // Serial errors are safe; device exceptions are deliberately sanitized by RecoveryREPL.
      status.className = 'status error'; status.textContent = error.message;
      if (error.name === 'SecurityError' && availability.ready && window.top !== window) workspace.classList.remove('hidden');
      if (job) {
        job.status = 'failed'; job.detail = error.message;
        try { sessionStorage.setItem('iot-md-seed-completion', JSON.stringify(job)); } catch (_) {}
        await publish().then(() => sessionStorage.removeItem('iot-md-seed-completion')).catch(() => {});
      }
      // Keep the failed result and completed milestones visible. Do not return
      // operators to a fresh destructive form after an interrupted restart.
      if (job) {
        window.localSeedJob = job;
        window.applySeedState({jobs: window.currentSeedJobs || []});
        window.setActionView('inflight', true);
      }
    } finally {
      clearInterval(timer); if (repl) await repl.close().catch(() => {});
      running = false; button.disabled = false; window.localSeedJob = null;
      form.elements.confirmation.value = ''; window.refreshSeed();
    }
  });
}

if (typeof document !== 'undefined') initializeRecovery();
