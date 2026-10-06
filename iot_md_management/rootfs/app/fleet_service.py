"""Fleet polling, policy application, and rollout use cases."""

import http.client
import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.request


BACKUP_KDF_ITERATIONS = 120000
BACKUP_SALT_BYTES = 16


def backup_derived_key(password, salt=None):
    """Derive a backup key on the Management host, not on the device loop."""
    salt = os.urandom(BACKUP_SALT_BYTES) if salt is None else bytes(salt)
    if len(salt) != BACKUP_SALT_BYTES:
        raise ValueError('encrypted backup salt is invalid')
    key = hashlib.pbkdf2_hmac(
        'sha256', str(password).encode(), salt, BACKUP_KDF_ITERATIONS, dklen=32
    )
    return salt, key


def bounded_text(value, maximum=256):
    return str(value or '')[:maximum]


def clock_minute(value, name):
    parts = str(value or '').split(':')
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(name + ' must use HH:MM')
    hour, minute = (int(part) for part in parts)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(name + ' must be a valid time')
    return hour * 60 + minute


def maintenance_window(request):
    if 'start_time' in request or 'end_time' in request:
        start = clock_minute(request.get('start_time'), 'maintenance start')
        end = clock_minute(request.get('end_time'), 'maintenance end')
        duration = (end - start) % 1440
        # Equal start and end represents the whole day, not an empty window.
        duration = duration or 1440
    else:
        start = int(request.get('start_minute', 120))
        duration = int(request.get('duration_minutes', 120))
    if start < 0 or start > 1439:
        raise ValueError('maintenance start is outside the supported range')
    if duration < 1 or duration > 1440:
        raise ValueError('maintenance duration is outside the supported range')
    return start, duration


def device_connection_error(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError) and 'handshake' in str(reason).lower():
        return (
            'Device TLS handshake timed out before an HTTP response. '
            'The device may be busy or its TLS service unresponsive; '
            'this is not an API permission rejection.'
        )
    if isinstance(exc, http.client.RemoteDisconnected):
        return (
            'Device closed the connection before an HTTP response. Verify that '
            'the configured client certificate is enrolled on the device, is '
            'valid for client authentication, and has the read scope.'
        )
    if isinstance(exc, ssl.SSLCertVerificationError):
        return 'Device TLS certificate verification failed: ' + str(exc)
    if isinstance(exc, ssl.SSLError):
        return (
            'Mutual TLS handshake failed. Verify the CA, client certificate and '
            'client key paths: ' + str(exc)
        )
    return bounded_text(exc, 256)


