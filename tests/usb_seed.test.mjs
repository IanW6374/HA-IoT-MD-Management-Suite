import test from 'node:test';
import assert from 'node:assert/strict';
import {validateFactoryImage, validateBlankBoard, flashFactory} from '../iot_md_management/rootfs/app/assets/usb_seed.js';

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
