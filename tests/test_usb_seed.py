import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from fleet_repository import FleetRepository
from usb_seed import USBSeedManager


class USBSeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FleetRepository(Path(self.directory.name) / 'fleet.db')
        self.time = 1000
        self.manager = USBSeedManager(self.store, now=lambda: self.time)

    def tearDown(self):
        self.store.connection.close()
        self.directory.cleanup()

    def start(self):
        return self.manager.start({'image': 'iot-md.factory.bin', 'sha256': 'a' * 64,
                                   'confirmation': 'SEED', 'credential_retained': True})

    def test_progress_cannot_backtrack_and_terminal_result_survives_restart(self):
        job = self.start()
        self.manager.update(job['id'], {'stage': 2, 'percent': 80})
        result = self.manager.update(job['id'], {'stage': 2, 'percent': 30})
        self.assertEqual(result['percent'], 80)
        with self.assertRaises(ValueError):
            self.manager.update(job['id'], {'stage': 1})
        with self.assertRaises(ValueError):
            self.manager.update(job['id'], {'stage': 2, 'status': 'complete'})
        self.manager.update(job['id'], {'stage': 5, 'percent': 100, 'status': 'complete'})
        result = self.manager.update(job['id'], {'stage': 2, 'status': 'running'})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(USBSeedManager(self.store).snapshot()['jobs'][0]['status'], 'complete')
        self.assertEqual(len(self.store.list_audit()), 2)

    def test_lost_browser_is_unknown_then_late_completion_can_resolve_it(self):
        job = self.start()
        self.time += 181
        self.assertEqual(self.manager.snapshot()['jobs'][0]['status'], 'interrupted')
        self.assertIn('unknown', self.manager.snapshot()['jobs'][0]['detail'])
        result = self.manager.update(job['id'], {'stage': 5, 'status': 'complete'})
        self.assertEqual(result['status'], 'complete')

    def test_password_and_image_bytes_are_not_retained(self):
        self.manager.start({'image': 'iot-md.factory.bin', 'sha256': 'a' * 64,
                            'confirmation': 'SEED', 'credential_retained': True,
                            'password': 'secret-that-must-not-be-stored', 'bytes': 'factory-content'})
        stored = self.store.metadata('usb_seed_jobs')
        self.assertNotIn('secret-that-must-not-be-stored', stored)
        self.assertNotIn('factory-content', stored)
        with self.assertRaises(ValueError):
            self.manager.start({'image': 'update.iotuni', 'sha256': 'a' * 64,
                                'confirmation': 'SEED', 'credential_retained': True})

    def test_flash_completion_never_claims_startup_confirmation(self):
        job = self.start()
        self.assertFalse(job['startup_confirmed'])
        result = self.manager.update(job['id'], {
            'stage': 5, 'percent': 100, 'status': 'complete',
            'startup_confirmed': True,
        })
        self.assertFalse(result['startup_confirmed'])
        self.assertFalse(USBSeedManager(self.store).snapshot()['jobs'][0]['startup_confirmed'])

    def test_legacy_completion_does_not_imply_startup_confirmation(self):
        self.store.set_metadata('usb_seed_jobs', '[{"id":"legacy","status":"complete"}]')
        result = USBSeedManager(self.store).snapshot()['jobs'][0]
        self.assertFalse(result['startup_confirmed'])

    def test_secured_recovery_requires_bundles_and_explicit_erasure(self):
        request = {'kind': 'recovery', 'image': 'core.iotcore', 'sha256': 'a' * 64,
                   'application': 'app.iotapp', 'application_sha256': 'b' * 64,
                   'confirmation': 'RECOVER', 'credential_retained': True,
                   'erase_confirmed': True, 'password': 'not-retained'}
        for changes in ({'erase_confirmed': False}, {'confirmation': 'SEED'},
                        {'application': 'factory.bin'}, {'image': 'core.factory.bin'},
                        {'application_sha256': ''}):
            with self.assertRaises(ValueError):
                self.manager.start(dict(request, **changes))
        job = self.manager.start(request)
        self.assertEqual(job['kind'], 'recovery')
        self.assertNotIn('not-retained', self.store.metadata('usb_seed_jobs'))
        with self.assertRaises(ValueError):
            self.manager.update(job['id'], {'stage': 5, 'status': 'complete'})
        result = self.manager.update(job['id'], {'stage': 7, 'status': 'complete'})
        self.assertFalse(result['startup_confirmed'])
        self.assertEqual(result['status'], 'complete')
        self.assertIn('device.usb_recovery', str(self.store.list_audit()))

    def test_recovery_restart_has_its_own_milestone_with_legacy_jobs_preserved(self):
        request = {'kind': 'recovery', 'image': 'core.iotcore', 'sha256': 'a' * 64,
                   'application': 'app.iotapp', 'application_sha256': 'b' * 64,
                   'confirmation': 'RECOVER', 'credential_retained': True,
                   'erase_confirmed': True, 'milestone_count': 8, 'resume': True}
        job = self.manager.start(request)
        self.assertEqual(job['milestone_count'], 8)
        self.assertTrue(job['resume'])
        self.manager.update(job['id'], {'stage': 4, 'percent': 100})
        with self.assertRaises(ValueError):
            self.manager.update(job['id'], {'stage': 7, 'status': 'complete'})
        result = self.manager.update(job['id'], {'stage': 8, 'status': 'complete'})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(USBSeedManager(self.store).snapshot()['jobs'][0]['milestone_count'], 8)
