"""Real client/controller regression tests with virtual HTTP time, no device I/O."""
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
import fleet_service
from fleet_service import DeviceClient, FleetController
from fleet_repository import FleetRepository
from device_scheduler import DeviceScheduler


class LiveStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clock = 1000.
        self.store = FleetRepository(Path(directory.name) / 'fleet.db', now=lambda: int(self.clock))
        self.addCleanup(self.store.close)
        self.identifier = self.store.register({'id': 'test.local', 'host': 'test.local'})['id']
        self.update = {'release_sequence': 2822, 'release_type': 'universal', 'version': 'alpha118'}
        self.deployment = self.store.create_deployment({'update': self.update, 'activation': 'now'}, [self.identifier])
        self.store.set_deployment_target(self.deployment['id'], self.identifier, 'installing')
        self.controller = FleetController(self.store, None, timeout=10, progress_timeout=5, now=lambda: int(self.clock))
        self.paths = []
        self.status = {'api_version': 3, 'boot_id': 'boot-one',
            'device': {'device_id': 'real-device', 'release_sequence': 2822, 'firmware_release_sequence': 2822}, 'fleet': {}}
        self.store.record_device_versions(self.identifier, dict(self.status['device'],
            application_version='alpha118', firmware_version='alpha118'))
        self.delay = lambda path: 3
        self.health = {'api_version': 3, 'health': {}}
        self.compact = True
        self.patch = mock.patch.object(DeviceClient, '_context', return_value=object())
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.timer = mock.patch.object(fleet_service.time, 'monotonic', side_effect=lambda: self.clock)
        self.timer.start(); self.addCleanup(self.timer.stop)
        self.transport = mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=self.read)
        self.transport.start(); self.addCleanup(self.transport.stop)

    def read(self, request, **kwargs):
        self.assertEqual(request.method, 'GET')
        path = request.full_url.split(':8444')[-1]
        self.paths.append(path)
        delay = self.delay(path)
        self.clock += min(delay, kwargs['timeout'])
        if delay > kwargs['timeout']:
            raise TimeoutError('Simulated slow HTTP response')
        value = ({'api_version': 3, 'capabilities': {'live_update_status': self.compact}}
                 if path == '/api/v3' else self.health if path.endswith('/health') else self.status)
        return io.BytesIO(json.dumps(value).encode())

    def result(self):
        return self.store.get_deployment(self.deployment['id'])['results'][self.identifier]

    def test_discovery_status_and_health_each_receive_an_independent_budget(self):
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths, ['/api/v3', '/api/v3/fleet/update-status', '/api/v3/health'])
        self.assertEqual(self.clock, 1009)
        self.assertEqual(self.result()['status'], 'complete')
        self.assertEqual([item['response_bytes'] > 0 for item in self.controller.progress_diagnostics()[self.identifier]['reads']], [True]*3)

    def test_transient_status_timeout_keeps_discovery_and_retry_succeeds(self):
        self.delay = lambda path: 6 if path.endswith('/update-status') else 3
        with self.assertRaises(TimeoutError): self.controller.poll_progress(self.identifier)
        client = self.controller._progress_client(self.store.get_device(self.identifier, public=False))
        self.assertIsNotNone(client.api_metadata)
        self.delay = lambda path: 3
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3'), 1)
        self.assertEqual(self.result()['status'], 'complete')

    def test_retained_failure_survives_recovery_completion_and_controller_restart(self):
        self.delay = lambda path: 6 if path.endswith('/update-status') else 3
        with self.assertRaises(TimeoutError):
            self.controller.poll_progress(self.identifier)
        self.delay = lambda path: 3
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.result()['status'], 'complete')
        self.assertEqual(self.controller.progress_diagnostics()[self.identifier]['error'], '')
        restarted = FleetController(self.store, None, timeout=10, now=lambda: int(self.clock))
        with mock.patch.object(restarted, '_client', side_effect=AssertionError('Device I/O')):
            exported = restarted.progress_trace_export(self.deployment['id'])
        group = exported['devices'][0]['traces']['deployments'][self.deployment['id']]
        self.assertEqual(group['attempts_total'], 2)
        self.assertEqual(group['failures_total'], 1)
        failure = group['failures'][0]
        self.assertEqual(failure['workflow_stage'], 'status')
        self.assertEqual(failure['reads'][-1]['failure'], 'socket_timeout')
        self.assertEqual(failure['reads'][-1]['path'], '/api/v3/fleet/update-status')
        self.assertEqual(failure['reads'][-1]['start_offset_ms'], 3000)
        self.assertEqual(failure['reads'][-1]['budget_ms'], 5000)
        self.assertEqual(failure['duration_ms'], 8000)
        self.assertEqual(group['attempts'][-1]['failure'], '')
        self.assertEqual(group['attempts'][-1]['workflow_stage'], 'confirmation')
        self.assertNotIn('progress_traces', self.store.get_device(self.identifier)['poll_state'])
        self.assertNotIn('progress_traces', self.store.get_device(self.identifier, public=False)['poll_state'])
        self.assertIn(self.deployment['id'], self.store.get_progress_traces(self.identifier)['deployments'])
        self.store.record_poll_state(self.identifier, {'api': {'last_success': 9999}})
        self.assertIn(self.deployment['id'], self.store.get_progress_traces(self.identifier)['deployments'])

    def test_later_deployment_export_cannot_inherit_an_earlier_failure(self):
        self.delay = lambda path: 6
        with self.assertRaises(TimeoutError): self.controller.poll_progress(self.identifier)
        self.delay = lambda path: 0
        self.controller.poll_progress(self.identifier)
        later = self.store.create_deployment({'update': {**self.update, 'release_sequence': 2823}}, [self.identifier])
        self.store.set_deployment_target(later['id'], self.identifier, 'checking')
        self.controller.poll_progress(self.identifier)
        data = self.controller.progress_trace_export(later['id'])['devices'][0]['traces']['deployments']
        self.assertEqual(list(data), [later['id']])
        self.assertEqual(data[later['id']]['failures_total'], 0)
        self.assertEqual(len(self.controller.progress_trace_export()['devices'][0]['traces']['deployments']), 2)

    def test_trace_failure_contains_no_exception_payload_or_credential_path(self):
        def unavailable(*args, **kwargs):
            raise TimeoutError('password=secret /private/client.key?token=secret')
        with mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=unavailable):
            with self.assertRaises(TimeoutError): self.controller.poll_progress(self.identifier)
        text = json.dumps(self.controller.progress_trace_export())
        self.assertNotIn('secret', text)
        self.assertNotIn('client.key', text)
        self.assertIn('socket_timeout', text)

    def test_expired_budget_is_also_measured_without_replaying_a_request(self):
        client = self.controller._progress_client(self.store.get_device(self.identifier, public=False))
        client.deadline = self.clock - 1
        with self.assertRaises(TimeoutError): client.request('/api/v3')
        self.assertEqual(self.paths, [])
        self.assertEqual(client.read_metrics[-1]['failure'], 'read_deadline_exceeded')
        self.assertEqual(client.read_metrics[-1]['stage'], 'reservation')

    def test_health_timeout_never_confirms_cached_health_or_evicts_discovery(self):
        self.delay = lambda path: 11 if path.endswith('/health') else 3
        with self.assertRaises(TimeoutError): self.controller.poll_progress(self.identifier)
        diagnostic = self.controller.progress_diagnostics()[self.identifier]
        self.assertTrue(diagnostic['received_on_attempt'])
        self.assertEqual(diagnostic['workflow_stage'], 'confirmation')
        self.assertTrue(diagnostic['responsive'])
        self.assertEqual(self.result()['status'], 'installing')
        self.delay = lambda path: 3
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3'), 1)
        self.assertEqual(self.result()['status'], 'complete')

    def test_trial_cannot_be_confirmed_even_when_versions_match(self):
        self.status['device']['update_progress'] = {'universal_status': 'trial', 'universal_sequence': 2822}
        self.controller.poll_progress(self.identifier)
        self.assertNotIn('/api/v3/health', self.paths)
        self.assertEqual(self.result()['status'], 'installing')

    def test_boot_change_invalidates_discovery_but_not_observed_progress(self):
        self.status['device']['update_progress'] = {'universal_status': 'trial', 'universal_sequence': 2822}
        self.controller.poll_progress(self.identifier)
        self.status['boot_id'] = 'boot-two'
        self.controller.poll_progress(self.identifier)
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3'), 2)
        self.assertEqual(self.controller.progress_diagnostics()[self.identifier]['boot_id'], 'boot-two')

    def test_inline_cancellation_acknowledgement_avoids_another_read_and_any_write(self):
        self.store.set_deployment_target(self.deployment['id'], self.identifier, 'cancelling')
        self.status['device']['release_sequence'] = 2821
        self.store.record_device_versions(self.identifier, dict(self.status['device'],
            application_version='alpha117', firmware_version='alpha118'))
        self.status['cancellation'] = {'status': 'cancelled', **self.update}
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.result()['status'], 'cancelled')
        self.assertEqual(self.paths, ['/api/v3', '/api/v3/fleet/update-status'])

    def test_invalid_status_cannot_advance_or_confirm(self):
        self.status['device'] = 'malformed'
        with self.assertRaisesRegex(ValueError, 'Invalid live'):
            self.controller.poll_progress(self.identifier)
        self.assertNotIn('/api/v3/health', self.paths)
        self.assertNotIn(self.identifier, self.controller._progress_clients)

    def test_enabling_upgrade_refreshes_negative_capability_discovery(self):
        self.compact = False
        self.status['device']['update_progress'] = {'universal_status': 'trial', 'universal_sequence': 2822}
        self.controller.poll_progress(self.identifier)
        self.assertIn('/api/v3/device/inventory', self.paths)
        self.clock += 61
        self.compact = True
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3'), 2)
        self.assertEqual(self.paths[-1], '/api/v3/fleet/update-status')

    def test_wrong_device_identity_and_oversized_status_fail_closed(self):
        self.store.record_poll(self.identifier, {'device': {'device_id': 'enrolled-device'}}, {'health': {}}, {'events': []})
        with self.assertRaisesRegex(ValueError, 'identity does not match'):
            self.controller.poll_progress(self.identifier)
        self.status['device']['device_id'] = 'enrolled-device'
        self.status['padding'] = 'x'*70000
        with self.assertRaisesRegex(ValueError, 'response is too large'):
            self.controller.poll_progress(self.identifier)
        self.assertNotIn('/api/v3/health', self.paths)

    def test_transient_http_503_preserves_valid_discovery(self):
        import urllib.error
        self.status['device']['update_progress'] = {'universal_status': 'trial', 'universal_sequence': 2822}
        self.controller.poll_progress(self.identifier)
        error = urllib.error.HTTPError('https://test/api', 503, 'Busy', {},
            io.BytesIO(json.dumps({'api_version': 3, 'error': 'Device busy'}).encode()))
        with mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(fleet_service.DeviceReadUnavailable):
                self.controller.poll_progress(self.identifier)
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.paths.count('/api/v3'), 1)

    def test_accepted_and_uncertain_dispatch_handoff_without_post_submission_full_poll(self):
        self.store.set_deployment_target(self.deployment['id'], self.identifier, 'cancelled')
        self.deployment = self.store.create_deployment({'update': self.update, 'activation': 'now'}, [self.identifier])
        self.store.record_poll(self.identifier, {'device': {'device_id': 'real-device',
            'release_sequence': 2821, 'firmware_release_sequence': 2821},
            'fleet': {'verification_key_fingerprint': 'a'*64}}, {'health': {}}, {'events': []})
        self.controller.signer = mock.Mock(fingerprint=lambda: 'a'*64, sign=lambda value: value)
        self.controller.poll_device = mock.Mock()
        client = mock.Mock(last_request_key=None)
        self.controller._client = mock.Mock(return_value=client)
        client.request.return_value = {'api_version': 3, 'accepted': True, 'capabilities': {'update_operation_contract': 1}}
        request = {'device_id': self.identifier, 'commands': [
            {'action': 'download-update', 'release_sequence': 2822, 'release_type': 'universal'}]}
        self.controller.apply_policy(request, deployment_id=self.deployment['id'])
        self.controller.poll_device.assert_called_once_with(self.identifier)
        self.assertEqual(client.request.call_count, 2)  # capability discovery + one write
        self.controller.poll_device.reset_mock()
        def uncertain(path, *args):
            if path == '/api/v3':
                return {'api_version': 3, 'capabilities': {'update_operation_contract': 1}}
            client.last_request_key = '1.0123456789abcdef'
            raise TimeoutError('POST acknowledgement lost')
        client.request.side_effect = uncertain
        result = self.controller.apply_policy(request, deployment_id=self.deployment['id'])
        self.assertTrue(result['uncertain'])
        self.controller.poll_device.assert_called_once_with(self.identifier)
        self.assertEqual(client.request.call_count, 4)
        self.assertEqual(self.result()['status'], 'checking')
        self.assertIn('without replay', self.result()['detail'])
        self.controller.execute_deployment_target(self.deployment['id'], self.identifier)
        self.assertEqual(client.request.call_count, 4)


