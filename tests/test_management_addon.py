import importlib.util
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature,
)


class FleetAddonTests(unittest.TestCase):
    def test_home_assistant_repository_layout_is_installable(self):
        root = Path(__file__).resolve().parents[1]
        repository = (root / 'repository.yaml').read_text(encoding='utf-8')
        addon = (root / 'iot_md_management/config.yaml').read_text(encoding='utf-8')

        self.assertIn('name: Home Assistant IoT MD Management Suite', repository)
        self.assertIn(
            'url: https://github.com/IanW6374/HA-IoT-MD-Management-Suite',
            repository,
        )
        self.assertIn('name: IoT MD Management Suite', addon)
        self.assertIn('version: 2.2.1', addon)
        self.assertIn('slug: iot_md_management', addon)
        self.assertIn('8443/tcp: 8443', addon)
        self.assertIn('github_sync_enabled: false', addon)
        self.assertIn('github_sync_enabled: bool', addon)
        self.assertIn(
            'release_base_url: https://iotmd-update.home.arpa:8443', addon
        )
        self.assertTrue((root / 'iot_md_management/Dockerfile').is_file())
        self.assertTrue((root / 'iot_md_management/translations/en.yaml').is_file())

    def test_ingress_uses_shared_iot_brand_shell(self):
        self.assertIn('<header class="topbar">', self.module.HTML)
        self.assertIn('<span class="brand-mark">MD</span><span>IoT MD Management Suite</span>', self.module.HTML)
        self.assertIn('<nav aria-label="Primary">', self.module.HTML)
        self.assertIn('Verified releases', self.module.HTML)
        self.assertIn('Management Suite verification key', self.module.HTML)
        self.assertIn('class="release-grid"', self.module.HTML)
        self.assertIn('class="release-channel"', self.module.HTML)
        self.assertIn('>Not promoted</option>', self.module.HTML)
        self.assertIn('setReleaseChannel(this)', self.module.HTML)
        self.assertNotIn('Promote stable', self.module.HTML)
        self.assertNotIn('Promote beta', self.module.HTML)

    def test_portal_sections_have_distinct_routes_and_active_tabs(self):
        self.assertEqual(
            set(self.module.PORTAL_PAGES),
            {'/', '/releases', '/devices', '/policy', '/rollouts', '/settings'},
        )
        settings = self.module.render_portal('settings').decode()
        self.assertIn('<body data-page="settings">', settings)
        self.assertIn('data-page-link="settings" href="settings"', settings)
        self.assertIn('data-page-section="settings"', settings)
        self.assertNotIn('__GITHUB_REPOSITORY__', settings)
        self.assertIn('IanW6374/IoT-Modular-Device', settings)

    def test_github_synchronization_is_explicitly_enabled(self):
        self.assertFalse(self.module.RELEASE_SYNC_STATE['enabled'])
        with self.assertRaisesRegex(ValueError, 'disabled in add-on settings'):
            self.module.start_release_sync()

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
        self.previous = os.environ.get('IOT_MD_MANAGEMENT_DATA')
        self.previous_release_root = os.environ.get('IOT_MD_RELEASE_ROOT')
        os.environ['IOT_MD_MANAGEMENT_DATA'] = self.temp.name
        os.environ['IOT_MD_RELEASE_ROOT'] = str(Path(self.temp.name) / 'releases')
        path = (
            Path(__file__).resolve().parents[1] /
            'iot_md_management/rootfs/app/management_app.py'
        )
        spec = importlib.util.spec_from_file_location('iot_md_fleet_test', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        self.module.STORE.close()
        if self.previous is None:
            os.environ.pop('IOT_MD_MANAGEMENT_DATA', None)
        else:
            os.environ['IOT_MD_MANAGEMENT_DATA'] = self.previous
        if self.previous_release_root is None:
            os.environ.pop('IOT_MD_RELEASE_ROOT', None)
        else:
            os.environ['IOT_MD_RELEASE_ROOT'] = self.previous_release_root
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

    def test_verified_artifacts_can_be_promoted_to_format_3_catalog(self):
        from release_catalog import (
            ArtifactVerifier, CatalogSigner, P256_ORDER, ReleaseCatalog,
            signed_message,
        )
        root = Path(self.temp.name) / 'catalog-test'
        root.mkdir()
        update_private = ec.generate_private_key(ec.SECP256R1())
        numbers = update_private.public_key().public_numbers()
        update_public_path = root / 'update-public.bin'
        update_public_path.write_bytes(
            numbers.x.to_bytes(32, 'big') + numbers.y.to_bytes(32, 'big')
        )

        def sign_manifest(kind, manifest):
            value = dict(manifest)
            value['signature_scheme'] = 'ecdsa-p256-sha256'
            der = update_private.sign(
                signed_message(kind, value), ec.ECDSA(hashes.SHA256())
            )
            r, s = decode_dss_signature(der)
            if s > P256_ORDER // 2:
                s = P256_ORDER - s
            value['signature'] = (
                r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
            ).hex()
            return value

        revision = '1' * 40
        app_payload = b'IoTMD_SOURCE_REVISION:' + revision.encode()
        app_manifest = sign_manifest('iotapp', {
            'format_version': 6, 'target_board': 'esp32-s3',
            'min_recovery_api': 6, 'max_recovery_api': 6,
            'version': '2.3.0', 'release_sequence': 23000,
            'minimum_core_api': 9, 'minimum_config_api': 3,
            'maximum_config_api': 3,
            'components': {'runtime': 60, 'modules': {}},
            'files': [{
                'path': 'iotmd.py', 'size': len(app_payload),
                'sha256': hashlib.sha256(app_payload).hexdigest(),
            }],
        })
        core_payload = b'\xe9IoTMD_SOURCE_REVISION:' + revision.encode()
        core_manifest = sign_manifest('iotcore', {
            'format_version': 6, 'target_board': 'esp32-s3',
            'version': '2.3.0', 'release_sequence': 23000,
            'minimum_core_api': 9, 'size': len(core_payload),
            'sha256': hashlib.sha256(core_payload).hexdigest(),
        })

        def bundle(name, magic, manifest, payload):
            encoded = json.dumps(manifest, separators=(',', ':')).encode()
            path = root / 'site' / 'bundles' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(magic + len(encoded).to_bytes(4, 'big') + encoded + payload)
            return path

        app = bundle('application-2.3.0.iotapp', b'IOTA1\n', app_manifest, app_payload)
        core = bundle('iotmd-core-2.3.0.iotcore', b'IOTC1\n', core_manifest, core_payload)
        verifier = ArtifactVerifier(update_public_path)
        app_details = verifier.verify(app)
        core_details = verifier.verify(core)
        signer = CatalogSigner(root / 'catalog.pem', root / 'catalog.bin')
        catalog = ReleaseCatalog(
            root / 'state.json', root / 'site', verifier, signer,
            'IanW6374/IoT-Modular-Device', 'https://updates.example:8443',
        )
        catalog.state['releases'] = [{
            'tag': 'v2.3.0', 'version': '2.3.0', 'verified': True,
            'release_sequence': 23000, 'source_revision': revision,
            'published_at': '2026-08-29T10:00:00Z', 'channels': [],
            'assets': {
                'application': {key: app_details[key] for key in (
                    'kind', 'version', 'release_sequence', 'size', 'sha256'
                )} | {'name': app.name},
                'firmware': {key: core_details[key] for key in (
                    'kind', 'version', 'release_sequence', 'size', 'sha256'
                )} | {'name': core.name},
            },
        }]
        catalog._save()
        catalog.promote('v2.3.0', 'stable')
        document = json.loads((root / 'site/stable/latest.json').read_text())
        self.assertEqual(document['format_version'], 3)
        self.assertEqual(len(document['releases']), 2)
        self.assertEqual(document['releases'][0]['type'], 'application')
        catalog_public = root.joinpath('catalog.bin').read_bytes()
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(catalog_public[:32], 'big'),
            int.from_bytes(catalog_public[32:], 'big'),
            ec.SECP256R1(),
        ).public_key()
        signature = bytes.fromhex(document['signature'])
        public_key.verify(
            encode_dss_signature(
                int.from_bytes(signature[:32], 'big'),
                int.from_bytes(signature[32:], 'big'),
            ), signed_message('release-catalog', document),
            ec.ECDSA(hashes.SHA256()),
        )
        self.assertEqual(catalog.state['releases'][0]['channels'], ['stable'])
        catalog.promote('v2.3.0', 'beta')
        self.assertFalse((root / 'site/stable/latest.json').exists())
        self.assertTrue((root / 'site/beta/latest.json').exists())
        self.assertEqual(catalog.state['releases'][0]['channels'], ['beta'])
        catalog.promote('v2.3.0', 'none')
        self.assertFalse((root / 'site/beta/latest.json').exists())
        self.assertEqual(catalog.state['releases'][0]['channels'], [])

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
