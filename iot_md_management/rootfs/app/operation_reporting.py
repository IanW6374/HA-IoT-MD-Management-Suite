"""Bounded wire contract for optional authenticated MQTT update observations."""
FIELDS = ('contract', 'id', 'device_id', 'policy_sequence', 'release_sequence',
          'release_type', 'activation', 'state', 'phase', 'revision', 'completed',
          'boot_id', 'boot_epoch', 'sample_sequence', 'percent', 'detail')
DEFAULT_TOPIC = 'iotmd/update-operations/{device_id}/{operation_id}'


def topic_template(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 192:
        raise ValueError('MQTT progress topic template is invalid')
    if value.count('{device_id}') != 1 or value.count('{operation_id}') != 1:
        raise ValueError('MQTT progress topic requires one device_id and operation_id placeholder')
    skeleton = value.replace('{device_id}', 'device').replace('{operation_id}', 'operation')
    if any(c in skeleton for c in '#+{}') or any(ord(c) <= 32 or ord(c) > 126 for c in skeleton):
        raise ValueError('MQTT progress topic cannot contain wildcards, control characters or other placeholders')
    if any(not segment for segment in skeleton.split('/')):
        raise ValueError('MQTT progress topic cannot contain empty levels')
    if any(part not in ('{device_id}', '{operation_id}') and ('{device_id}' in part or
            '{operation_id}' in part) for part in value.split('/')):
        raise ValueError('MQTT identity placeholders must occupy complete topic levels')
    return value


def report_topic(template, device_id, operation_id):
    topic_template(template)
    for value in (device_id, operation_id):
        if (not isinstance(value, str) or not 1 <= len(value) <= 64 or
                any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-' for c in value)):
            raise ValueError('MQTT progress topic identity is invalid')
    topic = template.replace('{device_id}', device_id).replace('{operation_id}', operation_id)
    if len(topic) > 256:
        raise ValueError('MQTT progress topic exceeds the report transport limit')
    return topic


def report_bytes(report):
    pieces = [b'iotmd-update-report-v1\n']
    for field in FIELDS:
        if field not in report:
            raise ValueError('MQTT progress report has a missing field')
        value = report[field]
        if value is None:
            encoded = b'-'
        elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9007199254740991:
            encoded = str(value).encode()
        elif isinstance(value, str) and len(value) <= 256 and all(ord(c) >= 32 for c in value):
            encoded = value.encode()
        elif isinstance(value, list) and len(value) <= 9 and all(
                isinstance(item, str) and len(item) <= 40 and
                all(c in 'abcdefghijklmnopqrstuvwxyz_' for c in item) for item in value):
            encoded = ','.join(value).encode()
        else:
            raise ValueError('MQTT progress report has an invalid field')
        pieces.append((str(len(encoded)) + ':').encode() + encoded + b'\n')
    return b''.join(pieces)
