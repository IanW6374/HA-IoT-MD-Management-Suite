"""Atomic migration of local fleet references; hardware identities stay intact."""

import hashlib
import json
import os
import sqlite3
import uuid


def hostname_identity(host):
    host = str(host or '').strip().lower().rstrip('.')
    if not host or len(host) > 253 or any(
        character.isspace() or character in '/\\?#@' for character in host
    ) or '://' in host:
        raise ValueError('enter a hostname or IP address, without a URL')
    identifier = host if len(host) <= 64 else host[:43] + '-' + hashlib.sha256(host.encode()).hexdigest()[:20]
    return host, identifier


def migrate_hostname_identities(store):
    """Run before workers start. Back up SQLite and update every local reference."""
    db = store.connection
    with store.lock:
        rows = db.execute('SELECT id,host FROM devices').fetchall()
        normalized = [(row['id'], *hostname_identity(row['host'])) for row in rows]
        identifiers = [new for old, host, new in normalized]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError('hostname migration blocked: multiple devices use the same hostname; resolve duplicate entries first')
        mapping = {old: new for old, host, new in normalized if old != new}
        if not mapping and all(old_host['host'] == host for old_host, (_, host, _) in zip(rows, normalized)):
            return {}

        def encode(value):
            return json.dumps(value, separators=(',', ':'), sort_keys=True)

        def rekey(values):
            result = {}
            for key, value in values.items():
                new = mapping.get(key, key)
                if new in result:
                    raise ValueError('hostname migration would merge existing history or schedules')
                result[new] = value
            return result

        def job_key(row):
            key = row['idempotency_key']
            if row['kind'] in ('poll', 'backup'):
                prefixes = ['poll:'] if row['kind'] == 'poll' else ['backup:manual:', 'backup:scheduled:', 'backup:']
                for prefix in prefixes:
                    old = row['target']
                    if old in mapping and key.startswith(prefix + old + ':'):
                        return prefix + mapping[old] + key[len(prefix + old):]
            elif row['kind'] == 'deployment':
                old = json.loads(row['payload']).get('device_id')
                if old in mapping and key == 'deployment:' + row['target'] + ':' + old:
                    return 'deployment:' + row['target'] + ':' + mapping[old]
            return key

        jobs = db.execute('SELECT * FROM jobs').fetchall()
        keys = [job_key(row) for row in jobs]
        if len(keys) != len(set(keys)):
            raise ValueError('hostname migration would merge queued job identities')
        # Backup includes WAL contents, encrypted envelopes and counters. Keys are
        # unchanged, so existing ciphertext needs no decrypt/re-encrypt operation.
        snapshot = store.path.with_name(store.path.name + '.before-hostname-migration-' + uuid.uuid4().hex + '.db')
        descriptor = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        destination = sqlite3.connect(snapshot)
        try:
            db.backup(destination)
        finally:
            destination.close()
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('PRAGMA defer_foreign_keys=ON')
            temporary = uuid.uuid4().hex
            temporary_ids = {old: 'migration-' + temporary + '-' + str(index) for index, old in enumerate(mapping)}
            for old, temporary_id in temporary_ids.items():
                db.execute('UPDATE devices SET id=? WHERE id=?', (temporary_id, old))
            for old, host, new in normalized:
                current = temporary_ids.get(old, old)
                db.execute('UPDATE devices SET id=?,host=? WHERE id=?', (new, host, current))
            for table, column in (('events', 'device_id'), ('backups', 'device_id')):
                records = db.execute('SELECT rowid AS migration_rowid,' + column + ' FROM ' + table).fetchall()
                for record in records:
                    if record[column] in mapping:
                        db.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE rowid=?', (mapping[record[column]], record['migration_rowid']))
            for row in db.execute('SELECT id,targets,results FROM deployments').fetchall():
                targets = [mapping.get(value, value) for value in json.loads(row['targets'])]
                results = rekey(json.loads(row['results']))
                db.execute('UPDATE deployments SET targets=?,results=? WHERE id=?', (encode(targets), encode(results), row['id']))
            for row in db.execute('SELECT id,results FROM rollouts').fetchall():
                db.execute('UPDATE rollouts SET results=? WHERE id=?', (encode(rekey(json.loads(row['results']))), row['id']))
            for row in jobs:
                db.execute('UPDATE jobs SET idempotency_key=? WHERE id=?', ('migration:' + temporary + ':' + str(row['id']), row['id']))
            for row, key in zip(jobs, keys):
                payload = json.loads(row['payload'])
                if row['kind'] == 'deployment' and payload.get('device_id') in mapping:
                    payload['device_id'] = mapping[payload['device_id']]
                target = mapping.get(row['target'], row['target']) if row['kind'] in ('poll', 'backup') else row['target']
                db.execute('UPDATE jobs SET target=?,payload=?,idempotency_key=? WHERE id=?', (target, encode(payload), key, row['id']))
            for row in db.execute('SELECT * FROM audit_events').fetchall():
                subject = mapping.get(row['subject'], row['subject']) if row['action'].startswith('device.') or row['action'] == 'backup.schedule' else row['subject']
                target = row['target']
                if row['action'] == 'deployment.created':
                    target = ', '.join(mapping.get(value, value) for value in target.split(', '))
                elif row['action'].startswith(('deployment.', 'profile.', 'backup.')):
                    target = mapping.get(target, target)
                detail = json.loads(row['detail'])
                for field in ('device_id', 'source_device_id', 'target_device_id'):
                    if isinstance(detail.get(field), str):
                        detail[field] = mapping.get(detail[field], detail[field])
                db.execute('UPDATE audit_events SET subject=?,target=?,detail=? WHERE id=?', (subject, target, encode(detail), row['id']))
            metadata_updates = {}
            metadata_rows = db.execute('SELECT key,value FROM metadata').fetchall()
            for row in metadata_rows:
                key, value = row['key'], row['value']
                if key == 'backup_device_settings':
                    value = encode(rekey(json.loads(value)))
                elif key == 'attention_acknowledged':
                    acknowledgements = {}
                    for old_key, fingerprint in json.loads(value).items():
                        old = old_key[len('device:'):] if old_key.startswith('device:') else ''
                        new_key = 'device:' + mapping[old] if old in mapping else old_key
                        if old in mapping:
                            device = store.get_device(mapping[old])
                            old_fingerprint = hashlib.sha256(encode({'key': old_key, 'status': 'failed', 'detail': device['last_error']}).encode()).hexdigest()
                            if fingerprint == old_fingerprint:
                                fingerprint = hashlib.sha256(encode({'key': new_key, 'status': 'failed', 'detail': device['last_error']}).encode()).hexdigest()
                        if new_key in acknowledgements:
                            raise ValueError('hostname migration would merge attention acknowledgements')
                        acknowledgements[new_key] = fingerprint
                    value = encode(acknowledgements)
                elif key.startswith('backup_last_schedule_date:'):
                    suffix = key[len('backup_last_schedule_date:'):]
                    if suffix in mapping:
                        key = 'backup_last_schedule_date:' + mapping[suffix]
                if key in metadata_updates:
                    raise ValueError('hostname migration would merge backup schedule markers')
                metadata_updates[key] = value
            for row in metadata_rows:
                db.execute('DELETE FROM metadata WHERE key=?', (row['key'],))
            for key, value in metadata_updates.items():
                db.execute('INSERT INTO metadata(key,value) VALUES(?,?)', (key, value))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('hostname migration failed its relationship checks')
            db.execute('INSERT INTO audit_events(action,status,subject,target,detail,created_at) VALUES(?,?,?,?,?,?)', ('device.identities.migrated', 'complete', '', '', encode({'mapping': mapping, 'database_snapshot': snapshot.name}), store.now()))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
        return mapping
