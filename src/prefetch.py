"""Bounded, batch-scoped readers with isolated authentication and connections."""

import threading
from dataclasses import dataclass, field
from queue import Queue
from typing import Any, List, Optional, Union

from src.network import Network, ParsedTransferPage
from src.utils import parse_response, update_cookie


@dataclass
class PrefetchState:
    event: threading.Event
    batch_generation: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    network: Optional[Network] = None
    result: Union[List[Any], int, None] = None
    bdclnd: Optional[str] = None
    error: Optional[Exception] = None


class PrefetchPool:
    """Each daemon reader owns one Session; cancellation is never cleared."""

    def __init__(self, size, source, on_retry, network_factory=Network):
        self.size = size
        self.headers = source.headers.copy()
        self.headers['Cookie'] = '; '.join(
            part.strip() for part in self.headers.get('Cookie', '').split(';')
            if part.strip() and part.strip().partition('=')[0] != 'BDCLND'
        )
        self.bdstoken = source.bdstoken
        self.trust_env = source.s.trust_env
        self.metrics = source.metrics
        self.on_retry = on_retry
        self.network_factory = network_factory
        self.cancel_event = threading.Event()
        self.queue = Queue()
        self.threads = []
        self.networks = []
        self.lock = threading.Lock()
        self._closed = False

    def submit(self, url, password, state):
        with self.lock:
            if self.cancel_event.is_set():
                return False
            state.cancel_event = self.cancel_event
            self.queue.put((url, password, state))
            if len(self.threads) < self.size:
                thread = threading.Thread(target=self._read, daemon=True)
                self.threads.append(thread)
                thread.start()
        return True

    def cancel(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self.cancel_event.set()
            networks = list(self.networks)
            for _ in self.threads:
                self.queue.put(None)
        for network in networks:
            try:
                network.s.close()
            except Exception:
                pass

    def _read(self):
        network = None
        try:
            while True:
                task = self.queue.get()
                if task is None:
                    return
                url, password, state = task
                try:
                    Network._check_cancelled(self.cancel_event)
                    if network is None:
                        network = self.network_factory()
                        with self.lock:
                            self.networks.append(network)
                        network.s.trust_env = self.trust_env
                        network.bdstoken = self.bdstoken
                        network.metrics = self.metrics
                    state.network = network
                    network.headers = self.headers.copy()
                    network.s.cookies.clear()
                    Network._check_cancelled(self.cancel_event)
                    if password:
                        bdclnd = network.verify_pass_code(
                            url, password, on_retry=self._retry,
                            cancel_event=self.cancel_event,
                        )
                        if isinstance(bdclnd, int):
                            state.result = bdclnd
                            continue
                        if not isinstance(bdclnd, str) or not bdclnd:
                            state.result = -1
                            continue
                        network.headers['Cookie'] = update_cookie(
                            bdclnd, network.headers['Cookie'],
                        )
                        state.bdclnd = bdclnd
                    Network._check_cancelled(self.cancel_event)
                    response = network.get_transfer_params(
                        url, on_retry=self._retry, cancel_event=self.cancel_event,
                    )
                    Network._check_cancelled(self.cancel_event)
                    state.result = (
                        response.parsed if isinstance(response, ParsedTransferPage)
                        else parse_response(response)
                    )
                except Exception as error:
                    state.error = error
                finally:
                    state.event.set()
        finally:
            if network is not None:
                network.s.close()

    def _retry(self, attempt, reason):
        if not self.cancel_event.is_set():
            self.on_retry(attempt, reason)
