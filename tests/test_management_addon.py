import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


class FleetAddonTests(unittest.TestCase):
    def test_home_assistant_repository_layout_is_installable(self):
        root = Path(__file__).resolve().parents[1]
        repository = (root / 'repository.yaml').read_text(encoding='utf-8')
        addon = (root / 'iotmd_management/config.yaml').read_text(encoding='utf-8')

        self.assertIn('name: IoTMD Management Suite', repository)
        self.assertIn(
            'url: https://github.com/IanW6374/HA-IoTMD-Management-Suite',
            repository,
        )
        self.assertIn('slug: iotmd_management', addon)
        self.assertIn('8443/tcp: 8443', addon)
        self.assertTrue((root / 'iotmd_management/Dockerfile').is_file())

    def test_incompatible_sqlite_schema_requires_clean_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fleet.db'
            connection = sqlite3.connect(str(path))
            connection.execute(
                'CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)'
            )
            connection.execute(
                'INSERT INTO metadata(key,value) VALUES(?,?)',
                ('schema_version', '999')
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, 'clean-seed'):
                self.module.FleetStore(path)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get('IOTMD_MANAGEMENT_DATA')
        os.environ['IOTMD_MANAGEMENT_DATA'] = self.temp.name
        path = (
            Path(__file__).resolve().parents[1] /
            'iotmd_management/rootfs/app/management_app.py'
        )
        spec = importlib.util.spec_from_file_location('iotmd_fleet_test', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        self.module.STORE.close()
        if self.previous is None:
            os.environ.pop('IOTMD_MANAGEMENT_DATA', None)
        else:
            os.environ['IOTMD_MANAGEMENT_DATA'] = self.previous
        self.temp.cleanup()

    def policy(self):
        return {
            'format_version': 1, 'target_board': 'esp32-s3', 'policy_sequence': 1,
            'issued_at': 2000000000, 'not_before': 1999999999,
            'expires_at': 2000003600, 'target_device': 'device-1',
            'target_cohort': '',
            'maintenance': {
                'weekdays': [0, 1, 2, 3, 4, 5, 6],
                'start_minute': 120, 'duration_minutes': 60,
            },
            'updates': {
                'channel': 'alpha', 'automatic_download': True,
                'automatic_activation': False,
                'maximum_consecutive_failures': 2,
            },
            'telemetry': {
                'enabled': True, 'minimum_interval_s': 60,
                'severities': ['warning', 'error', 'critical'],
            },
            'commands': [],
        }

    def test_addon_signature_matches_published_public_key(self):
        signed = self.module.SIGNER.sign(self.policy())
        public = self.module.PUBLIC_KEY_PATH.read_bytes()
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(public[:32], 'big'),
            int.from_bytes(public[32:], 'big'),
            ec.SECP256R1(),
        ).public_key()
        signature = bytes.fromhex(signed['signature'])
        from fleet_policy import policy_message
        public_key.verify(
            encode_dss_signature(
                int.from_bytes(signature[:32], 'big'),
                int.from_bytes(signature[32:], 'big'),
            ),
            policy_message(signed),
            ec.ECDSA(hashes.SHA256()),
        )
        self.assertNotEqual(
            self.module.SIGNING_KEY_PATH.read_bytes(),
            self.module.PUBLIC_KEY_PATH.read_bytes()
        )

    def test_registered_device_response_hides_certificate_paths(self):
        store = self.module.FleetStore(Path(self.temp.name) / 'state.json')
        self.addCleanup(store.close)
        result = store.register({
            'id': 'device-1', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        self.assertNotIn('key_path', result)
        self.assertNotIn('cert_path', result)
        self.assertEqual(result['host'], 'device.local')

    def test_rollout_advances_by_cohort_and_stops_at_failure_threshold(self):
        store = self.module.FleetStore(Path(self.temp.name) / 'rollout.json')
        self.addCleanup(store.close)
        for identifier, cohort in (('canary-1', 'canary'), ('main-1', 'main')):
            store.register({
                'id': identifier, 'host': identifier + '.local', 'cohort': cohort,
                'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
                'key_path': '/ssl/client-key.pem',
            })
        rollout = store.create_rollout({
            'release_sequence': 2101, 'cohorts': ['canary', 'main'],
            'maximum_failures': 1,
        })
        store.record_rollout_result(rollout['id'], 'canary-1', 'complete')
        advanced = store.advance_rollout(rollout['id'])
        self.assertEqual(advanced['cohort_index'], 1)
        stopped = store.record_rollout_result(
            rollout['id'], 'main-1', 'failed', 'health gate failed'
        )
        self.assertEqual(stopped['status'], 'stopped')
        with self.assertRaisesRegex(ValueError, 'stopped'):
            store.advance_rollout(rollout['id'])

    def test_sqlite_repository_persists_inventory_without_exposing_keys(self):
        path = Path(self.temp.name) / 'fleet.db'
        store = self.module.FleetStore(path)
        store.register({
            'id': 'device-1', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        store.record_poll(
            'device-1', {'device': {'application_version': '2.0.0'}},
            {'status': 'healthy'},
            {'cursor': 4, 'events': [{'id': 4, 'kind': 'boot'}]},
        )
        store.close()

        restored = self.module.FleetStore(path)
        self.addCleanup(restored.close)
        device = restored.get_device('device-1')
        self.assertEqual(device['inventory']['device']['application_version'], '2.0.0')
        self.assertNotIn('key_path', device)
        self.assertEqual(restored.list_events()[0]['event']['kind'], 'boot')
        self.assertEqual(path.read_bytes()[:16], b'SQLite format 3\x00')

    def test_durable_jobs_are_idempotent_and_retry_with_backoff(self):
        now = [1000]
        store = self.module.FleetStore(
            Path(self.temp.name) / 'jobs.db', now=lambda: now[0]
        )
        self.addCleanup(store.close)
        first = store.enqueue_job(
            'poll', 'device-1', idempotency_key='poll-device-1-slot-1'
        )
        duplicate = store.enqueue_job(
            'poll', 'device-1', idempotency_key='poll-device-1-slot-1'
        )
        self.assertEqual(first['id'], duplicate['id'])
        claimed = store.claim_job()
        self.assertEqual(claimed['status'], 'running')
        store.fail_job(claimed['id'], 'network unavailable')
        self.assertIsNone(store.claim_job())
        now[0] += 2
        self.assertEqual(store.claim_job()['attempts'], 2)


if __name__ == '__main__':
    unittest.main()
