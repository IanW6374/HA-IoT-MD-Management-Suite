import importlib.util
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertIn('version: 2.4.2', addon)
        self.assertIn('request_timeout_s: 30', addon)
        self.assertIn('slug: iot_md_management', addon)
        self.assertIn('8443/tcp: 8443', addon)
        self.assertIn('github_sync_enabled: false', addon)
        self.assertIn('github_sync_enabled: bool', addon)
        self.assertIn(
            'release_base_url: https://iot-upgrade.home.arpa:8443', addon
        )
        self.assertTrue((root / 'iot_md_management/Dockerfile').is_file())
        self.assertTrue((root / 'iot_md_management/translations/en.yaml').is_file())
        nginx = (root / 'iot_md_management/rootfs/etc/nginx/nginx.conf.template').read_text()
        self.assertIn('(latest|versions)[.]json', nginx)

    def test_ingress_uses_shared_iot_brand_shell(self):
        self.assertIn('<header class="topbar">', self.module.HTML)
        self.assertIn('<span class="brand-mark">IoT<br>MD</span>', self.module.HTML)
        self.assertIn('<span>IoT MD Management Suite</span>', self.module.HTML)
        self.assertIn('<nav aria-label="Primary">', self.module.HTML)
        self.assertIn('Verified releases', self.module.HTML)
        self.assertIn('Management trust', self.module.HTML)
        self.assertIn('data-page-link="activity"', self.module.HTML)
        self.assertIn('class="release-channel"', self.module.HTML)
        self.assertIn('>Not promoted</option>', self.module.HTML)
        self.assertIn('>Alpha</option>', self.module.HTML)
        self.assertIn('setReleaseChannel(this)', self.module.HTML)
        self.assertNotIn('Promote stable', self.module.HTML)
        self.assertNotIn('Promote beta', self.module.HTML)
        settings = self.module.render_portal('settings').decode()
        expected = hashlib.sha256(self.module.PUBLIC_KEY_PATH.read_bytes()).hexdigest()
        self.assertIn(expected, settings)
        self.assertNotIn('__FLEET_KEY_FINGERPRINT__', settings)

    def test_device_enrollment_has_guidance_and_management_actions(self):
        self.assertIn('placeholder="IoT-MD-002"', self.module.HTML)
        self.assertIn('without https://', self.module.HTML)
        self.assertNotIn('placeholder="e.g.', self.module.HTML)
        self.assertNotIn('name="ca_path"', self.module.HTML)
        self.assertNotIn('name="cert_path"', self.module.HTML)
        self.assertNotIn('name="key_path"', self.module.HTML)
        self.assertIn('add-on configuration', self.module.HTML)
        self.assertIn('Management ID', self.module.HTML)
        self.assertIn('immutable device identity', self.module.HTML)
        self.assertIn('Retry connection', self.module.HTML)
        self.assertIn("button.textContent='Retrying…'", self.module.HTML)
        self.assertIn("?'Retry failed':'Connected'", self.module.HTML)
        self.assertIn('>Remove</button>', self.module.HTML)

    def test_device_api_identity_is_core_addon_configuration(self):
        root = Path(__file__).resolve().parents[1]
        addon = (root / 'iot_md_management/config.yaml').read_text()
        translation = (root / 'iot_md_management/translations/en.yaml').read_text()
        for name in (
            'device_api_ca_path', 'device_api_client_cert_path',
            'device_api_client_key_path',
        ):
            self.assertIn(name + ':', addon)
            self.assertIn(name + ':', translation)

        from fleet_service import FleetController
        controller = FleetController(None, None, tls={
            'ca_path': '/ssl/core-ca.pem',
            'cert_path': '/ssl/core-client.pem',
            'key_path': '/ssl/core-key.pem',
        })
        client = controller._client({
            'ca_path': '/ssl/legacy-ca.pem',
            'cert_path': '/ssl/legacy-client.pem',
            'key_path': '/ssl/legacy-key.pem',
        })
        self.assertEqual(client.record['ca_path'], '/ssl/core-ca.pem')
        self.assertEqual(client.record['cert_path'], '/ssl/core-client.pem')
        self.assertEqual(client.record['key_path'], '/ssl/core-key.pem')

    def test_remote_disconnect_explains_client_certificate_requirements(self):
        import http.client
        from fleet_service import device_connection_error

        detail = device_connection_error(
            http.client.RemoteDisconnected('closed without response')
        )

        self.assertIn('client certificate is enrolled', detail)
        self.assertIn('read scope', detail)

    def test_policy_targets_discovered_immutable_device_identity(self):
        from fleet_service import FleetController

        record = {
            'id': 'IoT-MD-002', 'host': 'iot-md-002.local', 'port': 8444,
            'enabled': True, 'cohort': 'default',
            'inventory': {'device': {'device_id': '3cdc755be290'}},
            'fleet': {'device_id': '3cdc755be290'},
        }

        class Store:
            def get_device(self, _identifier, public=True):
                return dict(record)

            def next_policy_sequence(self):
                return 1

        class Signer:
            signed = None

            def sign(self, policy):
                self.signed = dict(policy)
                return dict(policy, signature='signed')

        signer = Signer()
        controller = FleetController(Store(), signer, now=lambda: 2000000000)
        controller.poll_device = mock.Mock(return_value=record)
        client = mock.Mock()
        client.request.return_value = {'accepted': True}

        with mock.patch('fleet_service.DeviceClient', return_value=client) as device_client:
            controller.apply_policy({
                'device_id': 'IoT-MD-002',
                'start_time': '23:00', 'end_time': '01:30',
                'commands': [
                    {
                        'action': 'check-update', 'release_sequence': 2765,
                        'release_type': 'universal',
                    },
                    {
                        'action': 'download-update', 'release_sequence': 2765,
                        'release_type': 'universal',
                    },
                ],
            })

        self.assertEqual(device_client.call_args.args[1], 30)
        self.assertEqual(signer.signed['target_device'], '3cdc755be290')
        self.assertNotEqual(signer.signed['target_device'], 'IoT-MD-002')
        self.assertEqual(signer.signed['maintenance']['start_minute'], 1380)
        self.assertEqual(signer.signed['maintenance']['duration_minutes'], 150)
        self.assertEqual(
            [item['action'] for item in signer.signed['commands']],
            ['check-update', 'download-update'],
        )
        self.assertEqual(signer.signed['commands'][0]['release_sequence'], 2765)
        self.assertEqual(signer.signed['format_version'], 2)
        self.assertEqual(signer.signed['commands'][0]['release_type'], 'universal')
        client.request.assert_called_once()

    def test_deployment_uses_device_schedule_and_explicit_admin_override(self):
        self.assertIn('Use device schedule', self.module.HTML)
        self.assertIn('Stage only', self.module.HTML)
        self.assertIn('Install now', self.module.HTML)
        self.assertIn('audited administrator exception', self.module.HTML)
        self.assertIn('Automatic schedule disabled', self.module.HTML)
        self.assertNotIn('name="start_time"', self.module.HTML)
        self.assertNotIn('name="end_time"', self.module.HTML)
        self.assertNotIn('name="start_minute"', self.module.HTML)
        self.assertNotIn('name="duration_minutes"', self.module.HTML)

    def test_unified_deployment_workflow_hides_policy_implementation_details(self):
        self.assertIn('data-page-link="deploy"', self.module.HTML)
        self.assertIn('Deploy updates and profiles', self.module.HTML)
        self.assertIn('Selected devices', self.module.HTML)
        self.assertIn('Groups', self.module.HTML)
        self.assertIn('All enabled', self.module.HTML)
        self.assertIn('An update, a configuration profile, or both', self.module.HTML)
        self.assertIn("document.querySelector('#deployment-form>.panel>.flow')", self.module.HTML)
        self.assertIn('if(workflow)workflow.remove()', self.module.HTML)
        self.assertNotIn('aria-label="Deployment workflow"', self.module.HTML)
        self.assertIn('id="deployment-active"', self.module.HTML)
        self.assertIn('Deployment history', self.module.HTML)
        self.assertNotIn('data-page-link="policy"', self.module.HTML)
        self.assertNotIn('data-page-link="rollouts"', self.module.HTML)
        self.assertIn("api('api/deployments'", self.module.HTML)

    def test_overview_links_summary_without_duplicate_activity_panels(self):
        self.assertIn('<a class="metric" href="devices">', self.module.HTML)
        self.assertIn('<a class="metric" href="releases">', self.module.HTML)
        self.assertIn(
            '<a class="metric" href="deploy#active-deployments">',
            self.module.HTML,
        )
        self.assertIn('<a class="metric" href="activity">', self.module.HTML)
        self.assertNotIn('id="overview-deployments"', self.module.HTML)
        self.assertNotIn('id="overview-activity"', self.module.HTML)

    def test_deployment_history_keeps_progress_only_for_in_flight_work(self):
        self.assertIn('function activeDeploymentCard(deployment)', self.module.HTML)
        self.assertIn('${flowFor(deployment)}${deviceResults}', self.module.HTML)
        self.assertIn("const terminal=new Set(['complete','failed','partial','staged'])", self.module.HTML)
        self.assertIn('active.map(activeDeploymentCard)', self.module.HTML)
        self.assertIn('history.map(deploymentHistoryItem)', self.module.HTML)
        self.assertIn('class="timeline"', self.module.HTML)
        self.assertIn('deployment.targets.length>1', self.module.HTML)
        self.assertIn('device results</summary>', self.module.HTML)
        self.assertIn('function conciseDeploymentDetail(detail,version)', self.module.HTML)
        self.assertIn('function refreshDeploymentProgress()', self.module.HTML)
        self.assertIn("if(activePage==='deploy')refreshDeploymentProgress()", self.module.HTML)
        self.assertNotIn('<p>${esc(deployment.id)}', self.module.HTML)
        self.assertNotIn('${statusBadge(firstResult.status)}', self.module.HTML)

    def test_release_grid_is_fixed_and_profiles_are_grouped(self):
        self.assertIn(
            '.release-grid{grid-template-columns:repeat(4,minmax(0,1fr))}',
            self.module.HTML,
        )
        self.assertIn('class="release-fingerprint"', self.module.HTML)
        for group in (
            'Profile details', 'Time and logging', 'Home Assistant', 'MQTT',
            'Remote syslog',
        ):
            self.assertIn('<legend>' + group + '</legend>', self.module.HTML)
        self.assertIn(
            'profile_name:profile,profile_fields:profileFields,activation',
            self.module.HTML,
        )
        self.assertNotIn('<pre id="result">', self.module.HTML)

    def test_profile_picker_and_backup_device_loading_are_concise(self):
        self.assertNotIn('Profile type<select', self.module.HTML)
        self.assertIn('Use baseline set', self.module.HTML)
        self.assertIn("remove.className='badge profile-remove'", self.module.HTML)
        self.assertIn("advanced?.classList.toggle('hidden',!showAdvanced)", self.module.HTML)
        self.assertIn(
            '#profile-editor>.profile-sections>.profile-group:first-child,.profile-picker{grid-column:1/-1}',
            self.module.HTML,
        )
        self.assertIn(
            "Promise.all([api('api/backups'),api('api/devices')])",
            self.module.HTML,
        )
        self.assertIn('Promise.allSettled', self.module.HTML)
        self.assertIn('Some management data could not be loaded', self.module.HTML)
        self.assertIn("if(activePage==='profiles')", self.module.HTML)

    def test_devices_are_editable_and_profiles_are_first_class(self):
        self.assertIn('Edit device', self.module.HTML)
        self.assertIn("method:'PATCH'", self.module.HTML)
        self.assertIn('data-page-link="profiles"', self.module.HTML)
        self.assertIn('<h1>Profiles</h1>', self.module.HTML)
        self.assertIn("group('Automatic updates'", self.module.HTML)
        self.assertIn('Advanced settings and certificate deployment', self.module.HTML)
        self.assertIn('Only selected items are pushed', self.module.HTML)
        self.assertIn("text('wifi_ssid','Wi-Fi SSID')", self.module.HTML)
        self.assertIn("text('wifi_password','Wi-Fi password'", self.module.HTML)
        self.assertIn("text('mqtt_password','MQTT password'", self.module.HTML)
        self.assertIn('data-profile-include', self.module.HTML)
        self.assertIn('allowing a profile to change one entity', self.module.HTML)
        self.assertIn("file('certificate_portal','portal certificate'", self.module.HTML)
        self.assertIn('/api/v2/configuration/profile', Path(
            self.module.__file__
        ).with_name('fleet_service.py').read_text())

    def test_equal_maintenance_times_mean_all_day(self):
        from fleet_service import maintenance_window

        self.assertEqual(
            maintenance_window({'start_time': '00:00', 'end_time': '00:00'}),
            (0, 1440),
        )

    def test_policy_signer_repairs_public_key_from_persisted_identity(self):
        from fleet_policy import PolicySigner

        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / 'fleet-signing-key.pem'
            public_path = Path(directory) / 'fleet-verification-key.bin'
            PolicySigner(private_path, public_path)
            expected = public_path.read_bytes()
            public_path.write_bytes(b'outdated')

            PolicySigner(private_path, public_path)

            self.assertEqual(public_path.read_bytes(), expected)
            self.assertEqual(len(expected), 64)
            self.assertEqual(
                PolicySigner(private_path, public_path).fingerprint(),
                hashlib.sha256(expected).hexdigest(),
            )

    def test_policy_refuses_a_reported_management_identity_mismatch(self):
        from fleet_service import FleetController

        record = {
            'id': 'device-1', 'enabled': True,
            'inventory': {
                'device': {'device_id': 'immutable-device'},
                'fleet': {'verification_key_fingerprint': 'a' * 64},
            },
        }

        class Store:
            def get_device(self, _identifier, public=False):
                return record

        signer = mock.Mock()
        signer.fingerprint.return_value = 'b' * 64
        controller = FleetController(Store(), signer)
        controller.poll_device = mock.Mock(return_value=record)

        with self.assertRaisesRegex(ValueError, 'Device trusts ' + 'a' * 64):
            controller.apply_policy({'device_id': 'device-1'})

    def test_policy_refuses_to_sign_without_a_fresh_device_identity(self):
        from fleet_service import FleetController

        record = {
            'id': 'device-1', 'enabled': True,
            'inventory': {'device': {'device_id': 'immutable-device'}, 'fleet': {}},
            'last_error': '',
        }

        class Store:
            def get_device(self, _identifier, public=False):
                return record

        signer = mock.Mock()
        signer.fingerprint.return_value = 'b' * 64
        controller = FleetController(Store(), signer)
        controller.poll_device = mock.Mock(return_value=record)

        with self.assertRaisesRegex(ValueError, 'did not report its active'):
            controller.apply_policy({'device_id': 'device-1'})
        signer.sign.assert_not_called()

    def test_policy_surfaces_refresh_error_before_signing(self):
        from fleet_service import FleetController

        record = {
            'id': 'device-1', 'enabled': True,
            'inventory': {'device': {'device_id': 'immutable-device'}},
            'last_error': "'module' object has no attribute 'verification_key_fingerprint'",
        }

        class Store:
            def get_device(self, _identifier, public=False):
                return record

        signer = mock.Mock()
        controller = FleetController(Store(), signer)
        controller.poll_device = mock.Mock(return_value=record)

        with self.assertRaisesRegex(ValueError, 'Cannot verify.*verification_key_fingerprint'):
            controller.apply_policy({'device_id': 'device-1'})
        signer.sign.assert_not_called()

    def test_matching_identity_signature_failure_requires_core_update(self):
        from fleet_service import FleetController

        fingerprint = 'b' * 64
        record = {
            'id': 'device-1', 'enabled': True, 'last_error': '',
            'inventory': {
                'device': {'device_id': 'immutable-device'},
                'fleet': {'verification_key_fingerprint': fingerprint},
            },
        }

        class Store:
            def get_device(self, _identifier, public=False):
                return record

            def next_policy_sequence(self):
                return 1

        signer = mock.Mock()
        signer.fingerprint.return_value = fingerprint
        signer.sign.side_effect = lambda policy: dict(policy, signature='signed')
        controller = FleetController(Store(), signer, now=lambda: 2000000000)
        controller.poll_device = mock.Mock(return_value=record)
        client = mock.Mock()
        client.request.side_effect = ValueError(
            'fleet policy signature verification failed'
        )
        controller._client = mock.Mock(return_value=client)

        with self.assertRaisesRegex(ValueError, 'universal/core update'):
            controller.apply_policy({'device_id': 'device-1'})

    def test_portal_sections_have_distinct_routes_and_active_tabs(self):
        self.assertEqual(
            set(self.module.PORTAL_PAGES),
            {'/', '/deploy', '/deployments', '/releases', '/devices',
             '/profiles', '/activity', '/backups', '/settings'},
        )
        settings = self.module.render_portal('settings').decode()
        self.assertIn('<body data-page="settings">', settings)
        self.assertIn('data-page-link="settings" href="settings"', settings)
        self.assertIn('data-page-section="settings"', settings)
        self.assertNotIn('__GITHUB_REPOSITORY__', settings)
        self.assertIn('IanW6374/IoT-Modular-Device', settings)

    def test_portal_navigation_does_not_depend_on_javascript_for_section_visibility(self):
        self.assertIn('[data-page-section]{display:none}', self.module.HTML)
        for page in ('overview', 'deploy', 'releases', 'devices', 'profiles',
                     'activity', 'settings'):
            self.assertIn(
                f'body[data-page="{page}"] [data-page-section="{page}"]',
                self.module.HTML,
            )
        deployments = self.module.render_portal('deployments').decode()
        self.assertIn('<body data-page="deploy">', deployments)
        self.assertIn('id="deployment-history"', deployments)
        self.assertIn('id="activity-timeline"', deployments)

    def test_github_synchronization_is_explicitly_enabled(self):
        self.assertFalse(self.module.RELEASE_SYNC_STATE['enabled'])
        with self.assertRaisesRegex(ValueError, 'disabled in add-on settings'):
            self.module.start_release_sync()

    def test_release_sync_removes_github_deleted_inventory_and_assets(self):
        from release_catalog import ReleaseCatalog
        root = Path(self.temp.name) / 'sync-reconcile'
        requested = []

        def opener(request, timeout=0):
            requested.append(request.full_url)
            return io.BytesIO(b'[]')

        catalog = ReleaseCatalog(
            root / 'inventory.json', root / 'site', None, None,
            'IanW6374/IoT-Modular-Device', 'https://updates.example:8443',
            opener=opener,
        )
        files = (
            'application-1.0.iotapp', 'iotmd-core-1.0.iotcore',
            'provenance-1.0.intoto.jsonl', 'sbom-1.0.cdx.json',
        )
        for name in files:
            (root / 'site' / 'bundles' / name).write_bytes(b'old')
        (root / 'site' / 'alpha' / 'latest.json').write_text('{}')
        catalog.state['releases'] = [{
            'tag': 'v1.0.0', 'version': '1.0.0', 'verified': True,
            'release_sequence': 100, 'channels': ['alpha'],
            'assets': {
                'application': {'name': files[0]},
                'firmware': {'name': files[1]},
            },
            'provenance': files[2], 'sbom': files[3],
        }]

        result = catalog.sync()

        self.assertEqual(result['removed'], ['v1.0.0'])
        self.assertEqual(result['inventory']['releases'], [])
        self.assertIn('per_page=100', requested[0])
        for name in files:
            self.assertFalse((root / 'site' / 'bundles' / name).exists())
        self.assertFalse((root / 'site' / 'alpha' / 'latest.json').exists())

    def test_release_import_accepts_standard_intoto_jsonl_provenance(self):
        from release_catalog import ASSET_SUFFIXES
        self.assertIn('.jsonl', ASSET_SUFFIXES)
        source = Path(
            self.module.__file__
        ).with_name('release_catalog.py').read_text()
        self.assertIn('does not contain a supported update bundle', source)
        self.assertNotIn('does not contain application and core bundles', source)

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
        universal_payload = core.read_bytes() + app.read_bytes()
        universal_manifest = sign_manifest('iotuni', {
            'format_version': 3, 'target_board': 'esp32-s3',
            'version': '2.3.0', 'release_sequence': 23000,
            'firmware': {
                'version': '2.3.0', 'release_sequence': 23000,
                'size': core.stat().st_size,
                'sha256': hashlib.sha256(core.read_bytes()).hexdigest(),
            },
            'application': {
                'version': '2.3.0', 'release_sequence': 23000,
                'size': app.stat().st_size,
                'sha256': hashlib.sha256(app.read_bytes()).hexdigest(),
            },
            'activation_order': ['application', 'firmware'],
            'maintenance_required': False, 'rollback_policy': 'paired',
            'trial_timeout_s': 180,
        })
        universal = bundle(
            'universal-2.3.0.iotuni', b'IOTU1\n', universal_manifest,
            universal_payload
        )
        verifier = ArtifactVerifier(update_public_path)
        app_details = verifier.verify(app)
        core_details = verifier.verify(core)
        universal_details = verifier.verify(universal)
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
                'universal': {key: universal_details[key] for key in (
                    'kind', 'version', 'release_sequence', 'size', 'sha256'
                )} | {'name': universal.name},
            },
        }]
        catalog._save()
        catalog.promote('v2.3.0', 'stable')
        document = json.loads((root / 'site/stable/latest.json').read_text())
        inventory = json.loads((root / 'site/stable/versions.json').read_text())
        self.assertEqual(document['format_version'], 3)
        self.assertEqual(inventory['format_version'], 1)
        self.assertEqual(inventory['channel'], 'stable')
        self.assertEqual(inventory['catalogs'], [document])
        self.assertEqual(len(document['releases']), 3)
        self.assertEqual(document['type'], 'universal')
        self.assertEqual(
            [release['type'] for release in document['releases']],
            ['universal', 'application', 'firmware'],
        )
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
        (root / 'site/stable/versions.json').unlink()
        catalog = ReleaseCatalog(
            root / 'state.json', root / 'site', verifier,
            CatalogSigner(root / 'catalog.pem', root / 'catalog.bin'),
            'IanW6374/IoT-Modular-Device', 'https://updates.example:8443',
        )
        self.assertTrue((root / 'site/stable/versions.json').is_file())
        older = json.loads(json.dumps(catalog.state['releases'][0]))
        older.update({
            'tag': 'v2.2.0', 'version': '2.2.0',
            'release_sequence': 22000, 'channels': [],
        })
        catalog.state['releases'].append(older)
        catalog.promote('v2.2.0', 'stable')
        inventory = json.loads((root / 'site/stable/versions.json').read_text())
        self.assertEqual(
            [item['version'] for item in inventory['catalogs']],
            ['2.3.0', '2.2.0'],
        )
        self.assertEqual(
            json.loads((root / 'site/stable/latest.json').read_text())['version'],
            '2.3.0',
        )
        catalog.promote('v2.2.0', 'none')
        catalog.promote('v2.3.0', 'beta')
        self.assertFalse((root / 'site/stable/latest.json').exists())
        self.assertFalse((root / 'site/stable/versions.json').exists())
        self.assertTrue((root / 'site/beta/latest.json').exists())
        self.assertEqual(catalog.state['releases'][0]['channels'], ['beta'])
        catalog.promote('v2.3.0', 'alpha')
        self.assertFalse((root / 'site/beta/latest.json').exists())
        self.assertTrue((root / 'site/alpha/latest.json').exists())
        self.assertEqual(catalog.state['releases'][0]['channels'], ['alpha'])
        catalog.promote('v2.3.0', 'none')
        self.assertFalse((root / 'site/alpha/latest.json').exists())
        self.assertEqual(catalog.state['releases'][0]['channels'], [])

        application_only = json.loads(json.dumps(catalog.state['releases'][0]))
        application_only['assets'] = {
            'application': application_only['assets']['application'],
        }
        application_only['channels'] = ['alpha']
        catalog.state['releases'] = [application_only]
        catalog._write_channel('alpha')
        application_catalog = json.loads(
            (root / 'site/alpha/latest.json').read_text()
        )
        self.assertEqual(
            [item['type'] for item in application_catalog['releases']],
            ['application'],
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

    def test_registered_device_can_be_deleted_with_its_events(self):
        store = self.module.FleetStore(Path(self.temp.name) / 'delete.db')
        self.addCleanup(store.close)
        store.register({
            'id': 'device-1', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        store.record_poll(
            'device-1', {'device': {}}, {},
            {'cursor': 1, 'events': [{'id': 1, 'kind': 'boot'}]},
        )

        self.assertEqual(
            store.delete_device('device-1'),
            {'deleted': True, 'id': 'device-1'},
        )
        self.assertIsNone(store.get_device('device-1'))
        self.assertEqual(store.list_events(), [])
        with self.assertRaisesRegex(ValueError, 'not registered'):
            store.delete_device('device-1')

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
            'maximum_failures': 1, 'release_type': 'application',
        })
        self.assertEqual(rollout['release_type'], 'application')
        from fleet_service import FleetController
        controller = FleetController(store, None)
        controller.apply_policy = mock.Mock(return_value={'accepted': True})
        controller.dispatch_rollout(rollout['id'])
        dispatched = controller.apply_policy.call_args.args[0]
        self.assertEqual(
            [command['action'] for command in dispatched['commands']],
            ['check-update', 'download-update'],
        )
        self.assertEqual(
            {command['release_type'] for command in dispatched['commands']},
            {'application'},
        )
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

    def test_schema_three_upgrade_preserves_existing_fleet_data(self):
        path = Path(self.temp.name) / 'upgrade.db'
        store = self.module.FleetStore(path)
        store.register({
            'id': 'device-1', 'name': 'Retained device', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        store.close()
        connection = sqlite3.connect(path)
        connection.execute('ALTER TABLE profiles DROP COLUMN profile_type')
        connection.execute(
            "UPDATE metadata SET value='3' WHERE key='schema_version'"
        )
        connection.commit()
        connection.close()

        upgraded = self.module.FleetStore(path)
        self.addCleanup(upgraded.close)

        self.assertEqual(upgraded.get_device('device-1')['name'], 'Retained device')
        columns = {
            row['name'] for row in upgraded.connection.execute(
                'PRAGMA table_info(profiles)'
            ).fetchall()
        }
        self.assertIn('profile_type', columns)
        self.assertEqual(upgraded.metadata('schema_version'), '4')

    def test_device_cohort_edit_and_configuration_profiles_persist(self):
        path = Path(self.temp.name) / 'editable.db'
        store = self.module.FleetStore(path)
        store.register({
            'id': 'device-1', 'name': 'Original', 'host': 'device.local',
            'cohort': 'default', 'ca_path': '/ssl/ca.pem',
            'cert_path': '/ssl/client.pem', 'key_path': '/ssl/key.pem',
        })
        updated = store.update_device('device-1', {
            'name': 'Edited', 'cohort': 'canary',
        })
        self.assertEqual(updated['name'], 'Edited')
        self.assertEqual(updated['cohort'], 'canary')
        private = store.get_device('device-1', public=False)
        self.assertEqual(private['key_path'], '/ssl/key.pem')
        profile = store.save_profile({
            'format_version': 1, 'name': 'Production',
            'description': 'Common settings',
            'settings': {
                'timezone_name': 'Europe/London', 'ha_discovery': True,
                'release_channel': 'alpha',
                'release_check_schedule': 'weekly',
                'release_check_time': '03:30',
                'release_check_weekday': 6,
                'release_auto_download': True,
                'release_auto_activate': False,
            },
            'secrets': {
                'wifi_password': 'correct horse battery staple',
                'mqtt_password': 'broker-secret',
            },
        })
        self.assertEqual(profile['settings']['timezone_name'], 'Europe/London')
        self.assertEqual(profile['secrets'], {
            'wifi_password': '********', 'mqtt_password': '********',
        })
        self.assertNotIn(b'correct horse battery staple', path.read_bytes())
        self.assertNotIn(b'broker-secret', path.read_bytes())
        private_profile = store.get_profile('Production', include_secrets=True)
        self.assertEqual(
            private_profile['secrets']['mqtt_password'], 'broker-secret'
        )
        store.close()
        restored = self.module.FleetStore(path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.list_profiles()[0]['name'], 'Production')
        self.assertEqual(
            restored.list_profiles()[0]['secrets']['wifi_password'], '********'
        )
        restored.delete_profile('Production')
        self.assertEqual(restored.list_profiles(), [])

    def test_selective_profile_and_certificate_material_are_encrypted(self):
        import base64
        from configuration_profiles import normalize_profile

        certificate = base64.b64encode(b'fake-der-certificate').decode()
        profile = normalize_profile({
            'name': 'Syslog only',
            'settings': {'syslog_enabled': True},
            'secrets': {'certificate_mqtt_ca': certificate},
        })
        self.assertEqual(profile['settings'], {'syslog_enabled': True})
        saved = self.module.STORE.save_profile(profile)
        self.assertEqual(saved['secrets']['certificate_mqtt_ca'], '********')
        database = Path(self.temp.name) / 'fleet.db'
        self.assertNotIn(certificate.encode(), database.read_bytes())

    def test_complete_backup_envelope_and_recovery_password_are_retained_securely(self):
        path = Path(self.temp.name) / 'backups.db'
        store = self.module.FleetStore(path)
        self.addCleanup(store.close)
        device = store.register({
            'id': 'device-1', 'name': 'Plant room', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        device['inventory'] = {'device': {
            'application_version': '3.0.0-alpha.89',
            'firmware_version': '3.0.0-alpha.88',
        }}
        envelope = {
            'format': 'iotmd-secure-backup', 'format_version': 2,
            'ciphertext': 'encrypted-device-configuration', 'tag': 'tag',
        }

        saved = store.save_backup(
            device, envelope, 'unique-recovery-password', 'manual'
        )

        self.assertNotIn('envelope', saved)
        self.assertNotIn('password', saved)
        self.assertNotIn(b'unique-recovery-password', path.read_bytes())
        private = store.get_backup(saved['id'], include_payload=True)
        self.assertEqual(private['envelope'], envelope)
        self.assertEqual(private['password'], 'unique-recovery-password')
        self.assertEqual(private['application_version'], '3.0.0-alpha.89')

    def test_backup_restore_is_previewed_before_confirmed_apply(self):
        from fleet_service import FleetController

        store = self.module.FleetStore(Path(self.temp.name) / 'restore.db')
        self.addCleanup(store.close)
        store.register({
            'id': 'device-1', 'name': 'Source', 'host': 'source.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        source = store.get_device('device-1', public=False)
        backup = store.save_backup(source, {
            'format': 'iotmd-secure-backup', 'format_version': 2,
            'ciphertext': 'ciphertext', 'tag': 'tag',
        }, 'recovery-password', 'manual')
        client = mock.Mock()
        client.request.side_effect = [
            {'preview': {'token': 'preview-token', 'change_count': 4}},
            {'accepted': True, 'restore': 'restart required'},
        ]
        controller = FleetController(store, mock.Mock())
        controller._client = mock.Mock(return_value=client)

        with self.assertRaisesRegex(ValueError, 'type "restore device-1"'):
            controller.restore_backup(backup['id'], 'device-1', 'yes')
        result = controller.restore_backup(
            backup['id'], 'device-1', 'restore device-1'
        )

        self.assertEqual(result['preview']['change_count'], 4)
        self.assertEqual(
            client.request.call_args_list[0].args[0],
            '/api/v2/configuration/backups/preview',
        )
        self.assertEqual(
            client.request.call_args_list[1].args,
            ('/api/v2/configuration/backups/apply', 'POST', {
                'token': 'preview-token'
            }),
        )

    def test_profile_application_sends_certificates_separately(self):
        import base64
        from fleet_service import FleetController

        record = {'id': 'device-1', 'enabled': True}
        class Store:
            def get_device(self, _identifier, public=False):
                return dict(record)

        controller = FleetController(Store(), mock.Mock())
        controller.poll_device = mock.Mock(return_value=record)
        client = mock.Mock()
        client.request.return_value = {'accepted': True}
        controller._client = mock.Mock(return_value=client)
        controller.apply_profile('device-1', {
            'format_version': 1, 'name': 'Trust only', 'settings': {},
            'secrets': {
                'certificate_mqtt_ca': base64.b64encode(b'ca-data').decode(),
            },
        })
        calls = client.request.call_args_list
        self.assertEqual(
            calls[0].args[:3],
            ('/api/v2/configuration/certificates/mqtt-ca', 'POST', b'ca-data'),
        )
        self.assertEqual(
            calls[1].args[0], '/api/v2/configuration/certificates/apply'
        )

    def test_network_profile_restarts_before_management_confirmation(self):
        from fleet_service import FleetController

        record = {'id': 'device-1', 'enabled': True}
        class Store:
            def get_device(self, _identifier, public=False):
                return dict(record)

        controller = FleetController(Store(), mock.Mock())
        controller.poll_device = mock.Mock(return_value=record)
        client = mock.Mock()
        client.request.side_effect = [
            {
                'accepted': True,
                'profile': {'network_trial_pending': True},
            },
            {'accepted': True, 'restart': {'message': 'restarting'}},
        ]
        controller._client = mock.Mock(return_value=client)

        controller.apply_profile('device-1', {
            'format_version': 1, 'name': 'Wi-Fi',
            'settings': {'wifi_ssid': 'Production'}, 'secrets': {},
        })

        self.assertEqual(
            client.request.call_args_list[1].args[0],
            '/api/v2/configuration/restart',
        )

    def test_deployment_can_apply_one_item_from_a_larger_profile(self):
        from fleet_service import FleetController

        store = self.module.FleetStore(Path(self.temp.name) / 'selective.db')
        self.addCleanup(store.close)
        store.register({
            'id': 'device-1', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        store.save_profile({
            'format_version': 1, 'name': 'Production', 'description': '',
            'settings': {
                'syslog_enabled': True, 'loglevel': 'DEBUG',
            },
            'secrets': {'mqtt_password': 'secret'},
        })
        controller = FleetController(store, mock.Mock())
        controller.apply_profile = mock.Mock(return_value={'accepted': True})
        deployment = controller.create_deployment({
            'target_scope': 'devices', 'targets': ['device-1'],
            'profile_name': 'Production',
            'profile_fields': ['syslog_enabled'], 'activation': 'stage',
        })

        controller.execute_deployment_target(deployment['id'], 'device-1')

        applied = controller.apply_profile.call_args.args[1]
        self.assertEqual(applied['settings'], {'syslog_enabled': True})
        self.assertEqual(applied['secrets'], {})
        self.assertEqual(
            store.get_deployment(deployment['id'])['profile_fields'],
            ['syslog_enabled'],
        )

    def test_deployment_history_and_audit_are_durable(self):
        path = Path(self.temp.name) / 'deployment-history.db'
        store = self.module.FleetStore(path, now=lambda: 2000000000)
        for identifier in ('device-1', 'device-2'):
            store.register({
                'id': identifier, 'host': identifier + '.local',
                'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
                'key_path': '/ssl/client-key.pem',
            })
        deployment = store.create_deployment({
            'activation': 'schedule', 'profile_name': 'Production',
            'update': {
                'release_sequence': 2785, 'release_type': 'application',
                'version': '3.0.0-alpha.80', 'channel': 'alpha',
            },
        }, ['device-1', 'device-2'])
        store.set_deployment_target(
            deployment['id'], 'device-1', 'scheduled',
            'Staged; waiting for the device update schedule'
        )
        store.set_deployment_target(
            deployment['id'], 'device-2', 'failed', 'Device unavailable'
        )
        store.close()

        restored = self.module.FleetStore(path, now=lambda: 2000000001)
        self.addCleanup(restored.close)
        saved = restored.get_deployment(deployment['id'])
        self.assertEqual(saved['update']['release_sequence'], 2785)
        self.assertEqual(saved['results']['device-1']['status'], 'scheduled')
        self.assertEqual(saved['status'], 'active')
        actions = [item['action'] for item in restored.list_audit()]
        self.assertIn('deployment.created', actions)
        self.assertIn('deployment.target', actions)

    def test_scheduled_deployment_uses_each_device_automatic_update_slot(self):
        from fleet_service import FleetController

        store = self.module.FleetStore(Path(self.temp.name) / 'schedule.db')
        self.addCleanup(store.close)
        store.register({
            'id': 'device-1', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        store.record_poll('device-1', {
            'device': {
                'device_id': 'immutable-1', 'release_sequence': 2784,
                'firmware_release_sequence': 2783,
            },
            'configuration': {'automatic_updates': {
                'schedule': 'weekly', 'time': '02:30', 'weekday': 6,
            }},
            'fleet': {'device_id': 'immutable-1'},
        }, {}, {'cursor': 0, 'events': []})
        controller = FleetController(store, mock.Mock())
        controller.apply_policy = mock.Mock(return_value={'accepted': True})
        deployment = controller.create_deployment({
            'target_scope': 'devices', 'targets': ['device-1'],
            'activation': 'schedule',
            'update': {
                'release_sequence': 2785, 'release_type': 'application',
                'version': '3.0.0-alpha.80', 'channel': 'alpha',
            },
        })

        controller.execute_deployment_target(deployment['id'], 'device-1')

        request = controller.apply_policy.call_args.args[0]
        self.assertEqual(request['weekdays'], [6])
        self.assertEqual(request['start_minute'], 150)
        self.assertEqual(request['duration_minutes'], 60)
        self.assertEqual(
            [item['action'] for item in request['commands']],
            ['check-update', 'download-update', 'activate-update'],
        )
        saved = store.get_deployment(deployment['id'])
        self.assertEqual(saved['results']['device-1']['status'], 'scheduled')
        self.assertFalse(saved['administrator_override'])

    def test_install_now_is_recorded_as_all_day_admin_override(self):
        from fleet_service import FleetController

        store = self.module.FleetStore(Path(self.temp.name) / 'override.db')
        self.addCleanup(store.close)
        store.register({
            'id': 'device-1', 'host': 'device.local',
            'ca_path': '/ssl/ca.pem', 'cert_path': '/ssl/client.pem',
            'key_path': '/ssl/client-key.pem',
        })
        controller = FleetController(store, mock.Mock())
        controller.apply_policy = mock.Mock(return_value={'accepted': True})
        deployment = controller.create_deployment({
            'target_scope': 'all', 'activation': 'now',
            'update': {
                'release_sequence': 2785, 'release_type': 'application',
                'version': '3.0.0-alpha.80', 'channel': 'alpha',
            },
        })

        controller.execute_deployment_target(deployment['id'], 'device-1')

        request = controller.apply_policy.call_args.args[0]
        self.assertEqual(request['weekdays'], list(range(7)))
        self.assertEqual(request['start_minute'], 0)
        self.assertEqual(request['duration_minutes'], 1440)
        self.assertTrue(store.get_deployment(
            deployment['id']
        )['administrator_override'])

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
