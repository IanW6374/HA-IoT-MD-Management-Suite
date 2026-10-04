import { ESPLoader, Transport } from './vendor/esptool-js-0.7.0.js';

export function browserName(browser) {
  const agent = browser.userAgent || '';
  if (/Edg(?:e|A|iOS)?\//.test(agent)) return 'Edge';
  if (/OPR\//.test(agent)) return 'Opera';
  if (/Chrome\/|CriOS\//.test(agent)) return 'Chrome';
  if (/Firefox\/|FxiOS\//.test(agent)) return 'Firefox';
  if (/Safari\//.test(agent)) return 'Safari';
  return 'This browser';
}

export function usbAvailability(browser, secure, allowed, embedded) {
  const name = browserName(browser);
  const ios = /iPhone|iPad|iPod|CriOS|EdgiOS|FxiOS/.test(browser.userAgent || '') ||
    (browser.platform === 'MacIntel' && browser.maxTouchPoints > 1);
  const hasSerial = !!browser.serial;
  const unsupported = !hasSerial && (secure || name === 'Safari' || ios);
  const issues = [];
  if (unsupported) issues.push(`${name}${ios ? ' on iOS' : ''} does not support browser USB (Web Serial). Use Chrome or Edge on a desktop computer.`);
  if (!secure) issues.push(`${issues.length ? 'Home Assistant also' : name + ' detected. Home Assistant'} needs to be opened over HTTPS for browser USB.`);
  if (issues.length) return {ready: false, workspace: false, message: issues.join(' ')};
  if (!allowed) return {ready: false, workspace: embedded,
    message: embedded ? `${name} detected. Home Assistant’s embedded panel blocks USB access. Open the seeding page in a separate tab to continue.` :
      `${name} detected. This page’s permissions policy blocks USB access.`};
  return {ready: true, workspace: false,
    message: `${name} detected. USB connects directly to this computer. Keep this tab open throughout seeding.`};
}

export function validateFactoryImage(bytes, filename) {
  if (!filename.endsWith('.factory.bin')) throw new Error('Choose an IoT-MD .factory.bin image.');
  if (bytes.length < 0x20000 || bytes.length > 16 * 1024 * 1024) throw new Error('Factory image size is invalid.');
  if (bytes.length % 4) throw new Error('Factory image is not aligned correctly.');
  if (bytes[0] !== 0xe9 || (bytes[12] | bytes[13] << 8) !== 9) throw new Error('Factory image does not contain an ESP32-S3 bootloader.');
  // IoT-MD reserves space for the signed secure-boot bootloader; its board
  // configuration places the partition table at 0x10000, not ESP-IDF's 0x8000 default.
  if (bytes[0x10000] !== 0xaa || bytes[0x10001] !== 0x50) throw new Error('Factory image is missing its partition table at 0x10000.');
}

export function validateBlankBoard(security) {
  // ESP32-S3 has BLOCK_KEY0 through BLOCK_KEY5. GET_SECURITY_INFO includes
  // a seventh byte that is not another S3 key slot (the ROM can return 12).
  if (security?.chipId !== 9 || !security.parsedFlags ||
      typeof security.parsedFlags.SECURE_BOOT_EN !== 'boolean' ||
      typeof security.parsedFlags.SECURE_DOWNLOAD_ENABLE !== 'boolean' ||
      security.parsedFlags.SECURE_BOOT_EN || security.parsedFlags.SECURE_DOWNLOAD_ENABLE ||
      security.flashCryptCnt !== 0 || !Array.isArray(security.keyPurposes) ||
      security.keyPurposes.length < 6 ||
      Array.from(security.keyPurposes.slice(0, 6)).some(value => value !== 0)) {
    throw new Error('The board is already secured or its security state could not be verified. Use secured-device recovery.');
  }
}

export async function flashFactory(loader, bytes, md5, progress) {
  await loader.detectChip();
  validateBlankBoard(await loader.getSecurityInfo(false));
  if (loader.chip.CHIP_NAME !== 'ESP32-S3') throw new Error('Only ESP32-S3 boards can be seeded.');
  if (loader.chip.postConnect) await loader.chip.postConnect(loader);
  await loader.runStub();
  const capacity = await loader.detectFlashSize();
  const capacityBytes = loader.flashSizeBytes(capacity);
  if (!capacity || capacityBytes < bytes.length) throw new Error('The factory image does not fit this board’s flash.');
  progress(2, 0, 'Erasing and writing the factory image. Keep USB connected.');
  const originalMd5 = loader.flashMd5sum.bind(loader);
  let verified = false;
  loader.flashMd5sum = async (address, size) => {
    progress(3, 0, 'Verifying the image before first boot.');
    const digest = await originalMd5(address, size);
    if (digest !== md5(bytes)) throw new Error('Factory image verification failed. Keep the board in bootloader mode.');
    verified = true;
    progress(3, 100, 'Factory image verified.');
    return digest;
  };
  try {
    // Preserve every byte of the signed image; changing flash header settings
    // would invalidate the factory bootloader's signature.
    await loader.writeFlash({fileArray: [{data: bytes, address: 0}],
      flashMode: 'keep', flashFreq: 'keep', flashSize: 'keep',
      eraseAll: true, compress: true, calculateMD5Hash: md5,
      reportProgress: (_file, written, total) => progress(2, Math.min(100, Math.floor(written / total * 100)), 'Writing the factory image. Keep USB connected.')});
    if (!verified) throw new Error('The device did not verify the written factory image.');
    progress(4, 0, 'Starting first boot and security initialization.');
    await loader.after('hard_reset');
    progress(5, 100, 'Factory image verified and first boot started. Allow security initialization to finish, then complete first-run setup using the matching setup password.');
  } finally {
    loader.flashMd5sum = originalMd5;
  }
}

function initialize() {
  const form = document.getElementById('seed-form');
  if (!form) return;
  const support = document.getElementById('seed-support'), button = form.querySelector('button');
  const policy = document.permissionsPolicy || document.featurePolicy;
  const allowed = !policy || policy.allowsFeature('serial');
  const https = window.location.protocol === 'https:' && window.isSecureContext;
  const availability = usbAvailability(navigator, https, allowed, window.top !== window);
  support.textContent = availability.message;
  support.className = availability.ready ? 'status' : 'status error';
  support.setAttribute('role', availability.ready ? 'status' : 'alert');
  button.disabled = !availability.ready;
  form.querySelector('#seed-settings').disabled = !availability.ready;
  form.setAttribute('aria-disabled', String(!availability.ready));
  const workspace = document.getElementById('seed-workspace-link');
  workspace.classList.toggle('hidden', !availability.workspace);
  let running = false;
  async function retryCompletion() {
    try {
      const value = sessionStorage.getItem('iot-md-seed-completion');
      if (!value) return;
      const result = JSON.parse(value);
      await window.api('api/seed/' + result.id, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: value});
      sessionStorage.removeItem('iot-md-seed-completion');
      window.refreshSeed();
    } catch (_) { /* Retain only result metadata until Management is reachable. */ }
  }
  retryCompletion();
  setInterval(retryCompletion, 5000);
  window.addEventListener('beforeunload', event => {
    if (running) { event.preventDefault(); event.returnValue = ''; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (running || button.disabled) return;
    const status = document.getElementById('seed-status');
    let transport, job, heartbeat, pending = Promise.resolve();
    const post = (path, body) => window.api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const publish = () => {
      if (!job) return;
      const snapshot = {...job};
      pending = pending.catch(() => {}).then(() => post('api/seed/' + snapshot.id, snapshot));
      return pending;
    };
    const progress = (stage, percent, detail) => {
      if (stage === job.stage && percent < job.percent) return;
      job = {...job, stage, percent, detail};
      window.localSeedJob = job;
      const jobs = window.currentSeedJobs || [];
      window.applySeedState({jobs: [...jobs.filter(item => item.id !== job.id), job]});
      window.renderDeployments();
    };
    button.disabled = true;
    running = true;
    status.className = 'status';
    status.textContent = 'Choose the connected USB board…';
    try {
      // requestPort must run directly from the user's click, before async reads.
      const port = await navigator.serial.requestPort();
      const file = form.elements.image.files[0];
      const bytes = new Uint8Array(await file.arrayBuffer());
      validateFactoryImage(bytes, file.name);
      const sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('');
      job = await post('api/seed', {image: file.name, sha256, confirmation: form.elements.confirmation.value,
                                  credential_retained: form.elements.credential_retained.checked});
      window.setActionView('inflight', true);
      status.textContent = '';
      transport = new Transport(port, true);
      const loader = new ESPLoader({transport, baudrate: 115200,
        terminal: {clean() {}, write() {}, writeLine() {}}, debugLogging: false});
      heartbeat = setInterval(() => { publish()?.catch(() => {}); }, 2000);
      await flashFactory(loader, bytes, data => window.SparkMD5.ArrayBuffer.hash(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength)), progress);
      job.status = 'complete';
      try { sessionStorage.setItem('iot-md-seed-completion', JSON.stringify(job)); } catch (_) {}
      await publish().then(() => { sessionStorage.removeItem('iot-md-seed-completion'); }).catch(() => {});
      const outcome = document.getElementById('seed-outcome');
      outcome.classList.remove('hidden');
      outcome.textContent = job.detail;
      form.reset();
    } catch (error) {
      status.className = 'status error';
      const canOpenTab = error.name === 'SecurityError' && window.top !== window && https && !!navigator.serial;
      if (canOpenTab) {
        workspace.classList.remove('hidden');
        form.querySelector('#seed-settings').disabled = true;
        form.setAttribute('aria-disabled', 'true');
      }
      status.textContent = canOpenTab ? 'USB permission was blocked. Open the seeding page in a separate tab to continue.' : error.message;
      if (job) {
        job.status = 'failed'; job.detail = error.message;
        await publish()?.catch(() => {});
        // Show the failure where the operator is watching, before moving it to Activity.
        window.applySeedState({jobs: [job]});
        window.setActionView('new', true);
        form.querySelector('[name="confirmation"]').value = '';
        const radio = document.querySelector('[name="action_mode"][value="seed"]');
        radio.checked = true;
        window.syncActionMode(true);
      }
    } finally {
      clearInterval(heartbeat);
      if (transport) await transport.disconnect().catch(() => {});
      running = false;
      window.localSeedJob = null;
      button.disabled = false;
      if (job) window.refreshSeed();
    }
  });
}

if (typeof document !== 'undefined') initialize();
