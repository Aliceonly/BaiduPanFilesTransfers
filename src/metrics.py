"""Thread-safe, credential-free counters for a batch of network requests."""

import threading


class RequestMetrics:
    """Share across Network instances; snapshots never expose mutable state."""

    STAGES = frozenset({'token', 'verify', 'page', 'list', 'create', 'transfer', 'share'})

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = 0
        self._retry_wait_seconds = 0.0
        self._stages = {}

    def record_request(self, stage: str, seconds: float) -> None:
        """Record one HTTP attempt, including failed attempts, without request data."""
        if stage not in self.STAGES:
            raise ValueError('Unknown request stage')
        with self._lock:
            self._requests += 1
            counters = self._stages.setdefault(stage, {'requests': 0, 'seconds': 0.0})
            counters['requests'] += 1
            counters['seconds'] += seconds

    def record_retry_wait(self, seconds: float) -> None:
        """Record elapsed backoff time, not the requested delay before cancellation."""
        with self._lock:
            self._retry_wait_seconds += seconds

    def snapshot(self) -> dict:
        """Return an atomic, detached snapshot of completed attempts and waits."""
        with self._lock:
            return {
                'requests': self._requests,
                'retry_wait_seconds': self._retry_wait_seconds,
                'stages': {stage: counters.copy() for stage, counters in self._stages.items()},
            }
