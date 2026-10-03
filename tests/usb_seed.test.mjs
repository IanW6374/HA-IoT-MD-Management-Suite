import test from 'node:test';
import assert from 'node:assert/strict';
import {validateFactoryImage, validateBlankBoard, flashFactory, browserName, usbAvailability} from '../iot_md_management/rootfs/app/assets/usb_seed.js';

const safari = {userAgent: 'Mozilla/5.0 (Macintosh) Version/26.0 Safari/605.1.15'};
const chrome = {userAgent: 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36'};
test('Safari on HTTP shows browser and HTTPS requirements together', () => {
  const value = usbAvailability(safari, false, true, true);
  assert.match(value.message, /Safari does not support/);
  assert.match(value.message, /also.*HTTPS/);
  assert.equal(value.ready, false);
  assert.equal(value.workspace, false);
});
test('HTTP must not misclassify Chrome as an unsupported browser', () => {
  const value = usbAvailability(chrome, false, true, true);
  assert.match(value.message, /Chrome detected/);
  assert.match(value.message, /HTTPS/);
  assert.doesNotMatch(value.message, /does not support/);
  assert.equal(value.workspace, false);
});
test('new tab is offered only for a supported secure embedded page with blocked permissions', () => {
  const browser = {...chrome, serial: {}};
  assert.equal(usbAvailability(browser, true, false, true).workspace, true);
  assert.equal(usbAvailability(browser, true, false, false).workspace, false);
  assert.equal(usbAvailability(browser, true, true, true).workspace, false);
  assert.equal(usbAvailability(safari, true, false, true).workspace, false);
  assert.equal(usbAvailability(browser, false, false, true).workspace, false);
});
test('actual Web Serial support takes precedence over a browser name', () => {
  assert.equal(usbAvailability({...safari, serial: {}}, true, true, false).ready, true);
  assert.equal(browserName({userAgent: 'Chrome/140.0 Safari/537.36 Edg/140.0'}), 'Edge');
  assert.match(usbAvailability({userAgent: 'CriOS/140.0 Safari/605.1.15'}, true, true, true).message, /Chrome on iOS/);
});

const blank = () => ({chipId: 9, flashCryptCnt: 0, keyPurposes: [0,0,0,0,0,0,0],
  parsedFlags: {SECURE_BOOT_EN: false, SECURE_DOWNLOAD_ENABLE: false}});
function board(security = blank(), digest = 'correct') {
  const calls = [], loader = {
    chip: {CHIP_NAME: 'ESP32-S3'},
    async detectChip() {calls.push('detect');},
    async getSecurityInfo() {calls.push('security'); return security;},
    async runStub() {calls.push('stub');},
    async detectFlashSize() {return '16MB';},
    flashSizeBytes() {return 16*1024*1024;},
    async flashMd5sum() {calls.push('verify'); return digest;},
    async writeFlash(options) {
      calls.push('write');
      assert.equal(options.flashMode, 'keep');
      assert.equal(options.flashFreq, 'keep');
      assert.equal(options.flashSize, 'keep');
      assert.equal(options.eraseAll, true);
      options.reportProgress(0, 100, 100);
      assert.equal(await this.flashMd5sum(0, options.fileArray[0].data.length), options.calculateMD5Hash(options.fileArray[0].data));
    },
    async after() {calls.push('reset');}
  };
  return {loader, calls};
}
test('factory validation rejects update files and wrong chips', () => {
  const bytes = new Uint8Array(0x20000);
  bytes[0]=0xe9; bytes[12]=9; bytes[0x8000]=0xaa; bytes[0x8001]=0x50;
  validateFactoryImage(bytes, 'new.factory.bin');
  assert.throws(() => validateFactoryImage(bytes, 'new.iotuni'));
  bytes[12]=0;
  assert.throws(() => validateFactoryImage(bytes, 'new.factory.bin'));
});
test('secured or unreadable boards are refused before erase or flash', async () => {
  for (const change of [info=>info.flashCryptCnt=2, info=>info.keyPurposes[0]=9,
    info=>info.parsedFlags.SECURE_BOOT_EN=true, info=>info.parsedFlags.SECURE_DOWNLOAD_ENABLE=true,
    info=>delete info.parsedFlags, info=>info.chipId=0]) {
    const security = blank(); change(security);
    assert.throws(() => validateBlankBoard(security));
    const {loader,calls} = board(security);
    await assert.rejects(flashFactory(loader, new Uint8Array(4), ()=>'correct', ()=>{}));
    assert.deepEqual(calls, ['detect','security']);
  }
});
test('verified image resets only after digest matches, with ordered progress', async () => {
  const {loader,calls} = board(), stages=[];
  await flashFactory(loader, new Uint8Array(4), ()=>'correct', stage=>stages.push(stage));
  assert.deepEqual(calls, ['detect','security','stub','write','verify','reset']);
  assert.deepEqual(stages, [2,2,3,3,4,5]);
});
test('verification failure never starts first boot', async () => {
  const {loader,calls} = board(blank(), 'wrong');
  await assert.rejects(flashFactory(loader, new Uint8Array(4), ()=>'correct', ()=>{}), /verification failed/);
  assert.ok(!calls.includes('reset'));
});
test('too small flash fails before write', async () => {
  const {loader,calls} = board(); loader.flashSizeBytes=()=>1;
  await assert.rejects(flashFactory(loader, new Uint8Array(4), ()=>'correct', ()=>{}), /does not fit/);
  assert.ok(!calls.includes('write'));
});
