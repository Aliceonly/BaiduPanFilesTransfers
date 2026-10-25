import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, NonCallableMock

import pytest
import requests

from src.metrics import RequestMetrics
from src.network import Network, RequestCancelledError, UNKNOWN_RESULT


SHARE_PAGE = b'{"shareid":123,"share_uk":456,"fs_id":789,"isdir":0,"server_filename":"file"}'
SHARE_URL = 'https://pan.baidu.com/s/1first'
TRANSFER_PARAMS = ['123', '456', ['789'], ['file'], ['0']]
ENDPOINTS = [
    ('get_bdstoken', (), 'get', 'token', {'errno': 0, 'result': {'bdstoken': 'token'}}),
    ('verify_pass_code', (SHARE_URL, 'abcd'), 'post', 'verify', {'errno': 0, 'randsk': 'key'}),
    ('get_transfer_params', (SHARE_URL,), 'get', 'page', None),
    ('get_dir_list', ('/target',), 'get', 'list', {'errno': 0, 'list': []}),
    ('probe_directory', ('/target',), 'get', 'list', {'errno': 0, 'list': []}),
    ('create_dir', ('target',), 'post', 'create', {'errno': 0}),
    ('transfer_file', (TRANSFER_PARAMS, 'target'), 'post', 'transfer', {'errno': 0}),
    ('create_share', (789, '0', 'abcd'), 'post', 'share', {'errno': 0, 'link': SHARE_URL}),
]


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


def response(payload=None, content=SHARE_PAGE, status_code=200, location=None):
    result = NonCallableMock(status_code=status_code, content=content, headers={'Location': location})
    result.json.return_value = payload
    return result


@pytest.fixture
def instrumented_network(monkeypatch):
    network = Network()
    assert network.metrics is None
    network.s.close()
    network.s = Mock(spec=requests.Session)
    network.metrics = RequestMetrics()
    clock = Clock()
    monkeypatch.setattr('src.network.time.perf_counter', clock)
    monkeypatch.setattr('src.network.random.uniform', lambda *_args: 1.5)
    monkeypatch.setattr('src.network.time.sleep', clock.advance)
    return network, clock


def timed_responses(network, clock, method, *outcomes):
    responses = iter(outcomes)

    def send(**_kwargs):
        clock.advance(0.25)
        outcome = next(responses)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome() if callable(outcome) else outcome

    getattr(network.s, method).side_effect = send


def assert_metrics(network, stage, count=1, seconds=0.25, wait=0.0):
    assert network.metrics.snapshot() == {
        'requests': count,
        'retry_wait_seconds': wait,
        'stages': {stage: {'requests': count, 'seconds': seconds}},
    }


@pytest.mark.parametrize('method,args,http_method,stage,payload', ENDPOINTS)
def test_every_endpoint_counts_only_session_time(
    instrumented_network, method, args, http_method, stage, payload,
):
    network, clock = instrumented_network
    result = response(payload)

    def parse_json():
        clock.advance(10)
        return payload

    result.json.side_effect = parse_json
    result.raise_for_status.side_effect = lambda: clock.advance(5)
    timed_responses(network, clock, http_method, result)

    getattr(network, method)(*args)

    assert_metrics(network, stage)
    kwargs = getattr(network.s, http_method).call_args.kwargs
    assert kwargs['verify'] is True
    assert kwargs['allow_redirects'] is False


def test_compatibility_create_hook_is_counted(instrumented_network):
    network, clock = instrumented_network
    timed_responses(network, clock, 'post', response({'errno': 0, 'path': '/target'}))

    assert Network.create_dir.__wrapped__(network, 'target') == 0
    assert_metrics(network, 'create')


def test_metrics_are_optional_and_do_not_read_the_clock(monkeypatch):
    network = Network()
    network.s.close()
    network.s = Mock(get=Mock(return_value=response({'errno': 0, 'list': []})))
    monkeypatch.setattr('src.network.time.perf_counter', lambda: pytest.fail('metrics disabled'))

    assert network.get_dir_list('/target') == []
    assert network.metrics is None


