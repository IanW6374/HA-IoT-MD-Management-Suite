"""Encrypted-at-rest storage for configuration-profile secrets."""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


MASK = '********'


class ProfileSecretCipher:
    def __init__(self, key_path):
        self.path = Path(key_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(self._key())

    def _key(self):
        try:
            return self.path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                with self.path.open('xb') as stream:
                    stream.write(key + b'\n')
                os.chmod(self.path, 0o600)
            except FileExistsError:
                return self.path.read_bytes().strip()
            return key

    def encrypt(self, secrets):
        if not secrets:
            return ''
        payload = json.dumps(
            secrets, separators=(',', ':'), sort_keys=True
        ).encode()
        return self.fernet.encrypt(payload).decode()

    def decrypt(self, token):
        if not token:
            return {}
        try:
            value = json.loads(self.fernet.decrypt(str(token).encode()))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError('stored profile secrets cannot be decrypted') from exc
        if not isinstance(value, dict):
            raise ValueError('stored profile secrets are invalid')
        return {str(key): str(secret) for key, secret in value.items()}

    @staticmethod
    def masked(secrets):
        return {str(key): MASK for key in (secrets or {})}
