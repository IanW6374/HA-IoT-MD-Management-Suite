"""Verified GitHub release import and Management-Suite-signed catalogs."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature,
)


SIGNATURE_SCHEME = 'ecdsa-p256-sha256'
TARGET_BOARD = 'esp32-s3'
P256_ORDER = int(
    'ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551', 16
)
MAGIC_TYPES = {
    b'IOTA1\n': 'application', b'IOTC1\n': 'firmware', b'IOTU1\n': 'universal',
}
TYPE_NAMES = {
    'application': 'iotapp', 'firmware': 'iotcore', 'universal': 'iotuni',
}
ASSET_SUFFIXES = ('.iotapp', '.iotcore', '.iotuni', '.json')
SOURCE_MARKER = b'IoTMD_SOURCE_REVISION:'
MAX_ASSET_BYTES = 16 * 1024 * 1024
VERSION_PATTERN = re.compile(r'^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$')


def _component_fields(components):
    components = components if isinstance(components, dict) else {}
    modules = components.get('modules', {})
    if not isinstance(modules, dict):
        modules = {}
    fields = [str(components.get('runtime', '')), str(len(modules))]
    for name in sorted(modules):
        fields.extend((str(name), str(modules[name])))
    return fields


def signed_message(bundle_type, manifest):
    """Match IoT-MD's compact, cross-runtime signed message encoding."""
    format_version = int(manifest.get('format_version', 1))
    fields = [
        str(bundle_type), str(format_version),
        str(manifest.get('target_board', manifest.get('platform', ''))),
    ]
    if bundle_type == 'iotapp':
        fields.extend((
            str(manifest.get('min_recovery_api', 1)),
            str(manifest.get('max_recovery_api', 6)),
            str(manifest.get('version', '')),
            str(manifest.get('release_sequence', '')),
            str(manifest.get('minimum_core_api', '')),
            str(manifest.get('minimum_config_api', '')),
            str(manifest.get('maximum_config_api', '')),
        ))
        fields.extend(_component_fields(manifest.get('components')))
        entries = sorted((
            str(entry.get('path', '')), str(entry.get('size', '')),
            str(entry.get('sha256', '')).lower(),
        ) for entry in manifest.get('files', []))
        for entry in entries:
            fields.extend(entry)
    elif bundle_type == 'iotcore':
        fields.extend((
            str(manifest.get('version', '')),
            str(manifest.get('release_sequence', '')),
            str(manifest.get('minimum_core_api', '')),
            str(manifest.get('size', '')),
            str(manifest.get('sha256', '')).lower(),
        ))
    elif bundle_type == 'iotuni':
        fields.extend((
            str(manifest.get('version', '')),
            str(manifest.get('release_sequence', '')),
        ))
        for name in ('firmware', 'application'):
            component = manifest.get(name, {})
            fields.extend((
                name, str(component.get('version', '')),
                str(component.get('release_sequence', '')),
                str(component.get('size', '')),
                str(component.get('sha256', '')).lower(),
            ))
        if format_version >= 2:
            fields.extend((
                ','.join(str(value) for value in manifest.get('activation_order', ())),
                str(bool(manifest.get('maintenance_required', False))),
                str(manifest.get('rollback_policy', '')),
                str(manifest.get('trial_timeout_s', '')),
            ))
    elif bundle_type in ('release', 'release-catalog'):
        fields.extend((
            str(manifest.get('channel', '')), str(manifest.get('type', '')),
            str(manifest.get('version', '')),
            str(manifest.get('release_sequence', '')),
            str(manifest.get('url', '')), str(manifest.get('size', '')),
            str(manifest.get('sha256', '')).lower(),
            str(manifest.get('minimum_core_api', '')),
            str(manifest.get('minimum_config_api', '')),
            str(manifest.get('maximum_config_api', '')),
            str(manifest.get('notes', '')),
            str(manifest.get('published_at', '')),
        ))
        fields.extend(_component_fields(manifest.get('components')))
    else:
        raise ValueError('unsupported signed message type: ' + str(bundle_type))
    return ('\n'.join(fields) + '\n').encode()


