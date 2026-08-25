"""Transactional SQLite repositories and durable jobs for IoTMD fleet state."""

import json
import sqlite3
import threading
import time
from pathlib import Path


SCHEMA_VERSION = 1


def _json(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True)


def _object(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except Exception:
        return default


class FleetRepository:
    def __init__(self, path, event_retention=5000, now=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event_retention = max(100, int(event_retention))
        self.now = now or (lambda: int(time.time()))
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
            ''')
            self.connection.execute(
                'INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',
                ('schema_version', str(SCHEMA_VERSION))
            )
            stored_version = int(self.connection.execute(
                'SELECT value FROM metadata WHERE key=?', ('schema_version',)
            ).fetchone()['value'])
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
        values = (
            identifier, release_sequence, str(request.get('channel') or 'alpha')[:16],
            _json(cohorts), 0, 'active', maximum_failures, 0, 0, '{}', self.now()
        )
        try:
            with self.lock, self.connection:
                self.connection.execute('''
                    INSERT INTO rollouts(
                        id,release_sequence,channel,cohorts,cohort_index,status,
                        maximum_failures,successes,failures,results,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
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
