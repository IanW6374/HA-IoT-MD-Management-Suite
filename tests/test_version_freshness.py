"""Installed versions remain independent of deployment and cached health state."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
import fleet_service
from fleet_repository import FleetRepository
from fleet_service import DeviceClient, FleetController


class VersionFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = 9000.
        self.path = Path(self.directory.name) / 'fleet.db'
        self.store = FleetRepository(self.path, now=lambda: int(self.clock))
        self.addCleanup(self.store.close)
        self.identifier = self.store.register({'id': 'test', 'host': 'test.local'})['id']
        self.old = {'device_id': 'device-two', 'release_sequence': 2824,
                    'firmware_release_sequence': 2824, 'application_version': 'alpha119',
                    'firmware_version': 'alpha119', 'runtime': {'retained': True}}
        self.store.record_poll(self.identifier, {'device': self.old, 'configuration': {'retained': True}},
                               {'health': {'cached': True}}, {'cursor': 7, 'events': []})
        self.deployment = self.store.create_deployment({'update': {
            'release_sequence': 2825, 'release_type': 'universal', 'version': 'alpha120'},
            'activation': 'now'}, [self.identifier])
        self.store.set_deployment_target(self.deployment['id'], self.identifier, 'cancelling')
        self.clock = 10000.
        self.controller = FleetController(self.store, None, timeout=10, progress_timeout=5,
                                          now=lambda: int(self.clock))
        self.observed = {'device_id': 'device-two', 'release_sequence': 2826,
                         'firmware_release_sequence': 2826}
        self.labels = dict(self.observed, application_version='alpha121', firmware_version='alpha121',
                           runtime={'must_not_replace': True}, secret='must_not_store')
        self.paths, self.budgets = [], []
        self.delay = 0
        self.cancellation = {'status': 'idle'}
        self.patch = mock.patch.object(DeviceClient, '_context', return_value=object())
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.timer = mock.patch.object(fleet_service.time, 'monotonic', side_effect=lambda: self.clock)
        self.timer.start(); self.addCleanup(self.timer.stop)
        self.transport = mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=self.read)
        self.transport.start(); self.addCleanup(self.transport.stop)

    def read(self, request, **kwargs):
        self.assertEqual(request.method, 'GET')
        path = request.full_url.split(':8444')[-1]
        self.paths.append(path); self.budgets.append(kwargs['timeout'])
        if path == '/api/v3':
            value = {'capabilities': {'live_update_status': True}}
        elif path == '/api/v3/fleet/update-status':
            value = {'boot_id': 'new-boot', 'device': self.observed, 'fleet': {},
                     'cancellation': self.cancellation}
        elif path == '/api/v3/device':
            self.clock += min(self.delay, kwargs['timeout'])
            if self.delay > kwargs['timeout']:
                raise TimeoutError('Version read timed out')
            value = {'device': self.labels}
        else:
            self.fail('Unexpected read: ' + path)
        return io.BytesIO(json.dumps(dict(value, api_version=3)).encode())

    def test_live_sequence_change_refreshes_labels_but_not_health_or_old_job(self):
        self.controller.poll_progress(self.identifier)
        record = self.store.get_device(self.identifier)
        self.assertEqual(record['inventory']['device']['application_version'], 'alpha121')
        self.assertEqual(record['inventory']['device']['firmware_version'], 'alpha121')
        self.assertEqual(record['inventory']['device']['runtime'], {'retained': True})
        self.assertNotIn('secret', record['inventory']['device'])
        self.assertEqual(record['inventory']['configuration'], {'retained': True})
        self.assertEqual(record['health'], {'health': {'cached': True}})
        self.assertEqual(record['last_seen'], 9000)
        self.assertEqual(record['event_cursor'], 7)
        self.assertEqual(self.store.get_deployment(self.deployment['id'])['status'], 'active')
        self.assertEqual(self.store.get_deployment(self.deployment['id'])['results'][self.identifier]['status'], 'cancelling')
        self.assertEqual(self.paths, ['/api/v3', '/api/v3/fleet/update-status', '/api/v3/device'])
        self.assertEqual(record['poll_state']['versions']['received_at'], 10000)
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3/device'), 1)
        self.store.close()
        reopened = FleetRepository(self.path)
        try:
            self.assertEqual(reopened.get_device(self.identifier)['inventory']['device']['application_version'], 'alpha121')
        finally:
            reopened.close()

    def test_failed_optional_version_read_is_bounded_and_never_freezes_status(self):
        self.delay = 6
        self.controller.poll_progress(self.identifier)
        record = self.store.get_device(self.identifier)
        self.assertEqual(self.budgets[-1], 5)
        self.assertEqual(record['inventory']['device']['application_version'], '')
        self.assertEqual(record['inventory']['device']['firmware_version'], '')
        self.assertIn('timed out', record['poll_state']['versions']['error'])
        self.assertEqual(self.controller.progress_diagnostics()[self.identifier]['last_received'], 10000)
        self.assertEqual(self.controller.progress_diagnostics()[self.identifier]['error'], '')
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3/device'), 1)
        self.clock += 31; self.delay = 0
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3/device'), 2)
        self.assertEqual(self.store.get_device(self.identifier)['inventory']['device']['application_version'], 'alpha121')

    def test_wrong_identity_sequence_or_invalid_labels_never_replace_current_labels(self):
        for changes in ({'device_id': 'other'}, {'release_sequence': 2825},
                        {'release_sequence': 2826.0}, {'application_version': 'x'*97},
                        {'application_version': 'bad\nlabel'}, {'firmware_version': ''}):
            with self.subTest(changes=changes):
                self.clock += 31
                self.labels = {**self.observed, 'application_version': 'alpha121', 'firmware_version': 'alpha121', **changes}
                self.controller.poll_progress(self.identifier)
                record = self.store.get_device(self.identifier)
                self.assertEqual(record['inventory']['device']['application_version'], '')
                self.assertIn('does not match', record['poll_state']['versions']['error'])

    def test_failed_label_read_backoff_survives_controller_restart_but_new_pair_can_refresh(self):
        self.delay = 6
        self.controller.poll_progress(self.identifier)
        self.controller = FleetController(self.store, None, timeout=10, progress_timeout=5,
                                          now=lambda: int(self.clock))
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3/device'), 1)
        self.observed = dict(self.observed, release_sequence=2827, firmware_release_sequence=2827)
        self.labels = dict(self.observed, application_version='alpha122', firmware_version='alpha122')
        self.delay = 0
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3/device'), 2)
        self.assertEqual(self.store.get_device(self.identifier)['inventory']['device']['application_version'], 'alpha122')

    def test_foreign_device_version_evidence_cannot_replace_enrolled_inventory(self):
        self.store.record_device_versions(self.identifier, dict(self.labels, device_id='different-device'))
        self.assertEqual(self.store.get_device(self.identifier)['inventory']['device']['application_version'], 'alpha119')

    def test_matching_cancellation_ack_still_required_not_newer_installed_version(self):
        self.cancellation = {'status': 'cancelled', 'release_sequence': 2826, 'release_type': 'universal'}
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.store.get_deployment(self.deployment['id'])['results'][self.identifier]['status'], 'cancelling')
        self.cancellation['release_sequence'] = 2825
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.store.get_deployment(self.deployment['id'])['status'], 'cancelled')

    def test_repository_partial_merge_rejects_invalid_fields_and_does_not_write_unchanged_inventory(self):
        for bad in (True, '2826', 2826.0, -1, 9007199254740992):
            self.store.record_device_versions(self.identifier, {'device_id': 'device-two',
                'release_sequence': bad, 'application_version': 'wrong'})
            self.assertEqual(self.store.get_device(self.identifier)['inventory']['device']['application_version'], 'alpha119')
        before = self.store.connection.total_changes
        self.store.record_device_versions(self.identifier, self.old)
        self.assertEqual(self.store.connection.total_changes, before)

    def test_partial_full_poll_keeps_fresh_versions_after_configuration_failure(self):
        client = mock.Mock()
        client.request.side_effect = [dict(device=self.labels), TimeoutError('Configuration timed out')]
        self.controller._client = mock.Mock(return_value=client)
        record = self.controller.poll_device(self.identifier)
        self.assertEqual(record['inventory']['device']['application_version'], 'alpha121')
        self.assertEqual(record['last_seen'], 9000)
        self.assertEqual(record['health'], {'health': {'cached': True}})
        self.assertEqual(record['poll_state']['refresh']['stage'], 'configuration')
        self.assertEqual(record['poll_state']['versions']['source'], '/api/v3/device/inventory')
