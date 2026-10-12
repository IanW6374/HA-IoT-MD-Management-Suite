import hashlib
import hmac
import json
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from fleet_repository import FleetRepository
from fleet_service import FleetController
from mqtt_progress import MQTTProgressReceiver, tracking_settings, MAX_REPORT_BYTES
from operation_reporting import DEFAULT_TOPIC, report_bytes, report_topic
from profile_secrets import ProfileSecretCipher


class MQTTProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = FleetRepository(root / 'fleet.db', profile_cipher=ProfileSecretCipher(root / 'key'))
        self.addCleanup(self.store.close)
        self.identifier = self.store.register({'id': 'one', 'host': 'one.local'})['id']
        self.store.record_poll('one', {'device': {'device_id': 'hardware', 'release_sequence': 1,
                                               'firmware_release_sequence': 1}}, {}, {})
        self.options = {'update_tracking_method': 'mqtt'}
        self.clock = 1000
        self.controller = FleetController(self.store, mock.Mock(sign=lambda value: value, fingerprint=lambda: ''),
                                          now=lambda: self.clock, tracking_options=self.options)
        self.controller.poll_device = mock.Mock()
        self.client = mock.Mock()
        self.client.request.return_value = {'capabilities': {'update_operation_contract': 1,
                                                             'update_operation_reports': True}}
        self.controller._client = mock.Mock(return_value=self.client)
        self.deployment = self.store.create_deployment({'activation': 'now', 'update': {
            'release_sequence': 2828, 'release_type': 'universal'}}, ['one'])
        self.controller.execute_deployment_target(self.deployment['id'], 'one')
        self.dispatch = self.result()['dispatch']
        self.secret = self.store.get_update_report_key(self.dispatch['update_operation_id'])
        self.receiver = MQTTProgressReceiver(self.store, self.controller, self.options, now=lambda: self.clock)
        self.receiver.connected = True
        self.controller.mqtt_connected = lambda: self.receiver.connected

    def result(self):
        return self.store.get_deployment(self.deployment['id'])['results']['one']

    def report(self, **changes):
        value = {'contract': 1, 'id': self.dispatch['update_operation_id'], 'device_id': 'hardware',
            'policy_sequence': self.dispatch['policy_sequence'], 'release_sequence': 2828,
            'release_type': 'universal', 'activation': 'install', 'state': 'checking', 'phase': 'inspect',
            'revision': 2, 'completed': ['queued'], 'boot_id': 'boot-1', 'boot_epoch': 1,
            'sample_sequence': 1, 'percent': None, 'detail': 'Device update checking', **changes}
        value['mac'] = hmac.new(bytes.fromhex(self.secret['key']), report_bytes(value), hashlib.sha256).hexdigest()
        return value

    def receive(self, value=None, topic=None):
        return self.receiver.receive(topic or self.secret['topic'], json.dumps(value or self.report()).encode())

    def test_api_is_default_and_never_creates_a_broker_connection(self):
        self.assertEqual(tracking_settings({}), {'method': 'api'})
        receiver = MQTTProgressReceiver(self.store, self.controller, {}, client_factory=mock.Mock())
        self.assertFalse(receiver.start())
        receiver.client_factory.assert_not_called()
        self.assertFalse(receiver.receive(self.secret['topic'], json.dumps(self.report()).encode()))

    def test_key_is_encrypted_before_submission_and_never_exposed_in_public_results(self):
        payload = self.client.request.call_args.args[2]
        self.assertEqual(set(payload), {'policy', 'report_key', 'reporting'})
        self.assertEqual(payload['report_key'], self.secret['key'])
        self.assertEqual(payload['reporting']['host'], 'mqtt-broker.home.arpa')
        with self.store.lock:
            token = self.store.connection.execute('SELECT secret FROM update_report_keys').fetchone()[0]
        self.assertNotIn(self.secret['key'], token)
        self.assertNotIn(self.secret['key'], json.dumps(self.store.list_deployments()))
        self.assertNotIn(self.secret['key'], json.dumps(self.receiver.snapshot()))

    def test_valid_report_updates_same_projection_without_device_io(self):
        self.assertTrue(self.receive())
        self.client.request.reset_mock()
        self.receiver.drain()
        self.assertEqual(self.result()['update_operation']['revision'], 2)
        self.client.request.assert_not_called()
        self.assertEqual(self.store.get_device('one')['poll_state']['mqtt_progress']['operation_id'], self.dispatch['update_operation_id'])

    def test_tampered_unsigned_wrong_topic_and_wrong_device_reports_rejected(self):
        value = self.report()
        value['state'] = 'failed'
        self.assertFalse(self.receive(value))
        self.assertFalse(self.receive(topic='unapproved/topic'))
        self.assertFalse(self.receive(self.report(device_id='other')))
        self.assertFalse(self.receive(self.report(policy_sequence=999)))
        self.assertEqual(self.receiver.snapshot()['pending'], 0)
        self.assertNotIn('update_operation', self.result())

    def test_duplicate_and_pre_reboot_reports_cannot_refresh_freshness(self):
        value = self.report()
        self.assertTrue(self.receive(value))
        self.receiver.drain()
        self.clock += 10
        self.assertFalse(self.receive(value))
        self.assertTrue(self.receive(self.report(boot_id='boot-2', boot_epoch=2, sample_sequence=0)))
        self.receiver.drain()
        self.assertFalse(self.receive(self.report(sample_sequence=1000)))

    def test_newest_per_operation_coalesced_and_older_payload_cannot_overwrite_it(self):
        self.assertTrue(self.receive(self.report(sample_sequence=2)))
        self.assertTrue(self.receive(self.report(sample_sequence=3)))
        self.assertFalse(self.receive(self.report(sample_sequence=2)))
        self.assertEqual(self.receiver.snapshot()['pending'], 1)
        self.receiver.drain()
        self.assertEqual(self.result()['update_operation']['sample_sequence'], 3)

    def test_stale_queued_reports_do_not_claim_recent_observation(self):
        self.assertTrue(self.receive())
        self.clock += 16
        self.receiver.drain()
        self.assertNotIn('update_operation', self.result())

    def test_fresh_push_suppresses_frequent_reads_but_not_periodic_reconciliation(self):
        self.assertTrue(self.receive())
        self.receiver.drain()
        self.assertTrue(self.controller.progress_read_needed('one'))  # Initial API reconciliation required.
        self.controller._api_reads['one']['mqtt_progress']['api_snapshot_at'] = self.clock
        self.store.record_poll_state('one', self.controller._poll_state('one'))
        self.assertFalse(self.controller.progress_read_needed('one'))
        self.clock += 16
        self.assertTrue(self.controller.progress_read_needed('one'))
        self.assertTrue(self.receive(self.report(sample_sequence=2)))
        self.receiver.drain()
        self.assertFalse(self.controller.progress_read_needed('one'))
        self.clock += 45
        self.assertTrue(self.receive(self.report(sample_sequence=3)))
        self.receiver.drain()
        self.assertTrue(self.controller.progress_read_needed('one'))  # 60-second snapshot deadline.

    def test_disconnect_and_cancellation_always_keep_api_reconciliation(self):
        self.assertTrue(self.receive())
        self.receiver.drain()
        self.controller._api_reads['one']['mqtt_progress']['api_snapshot_at'] = self.clock
        self.store.record_poll_state('one', self.controller._poll_state('one'))
        self.receiver.connected = False
        self.assertTrue(self.controller.progress_read_needed('one'))
        self.receiver.connected = True
        self.store.set_deployment_target(self.deployment['id'], 'one', 'cancelling')
        self.assertTrue(self.controller.progress_read_needed('one'))

    def test_stop_tracking_before_drain_cannot_resurrect_a_job(self):
        self.assertTrue(self.receive())
        self.controller.stop_tracking_deployment(self.deployment['id'], 'stop tracking ' + self.deployment['id'])
        self.receiver.drain()
        self.assertEqual(self.result()['status'], 'unresolved')
        self.assertFalse(self.receive(self.report(sample_sequence=2)))

    def test_hostname_rekey_preserves_encrypted_key_and_hardware_topic_identity(self):
        self.store.migrate_hostname_identities()
        updated = self.store.get_update_report_key(self.dispatch['update_operation_id'])
        self.assertEqual(updated['device_id'], 'one.local')
        self.assertEqual(updated['key'], self.secret['key'])
        self.assertEqual(updated['topic'], self.secret['topic'])
        self.assertTrue(self.receive())
        self.receiver.drain()
        result = self.store.get_deployment(self.deployment['id'])['results']['one.local']
        self.assertEqual(result['update_operation']['revision'], 2)

    def test_scheduler_filter_uses_supplied_dispatch_and_cached_small_state(self):
        self.assertTrue(self.receive())
        self.receiver.drain()
        self.controller._api_reads['one']['mqtt_progress']['api_snapshot_at'] = self.clock
        with mock.patch.object(self.store, 'active_deployments', side_effect=AssertionError('Repeated full fleet scan')), \
             mock.patch.object(self.store, 'get_device', side_effect=AssertionError('Repeated inventory read')):
            for _ in range(100):
                self.assertFalse(self.controller.progress_read_needed('one', self.result()))

    def test_pending_receiver_capacity_is_bounded_even_for_valid_registered_operations(self):
        import mqtt_progress
        with mock.patch.object(mqtt_progress, 'MAX_PENDING_REPORTS', 0):
            self.assertFalse(self.receive())
        self.assertEqual(self.receiver.snapshot()['dropped'], 1)
        self.assertEqual(self.receiver.snapshot()['pending'], 0)

    def test_startup_tls_error_is_redacted_and_keeps_api_fallback(self):
        with mock.patch('mqtt_progress.ssl.create_default_context', side_effect=OSError('private-password detail')):
            self.assertFalse(self.receiver.start())
        self.assertEqual(self.receiver.snapshot()['state'], 'unavailable')
        self.assertNotIn('private-password', json.dumps(self.receiver.snapshot()))
        self.assertTrue(self.controller.progress_read_needed('one'))

    def test_custom_template_has_derived_subscription_and_tls_is_mandatory(self):
        options = {**self.options, 'mqtt_progress_topic': 'custom/{device_id}/updates/{operation_id}',
                   'mqtt_username': 'private-user', 'mqtt_password': 'private-pass'}
        client = mock.Mock()
        receiver = MQTTProgressReceiver(self.store, self.controller, options, client_factory=mock.Mock(return_value=client))
        self.addCleanup(receiver.stop)
        self.assertTrue(receiver.start())  # Real SDK, fake transport: no broker connection.
        context = client.tls_set_context.call_args.args[0]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        client.connect_async.assert_called_once()
        self.assertEqual(client.connect_async.call_args.args[:2], ('mqtt-broker.home.arpa', 8883))
        properties = client.connect_async.call_args.kwargs['properties']
        self.assertEqual(properties.MaximumPacketSize, 8192)
        self.assertEqual(properties.ReceiveMaximum, 32)
        receiver._on_connect(client, None, None, 0, None)
        client.subscribe.assert_called_once_with('custom/+/updates/+', qos=1)
        self.assertNotIn('private-pass', json.dumps(receiver.snapshot()))

    def test_bad_topics_overlong_and_malformed_messages_fail_closed(self):
        with self.assertRaises(ValueError): tracking_settings({**self.options, 'mqtt_progress_topic': 'arbitrary/#'})
        with self.assertRaises(ValueError): tracking_settings({**self.options, 'mqtt_client_cert_path': '/ssl/client.pem'})
        self.assertFalse(self.receiver.receive(self.secret['topic'], b'['))
        self.assertFalse(self.receiver.receive(self.secret['topic'], b'x' * (MAX_REPORT_BYTES + 1)))
        value = self.report()
        value['secret'] = 'must not be accepted'
        self.assertFalse(self.receive(value))
