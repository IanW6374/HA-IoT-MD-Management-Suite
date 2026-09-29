"""Transactional SQLite repositories and durable jobs for IoT MD fleet state."""

import json
import sqlite3
import threading
import time
from pathlib import Path


SCHEMA_VERSION = 4


def _json(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True)


def _object(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except Exception:
        return default


class FleetRepository:
    def __init__(self, path, event_retention=5000, now=None, profile_cipher=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event_retention = max(100, int(event_retention))
        self.now = now or (lambda: int(time.time()))
        self.profile_cipher = profile_cipher
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._create_schema()
        except Exception:
            self.connection.close()
            raise

    def _configure(self):
        with self.connection:
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA busy_timeout=5000')

    def _create_schema(self):
        with self.lock, self.connection:
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    ca_path TEXT NOT NULL,
                    cert_path TEXT NOT NULL,
                    key_path TEXT NOT NULL,
                    cohort TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    inventory TEXT NOT NULL DEFAULT '{}',
                    health TEXT NOT NULL DEFAULT '{}',
                    fleet TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_seen INTEGER NOT NULL DEFAULT 0,
                    event_cursor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_device_sequence
                    ON events(device_id, sequence);
                CREATE TABLE IF NOT EXISTS rollouts (
                    id TEXT PRIMARY KEY,
                    release_sequence INTEGER NOT NULL,
                    release_type TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL,
                    cohorts TEXT NOT NULL,
                    cohort_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    maximum_failures INTEGER NOT NULL,
                    successes INTEGER NOT NULL,
                    failures INTEGER NOT NULL,
                    results TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    not_before INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS jobs_due
                    ON jobs(status, not_before, id);
                CREATE TABLE IF NOT EXISTS profiles (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    profile_type TEXT NOT NULL DEFAULT 'patch',
                    settings TEXT NOT NULL,
                    secrets TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    activation TEXT NOT NULL,
                    update_spec TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    profile_fields TEXT NOT NULL DEFAULT '[]',
                    targets TEXT NOT NULL,
                    results TEXT NOT NULL,
                    administrator_override INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    target TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_events_created
                    ON audit_events(created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    application_version TEXT NOT NULL,
                    firmware_version TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    recovery_secret TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS backups_device_created
                    ON backups(device_id,created_at DESC,id DESC);
            ''')
            rollout_columns = {
                row['name'] for row in self.connection.execute(
                    'PRAGMA table_info(rollouts)'
                ).fetchall()
            }
            if 'release_type' not in rollout_columns:
                self.connection.execute(
                    "ALTER TABLE rollouts ADD COLUMN release_type TEXT NOT NULL DEFAULT ''"
                )
            profile_columns = {
                row['name'] for row in self.connection.execute(
                    'PRAGMA table_info(profiles)'
                ).fetchall()
            }
            if 'secrets' not in profile_columns:
                self.connection.execute(
                    "ALTER TABLE profiles ADD COLUMN secrets TEXT NOT NULL DEFAULT ''"
                )
            if 'profile_type' not in profile_columns:
                self.connection.execute(
                    "ALTER TABLE profiles ADD COLUMN profile_type TEXT NOT NULL DEFAULT 'patch'"
                )
            deployment_columns = {
                row['name'] for row in self.connection.execute(
                    'PRAGMA table_info(deployments)'
                ).fetchall()
            }
            if 'profile_fields' not in deployment_columns:
                self.connection.execute(
                    "ALTER TABLE deployments ADD COLUMN profile_fields TEXT NOT NULL DEFAULT '[]'"
                )
            self.connection.execute(
                'INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',
                ('schema_version', str(SCHEMA_VERSION))
            )
            stored_version = int(self.connection.execute(
                'SELECT value FROM metadata WHERE key=?', ('schema_version',)
            ).fetchone()['value'])
            if stored_version in (1, 2, 3):
                self.connection.execute(
                    'UPDATE metadata SET value=? WHERE key=?',
                    (str(SCHEMA_VERSION), 'schema_version')
                )
                stored_version = SCHEMA_VERSION
            if stored_version != SCHEMA_VERSION:
                raise RuntimeError(
                    'fleet database schema ' + str(stored_version) +
                    ' is incompatible with required schema ' + str(SCHEMA_VERSION) +
                    '; clean-seed the add-on data directory'
                )
            self.connection.execute(
                'INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',
                ('next_policy_sequence', '1')
            )

    def metadata(self, key, default=None):
        with self.lock:
            row = self.connection.execute(
                'SELECT value FROM metadata WHERE key=?', (str(key),)
            ).fetchone()
        return default if row is None else row['value']

    def set_metadata(self, key, value):
        with self.lock, self.connection:
            self.connection.execute('''
                INSERT INTO metadata(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (str(key), str(value)))
        return value

    @staticmethod
    def _backup(row, include_payload=False, profile_cipher=None):
        if row is None:
            return None
        value = dict(row)
        value['id'] = int(value['id'])
        if include_payload:
            value['envelope'] = _object(value['envelope'], {})
            if profile_cipher is None:
                raise RuntimeError('backup recovery encryption is unavailable')
            value['password'] = profile_cipher.decrypt(
                value.pop('recovery_secret')
            ).get('password', '')
        else:
            value.pop('envelope', None)
            value.pop('recovery_secret', None)
        return value

    def save_backup(self, device, envelope, password, source='manual'):
        if self.profile_cipher is None:
            raise RuntimeError('backup recovery encryption is unavailable')
        if not isinstance(envelope, dict) or envelope.get('format') != 'iotmd-secure-backup':
            raise ValueError('device returned an invalid complete backup')
        encoded = _json(envelope)
        import hashlib
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        inventory = (device.get('inventory') or {}).get('device') or {}
        now = self.now()
        with self.lock, self.connection:
            cursor = self.connection.execute('''
                INSERT INTO backups(
                    device_id,device_name,application_version,firmware_version,
                    envelope,recovery_secret,digest,size_bytes,source,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ''', (
                str(device.get('id') or '')[:64],
                str(device.get('name') or device.get('id') or '')[:64],
                str(inventory.get('application_version') or '')[:64],
                str(inventory.get('firmware_version') or '')[:64], encoded,
                self.profile_cipher.encrypt({'password': str(password)}),
                digest, len(encoded.encode()), str(source)[:32], now,
            ))
        return self.get_backup(cursor.lastrowid)

    def get_backup(self, identifier, include_payload=False):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM backups WHERE id=?', (int(identifier),)
            ).fetchone()
        return self._backup(row, include_payload, self.profile_cipher)

    def list_backups(self, limit=500):
        limit = max(1, min(2000, int(limit)))
        with self.lock:
            rows = self.connection.execute('''
                SELECT * FROM backups ORDER BY created_at DESC,id DESC LIMIT ?
            ''', (limit,)).fetchall()
        return [self._backup(row) for row in rows]

    def delete_backup(self, identifier):
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'DELETE FROM backups WHERE id=?', (int(identifier),)
            )
        if cursor.rowcount != 1:
            raise ValueError('backup does not exist')
        return {'deleted': True, 'id': int(identifier)}

    def enforce_backup_retention(self, device_id, keep):
        keep = max(1, min(365, int(keep)))
        with self.lock, self.connection:
            rows = self.connection.execute('''
                SELECT id FROM backups WHERE device_id=?
                ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?
            ''', (str(device_id), keep)).fetchall()
            if rows:
                self.connection.executemany(
                    'DELETE FROM backups WHERE id=?',
                    [(row['id'],) for row in rows]
                )
        return len(rows)

    @staticmethod
    def _device(row, public=True):
        if row is None:
            return None
        value = dict(row)
        value['enabled'] = bool(value['enabled'])
        for field in ('inventory', 'health', 'fleet'):
            value[field] = _object(value[field], {})
        if public:
            for field in ('ca_path', 'cert_path', 'key_path'):
                value.pop(field, None)
        return value

    @staticmethod
    def _rollout(row):
        if row is None:
            return None
        value = dict(row)
        value['cohorts'] = _object(value['cohorts'], [])
        value['results'] = _object(value['results'], {})
        return value

    @staticmethod
    def _deployment(row):
        if row is None:
            return None
        value = dict(row)
        value['update'] = _object(value.pop('update_spec'), {})
        value['profile_fields'] = _object(value['profile_fields'], [])
        value['targets'] = _object(value['targets'], [])
        value['results'] = _object(value['results'], {})
        value['administrator_override'] = bool(value['administrator_override'])
        return value

    def register(self, record):
        identifier = str(record.get('id') or '')[:64]
        host = str(record.get('host') or '')[:253]
        if not identifier:
            raise ValueError('device id is required')
        if not host:
            raise ValueError('device host is required')
        port = int(record.get('port', 8444))
        if not 1 <= port <= 65535:
            raise ValueError('device port is invalid')
        values = (
            identifier, str(record.get('name') or identifier)[:64], host, port,
            str(record.get('ca_path') or '')[:512],
            str(record.get('cert_path') or '')[:512],
            str(record.get('key_path') or '')[:512],
            str(record.get('cohort') or 'default')[:64],
            1 if record.get('enabled', True) else 0,
        )
        with self.lock, self.connection:
            self.connection.execute('''
                INSERT INTO devices(
                    id,name,host,port,ca_path,cert_path,key_path,cohort,enabled
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, host=excluded.host, port=excluded.port,
                    ca_path=excluded.ca_path, cert_path=excluded.cert_path,
                    key_path=excluded.key_path, cohort=excluded.cohort,
                    enabled=excluded.enabled
            ''', values)
        return self.get_device(identifier)

    def update_device(self, identifier, changes):
        current = self.get_device(identifier, public=False)
        if not current:
            raise ValueError('device is not registered')
        allowed = {'name', 'host', 'port', 'cohort', 'enabled'}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError('unsupported device field: ' + sorted(unknown)[0])
        record = dict(current)
        record.update(changes)
        record['id'] = current['id']
        return self.register(record)

    def get_device(self, identifier, public=True):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM devices WHERE id=?', (str(identifier),)
            ).fetchone()
        return self._device(row, public)

    def list_devices(self, public=True):
        with self.lock:
            rows = self.connection.execute(
                'SELECT * FROM devices ORDER BY id'
            ).fetchall()
        return [self._device(row, public) for row in rows]

    def device_ids(self, enabled_only=False):
        query = 'SELECT id FROM devices'
        if enabled_only:
            query += ' WHERE enabled=1'
        query += ' ORDER BY id'
        with self.lock:
            return [row['id'] for row in self.connection.execute(query).fetchall()]

    def count_devices(self):
        with self.lock:
            return int(self.connection.execute(
                'SELECT COUNT(*) FROM devices'
            ).fetchone()[0])

    def delete_device(self, identifier):
        identifier = str(identifier)
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'DELETE FROM devices WHERE id=?', (identifier,)
            )
        if cursor.rowcount != 1:
            raise ValueError('device is not registered')
        return {'deleted': True, 'id': identifier}

    def set_device_error(self, identifier, detail):
        with self.lock, self.connection:
            self.connection.execute(
                'UPDATE devices SET last_error=? WHERE id=?',
                (str(detail)[:256], str(identifier))
            )

    def record_poll(self, identifier, inventory, health, events):
        cursor = int(events.get('cursor', 0) or 0)
        received_at = self.now()
        with self.lock, self.connection:
            self.connection.execute('''
                UPDATE devices SET inventory=?,health=?,fleet=?,last_error='',
                    last_seen=?,event_cursor=? WHERE id=?
            ''', (
                _json(inventory), _json(health),
                _json(inventory.get('fleet') or {}), received_at, cursor,
                str(identifier),
            ))
            for event in events.get('events', ()):
                self.connection.execute(
                    'INSERT INTO events(device_id,event,received_at) VALUES(?,?,?)',
                    (str(identifier), _json(event), received_at)
                )
            excess = self.connection.execute(
                'SELECT COUNT(*) FROM events'
            ).fetchone()[0] - self.event_retention
            if excess > 0:
                self.connection.execute('''
                    DELETE FROM events WHERE sequence IN (
                        SELECT sequence FROM events ORDER BY sequence LIMIT ?
                    )
                ''', (excess,))

    def list_events(self, limit=500):
        limit = max(1, min(self.event_retention, int(limit)))
        with self.lock:
            rows = self.connection.execute('''
                SELECT sequence,device_id,event,received_at FROM events
                ORDER BY sequence DESC LIMIT ?
            ''', (limit,)).fetchall()
        result = []
        for row in reversed(rows):
            result.append({
                'sequence': row['sequence'], 'device_id': row['device_id'],
                'event': _object(row['event'], {}),
                'received_at': row['received_at'],
            })
        return result

    def record_audit(self, action, status, subject='', target='', detail=None):
        now = self.now()
        with self.lock, self.connection:
            cursor = self.connection.execute('''
                INSERT INTO audit_events(
                    action,status,subject,target,detail,created_at
                ) VALUES(?,?,?,?,?,?)
            ''', (
                str(action)[:64], str(status)[:32], str(subject)[:128],
                str(target)[:256], _json(detail or {}), now,
            ))
        return {
            'id': cursor.lastrowid, 'action': str(action)[:64],
            'status': str(status)[:32], 'subject': str(subject)[:128],
            'target': str(target)[:256], 'detail': detail or {},
            'created_at': now,
        }

    def list_audit(self, limit=500):
        limit = max(1, min(self.event_retention, int(limit)))
        with self.lock:
            rows = self.connection.execute('''
                SELECT * FROM audit_events ORDER BY created_at DESC,id DESC LIMIT ?
            ''', (limit,)).fetchall()
        return [{
            **dict(row), 'detail': _object(row['detail'], {})
        } for row in rows]

    def create_deployment(self, request, targets):
        targets = [str(value)[:64] for value in targets]
        if not targets or len(targets) > 256 or len(set(targets)) != len(targets):
            raise ValueError('deployment requires 1 to 256 unique devices')
        update = request.get('update') or {}
        profile_name = str(request.get('profile_name') or '')[:64]
        profile_fields = request.get('profile_fields') or []
        if not isinstance(profile_fields, list):
            raise ValueError('profile field selection must be a list')
        profile_fields = list(dict.fromkeys(
            str(value)[:64] for value in profile_fields if str(value)
        ))
        if len(profile_fields) > 64:
            raise ValueError('too many profile fields selected')
        if profile_fields and not profile_name:
            raise ValueError('profile fields require a configuration profile')
        if not update and not profile_name:
            raise ValueError('select an update, a profile, or both')
        if update:
            release_sequence = int(update.get('release_sequence', 0))
            release_type = str(update.get('release_type') or '')
            if release_sequence <= 0:
                raise ValueError('deployment update sequence must be positive')
            if release_type not in ('application', 'firmware', 'universal'):
                raise ValueError('deployment update type is invalid')
            update = {
                'release_sequence': release_sequence,
                'release_type': release_type,
                'version': str(update.get('version') or '')[:64],
                'channel': str(update.get('channel') or 'alpha')[:16],
            }
        activation = str(request.get('activation') or 'schedule')
        if activation not in ('stage', 'schedule', 'now'):
            raise ValueError('deployment activation mode is invalid')
        identifier = str(request.get('id') or (
            'deployment-' + str(self.now()) + '-' +
            str(self.next_policy_sequence())
        ))[:64]
        results = {
            target: {
                'status': 'queued', 'detail': 'Waiting to start',
                'updated_at': self.now(),
            } for target in targets
        }
        now = self.now()
        with self.lock, self.connection:
            try:
                self.connection.execute('''
                    INSERT INTO deployments(
                        id,status,activation,update_spec,profile_name,profile_fields,
                        targets,results,administrator_override,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ''', (
                    identifier, 'queued', activation, _json(update), profile_name,
                    _json(profile_fields), _json(targets), _json(results),
                    1 if activation == 'now' else 0, now, now,
                ))
            except sqlite3.IntegrityError:
                raise ValueError('deployment id already exists')
        self.record_audit(
            'deployment.created', 'queued', identifier,
            ', '.join(targets), {
                'activation': activation, 'update': update,
                'profile_name': profile_name,
                'profile_fields': profile_fields,
                'administrator_override': activation == 'now',
            }
        )
        return self.get_deployment(identifier)

    def get_deployment(self, identifier):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM deployments WHERE id=?', (str(identifier),)
            ).fetchone()
        return self._deployment(row)

    def list_deployments(self, limit=200):
        limit = max(1, min(1000, int(limit)))
        with self.lock:
            rows = self.connection.execute('''
                SELECT * FROM deployments ORDER BY created_at DESC,id DESC LIMIT ?
            ''', (limit,)).fetchall()
        return [self._deployment(row) for row in rows]

    def set_deployment_target(self, identifier, device_id, status, detail=''):
        terminal = {'complete', 'failed', 'staged'}
        with self.lock, self.connection:
            deployment = self.get_deployment(identifier)
            if not deployment:
                raise ValueError('deployment does not exist')
            if device_id not in deployment['targets']:
                raise ValueError('device is not part of this deployment')
            previous = deployment['results'].get(device_id, {}).get('status', '')
            deployment['results'][device_id] = {
                'status': str(status)[:32], 'detail': str(detail)[:256],
                'updated_at': self.now(),
            }
            states = [
                value.get('status', 'queued')
                for value in deployment['results'].values()
            ]
            if any(value not in terminal for value in states):
                overall = 'active'
            elif all(value == 'failed' for value in states):
                overall = 'failed'
            elif any(value == 'failed' for value in states):
                overall = 'partial'
            elif all(value == 'staged' for value in states):
                overall = 'staged'
            else:
                overall = 'complete'
            self.connection.execute('''
                UPDATE deployments SET status=?,results=?,updated_at=? WHERE id=?
            ''', (
                overall, _json(deployment['results']), self.now(),
                str(identifier),
            ))
        if previous != status:
            self.record_audit(
                'deployment.target', status, identifier, device_id,
                {'detail': str(detail)[:256], 'previous': previous}
            )
        return self.get_deployment(identifier)

    def save_profile(self, profile):
        existing = self.get_profile(profile['name'], include_secrets=True)
        secrets = dict((existing or {}).get('secrets', {}))
        secrets.update(profile.get('secrets', {}))
        encrypted = self.profile_cipher.encrypt(secrets) if self.profile_cipher else ''
        with self.lock, self.connection:
            self.connection.execute('''
                INSERT INTO profiles(name,description,profile_type,settings,secrets,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    profile_type=excluded.profile_type,
                    settings=excluded.settings,
                    secrets=excluded.secrets,
                    updated_at=excluded.updated_at
            ''', (
                profile['name'], profile.get('description', ''),
                profile.get('profile_type', 'patch'),
                _json(profile['settings']), encrypted, self.now(),
            ))
        return self.get_profile(profile['name'])

    def get_profile(self, name, include_secrets=False):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM profiles WHERE name=?', (str(name),)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['format_version'] = 1
        value['settings'] = _object(value['settings'], {})
        encrypted = value.pop('secrets', '')
        secrets = self.profile_cipher.decrypt(encrypted) if self.profile_cipher else {}
        value['secrets'] = secrets if include_secrets else (
            self.profile_cipher.masked(secrets) if self.profile_cipher else {}
        )
        return value

    def list_profiles(self):
        with self.lock:
            rows = self.connection.execute(
                'SELECT * FROM profiles ORDER BY name'
            ).fetchall()
        return [self.get_profile(row['name']) for row in rows]

    def delete_profile(self, name):
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'DELETE FROM profiles WHERE name=?', (str(name),)
            )
        if cursor.rowcount != 1:
            raise ValueError('configuration profile does not exist')
        return {'deleted': True, 'name': str(name)}

    def next_policy_sequence(self):
        with self.lock, self.connection:
            row = self.connection.execute(
                'SELECT value FROM metadata WHERE key=?',
                ('next_policy_sequence',)
            ).fetchone()
            value = int(row['value'])
            self.connection.execute(
                'UPDATE metadata SET value=? WHERE key=?',
                (str(value + 1), 'next_policy_sequence')
            )
            return value

    def create_rollout(self, request):
        identifier = str(request.get('id') or (
            'rollout-' + str(self.now()) + '-' + str(self.next_policy_sequence())
        ))[:64]
        if any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_'
               for character in identifier):
            raise ValueError('rollout id contains unsupported characters')
        cohorts = [str(value)[:64] for value in request.get('cohorts', ()) if str(value)]
        if not cohorts or len(cohorts) > 16:
            raise ValueError('rollout requires 1 to 16 ordered cohorts')
        maximum_failures = int(request.get('maximum_failures', 1))
        if not 1 <= maximum_failures <= 100:
            raise ValueError('rollout failure threshold is invalid')
        release_sequence = int(request.get('release_sequence', 0))
        if release_sequence <= 0:
            raise ValueError('rollout release sequence must be positive')
        release_type = str(request.get('release_type', '') or '')
        if release_type not in ('', 'application', 'firmware', 'universal'):
            raise ValueError('rollout update type is invalid')
        values = (
            identifier, release_sequence, release_type,
            str(request.get('channel') or 'alpha')[:16],
            _json(cohorts), 0, 'active', maximum_failures, 0, 0, '{}', self.now()
        )
        try:
            with self.lock, self.connection:
                self.connection.execute('''
                    INSERT INTO rollouts(
                        id,release_sequence,release_type,channel,cohorts,cohort_index,status,
                        maximum_failures,successes,failures,results,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ''', values)
        except sqlite3.IntegrityError:
            raise ValueError('rollout id already exists')
        return self.get_rollout(identifier)

    def get_rollout(self, identifier):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM rollouts WHERE id=?', (str(identifier),)
            ).fetchone()
        return self._rollout(row)

    def list_rollouts(self):
        with self.lock:
            rows = self.connection.execute(
                'SELECT * FROM rollouts ORDER BY created_at,id'
            ).fetchall()
        return [self._rollout(row) for row in rows]

    def _save_rollout(self, rollout):
        self.connection.execute('''
            UPDATE rollouts SET cohort_index=?,status=?,successes=?,failures=?,
                results=? WHERE id=?
        ''', (
            rollout['cohort_index'], rollout['status'], rollout['successes'],
            rollout['failures'], _json(rollout['results']), rollout['id']
        ))

    def record_rollout_result(self, identifier, device_id, result, detail=''):
        with self.lock, self.connection:
            rollout = self.get_rollout(identifier)
            if not rollout:
                raise ValueError('rollout does not exist')
            if rollout['status'] not in ('active', 'stopped'):
                raise ValueError('rollout is already complete')
            device = self.get_device(device_id, public=False)
            if not device:
                raise ValueError('device is not registered')
            expected = rollout['cohorts'][rollout['cohort_index']]
            if device.get('cohort') != expected:
                raise ValueError('device is not in the active rollout cohort')
            normalized = 'complete' if str(result) == 'complete' else 'failed'
            previous = rollout['results'].get(str(device_id))
            if previous:
                counter = 'successes' if previous['result'] == 'complete' else 'failures'
                rollout[counter] -= 1
            rollout['results'][str(device_id)] = {
                'result': normalized, 'detail': str(detail)[:256],
                'recorded_at': self.now(),
            }
            rollout['successes' if normalized == 'complete' else 'failures'] += 1
            if rollout['failures'] >= rollout['maximum_failures']:
                rollout['status'] = 'stopped'
            self._save_rollout(rollout)
            return rollout

    def advance_rollout(self, identifier):
        with self.lock, self.connection:
            rollout = self.get_rollout(identifier)
            if not rollout:
                raise ValueError('rollout does not exist')
            if rollout['status'] == 'stopped':
                raise ValueError('rollout is stopped at its failure threshold')
            cohort = rollout['cohorts'][rollout['cohort_index']]
            targets = [
                value['id'] for value in self.list_devices(public=False)
                if value['enabled'] and value['cohort'] == cohort
            ]
            incomplete = [value for value in targets if value not in rollout['results']]
            failed = [
                value for value in targets
                if rollout['results'].get(value, {}).get('result') == 'failed'
            ]
            if incomplete:
                raise ValueError('active cohort still has incomplete devices')
            if failed:
                raise ValueError('active cohort contains failed devices')
            if rollout['cohort_index'] + 1 >= len(rollout['cohorts']):
                rollout['status'] = 'complete'
            else:
                rollout['cohort_index'] += 1
            self._save_rollout(rollout)
            return rollout

    def enqueue_job(self, kind, target, payload=None, idempotency_key=None,
                    not_before=None):
        now = self.now()
        key = str(idempotency_key or (
            str(kind) + ':' + str(target) + ':' + str(now)
        ))[:160]
        with self.lock, self.connection:
            self.connection.execute('''
                INSERT OR IGNORE INTO jobs(
                    idempotency_key,kind,target,payload,status,attempts,
                    not_before,created_at,updated_at,last_error
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ''', (
                key, str(kind)[:32], str(target)[:64], _json(payload or {}),
                'queued', 0, int(not_before if not_before is not None else now),
                now, now, ''
            ))
            row = self.connection.execute(
                'SELECT * FROM jobs WHERE idempotency_key=?', (key,)
            ).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row):
        if row is None:
            return None
        value = dict(row)
        value['payload'] = _object(value['payload'], {})
        return value

    def claim_job(self):
        now = self.now()
        with self.lock, self.connection:
            row = self.connection.execute('''
                SELECT * FROM jobs WHERE status='queued' AND not_before<=?
                ORDER BY not_before,id LIMIT 1
            ''', (now,)).fetchone()
            if row is None:
                return None
            updated = self.connection.execute('''
                UPDATE jobs SET status='running',attempts=attempts+1,updated_at=?
                WHERE id=? AND status='queued'
            ''', (now, row['id']))
            if updated.rowcount != 1:
                return None
            row = self.connection.execute(
                'SELECT * FROM jobs WHERE id=?', (row['id'],)
            ).fetchone()
        return self._job(row)

    def complete_job(self, identifier):
        with self.lock, self.connection:
            self.connection.execute('''
                UPDATE jobs SET status='complete',updated_at=?,last_error=''
                WHERE id=?
            ''', (self.now(), int(identifier)))

    def fail_job(self, identifier, detail, maximum_attempts=5):
        with self.lock, self.connection:
            row = self.connection.execute(
                'SELECT attempts FROM jobs WHERE id=?', (int(identifier),)
            ).fetchone()
            if row is None:
                return
            attempts = int(row['attempts'])
            status = 'failed' if attempts >= int(maximum_attempts) else 'queued'
            delay = min(3600, 2 ** min(attempts, 10))
            self.connection.execute('''
                UPDATE jobs SET status=?,not_before=?,updated_at=?,last_error=?
                WHERE id=?
            ''', (
                status, self.now() + delay, self.now(), str(detail)[:256],
                int(identifier)
            ))

    def close(self):
        with self.lock:
            self.connection.close()
