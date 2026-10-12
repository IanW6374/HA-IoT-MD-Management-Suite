"""Fleet polling, policy application, and rollout use cases."""

import http.client
import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import threading
import urllib.error
import urllib.request

from progress_traces import clean_traces, copy_trace, failure_reason, retain_attempt
from update_operation_contract import reduce_operation
from mqtt_progress import tracking_settings
from operation_reporting import report_topic


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


_DEVICE_REQUEST_LOCKS = {}
_DEVICE_REQUEST_LOCK = threading.Lock()


class DeviceMutationRejected(ValueError):
    """An explicit precondition/permission rejection, not an ambiguous I/O failure."""


class DeviceReadUnavailable(RuntimeError):
    """Transient HTTP read failure; valid discovery remains reusable."""


class DeviceClient:
    def __init__(self, record, timeout=10, sequence_provider=None, request_recorder=None,
                 request_slots=None, deadline=None, read_observer=None):
        self.record = record
        self.timeout = int(timeout)
        self.api_metadata = None
        self.discovered_at = 0
        self.sequence_provider = sequence_provider
        self.request_recorder = request_recorder
        self.last_request_key = None
        self.request_slots = request_slots
        self.deadline = deadline
        self.ssl_context = None
        self.read_only = False
        self.read_observer = read_observer
        self.read_path = ''
        self.request_deadline = None
        self.read_metrics = []
        self.read_stage = ''
        self.read_bytes = 0
        self.read_timings = {}
        self.read_stage_started = time.monotonic()
        self.response_limit = 16 * 1024 * 1024
        with _DEVICE_REQUEST_LOCK:
            self.request_lock = _DEVICE_REQUEST_LOCKS.setdefault(
                (str(record.get('host', '')).lower(), int(record.get('port', 8444))), threading.RLock())

    def _context(self):
        if self.ssl_context is not None:
            return self.ssl_context
        context = ssl.create_default_context(cafile=self.record['ca_path'])
        # Shared device HTTPS identities may be publicly issued; private roots
        # remain trusted for locally issued identities. Host verification stays on.
        context.load_default_certs()
        context.load_cert_chain(self.record['cert_path'], self.record['key_path'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ssl_context = context
        return context

    def _remaining(self):
        remaining = self.timeout if self.deadline is None else min(
            self.timeout, self.deadline - time.monotonic())
        if self.request_deadline is not None:
            remaining = min(remaining, self.request_deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('Device request deadline exceeded')
        return remaining

    def request(self, path, method='GET', payload=None, content_type='application/json'):
        started = time.monotonic()
        self.read_stage, self.read_bytes = '', 0
        self.read_timings, self.read_stage_started = {}, started
        self._read_stage('reservation')
        self.read_path = path.split('?', 1)[0]
        error = ''
        failure = ''
        self.read_http_status = 0
        budget_ms = 0
        try:
            budget_ms = round(self._remaining() * 1000)
            return self._reserved_request(path, method, payload, content_type)
        except Exception as exc:
            error = device_connection_error(exc)
            failure = failure_reason(exc)
            if method == 'GET':
                self._observe_read(self.read_path, error)
            raise
        finally:
            if method == 'GET':
                stage = self.read_stage
                self._read_stage('finished')
                self.read_metrics.append({'path': self.read_path, 'stage': self.read_stage,
                    'start_offset_ms': round((started - getattr(self, 'metrics_started_at', started)) * 1000),
                    'budget_ms': budget_ms, 'http_status': self.read_http_status, 'failure': failure,
                    'duration_ms': round((time.monotonic() - started) * 1000),
                    'response_bytes': self.read_bytes, 'error': bounded_text(error),
                    'stage_ms': dict(self.read_timings)})
                self.read_metrics[-1]['stage'] = stage
                self.read_metrics = self.read_metrics[-6:]

    def _read_stage(self, stage):
        now = time.monotonic()
        if self.read_stage:
            self.read_timings[self.read_stage] = self.read_timings.get(self.read_stage, 0) + round(
                (now - self.read_stage_started) * 1000)
        self.read_stage, self.read_stage_started = stage, now

    def _observe_read(self, path, error=''):
        if self.read_observer:
            self.read_observer(path.split('?', 1)[0], error)

    def _reserved_request(self, path, method, payload, content_type):
        if self.read_only and method != 'GET':
            raise DeviceMutationRejected('Live progress clients cannot mutate devices')
        # Include lock/semaphore wait in the read deadline. Never abandon a
        # running mutation and spawn a duplicate request on another worker.
        if not self.request_lock.acquire(timeout=self._remaining()):
            raise TimeoutError('Device is busy; request deadline exceeded')
        reserved = False
        try:
            if self.request_slots is not None:
                reserved = self.request_slots.acquire(timeout=self._remaining())
                if not reserved:
                    raise TimeoutError('Device worker capacity deadline exceeded')
            return self._serial_request(path, method, payload, content_type)
        finally:
            if reserved:
                self.request_slots.release()
            self.request_lock.release()

    def _serial_request(self, path, method, payload, content_type):
        if path != '/api/v3' and not path.startswith('/api/v3/'):
            raise ValueError('Management requires Device API v3')
        if self.api_metadata is None and path != '/api/v3':
            self.read_path = '/api/v3'
            metadata = self._request('/api/v3')
            if not isinstance(metadata, dict) or metadata.get('api_version') != 3:
                raise ValueError('Device API v3 required; update IoT-MD and Management together')
            self.api_metadata = metadata
            self.discovered_at = time.monotonic()
            self._observe_read('/api/v3')
        self.read_path = path.split('?', 1)[0]
        headers = {}
        if method == 'POST':
            metadata = self.api_metadata or {}
            if not metadata.get('capabilities', {}).get('persistent_idempotency') or not self.sequence_provider:
                raise ValueError('Durable Device API v3 operations required; update IoT-MD and Management together')
            minimum = metadata.get('next_request_sequence')
            if not isinstance(minimum, int) or isinstance(minimum, bool) or not 1 <= minimum <= 9007199254740991:
                raise ValueError('Invalid device mutation sequence')
            sequence = self.sequence_provider(minimum)
            self.last_request_key = str(sequence) + '.' + secrets.token_hex(8)
            headers['Idempotency-Key'] = self.last_request_key
            metadata['next_request_sequence'] = sequence + 1
            if self.request_recorder:
                self.request_recorder(self.last_request_key, method, path, 'reserved', '')
        try:
            result = self._request(path, method, payload, content_type, headers)
        except Exception as exc:
            if method == 'POST' and self.request_recorder:
                self.request_recorder(self.last_request_key, method, path,
                    'rejected' if isinstance(exc, DeviceMutationRejected) else 'uncertain', '')
            raise
        if not isinstance(result, dict) or result.get('api_version') != 3:
            raise ValueError('Invalid Device API v3 response')
        if method == 'GET':
            self._observe_read(path)
            if path == '/api/v3':
                self.api_metadata = result
                self.discovered_at = time.monotonic()
        if method == 'POST' and self.request_recorder:
            operation = result.get('operation', {})
            self.request_recorder(self.last_request_key, method, path,
                operation.get('status', 'uncertain'), operation.get('id', ''))
        return result

    def _request(self, path, method='GET', payload=None, content_type='application/json', headers=None):
        # Each actual HTTP read has its own wall-clock budget, while the caller
        # may impose a separate workflow deadline (e.g. the live-read budget).
        previous = self.request_deadline
        if method == 'GET':
            self.request_deadline = time.monotonic() + self.timeout
        try:
            return self._transport_request(path, method, payload, content_type, headers)
        finally:
            self.request_deadline = previous

    def _transport_request(self, path, method='GET', payload=None, content_type='application/json', headers=None):
        body = None if payload is None else (
            bytes(payload) if isinstance(payload, (bytes, bytearray))
            else json.dumps(payload).encode()
        )
        request = urllib.request.Request(
            'https://' + self.record['host'] + ':' + str(self.record['port']) + path,
            data=body, method=method,
            headers=dict({'Content-Type': content_type, 'Accept': 'application/json'}, **(headers or {})),
        )
        try:
            self._read_stage('tls_context')
            context = self._context()
            self._read_stage('connect/headers')
            with urllib.request.urlopen(
                request, context=context, timeout=self._remaining()
            ) as response:
                self.read_http_status = getattr(response, 'status', 0) or 0
                return self._response_json(response)
        except urllib.error.HTTPError as exc:
            self.read_http_status = exc.code
            rejected_before_dispatch = False
            try:
                value = self._response_json(exc.fp)
                detail = value.get('error') if isinstance(value, dict) else value
                if isinstance(detail, dict):
                    rejected_before_dispatch = (method == 'POST' and exc.code == 503 and
                        value.get('api_version') == 3 and
                        detail.get('code') in ('operation_reservation_failed', 'journal_full', 'operation_limit') and
                        detail.get('phase') == 'reservation' and detail.get('dispatched') is False)
                    detail = detail.get('message') or detail.get('code')
            except TimeoutError:
                raise
            except Exception:
                detail = str(exc)
            finally:
                exc.close()
            if path == '/api/v3' and exc.code in (404, 410):
                detail = 'Device API v3 required; update IoT-MD and Management together'
            error = (DeviceMutationRejected if rejected_before_dispatch else
                     DeviceReadUnavailable if method == 'GET' and exc.code in (429, 500, 502, 503, 504)
                     else DeviceMutationRejected if exc.code in (400, 403, 404, 405, 410) else ValueError)
            raise error(bounded_text(detail, 256)) from None

    def _response_json(self, response):
        self._read_stage('body')
        if self.deadline is None and self.request_deadline is None:
            return json.loads(response.read())
        chunks, size = [], 0
        while True:
            # read1 returns available bytes instead of waiting for a full body.
            # Re-budget every read, including bodies of HTTP error responses.
            remaining = self._remaining()
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = response.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            self.read_bytes = size
            if size > self.response_limit:
                raise ValueError('Device API response is too large')
        self._remaining()
        self._read_stage('decode')
        return json.loads(b''.join(chunks))


class FleetController:
    def __init__(self, store, signer, timeout=10, now=None, tls=None,
                 worker_limit=8, progress_timeout=5, tracking_options=None):
        self.store = store
        self.signer = signer
        self.timeout = int(timeout)
        self.now = now or (lambda: int(time.time()))
        self.tls = dict(tls or {})
        self._action_locks = {}
        self._action_locks_guard = threading.Lock()
        workers = max(2, min(32, int(worker_limit)))
        # Reserve transport capacity too: manual refresh/Cancel all must not
        # consume the connections reserved for the live-read scheduler lane.
        self._request_slots = threading.BoundedSemaphore(workers // 2)
        self._progress_slots = threading.BoundedSemaphore(workers - workers // 2)
        self.progress_timeout = max(2, min(15, int(progress_timeout)))
        self._progress_lock = threading.RLock()
        self._progress_reads = {}
        self._progress_clients = {}
        self._api_reads = {}
        self._progress_traces = {}
        self._operation_observation_lock = threading.RLock()
        self.tracking = tracking_settings(tracking_options or {})
        self.mqtt_connected = lambda: False

    def progress_read_needed(self, identifier, result=None):
        if self.tracking['method'] != 'mqtt' or not self.mqtt_connected():
            return True
        if result is None:
            result = next((item['results'].get(identifier, {}) for item in self.store.active_deployments()
                           if identifier in item['targets']), {})
        dispatch = result.get('dispatch') or {}
        if result.get('status') == 'cancelling' or dispatch.get('tracking_method') != 'mqtt':
            return True
        with self._progress_lock:
            if identifier not in self._api_reads:
                self._api_reads[identifier] = self.store.get_poll_state(identifier)
            state = self._api_reads[identifier].get('mqtt_progress') or {}
        if state.get('operation_id') != dispatch.get('update_operation_id'):
            return True
        age = self.now() - state.get('received_at', 0)
        snapshot_age = self.now() - state.get('api_snapshot_at', 0)
        return (age < 0 or age >= self.tracking['stale_s'] or snapshot_age < 0 or
                snapshot_age >= self.tracking['snapshot_interval_s'])

    def _action_lock(self, identifier):
        with self._action_locks_guard:
            return self._action_locks.setdefault(identifier, threading.RLock())

    def _client(self, record, timeout=None):
        settings = dict(record)
        settings.update(self.tls)
        identifier = record.get('id')
        if identifier:
            with self._progress_lock:
                self._api_reads.setdefault(identifier, json.loads(json.dumps(record.get('poll_state') or {})))
        return DeviceClient(settings, self.timeout if timeout is None else timeout,
            sequence_provider=lambda minimum: self.store.next_api_sequence(minimum),
            request_recorder=lambda key, method, path, state, operation: self.store.record_api_request(
                record['id'], key, method, path, state, operation),
            request_slots=self._request_slots,
            read_observer=(lambda path, error: self._observe_api_read(identifier, path, error)) if identifier else None)

    def _observe_api_read(self, identifier, path, error=''):
        with self._progress_lock:
            state = self._api_reads.setdefault(identifier, {})
            api = state.setdefault('api', {})
            api.update(path=path.split('?', 1)[0], error=bounded_text(error), checked_at=self.now())
            api['last_failure' if error else 'last_success'] = self.now()

    def _poll_state(self, identifier):
        with self._progress_lock:
            return json.loads(json.dumps(self._api_reads.get(identifier, {})))

    def _refresh_state(self, identifier, stage, error=''):
        with self._progress_lock:
            state = self._api_reads.setdefault(identifier, {})
            state['refresh'] = {
                'stage': stage, 'error': bounded_text(error), 'at': self.now()}
            return json.loads(json.dumps(state))

    def _read_device(self, identifier, client, path):
        # Real clients observe discovery too; test/alternative clients still
        # report the known read result without generating additional requests.
        if isinstance(client, DeviceClient):
            return client.request(path)
        try:
            result = client.request(path)
        except Exception as exc:
            self._observe_api_read(identifier, path, device_connection_error(exc))
            raise
        self._observe_api_read(identifier, path)
        return result

    def create_backup(self, identifier, source='manual', retention=7):
        record = self.store.get_device(identifier, public=False)
        if not record:
            raise ValueError('device is not registered')
        if not record.get('enabled'):
            raise ValueError('device is disabled')
        password = secrets.token_urlsafe(32)
        salt, derived_key = backup_derived_key(password)
        result = self._client(record, timeout=max(self.timeout, 60)).request(
            '/api/v3/configuration/backups', 'POST', {
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
            '/api/v3/configuration/backups/preview', 'POST', {
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
            '/api/v3/configuration/backups/apply', 'POST', {
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

    def complete_enrollment(self, identifier, description=''):
        self.poll_device(identifier)
        if description:
            try:
                self.update_device(identifier, {'description': description}, force_description=True)
            except Exception as exc:
                self.store.set_device_error(identifier, device_connection_error(exc))
                self.store.record_audit('device.description.updated', 'failed', identifier, '',
                    {'error': bounded_text(exc)})

    def update_device(self, identifier, changes, force_description=False):
        candidate = self.store.prepare_device_update(identifier, changes)
        description = str(candidate.get('description') or '').strip()
        # An explicitly submitted description is a device write, even if it
        # matches our cache: an earlier accepted response may not reflect the
        # device's persisted configuration. Unedited forms omit this field.
        changed = 'description' in changes
        if changed:
            # Metadata belongs to the enrolled device, not a newly edited address.
            record = self.store.get_device(identifier, public=False)
            client = self._client(record)
            result = client.request('/api/v3/configuration/profile', 'POST', {
                'format_version': 1, 'name': 'Device description',
                'settings': {'device_description': description},
            })
            if not result.get('accepted') or 'device_description' not in (
                    result.get('profile') or {}).get('applied_settings', []):
                raise ValueError('Device did not accept the description update')
            configuration = client.request('/api/v3/configuration').get('configuration') or {}
            if configuration.get('device_description') != description:
                raise ValueError('Device accepted the description update but its saved configuration did not confirm it. Refresh the device and retry; check that its application supports device descriptions.')
        self.store.update_device(identifier, changes)
        if changed:
            self.store.record_device_description(identifier, description)
        return self.store.get_device(identifier)

    def poll_device(self, identifier):
        started = time.monotonic()
        record = self.store.get_device(identifier, public=False)
        if not record:
            raise ValueError('device is not registered')
        if not record.get('enabled'):
            return self.store.get_device(identifier)
        cursor = int(record.get('event_cursor', 0))
        client = self._client(record)
        # Discovery + inventory + configuration + optional trial confirmation
        # + health + events: bounded workflow, not one request's timeout shared
        # by the entire cycle. Live progress retains its independent short cap.
        client.deadline = time.monotonic() + 6 * self.timeout
        stage = 'inventory'
        try:
            inventory = self._read_device(identifier, client, '/api/v3/device/inventory')
            self._record_installed_versions(identifier, inventory.get('device'), '/api/v3/device/inventory')
            self._progress_observed(identifier, started, received=True)
            # Publish observed transfer/staging boundaries before slower health,
            # configuration or log reads. This is not installation confirmation
            # and does not mark cached health/last-seen data fresh.
            self._reconcile_deployments(identifier, {
                **record, 'inventory': inventory,
                'fleet': inventory.get('fleet') or {},
            }, progress_only=True)
            stage = 'configuration'
            configuration = self._read_device(identifier, client, '/api/v3/configuration')
            if (configuration.get('configuration') or {}).get(
                'network_trial_confirmation_ready'
            ):
                stage = 'network confirmation'
                confirmation = client.request(
                    '/api/v3/configuration/network/confirm', 'POST', {}
                )
                if confirmation.get('confirmed'):
                    configuration['configuration']['network_trial_pending'] = False
            inventory['configuration'] = configuration.get('configuration') or {}
            stage = 'health'
            health = self._read_device(identifier, client, '/api/v3/health')
            stage = 'events'
            events = self._read_device(identifier, client, '/api/v3/events?cursor=' + str(cursor) + '&limit=64')
        except Exception as exc:
            self._progress_observed(identifier, started, error=device_connection_error(exc))
            state = self._refresh_state(identifier, stage, device_connection_error(exc))
            self.store.set_device_error(identifier, device_connection_error(exc), poll_state=state)
            self._mark_immediate_install_restart(identifier)
            return self.store.get_device(identifier)
        state = self._refresh_state(identifier, 'complete')
        self.store.record_poll(identifier, inventory, health, events, poll_state=state)
        record = self.store.get_device(identifier)
        self._reconcile_deployments(identifier, record, client=client)
        return record

    def progress_diagnostics(self):
        enabled = set(self.store.device_ids(enabled_only=True))
        registered = set(self.store.device_ids())
        with self._progress_lock:
            self._progress_reads = {key: value for key, value in self._progress_reads.items() if key in enabled}
            self._progress_clients = {key: value for key, value in self._progress_clients.items() if key in enabled}
            self._api_reads = {key: value for key, value in self._api_reads.items() if key in enabled}
            self._progress_traces = {key: value for key, value in self._progress_traces.items() if key in registered}
            return copy_trace(self._progress_reads)

    def progress_trace_export(self, deployment_id='', device_id=''):
        """Read retained local evidence only; never contact a device."""
        deployment = self.store.get_deployment(deployment_id) if deployment_id else None
        if deployment_id and not deployment:
            raise ValueError('deployment is not registered')
        identifiers = [device_id] if device_id else (
            deployment['targets'] if deployment else self.store.device_ids())
        devices = []
        for identifier in identifiers:
            if deployment and identifier not in deployment['targets']:
                continue
            record = self.store.get_device(identifier, public=False)
            if not record:
                continue
            with self._progress_lock:
                cached = self._progress_traces.get(identifier)
                traces = clean_traces(cached) if cached is not None else None
            if traces is None:
                traces = clean_traces(self.store.get_progress_traces(identifier))
            if deployment_id:
                traces['deployments'] = {key: value for key, value in traces['deployments'].items()
                                          if key == deployment_id}
            devices.append({'id': identifier, 'host': record['host'], 'port': record['port'],
                            'traces': traces})
        return {'format_version': 1, 'exported_at': self.now(),
                'request_timeout_s': self.timeout, 'progress_timeout_s': self.progress_timeout,
                'devices': devices}

    def _progress_client(self, record):
        # Only the scheduler's serialized, read-only live lane reuses clients.
        # Avoid API discovery and CA loading on every inventory read; refresh
        # after connection errors, address changes or certificate/key rotation.
        settings = {**record, **self.tls}
        identity = [str(settings.get('host', '')).lower(), int(settings.get('port', 8444))]
        for key in ('ca_path', 'cert_path', 'key_path'):
            path = str(settings.get(key) or '')
            try:
                state = os.stat(path)
                fingerprint = (state.st_ino, state.st_mtime_ns, state.st_size)
            except OSError:
                fingerprint = None
            identity.append((path, fingerprint))
        with self._progress_lock:
            cached = self._progress_clients.get(record['id'])
            if cached is None or cached[0] != identity:
                cached = (identity, self._client(record, self.progress_timeout))
                cached[1].read_only = True
                cached[1].request_slots = self._progress_slots
                self._progress_clients[record['id']] = cached
            return cached[1]

    def _progress_observed(self, identifier, started, error='', received=False):
        with self._progress_lock:
            previous = self._progress_reads.get(identifier, {})
            self._progress_reads[identifier] = {
                **previous,
                'last_attempt': self.now(), 'last_received': self.now() if received else previous.get('last_received', 0),
                'error': bounded_text(error), 'duration_ms': round((time.monotonic() - started) * 1000),
                'received_on_attempt': bool(received),
            }

    def poll_progress(self, identifier):
        """Independent discovery, status and confirmation budgets; no writes/replays."""
        started = time.monotonic()
        record = self.store.get_device(identifier, public=False)
        if not record or not record.get('enabled'):
            return
        started_at = self.now()
        deployments = [item for item in self.store.active_deployments()
                       if identifier in item['targets']]
        with self._progress_lock:
            self._api_reads.setdefault(identifier, copy_trace(record.get('poll_state') or {}))
            loaded = identifier in self._progress_traces
        if not loaded:
            saved = clean_traces(self.store.get_progress_traces(identifier))
            with self._progress_lock:
                self._progress_traces.setdefault(identifier, saved)
        received = False
        failure = ''
        client = None
        workflow_stage = 'discovery'
        boot_id = ''
        rediscover = False
        try:
            client = self._progress_client(record)
            if isinstance(client, DeviceClient):
                client.read_metrics = []
                client.metrics_started_at = started
                # Discovery is bootstrap, not part of the status-read deadline.
                client.timeout = self.timeout
                if (client.api_metadata is not None and
                        not client.api_metadata.get('capabilities', {}).get('live_update_status') and
                        time.monotonic() - client.discovered_at >= 60):
                    # An enabling universal upgrade may restart an older v3
                    # device whose inventory doesn't carry a boot identity.
                    client.api_metadata = None
                if client.api_metadata is None:
                    client.deadline = time.monotonic() + self.timeout
                    self._read_device(identifier, client, '/api/v3')
            workflow_stage = 'status'
            client.deadline = time.monotonic() + self.progress_timeout
            compact = (isinstance(client, DeviceClient) and
                       bool((client.api_metadata or {}).get('capabilities', {}).get('live_update_status')))
            tracked = [item for item in deployments if
                item['results'].get(identifier, {}).get('dispatch', {}).get('update_operation_id')]
            # Existing API v3 devices can receive the upgrade which introduces
            # this capability. Never fall back to v2 or bypass authentication.
            path = '/api/v3/fleet/update-status' if compact else '/api/v3/device/inventory'
            if tracked:
                operation_id = tracked[0]['results'][identifier]['dispatch']['update_operation_id']
                path = '/api/v3/fleet/update-operations/' + operation_id
                if len(tracked) != 1:
                    raise ValueError('More than one device update operation is being tracked')
            if isinstance(client, DeviceClient):
                client.response_limit = 65536 if compact else 16 * 1024 * 1024
            inventory = self._read_device(identifier, client, path)
            if not isinstance(inventory.get('device'), dict) or not isinstance(inventory.get('fleet') or {}, dict):
                raise ValueError('Invalid live device status response')
            if compact and not tracked and not isinstance(inventory.get('fleet'), dict):
                raise ValueError('Invalid live fleet status response')
            expected_id = ((record.get('inventory') or {}).get('device') or {}).get('device_id')
            if expected_id and inventory['device'].get('device_id') != expected_id:
                raise ValueError('Live device identity does not match the enrolled device')
            boot_id = str(inventory.get('boot_id') or '')
            versions = self.store.record_device_versions(identifier, inventory['device'])
            observed = {**record, 'inventory': inventory, 'fleet': inventory.get('fleet') or {}}
            received = True
            self._progress_observed(identifier, started, received=True)
            self._reconcile_deployments(identifier, observed, progress_only=True)
            if tracked and tracked[0]['results'][identifier]['dispatch'].get('tracking_method') == 'mqtt':
                with self._progress_lock:
                    self._api_reads.setdefault(identifier, {}).setdefault('mqtt_progress', {})['api_snapshot_at'] = self.now()
            # A fresh health read may confirm a matching dispatched update,
            # but inventory alone, cached health, or an active trial may not.
            candidates = []
            for deployment in self.store.active_deployments():
                result = deployment['results'].get(identifier, {})
                if result.get('dispatch', {}).get('update_operation_id'):
                    continue  # Local operation confirmation, never version inference.
                if result.get('status') not in ('checking', 'staging', 'staged', 'scheduled', 'installing', 'cancelling'):
                    continue
                update = deployment.get('update') or {}
                if update and self._deployment_update_completed(deployment, identifier, observed) and not self._installation_started(observed, update, {}):
                    candidates.append(deployment)
            if candidates:
                workflow_stage = 'confirmation'
                client.deadline = time.monotonic() + self.timeout
                health = self._read_device(identifier, client, '/api/v3/health')
                if not isinstance(health.get('health'), dict):
                    raise ValueError('Invalid device health confirmation response')
                for deployment in candidates:
                    self.store.set_deployment_target(deployment['id'], identifier, 'complete',
                        (deployment['update'].get('version') or 'Update') + ' installed',
                        progress={'completed': ['queued', 'inspect', 'core_write', 'core_verify',
                            'application_download', 'application_verify', 'pair', 'install', 'complete'], 'phase': 'complete'})
                if isinstance(client, DeviceClient):
                    # The next deployment must rediscover post-upgrade API
                    # capabilities, while retaining the verified TLS context.
                    rediscover = True
            # Cancellation acknowledgement is a read, never a replay of its POST.
            cancelling = [item for item in self.store.active_deployments()
                if item['results'].get(identifier, {}).get('status') == 'cancelling' and
                not item['results'].get(identifier, {}).get('dispatch', {}).get('update_operation_id')]
            if cancelling:
                workflow_stage = 'cancellation'
                if compact:
                    acknowledgement = inventory.get('cancellation') or {}
                else:
                    client.deadline = time.monotonic() + self.timeout
                    acknowledgement = self._read_device(identifier, client, '/api/v3/fleet/update-cancel')
                for deployment in cancelling:
                    update = deployment.get('update') or {}
                    if (acknowledgement.get('status') == 'cancelled' and
                            int(acknowledgement.get('release_sequence', 0)) == int(update.get('release_sequence', 0)) and
                            acknowledgement.get('release_type') == update.get('release_type')):
                        self.store.set_deployment_target(deployment['id'], identifier,
                            'cancelled', 'Update cancelled; staged data discarded')
            self._progress_observed(identifier, started, received=True)
            if compact:
                self._refresh_progress_versions(identifier, client, inventory['device'], versions)
        except Exception as exc:
            failure = failure_reason(exc)
            # A slow status/health response doesn't invalidate successful API
            # discovery. Identity/certificate changes are handled by the cache
            # fingerprint; protocol/auth rejection explicitly invalidates it.
            if isinstance(exc, (ValueError, ssl.SSLError)):
                with self._progress_lock:
                    self._progress_clients.pop(identifier, None)
            self._progress_observed(identifier, started, error=device_connection_error(exc), received=received)
            self._mark_immediate_install_restart(identifier)
            raise
        finally:
            if rediscover and isinstance(client, DeviceClient):
                client.api_metadata = None
            with self._progress_lock:
                diagnostic = self._progress_reads.get(identifier, {})
                previous_boot = diagnostic.get('boot_id', '')
                diagnostic.update(workflow_stage=workflow_stage,
                    reads=list(client.read_metrics) if isinstance(client, DeviceClient) else [])
                api_success = (self._api_reads.get(identifier, {}).get('api') or {}).get('last_success', 0)
                diagnostic['responsive'] = bool(received or api_success and self.now() - api_success <= 120)
                if boot_id:
                    diagnostic['boot_id'] = boot_id
                    if previous_boot and previous_boot != boot_id and isinstance(client, DeviceClient):
                        client.api_metadata = None
                self._progress_reads[identifier] = diagnostic
                traces = retain_attempt(self._progress_traces.get(identifier), deployments, {
                    'started_at': started_at, 'finished_at': self.now(),
                    'duration_ms': round((time.monotonic() - started) * 1000),
                    'workflow_stage': workflow_stage, 'received': received,
                    'failure': failure, 'reads': diagnostic['reads']})
                self._progress_traces[identifier] = traces
            # One durable diagnostic write per live workflow, not per HTTP read.
            self.store.record_progress_poll(identifier, self._poll_state(identifier), traces)

    def _refresh_progress_versions(self, identifier, client, observed, cached):
        """Occasional bounded read for missing labels; never a device mutation."""
        fields = ('release_sequence', 'firmware_release_sequence')
        if not isinstance(observed.get('device_id'), str) or not 0 < len(observed['device_id']) <= 128:
            return
        if any(not isinstance(observed.get(field), int) or isinstance(observed.get(field), bool)
               or not 0 <= observed[field] <= 9007199254740991 for field in fields):
            return
        cached = cached or {}
        if all(cached.get(field) == observed[field] for field in fields) and all(
                cached.get(field) for field in ('application_version', 'firmware_version')):
            self._version_evidence(identifier, cached, '/api/v3/fleet/update-status')
            return
        previous = self._poll_state(identifier).get('versions') or {}
        sequences = [observed[field] for field in fields]
        if previous.get('sequences') == sequences and previous.get('next_retry_at', 0) > self.now():
            return
        state = {'sequences': sequences, 'source': '/api/v3/device', 'received_at': 0,
                 'error': '', 'next_retry_at': 0}
        try:
            client.deadline = time.monotonic() + self.progress_timeout
            value = self._read_device(identifier, client, '/api/v3/device').get('device')
            if (not isinstance(value, dict) or value.get('device_id') != observed.get('device_id') or
                    any(not isinstance(value.get(field), int) or isinstance(value.get(field), bool)
                        or value.get(field) != observed[field] for field in fields) or
                    any(not isinstance(value.get(field), str) or not 0 < len(value[field]) <= 96 or
                        any(ord(c) < 32 for c in value[field])
                        for field in ('application_version', 'firmware_version'))):
                raise ValueError('Installed version read does not match the observed device/components')
            self.store.record_device_versions(identifier, value)
            state['received_at'] = self.now()
        except Exception as exc:
            # Status/cancellation evidence already received remains valid. Only
            # the optional labels stay unknown; never turn this into job failure.
            state.update(error=device_connection_error(exc), next_retry_at=self.now() + 30)
        with self._progress_lock:
            self._api_reads.setdefault(identifier, {})['versions'] = state

    def _record_installed_versions(self, identifier, device, source):
        cached = self.store.record_device_versions(identifier, device) or {}
        if not isinstance(device, dict):
            return
        fields = ('release_sequence', 'firmware_release_sequence')
        if (any(not isinstance(device.get(field), int) or isinstance(device.get(field), bool)
                or device[field] != cached.get(field) for field in fields) or
                any(not device.get(field) or device.get(field) != cached.get(field)
                    for field in ('application_version', 'firmware_version'))):
            return
        self._version_evidence(identifier, cached, source)

    def _version_evidence(self, identifier, device, source):
        with self._progress_lock:
            self._api_reads.setdefault(identifier, {})['versions'] = {
                'sequences': [device[field] for field in ('release_sequence', 'firmware_release_sequence')], 'source': source,
                'received_at': self.now(), 'error': '', 'next_retry_at': 0}

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

    def _deployment_update_completed(self, deployment, identifier, record):
        update = deployment.get('update') or {}
        if not self._update_installed(record, update):
            return False
        result = deployment['results'].get(identifier, {})
        dispatch = result.get('dispatch')
        if dispatch:
            # An inventory match is only completion evidence after this job
            # dispatched from a lower installed sequence, not a previous job.
            baseline = {'inventory': {'device': dispatch['baseline']}}
            if self._update_installed(baseline, update):
                return False
            fleet = record.get('fleet') or {}
            if fleet.get('rollout_paused') or fleet.get('command_chain_failed'):
                return False
            commands = (fleet.get('policy') or {}).get('commands') or []
            return any(command.get('id') in dispatch['command_ids'] and
                int(command.get('release_sequence', 0)) == int(update['release_sequence']) and
                command.get('release_type') == update['release_type'] for command in commands)
        # Existing in-flight records without dispatch evidence need an observed
        # installation boundary. Never promote a mere compatibility check.
        return int(result.get('milestone_rank', 0)) >= 3

    @staticmethod
    def _installation_started(record, update, progress):
        """Prefer a matching live trial over commands left pending across reset."""
        device = (record.get('inventory') or {}).get('device') or {}
        live = device.get('update_progress') or {}
        sequence = int(update.get('release_sequence', 0))
        release_type = update.get('release_type', '')
        components = {
            'application': ('application',), 'firmware': ('firmware',),
            'universal': ('application', 'firmware', 'universal'),
        }.get(release_type, ())
        for component in components:
            if live.get(component + '_status') not in ('trial', 'activating', 'committing'):
                continue
            running_key = 'firmware_release_sequence' if component == 'firmware' else 'release_sequence'
            if (int(live.get(component + '_sequence', 0) or 0) == sequence or
                    progress or int(device.get(running_key, 0) or 0) == sequence):
                return True
        return False

    def _mark_immediate_install_restart(self, identifier):
        """Keep fast install-now reboots from looking stuck in staging."""
        if not hasattr(self.store, 'list_deployments'):
            return
        for deployment in self.store.active_deployments():
            if deployment['results'].get(identifier, {}).get('dispatch', {}).get('update_operation_id'):
                continue  # Timeout is connectivity, not an installation boundary.
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

    def _reconcile_deployments(self, identifier, record, progress_only=False, client=None):
        if not hasattr(self.store, 'list_deployments'):
            return
        fleet = record.get('fleet') or {}
        for deployment in self.store.active_deployments():
            if identifier not in deployment['targets']:
                continue
            current = deployment['results'].get(identifier, {}).get('status', '')
            if deployment['results'].get(identifier, {}).get('dispatch', {}).get('update_operation_id'):
                operation = (record.get('inventory') or {}).get('update_operation')
                if operation:
                    self.observe_update_operation(deployment['id'], identifier, operation)
                continue
            if current == 'cancelling':
                if progress_only:
                    continue
                if self._deployment_update_completed(deployment, identifier, record):
                    self.store.set_deployment_target(deployment['id'], identifier, 'complete',
                        'Installed version confirmed; cancellation arrived after installation',
                        progress={'completed': ['queued', 'inspect', 'core_write', 'core_verify',
                            'application_download', 'application_verify', 'pair', 'install', 'complete'],
                            'phase': 'complete'})
                    continue
                try:
                    result = (client or self._client(self.store.get_device(identifier, public=False))).request(
                        '/api/v3/fleet/update-cancel')
                    update = deployment.get('update') or {}
                    if (result.get('status') == 'cancelled' and
                            int(result.get('release_sequence', 0)) == int(update.get('release_sequence', 0)) and
                            result.get('release_type') == update.get('release_type')):
                        self.store.set_deployment_target(deployment['id'], identifier,
                            'cancelled', 'Update cancelled; staged data discarded')
                except Exception:
                    # An unreadable/ambiguous acknowledgement must never be
                    # presented as a successful cancellation or replayed write.
                    pass
                continue
            if current in ('queued', 'running'):
                # Polling inventory is not proof that a queued profile ran.
                continue
            if current in ('complete', 'failed', 'cancelled') or (
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
                if progress_only:
                    continue
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'complete', 'Profile applied'
                )
                continue
            sequence = int(update.get('release_sequence', 0))
            release_type = update.get('release_type', '')
            command_ids = (deployment['results'].get(identifier, {}).get('dispatch') or {}).get('command_ids')
            policy_commands = (fleet.get('policy') or {}).get('commands') or []
            matching = [
                command for command in policy_commands
                if int(command.get('release_sequence', 0)) == sequence and
                (command_ids is None or command.get('id') in command_ids) and
                (not command.get('release_type') or
                 command.get('release_type') == release_type)
            ]
            pending = [
                command for command in (fleet.get('pending_commands') or [])
                if int(command.get('release_sequence', 0)) == sequence and
                (command_ids is None or command.get('id') in command_ids) and
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
            if not progress_only and self._deployment_update_completed(deployment, identifier, record):
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'complete',
                    (update.get('version') or 'Update') + ' installed',
                    progress={'completed': ['queued', 'inspect', 'core_write',
                        'core_verify', 'application_download', 'application_verify',
                        'pair', 'install', 'complete'], 'phase': 'complete'}
                )
                continue
            if self._installation_started(record, update, progress):
                progress = dict(progress)
                progress['phase'] = 'install'
                self.store.set_deployment_target(
                    deployment['id'], identifier, 'installing',
                    'Device restarted; waiting for core and application confirmation'
                    if release_type == 'universal' else
                    'Device restarted; waiting to confirm the installed version',
                    progress=progress,
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
                if not progress_only and completed_at and returned_at > completed_at + 30 and not trial_active:
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
        with self._action_lock(identifier):
            return self._execute_deployment_target(deployment_id, identifier)

    def observe_update_operation(self, deployment_id, identifier, value):
        """One ordered projection for API snapshots and authenticated push reports."""
        with self._operation_observation_lock:
            deployment = self.store.get_deployment(deployment_id)
            result = (deployment or {}).get('results', {}).get(identifier, {})
            if not deployment or deployment['status'] == 'unresolved' or result.get('status') in (
                    'complete', 'failed', 'cancelled', 'unresolved'):
                return False
            dispatch = result.get('dispatch') or {}
            projection = reduce_operation(value, dispatch, dispatch.get('device_id'),
                                          deployment.get('update') or {}, result.get('update_operation'))
            if projection is None:
                return False
            status = projection['status']
            if status == 'staged' and deployment.get('activation') != 'stage':
                status = 'scheduled'
            self.store.set_deployment_target(deployment_id, identifier, status,
                projection['detail'], progress=projection['progress'], operation=projection['operation'])
            return True

    def observe_mqtt_operation(self, deployment_id, identifier, value):
        if not self.observe_update_operation(deployment_id, identifier, value):
            return False
        with self._progress_lock:
            record = self.store.get_device(identifier)
            cached = (record or {}).get('poll_state') or {}
            state = self._api_reads.setdefault(identifier, copy_trace(cached))
            previous = state.get('mqtt_progress') or {}
            state['mqtt_progress'] = {**previous, 'operation_id': value['id'], 'received_at': self.now(),
                                      'revision': value['revision'], 'boot_epoch': value['boot_epoch']}
        self._progress_observed(identifier, time.monotonic(), received=True)
        with self._progress_lock:
            self._progress_reads[identifier]['transport'] = 'mqtt'
            self.store.record_poll_state(identifier, self._poll_state(identifier))
        return True

    def cancel_deployment_target(self, deployment_id, identifier):
        with self._action_lock(identifier):
            deployment = self.store.get_deployment(deployment_id)
            if not deployment or identifier not in deployment['targets']:
                raise ValueError('device is not part of this deployment')
            update = deployment.get('update') or {}
            if not update:
                raise ValueError('only update deployments can be cancelled')
            current = deployment['results'].get(identifier, {}).get('status', 'queued')
            if current in ('complete', 'failed', 'cancelled', 'unresolved'):
                return deployment
            if current == 'installing':
                raise ValueError('Installation has begun; wait for confirmation before using rollback')
            if current == 'queued':
                # No device mutation has been submitted yet. Late worker claims
                # see this terminal result and must not dispatch a policy.
                return self.store.set_deployment_target(deployment_id, identifier,
                    'cancelled', 'Cancelled before dispatch')
            if current == 'cancelling':
                return deployment
            record = self.store.get_device(identifier, public=False)
            client = self._client(record)
            metadata = client.request('/api/v3')
            if not metadata.get('capabilities', {}).get('update_cancellation'):
                raise ValueError('This device application does not support safe cancellation; update IoT-MD first')
            self.store.set_deployment_target(deployment_id, identifier, 'cancelling',
                'Cancellation requested; awaiting device acknowledgement')
            try:
                result = client.request('/api/v3/fleet/update-cancel', 'POST', {
                    'release_sequence': update['release_sequence'],
                    'release_type': update['release_type'],
                    **({'update_operation_id': deployment['results'][identifier]['dispatch']['update_operation_id']}
                       if deployment['results'].get(identifier, {}).get('dispatch', {}).get('update_operation_id') else {}),
                })
            except DeviceMutationRejected as exc:
                self.store.set_deployment_target(deployment_id, identifier, current,
                    'Cancellation rejected: ' + str(exc), cancellation_refused=True)
                raise
            if result.get('status') not in ('cancelling', 'cancelled'):
                raise ValueError('Device cancellation outcome is uncertain; refresh to reconcile, do not repeat the write')
            return self.store.set_deployment_target(deployment_id, identifier,
                result['status'], 'Update cancelled; staged data discarded' if
                result['status'] == 'cancelled' else 'Waiting for staging to stop safely')

    def stop_tracking_deployment(self, deployment_id, confirmation):
        if confirmation != 'stop tracking ' + str(deployment_id):
            raise ValueError('Explicit confirmation is required to stop Management tracking')
        deployment = self.store.get_deployment(deployment_id)
        if not deployment:
            raise ValueError('deployment does not exist')
        acquired = []
        try:
            for identifier in sorted(deployment['targets']):
                lock = self._action_lock(identifier)
                if not lock.acquire(blocking=False):
                    raise ValueError('A Management device request is still running; wait for it to settle before stopping tracking')
                acquired.append(lock)
            return self.store.stop_tracking_deployment(deployment_id)
        finally:
            for lock in reversed(acquired):
                lock.release()

    def _execute_deployment_target(self, deployment_id, identifier):
        deployment = self.store.get_deployment(deployment_id)
        if not deployment:
            raise ValueError('deployment does not exist')
        if identifier not in deployment['targets']:
            raise ValueError('device is not part of this deployment')
        current = deployment['results'].get(identifier, {})
        if current.get('status') in ('complete', 'failed', 'cancelled', 'unresolved') or (
            current.get('status') == 'staged' and deployment['activation'] == 'stage'
        ):
            # Old retry jobs must not resurrect an action from history.
            return current
        if current.get('status') != 'queued':
            # An accepted/ambiguous device mutation must not be replayed.
            return current
        self.store.set_deployment_target(
            deployment_id, identifier, 'running', 'Connecting to device'
        )
        try:
            for other in self.store.list_deployments(limit=1000):
                if other['id'] == deployment_id or identifier not in other['targets']:
                    continue
                status = other['results'].get(identifier, {}).get('status', 'queued')
                if status not in ('queued', 'complete', 'failed', 'cancelled', 'unresolved') and not (
                    status == 'staged' and other['activation'] == 'stage'
                ):
                    raise ValueError('Device already has an in-flight deployment: ' + other['id'])
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
            }, deployment_id=deployment_id)
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
                '/api/v3/configuration/profile', 'POST', device_profile
            )
        else:
            result = {'accepted': True, 'profile': {'applied_settings': []}}
        for kind, payload in certificate_values.items():
            client.request(
                '/api/v3/configuration/certificates/' + kind, 'POST', payload,
                'application/octet-stream'
            )
        if certificate_values:
            result['certificates'] = client.request(
                '/api/v3/configuration/certificates/apply', 'POST', {}
            )
        if (result.get('profile') or {}).get('network_trial_pending'):
            result['restart'] = client.request(
                '/api/v3/configuration/restart', 'POST', {}
            )
        self.poll_device(identifier)
        return result

    def apply_policy(self, request, deployment_id=None):
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
        if deployment_id is not None:
            deployment = self.store.get_deployment(deployment_id)
            update = deployment.get('update') or {}
            if self._update_installed(record, update):
                raise ValueError('No newer compatible release: the selected update is already installed')
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
        client = self._client(record, max(30, self.timeout))
        operation_id = None
        report_key = None
        if deployment_id is not None:
            metadata = client.request('/api/v3')
            if metadata.get('capabilities', {}).get('update_operation_contract') != 1:
                raise ValueError('Update IoT-MD locally to the device-owned operation release before deploying; legacy inferred tracking is retired')
            operation_id = secrets.token_hex(16)
            commands[0]['id'] = operation_id
            if self.tracking['method'] == 'mqtt':
                if metadata.get('capabilities', {}).get('update_operation_reports') is not True:
                    raise ValueError('Update IoT-MD to a release with authenticated MQTT operation reports before selecting MQTT tracking')
                report_key = secrets.token_hex(32)
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
        payload = signed
        if deployment_id is not None:
            device = (record.get('inventory') or {}).get('device') or {}
            self.store.set_deployment_target(deployment_id, target, 'checking',
                'Submitting signed device-owned update operation', dispatch={
                    'baseline': {name: int(device.get(name, 0) or 0) for name in
                        ('release_sequence', 'firmware_release_sequence')},
                    'command_ids': [command['id'] for command in commands],
                    'update_operation_id': operation_id, 'policy_sequence': policy['policy_sequence'],
                    'device_id': device_target,
                    'tracking_method': self.tracking['method'],
                })
            if report_key:
                topic = report_topic(self.tracking['topic_template'], device_target, operation_id)
                self.store.save_update_report_key(operation_id, deployment_id, target, topic, report_key)
                payload = {'policy': signed, 'report_key': report_key,
                    'reporting': {name: self.tracking[name] for name in ('host', 'port', 'topic_template')}}
        # Bound a dripping POST response too. An uncertain outcome is durably
        # handed to reconciliation below, never retried on a second worker.
        client.deadline = time.monotonic() + 2 * max(30, self.timeout)
        try:
            result = client.request(
                '/api/v3/fleet/policy', 'POST', payload
            )
        except Exception as exc:
            if 'fleet policy signature verification failed' in str(exc):
                raise ValueError(
                    'The device reports the expected Management signing identity but '
                    'its core rejected the format-2 fleet policy signature. Install a '
                    'current universal/core update that supports typed fleet commands, '
                    'then refresh the device and retry.'
                ) from None
            if (isinstance(exc, DeviceMutationRejected) or deployment_id is None or
                    not isinstance(getattr(client, 'last_request_key', None), str)):
                raise
            # The durable request recorder already marked the POST uncertain.
            # Release dispatch to read-only reconciliation; never resend it.
            self.store.set_deployment_target(deployment_id, target, 'checking',
                'Update submission unconfirmed; reconciling without replay: ' + bounded_text(exc, 160))
            return {'uncertain': True}
        if deployment_id is None:
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
        if not targets:
            raise ValueError('The active rollout cohort has no enabled devices')
        deployment_id = 'rollout-deployment-' + secrets.token_hex(12)
        # Cohorts use the same durable queue, bounded handlers and exact
        # operation reducer as ordinary update actions, not a sequential POST loop.
        self.store.set_metadata('operation-rollout:' + deployment_id, identifier)
        deployment = self.create_deployment({'id': deployment_id, 'targets': targets,
            'activation': 'schedule', 'update': {'channel': rollout['channel'],
                'release_sequence': rollout['release_sequence'], 'release_type': rollout['release_type']}})
        return {'rollout': self.store.get_rollout(identifier), 'deployment': deployment,
                'dispatch': {target: {'queued': True} for target in targets}}
