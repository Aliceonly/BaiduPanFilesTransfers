import json
import threading

import pytest
import requests

from src.network import Network, RequestCancelledError, UNKNOWN_RESULT
from src.utils import parse_response, parse_url_and_code


class FakeResponse:
    def __init__(self, errno, payload=None, content=b""):
        self.errno = errno
        self.payload = {"errno": errno} if payload is None else payload
        self.content = content

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.requests = []

    def post(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_transfer_file_retries_nonzero_errno(monkeypatch):
    network = Network()
    network.s = FakeSession([FakeResponse(500), FakeResponse(0)])
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = network.transfer_file(
        ["share", "user", ["123"], ["file"], ["0"]],
        "target",
    )

    assert result == 0
    assert network.s.calls == 2


def test_parse_response_rejects_mismatched_file_fields():
    response = (
        '"shareid":1,"share_uk":"2",'
        '"fs_id":3,"isdir":0,"server_filename":"one","'
        '"fs_id":4,"'
    )

    assert parse_response(response) == -1


def test_transfer_exhaustion_raises_a_retryable_error(monkeypatch):
    network = Network()
    network.s = FakeSession([FakeResponse(500) for _ in range(5)])
    sleeps = []
    attempts = []
    monkeypatch.setattr("src.network.time.sleep", sleeps.append)

    with pytest.raises(requests.RequestException, match="500"):
        network.transfer_file(
            ["123", "456", ["789"], ["file"], ["0"]], "target",
            on_retry=lambda attempt, reason: attempts.append((attempt, reason)),
        )

    assert network.s.calls == 5
    assert len(sleeps) == 4
    assert attempts == [(2, 500), (3, 500), (4, 500), (5, 500)]
    assert all(1 <= delay <= 2 for delay in sleeps)


@pytest.mark.parametrize("method", ["get_bdstoken", "get_dir_list", "verify_pass_code", "create_dir"])
def test_temporary_api_errno_exhaustion_raises_instead_of_becoming_permanent(monkeypatch, method):
    network = Network()
    network.s = FakeSession([FakeResponse(500)] * 5)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)
    arguments = {
        "get_bdstoken": (), "get_dir_list": ("/target",),
        "verify_pass_code": ("https://pan.baidu.com/s/1first", "1234"),
        "create_dir": ("target/1",),
    }

    with pytest.raises(requests.RequestException, match="500"):
        getattr(network, method)(*arguments[method])

    assert network.s.calls == 5


def test_transfer_timeout_retries_without_changing_destination(monkeypatch):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout"), FakeResponse(0)])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    result = network.transfer_file(["123", "456", ["789"], ["file"], ["0"]], "target/1")

    assert result == 0
    assert network.s.calls == 2
    assert all(request["data"]["path"] == "/target/1" for request in network.s.requests)


def test_transfer_timeout_then_collision_keeps_result_unknown(monkeypatch):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout"), FakeResponse(4)])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)
    retries = []

    result = network.transfer_file(
        ["123", "456", ["789"], ["file"], ["0"]],
        "target/1",
        on_retry=lambda attempt, reason: retries.append((attempt, reason)),
    )

    assert result == UNKNOWN_RESULT
    assert network.s.calls == 2
    assert retries == [(2, "Timeout")]


@pytest.mark.parametrize("errno", [-6, 20, 500])
def test_failure_after_timeout_cannot_discard_an_unknown_outcome(monkeypatch, errno):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout")] + [FakeResponse(errno)] * 4)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    result = network.transfer_file(
        ["123", "456", ["789"], ["file"], ["0"]], "target/1",
        on_retry=lambda *_args: None,
    )

    assert result == UNKNOWN_RESULT
    assert network.s.calls == (5 if errno == 500 else 2)


@pytest.mark.parametrize("method, arguments", [
    ("create_dir", ("target/1",)),
    ("create_share", (789, 0, "1234")),
])
@pytest.mark.parametrize("errno", [-8, 500])
def test_side_effecting_json_api_retains_unknown_outcome_after_timeout(
    monkeypatch, method, arguments, errno,
):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout")] + [FakeResponse(errno)] * 4)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    assert getattr(network, method)(*arguments) == UNKNOWN_RESULT
    assert network.s.calls == (5 if errno == 500 else 2)


def test_exhausted_timeouts_return_unknown_even_without_a_callback(monkeypatch):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout") for _ in range(5)])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    assert network.transfer_file(
        ["123", "456", ["789"], ["file"], ["0"]], "target",
    ) == UNKNOWN_RESULT

    assert network.s.calls == 5