class LiveStatusLoadTests(unittest.TestCase):
    def test_real_client_path_at_3_50_and_100_devices_with_scaled_latency_and_failures(self):
        # Seconds-scale budgets are tested with virtual time above. Here the
        # real worker pool/client/body reader uses scaled latency to stress
        # fairness and bounds without turning this into a multi-minute test.
        for count in (3, 50, 100):
            with self.subTest(devices=count), tempfile.TemporaryDirectory() as directory:
                store = FleetRepository(Path(directory)/'load.db')
                ids = [store.register({'id': f'load-{index}', 'host': f'load-{index}.local'})['id'] for index in range(count)]
                deployment = store.create_deployment({'update': {'release_sequence': 2822, 'release_type': 'universal'}}, ids)
                for identifier in ids: store.set_deployment_target(deployment['id'], identifier, 'staging')
                controller = FleetController(store, None)
                clock = [1000.]
                scheduler = DeviceScheduler(store, controller, lambda job: store.complete_job(job['id']), now=lambda: clock[0])
                lock = threading.Lock()
                requests, failures = [], set()
                class SlowBody(io.BytesIO):
                    def read1(self, size):
                        time.sleep(.005)
                        return super().read1(min(size, 64))
                def transport(request, **kwargs):
                    identifier = request.host.split(':')[0].split('.')[0]
                    path = request.full_url.split(':8444')[-1]
                    self.assertEqual(request.method, 'GET')
                    with lock:
                        requests.append((identifier, path))
                    time.sleep(.01)
                    if identifier == ids[0] and path.endswith('/update-status') and identifier not in failures:
                        failures.add(identifier)
                        raise TimeoutError('Simulated transient status failure')
                    value = {'api_version': 3, 'capabilities': {'live_update_status': True}} if path == '/api/v3' else {
                        'api_version': 3, 'device': {'release_sequence': 2821, 'firmware_release_sequence': 2821,
                            'update_progress': {'release_sequence': 2822, 'type': 'universal', 'phase': 'core_write', 'percent': 37}},
                        'fleet': {'policy': {'commands': [{'id': 'download', 'action': 'download-update',
                            'release_sequence': 2822, 'release_type': 'universal'}]},
                            'pending_commands': [{'id': 'download', 'action': 'download-update',
                            'release_sequence': 2822, 'release_type': 'universal'}]}}
                    return SlowBody(json.dumps(value).encode())
                try:
                    with mock.patch.object(DeviceClient, '_context', return_value=object()), \
                            mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=transport):
                        deadline = time.monotonic()+10
                        while time.monotonic()<deadline:
                            scheduler.tick()
                            self.assertLessEqual(scheduler.snapshot()['progress_workers'], 4)
                            observed = controller.progress_diagnostics()
                            if all(observed.get(key, {}).get('last_received') for key in ids[1:]): break
                            time.sleep(.002)
                        else: self.fail('Healthy devices starved behind slow device')
                        clock[0] += 60
                        deadline = time.monotonic()+5
                        while time.monotonic()<deadline:
                            scheduler.tick()
                            if controller.progress_diagnostics().get(ids[0], {}).get('last_received'): break
                            time.sleep(.002)
                        else: self.fail('Transient failure did not recover')
                        scheduler.close()
                    self.assertEqual(sum(identifier == ids[0] and path == '/api/v3' for identifier,path in requests), 1)
                    self.assertFalse(any('/inventory' in path or '/configuration' in path for _,path in requests))
                    self.assertTrue(all(item['update_percent']==37 for item in store.get_deployment(deployment['id'])['results'].values()))
                    print(f'Live client load: {count} devices; bounded pool, delayed bodies, transient recovery; {len(requests)} reads')
                finally:
                    scheduler.close()
                    store.close()


if __name__ == '__main__':
    unittest.main()
