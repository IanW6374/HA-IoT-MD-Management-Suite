import {usbAvailability} from './usb_seed.js';

const encoder = new TextEncoder(), decoder = new TextDecoder();
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const hex = bytes => [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
const digest = async bytes => hex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
const pyString = value => JSON.stringify(String(value));
export const RECOVERY_WATCHDOG_MS = 60000;
const binaryString = bytes => {
  let value = '';
  for (let offset = 0; offset < bytes.length; offset += 4096) value += String.fromCharCode(...bytes.subarray(offset, offset + 4096));
  return value;
};
const uartError = () => Object.assign(new Error('The selected USB serial connection was lost or closed. A device reset, USB interruption or computer sleep may have caused this; the cause is not confirmed.'), {code:'UART_DISCONNECTED'});
const uartTimeout = () => Object.assign(new Error('UART response timed out. Keep the USB workspace visible and computer awake, and check the selected UART interface.'), {code:'UART_TIMEOUT'});

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
  constructor(port) { this.port = port; this.buffer = ''; this.closed = false; this.readFaults = 0; this.waiters = new Set(); }
  async open() {
    this.closed = false; this.readError = null; this.buffer = '';
    await this.port.open({baudRate: 115200});
    await this.port.setSignals({dataTerminalReady: false, requestToSend: false});
    this.writer = this.port.writable.getWriter();
    this.isOpen = true;
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
            // Preserve all eight bits: raw-paste window sizes are binary,
            // not UTF-8. Decode only the final command output as text.
            this.buffer += binaryString(value);
            if (this.buffer.length > 1024 * 1024) {
              this.readError = new Error('Unexpected UART output.'); this.notifyInput(); return;
            }
            this.notifyInput();
          }
        } catch (_) {
          if (!this.closed) this.readFaults++;
        } finally {
          reader.releaseLock(); if (this.reader === reader) this.reader = null;
        }
        if (!this.closed) await delay(25);
      }
      if (!this.closed) { this.readError = uartError(); this.notifyInput(); }
    })();
  }
  notifyInput() { for (const wake of [...this.waiters]) wake(); }
  waitForInput(deadline) {
    return new Promise(resolve => {
      const wake = () => { clearTimeout(timer); this.waiters.delete(wake); resolve(); };
      const timer = setTimeout(wake, Math.max(0, deadline - Date.now()));
      this.waiters.add(wake);
    });
  }
  async write(value) {
    if (this.closed || !this.writer) throw uartError();
    if (this.readError) throw this.readError;
    try { await this.writer.write(typeof value === 'string' ? encoder.encode(value) : value); }
    catch (_) { this.readError = uartError(); this.notifyInput(); throw this.readError; }
  }
  async read(length, timeout = 15000) {
    const deadline = Date.now() + timeout;
    while (this.buffer.length < length) {
      if (this.readError) throw this.readError;
      if (this.closed) throw uartError();
      if (Date.now() >= deadline) throw uartTimeout();
      await this.waitForInput(deadline);
    }
    const value = this.buffer.slice(0, length); this.buffer = this.buffer.slice(length);
    return value;
  }
  async until(marker, timeout = 15000) {
    const end = Date.now() + timeout;
    while (true) {
      const index = this.buffer.indexOf(marker);
      if (index >= 0) { const value = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + marker.length); return value; }
      if (this.readError) throw this.readError;
      if (this.closed) throw uartError();
      if (Date.now() >= end) throw uartTimeout();
      await this.waitForInput(end);
    }
  }
  async enter(timeout = 15000) {
    this.buffer = '';
    await this.write('\r\x03\x03\x02');
    await delay(100);
    await this.write('\r\x01');
    await this.until('raw REPL; CTRL-B to exit\r\n>', timeout);
  }
  async exec(source, timeout = 30000) {
    // Receiver-controlled raw-paste avoids both timer throttling and UART
    // buffer overflow. Never fall back to an unpaced large raw-REPL write.
    await this.write('\x05A\x01');
    if (await this.read(2, timeout) !== 'R\x01') throw new Error('The running core does not support flow-controlled USB recovery. No command was sent; use a supported IoT-MD core.');
    const header = await this.read(2, timeout), windowSize = header.charCodeAt(0) | (header.charCodeAt(1) << 8);
    if (!windowSize) throw new Error('Invalid USB recovery flow-control window.');
    const bytes = encoder.encode(source);
    let remaining = windowSize;
    for (let offset = 0; offset < bytes.length;) {
      while (remaining === 0 || this.buffer.length) {
        const control = await this.read(1, timeout);
        if (control === '\x01') remaining += windowSize;
        else if (control === '\x04') {
          await this.write('\x04');
          throw new Error('The device rejected a recovery command before transfer completed. No command will be replayed; inspect the device.');
        } else throw new Error('Unexpected USB recovery flow-control response.');
      }
      const count = Math.min(remaining, bytes.length - offset);
      await this.write(bytes.subarray(offset, offset + count));
      remaining -= count; offset += count;
    }
    await this.write('\x04');
    const ack = await this.until('\x04', timeout);
    if ([...ack].some(char => char !== '\x01')) throw new Error('Unexpected USB recovery transfer acknowledgement.');
    const output = await this.until('\x04', timeout);
    const error = await this.until('\x04', timeout);
    await this.until('>', timeout);
    // Do not expose arbitrary UART exceptions (they can contain credentials).
    if (error) throw new Error('The device rejected a recovery step. User state may be partially changed; keep power connected and inspect the device.');
    return decoder.decode(Uint8Array.from(output, char => char.charCodeAt(0))).trim();
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
  async reopenAfterReset(timeout = 15000) {
    // Reuse only the exact user-selected SerialPort. Never pick another USB
    // device or manipulate BOOT/EN to recover a disappearing console.
    await this.close();
    const deadline = Date.now() + timeout;
    do {
      try { await this.open(); return; }
      catch (_) { await this.close(); await delay(500); }
    } while (Date.now() < deadline);
    throw Object.assign(new Error('The selected UART has not returned after reset. Recovery may already be complete; keep power connected and verify its saved result. Do not erase or factory-flash again.'), {code:'UART_DISCONNECTED'});
  }
  async close() {
    this.closed = true;
    this.isOpen = false;
    this.notifyInput();
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
_wdt=machine.WDT(0,timeout=${RECOVERY_WATCHDOG_MS})
_wdt.feed()
print(json.dumps({'device':ubinascii.hexlify(machine.unique_id()).decode(),'capacity':_target.info()[3],'target':_target.info()[4],'running':_running.info()[4]}))`;

export const recoveryResultProbe = `import machine, ubinascii, ujson as json, credential_store, core_metadata, esp32, app_update
try:
 with open('.usb-recovery-result.json','r') as stream:
  _receipt=json.load(stream)
except (OSError, ValueError):
 _receipt={}
_state=app_update.update_status()
print(json.dumps({'device':ubinascii.hexlify(machine.unique_id()).decode(),'core_version':core_metadata.CORE_FIRMWARE_VERSION,'partition':esp32.Partition(esp32.Partition.RUNNING).info()[4],'provisioned':credential_store.is_provisioned(),'receipt':_receipt,'application_status':_state.get('status'),'application_version':_state.get('version'),'has_application':_state.get('has_application'),'selected_paths':_state.get('selected_paths',[])}))`;

export function validateRecoveryReceipt(result, expected) {
  if (!result || result.status !== 'ready' || result.core_version !== expected.coreVersion || result.application_sha256 !== expected.applicationSHA256 || result.application_version !== expected.applicationVersion) {
    throw new Error('New core did not confirm the selected application staging. Inspect the device result; do not erase or factory-flash this secured board.');
  }
}

export async function confirmRecoveryAfterReset(repl, expected, progress, timeout = 180000) {
  const deadline = Date.now() + timeout;
  let receipt;
  try {
    try { await repl.until('USB-RECOVERY-RESULT ', Math.max(1, deadline-Date.now())); }
    catch (error) {
      if (error.code !== 'UART_DISCONNECTED') throw error;
      progress(7, 0, 'USB reset disconnected the console. Reconnecting the selected UART; no erase or upload will be repeated.');
      await repl.reopenAfterReset();
      progress(7, 0, 'UART reconnected. Waiting for boot validation (up to three minutes); a missed message will be checked against the saved device result.');
      await repl.until('USB-RECOVERY-RESULT ', Math.max(1, deadline-Date.now()));
    }
    receipt = JSON.parse(await repl.until('\r\n', 15000));
  } catch (error) {
    if (!['UART_DISCONNECTED','UART_TIMEOUT'].includes(error.code)) throw error;
    // Allow the complete boot-validation budget before Ctrl-C. The receipt
    // may have been emitted while USB was absent; it is also stored durably.
    const remaining = deadline-Date.now();
    if (remaining > 0) await delay(remaining);
    progress(7, 0, 'Boot message was missed. Checking the saved device result, without repeating recovery. Setup will restart after this check.');
    if (repl.readError || repl.isOpen === false) await repl.reopenAfterReset();
    await repl.enter();
    let restartSetup = false;
    try {
      const saved = JSON.parse(await repl.exec(recoveryResultProbe));
      if (saved.device !== expected.device) throw new Error('The reconnected UART is not the original device. No further device commands will be sent.');
      restartSetup = saved.provisioned === false;
      if (!restartSetup) throw new Error('The device is already configured. Check its portal; first-run setup will not be restarted.');
      if (saved.core_version !== expected.coreVersion || saved.partition !== expected.partition || saved.application_status !== 'ready' || saved.application_version !== expected.applicationVersion || saved.has_application !== true || !saved.selected_paths?.includes('iotmd.py') || !saved.selected_paths?.includes('app_settings.json')) throw new Error('Saved recovery state does not match the selected core and staged application. Do not erase or factory-flash again.');
      receipt = saved.receipt;
      validateRecoveryReceipt(receipt, expected);
    } finally {
      // Only the original, unprovisioned device is restarted. This restores
      // the setup wizard interrupted by inspection, not the recovery writes.
      if (restartSetup) await repl.reboot();
    }
  }
  validateRecoveryReceipt(receipt, expected);
}

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
  const handoff = Number(core.manifest.release_sequence) >= 2802;
  if (!handoff && !resume) throw new Error('Single-reset recovery requires Alpha 97 or newer signed core. Use the new core bundle, or explicitly resume an existing Alpha 96 recovery.');
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
  if (handoff) {
    if (!resume) {
      await repl.exec('_target.set_boot()\n_wdt.feed()');
      progress(4, 0, 'Erasing configuration before transferring the application.');
      await repl.exec(`_password=ubinascii.unhexlify('${hex(encoder.encode(password))}')\nimport credential_security\ncredential_security.validate_password_strength(_password.decode())`);
      if (await repl.exec(eraseRecoveryState, 120000) !== 'recovery-ready') throw new Error('Configuration reset acknowledgement was not received. Inspect the device before retrying.');
    }
    progress(4, 100, resume ? 'Retaining unprovisioned setup; no configuration erasure.' : 'Configuration erasure confirmed.');
    progress(5, 0, 'Uploading application before reset. Keep USB connected.');
    await repl.exec("_file=open('.app-update.bundle.upload','wb')");
    for (let offset = 0; offset < application.bytes.length; offset += 512) {
      await repl.exec(`_file.write(ubinascii.unhexlify('${hex(application.bytes.slice(offset, offset + 512))}'))\n_wdt.feed()`);
      progress(5, Math.min(99, Math.floor((offset + 512) / application.bytes.length * 100)), 'Uploading application before reset.');
    }
    const transferred = await repl.exec("_file.close()\n_hash=uhashlib.sha256()\n_file=open('.app-update.bundle.upload','rb')\nwhile True:\n _chunk=_file.read(4096)\n if not _chunk:break\n _hash.update(_chunk)\n _wdt.feed()\n_file.close()\nprint(ubinascii.hexlify(_hash.digest()).decode())", 120000);
    if (transferred !== application.sha256) throw new Error('Application readback verification failed; no recovery handoff committed.');
    progress(5, 100, 'Application transfer verified.');
    const marker = {format_version:1, core_version:core.manifest.version, partition:resume ? undefined : board.target, application_size:application.bytes.length, application_sha256:application.sha256};
    await repl.exec(`import uos as os\n_marker=json.loads(${pyString(JSON.stringify(marker))})\n${resume ? "_marker['partition']=_running.info()[4]\n" : ''}try:os.remove('.usb-recovery.json')\nexcept OSError:pass\ntry:os.remove('.usb-recovery-result.json')\nexcept OSError:pass\ntry:os.remove('.app-update.bundle')\nexcept OSError:pass\nos.rename('.app-update.bundle.upload','.app-update.bundle')\n_file=open('.usb-recovery.json.tmp','w')\njson.dump(_marker,_file)\n_file.close()\nos.rename('.usb-recovery.json.tmp','.usb-recovery.json')\nprint('handoff-ready')`).then(result => {if (result !== 'handoff-ready') throw new Error('Recovery handoff acknowledgement missing.');});
    progress(6, 100, 'Core and application transferred; boot validation pending.');
    progress(7, 0, 'Resetting once; waiting for core-owned application validation (up to three minutes).');
    await repl.reboot();
    // Never send Ctrl-C while the frozen core is validating/staging the bundle.
    // First wait passively for its durable-result marker, then inspect receipt.
    await confirmRecoveryAfterReset(repl, {device:board.device, partition:resume?board.running:board.target, coreVersion:core.manifest.version, applicationVersion:application.manifest.version, applicationSHA256:application.sha256}, progress);
    progress(8, 100, 'New core verified and application staging confirmed. Setup hotspot is not independently confirmed. Use the retained password file.');
    return board.device;
  }
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
  await repl.exec(`import esp32, credential_store, core_metadata, app_update, ubinascii, machine\nassert core_metadata.CORE_FIRMWARE_VERSION==${pyString(core.manifest.version)}\nassert not credential_store.is_provisioned()\nassert len(credential_store.bootstrap_key())==${password.length}\nesp32.Partition.mark_app_valid_cancel_rollback()\n_wdt=machine.WDT(0,timeout=${RECOVERY_WATCHDOG_MS})\n_file=open('.app-update.bundle','wb')`);
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

export function configureRecoveryMode(form, resume) {
  form.elements.resume.value = resume ? '1' : '0';
  const section = form.closest('section');
  section.querySelector('h2').textContent = resume ? 'Resume interrupted recovery' : 'Clean USB recovery';
  section.querySelector('.section-head p').textContent = resume
    ? 'Finish staging after an interrupted recovery, preserving the running core and existing setup state.'
    : 'Return a secured IoT-MD to first-run setup, preserving its hardware security keys.';
  section.querySelector('#usb-recovery-resume-notice').classList.toggle('hidden', !resume);
  section.querySelector('#usb-recovery-approval').textContent = resume
    ? 'I approve resuming application staging only, without a core write or configuration erase.'
    : 'I approve clean recovery, which erases all user configuration, credentials, certificates and logs.';
  form.querySelector('button').textContent = resume ? 'Resume application staging' : 'Recover device';
}

export class RecoveryActivityGuard {
  constructor(browser, page, message) {
    this.browser = browser; this.page = page; this.message = message;
    this.active = false; this.backgrounded = false; this.wakeLock = null;
    this.changed = () => { this.update().catch(() => {}); };
  }
  start() { this.active = true; this.page.addEventListener('visibilitychange', this.changed); this.changed(); }
  async update() {
    if (!this.active) return;
    if (this.page.hidden) {
      this.backgrounded = true;
      this.message('USB recovery is running in the background. Keep this workspace visible and the computer awake; sleep can disconnect USB.');
      const lock = this.wakeLock; this.wakeLock = null;
      await lock?.release().catch(() => {});
      return;
    }
    if (this.wakeLock?.released) this.wakeLock = null;
    if (!this.wakeLock && this.browser.wakeLock) {
      try {
        const lock = await this.browser.wakeLock.request('screen');
        if (!this.active || this.page.hidden || this.wakeLock) await lock.release();
        else this.wakeLock = lock;
      } catch (_) { /* Unsupported or denied wake lock must not block USB. */ }
    }
    if (this.active && !this.page.hidden) this.message(this.wakeLock
      ? 'USB recovery is running. Screen sleep prevention is active while this workspace is visible; keep USB connected.'
      : 'USB recovery is running. Keep this workspace visible, prevent computer sleep and keep USB connected.');
  }
  async stop() {
    this.active = false; this.page.removeEventListener('visibilitychange', this.changed);
    const lock = this.wakeLock; this.wakeLock = null;
    await lock?.release().catch(() => {});
  }
}

export function recoveryFailure(error, job, backgrounded = false) {
  if (!['UART_DISCONNECTED','UART_TIMEOUT'].includes(error.code)) return {status:'failed', detail:error.message};
  let detail;
  if (job.stage <= 2) detail = 'USB transfer interrupted before configuration reset. This recovery has not erased user configuration; the inactive core write may be incomplete. Check the board before restarting clean recovery.';
  else if (job.stage === 3) detail = 'USB transfer interrupted while verifying or preparing the core. Configuration erasure has not started. The boot target may have changed; inspect the board before retrying.';
  else if (job.stage === 4) detail = 'USB transfer interrupted during configuration reset. Erasure may be partially complete; its outcome is unknown. Inspect the board before retrying.';
  else if (job.stage < 7) detail = 'USB transfer interrupted after setup reset. Application staging may be incomplete. Keep power connected and inspect the board before using non-erasing resume.';
  else detail = 'Recovery handoff or restart confirmation was interrupted. Recovery may already be complete. Keep power connected and verify the saved device result; do not repeat erasure.';
  detail += ' Do not factory-flash this secured board.';
  if (backgrounded) detail += ' This workspace was backgrounded; prevent computer sleep and keep it visible when retrying.';
  return {status:'interrupted', detail};
}

function initializeRecovery() {
  const form = document.getElementById('usb-recovery-form');
  if (!form) return;
  const support = document.getElementById('usb-recovery-support'), status = document.getElementById('usb-recovery-status');
  const runtime = document.getElementById('usb-recovery-runtime');
  const button = form.querySelector('button'), fieldset = form.querySelector('fieldset');
  const policy = document.permissionsPolicy || document.featurePolicy;
  const availability = usbAvailability(navigator, window.isSecureContext && location.protocol === 'https:', !policy || policy.allowsFeature('serial'), window.top !== window);
  support.textContent = availability.message.replaceAll('seeding', 'USB recovery');
  support.className = availability.ready ? 'status' : 'status error';
  fieldset.disabled = !availability.ready;
  const workspace = document.getElementById('usb-recovery-workspace');
  workspace.classList.toggle('hidden', !availability.workspace);
  const resumeMode = new URLSearchParams(location.search).get('resume') === '1';
  configureRecoveryMode(form, resumeMode);
  let running = false;
  let activity;
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
    activity = new RecoveryActivityGuard(navigator, document, message => {
      support.textContent = message;
      runtime.textContent = message; runtime.classList.remove('hidden');
    });
    activity.start();
    status.textContent = 'Choose the device’s UART interface. Firmware must be running; do not hold BOOT.';
    try {
      const port = await navigator.serial.requestPort();
      const coreFile = form.elements.core.files[0], appFile = form.elements.application.files[0];
      const core = await readRecoveryBundle(new Uint8Array(await coreFile.arrayBuffer()), coreFile.name, 'iotcore');
      const application = await readRecoveryBundle(new Uint8Array(await appFile.arrayBuffer()), appFile.name, 'iotapp');
      const password = (await form.elements.password_file.files[0].text()).trim();
      if (!/^[\x21-\x7e]{16,63}$/.test(password) || !/[A-Z]/.test(password) || !/[a-z]/.test(password) || !/[0-9]/.test(password)) throw new Error('Choose the retained strong setup-password file (16–63 printable characters).');
      const resume = form.elements.resume.value === '1';
      job = await window.api('api/seed', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({kind:'recovery', milestone_count:8, handoff_version:Number(core.manifest.release_sequence)>=2802?1:0, resume, image:core.filename, application:application.filename, sha256:core.sha256, application_sha256:application.sha256, confirmation:form.elements.confirmation.value, credential_retained:form.elements.credential_retained.checked, erase_confirmed:form.elements.erase_confirmed.checked})});
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
      configureRecoveryMode(form, resumeMode);
    } catch (error) {
      // Serial errors are safe; device exceptions are deliberately sanitized by RecoveryREPL.
      status.className = 'status error'; status.textContent = error.message;
      if (error.name === 'SecurityError' && availability.ready && window.top !== window) workspace.classList.remove('hidden');
      if (job) {
        Object.assign(job, recoveryFailure(error, job, activity.backgrounded));
        status.textContent = job.detail;
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
      await activity.stop(); support.textContent = availability.message.replaceAll('seeding', 'USB recovery');
      runtime.classList.add('hidden'); runtime.textContent = '';
      running = false; button.disabled = false; window.localSeedJob = null;
      form.elements.confirmation.value = ''; window.refreshSeed();
    }
  });
}

if (typeof document !== 'undefined') initializeRecovery();
