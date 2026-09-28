"""Validation for reusable management configuration profiles."""


FORMAT_VERSION = 1
BOOLEAN_FIELDS = {
    'ha_discovery', 'mqtt_enabled', 'mqtt_retain_state',
    'mqtt_command_subscriptions', 'syslog_enabled', 'syslog_audit_enabled',
    'release_auto_download', 'release_auto_activate',
}
INTEGER_FIELDS = {
    'log_buffer_lines': (50, 2000), 'mqtt_port': (1, 65535),
    'mqtt_qos': (0, 1), 'syslog_port': (1, 65535),
    'release_check_weekday': (0, 6),
}
TEXT_FIELDS = {
    'timezone_name': 64, 'ha_discovery_prefix': 64,
    'mqtt_server': 253, 'mqtt_username': 128, 'mqtt_base_topic': 128,
    'mqtt_state_topic': 192, 'mqtt_command_topic': 192,
    'mqtt_response_topic': 192, 'mqtt_availability_topic': 192,
    'syslog_host': 253, 'release_base_url': 512,
}
CHOICE_FIELDS = {
    'loglevel': ('ERROR', 'INFO', 'DEBUG'),
    'syslog_transport': ('udp', 'tcp', 'tls'),
    'release_channel': ('stable', 'beta', 'alpha'),
    'release_check_schedule': ('disabled', 'daily', 'weekly'),
}
SECRET_FIELDS = {'wifi_password': 64, 'mqtt_password': 256}
ALLOWED_SETTINGS = set(BOOLEAN_FIELDS) | set(INTEGER_FIELDS) | set(TEXT_FIELDS) | set(CHOICE_FIELDS) | {'ntp_servers', 'release_check_time'}


def _text(value, name, maximum, allow_empty=False):
    value = str(value or '').strip()
    if (not value and not allow_empty) or len(value) > maximum:
        raise ValueError(name + ' is invalid')
    return value


def normalize_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('configuration profile must be an object')
    unknown = set(profile) - {
        'format_version', 'name', 'description', 'settings', 'secrets'
    }
    if unknown:
        raise ValueError('unknown configuration profile field: ' + sorted(unknown)[0])
    if int(profile.get('format_version', FORMAT_VERSION)) != FORMAT_VERSION:
        raise ValueError('unsupported configuration profile format')
    settings = profile.get('settings')
    if not isinstance(settings, dict) or not settings:
        raise ValueError('configuration profile settings must be a non-empty object')
    unknown = set(settings) - ALLOWED_SETTINGS
    if unknown:
        raise ValueError('unsupported configuration profile setting: ' + sorted(unknown)[0])
    normalized = {}
    for name in BOOLEAN_FIELDS:
        if name in settings:
            if not isinstance(settings[name], bool):
                raise ValueError(name + ' must be true or false')
            normalized[name] = settings[name]
    for name, limits in INTEGER_FIELDS.items():
        if name in settings:
            setting = settings[name]
            if not isinstance(setting, int) or isinstance(setting, bool):
                raise ValueError(name + ' must be an integer')
            if setting < limits[0] or setting > limits[1]:
                raise ValueError(name + ' is outside the supported range')
            normalized[name] = setting
    for name, maximum in TEXT_FIELDS.items():
        if name in settings:
            normalized[name] = _text(
                settings[name], name, maximum,
                allow_empty=name in ('mqtt_server', 'mqtt_username', 'syslog_host'),
            )
    for name, choices in CHOICE_FIELDS.items():
        if name in settings:
            choice = (
                str(settings[name]).lower()
                if name in ('syslog_transport', 'release_channel',
                            'release_check_schedule')
                else str(settings[name]).upper()
            )
            if choice not in choices:
                raise ValueError(name + ' is invalid')
            normalized[name] = choice
    if 'ntp_servers' in settings:
        servers = settings['ntp_servers']
        if not isinstance(servers, list) or not 1 <= len(servers) <= 4:
            raise ValueError('ntp_servers must contain 1 to 4 servers')
        normalized['ntp_servers'] = [_text(server, 'NTP server', 253) for server in servers]
    if 'release_check_time' in settings:
        value = str(settings['release_check_time'])
        try:
            hour, minute = [int(part) for part in value.split(':')]
        except (TypeError, ValueError):
            raise ValueError('release_check_time must use HH:MM') from None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError('release_check_time is invalid')
        normalized['release_check_time'] = '{:02}:{:02}'.format(hour, minute)
    if 'release_base_url' in normalized and not normalized['release_base_url'].startswith('https://'):
        raise ValueError('release_base_url must use HTTPS')
    secrets = profile.get('secrets') or {}
    if not isinstance(secrets, dict):
        raise ValueError('configuration profile secrets must be an object')
    unknown = set(secrets) - set(SECRET_FIELDS)
    if unknown:
        raise ValueError('unsupported configuration profile secret: ' + sorted(unknown)[0])
    normalized_secrets = {
        name: _text(secrets[name], name, maximum)
        for name, maximum in SECRET_FIELDS.items() if secrets.get(name)
    }
    return {
        'format_version': FORMAT_VERSION,
        'name': _text(profile.get('name'), 'configuration profile name', 64),
        'description': _text(profile.get('description', ''), 'configuration profile description', 256, True),
        'settings': normalized,
        'secrets': normalized_secrets,
    }