def _raw_public_key(path):
    raw = Path(path).read_bytes()
    if len(raw) != 64:
        raw = raw.strip()
    if len(raw) == 128:
        raw = bytes.fromhex(raw.decode())
    if len(raw) != 64:
        raise ValueError('update verification key must contain exactly 64 bytes')
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'),
        ec.SECP256R1(),
    ).public_key()


def _verify_signature(public_key, bundle_type, manifest):
    if manifest.get('signature_scheme') != SIGNATURE_SCHEME:
        raise ValueError('artifact does not use the required signature scheme')
    try:
        signature = bytes.fromhex(str(manifest.get('signature', '')))
        if len(signature) != 64:
            raise ValueError
        r = int.from_bytes(signature[:32], 'big')
        s = int.from_bytes(signature[32:], 'big')
        if not 1 <= r < P256_ORDER or not 1 <= s <= P256_ORDER // 2:
            raise ValueError
        public_key.verify(
            encode_dss_signature(r, s), signed_message(bundle_type, manifest),
            ec.ECDSA(hashes.SHA256())
        )
    except Exception as exc:
        raise ValueError('artifact signature verification failed') from exc


def _source_revision(payload):
    revisions = set()
    start = 0
    while True:
        index = payload.find(SOURCE_MARKER, start)
        if index < 0:
            break
        value = payload[index + len(SOURCE_MARKER):index + len(SOURCE_MARKER) + 40]
        try:
            text = value.decode().lower()
        except UnicodeError:
            text = ''
        if len(text) == 40 and all(character in '0123456789abcdef' for character in text):
            revisions.add(text)
        start = index + len(SOURCE_MARKER)
    if len(revisions) > 1:
        raise ValueError('artifact contains conflicting source revisions')
    return next(iter(revisions), '')


class ArtifactVerifier:
    def __init__(self, public_key_path):
        self.public_key_path = Path(public_key_path)

    def verify(self, path):
        path = Path(path)
        public_key = _raw_public_key(self.public_key_path)
        with path.open('rb') as stream:
            magic = stream.read(6)
            release_type = MAGIC_TYPES.get(magic)
            if not release_type:
                raise ValueError('asset is not an IoT-MD release bundle')
            manifest_size = int.from_bytes(stream.read(4), 'big')
            if not 0 < manifest_size <= 65535:
                raise ValueError('bundle manifest length is invalid')
            manifest = json.loads(stream.read(manifest_size))
            if not isinstance(manifest, dict):
                raise ValueError('bundle manifest must be an object')
            accepted_formats = (2, 3) if release_type == 'universal' else (6,)
            if int(manifest.get('format_version', 0)) not in accepted_formats:
                raise ValueError('bundle does not use the current IoT-MD format')
            if manifest.get('target_board', manifest.get('platform')) != TARGET_BOARD:
                raise ValueError('bundle target board is not supported')
            sequence = int(manifest.get('release_sequence', 0))
            if sequence <= 0 or not str(manifest.get('version', '')).strip():
                raise ValueError('bundle version or release sequence is invalid')
            _verify_signature(public_key, TYPE_NAMES[release_type], manifest)
            signed_content = bytearray()
            if release_type == 'application':
                for entry in manifest.get('files', []):
                    size = int(entry.get('size', -1))
                    payload = stream.read(size)
                    if size < 0 or len(payload) != size:
                        raise ValueError('application bundle payload is truncated')
                    if hashlib.sha256(payload).hexdigest() != str(entry.get('sha256', '')).lower():
                        raise ValueError('application bundle payload hash failed')
                    signed_content.extend(payload)
            elif release_type == 'firmware':
                size = int(manifest.get('size', -1))
                payload = stream.read(size)
                if size < 0 or len(payload) != size:
                    raise ValueError('firmware bundle payload is truncated')
                if hashlib.sha256(payload).hexdigest() != str(manifest.get('sha256', '')).lower():
                    raise ValueError('firmware bundle payload hash failed')
                signed_content.extend(payload)
            else:
                for name in ('firmware', 'application'):
                    component = manifest.get(name, {})
                    size = int(component.get('size', -1))
                    payload = stream.read(size)
                    if size < 0 or len(payload) != size:
                        raise ValueError('universal ' + name + ' payload is truncated')
                    if hashlib.sha256(payload).hexdigest() != str(component.get('sha256', '')).lower():
                        raise ValueError('universal ' + name + ' payload hash failed')
                    signed_content.extend(payload)
            if stream.read(1):
                raise ValueError('bundle contains unsigned trailing content')
        return {
            'kind': release_type, 'version': str(manifest['version']),
            'release_sequence': sequence, 'manifest': manifest,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'size': path.stat().st_size,
            'source_revision': _source_revision(signed_content),
        }


