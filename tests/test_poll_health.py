"""Offline timeout and reachability regressions; no real device traffic."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from fleet_repository import FleetRepository
from fleet_service import DeviceClient, FleetController
import fleet_service


class PollHealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FleetRepository(Path(self.directory.name) / 'fleet.db', now=lambda: 2000)
        self.addCleanup(self.store.close)
        self.device = self.store.register({'id': 'mock.local', 'host': 'mock.local'})
        self.controller = FleetController(self.store, None, timeout=10, now=lambda: 2000)

    def test_ten_second_requests_do_not_share_one_ten_second_poll_budget(self):
        clock, requests = [1000.0], []
        def transport(request, **kwargs):
            path = request.full_url.split(':8444')[-1]
            requests.append((path, kwargs['timeout']))
            clock[0] += 3
            value = {'api_version': 3}
            if path.endswith('/inventory'):
                value['device'] = {'application_version': 'alpha116'}
            elif path.endswith('/configuration'):
                value['configuration'] = {}
            elif path.endswith('/health'):
                value['health'] = {}
            elif '/events?' in path:
                value.update(events=[], cursor=7)
            return io.BytesIO(json.dumps(value).encode())
        with mock.patch.object(DeviceClient, '_context', return_value=object()), \
                mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=transport), \
                mock.patch.object(fleet_service.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.controller.poll_device(self.device['id'])
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(timeout == 10 for _, timeout in requests))
        self.assertEqual(result['last_error'], '')
        self.assertEqual(result['last_seen'], 2000)
        self.assertEqual(result['event_cursor'], 7)
        self.assertEqual(result['poll_state']['api']['last_success'], 2000)
        self.assertEqual(result['poll_state']['refresh']['stage'], 'complete')

    def test_metadata_response_remains_evidence_after_inventory_failure(self):
        def transport(request, **kwargs):
            if request.full_url.endswith('/api/v3'):
                return io.BytesIO(b'{"api_version":3}')
            raise TimeoutError('The read operation timed out')
        with mock.patch.object(DeviceClient, '_context', return_value=object()), \
                mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=transport):
            result = self.controller.poll_device(self.device['id'])
        self.assertEqual(result['last_seen'], 0)
        self.assertEqual(result['poll_state']['api']['last_success'], 2000)
        self.assertEqual(result['poll_state']['api']['path'], '/api/v3/device/inventory')
        self.assertIn('timed out', result['poll_state']['api']['error'])
        self.assertEqual(result['poll_state']['refresh']['stage'], 'inventory')

    def test_first_discovery_failure_does_not_invent_reachability(self):
        with mock.patch.object(DeviceClient, '_context', return_value=object()), \
                mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=TimeoutError('unreachable')):
            result = self.controller.poll_device(self.device['id'])
        self.assertEqual(result['last_seen'], 0)
        self.assertNotIn('last_success', result['poll_state']['api'])
        self.assertEqual(result['poll_state']['api']['path'], '/api/v3')

    def test_live_read_clears_api_failure_without_refreshing_cached_health(self):
        def partial(path):
            if path.endswith('/inventory'):
                return {'device': {}}
            raise TimeoutError('Configuration stalled')
        self.controller._client = mock.Mock(return_value=mock.Mock(request=partial))
        first = self.controller.poll_device(self.device['id'])
        self.assertIn('stalled', first['poll_state']['api']['error'])
        self.controller._client = mock.Mock(return_value=mock.Mock(request=lambda path: {'device': {}}))
        self.controller.poll_progress(self.device['id'])
        result = self.store.get_device(self.device['id'])
        self.assertEqual(result['poll_state']['api']['error'], '')
        self.assertEqual(result['last_seen'], 0)
        self.assertEqual(result['health'], {})
        self.assertEqual(result['last_error'], 'Configuration stalled')
        self.assertEqual(result['poll_state']['refresh']['stage'], 'configuration')

    def test_poll_state_survives_restart_and_schema_four_upgrade(self):
        self.store.record_poll(self.device['id'], {'device': {'marker': 'retained'}}, {}, {})
        deployment = self.store.create_deployment({'update': {'release_sequence': 2821, 'release_type': 'universal'}}, [self.device['id']])
        job = self.store.enqueue_job('backup', self.device['id'], idempotency_key='retained-backup')
        self.store.set_metadata('next_policy_sequence', '199')
        self.store.connection.execute('ALTER TABLE devices DROP COLUMN poll_state')
        self.store.set_metadata('schema_version', '4')
        self.store.close()
        upgraded = FleetRepository(Path(self.directory.name) / 'fleet.db')
        self.addCleanup(upgraded.close)
        record = upgraded.get_device(self.device['id'])
        self.assertEqual(record['inventory']['device']['marker'], 'retained')
        self.assertEqual(record['last_seen'], 2000)
        self.assertEqual(record['poll_state'], {})
        self.assertEqual(upgraded.metadata('schema_version'), '6')
        self.assertEqual(upgraded.get_deployment(deployment['id'])['status'], 'queued')
        self.assertEqual(upgraded.get_job(job['id'])['status'], 'queued')
        self.assertEqual(upgraded.metadata('next_policy_sequence'), '199')
        upgraded.record_poll_state(record['id'], {'api': {'last_success': 2000, 'error': ''}})
        upgraded.close()
        reopened = FleetRepository(Path(self.directory.name) / 'fleet.db')
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_device(record['id'])['poll_state']['api']['last_success'], 2000)


if __name__ == '__main__':
    unittest.main()