@pytest.mark.parametrize('failure', [
    requests.Timeout('private request details'),
    requests.ConnectionError('private request details'),
    requests.exceptions.SSLError('private request details'),
    response({'errno': 500}),
    response({'errno': 0, 'result': {}}),
    response(None),
])
def test_retries_count_failures_and_keep_backoff_out_of_http_time(instrumented_network, failure):
    network, clock = instrumented_network
    timed_responses(
        network, clock, 'get', failure,
        response({'errno': 0, 'result': {'bdstoken': 'token'}}),
    )
    retries = []

    def retry(attempt, reason):
        retries.append((attempt, reason))
        clock.advance(3)

    assert network.get_bdstoken(on_retry=retry) == 'token'
    assert len(retries) == 1
    assert retries[0][0] == 2
    assert_metrics(network, 'token', count=2, seconds=0.5, wait=1.5)


def test_http_status_failure_counts_as_an_attempt(instrumented_network):
    network, clock = instrumented_network
    bad = response({'errno': 0, 'list': []})
    bad.raise_for_status.side_effect = requests.HTTPError('503')
    timed_responses(network, clock, 'get', bad, response({'errno': 0, 'list': []}))

    assert network.get_dir_list('/target') == []
    assert_metrics(network, 'list', count=2, seconds=0.5, wait=1.5)


def test_pagination_counts_each_page_and_its_validation_retries(instrumented_network):
    network, clock = instrumented_network
    timed_responses(
        network, clock, 'get',
        response({'errno': 0, 'list': [{'fs_id': 1}], 'total': 2}),
        response({'errno': 0, 'list': [], 'total': 2}),
        response({'errno': 0, 'list': [{'fs_id': 2}], 'total': 2}),
    )

    assert network.get_dir_list('/target') == [{'fs_id': 1}, {'fs_id': 2}]
    assert_metrics(network, 'list', count=3, seconds=0.75, wait=1.5)
    assert [call.kwargs['params']['page'] for call in network.s.get.call_args_list] == ['1', '2', '2']


@pytest.mark.parametrize('write', [False, True])
def test_exhausted_transport_failures_are_all_counted(instrumented_network, write):
    network, clock = instrumented_network
    timed_responses(network, clock, 'post' if write else 'get', *[
        requests.Timeout('private request details') for _ in range(5)
    ])

    if write:
        assert network.transfer_file(TRANSFER_PARAMS, 'target') == UNKNOWN_RESULT
    else:
        with pytest.raises(requests.Timeout):
            network.get_bdstoken()

    assert_metrics(network, 'transfer' if write else 'token', count=5, seconds=1.25, wait=6.0)


def test_permanent_api_error_is_counted_without_backoff(instrumented_network):
    network, clock = instrumented_network
    timed_responses(network, clock, 'get', response({'errno': -6}))

    assert network.get_bdstoken() == -6
    assert_metrics(network, 'token')


def test_each_trusted_redirect_and_page_retry_is_counted(instrumented_network):
    network, clock = instrumented_network
    timed_responses(
        network, clock, 'get',
        response(status_code=302, location='/s/1next'), requests.Timeout(),
        response(status_code=307, location='/s/1next'), response(),
    )

    assert network.get_transfer_params(SHARE_URL).parsed == TRANSFER_PARAMS
    assert_metrics(network, 'page', count=4, seconds=1.0, wait=1.5)


def test_redirect_exhaustion_counts_all_sent_requests(instrumented_network):
    network, clock = instrumented_network
    redirect = response(status_code=302, location='/s/1first')
    timed_responses(network, clock, 'get', *[redirect] * 30)

    with pytest.raises(requests.TooManyRedirects):
        network.get_transfer_params(SHARE_URL)

    assert_metrics(network, 'page', count=30, seconds=7.5, wait=6.0)


def test_blocked_redirect_counts_only_the_trusted_request(instrumented_network):
    network, clock = instrumented_network
    timed_responses(
        network, clock, 'get', response(status_code=302, location='https://example.invalid/'),
    )

    assert network.get_transfer_params(SHARE_URL).parsed == -1
    assert_metrics(network, 'page')


