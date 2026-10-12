"""Bounded, payload-free evidence for live progress reads on the HA host."""

import http.client
import json
import math
import ssl
import urllib.error


DEPLOYMENTS = 4
ATTEMPTS = 32
FAILURES = 8
READS = 6
STAGES = ('reservation', 'tls_context', 'connect/headers', 'body', 'decode')
WORKFLOWS = ('discovery', 'status', 'confirmation', 'cancellation')
PATHS = ('/api/v3', '/api/v3/fleet/update-status', '/api/v3/device/inventory',
         '/api/v3/health', '/api/v3/fleet/update-cancel')


def number(value):
    return max(0, min(9007199254740991, value)) if (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)) else 0


def failure_reason(exc):
    """Never copy response bodies, URLs, credential paths or exception args."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        text = str(reason).lower()
        if 'handshake' in text:
            return 'tls_handshake_timeout'
        if 'busy' in text:
            return 'device_reservation_timeout'
        if 'capacity' in text:
            return 'worker_capacity_timeout'
        if 'deadline' in text:
            return 'read_deadline_exceeded'
        return 'socket_timeout'
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'tls_certificate_verification_failed'
    if isinstance(reason, ssl.SSLError):
        return 'tls_handshake_failed'
    if isinstance(reason, http.client.RemoteDisconnected):
        return 'connection_closed_before_response'
    if isinstance(reason, OSError):
        return 'transport_error'
    if isinstance(reason, ValueError):
        return 'response_or_protocol_rejected'
    return 'request_failed'


REASONS = ('tls_handshake_timeout', 'device_reservation_timeout', 'worker_capacity_timeout',
           'read_deadline_exceeded', 'socket_timeout', 'tls_certificate_verification_failed',
           'tls_handshake_failed', 'connection_closed_before_response', 'transport_error',
           'response_or_protocol_rejected', 'request_failed')


def clean_attempt(value):
    """Whitelist persisted/exported fields, including on restart."""
    if not isinstance(value, dict):
        return None
    reads = []
    for read in (value.get('reads') if isinstance(value.get('reads'), list) else [])[-READS:]:
        if not isinstance(read, dict):
            continue
        reads.append({
            'path': read.get('path', '').split('?', 1)[0] if isinstance(read.get('path'), str)
                and read['path'].split('?', 1)[0] in PATHS else 'other',
            'stage': read.get('stage') if read.get('stage') in STAGES else 'unknown',
            'start_offset_ms': number(read.get('start_offset_ms')),
            'budget_ms': number(read.get('budget_ms')),
            'duration_ms': number(read.get('duration_ms')),
            'response_bytes': number(read.get('response_bytes')),
            'http_status': number(read.get('http_status')),
            'failure': read.get('failure') if read.get('failure') in REASONS else '',
            'stage_ms': {stage: number((read.get('stage_ms') or {}).get(stage)) for stage in STAGES}
                if isinstance(read.get('stage_ms'), dict) else {},
        })
    return {**{key: number(value.get(key)) for key in
               ('attempt', 'started_at', 'finished_at', 'duration_ms')},
            'workflow_stage': value.get('workflow_stage') if value.get('workflow_stage') in WORKFLOWS else 'unknown',
            'received': value.get('received') is True,
            'failure': value.get('failure') if value.get('failure') in REASONS else '',
            'reads': reads}


def clean_traces(value):
    groups = value.get('deployments', {}) if isinstance(value, dict) else {}
    if not isinstance(groups, dict):
        groups = {}
    result = {}
    for identifier, group in list(groups.items())[-DEPLOYMENTS:]:
        if not isinstance(identifier, str) or len(identifier) > 256 or not isinstance(group, dict):
            continue
        cleaned = {key: number(group.get(key)) for key in
                   ('release_sequence', 'attempts_total', 'failures_total', 'reads_total')}
        cleaned['release_type'] = group.get('release_type') if group.get('release_type') in (
            'universal', 'firmware', 'application') else ''
        for key, limit in (('attempts', ATTEMPTS), ('failures', FAILURES)):
            entries = group.get(key) if isinstance(group.get(key), list) else []
            cleaned[key] = [entry for entry in (clean_attempt(item) for item in entries[-limit:]) if entry]
        result[identifier] = cleaned
    return {'format_version': 1, 'deployments': result}


def retain_attempt(saved, deployments, attempt):
    traces = clean_traces(saved)
    groups = traces['deployments']
    # Capture IDs before reconciliation can move the deployment into history.
    for deployment in deployments[-DEPLOYMENTS:]:
        identifier = deployment['id']
        group = groups.pop(identifier, None) or clean_traces({'deployments': {
            identifier: deployment.get('update') or {}}})['deployments'][identifier]
        group['attempts_total'] += 1
        entry = clean_attempt({**attempt, 'attempt': group['attempts_total']})
        group['reads_total'] += len(entry['reads'])
        group['attempts'] = (group['attempts'] + [entry])[-ATTEMPTS:]
        if entry['failure']:
            group['failures_total'] += 1
            # Separate failure retention: recovery reads cannot evict evidence.
            group['failures'] = (group['failures'] + [entry])[-FAILURES:]
        groups[identifier] = group
    traces['deployments'] = dict(list(groups.items())[-DEPLOYMENTS:])
    return traces


def copy_trace(value):
    return json.loads(json.dumps(value))
