"""Management-only retirement: no devices, TLS keys or mutation replay."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from fleet_repository import FleetRepository
from fleet_service import FleetController
from device_scheduler import DeviceScheduler


class StopTrackingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fleet.db'
        self.store = FleetRepository(self.path, now=lambda: 1000)
        self.addCleanup(self.store.close)
        for identifier in ('done', 'waiting', 'queued'):
            self.store.register({'id': identifier, 'host': identifier + '.local'})
        self.spec = {'update': {'release_sequence': 2825, 'release_type': 'universal', 'version': 'alpha120'}}
        self.deployment = self.store.create_deployment(self.spec, ['done', 'waiting', 'queued'])
        self.id = self.deployment['id']
        self.store.set_deployment_target(self.id, 'done', 'complete', 'alpha120 installed')
        self.store.set_deployment_target(self.id, 'waiting', 'cancelling', 'Cancellation acknowledgement uncertain',
                                         progress={'phase': 'inspect', 'completed': ['queued']})
        self.job = self.store.enqueue_job('deployment', self.id, {'device_id': 'queued'})
        self.poll = self.store.enqueue_job('poll', 'waiting')
        self.controller = FleetController(self.store, None)
        self.controller._client = mock.Mock(side_effect=AssertionError('No device requests allowed'))

    def stop(self):
        return self.controller.stop_tracking_deployment(self.id, 'stop tracking ' + self.id)

    def test_preserves_known_results_evidence_and_history_without_device_commands(self):
        before = self.store.get_deployment(self.id)
        stopped = self.stop()
        self.assertEqual(stopped['status'], 'unresolved')
        self.assertEqual(stopped['results']['done'], before['results']['done'])
        result = stopped['results']['waiting']
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['tracking_previous_status'], 'cancelling')
        self.assertEqual(result['tracking_previous_detail'], 'Cancellation acknowledgement uncertain')
        self.assertEqual(result['update_milestones'], before['results']['waiting']['update_milestones'])
        self.assertEqual(result['update_phase'], 'inspect')
        self.assertIn('unconfirmed', result['detail'])
        self.assertGreater(stopped['revision'], before['revision'])
        self.assertEqual(self.store.active_deployments(), [])
        self.assertEqual(self.store.get_job(self.job['id'])['status'], 'unresolved')
        self.assertEqual(self.store.get_job(self.poll['id'])['status'], 'queued')
        self.controller._client.assert_not_called()
        audit = [item for item in self.store.list_audit() if item['action'] == 'deployment.tracking_stopped']
        self.assertEqual(len(audit), 1)
        self.assertFalse(audit[0]['detail']['device_command_sent'])
        self.assertEqual(audit[0]['detail']['unresolved_targets'], ['waiting', 'queued'])

    def test_repeated_local_request_is_idempotent_and_old_job_never_dispatches(self):
        first = self.stop()
        self.assertEqual(self.stop(), first)
        self.controller.execute_deployment_target(self.id, 'queued')
        self.controller.cancel_deployment_target(self.id, 'waiting')
        self.controller._client.assert_not_called()
        self.store.complete_job(self.job['id'])
        self.store.fail_job(self.job['id'], 'Late worker failure')
        self.assertEqual(self.store.get_job(self.job['id'])['status'], 'unresolved')
        self.assertEqual(self.store.claim_job(poll_only=False), None)
        self.assertEqual(len([item for item in self.store.list_audit() if item['action'] == 'deployment.tracking_stopped']), 1)

    def test_new_deployment_allowed_but_late_reports_cannot_resurrect_old_tracking(self):
        stopped = self.stop()
        later = self.store.create_deployment(self.spec, ['waiting'])
        self.assertNotEqual(later['id'], self.id)
        for status in ('checking', 'staging', 'installing', 'cancelled', 'complete', 'failed'):
            changed = self.store.set_deployment_target(self.id, 'waiting', status, 'Late report',
                progress={'phase': 'complete', 'completed': ['complete']}, cancellation_refused=True)
            self.assertEqual(changed, stopped)

    def test_stop_tracking_survives_restart_and_leaves_live_poll_lane(self):
        self.stop()
        scheduler = DeviceScheduler(self.store, self.controller, mock.Mock())
        try:
            self.assertEqual(scheduler._active_devices(), set())
        finally:
            scheduler.close()
        self.store.close()
        reopened = FleetRepository(self.path)
        try:
            self.assertEqual(reopened.get_deployment(self.id)['status'], 'unresolved')
            self.assertEqual(reopened.active_deployments(), [])
            self.assertEqual(reopened.get_job(self.job['id'])['status'], 'unresolved')
        finally:
            reopened.close()

    def test_explicit_confirmation_and_known_deployment_required(self):
        for text in ('', 'stop', 'stop tracking other'):
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                self.controller.stop_tracking_deployment(self.id, text)
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.controller.stop_tracking_deployment('absent', 'stop tracking absent')
        self.assertEqual(self.store.get_deployment(self.id)['status'], 'active')

    def test_active_local_mutation_is_not_interrupted_and_lock_admission_is_bounded(self):
        ready, release = threading.Event(), threading.Event()
        def busy():
            with self.controller._action_lock('waiting'):
                ready.set();release.wait(5)
        thread = threading.Thread(target=busy)
        thread.start();self.assertTrue(ready.wait(2))
        try:
            with self.assertRaisesRegex(ValueError, 'still running'):
                self.stop()
            self.assertEqual(self.store.get_deployment(self.id)['status'], 'active')
            # An earlier acquired lock was released even after partial admission.
            self.assertTrue(self.controller._action_lock('done').acquire(blocking=False))
            self.controller._action_lock('done').release()
        finally:
            release.set();thread.join(2)
        self.assertEqual(self.stop()['status'], 'unresolved')

    def test_orphaned_running_queue_record_can_be_retired_without_replay(self):
        claimed = self.store.claim_job(poll_only=False)
        self.assertEqual(claimed['id'], self.job['id'])
        self.assertEqual(claimed['status'], 'running')
        self.stop()
        self.assertEqual(self.store.get_job(claimed['id'])['status'], 'unresolved')
        self.controller.execute_deployment_target(self.id, 'queued')
        self.controller._client.assert_not_called()

    def test_retirement_and_audit_are_one_transaction_on_storage_failure(self):
        before = self.store.get_deployment(self.id)
        self.store.connection.execute("CREATE TRIGGER fail_retirement_audit BEFORE INSERT ON audit_events WHEN NEW.action='deployment.tracking_stopped' BEGIN SELECT RAISE(ABORT,'test write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.stop()
        self.assertEqual(self.store.get_deployment(self.id), before)
        self.assertEqual(self.store.get_job(self.job['id'])['status'], 'queued')
        self.assertFalse(self.store.connection.in_transaction)

    def test_late_stop_request_does_not_rewrite_already_confirmed_job(self):
        for target in ('waiting', 'queued'):
            self.store.set_deployment_target(self.id, target, 'cancelled', 'Confirmed cancelled')
        before = self.store.get_deployment(self.id)
        self.assertEqual(before['status'], 'partial')
        self.assertEqual(self.stop(), before)
