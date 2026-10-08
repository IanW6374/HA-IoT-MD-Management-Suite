"""Offline checks for the deliberately breaking, v3-only device client."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.error
import tempfile
import sys

SOURCE = Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app/fleet_service.py'
SPEC = importlib.util.spec_from_file_location('v3_fleet_service', SOURCE)
SERVICE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVICE)


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class DeviceAPIv3ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = SERVICE.DeviceClient({'host': 'iot-md-001.local', 'port': 8444}, sequence_provider=lambda minimum: minimum)
        self.context = mock.patch.object(self.client, '_context', return_value=object())
        self.context.start()
        self.addCleanup(self.context.stop)

    def test_discovery_before_write_and_cached_for_subsequent_requests(self):
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', side_effect=[
            response({'api_version': 3, 'capabilities': {'persistent_idempotency': True}, 'next_request_sequence': 1}),
            response({'api_version': 3, 'accepted': True}),
            response({'api_version': 3, 'health': {}}),
        ]) as transport:
            self.client.request('/api/v3/configuration/profile', 'POST', {'settings': {}})
            self.client.request('/api/v3/health')
        requests = [call.args[0] for call in transport.call_args_list]
        self.assertEqual(requests[0].method, 'GET')
        self.assertTrue(requests[0].full_url.endswith('/api/v3'))
        self.assertEqual(requests[1].method, 'POST')
        self.assertEqual(len(requests), 3)
        self.assertRegex(requests[1].get_header('Idempotency-key'), r'^1\.[0-9a-f]{16}$')

    def test_old_protocol_is_rejected_without_write_or_fallback(self):
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', return_value=response({'api_version': 2})) as transport:
            with self.assertRaisesRegex(ValueError, 'Device API v3 required'):
                self.client.request('/api/v3/configuration/profile', 'POST', {})
        self.assertEqual(transport.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'requires Device API v3'):
            self.client.request('/api/v2/device')

    def test_permission_error_is_not_misreported_as_protocol_error(self):
        failure = urllib.error.HTTPError('https://device/api/v3', 403, 'Forbidden', {},
            response({'api_version': 3, 'error': {'code': 'permission_denied', 'message': 'read scope required', 'retryable': False}}))
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', side_effect=failure) as transport:
            with self.assertRaisesRegex(ValueError, 'read scope required'):
                self.client.request('/api/v3/health')
        self.assertEqual(transport.call_count, 1)

    def test_missing_discovery_returns_coordinated_upgrade_message(self):
        failure = urllib.error.HTTPError('https://device/api/v3', 404, 'Not Found', {}, response({'error': 'endpoint not found'}))
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', side_effect=failure):
            with self.assertRaisesRegex(ValueError, 'update IoT-MD and Management together'):
                self.client.request('/api/v3/health')

    def test_ambiguous_write_is_never_retried(self):
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', side_effect=[
            response({'api_version': 3, 'capabilities': {'persistent_idempotency': True}, 'next_request_sequence': 1}), TimeoutError('response lost'),
        ]) as transport:
            with self.assertRaises(TimeoutError):
                self.client.request('/api/v3/configuration/restart', 'POST', {})
        self.assertEqual(transport.call_count, 2)

    def test_operational_response_must_also_be_v3(self):
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', side_effect=[response({'api_version': 3}), response({'api_version': 2})]):
            with self.assertRaisesRegex(ValueError, 'Invalid Device API v3 response'):
                self.client.request('/api/v3/health')

    def test_old_v3_without_durable_operations_cannot_mutate(self):
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', return_value=response({'api_version': 3})) as transport:
            with self.assertRaisesRegex(ValueError, 'Durable Device API v3'):
                self.client.request('/api/v3/configuration/restart', 'POST', {})
        self.assertEqual(transport.call_count, 1)

    def test_mutation_sequence_advances_on_lost_response(self):
        self.client.api_metadata = {'api_version': 3, 'capabilities': {'persistent_idempotency': True}, 'next_request_sequence': 7}
        observer = mock.Mock()
        self.client.request_recorder = observer
        with mock.patch.object(SERVICE.urllib.request, 'urlopen', side_effect=TimeoutError('response lost')) as transport:
            with self.assertRaises(TimeoutError):
                self.client.request('/api/v3/configuration/restart', 'POST', {})
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.client.api_metadata['next_request_sequence'], 8)
        self.assertEqual(observer.call_args_list[0].args[3], 'reserved')
        self.assertEqual(observer.call_args_list[1].args[3], 'uncertain')
        self.assertEqual(observer.call_args_list[0].args[0], observer.call_args_list[1].args[0])

    def test_request_persistence_failure_prevents_transport_write(self):
        self.client.api_metadata = {'api_version': 3, 'capabilities': {'persistent_idempotency': True}, 'next_request_sequence': 1}
        self.client.request_recorder = mock.Mock(side_effect=OSError('database full'))
        with mock.patch.object(SERVICE.urllib.request, 'urlopen') as transport:
            with self.assertRaises(OSError):
                self.client.request('/api/v3/configuration/restart', 'POST', {})
        transport.assert_not_called()

    def test_allocated_sequences_and_request_keys_survive_management_restart(self):
        with mock.patch.object(sys, 'path', [str(SOURCE.parent)] + sys.path):
            from fleet_repository import FleetRepository
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fleet.sqlite'
            store = FleetRepository(path)
            self.assertEqual(store.next_api_sequence(7), 7)
            store.record_api_request('iot-md-001.local', '7.0123456789abcdef', 'POST',
                '/api/v3/configuration/restart', 'reserved', '')
            store.close()
            store = FleetRepository(path)
            try:
                self.assertEqual(store.next_api_sequence(1), 8)
                records = json.loads(store.connection.execute(
                    "SELECT value FROM metadata WHERE key='api_request_journal'").fetchone()['value'])
                self.assertEqual(records[0]['key'], '7.0123456789abcdef')
                self.assertNotIn('payload', records[0])
            finally:
                store.close()