class CatalogSigner:
    def __init__(self, private_path, public_path):
        self.private_path = Path(private_path)
        self.public_path = Path(public_path)
        self.private_key = self._load_or_create()

    def _load_or_create(self):
        if self.private_path.exists():
            private = serialization.load_pem_private_key(
                self.private_path.read_bytes(), password=None
            )
        else:
            self.private_path.parent.mkdir(parents=True, exist_ok=True)
            private = ec.generate_private_key(ec.SECP256R1())
            temporary = self.private_path.with_suffix('.tmp')
            temporary.write_bytes(private.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.private_path)
        numbers = private.public_key().public_numbers()
        self.public_path.write_bytes(
            numbers.x.to_bytes(32, 'big') + numbers.y.to_bytes(32, 'big')
        )
        os.chmod(self.public_path, 0o644)
        return private

    def sign(self, descriptor):
        value = json.loads(json.dumps(descriptor))
        value.pop('signature', None)
        value['format_version'] = 3
        value['target_board'] = TARGET_BOARD
        value['signature_scheme'] = SIGNATURE_SCHEME
        der = self.private_key.sign(
            signed_message('release-catalog', value), ec.ECDSA(hashes.SHA256())
        )
        r, s = decode_dss_signature(der)
        if s > P256_ORDER // 2:
            s = P256_ORDER - s
        value['signature'] = (r.to_bytes(32, 'big') + s.to_bytes(32, 'big')).hex()
        return value


