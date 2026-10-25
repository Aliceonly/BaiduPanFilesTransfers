import threading
from unittest.mock import Mock

import pytest
import requests

from src.network import Network, RequestCancelledError, RetryExhaustedError, UNKNOWN_RESULT


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


@pytest.fixture
def directory_network(monkeypatch):
    network = Network()
    network.s.close()
    network.s = Mock(spec=requests.Session)
    monkeypatch.setattr('src.network.time.sleep', lambda _seconds: None)
    return network


def queue(network, *payloads):
    network.s.get.side_effect = [
        payload if isinstance(payload, Exception) else response(payload) for payload in payloads
    ]


def requested_pages(network):
    return [call.kwargs['params']['page'] for call in network.s.get.call_args_list]


@pytest.mark.parametrize('metadata', [
    {'total': 2000}, {'has_more': 1}, {'has_more': '1'}, {},
])
def test_probe_stops_on_first_valid_nonempty_page(directory_network, metadata):
    network = directory_network
    first = [{'fs_id': index} for index in range(1000)]
    queue(network, {'errno': 0, 'list': first, **metadata})

    assert network.probe_directory('/target') == first
    assert requested_pages(network) == ['1']
    kwargs = network.s.get.call_args.kwargs
    assert kwargs['verify'] is True
    assert kwargs['allow_redirects'] is False
    assert kwargs['params']['dir'] == '/target'


def test_complete_list_still_reads_all_pages(directory_network):
    network = directory_network
    first, second = [{'fs_id': 1}], [{'fs_id': 2}]
    queue(network, {'errno': 0, 'list': first, 'total': 2},
          {'errno': 0, 'list': second, 'total': 2})

    assert network.get_dir_list('/target') == first + second
    assert requested_pages(network) == ['1', '2']


@pytest.mark.parametrize('metadata', [
    {}, {'total': 0}, {'total': '0'}, {'has_more': 0}, {'has_more': '0'},
    {'has_more': False}, {'total': 0, 'has_more': 0}, {'total': '0', 'has_more': False},
])
def test_probe_returns_empty_only_for_a_valid_terminal_empty_page(directory_network, metadata):
    queue(directory_network, {'errno': 0, 'list': [], **metadata})

    assert directory_network.probe_directory('/target') == []
    assert requested_pages(directory_network) == ['1']


@pytest.mark.parametrize('metadata', [
    {'total': 1}, {'has_more': 1}, {'has_more': True}, {'has_more': '1'},
    {'total': '1', 'has_more': 0}, {'total': 0, 'has_more': 1},
])
def test_conflicting_empty_page_retries_same_page_then_accepts_nonempty(directory_network, metadata):
    network = directory_network
    valid = [{'fs_id': 10}]
    queue(network, {'errno': 0, 'list': [], **metadata},
          {'errno': 0, 'list': valid, 'has_more': 1})
    retries = []

    assert network.probe_directory('/target', on_retry=lambda *args: retries.append(args)) == valid
    assert requested_pages(network) == ['1', '1']
    assert retries == [(2, 'ValueError')]


@pytest.mark.parametrize('metadata', [
    {'total': 1}, {'has_more': 1}, {'has_more': True}, {'has_more': '1'},
    {'total': 1, 'has_more': 0}, {'total': 0, 'has_more': 1},
    {'total': 'invalid'}, {'has_more': 'invalid'}, {'total': -1},
    {'total': 0.5}, {'has_more': 2}, {'total': False},
])
def test_uncertain_empty_page_exhaustion_never_claims_empty(directory_network, metadata):
    network = directory_network
    queue(network, *[{'errno': 0, 'list': [], **metadata}] * 5)
    retries = []

    with pytest.raises(ValueError, match='pagination'):
        network.probe_directory('/target', on_retry=lambda *args: retries.append(args))

    assert requested_pages(network) == ['1'] * 5
    assert retries == [(attempt, 'ValueError') for attempt in range(2, 6)]


@pytest.mark.parametrize('invalid', [None, {}, 'invalid', True, {'fs_id': -1}, {'fs_id': 'invalid'}])
def test_probe_validates_entire_nonempty_page_before_short_circuit(directory_network, invalid):
    network = directory_network
    valid = [{'fs_id': 10}]
    queue(network, {'errno': 0, 'list': valid + [invalid], 'has_more': 1},
          {'errno': 0, 'list': valid, 'has_more': 1})

    assert network.probe_directory('/target') == valid
    assert requested_pages(network) == ['1', '1']


def test_probe_never_returns_a_malformed_nonempty_page(directory_network):
    network = directory_network
    queue(network, *[{'errno': 0, 'list': [{'fs_id': 10}, {}]}] * 5)

    with pytest.raises(ValueError, match='目录列表'):
        network.probe_directory('/target')

    assert requested_pages(network) == ['1'] * 5


@pytest.mark.parametrize('payload', [None, {}, {'errno': 0}, {'errno': 0, 'list': {}}, {'errno': False}])
def test_invalid_response_is_not_mistaken_for_an_empty_directory(directory_network, payload):
    network = directory_network
    queue(network, *[payload] * 5)

    with pytest.raises(ValueError):
        network.probe_directory('/target')

    assert network.s.get.call_count == 5


@pytest.mark.parametrize('errno', [-9, -6, 4])
def test_probe_preserves_permanent_api_error_contract(directory_network, errno):
    queue(directory_network, {'errno': errno})

    assert directory_network.probe_directory('/target') == errno
    assert directory_network.s.get.call_count == 1


@pytest.mark.parametrize('result', [UNKNOWN_RESULT, -9])
def test_probe_preserves_internal_error_sentinels(directory_network, monkeypatch, result):
    monkeypatch.setattr(directory_network, '_request_json', lambda *_args, **_kwargs: result)

    assert directory_network.probe_directory('/target') == result
    directory_network.s.get.assert_not_called()


def test_probe_temporary_api_error_uses_existing_retry_budget(directory_network):
    queue(directory_network, *[{'errno': 500}] * 5)

    with pytest.raises(RetryExhaustedError):
        directory_network.probe_directory('/target')

    assert directory_network.s.get.call_count == 5


def test_probe_transport_failure_raises_instead_of_returning_empty(directory_network):
    queue(directory_network, *[requests.Timeout('offline')] * 5)

    with pytest.raises(requests.Timeout):
        directory_network.probe_directory('/target')

    assert directory_network.s.get.call_count == 5


def test_probe_cancelled_before_request_does_not_send_http(directory_network):
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(RequestCancelledError):
        directory_network.probe_directory('/target', cancel_event=cancelled)

    directory_network.s.get.assert_not_called()


def test_probe_retry_can_be_cancelled_without_advancing_page(directory_network):
    network = directory_network
    queue(network, {'errno': 0, 'list': [], 'has_more': 1})
    cancelled = threading.Event()

    with pytest.raises(RequestCancelledError):
        network.probe_directory(
            '/target', cancel_event=cancelled, on_retry=lambda *_args: cancelled.set(),
        )

    assert requested_pages(network) == ['1']


def test_probe_cancelled_in_flight_does_not_return_partial_data(directory_network):
    network = directory_network
    cancelled = threading.Event()

    def send(**_kwargs):
        cancelled.set()
        return response({'errno': 0, 'list': [{'fs_id': 1}], 'has_more': 1})

    network.s.get.side_effect = send
    with pytest.raises(RequestCancelledError):
        network.probe_directory('/target', cancel_event=cancelled)

    assert requested_pages(network) == ['1']
