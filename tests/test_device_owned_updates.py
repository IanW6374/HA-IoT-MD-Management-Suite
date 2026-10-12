import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from fleet_repository import FleetRepository
from fleet_service import FleetController
from update_operation_contract import PHASES


class DeviceOwnedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FleetRepository(Path(self.directory.name) / 'fleet.db')
        self.addCleanup(self.store.close)
        self.identifier = self.store.register({'id': 'test', 'host': 'test.local'})['id']
        self.store.record_poll(self.identifier, {'device': {'device_id': 'hardware-id'}}, {}, {})
        self.store.record_device_versions(self.identifier, {'device_id': 'hardware-id', 'release_sequence': 2827,
            'firmware_release_sequence': 2827, 'application_version': 'alpha122', 'firmware_version': 'alpha122'})
        self.update = {'release_sequence': 2828, 'release_type': 'universal', 'version': 'alpha123'}
        self.deployment = self.store.create_deployment({'update': self.update, 'activation': 'now'}, [self.identifier])
        self.controller = FleetController(self.store, Mock(sign=lambda value: value, fingerprint=lambda: ''))
        self.controller.poll_device = Mock()
        self.client = Mock()
        self.client.request.return_value = {'api_version': 3, 'capabilities': {
            'update_operation_contract': 1, 'update_cancellation': True}}
        self.controller._client = Mock(return_value=self.client)

    def result(self):
        return self.store.get_deployment(self.deployment['id'])['results'][self.identifier]

    def dispatch(self):
        self.controller.execute_deployment_target(self.deployment['id'], self.identifier)
        return self.result()['dispatch']

    def observation(self, dispatch, state='checking', revision=2):
        return {'contract': 1, 'id': dispatch['update_operation_id'], 'device_id': 'hardware-id',
            'release_sequence': 2828, 'release_type': 'universal', 'policy_sequence': dispatch['policy_sequence'],
            'state': state, 'phase': 'complete' if state == 'complete' else 'inspect',
            'completed': list(PHASES) if state == 'complete' else ['queued'], 'revision': revision,
            'boot_id': 'boot-1', 'sample_sequence': 0, 'percent': None, 'detail': 'Observed by device'}

    def test_id_and_signed_intent_are_saved_before_post(self):
        def request(path, method='GET', policy=None):
            if method == 'POST':
                dispatch = self.result()['dispatch']
                self.assertEqual(policy['commands'][0]['id'], dispatch['update_operation_id'])
                self.assertEqual(policy['policy_sequence'], dispatch['policy_sequence'])
                self.assertEqual(dispatch['device_id'], 'hardware-id')
                return {'accepted': True}
            return {'capabilities': {'update_operation_contract': 1}}
        self.client.request.side_effect = request
        dispatch = self.dispatch()
        self.assertEqual(len(dispatch['update_operation_id']), 32)
        self.controller.execute_deployment_target(self.deployment['id'], self.identifier)
        self.assertEqual(sum(call.args[1:2] == ('POST',) for call in self.client.request.call_args_list), 1)

    def test_legacy_firmware_cannot_start_new_inferred_job(self):
        self.client.request.return_value = {'capabilities': {}}
        with self.assertRaisesRegex(ValueError, 'legacy inferred tracking is retired'): self.dispatch()
        self.assertEqual(self.result()['status'], 'failed')
        self.assertFalse(any(call.args[1:2] == ('POST',) for call in self.client.request.call_args_list))

    def test_matching_installed_versions_do_not_complete_a_new_operation(self):
        self.dispatch()
        record = self.store.get_device(self.identifier)
        record['inventory']['device'].update(release_sequence=2828, firmware_release_sequence=2828)
        self.controller._reconcile_deployments(self.identifier, record)
        self.assertEqual(self.result()['status'], 'checking')

    def test_operation_snapshot_needs_no_command_list_or_extra_health_confirmation(self):
        dispatch = self.dispatch()
        self.client.request.reset_mock()
        self.controller._progress_client = Mock(return_value=self.client)
        self.client.request.return_value = {'device': {'device_id': 'hardware-id', 'release_sequence': 2828,
            'firmware_release_sequence': 2828}, 'update_operation': self.observation(dispatch, 'complete', 5)}
        self.controller.poll_progress(self.identifier)
        self.assertEqual(self.result()['status'], 'complete')
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list],
                         ['/api/v3/fleet/update-operations/' + dispatch['update_operation_id']])

    def test_different_operation_or_newer_firmware_cannot_complete_this_job(self):
        dispatch = self.dispatch()
        value = self.observation(dispatch, 'complete', 5)
        value['id'] = 'f'*32
        with self.assertRaises(ValueError): self.controller.observe_update_operation(self.deployment['id'], self.identifier, value)
        self.assertEqual(self.result()['status'], 'checking')

    def test_stale_snapshots_do_not_rewind_and_reducer_survives_controller_restart(self):
        dispatch = self.dispatch()
        value = self.observation(dispatch, revision=4)
        self.assertTrue(self.controller.observe_update_operation(self.deployment['id'], self.identifier, value))
        self.controller = FleetController(self.store, None)
        stale = dict(value, revision=3)
        self.assertFalse(self.controller.observe_update_operation(self.deployment['id'], self.identifier, stale))
        self.assertEqual(self.result()['update_operation']['revision'], 4)

    def test_cancel_sends_exact_operation_id(self):
        dispatch = self.dispatch()
        self.client.request.side_effect = [{'capabilities': {'update_cancellation': True}}, {'status': 'cancelling'}]
        self.controller.cancel_deployment_target(self.deployment['id'], self.identifier)
        self.assertEqual(self.client.request.call_args.args[2]['update_operation_id'], dispatch['update_operation_id'])

    def test_timeout_is_not_interpreted_as_restart_or_completed_install(self):
        self.dispatch()
        self.controller._progress_client = Mock(return_value=self.client)
        self.client.request.side_effect = TimeoutError('Device unavailable')
        with self.assertRaises(TimeoutError): self.controller.poll_progress(self.identifier)
        self.assertEqual(self.result()['status'], 'checking')

    def test_interrupted_is_explicit_unresolved_history_and_not_an_active_job(self):
        dispatch = self.dispatch()
        self.controller.observe_update_operation(self.deployment['id'], self.identifier,
                                                self.observation(dispatch, 'interrupted', 3))
        self.assertEqual(self.result()['status'], 'unresolved')
        self.assertEqual(self.store.active_deployments(), [])
        self.assertIn('update_operation', json.dumps(self.store.list_deployments()))

