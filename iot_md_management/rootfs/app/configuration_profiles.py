"""Validation for reusable management configuration profiles."""

import base64


FORMAT_VERSION = 1
BOOLEAN_FIELDS = {
    'ha_discovery', 'mqtt_enabled', 'mqtt_retain_state',
    'mqtt_command_subscriptions', 'syslog_enabled', 'syslog_audit_enabled',
    'release_auto_download', 'release_auto_activate',
    'wifi_dhcp', 'api_enabled',
}
INTEGER_FIELDS = {
    'log_buffer_lines': (0, 500), 'mqtt_port': (1, 65535),
    'mqtt_qos': (0, 1), 'syslog_port': (1, 65535),
    'release_check_weekday': (0, 6),
    'portal_port': (1, 65535), 'portal_session_timeout_s': (300, 86400),
    'api_port': (1, 65535),
}
TEXT_FIELDS = {
    'device_name': 64, 'device_description': 256, 'wifi_ssid': 32,
    'wifi_ip_address': 15, 'wifi_subnet_mask': 15,
    'wifi_gateway': 15, 'wifi_dns_server': 15,
    'timezone_name': 64, 'ha_discovery_prefix': 64,
    'mqtt_server': 253, 'mqtt_username': 128, 'mqtt_base_topic': 128,
    'mqtt_state_topic': 192, 'mqtt_command_topic': 192,
    'mqtt_response_topic': 192, 'mqtt_availability_topic': 192,
    'syslog_host': 253, 'release_base_url': 512,
    'portal_username': 32, 'acme_directory_url': 512,
    'certificate_hostname': 253, 'portal_certificate_hostname': 253,
}
CHOICE_FIELDS = {
    'loglevel': ('ERROR', 'INFO', 'DEBUG'),
    'syslog_transport': ('udp', 'tls'),
    'release_channel': ('stable', 'beta', 'alpha'),
    'release_check_schedule': ('disabled', 'daily', 'weekly'),
    'portal_transport': ('auto', 'https', 'http'),
    'certificate_mode': ('self_signed', 'manual', 'acme', 'iot_ca'),
    'certificate_method': ('self_signed', 'manual', 'acme', 'iot_ca_auto', 'iot_ca_file'),
}
SECRET_FIELDS = {
    'wifi_password': 64, 'mqtt_password': 256,
    'certificate_mqtt_ca': 32768, 'certificate_release_ca': 32768,
    'certificate_syslog_ca': 32768, 'certificate_portal': 32768,
    'certificate_portal_key': 32768, 'certificate_api_server': 32768,
    'certificate_api_server_key': 32768, 'certificate_api_client_ca': 32768,
    'management_suite_key': 32768,
}
CERTIFICATE_SECRET_FIELDS = {
    name for name in SECRET_FIELDS if name.startswith('certificate_')
} | {'management_suite_key'}
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
        'format_version', 'name', 'description', 'profile_type', 'settings', 'secrets'
    }
    if unknown:
        raise ValueError('unknown configuration profile field: ' + sorted(unknown)[0])
    if int(profile.get('format_version', FORMAT_VERSION)) != FORMAT_VERSION:
        raise ValueError('unsupported configuration profile format')
    settings = profile.get('settings') or {}
    if not isinstance(settings, dict):
        raise ValueError('configuration profile settings must be an object')
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
                allow_empty=name in (
                    'wifi_ip_address', 'wifi_subnet_mask', 'wifi_gateway',
                    'wifi_dns_server', 'mqtt_server', 'mqtt_username',
                    'syslog_host', 'acme_directory_url',
                    'certificate_hostname', 'portal_certificate_hostname',
                ),
            )
    for name, choices in CHOICE_FIELDS.items():
        if name in settings:
            choice = (
                str(settings[name]).lower()
                if name in (
                    'syslog_transport', 'release_channel',
                    'release_check_schedule', 'portal_transport',
                    'certificate_mode', 'certificate_method',
                )
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
    normalized_secrets = {}
    for name, maximum in SECRET_FIELDS.items():
        if not secrets.get(name):
            continue
        value = _text(secrets[name], name, maximum)
        if name in CERTIFICATE_SECRET_FIELDS:
            try:
                decoded = base64.b64decode(value, validate=True)
            except Exception as exc:
                raise ValueError(name + ' is not valid certificate data') from exc
            if not decoded or len(decoded) > 16384:
                raise ValueError(name + ' exceeds the certificate size limit')
        normalized_secrets[name] = value
    for certificate, private_key in (
        ('certificate_portal', 'certificate_portal_key'),
        ('certificate_api_server', 'certificate_api_server_key'),
    ):
        if bool(normalized_secrets.get(certificate)) != bool(
            normalized_secrets.get(private_key)
        ):
            raise ValueError(
                certificate.replace('_', ' ') +
                ' and its private key must be supplied together'
            )
    if not normalized and not normalized_secrets:
        raise ValueError('configuration profile must contain at least one setting')
    profile_type = str(profile.get('profile_type') or 'patch').lower()
    if profile_type not in ('patch', 'baseline'):
        raise ValueError('configuration profile type is invalid')
    return {
        'format_version': FORMAT_VERSION,
        'profile_type': profile_type,
        'name': _text(profile.get('name'), 'configuration profile name', 64),
        'description': _text(profile.get('description', ''), 'configuration profile description', 256, True),
        'settings': normalized,
        'secrets': normalized_secrets,
    }
