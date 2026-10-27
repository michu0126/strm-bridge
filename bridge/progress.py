"""Small, rate-limited progress records for long directory and execution jobs."""
import time


class ScanProgress:
    def __init__(self, callback):
        self.callback = callback
        self.started = time.monotonic()
        self.last = 0
        self.value = dict(phase='listing', directories=0, files=0, videos=0,
                          pending=0, cached_directories=0, current='')

    def update(self, force=False, **values):
        self.value.update(values)
        now = time.monotonic()
        if force or now - self.last >= 1:
            self.last = now
            self.callback(dict(self.value, elapsed=round(now - self.started, 1), updated=time.time()))


class ExecutionProgress:
    def __init__(self, state, job):
        self.state, self.job, self.last = state, job, 0

    def __call__(self, done, total, current):
        self.job.update(done=done, total=total, current=current)
        now = time.monotonic()
        if done == total or now - self.last >= 1:
            self.last = now
            self.state.put_job(self.job)
