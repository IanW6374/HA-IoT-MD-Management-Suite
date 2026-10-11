"""Offline saturation tests: no real devices, TLS credentials or mutations."""
import sys
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'
sys.path.insert(0, str(APP))
from device_scheduler import DeviceScheduler
from fleet_repository import FleetRepository
from fleet_service import DeviceClient, FleetController
import fleet_service


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FleetRepository(Path(self.directory.name) / 'fleet.db')
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def devices(self, count):
        ids = [f'device-{index:03}' for index in range(count)]
        for identifier in ids:
            self.store.register({'id': identifier, 'host': identifier + '.local'})
        return ids

    def test_configured_limits_reserve_progress_capacity(self):
        for workers in (2, 3, 8, 16, 32):
            with self.subTest(workers=workers):
                scheduler = DeviceScheduler(self.store, mock.Mock(), mock.Mock(), workers=workers)
                try:
                    self.assertEqual(scheduler.snapshot()['worker_limit'], workers)
                    self.assertEqual(scheduler.background_limit, workers // 2)
                    self.assertEqual(scheduler.progress_limit, workers - workers // 2)
                finally:
                    scheduler.close()

    def test_batch_poll_admission_coalesces_queued_and_running_reads(self):
        ids = self.devices(5)
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            self.store.enqueue_polls(ids, 1)
        finally:
            self.store.connection.set_trace_callback(None)
        self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
        self.assertEqual(statements.count('COMMIT'), 1)
        self.assertEqual(self.store.connection.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(len(self.store.active_jobs('poll')), 5)
        self.store.claim_job(poll_only=True)
        self.store.enqueue_polls(ids, 2)
        self.assertEqual(len(self.store.active_jobs('poll')), 5)
        self.assertEqual(len([job for job in self.store.active_jobs('poll') if job['status'] == 'running']), 1)

    def test_unchanged_live_state_does_not_increment_durable_revision(self):
        identifier = self.devices(1)[0]
        deployment = self.store.create_deployment({'profile_name': 'baseline'}, [identifier])
        first = self.store.set_deployment_target(deployment['id'], identifier, 'staging',
            'Working', progress={'phase': 'core_write', 'percent': 20})
        same = self.store.set_deployment_target(deployment['id'], identifier, 'staging',
            'Working', progress={'phase': 'core_write', 'percent': 20})
        self.assertEqual(same['revision'], first['revision'])
        changed = self.store.set_deployment_target(deployment['id'], identifier, 'staging',
            'Working', progress={'phase': 'core_write', 'percent': 21})
        self.assertGreater(changed['revision'], first['revision'])

    def drive(self, scheduler, condition, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            scheduler.tick()
            if condition():
                return
            time.sleep(.002)
        self.fail('Scheduler failed to make bounded progress')

    def test_50_and_100_device_fleets_progress_while_background_slots_are_stalled(self):
        for count in (50, 100):
            with self.subTest(devices=count):
                ids = self.devices(count)
                release = threading.Event()
                lock = threading.Lock()
                active, seen, background, overlap = set(), set(), set(), []
                maximum = 0
                def enter(identifier):
                    nonlocal maximum
                    with lock:
                        if identifier in active:
                            overlap.append(identifier)
                        active.add(identifier)
                        maximum = max(maximum, len(active))
                def leave(identifier):
                    with lock:
                        active.remove(identifier)
                def progress(identifier):
                    enter(identifier)
                    try:
                        time.sleep(.001)
                        with lock:
                            seen.add(identifier)
                    finally:
                        leave(identifier)
                def execute(job):
                    identifier = job['target']
                    enter(identifier)
                    try:
                        if job['kind'] == 'backup':
                            with lock:
                                background.add(identifier)
                            release.wait(4)
                        self.store.complete_job(job['id'])
                    finally:
                        leave(identifier)
                for identifier in ids[:4]:
                    self.store.enqueue_job('backup', identifier, idempotency_key=f'backup-{count}-{identifier}')
                scheduler = DeviceScheduler(self.store, mock.Mock(poll_progress=progress), execute,
                    workers=8, progress_interval=1)
                scheduler._active_devices = lambda: set()
                try:
                    self.drive(scheduler, lambda: len(background) == 4)
                    scheduler._active_devices = lambda: set(ids)
                    started = time.monotonic()
                    self.drive(scheduler, lambda: set(ids[4:]) <= seen)
                    self.assertFalse(release.is_set())
                    self.assertLessEqual(maximum, 8)
                    self.assertEqual(overlap, [])
                    self.assertLessEqual(scheduler.snapshot()['progress_workers'], 4)
                    print(f'Scheduler simulated load: {count} devices; {len(seen)} live reads '
                          f'while four background devices stalled; {time.monotonic()-started:.3f}s', flush=True)
                finally:
                    release.set()
                    scheduler.close()

    def test_failed_progress_backs_off_without_duplicate_or_abandoned_tasks(self):
        identifier = self.devices(1)[0]
        clock = [100.]
        calls = []
        def progress(device):
            calls.append(device)
            raise TimeoutError('Simulated unreachable device')
        scheduler = DeviceScheduler(self.store, mock.Mock(poll_progress=progress),
            lambda job: self.store.complete_job(job['id']), now=lambda: clock[0])
        scheduler._active_devices = lambda: {identifier}
        try:
            scheduler.tick()
            self.drive(scheduler, lambda: identifier in scheduler.progress)
            self.assertEqual(calls, [identifier])
            self.assertEqual(scheduler.progress[identifier]['due'], 110.)
            for _ in range(10):
                scheduler.tick()
            self.assertEqual(len(calls), 1)
            clock[0] = 110.
            self.drive(scheduler, lambda: len(calls) == 2)
            self.drive(scheduler, lambda: scheduler.progress[identifier]['failures'] == 2)
            self.assertEqual(scheduler.progress[identifier]['due'], 130.)
        finally:
            scheduler.close()

    def test_100_device_live_queue_keeps_moving_past_unreachable_devices(self):
        ids = self.devices(100)
        seen, failed = set(), set()
        lock = threading.Lock()
        def progress(identifier):
            if identifier in ids[:4]:
                time.sleep(.04)
                with lock:
                    failed.add(identifier)
                raise TimeoutError('Simulated bounded unreachable read')
            time.sleep(.001)
            with lock:
                seen.add(identifier)
        scheduler = DeviceScheduler(self.store, mock.Mock(poll_progress=progress),
            lambda job: self.store.complete_job(job['id']))
        scheduler._active_devices = lambda: set(ids)
        try:
            self.drive(scheduler, lambda: set(ids[4:]) <= seen)
            self.assertEqual(failed, set(ids[:4]))
            self.assertTrue(all(scheduler.progress[identifier]['failures'] == 1 for identifier in ids[:4]))
        finally:
            scheduler.close()

    def test_claim_skips_busy_device_actions_and_keeps_their_jobs_queued(self):
        one, two = self.devices(2)
        deployment = self.store.create_deployment({'profile_name': 'baseline'}, [one, two])
        first = self.store.enqueue_job('deployment', deployment['id'], {'device_id': one}, idempotency_key='first')
        second = self.store.enqueue_job('deployment', deployment['id'], {'device_id': two}, idempotency_key='second')
        self.assertEqual(self.store.claim_job(exclude_devices={one}, prefer_actions=True)['id'], second['id'])
        self.assertEqual(self.store.get_job(first['id'])['status'], 'queued')
        self.assertIsNone(self.store.claim_job(exclude_devices={one, two}))

    def test_different_device_actions_run_concurrently_and_a_busy_device_is_not_replayed(self):
        one, two = self.devices(2)
        deployment = self.store.create_deployment({'profile_name': 'baseline'}, [one, two])
        for device in (one, two):
            self.store.enqueue_job('deployment', deployment['id'], {'device_id': device}, idempotency_key=device)
        release, second = threading.Event(), threading.Event()
        def execute(job):
            if job['payload']['device_id'] == one:
                release.wait(3)
            self.store.complete_job(job['id'])
            if job['payload']['device_id'] == two:
                second.set()
        scheduler = DeviceScheduler(self.store, mock.Mock(), execute, workers=4)
        scheduler._active_devices = lambda: set()
        try:
            self.drive(scheduler, second.is_set)
            self.assertFalse(release.is_set())
            self.assertEqual(len(self.store.active_jobs('deployment')), 1)
        finally:
            release.set()
            scheduler.close()

    def test_long_action_queue_does_not_starve_background_health(self):
        ids = self.devices(20)
        for identifier in ids:
            self.store.enqueue_job('backup', identifier, idempotency_key='backup:' + identifier)
        ordered = []
        def execute(job):
            ordered.append(job['kind'])
            self.store.complete_job(job['id'])
        scheduler = DeviceScheduler(self.store, mock.Mock(), execute, workers=2)
        try:
            self.drive(scheduler, lambda: 'poll' in ordered)
            self.assertLessEqual(ordered.index('poll'), 4)
        finally:
            scheduler.close()

    def test_full_polls_defer_during_live_updates_without_disappearing(self):
        identifier = self.devices(1)[0]
        deployment = self.store.create_deployment({'update': {'release_sequence': 5, 'release_type': 'universal'}}, [identifier])
        self.store.set_deployment_target(deployment['id'], identifier, 'staging')
        kinds = []
        def execute(job):
            kinds.append(job['kind'])
            self.store.complete_job(job['id'])
        controller = mock.Mock()
        scheduler = DeviceScheduler(self.store, controller, execute)
        try:
            self.drive(scheduler, lambda: controller.poll_progress.called)
            self.assertEqual(kinds, [])
            self.assertEqual(len(self.store.active_jobs('poll', identifier)), 1)
            self.store.set_deployment_target(deployment['id'], identifier, 'complete')
            self.drive(scheduler, lambda: 'poll' in kinds)
        finally:
            scheduler.close()

    def test_live_deployments_are_not_hidden_behind_newer_history(self):
        identifier, other = self.devices(2)
        older = self.store.create_deployment({'update': {'release_sequence': 5, 'release_type': 'universal'}}, [identifier])
        self.store.set_deployment_target(older['id'], identifier, 'staging')
        for _ in range(205):
            newer = self.store.create_deployment({'profile_name': 'baseline'}, [other])
            self.store.set_deployment_target(newer['id'], other, 'complete')
        self.assertNotIn(older['id'], [item['id'] for item in self.store.list_deployments()])
        self.assertEqual([item['id'] for item in self.store.active_deployments()], [older['id']])

    def test_fast_progress_reads_matching_cancellation_ack_without_replaying_write(self):
        identifier = self.devices(1)[0]
        update = {'release_sequence': 2821, 'release_type': 'universal'}
        deployment = self.store.create_deployment({'update': update, 'activation': 'now'}, [identifier])
        self.store.set_deployment_target(deployment['id'], identifier, 'cancelling')
        controller = FleetController(self.store, None)
        paths = []
        def request(path):
            paths.append(path)
            return {'device': {}} if path.endswith('/inventory') else {'status': 'cancelled', **update}
        controller._client = mock.Mock(return_value=mock.Mock(request=request))
        controller.poll_progress(identifier)
        self.assertEqual(paths, ['/api/v3/device/inventory', '/api/v3/fleet/update-cancel'])
        self.assertEqual(self.store.get_deployment(deployment['id'])['status'], 'cancelled')

    def test_fast_progress_never_reads_configuration_or_events_or_confirms_cached_health(self):
        identifier = self.devices(1)[0]
        update = {'release_sequence': 2821, 'release_type': 'universal', 'version': 'alpha116'}
        deployment = self.store.create_deployment({'update': update, 'activation': 'now'}, [identifier])
        self.store.set_deployment_target(deployment['id'], identifier, 'installing')
        inventory = {'device': {'release_sequence': 2821, 'firmware_release_sequence': 2821}}
        controller = FleetController(self.store, None)
        paths = []
        def request(path):
            paths.append(path)
            if path.endswith('/inventory'):
                return inventory
            raise TimeoutError('Health unavailable')
        controller._client = mock.Mock(return_value=mock.Mock(request=request))
        with self.assertRaises(TimeoutError):
            controller.poll_progress(identifier)
        self.assertEqual(paths, ['/api/v3/device/inventory', '/api/v3/health'])
        self.assertEqual(self.store.get_deployment(deployment['id'])['results'][identifier]['status'], 'installing')
        self.assertGreater(controller.progress_diagnostics()[identifier]['last_received'], 0)
        self.assertEqual(self.store.get_device(identifier)['last_seen'], 0)
        controller._client = mock.Mock(return_value=mock.Mock(request=lambda path:
            inventory if path.endswith('/inventory') else {'health': {}}))
        controller.poll_progress(identifier)
        self.assertEqual(self.store.get_deployment(deployment['id'])['results'][identifier]['status'], 'complete')

    def test_fast_inventory_trial_is_not_completion_even_when_versions_match(self):
        identifier = self.devices(1)[0]
        update = {'release_sequence': 2821, 'release_type': 'universal'}
        deployment = self.store.create_deployment({'update': update, 'activation': 'now'}, [identifier])
        self.store.set_deployment_target(deployment['id'], identifier, 'installing')
        inventory = {'device': {'release_sequence': 2821, 'firmware_release_sequence': 2821,
            'update_progress': {'release_sequence': 2821, 'type': 'universal',
                'universal_status': 'trial', 'universal_sequence': 2821}}}
        controller = FleetController(self.store, None)
        client = mock.Mock(request=mock.Mock(return_value=inventory))
        controller._client = mock.Mock(return_value=client)
        controller.poll_progress(identifier)
        client.request.assert_called_once_with('/api/v3/device/inventory')
        self.assertEqual(self.store.get_deployment(deployment['id'])['results'][identifier]['status'], 'installing')

    def test_live_clients_reuse_discovery_but_cannot_send_mutations(self):
        identifier = self.devices(1)[0]
        controller = FleetController(self.store, None)
        client = DeviceClient(self.store.get_device(identifier, public=False))
        paths = []
        def response(request, **kwargs):
            paths.append(request.full_url.split(':8444')[-1])
            return io.BytesIO(json.dumps({'api_version': 3, 'device': {}}).encode())
        with mock.patch.object(controller, '_client', return_value=client) as factory, \
                mock.patch.object(client, '_context', return_value=object()), \
                mock.patch.object(fleet_service.urllib.request, 'urlopen', side_effect=response):
            controller.poll_progress(identifier)
            controller.poll_progress(identifier)
            factory.assert_called_once()
            self.assertEqual(paths, ['/api/v3', '/api/v3/device/inventory', '/api/v3/device/inventory'])
            with self.assertRaisesRegex(ValueError, 'cannot mutate'):
                client.request('/api/v3/configuration/restart', 'POST', {})
            self.assertEqual(len(paths), 3)

    def test_manual_request_saturation_cannot_take_live_read_capacity(self):
        identifier = self.devices(1)[0]
        controller = FleetController(self.store, None, worker_limit=8)
        record = self.store.get_device(identifier, public=False)
        normal = controller._client(record)
        live = controller._progress_client(record)
        self.assertIsNot(normal.request_slots, live.request_slots)
        normal_reserved = live_reserved = 0
        try:
            while normal.request_slots.acquire(blocking=False):
                normal_reserved += 1
            while live.request_slots.acquire(blocking=False):
                live_reserved += 1
            self.assertEqual(normal_reserved, 4)
            self.assertEqual(live_reserved, 4)
            self.assertEqual(normal_reserved + live_reserved, 8)
        finally:
            for _ in range(normal_reserved):
                normal.request_slots.release()
            for _ in range(live_reserved):
                live.request_slots.release()

    def test_live_client_is_invalidated_after_credential_rotation(self):
        identifier = self.devices(1)[0]
        controller = FleetController(self.store, None)
        record = self.store.get_device(identifier, public=False)
        state = SimpleNamespace(st_ino=1, st_mtime_ns=100, st_size=20)
        with mock.patch.object(controller, '_client', side_effect=[mock.Mock(), mock.Mock()]) as factory, \
                mock.patch.object(fleet_service.os, 'stat', return_value=state):
            first = controller._progress_client(record)
            self.assertIs(controller._progress_client(record), first)
            state.st_mtime_ns = 200
            self.assertIsNot(controller._progress_client(record), first)
            self.assertEqual(factory.call_count, 2)