@pytest.mark.parametrize('method,args,http_method,stage,payload', ENDPOINTS)
def test_cancellation_before_http_does_not_increment_metrics(
    instrumented_network, method, args, http_method, stage, payload,
):
    network, _clock = instrumented_network
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(RequestCancelledError):
        getattr(network, method)(*args, cancel_event=cancelled)

    assert network.metrics.snapshot() == {'requests': 0, 'retry_wait_seconds': 0.0, 'stages': {}}
    getattr(network.s, http_method).assert_not_called()


@pytest.mark.parametrize('method,args,http_method,stage,payload', ENDPOINTS)
def test_cancellation_in_flight_preserves_sent_request_metrics(
    instrumented_network, method, args, http_method, stage, payload,
):
    network, clock = instrumented_network
    cancelled = threading.Event()

    def finish():
        cancelled.set()
        return response(payload)

    timed_responses(network, clock, http_method, finish)
    if method in ('create_dir', 'transfer_file', 'create_share'):
        assert getattr(network, method)(*args, cancel_event=cancelled) in (0, SHARE_URL)
    else:
        with pytest.raises(RequestCancelledError):
            getattr(network, method)(*args, cancel_event=cancelled)

    assert_metrics(network, stage)


@pytest.mark.parametrize('write', [False, True])
def test_interrupted_retry_wait_records_only_elapsed_time(instrumented_network, write):
    network, clock = instrumented_network

    class CancellationEvent(threading.Event):
        def wait(self, timeout=None):
            assert timeout == 1.5
            clock.advance(0.125)
            self.set()
            return True

    cancelled = CancellationEvent()
    timed_responses(network, clock, 'post' if write else 'get', requests.Timeout())

    if write:
        assert network.transfer_file(TRANSFER_PARAMS, 'target', cancel_event=cancelled) == UNKNOWN_RESULT
    else:
        with pytest.raises(RequestCancelledError):
            network.get_bdstoken(cancel_event=cancelled)

    assert_metrics(network, 'transfer' if write else 'token', wait=0.125)


def test_uncancelled_event_wait_is_recorded_separately(instrumented_network):
    network, clock = instrumented_network

    class ContinuingEvent(threading.Event):
        def wait(self, timeout=None):
            clock.advance(timeout)
            return False

    timed_responses(network, clock, 'get', response({'errno': 500}), response({'errno': -6}))

    assert network.get_bdstoken(cancel_event=ContinuingEvent()) == -6
    assert_metrics(network, 'token', count=2, seconds=0.5, wait=1.5)


@pytest.mark.parametrize('write', [False, True])
def test_failure_and_cancellation_in_the_same_attempt_are_still_counted(instrumented_network, write):
    network, clock = instrumented_network
    cancelled = threading.Event()

    def fail():
        cancelled.set()
        raise requests.Timeout()

    timed_responses(network, clock, 'post' if write else 'get', fail)
    if write:
        assert network.transfer_file(TRANSFER_PARAMS, 'target', cancel_event=cancelled) == UNKNOWN_RESULT
    else:
        with pytest.raises(RequestCancelledError):
            network.get_bdstoken(cancel_event=cancelled)

    assert_metrics(network, 'transfer' if write else 'token')


def test_cancellation_during_redirect_counts_only_first_request(instrumented_network):
    network, clock = instrumented_network
    cancelled = threading.Event()
    redirect = response(status_code=302, location='/s/1next')
    redirect.close.side_effect = cancelled.set
    timed_responses(network, clock, 'get', redirect)

    with pytest.raises(RequestCancelledError):
        network.get_transfer_params(SHARE_URL, cancel_event=cancelled)

    assert_metrics(network, 'page')


def test_invalid_input_does_not_count_as_http(instrumented_network):
    network, _clock = instrumented_network

    assert network.get_transfer_params('https://example.invalid/').parsed == -1
    assert network.transfer_file([], 'target') == -1
    assert network.metrics.snapshot()['requests'] == 0
    network.s.get.assert_not_called()
    network.s.post.assert_not_called()


