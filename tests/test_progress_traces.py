"""Retention bounds and payload-free exports, without network or devices."""
import json
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from progress_traces import ATTEMPTS, FAILURES, DEPLOYMENTS, clean_traces, retain_attempt
from fleet_repository import FleetRepository


class TraceBoundsTests(unittest.TestCase):
    def test_separate_storage_preserves_bounds_on_restart_and_avoids_rewriting_old_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fleet.db'
            store = FleetRepository(path)
            self.addCleanup(store.close)
            store.register({'id': 'test', 'host': 'test.local'})
            # Simulate a real 3.2.9 database without the new trace table.
            store.connection.execute('DROP TABLE progress_traces')
            store.set_metadata('schema_version', '5')
            store.close()
            store = FleetRepository(path)
            self.addCleanup(store.close)
            self.assertEqual(store.metadata('schema_version'), '6')
            traces = {}
            for batch in range(6):
                traces = retain_attempt(traces, [{'id': 'batch-' + str(batch), 'update': {}}],
                    {'workflow_stage': 'status', 'finished_at': batch})
                store.record_progress_poll('test', {'api': {'last_success': batch}}, traces)
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM progress_traces').fetchone()[0], DEPLOYMENTS)
            changes = store.connection.total_changes
            traces = retain_attempt(traces, [{'id': 'batch-5', 'update': {}}],
                                    {'workflow_stage': 'status', 'finished_at': 10})
            store.record_progress_poll('test', {}, traces)
            self.assertEqual(store.connection.total_changes - changes, 2)  # poll state + current group only
            store.close()
            store = FleetRepository(path)
            self.addCleanup(store.close)
            self.assertEqual(store.get_progress_traces('test'), traces)
            self.assertNotIn('progress_traces', store.get_device('test')['poll_state'])
            store.delete_device('test')
            self.assertEqual(store.get_progress_traces('test')['deployments'], {})

    def test_recovery_does_not_evict_failures_and_deployment_count_is_bounded(self):
        traces = {}
        for batch in range(10):
            deployment = {'id': 'batch-' + str(batch), 'update': {'release_sequence': batch + 1,
                'release_type': 'universal'}}
            for attempt in range(100):
                traces = retain_attempt(traces, [deployment], {'started_at': attempt,
                    'finished_at': attempt, 'workflow_stage': 'status',
                    'failure': 'socket_timeout' if attempt < 12 else '', 'reads': []})
        groups = traces['deployments']
        self.assertEqual(len(groups), DEPLOYMENTS)
        for group in groups.values():
            self.assertEqual(group['attempts_total'], 100)
            self.assertEqual(group['failures_total'], 12)
            self.assertEqual(len(group['attempts']), ATTEMPTS)
            self.assertEqual(len(group['failures']), FAILURES)
            self.assertTrue(all(not item['failure'] for item in group['attempts']))
            self.assertTrue(all(item['failure'] for item in group['failures']))
        self.assertLess(len(json.dumps(traces)), 50000)

    def test_corrupt_saved_records_are_bounded_and_whitelisted(self):
        for value in (None, [], 'invalid', {'deployments': []}, {'deployments': {'bad': None}}):
            self.assertEqual(clean_traces(value)['deployments'], {})
        entry = {'started_at': float('nan'), 'finished_at': True, 'secret': 'private-key',
                 'reads': [{'path': '/api/v3/health?token=private-key', 'headers': 'private-key',
                            'error': 'private-key', 'stage_ms': ['private-key'], 'stage': ['private-key']}]}
        trace = clean_traces({'deployments': {'batch': {'attempts': [entry] * 1000,
                                                       'failures': [entry] * 1000}}})
        text = json.dumps(trace)
        self.assertNotIn('private-key', text)
        self.assertNotIn('NaN', text)
        self.assertIn('/api/v3/health', text)
        self.assertEqual(len(trace['deployments']['batch']['attempts']), ATTEMPTS)

    def test_many_devices_have_independent_bounded_evidence(self):
        records = {}
        for device in range(100):
            records[device] = retain_attempt({}, [{'id': 'batch', 'update': {}}],
                {'workflow_stage': 'status', 'failure': 'socket_timeout' if device % 2 else ''})
        self.assertEqual(sum(item['deployments']['batch']['failures_total'] for item in records.values()), 50)
        first = clean_traces(records[0])
        first['deployments']['batch']['attempts'][0]['failure'] = 'request_failed'
        self.assertEqual(records[0]['deployments']['batch']['attempts'][0]['failure'], '')
