import json
import threading
from collections import Counter
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from src.network import Network
from src.operations import Operations, PrefetchState
from src.prefetch import PrefetchPool
from test_operations import VALID_RESULT, make_root


def mock_batch(monkeypatch, passwords, custom=True):
    urls = [f'https://pan.baidu.com/s/1item{i:04d}' for i in range(len(passwords))]
    root = make_root('\n'.join(
        url + (' abcd' if password else '')
        for url, password in zip(urls, passwords)
    ))
    root.var_custom_mode.value = custom
    operation = Operations(root)
    calls, transfers, sessions = Counter(), [], set()
    lock = threading.Lock()

    def request(session, method, url, **kwargs):
        endpoint = urlparse(url).path
        with lock:
            calls['page' if endpoint.startswith('/s/') else endpoint] += 1
            sessions.add(session)
        params = kwargs.get('params', {})
        data = kwargs.get('data', {})
        payload = {'errno': 0}
        if endpoint == '/api/gettemplatevariable':
            payload['result'] = {'bdstoken': 'token'}
        elif endpoint == '/api/list':
            payload.update(list=[], total=0)
        elif endpoint == '/api/create':
            payload['path'] = data['path']
        elif endpoint == '/share/verify':
            payload['randsk'] = 'auth-' + params['surl']
        elif endpoint.startswith('/s/'):
            index = urls.index(url)
            if passwords[index]:
                assert f'BDCLND=auth-item{index:04d}' in kwargs['headers']['Cookie']
            # Emulate a per-share cookie; the next task must clear it.
            if threading.current_thread() is not operation._ui_thread:
                assert not session.cookies.get('previous-share')
                session.cookies.set('previous-share', str(index))
            payload = {
                'shareid': 2000 + index, 'share_uk': 3000,
                'file_list': [{
                    'fs_id': 1000 + index, 'server_filename': f'file-{index}',
                    'isdir': 0, 'size': 100, 'md5': 'a' * 32,
                }],
            }
        elif endpoint == '/share/transfer':
            index = int(params['shareid']) - 2000
            assert threading.current_thread() is operation._ui_thread
            if passwords[index]:
                assert f'BDCLND=auth-item{index:04d}' in kwargs['headers']['Cookie']
            transfers.append((index, data['path']))
        else:
            pytest.fail(f'Unexpected endpoint: {endpoint}')
        return SimpleNamespace(
            json=lambda: payload, content=json.dumps(payload).encode(),
            status_code=200, raise_for_status=lambda: None,
        )

    monkeypatch.setattr('requests.Session.request', request)
    return operation, calls, transfers, sessions


@pytest.mark.parametrize('passwords', [[True] * 40, [False] * 40, [True, False] * 20])
def test_preparation_has_no_duplicate_requests_and_reuses_two_reader_sessions(monkeypatch, passwords):
    operation, calls, transfers, sessions = mock_batch(monkeypatch, passwords)

    operation.save()

    assert operation.completed_task_count == 40
    assert not operation.resume_available
    assert calls['page'] == 40
    assert calls['/share/verify'] == sum(passwords)
    assert sum(calls.values()) == 162 + sum(passwords)
    assert len(sessions) <= 3  # Two persistent readers plus the serial writer.
    assert transfers == [(i, f'/target/{i + 1}') for i in range(40)]
    assert operation.metrics.snapshot()['requests'] == sum(calls.values())
    assert '本次运行统计' in operation.root.text_logs.value


def test_password_auth_is_bound_to_the_cached_share_not_the_reader(monkeypatch):
    operation, calls, transfers, _sessions = mock_batch(monkeypatch, [True, True])
    operation.root.text_links.value += '\nhttps://pan.baidu.com/s/1item0000 abcd'

    operation.save()

    assert operation.completed_task_count == 3
    assert calls['page'] == calls['/share/verify'] == 2
    assert transfers == [(0, '/target/1'), (1, '/target/2'), (0, '/target/3')]


def test_stale_prefetch_is_discarded_without_waiting():
    operation = Operations(make_root(''))
    state = PrefetchState(threading.Event(), operation.batch_generation - 1)
    operation.prefetch_states[('https://pan.baidu.com/s/1old', '')] = state
    operation.verify_link = lambda *_args: VALID_RESULT

    assert operation.get_cached_transfer_params('https://pan.baidu.com/s/1old') == VALID_RESULT
    assert state.cancel_event.is_set()
    assert not state.event.is_set()


