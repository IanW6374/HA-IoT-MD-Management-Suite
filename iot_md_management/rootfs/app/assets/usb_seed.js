import { ESPLoader, Transport } from './vendor/esptool-js-0.7.0.js';

export function validateFactoryImage(bytes, filename) {
  if (!filename.endsWith('.factory.bin')) throw new Error('Choose an IoT-MD .factory.bin image.');
  if (bytes.length < 0x20000 || bytes.length > 16 * 1024 * 1024) throw new Error('Factory image size is invalid.');
  if (bytes.length % 4) throw new Error('Factory image is not aligned correctly.');
  if (bytes[0] !== 0xe9 || (bytes[12] | bytes[13] << 8) !== 9) throw new Error('Factory image does not contain an ESP32-S3 bootloader.');
  if (bytes[0x8000] !== 0xaa || bytes[0x8001] !== 0x50) throw new Error('Factory image is missing its partition table.');
}

export function validateBlankBoard(security) {
  if (security?.chipId !== 9 || !security.parsedFlags ||
      typeof security.parsedFlags.SECURE_BOOT_EN !== 'boolean' ||
      typeof security.parsedFlags.SECURE_DOWNLOAD_ENABLE !== 'boolean' ||
      security.parsedFlags.SECURE_BOOT_EN || security.parsedFlags.SECURE_DOWNLOAD_ENABLE ||
      security.flashCryptCnt !== 0 || !Array.isArray(security.keyPurposes) ||
      security.keyPurposes.length < 6 || security.keyPurposes.some(value => value !== 0)) {
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
  if (!window.isSecureContext) {
    support.textContent = 'Open Home Assistant over HTTPS to use browser USB.';
    button.disabled = true;
  } else if (!navigator.serial) {
    support.textContent = 'Browser USB requires Web Serial. Open this workspace in Chrome or Edge; Safari does not support it.';
    button.disabled = true;
  } else if (!allowed) {
    support.textContent = 'USB access is blocked in the embedded page. Use Open USB workspace to continue in a separate tab.';
    button.disabled = true;
  } else {
    support.textContent = 'USB connects directly to this computer. Keep this tab open throughout seeding.';
  }
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
      status.textContent = error.name === 'SecurityError' ? 'USB permission was blocked. Open the USB workspace in a separate tab.' : error.message;
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