def test_malformed_json_is_retried(monkeypatch):
    network = Network()
    network.s = FakeSession([FakeResponse(None), FakeResponse(0)])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    assert network.transfer_file(["123", "456", ["789"], ["file"], ["0"]], "target") == 0
    assert network.s.calls == 2


@pytest.mark.parametrize("errno", [-6, -7, -8, -62, 4, 12, 20])
def test_permanent_error_is_returned_without_wasting_retries(errno):
    network = Network()
    network.s = FakeSession([FakeResponse(errno)])

    assert network.transfer_file(["123", "456", ["789"], ["file"], ["0"]], "target") == errno
    assert network.s.calls == 1


@pytest.mark.parametrize("invalid", [None, [], ["123"], ["123", "456", [None], ["file"], ["0"]]])
def test_invalid_parameters_are_rejected_without_http_requests(invalid):
    network = Network()
    network.s = FakeSession([])

    assert network.transfer_file(invalid, "target") == -1
    assert network.s.calls == 0


def test_duplicate_file_ids_are_sent_only_once():
    network = Network()
    network.s = FakeSession([FakeResponse(0)])

    result = network.transfer_file(
        ["123", "456", ["789", "789"], ["file", "file"], ["0", "0"]], "target",
    )

    assert result == 0
    assert network.s.requests[0]["data"]["fsidlist"] == "[789]"


def test_create_directory_disables_implicit_rename():
    network = Network()
    network.s = FakeSession([FakeResponse(0, {"errno": 0, "path": "/target/1"})])

    assert network.create_dir("target/1") == 0
    assert network.s.requests[0]["data"]["rtype"] == "0"
    assert network.s.requests[0]["data"]["path"] == "/target/1"


def test_create_directory_rejects_an_unexpected_destination():
    network = Network()
    network.s = FakeSession([FakeResponse(0, {"errno": 0, "path": "/target/1_renamed"})])

    with pytest.raises(ValueError, match="1_renamed"):
        Network.create_dir.__wrapped__(network, "target/1")


def test_get_bdstoken_retries_invalid_response_five_times(monkeypatch):
    network = Network()
    network.s = FakeSession([FakeResponse(None)] * 4 + [
        FakeResponse(0, {"errno": 0, "result": {"bdstoken": "token"}})
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)
    retries = []

    assert network.get_bdstoken(
        on_retry=lambda attempt, reason: retries.append((attempt, reason))
    ) == "token"
    assert network.s.calls == 5
    assert [attempt for attempt, _reason in retries] == [2, 3, 4, 5]


SHARE_PAGE = b'{"shareid":123,"share_uk":456,"fs_id":789,"isdir":0,"server_filename":"file"}'


@pytest.mark.parametrize("invalid_page", [
    b"<html>upstream busy</html>",
    b'<script>var errorMessage="' + "分享已取消".encode() + b'";</script><p>Busy</p>',
])
def test_invalid_nonempty_share_pages_are_retried(monkeypatch, invalid_page):
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, content=invalid_page),
        FakeResponse(0, content=SHARE_PAGE),
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)
    attempts = []

    page = network.get_transfer_params(
        "https://pan.baidu.com/s/1first",
        on_retry=lambda attempt, _reason: attempts.append(attempt),
    )

    assert page == SHARE_PAGE.decode()
    assert network.s.calls == 2
    assert attempts == [2]


def test_invalid_share_page_exhaustion_is_not_returned_as_a_dead_link(monkeypatch):
    network = Network()
    network.s = FakeSession([FakeResponse(0, content=b"<p>Busy</p>")] * 5)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(ValueError):
        network.get_transfer_params("https://pan.baidu.com/s/1first")

    assert network.s.calls == 5


def test_cancelled_share_page_does_not_send_a_request():
    network = Network()
    network.s = FakeSession([])
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(RequestCancelledError):
        network.get_transfer_params("https://pan.baidu.com/s/1first", cancel_event=cancelled)

    assert network.s.calls == 0


def test_share_page_retry_wait_is_interruptible(monkeypatch):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout"), FakeResponse(0, content=SHARE_PAGE)])
    cancelled = threading.Event()
    monkeypatch.setattr("src.network.time.sleep", lambda *_args: pytest.fail("non-interruptible wait"))

    with pytest.raises(RequestCancelledError):
        network.get_transfer_params(
            "https://pan.baidu.com/s/1first",
            on_retry=lambda *_args: cancelled.set(),
            cancel_event=cancelled,
        )

    assert network.s.calls == 1