def test_pool_shutdown_wakes_idle_readers_after_cancellation_was_already_signalled(monkeypatch):
    operation = Operations(make_root(''))
    pool = PrefetchPool(1, operation.network, lambda *_args: None)
    state = PrefetchState(threading.Event(), 0)
    monkeypatch.setattr(Network, 'get_transfer_params', lambda *_args, **_kwargs: '')
    pool.submit('https://pan.baidu.com/s/1test', '', state)
    try:
        assert state.event.wait(2)
        pool.cancel_event.set()
    finally:
        pool.cancel()
        for thread in pool.threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


def test_prefetch_bounds_work_and_overlaps_authentication_without_changing_main_cookie(monkeypatch):
    operation = Operations(make_root(''))
    operation.network.headers['Cookie'] = 'BAIDUID=account;BDCLND=main'
    operation.network.bdstoken = 'token'
    operation.link_list = [f'https://pan.baidu.com/s/1item{i:04d} abcd' for i in range(20)]
    both_reading = threading.Event()
    release = threading.Event()
    counts = Counter()
    lock = threading.Lock()

    def verify(network, _url, _password, **kwargs):
        with lock:
            counts['active'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
            if counts['active'] == 2:
                both_reading.set()
        try:
            assert release.wait(2)
            Network._check_cancelled(kwargs['cancel_event'])
            return 'reader-only'
        finally:
            with lock:
                counts['active'] -= 1

    monkeypatch.setattr(Network, 'verify_pass_code', verify)
    operation.schedule_prefetch(0)
    pool = operation._prefetch_pool
    try:
        assert both_reading.wait(2)
        assert len(operation.prefetch_states) == 4
        operation.schedule_prefetch(0)
        assert len(operation.prefetch_states) == 4
        assert counts['peak'] == 2
        assert operation.network.headers['Cookie'] == 'BAIDUID=account;BDCLND=main'
    finally:
        operation.request_cancel()
        release.set()
        for thread in pool.threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
    assert all(state.event.is_set() for state in operation.prefetch_states.values())


def test_reset_cannot_revive_an_old_reader_or_mix_its_metrics_into_new_run(monkeypatch):
    operation = Operations(make_root(''))
    operation.network.bdstoken = 'token'
    operation.link_list = ['https://pan.baidu.com/s/1old']
    entered, release = threading.Event(), threading.Event()
    requests = []

    def request(_session, _method, _url=None, **_kwargs):
        requests.append(True)
        entered.set()
        assert release.wait(2)
        raise TimeoutError('old read completed after reset')

    monkeypatch.setattr('requests.Session.request', request)
    operation.schedule_prefetch(0)
    pool = operation._prefetch_pool
    old_metrics = operation.metrics
    try:
        assert entered.wait(2)
        operation.reset_progress()
        assert not operation.cancel_event.is_set()
        assert pool.cancel_event.is_set()
    finally:
        release.set()
        for thread in pool.threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
    assert requests == [True]
    assert old_metrics.snapshot()['requests'] == 1
    assert operation.metrics.snapshot()['requests'] == 0
    assert operation.transfer_params_cache == operation.prefetch_states == {}


def test_custom_base_probe_never_becomes_a_partial_baseline(monkeypatch):
    operation, _calls, _transfers, _sessions = mock_batch(monkeypatch, [True])
    operation.prepare_run()
    operation.network.probe_directory = lambda *_args: [{'fs_id': 1}]

    operation.handle_create_dir('target')

    assert 'target' not in operation.destination_baselines


def test_performance_log_identifies_completed_requests_instead_of_claiming_a_final_total():
    operation = Operations(make_root(''))
    operation.prepare_run()
    operation.metrics.record_request('page', 0.01)
    operation.prefetch_states[('pending', '')] = PrefetchState(
        threading.Event(), operation.batch_generation,
    )

    operation._log_performance()

    assert '截至此刻' in operation.root.text_logs.value
    assert 'HTTP 已结束请求 1 次' in operation.root.text_logs.value


def test_normal_mode_keeps_its_complete_fresh_baseline(monkeypatch):
    operation, calls, transfers, _sessions = mock_batch(monkeypatch, [True] * 3, custom=False)

    operation.save()

    assert calls['/api/list'] == 4  # Initial complete list + one fresh list per write.
    assert transfers == [(i, '/target') for i in range(3)]


def test_failed_prefetched_password_does_not_cache_or_transfer(monkeypatch):
    operation, _calls, transfers, _sessions = mock_batch(monkeypatch, [True] * 3)
    original = Network.verify_pass_code

    def verify(network, url, password, **kwargs):
        if url.endswith('0001'):
            return -9
        return original(network, url, password, **kwargs)

    monkeypatch.setattr(Network, 'verify_pass_code', verify)
    operation.save()

    assert [index for index, _ in transfers] == [0, 2]
    assert ('https://pan.baidu.com/s/1item0001', 'abcd') not in operation.transfer_params_cache
    assert operation.completed_task_count == 3