def test_snapshot_is_a_detached_copy_and_stages_accumulate():
    metrics = RequestMetrics()
    assert metrics.snapshot() == {'requests': 0, 'retry_wait_seconds': 0.0, 'stages': {}}
    metrics.record_request('token', 0.25)
    metrics.record_request('list', 0.5)
    metrics.record_retry_wait(1.5)
    snapshot = metrics.snapshot()
    snapshot['stages']['token']['seconds'] = 100
    snapshot['stages']['list']['requests'] = 100
    snapshot['requests'] = 100
    snapshot['stages']['extra'] = {}

    assert metrics.snapshot() == {
        'requests': 2, 'retry_wait_seconds': 1.5,
        'stages': {'token': {'requests': 1, 'seconds': 0.25},
                   'list': {'requests': 1, 'seconds': 0.5}},
    }


def test_concurrent_stage_durations_and_wait_totals_are_not_lost():
    metrics = RequestMetrics()
    stages = sorted(RequestMetrics.STAGES)
    barrier = threading.Barrier(len(stages))

    def record(stage):
        barrier.wait(timeout=5)
        for _ in range(1000):
            metrics.record_request(stage, 0.125)
            metrics.record_retry_wait(0.25)
            snapshot = metrics.snapshot()
            assert snapshot['requests'] == sum(item['requests'] for item in snapshot['stages'].values())

    with ThreadPoolExecutor(max_workers=len(stages)) as pool:
        list(pool.map(record, stages))

    assert metrics.snapshot() == {
        'requests': 7000,
        'retry_wait_seconds': 1750.0,
        'stages': {stage: {'requests': 1000, 'seconds': 125.0} for stage in stages},
    }


def test_shared_metrics_remain_atomic_under_concurrent_network_calls():
    metrics = RequestMetrics()
    workers = 8
    barrier = threading.Barrier(workers)

    def run_worker(index):
        network = Network()
        network.s.close()
        network.metrics = metrics
        network.s = Mock(get=Mock(return_value=response({'errno': 0, 'list': []})))
        barrier.wait(timeout=5)
        for _ in range(200):
            assert network.get_dir_list('/target') == []
            metrics.record_retry_wait(0.125)
            snapshot = metrics.snapshot()
            assert snapshot['requests'] == sum(stage['requests'] for stage in snapshot['stages'].values())
        return index

    with ThreadPoolExecutor(max_workers=workers) as pool:
        assert list(pool.map(run_worker, range(workers))) == list(range(workers))

    snapshot = metrics.snapshot()
    assert snapshot['requests'] == snapshot['stages']['list']['requests'] == 1600
    assert snapshot['stages']['list']['seconds'] >= 0.0
    assert snapshot['retry_wait_seconds'] == 200.0


def test_reset_session_keeps_shared_batch_metrics(instrumented_network, monkeypatch):
    network, clock = instrumented_network
    shared = network.metrics
    timed_responses(network, clock, 'get', response({'errno': 0, 'list': []}))
    assert network.get_dir_list('/target') == []
    old_session = network.s
    replacement = Mock(spec=requests.Session)
    monkeypatch.setattr('src.network.requests.Session', lambda: replacement)

    network.reset_session()

    old_session.close.assert_called_once()
    assert network.s is replacement
    assert network.metrics is shared
    assert_metrics(network, 'list')


def test_statistics_never_store_or_output_request_secrets(instrumented_network, capsys, caplog):
    network, clock = instrumented_network
    network.headers['Cookie'] = 'COOKIE_SECRET'
    network.bdstoken = 'TOKEN_SECRET'
    timed_responses(network, clock, 'post', response({'errno': 0, 'randsk': 'RANSK_SECRET'}))

    result = network.verify_pass_code('https://pan.baidu.com/s/1LINK_SECRET', 'PASSWORD_SECRET')
    assert result == 'RANSK_SECRET'

    observed = repr(network.metrics.snapshot()) + repr(vars(network.metrics)) + caplog.text
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''
    assert all(secret not in observed for secret in (
        'COOKIE_SECRET', 'TOKEN_SECRET', 'LINK_SECRET', 'PASSWORD_SECRET', 'RANSK_SECRET',
    ))
    with pytest.raises(ValueError, match='^Unknown request stage$'):
        network.metrics.record_request('COOKIE_SECRET', 1.0)
