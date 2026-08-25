"""Fleet policy canonicalization and the independent policy-signing trust domain."""

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


P256_ORDER = int(
    'ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551', 16
)


def policy_message(policy):
    maintenance = policy.get('maintenance', {}) or {}
    updates = policy.get('updates', {}) or {}
    telemetry = policy.get('telemetry', {}) or {}
    fields = [
        'fleet-policy', str(policy.get('format_version', 1)), 'esp32-s3',
        str(policy.get('policy_sequence', '')), str(policy.get('issued_at', '')),
        str(policy.get('not_before', '')), str(policy.get('expires_at', '')),
        str(policy.get('target_device', '')), str(policy.get('target_cohort', '')),
        ','.join(str(value) for value in maintenance.get('weekdays', ()) or ()),
        str(maintenance.get('start_minute', '')),
        str(maintenance.get('duration_minutes', '')),
        str(updates.get('channel', '')),
        str(bool(updates.get('automatic_download', False))),
        str(bool(updates.get('automatic_activation', False))),
        str(updates.get('maximum_consecutive_failures', '')),
        str(bool(telemetry.get('enabled', False))),
        str(telemetry.get('minimum_interval_s', '')),
        ','.join(str(value) for value in telemetry.get('severities', ()) or ()),
    ]
    commands = policy.get('commands', ()) or ()
    fields.append(str(len(commands)))
    for command in commands:
        fields.extend((
            str(command.get('id', '')), str(command.get('action', '')),
            str(command.get('release_sequence', '')),
        ))
    return ('\n'.join(fields) + '\n').encode()


class PolicySigner:
    def __init__(self, private_path, public_path):
        self.private_path = Path(private_path)
        self.public_path = Path(public_path)
        self.private_key = self._load_or_create()

    def _load_or_create(self):
        if self.private_path.exists():
            return serialization.load_pem_private_key(
                self.private_path.read_bytes(), password=None
            )
        self.private_path.parent.mkdir(parents=True, exist_ok=True)
        private = ec.generate_private_key(ec.SECP256R1())
        self.private_path.write_bytes(private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        os.chmod(self.private_path, 0o600)
        numbers = private.public_key().public_numbers()
        self.public_path.write_bytes(
            numbers.x.to_bytes(32, 'big') + numbers.y.to_bytes(32, 'big')
        )
        os.chmod(self.public_path, 0o644)
        return private

    def sign(self, policy):
        value = json.loads(json.dumps(policy))
        value.pop('signature', None)
        value['target_board'] = 'esp32-s3'
        value['signature_scheme'] = 'ecdsa-p256-sha256'
        der = self.private_key.sign(policy_message(value), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        if s > P256_ORDER // 2:
            s = P256_ORDER - s
        value['signature'] = (
            r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
        ).hex()
        return value