class ReleaseCatalog:
    def __init__(self, state_path, release_root, verifier, signer, source_repo,
                 base_url, github_token='', opener=None, now=None):
        self.state_path = Path(state_path)
        self.release_root = Path(release_root)
        self.verifier = verifier
        self.signer = signer
        self.source_repo = str(source_repo).strip()
        self.base_url = str(base_url).rstrip('/')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.source_repo):
            raise ValueError('GitHub repository must use owner/name format')
        if not self.base_url.startswith('https://'):
            raise ValueError('release base URL must use HTTPS')
        self.github_token = str(github_token or '').strip()
        self.opener = opener or urllib.request.urlopen
        self.now = now or time.time
        self.lock = threading.RLock()
        self.release_root.joinpath('bundles').mkdir(parents=True, exist_ok=True)
        for channel in ('stable', 'beta'):
            self.release_root.joinpath(channel).mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _empty(self):
        return {'format_version': 1, 'last_sync': 0, 'last_error': '', 'releases': []}

    def _load(self):
        try:
            value = json.loads(self.state_path.read_text())
            if value.get('format_version') != 1 or not isinstance(value.get('releases'), list):
                raise ValueError
            return value
        except Exception:
            return self._empty()

    def _save(self):
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, indent=2) + '\n')
        os.replace(temporary, self.state_path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _request(self, url, accept='application/vnd.github+json'):
        headers = {
            'Accept': accept, 'User-Agent': 'IoT-MD-Management-Suite/2',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        if self.github_token:
            headers['Authorization'] = 'Bearer ' + self.github_token
        return urllib.request.Request(url, headers=headers)

    def _download(self, asset, destination):
        expected_size = int(asset.get('size', 0))
        if not 0 < expected_size <= MAX_ASSET_BYTES:
            raise ValueError('GitHub asset size is invalid: ' + str(asset.get('name', '')))
        request = self._request(asset['browser_download_url'], 'application/octet-stream')
        hasher = hashlib.sha256()
        count = 0
        with self.opener(request, timeout=60) as response, destination.open('wb') as stream:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                count += len(chunk)
                if count > expected_size or count > MAX_ASSET_BYTES:
                    raise ValueError('GitHub asset exceeds its declared size')
                hasher.update(chunk)
                stream.write(chunk)
        if count != expected_size:
            raise ValueError('GitHub asset download ended early')
        digest = hasher.hexdigest()
        github_digest = str(asset.get('digest') or '')
        if github_digest and github_digest != 'sha256:' + digest:
            raise ValueError('GitHub asset digest verification failed')
        return digest

    @staticmethod
    def _provenance(provenance, verified_assets):
        subjects = {
            str(item.get('name', '')): str((item.get('digest') or {}).get('sha256', '')).lower()
            for item in provenance.get('subject', []) if isinstance(item, dict)
        }
        for asset in verified_assets.values():
            if subjects.get(asset['name']) != asset['sha256']:
                raise ValueError('provenance does not bind ' + asset['name'])
        dependencies = (
            provenance.get('predicate', {}).get('buildDefinition', {})
            .get('resolvedDependencies', [])
        )
        revisions = [
            str((item.get('digest') or {}).get('gitCommit', '')).lower()
            for item in dependencies
            if str(item.get('uri', '')).rstrip('/').endswith('/IoT-Modular-Device')
        ]
        return revisions[0] if len(revisions) == 1 else ''

    def _import_release(self, release):
        tag = str(release.get('tag_name', '')).strip()
        match = VERSION_PATTERN.fullmatch(tag)
        if not match:
            raise ValueError('release tag is not a supported semantic version: ' + tag)
        version = match.group(1)
        assets = {
            str(asset.get('name', '')): asset for asset in release.get('assets', [])
            if str(asset.get('name', '')).endswith(ASSET_SUFFIXES) and
            Path(str(asset.get('name', ''))).name == str(asset.get('name', ''))
        }
        bundle_assets = {
            name: asset for name, asset in assets.items()
            if name.endswith(('.iotapp', '.iotcore', '.iotuni'))
        }
        if not any(name.endswith('.iotapp') for name in bundle_assets) or not any(
            name.endswith('.iotcore') for name in bundle_assets
        ):
            raise ValueError(tag + ' does not contain application and core bundles')
        provenance_name = next((name for name in assets if name.startswith('provenance-')), '')
        sbom_name = next((name for name in assets if name.startswith('sbom-')), '')
        if not provenance_name or not sbom_name:
            raise ValueError(tag + ' does not contain provenance and SBOM assets')

        incoming = Path(tempfile.mkdtemp(prefix='.incoming-', dir=self.release_root))
        try:
            downloaded = {}
            for name, asset in assets.items():
                path = incoming / name
                digest = self._download(asset, path)
                downloaded[name] = {'path': path, 'sha256': digest, 'name': name}
            provenance = json.loads(downloaded[provenance_name]['path'].read_text())
            json.loads(downloaded[sbom_name]['path'].read_text())
            verified = {}
            for name in bundle_assets:
                details = self.verifier.verify(downloaded[name]['path'])
                if details['version'] != version:
                    raise ValueError(name + ' version does not match tag ' + tag)
                if details['kind'] in verified:
                    raise ValueError(tag + ' contains duplicate ' + details['kind'] + ' bundles')
                details.update({'name': name, 'sha256': downloaded[name]['sha256']})
                verified[details['kind']] = details
            source_revision = self._provenance(provenance, verified)
            if not source_revision:
                raise ValueError('provenance has no unique IoT-MD source revision')
            for details in verified.values():
                if details['source_revision'] and details['source_revision'] != source_revision:
                    raise ValueError(details['name'] + ' source revision does not match provenance')
            sequences = {details['release_sequence'] for details in verified.values()}
            if len(sequences) != 1:
                raise ValueError('release bundle sequences do not match')
            for name, details in downloaded.items():
                destination = self.release_root / 'bundles' / name
                if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != details['sha256']:
                    raise ValueError('local release asset collision: ' + name)
                if not destination.exists():
                    os.replace(details['path'], destination)
            return {
                'tag': tag, 'version': version, 'github_release_id': int(release['id']),
                'prerelease': bool(release.get('prerelease')), 'draft': False,
                'published_at': str(release.get('published_at') or ''),
                'html_url': str(release.get('html_url') or ''),
                'source_revision': source_revision,
                'release_sequence': sequences.pop(),
                'assets': {
                    kind: {
                        key: value for key, value in details.items()
                        if key in ('name', 'sha256', 'size', 'version', 'release_sequence', 'kind')
                    } for kind, details in verified.items()
                },
                'provenance': provenance_name, 'sbom': sbom_name,
                'verified': True, 'imported_at': int(self.now()), 'channels': [],
            }
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

    def sync(self):
        url = 'https://api.github.com/repos/' + self.source_repo + '/releases?per_page=50'
        try:
            with self.opener(self._request(url), timeout=30) as response:
                releases = json.loads(response.read())
            if not isinstance(releases, list):
                raise ValueError('GitHub Releases response is invalid')
            imported = []
            errors = []
            with self.lock:
                known = {item['tag']: item for item in self.state['releases']}
                for release in releases:
                    if release.get('draft'):
                        continue
                    tag = str(release.get('tag_name', ''))
                    if tag in known and known[tag].get('github_release_id') == release.get('id'):
                        continue
                    try:
                        record = self._import_release(release)
                    except Exception as exc:
                        errors.append(tag + ': ' + str(exc))
                        continue
                    previous = known.get(tag, {})
                    record['channels'] = list(previous.get('channels', []))
                    known[tag] = record
                    imported.append(tag)
                self.state['releases'] = sorted(
                    known.values(), key=lambda item: (
                        int(item.get('release_sequence', 0)), item.get('tag', '')
                    ), reverse=True
                )
                self.state['last_sync'] = int(self.now())
                self.state['last_error'] = '; '.join(errors)[:2048]
                self._save()
            return {'imported': imported, 'errors': errors, 'inventory': self.snapshot()}
        except Exception as exc:
            with self.lock:
                self.state['last_sync'] = int(self.now())
                self.state['last_error'] = str(exc)[:2048]
                self._save()
            raise

    def promote(self, tag, channel):
        channel = str(channel)
        if channel not in ('stable', 'beta'):
            raise ValueError('release channel must be stable or beta')
        with self.lock:
            release = next((item for item in self.state['releases'] if item['tag'] == tag), None)
            if not release or not release.get('verified'):
                raise ValueError('release has not been imported and verified')
            descriptors = []
            for kind in ('application', 'firmware'):
                asset = release.get('assets', {}).get(kind)
                if not asset:
                    raise ValueError('release has no verified ' + kind + ' bundle')
                manifest = self.verifier.verify(self.release_root / 'bundles' / asset['name'])['manifest']
                descriptor = {
                    'format_version': 3, 'target_board': TARGET_BOARD,
                    'channel': channel, 'type': kind, 'version': release['version'],
                    'release_sequence': int(release['release_sequence']),
                    'url': self.base_url + '/bundles/' + asset['name'],
                    'size': int(asset['size']), 'sha256': asset['sha256'],
                    'minimum_core_api': int(manifest.get('minimum_core_api', 9)),
                    'minimum_config_api': int(manifest.get('minimum_config_api', 3)),
                    'maximum_config_api': int(manifest.get('maximum_config_api', 3)),
                    'notes': 'GitHub ' + release['tag'] + ' · Source: ' + release['source_revision'],
                    'published_at': release.get('published_at') or datetime.now(
                        timezone.utc
                    ).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
                    'signature_scheme': SIGNATURE_SCHEME,
                }
                if kind == 'application':
                    descriptor['components'] = manifest.get('components', {})
                descriptors.append(self.signer.sign(descriptor))
            index = dict(descriptors[0])
            index['releases'] = descriptors
            target = self.release_root / channel / 'latest.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(index, indent=2) + '\n')
            os.replace(temporary, target)
            for item in self.state['releases']:
                channels = set(item.get('channels', []))
                channels.discard(channel)
                item['channels'] = sorted(channels)
            release['channels'] = sorted(set(release.get('channels', [])) | {channel})
            self._save()
            return {'tag': tag, 'channel': channel, 'catalog': str(target)}