def test_cancelling_during_a_retry_wait_wakes_the_request_worker(monkeypatch):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout")])
    waiting = threading.Event()
    errors = []

    class CancellationEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    cancelled = CancellationEvent()
    monkeypatch.setattr("src.network.random.uniform", lambda *_args: 60)

    def request():
        try:
            network.get_transfer_params("https://pan.baidu.com/s/1first", cancel_event=cancelled)
        except RequestCancelledError as error:
            errors.append(error)

    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
    finally:
        cancelled.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert network.s.calls == 1


@pytest.mark.parametrize("message", ["分享已取消", "分享的文件已经被删除了", "链接已失效"])
def test_confirmed_unavailable_share_page_does_not_retry(message):
    network = Network()
    body = f"<html><p>{message}</p></html>".encode()
    network.s = FakeSession([FakeResponse(0, content=body)])

    assert network.get_transfer_params("https://pan.baidu.com/s/1first") == body.decode()
    assert network.s.calls == 1


def test_get_dir_list_reads_all_pages():
    first_page = [{"fs_id": str(index)} for index in range(1000)]
    second_page = [{"fs_id": "1000"}]
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": first_page, "total": 1001}),
        FakeResponse(0, {"errno": 0, "list": second_page, "total": 1001}),
    ])

    result = network.get_dir_list("/target")

    assert len(result) == 1001
    assert network.s.requests[0]["params"]["page"] == "1"
    assert network.s.requests[1]["params"]["page"] == "2"


def test_pagination_deduplicates_overlapping_items_and_uses_unique_total():
    first = [{"fs_id": index} for index in range(1000)]
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": first, "total": 1002}),
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": "999"}, {"fs_id": "1000"}], "total": 1002}),
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": "1001"}], "total": 1002}),
    ])

    result = network.get_dir_list("/target")

    assert len(result) == 1002
    assert len({str(item["fs_id"]) for item in result}) == 1002
    assert result[-1] == {"fs_id": "1001"}
    assert [request["params"]["page"] for request in network.s.requests] == ["1", "2", "3"]


def test_pagination_retries_an_overlap_only_page_without_claiming_completion(monkeypatch):
    first = [{"fs_id": index} for index in range(1000)]
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": first, "total": 1001}),
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": 999}], "total": 1001}),
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": 1000}], "total": 1001}),
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    result = network.get_dir_list("/target")

    assert len(result) == 1001
    assert result[-1] == {"fs_id": 1000}
    assert [request["params"]["page"] for request in network.s.requests] == ["1", "2", "2"]


def test_pagination_uses_canonical_records_when_file_ids_are_unavailable():
    first = {"server_filename": "first", "isdir": 0}
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": [first, dict(first)], "total": 2}),
        FakeResponse(0, {"errno": 0, "list": [
            {"isdir": 0, "server_filename": "first"}, {"server_filename": "second", "isdir": 0},
        ], "total": 2}),
    ])

    assert network.get_dir_list("/target") == [first, {"server_filename": "second", "isdir": 0}]


@pytest.mark.parametrize("errno", [-9, -6, 4])
@pytest.mark.parametrize("extra", [{}, {"list": []}])
def test_get_dir_list_returns_api_errors_without_reading_the_list(errno, extra):
    network = Network()
    network.s = FakeSession([FakeResponse(errno, {"errno": errno, **extra})])

    assert network.get_dir_list("/target") == errno
    assert network.s.calls == 1


@pytest.mark.parametrize("metadata", [{"total": 1}, {"has_more": 1}, {"total": 1, "has_more": True}])
def test_pagination_with_an_empty_page_and_more_metadata_stops_with_an_error(monkeypatch, metadata):
    network = Network()
    network.s = FakeSession([FakeResponse(0, {"errno": 0, "list": [], **metadata})] * 5)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(ValueError, match="pagination"):
        network.get_dir_list("/target")

    assert network.s.calls == 5


def test_repeated_pagination_page_is_not_appended_or_requested_forever(monkeypatch):
    page = {"errno": 0, "list": [{"fs_id": 1}], "has_more": 1}
    network = Network()
    network.s = FakeSession([FakeResponse(0, page)] * 6)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(ValueError, match="pagination"):
        network.get_dir_list("/target")

    assert network.s.calls == 6


def test_pagination_cannot_cycle_between_two_pages(monkeypatch):
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": fs_id}], "has_more": 1})
        for fs_id in [1, 2, 1, 2, 1, 2, 1]
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(ValueError, match="pagination"):
        network.get_dir_list("/target")

    assert network.s.calls == 7


def test_pagination_retries_conflicting_metadata_without_advancing_the_page(monkeypatch):
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": [], "total": 1}),
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": 1}], "total": 1}),
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    assert network.get_dir_list("/target") == [{"fs_id": 1}]
    assert [request["params"]["page"] for request in network.s.requests] == ["1", "1"]


