"""Retain browser-reported USB seeding progress without receiving device secrets."""

import json
import re
import threading
import time
import uuid


class USBSeedManager:
    def __init__(self, store, now=time.time):
        self.store = store
        self.now = now
        self.lock = threading.RLock()
        try:
            self.jobs = json.loads(store.metadata('usb_seed_jobs', '[]'))
        except ValueError:
            self.jobs = []
        # No browser USB operation independently verifies the first-run AP.
        for job in self.jobs:
            job['startup_confirmed'] = False

    def _save(self):
        self.store.set_metadata('usb_seed_jobs', json.dumps(self.jobs[-100:]))

    def snapshot(self):
        with self.lock:
            for job in self.jobs:
                if job['status'] == 'running' and self.now() - job['updated_at'] > 180:
                    job.update(status='interrupted', detail='Browser connection lost; the hardware outcome is unknown. Inspect the board before retrying.')
                    self._audit(job)
                    self._save()
            return {'jobs': json.loads(json.dumps(self.jobs[-100:]))}

    def _audit(self, job):
        self.store.record_audit('device.usb_recovery' if job.get('kind') == 'recovery' else 'device.seed', job['status'], job['image'], 'Browser USB',
                                {'id': job['id'], 'sha256': job['sha256'], 'detail': job['detail'],
                                 'kind': job.get('kind', 'seed'),
                                 'application': job.get('application', ''),
                                 'application_sha256': job.get('application_sha256', ''),
                                 'startup_confirmed': False})

    def start(self, request):
        kind = request.get('kind', 'seed')
        if kind not in ('seed', 'recovery'):
            raise ValueError('Unknown USB operation.')
        recovery = kind == 'recovery'
        if request.get('confirmation') != ('RECOVER' if recovery else 'SEED') or request.get('credential_retained') is not True:
            raise ValueError('Type ' + ('RECOVER' if recovery else 'SEED') + ' and retain the matching setup password file.')
        if recovery and request.get('erase_confirmed') is not True:
            raise ValueError('Confirm erasure of device configuration, credentials, certificates and logs.')
        name = str(request.get('image', ''))[:128]
        digest = str(request.get('sha256', ''))
        if not name.endswith('.iotcore' if recovery else '.factory.bin') or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Choose a validated IoT-MD ' + ('signed core bundle.' if recovery else 'factory image.'))
        application = str(request.get('application', ''))[:128]
        application_digest = str(request.get('application_sha256', ''))
        if recovery and (not application.endswith('.iotapp') or not re.fullmatch(r'[a-f0-9]{64}', application_digest)):
            raise ValueError('Choose a validated signed application bundle.')
        with self.lock:
            job = {'id': uuid.uuid4().hex, 'kind': kind, 'image': name, 'sha256': digest,
                   'created_at': int(self.now()), 'updated_at': int(self.now()),
                   'status': 'running', 'stage': 1, 'percent': 0,
                   'startup_confirmed': False,
                   'detail': 'Signed bundles validated locally. Inspecting the secured UART device.' if recovery else 'Factory image validated locally. Inspecting the USB board.'}
            if recovery:
                job.update(application=application, application_sha256=application_digest)
            self.jobs.append(job)
            self.jobs = self.jobs[-100:]
            self._save()
            self._audit(job)
            return dict(job)

    def update(self, identifier, request):
        with self.lock:
            job = next((item for item in self.jobs if item['id'] == identifier), None)
            if not job:
                raise ValueError('USB seed operation does not exist.')
            if job['status'] in ('complete', 'failed'):
                return dict(job)
            stage = int(request.get('stage', job['stage']))
            percent = int(request.get('percent', job['percent']))
            status = request.get('status', 'running')
            final_stage = 7 if job.get('kind') == 'recovery' else 5
            if not job['stage'] <= stage <= final_stage or not 0 <= percent <= 100 or status not in ('running', 'complete', 'failed'):
                raise ValueError('USB seeding progress is invalid.')
            if status == 'complete' and stage != final_stage:
                raise ValueError('USB operation must finish verification and reboot steps before completion.')
            if stage == job['stage']:
                percent = max(percent, job['percent'])
            job.update(stage=stage, percent=percent, status=status,
                       updated_at=int(self.now()), detail=str(request.get('detail', job['detail']))[:512])
            self._save()
            if status in ('complete', 'failed'):
                self._audit(job)
            return dict(job)
