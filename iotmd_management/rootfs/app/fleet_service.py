"""Fleet polling, policy application, and rollout use cases."""

import json
import os
import ssl
import time
import urllib.request


def bounded_text(value, maximum=256):
    return str(value or '')[:maximum]


class DeviceClient:
    def __init__(self, record, timeout=10):
        self.record = record
        self.timeout = int(timeout)

    def _context(self):
        context = ssl.create_default_context(cafile=self.record['ca_path'])
        context.load_cert_chain(self.record['cert_path'], self.record['key_path'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def request(self, path, method='GET', payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            'https://' + self.record['host'] + ':' + str(self.record['port']) + path,
            data=body, method=method,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        )
        with urllib.request.urlopen(
            request, context=self._context(), timeout=self.timeout
        ) as response:
            return json.loads(response.read())


class FleetController:
    def __init__(self, store, signer, timeout=10, now=None):
        self.store = store
        self.signer = signer
        self.timeout = int(timeout)
        self.now = now or (lambda: int(time.time()))

    def poll_device(self, identifier):
        record = self.store.get_device(identifier, public=False)
        if not record:
            raise ValueError('device is not registered')
        if not record.get('enabled'):
            return
        cursor = int(record.get('event_cursor', 0))
        client = DeviceClient(record, self.timeout)
        try:
            inventory = client.request('/api/v2/device/inventory')
            health = client.request('/api/v2/health')
            events = client.request(
                '/api/v2/events?cursor=' + str(cursor) + '&limit=64'
            )
        except Exception as exc:
            self.store.set_device_error(identifier, bounded_text(exc, 256))
            return
        self.store.record_poll(identifier, inventory, health, events)

    def apply_policy(self, request):
        now = self.now()
        target = bounded_text(request.get('device_id'), 64)
        record = self.store.get_device(target, public=False)
        if not record:
            raise ValueError('device is not registered')
        command = request.get('command') or None
        commands = [] if not command else [{
            'id': bounded_text(command.get('id') or os.urandom(8).hex(), 64),
            'action': command.get('action', 'check-update'),
            'release_sequence': int(command.get('release_sequence', 0)),
        }]
        policy = {
            'format_version': 1,
            'target_board': 'esp32-s3',
            'policy_sequence': self.store.next_policy_sequence(),
            'issued_at': now - 5, 'not_before': now - 5,
            'expires_at': now + int(request.get('valid_for_s', 86400)),
            'target_device': target, 'target_cohort': '',
            'maintenance': {
                'weekdays': request.get('weekdays', [0, 1, 2, 3, 4, 5, 6]),
                'start_minute': int(request.get('start_minute', 120)),
                'duration_minutes': int(request.get('duration_minutes', 120)),
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
        result = DeviceClient(record, self.timeout).request(
            '/api/v2/fleet/policy', 'POST', signed
        )
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
                    'command': {
                        'action': 'download-update',
                        'release_sequence': rollout['release_sequence'],
                    },
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
