import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot_md_management/rootfs/app'))
from update_operation_contract import reduce_operation, PHASES


class OperationReducerTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = {'update_operation_id': 'a'*32, 'policy_sequence': 500}
        self.update = {'release_sequence': 2828, 'release_type': 'universal'}
        self.value = {'id': 'a'*32, 'device_id': 'device', 'contract': 1,
            'policy_sequence': 500, 'release_sequence': 2828, 'release_type': 'universal',
            'state': 'checking', 'phase': 'inspect', 'completed': ['queued'],
            'revision': 2, 'boot_id': 'boot-one', 'sample_sequence': 0, 'percent': None,
            'detail': 'Checking signed release'}

    def reduce(self, previous=None):
        return reduce_operation(self.value, self.dispatch, 'device', self.update, previous)

    def test_completed_versions_are_not_an_input_or_a_successful_job(self):
        self.value.update(application_version='newer', firmware_version='newer', health={'healthy': True})
        self.assertEqual(self.reduce()['status'], 'checking')

    def test_exact_id_device_release_policy_are_required(self):
        for key, value in {'id': 'b'*32, 'device_id': 'another', 'release_sequence': 2829,
                           'release_type': 'application', 'policy_sequence': 501}.items():
            original = self.value[key]
            self.value[key] = value
            with self.assertRaises(ValueError): self.reduce()
            self.value[key] = original

    def test_no_rewind_from_stale_duplicate_or_old_percent(self):
        previous = self.reduce()['operation']
        self.assertIsNone(self.reduce(previous))
        self.value['revision'] = 1
        self.assertIsNone(self.reduce(previous))
        self.value.update(revision=3, state='staging', phase='core_write', sample_sequence=10, percent=40)
        previous = self.reduce(previous)['operation']
        self.value.update(sample_sequence=9, percent=30)
        self.assertIsNone(self.reduce(previous))

    def test_conflicting_same_revision_or_phase_regression_rejected(self):
        previous = self.reduce()['operation']
        self.value.update(phase='core_write', state='staging')
        with self.assertRaises(ValueError): self.reduce(previous)
        self.value.update(phase='queued', revision=3)
        with self.assertRaises(ValueError): self.reduce(previous)

    def test_reboot_snapshot_resets_only_sample_not_durable_revision(self):
        self.value.update(state='staging', phase='core_write', sample_sequence=100, percent=90)
        previous = self.reduce()['operation']
        self.value.update(boot_id='boot-two', boot_epoch=1, sample_sequence=0, percent=None)
        result = self.reduce(previous)
        self.assertEqual(result['status'], 'staging')
        self.assertIsNone(result['progress']['percent'])

    def test_complete_requires_full_confirmed_milestones(self):
        self.value.update(state='complete', phase='complete')
        with self.assertRaises(ValueError): self.reduce()
        self.value['completed'] = list(PHASES)
        self.assertEqual(self.reduce()['status'], 'complete')

    def test_uncertain_recovery_is_unresolved_not_cancelled_or_failed(self):
        self.value['state'] = 'interrupted'
        self.assertEqual(self.reduce()['status'], 'unresolved')

    def test_terminal_job_cannot_restart_even_with_newer_revision(self):
        self.value['state'] = 'failed'
        previous = self.reduce()['operation']
        self.value.update(state='checking', revision=3)
        with self.assertRaises(ValueError): self.reduce(previous)

    def test_malformed_samples_and_unbounded_reports_rejected(self):
        for key, value in {'percent': 100, 'revision': True, 'sample_sequence': -1,
                           'detail': 'x'*161, 'completed': ['unknown']}.items():
            original = self.value[key]
            self.value[key] = value
            with self.assertRaises(ValueError): self.reduce()
            self.value[key] = original

    def test_projection_excludes_credentials_and_large_extraneous_data(self):
        self.value.update(secret='sensitive', commands=['large'], signature='key-material')
        result = self.reduce()
        self.assertNotIn('secret', result['operation'])
        self.assertNotIn('commands', result['operation'])


if __name__ == '__main__':
    unittest.main()