class DeviceClient:
    def __init__(self, record, timeout=10):
        self.record = record
        self.timeout = int(timeout)

    def _context(self):
        context = ssl.create_default_context(cafile=self.record['ca_path'])
        context.load_cert_chain(self.record['cert_path'], self.record['key_path'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def request(self, path, method='GET', payload=None, content_type='application/json'):
        body = None if payload is None else (
            bytes(payload) if isinstance(payload, (bytes, bytearray))
            else json.dumps(payload).encode()
        )
        request = urllib.request.Request(
            'https://' + self.record['host'] + ':' + str(self.record['port']) + path,
            data=body, method=method,
            headers={'Content-Type': content_type, 'Accept': 'application/json'},
        )
        try:
            with urllib.request.urlopen(
                request, context=self._context(), timeout=self.timeout
            ) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read())
                detail = value.get('error') if isinstance(value, dict) else value
            except Exception:
                detail = str(exc)
            raise ValueError(bounded_text(detail, 256)) from None


class FleetController:
    def __init__(self, store, signer, timeout=10, now=None, tls=None):
        self.store = store
        self.signer = signer
        self.timeout = int(timeout)
        self.now = now or (lambda: int(time.time()))
        self.tls = dict(tls or {})

    def _client(self, record, timeout=None):
        settings = dict(record)
        settings.update(self.tls)
        return DeviceClient(settings, self.timeout if timeout is None else timeout)

    def create_backup(self, identifier, source='manual', retention=7):
        record = self.store.get_device(identifier, public=False)
        if not record:
            raise ValueError('device is not registered')
        if not record.get('enabled'):
            raise ValueError('device is disabled')
        password = secrets.token_urlsafe(32)
        salt, derived_key = backup_derived_key(password)
        result = self._client(record, timeout=max(self.timeout, 60)).request(
            '/api/v2/configuration/backups', 'POST', {
                'salt': salt.hex(), 'derived_key': derived_key.hex(),
            }
        )
        backup = self.store.save_backup(
            record, result.get('backup') or {}, password, source
        )
        removed = self.store.enforce_backup_retention(identifier, retention)
        self.store.record_audit(
            'backup.created', 'complete', str(backup['id']), identifier, {
                'source': source, 'size_bytes': backup['size_bytes'],
                'digest': backup['digest'], 'retention_removed': removed,
            }
        )
        return backup

    def backup_targets(self, request):
        """Resolve backup targets with the same scopes used by Deploy."""
        devices = self.store.list_devices(public=False)
        scope = str(request.get('target_scope') or '')
        if not scope and request.get('device_id'):
            scope = 'devices'
            requested = {str(request.get('device_id'))}
        else:
            requested = {
                str(value) for value in request.get('targets', ()) if str(value)
            }
        if scope == 'all':
            targets = [item['id'] for item in devices if item.get('enabled')]
        elif scope == 'cohort':
            cohorts = {
                str(value) for value in request.get('cohorts', ()) if str(value)
            }
            targets = [
                item['id'] for item in devices
                if item.get('enabled') and item.get('cohort') in cohorts
            ]
        elif scope == 'devices':
            targets = [
                item['id'] for item in devices
                if item.get('enabled') and item['id'] in requested
            ]
            unavailable = requested - set(targets)
            if unavailable:
                raise ValueError(
                    'backup contains an unavailable device: ' +
                    sorted(unavailable)[0]
                )
        else:
            raise ValueError('backup target scope is invalid')
        if not targets:
            raise ValueError('choose at least one available backup target')
        return targets

    def preview_backup_restore(self, backup_id, target_id, sections=None):
        backup = self.store.get_backup(backup_id, include_payload=True)
        if not backup:
            raise ValueError('backup does not exist')
        target = self.store.get_device(target_id, public=False)
        if not target:
            raise ValueError('target device is not registered')
        selected = sections or [
            'credentials', 'module_settings', 'certificates_and_trust'
        ]
        try:
            salt = bytes.fromhex(str(backup['envelope'].get('salt') or ''))
        except (TypeError, ValueError):
            raise ValueError('encrypted backup salt is invalid') from None
        _salt, derived_key = backup_derived_key(backup['password'], salt)
        result = self._client(target, timeout=max(self.timeout, 60)).request(
            '/api/v2/configuration/backups/preview', 'POST', {
                'backup': backup['envelope'],
                'derived_key': derived_key.hex(),
                'sections': selected,
            }
        )
        preview = result.get('preview') or {}
        preview.update({
            'backup_id': backup['id'],
            'source_device_id': backup['device_id'],
            'source_device_name': backup['device_name'],
            'target_device_id': target_id,
            'cross_device': backup['device_id'] != target_id,
            'sections': selected,
        })
        return preview

    def restore_backup(self, backup_id, target_id, confirmation, sections=None):
        expected = 'restore ' + str(target_id)
        if str(confirmation or '').strip().lower() != expected.lower():
            raise ValueError('type "' + expected + '" to confirm complete restore')
        preview = self.preview_backup_restore(backup_id, target_id, sections)
        target = self.store.get_device(target_id, public=False)
        result = self._client(target, timeout=max(self.timeout, 60)).request(
            '/api/v2/configuration/backups/apply', 'POST', {
                'token': preview.get('token', '')
            }
        )
        self.store.record_audit(
            'backup.restored', 'complete', str(backup_id), target_id, {
                'source_device_id': preview['source_device_id'],
                'sections': preview['sections'],
                'change_count': preview.get('change_count', 0),
                'cross_device': preview['cross_device'],
            }
        )
        return {'preview': preview, 'result': result}

    def poll_device(self, identifier):
        record = self.store.get_device(identifier, public=False)
        if not record:
            raise ValueError('device is not registered')
        if not record.get('enabled'):
            return self.store.get_device(identifier)
        cursor = int(record.get('event_cursor', 0))
        client = self._client(record)
        try:
            inventory = client.request('/api/v2/device/inventory')
            configuration = client.request('/api/v2/configuration')
            if (configuration.get('configuration') or {}).get(
                'network_trial_confirmation_ready'
            ):
                confirmation = client.request(
                    '/api/v2/configuration/network/confirm', 'POST', {}
                )
                if confirmation.get('confirmed'):
                    configuration['configuration']['network_trial_pending'] = False
            inventory['configuration'] = configuration.get('configuration') or {}
            health = client.request('/api/v2/health')
            events = client.request(
                '/api/v2/events?cursor=' + str(cursor) + '&limit=64'
            )
        except Exception as exc:
            self.store.set_device_error(identifier, device_connection_error(exc))
            self._mark_immediate_install_restart(identifier)
            return self.store.get_device(identifier)
        self.store.record_poll(identifier, inventory, health, events)
        record = self.store.get_device(identifier)
        self._reconcile_deployments(identifier, record)
        return record

    @staticmethod
    def _update_installed(record, update):
        device = (record.get('inventory') or {}).get('device') or {}
        sequence = int(update.get('release_sequence', 0))
        release_type = update.get('release_type', '')
        application = int(device.get('release_sequence', 0) or 0)
        firmware = int(device.get('firmware_release_sequence', 0) or 0)
        progress = device.get('update_progress') or {}
        if any(progress.get(name) in ('trial', 'activating', 'committing') for name in (
                'application_status', 'firmware_status', 'universal_status')):
            return False
        if release_type == 'application':
            return application >= sequence
        if release_type == 'firmware':
            return firmware >= sequence
        return release_type == 'universal' and application >= sequence and firmware >= sequence

    def _mark_immediate_install_restart(self, identifier):
        """Keep fast install-now reboots from looking stuck in staging."""
        if not hasattr(self.store, 'list_deployments'):
            return
        for deployment in self.store.list_deployments():
            if (
                deployment.get('activation') != 'now' or
                identifier not in deployment.get('targets', ())
            ):
                continue
            current = (deployment.get('results', {}).get(identifier, {}) or {}).get(
                'status', ''
            )
            if current in ('scheduled', 'installing'):
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'installing',
                    'Device unavailable; waiting to confirm the installed version'
                )

    def _reconcile_deployments(self, identifier, record):
        if not hasattr(self.store, 'list_deployments'):
            return
        fleet = record.get('fleet') or {}
        for deployment in self.store.list_deployments():
            if identifier not in deployment['targets']:
                continue
            current = deployment['results'].get(identifier, {}).get('status', '')
            if current in ('complete', 'failed') or (
                current == 'staged' and deployment.get('activation') == 'stage'
            ):
                continue
            update = deployment.get('update') or {}
            progress = ((record.get('inventory') or {}).get('device') or {}).get('update_progress') or {}
            progress = progress if (
                int(progress.get('release_sequence', 0) or 0) == int(update.get('release_sequence', 0)) and
                progress.get('type') == update.get('release_type')
            ) else {}
            if not update:
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'complete', 'Profile applied'
                )
                continue
            if self._update_installed(record, update):
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'complete',
                    (update.get('version') or 'Update') + ' installed',
                    progress={'completed': ['queued', 'inspect', 'core_write',
                        'core_verify', 'application_download', 'application_verify',
                        'pair', 'install', 'complete'], 'phase': 'complete'}
                )
                continue
            sequence = int(update.get('release_sequence', 0))
            release_type = update.get('release_type', '')
            policy_commands = (fleet.get('policy') or {}).get('commands') or []
            matching = [
                command for command in policy_commands
                if int(command.get('release_sequence', 0)) == sequence and
                (not command.get('release_type') or
                 command.get('release_type') == release_type)
            ]
            pending = [
                command for command in (fleet.get('pending_commands') or [])
                if int(command.get('release_sequence', 0)) == sequence and
                (not command.get('release_type') or
                 command.get('release_type') == release_type)
            ]
            if matching and (
                fleet.get('rollout_paused') or fleet.get('command_chain_failed')
            ):
                detail = (fleet.get('last_result') or {}).get(
                    'detail', 'The device stopped this deployment.'
                )
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'failed', detail, progress=progress
                )
                continue
            if pending:
                action = pending[0].get('action', '')
                status = {
                    'check-update': 'checking',
                    'download-update': 'staging',
                    'activate-update': (
                        'installing' if fleet.get('within_maintenance_window')
                        else 'scheduled'
                    ),
                }.get(action, 'active')
                detail = {
                    'checking': 'Checking update compatibility',
                    'staging': 'Downloading and verifying update',
                    'installing': 'Installing update',
                    'scheduled': 'Staged; waiting for the device update schedule',
                    'active': 'Deployment active',
                }[status]
                if action == 'download-update' and progress.get('phase'):
                    detail = {
                        'core_write': 'Downloading and writing core firmware',
                        'core_verify': 'Verifying core firmware',
                        'application_download': 'Downloading application',
                        'application_verify': 'Verifying and staging application',
                        'pair': 'Pairing verified components',
                    }.get(progress['phase'], detail)
                if action == 'activate-update':
                    progress = dict(progress)
                    progress['completed'] = ['queued', 'inspect', 'core_write',
                        'core_verify', 'application_download', 'application_verify', 'pair']
                self.store.set_deployment_target(
                    deployment['id'], identifier, status, detail, progress=progress
                )
                continue
            if matching and deployment['activation'] == 'stage':
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'staged',
                    'Update staged for later activation', progress={'phase': 'pair',
                        'completed': ['queued', 'inspect', 'core_write', 'core_verify',
                            'application_download', 'application_verify', 'pair']}
                )
                continue
            if matching:
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'installing',
                    'Waiting for core and application confirmation' if release_type == 'universal'
                    else 'Waiting to confirm the installed version', progress=progress
                )
                completed_at = int((fleet.get('last_result') or {}).get('time', 0) or 0)
                returned_at = int(record.get('last_seen', 0) or 0)
                trial_active = any(progress.get(name) in ('trial', 'activating', 'committing')
                    for name in ('application_status', 'firmware_status', 'universal_status'))
                if completed_at and returned_at > completed_at + 30 and not trial_active:
                    self.store.set_deployment_target(
                        deployment['id'], identifier, 'failed',
                        'Device returned on its previous version'
                    )

    @staticmethod
    def _device_schedule(record):
        configuration = (record.get('inventory') or {}).get('configuration') or {}
        schedule = configuration.get('automatic_updates') or {}
        cadence = str(schedule.get('schedule') or 'disabled')
        check_time = str(schedule.get('time') or '')
        if cadence not in ('daily', 'weekly'):
            raise ValueError(
                'The device automatic update schedule is disabled; choose Stage only '
                'or Install now, or configure a device schedule first.'
            )
        start = clock_minute(check_time, 'device automatic update time')
        weekdays = list(range(7)) if cadence == 'daily' else [
            int(schedule.get('weekday', 0))
        ]
        if any(day < 0 or day > 6 for day in weekdays):
            raise ValueError('device automatic update weekday is invalid')
        return {
            'weekdays': weekdays,
            'start_minute': start,
            'duration_minutes': 60,
            'label': cadence.title() + ' at ' + check_time,
        }

    def create_deployment(self, request):
        scope = str(request.get('target_scope') or 'devices')
        devices = self.store.list_devices(public=False)
        if scope == 'all':
            targets = [item['id'] for item in devices if item.get('enabled')]
        elif scope == 'cohort':
            cohorts = {
                str(value) for value in request.get('cohorts', ()) if str(value)
            }
            targets = [
                item['id'] for item in devices
                if item.get('enabled') and item.get('cohort') in cohorts
            ]
        elif scope == 'devices':
            requested = {
                str(value) for value in request.get('targets', ()) if str(value)
            }
            targets = [
                item['id'] for item in devices
                if item.get('enabled') and item['id'] in requested
            ]
            if targets != [item['id'] for item in devices if item['id'] in requested]:
                unavailable = requested - set(targets)
                if unavailable:
                    raise ValueError(
                        'deployment contains an unavailable device: ' +
                        sorted(unavailable)[0]
                    )
        else:
            raise ValueError('deployment target scope is invalid')
        if request.get('update') and str(
            request.get('activation') or 'schedule'
        ) == 'schedule':
            # Validate every target before recording/enqueueing anything. This
            # prevents a combined profile/update deployment being only partly
            # applied before a missing device schedule is discovered.
            records = {item['id']: item for item in devices}
            for target in targets:
                try:
                    self._device_schedule(records[target])
                except ValueError as exc:
                    raise ValueError(target + ': ' + str(exc)) from None
        deployment = self.store.create_deployment(request, targets)
        for target in targets:
            self.store.enqueue_job(
                'deployment', deployment['id'],
                payload={'device_id': target},
                idempotency_key='deployment:' + deployment['id'] + ':' + target,
            )
        return deployment

    def execute_deployment_target(self, deployment_id, identifier):
        deployment = self.store.get_deployment(deployment_id)
        if not deployment:
            raise ValueError('deployment does not exist')
        self.store.set_deployment_target(
            deployment_id, identifier, 'running', 'Connecting to device'
        )
        try:
            if deployment.get('profile_name'):
                profile = self.store.get_profile(
                    deployment['profile_name'], include_secrets=True
                )
                if not profile:
                    raise ValueError('configuration profile no longer exists')
                selected = deployment.get('profile_fields') or []
                if selected:
                    available = set(profile.get('settings', ())) | set(
                        profile.get('secrets', ())
                    )
                    unknown = set(selected) - available
                    if unknown:
                        raise ValueError(
                            'selected profile field is unavailable: ' +
                            sorted(unknown)[0]
                        )
                    profile = dict(profile)
                    profile['settings'] = {
                        name: value for name, value in profile['settings'].items()
                        if name in selected
                    }
                    profile['secrets'] = {
                        name: value for name, value in profile['secrets'].items()
                        if name in selected
                    }
                self.apply_profile(identifier, profile)
                self.store.record_audit(
                    'profile.applied', 'complete', deployment_id, identifier,
                    {
                        'profile_name': profile['name'],
                        'profile_fields': selected,
                    }
                )
            update = deployment.get('update') or {}
            if not update:
                return self.store.set_deployment_target(
                    deployment_id, identifier, 'complete', 'Profile applied'
                )
            record = self.store.get_device(identifier, public=False)
            activation = deployment['activation']
            if activation == 'schedule':
                window = self._device_schedule(record)
            else:
                window = {
                    'weekdays': list(range(7)), 'start_minute': 0,
                    'duration_minutes': 1440,
                    'label': 'Administrator override' if activation == 'now'
                    else 'Staging only',
                }
            actions = ['check-update', 'download-update']
            if activation != 'stage':
                actions.append('activate-update')
            self.store.set_deployment_target(
                deployment_id, identifier, 'checking',
                'Deployment accepted; checking update compatibility'
            )
            self.apply_policy({
                'device_id': identifier, 'channel': update.get('channel', 'alpha'),
                'weekdays': window['weekdays'],
                'start_minute': window['start_minute'],
                'duration_minutes': window['duration_minutes'],
                'automatic_download': True,
                'automatic_activation': activation != 'stage',
                'commands': [{
                    'action': action,
                    'release_sequence': update['release_sequence'],
                    'release_type': update['release_type'],
                } for action in actions],
            })
            return self.store.get_deployment(deployment_id)['results'][identifier]
        except Exception as exc:
            self.store.set_deployment_target(
                deployment_id, identifier, 'failed', bounded_text(exc)
            )
            raise

    def apply_profile(self, identifier, profile):
        record = self.store.get_device(identifier, public=False)
        if not record:
            raise ValueError('device is not registered')
        client = self._client(record)
        protected = dict(profile.get('secrets') or {})
        certificate_kinds = {
            'certificate_mqtt_ca': 'mqtt-ca',
            'certificate_release_ca': 'release-ca',
            'certificate_syslog_ca': 'syslog-ca',
            'certificate_portal': 'portal-cert',
            'certificate_portal_key': 'portal-key',
            'certificate_api_server': 'api-server-cert',
            'certificate_api_server_key': 'api-server-key',
            'certificate_api_client_ca': 'api-client-ca',
            'management_suite_key': 'management-suite-key',
        }
        certificate_values = {
            certificate_kinds[name]: base64.b64decode(protected.pop(name))
            for name in certificate_kinds if protected.get(name)
        }
        device_profile = dict(profile)
        device_profile.pop('profile_type', None)
        device_profile.pop('updated_at', None)
        device_profile['secrets'] = protected
        if device_profile.get('settings') or protected:
            result = client.request(
                '/api/v2/configuration/profile', 'POST', device_profile
            )
        else:
            result = {'accepted': True, 'profile': {'applied_settings': []}}
        for kind, payload in certificate_values.items():
            client.request(
                '/api/v2/configuration/certificates/' + kind, 'POST', payload,
                'application/octet-stream'
            )
        if certificate_values:
            result['certificates'] = client.request(
                '/api/v2/configuration/certificates/apply', 'POST', {}
            )
        if (result.get('profile') or {}).get('network_trial_pending'):
            result['restart'] = client.request(
                '/api/v2/configuration/restart', 'POST', {}
            )
        self.poll_device(identifier)
        return result

    def apply_policy(self, request):
        now = self.now()
        start_minute, duration_minutes = maintenance_window(request)
        target = bounded_text(request.get('device_id'), 64)
        record = self.store.get_device(target, public=False)
        if not record:
            raise ValueError('device is not registered')
        if record.get('enabled'):
            self.poll_device(target)
            record = self.store.get_device(target, public=False)
        if record.get('last_error'):
            raise ValueError(
                'Cannot verify the device Management signing identity: ' +
                bounded_text(record.get('last_error'), 192)
            )
        device_target = str(
            (record.get('inventory') or {}).get('device', {}).get('device_id') or
            (record.get('fleet') or {}).get('device_id') or ''
        )
        if not device_target:
            self.poll_device(target)
            record = self.store.get_device(target, public=False)
            device_target = str(
                (record.get('inventory') or {}).get('device', {}).get('device_id') or
                (record.get('fleet') or {}).get('device_id') or ''
            )
        if not device_target:
            raise ValueError(
                'device identity is unavailable; complete a successful poll first'
            )
        device_key_fingerprint = str(
            ((record.get('inventory') or {}).get('fleet') or {}).get(
                'verification_key_fingerprint', ''
            )
        ).lower()
        signer_fingerprint = getattr(self.signer, 'fingerprint', lambda: '')()
        if signer_fingerprint and not device_key_fingerprint:
            raise ValueError(
                'The device did not report its active Management signing-key '
                'fingerprint. Manually update the device to a compatible release, '
                'refresh it, and retry.'
            )
        if (
            device_key_fingerprint and signer_fingerprint and
            device_key_fingerprint != signer_fingerprint
        ):
            raise ValueError(
                'Management signing identity mismatch. Device trusts ' +
                device_key_fingerprint + '; Management uses ' +
                signer_fingerprint + '. Replace the device Management Suite '
                'signing key before deploying.'
            )
        requested_commands = request.get('commands')
        if requested_commands is None:
            command = request.get('command') or None
            requested_commands = [] if not command else [command]
        if not isinstance(requested_commands, list) or len(requested_commands) > 16:
            raise ValueError('deployment commands are invalid')
        commands = [{
            'id': bounded_text(command.get('id') or os.urandom(8).hex(), 64),
            'action': command.get('action', 'check-update'),
            'release_sequence': int(command.get('release_sequence', 0)),
            'release_type': str(command.get('release_type', '') or ''),
        } for command in requested_commands]
        if any(command['release_type'] not in (
                '', 'application', 'firmware', 'universal')
               for command in commands):
            raise ValueError('deployment update type is invalid')
        policy = {
            'format_version': 2,
            'target_board': 'esp32-s3',
            'policy_sequence': self.store.next_policy_sequence(),
            'issued_at': now - 5, 'not_before': now - 5,
            'expires_at': now + int(request.get('valid_for_s', 86400)),
            'target_device': device_target, 'target_cohort': '',
            'maintenance': {
                'weekdays': request.get('weekdays', [0, 1, 2, 3, 4, 5, 6]),
                'start_minute': start_minute,
                'duration_minutes': duration_minutes,
            },
            'updates': {
                'channel': request.get('channel', 'alpha'),
                'automatic_download': bool(request.get('automatic_download', False)),
                'automatic_activation': bool(request.get('automatic_activation', False)),
                'maximum_consecutive_failures': int(request.get('maximum_failures', 2)),
            },
            'telemetry': {
                'enabled': bool(request.get('telemetry_enabled', True)),
                'minimum_interval_s': int(request.get('telemetry_interval_s', 60)),
                'severities': request.get(
                    'severities', ['warning', 'error', 'critical']
                ),
            },
            'commands': commands,
        }
        signed = self.signer.sign(policy)
        try:
            result = self._client(record, max(30, self.timeout)).request(
                '/api/v2/fleet/policy', 'POST', signed
            )
        except ValueError as exc:
            if 'fleet policy signature verification failed' in str(exc):
                raise ValueError(
                    'The device reports the expected Management signing identity but '
                    'its core rejected the format-2 fleet policy signature. Install a '
                    'current universal/core update that supports typed fleet commands, '
                    'then refresh the device and retry.'
                ) from None
            raise
        self.poll_device(target)
        return result

    def dispatch_rollout(self, identifier):
        rollout = self.store.get_rollout(identifier)
        if not rollout:
            raise ValueError('rollout does not exist')
        if rollout['status'] != 'active':
            raise ValueError('rollout is not active')
        cohort = rollout['cohorts'][rollout['cohort_index']]
        targets = [
            value['id'] for value in self.store.list_devices(public=False)
            if value.get('enabled') and value.get('cohort') == cohort
        ]
        results = {}
        for device_id in targets:
            try:
                results[device_id] = self.apply_policy({
                    'device_id': device_id, 'channel': rollout['channel'],
                    'automatic_download': True, 'automatic_activation': True,
                    'maximum_failures': rollout['maximum_failures'],
                    'commands': [
                        {
                            'action': action,
                            'release_sequence': rollout['release_sequence'],
                            'release_type': rollout.get('release_type', ''),
                        }
                        for action in ('check-update', 'download-update')
                    ],
                })
            except Exception as exc:
                results[device_id] = {'error': bounded_text(exc)}
                self.store.record_rollout_result(
                    identifier, device_id, 'failed', str(exc)
                )
                if self.store.get_rollout(identifier)['status'] == 'stopped':
                    break
        return {
            'rollout': self.store.get_rollout(identifier),
            'dispatch': results,
        }
