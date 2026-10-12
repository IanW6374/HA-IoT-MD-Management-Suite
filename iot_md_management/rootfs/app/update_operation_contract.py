"""Validate device-owned lifecycle observations before changing a fleet job.

Connectivity errors and installed version evidence are deliberately not inputs
to this reducer. A job completes only from its exact device operation record.
"""
PHASES = ('queued', 'inspect', 'core_write', 'core_verify',
          'application_download', 'application_verify', 'pair', 'install', 'complete')
STATES = {'prepared': 'checking', 'accepted': 'checking', 'checking': 'checking',
          'staging': 'staging', 'staged': 'staged', 'scheduled': 'scheduled',
          'installing': 'installing', 'cancelling': 'cancelling',
          'complete': 'complete', 'failed': 'failed', 'cancelled': 'cancelled',
          'interrupted': 'unresolved'}


def _integer(value, minimum=0, maximum=9007199254740991):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def reduce_operation(value, dispatch, expected_device, update, previous=None):
    if not isinstance(value, dict) or value.get('contract') != 1:
        raise ValueError('Unsupported device update operation contract')
    if (not _integer(value.get('contract'), 1, 1) or
            not _integer(value.get('release_sequence'), 1, 2147483647) or
            not _integer(value.get('policy_sequence'), 1, 2147483647)):
        raise ValueError('Invalid signed update operation identity')
    if (value.get('id') != dispatch.get('update_operation_id') or
            value.get('device_id') != expected_device or
            value.get('release_sequence') != update.get('release_sequence') or
            value.get('release_type') != update.get('release_type') or
            value.get('policy_sequence') != dispatch.get('policy_sequence')):
        raise ValueError('Device operation does not match this dispatched job')
    state, phase = value.get('state'), value.get('phase')
    completed, revision = value.get('completed'), value.get('revision')
    sample, boot = value.get('sample_sequence'), value.get('boot_id')
    epoch = value.get('boot_epoch', 0)
    if (state not in STATES or phase not in PHASES or not _integer(revision, 1) or
            not _integer(sample) or not _integer(epoch) or not isinstance(boot, str) or not 1 <= len(boot) <= 64 or
            not isinstance(completed, list) or len(completed) > len(PHASES) or
            any(item not in PHASES for item in completed) or len(set(completed)) != len(completed)):
        raise ValueError('Malformed device update operation lifecycle')
    if state == 'complete' and (phase != 'complete' or set(completed) != set(PHASES)):
        raise ValueError('Incomplete local health confirmation evidence')
    previous = previous or {}
    if previous:
        if epoch < previous.get('boot_epoch', 0):
            return None
        if epoch == previous.get('boot_epoch', 0) and previous.get('boot_id') != boot:
            raise ValueError('Conflicting device observation boot epoch')
        if revision < previous['revision']:
            return None
        if revision == previous['revision']:
            if (state != previous['state'] or phase != previous['phase'] or
                    completed != previous['completed']):
                raise ValueError('Conflicting device operation revision')
            if previous.get('boot_id') == boot and sample <= previous.get('sample_sequence', 0):
                return None
        if (previous['state'] in ('complete', 'failed', 'cancelled', 'interrupted') and
                state != previous['state']):
            raise ValueError('Terminal device operation cannot restart')
        if PHASES.index(phase) < PHASES.index(previous['phase']):
            raise ValueError('Device update phase regressed')
    percent = value.get('percent')
    if percent is not None and (not _integer(percent, 0, 100) or state != 'staging'):
        raise ValueError('Invalid device update transfer sample')
    detail = value.get('detail', '')
    if not isinstance(detail, str) or len(detail) > 160 or any(ord(c) < 32 for c in detail):
        raise ValueError('Invalid device operation detail')
    # Only validated, bounded lifecycle fields enter the durable job projection.
    evidence = {key: value[key] for key in ('id', 'state', 'phase', 'revision',
                                          'completed', 'boot_id', 'sample_sequence')}
    evidence['boot_epoch'] = epoch
    return {'status': STATES[state], 'detail': detail or 'Device update ' + state,
            'progress': {'phase': phase, 'completed': list(completed), 'percent': percent},
            'operation': evidence}
