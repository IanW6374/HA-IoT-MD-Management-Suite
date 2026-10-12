"""Bounded, fair device work admission; never queue tasks behind a busy device."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor


class DeviceScheduler:
    def __init__(self, store, controller, execute_job, workers=8,
                 progress_interval=5, poll_interval=60, now=time.monotonic):
        self.store, self.controller, self.execute_job = store, controller, execute_job
        self.workers = max(2, min(32, int(workers)))
        # Slow work may occupy at most half the pool, leaving live-read capacity.
        self.background_limit = max(1, self.workers // 2)
        self.progress_limit = self.workers - self.background_limit
        self.progress_interval = max(1, float(progress_interval))
        self.poll_interval = max(10, float(poll_interval))
        self.now = now
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='device')
        self.lock = threading.RLock()
        self.inflight, self.progress, self.full_due = {}, {}, {}
        self.waiting_since = {}
        self.last_device = ''
        self.action_admissions = 0
        self.stopped = threading.Event()

    def _active_devices(self):
        active = set()
        for deployment in self.store.active_deployments():
            if not deployment.get('update'):
                continue
            for identifier in deployment['targets']:
                status = deployment['results'].get(identifier, {}).get('status', 'queued')
                if status not in ('queued', 'running', 'complete', 'failed', 'cancelled'):
                    active.add(identifier)
        return active

    def _submit(self, kind, identifiers, callback):
        # Admission and reservations happen before submit; no unbounded executor queue.
        future = self.pool.submit(callback)
        self.inflight[future] = (kind, frozenset(identifiers), self.now())

    def tick(self):
        with self.lock:
            now = self.now()
            for future, (kind, identifiers, started) in list(self.inflight.items()):
                if not future.done():
                    continue
                del self.inflight[future]
                error = ''
                try:
                    future.result()
                except Exception as exc:
                    error = str(exc)[:256] or type(exc).__name__
                if kind == 'progress':
                    identifier = next(iter(identifiers))
                    failures = self.progress.get(identifier, {}).get('failures', 0) + 1 if error else 0
                    delay = min(60, self.progress_interval * 2 ** min(failures, 4))
                    observations = self.controller.progress_diagnostics()
                    observation = observations.get(identifier, {}) if isinstance(observations, dict) else {}
                    if error and observation.get('responsive'):
                        # Busy/updating or confirmation-only failure is not an
                        # offline device. Keep the live lane observing it.
                        delay = self.progress_interval if observation.get('received_on_attempt') else min(10, delay)
                    self.progress[identifier] = {'failures': failures, 'due': now + delay,
                        'duration_ms': round((now - started) * 1000), 'error': error}
            enabled = set(self.store.device_ids(enabled_only=True))
            self.progress = {key: value for key, value in self.progress.items() if key in enabled}
            self.full_due = {key: value for key, value in self.full_due.items() if key in enabled}
            busy = set().union(*(item[1] for item in self.inflight.values())) if self.inflight else set()
            active = self._active_devices() & enabled
            self.waiting_since = {key: self.waiting_since.get(key, now) for key in active}
            # Earliest-due first, with a rotating tie-break: large fleets cannot
            # keep serving their first few devices while starving the last ones.
            due = sorted((identifier for identifier in active - busy
                          if self.progress.get(identifier, {}).get('due', 0) <= now),
                         key=lambda key: (self.progress.get(key, {}).get('due', 0),
                                          key <= self.last_device, key))
            progressing = sum(item[0] == 'progress' for item in self.inflight.values())
            for identifier in due:
                if len(self.inflight) >= self.workers or progressing >= self.progress_limit:
                    break
                self._submit('progress', [identifier],
                             lambda identifier=identifier: self.controller.poll_progress(identifier))
                busy.add(identifier)
                progressing += 1
                self.last_device = identifier
            # Persist ordinary health polls, coalesced by the existing durable queue.
            full_due = [identifier for identifier in sorted(enabled) if self.full_due.get(identifier, 0) <= now]
            if full_due:
                self.store.enqueue_polls(full_due, int(time.time()) // int(self.poll_interval))
                self.full_due.update({identifier: now + self.poll_interval for identifier in full_due})
            background = sum(item[0] != 'progress' for item in self.inflight.values())
            while len(self.inflight) < self.workers and background < self.background_limit:
                # Prefer dispatch, but admit a background read after at most
                # four actions. Never put a full poll in front of live progress
                # on an updating device; its inventory/confirmation has its own lane.
                job = None
                if self.action_admissions >= 4:
                    job = self.store.claim_job(poll_only=True, exclude_devices=busy | active)
                if job is None:
                    job = self.store.claim_job(poll_only=False, exclude_devices=busy)
                if job is None:
                    job = self.store.claim_job(poll_only=True, exclude_devices=busy | active)
                if job is None:
                    break
                self.action_admissions = 0 if job['kind'] == 'poll' else self.action_admissions + 1
                identifiers = self.store.job_devices(job)
                self._submit(job['kind'], identifiers, lambda job=job: self.execute_job(job))
                busy.update(identifiers)
                background += 1

    def snapshot(self):
        with self.lock:
            now = self.now()
            active = self._active_devices()
            duration = max([item.get('duration_ms', 0) / 1000 for key, item in self.progress.items()
                            if key in active] or [self.progress_interval])
            rounds = max(1, (len(active) + self.progress_limit - 1) // self.progress_limit)
            expected = max(self.progress_interval, rounds * duration + self.progress_interval)
            busy = {identifier: kind for kind, identifiers, started in self.inflight.values()
                    for identifier in identifiers}
            devices = {key: {**self.progress.get(key, {}),
                'next_retry_s': max(0, round(self.progress.get(key, {}).get('due', now) - now, 1)),
                'queue_wait_s': max(0, round(now - self.progress.get(key, {}).get('due', self.waiting_since.get(key, now)), 1)),
                'waiting_for': busy.get(key, 'capacity' if self.progress.get(key, {}).get('due', 0) <= now else 'retry')}
                for key in active | set(self.progress)}
            return {'worker_limit': self.workers, 'background_limit': self.background_limit,
                'progress_limit': self.progress_limit,
                'active_workers': len(self.inflight),
                'progress_workers': sum(item[0] == 'progress' for item in self.inflight.values()),
                'inflight': [{'kind': kind, 'devices': sorted(identifiers),
                    'duration_ms': round((self.now() - started) * 1000)}
                    for kind, identifiers, started in self.inflight.values()],
                'active_devices': len(active), 'expected_refresh_s': round(expected, 1),
                'devices': devices}

    def run(self):
        while not self.stopped.is_set():
            try:
                self.tick()
            except Exception as exc:
                print('Device scheduler error: ' + str(exc)[:256], flush=True)
            self.stopped.wait(.2)

    def close(self):
        self.stopped.set()
        self.pool.shutdown(wait=True)