def test_pagination_retries_a_repeated_page_then_returns_the_complete_list(monkeypatch):
    first = {"errno": 0, "list": [{"fs_id": 1}], "has_more": 1}
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, first), FakeResponse(0, first),
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": 2}]}),
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    assert network.get_dir_list("/target") == [{"fs_id": 1}, {"fs_id": 2}]
    assert [request["params"]["page"] for request in network.s.requests] == ["1", "2", "2"]


def test_directory_without_more_metadata_stops_at_a_normal_empty_final_page():
    first = [{"fs_id": index} for index in range(1000)]
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": first}),
        FakeResponse(0, {"errno": 0, "list": []}),
    ])

    assert network.get_dir_list("/target") == first
    assert network.s.calls == 2


def test_network_instances_do_not_share_cookie_headers():
    first = Network()
    second = Network()
    first.headers["Cookie"] = "BAIDUID=first"

    assert "Cookie" not in second.headers


def test_reset_session_discards_session_cookies_and_token(monkeypatch):
    network = Network()
    old_session = network.s
    closed = []
    monkeypatch.setattr(old_session, "close", lambda: closed.append(True))
    old_session.cookies.set("old-share-cookie", "expired")
    network.bdstoken = "expired-token"
    network.headers["Cookie"] = "BAIDUID=current"

    network.reset_session()

    assert closed == [True]
    assert network.s is not old_session
    assert not network.s.cookies
    assert network.bdstoken == ""
    assert network.headers["Cookie"] == "BAIDUID=current"
    network.s.close()


def test_parse_response_deduplicates_embedded_file_records():
    record = '{"fs_id":3,"isdir":0,"server_filename":"one"}'
    response = f'{{"shareid":1,"share_uk":"2","list":[{record},{record}]}}'

    assert parse_response(response) == ["1", "2", ["3"], ["one"], ["0"]]


def test_parse_response_keeps_file_metadata_with_its_own_record():
    first = {"fs_id": 3, "isdir": 0, "server_filename": "{one}", "size": 100, "md5": "a" * 32}
    second = {"fs_id": 4, "isdir": 0, "server_filename": "two"}
    page = json.dumps({"size": 999, "md5": "unrelated", "shareid": 1, "share_uk": 2,
                       "list": [first, second, first, second]})

    result = parse_response(page)

    assert result == ["1", "2", ["3", "4"], ["{one}", "two"], ["0", "0"]]
    assert result.item_metadata == [{"size": 100, "md5": "a" * 32}, {}]


def test_parse_response_discards_conflicting_metadata_for_duplicate_file_ids():
    first = {"fs_id": 3, "isdir": 0, "server_filename": "one", "size": 100, "md5": "a" * 32}
    second = {**first, "size": 200}
    page = json.dumps({"shareid": 1, "share_uk": 2, "list": [first, second]})

    assert parse_response(page).item_metadata == [{}]


def test_parse_response_reads_metadata_from_nested_share_page_json():
    record = {"fs_id": 3, "isdir": 0, "server_filename": "one", "size": 100, "md5": "a" * 32,
              "thumbs": {"url1": "https://example.invalid/image"}}
    page = '<script>var settings = {invalid: true}; var share = ' + json.dumps({
        "shareid": 1, "share_uk": 2, "file_list": {"list": [record]},
    }) + ';</script>'

    result = parse_response(page)

    assert result == ["1", "2", ["3"], ["one"], ["0"]]
    assert result.item_metadata == [{"size": 100, "md5": "a" * 32}]


def test_parse_response_keeps_distinct_files_with_the_same_display_name():
    response = (
        '{"shareid":1,"share_uk":"2","list":['
        '{"fs_id":3,"isdir":0,"server_filename":"one"},'
        '{"fs_id":4,"isdir":0,"server_filename":"one"}]}'
    )

    assert parse_response(response) == ["1", "2", ["3", "4"], ["one", "one"], ["0", "0"]]


def test_parse_response_decodes_escaped_names_and_whitespace():
    response = r'{"shareid": 1, "share_uk": 2, "fs_id": 3, "isdir": 0, "server_filename": "\u6587\u4ef6\"name"}'

    assert parse_response(response) == ["1", "2", ["3"], ['\u6587\u4ef6"name'], ["0"]]


@pytest.mark.parametrize("response", [None, {}, [], "", "not a sharing page"])
def test_invalid_page_is_a_link_error(response):
    assert parse_response(response) == -1


def test_link_without_a_password_separator_can_be_parsed():
    assert parse_url_and_code("https://pan.baidu.com/s/1link") == ("https://pan.baidu.com/s/1link", "")
