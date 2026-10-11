import pytest
import requests

from src.network import Network, UNKNOWN_RESULT
from src.utils import parse_response, parse_url_and_code


class FakeResponse:
    def __init__(self, errno, payload=None):
        self.errno = errno
        self.payload = {"errno": errno} if payload is None else payload

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


def test_transfer_exhaustion_returns_the_last_errno(monkeypatch):
    network = Network()
    network.s = FakeSession([FakeResponse(500) for _ in range(5)])
    sleeps = []
    attempts = []
    monkeypatch.setattr("src.network.time.sleep", sleeps.append)

    result = network.transfer_file(
        ["123", "456", ["789"], ["file"], ["0"]], "target",
        on_retry=lambda attempt, reason: attempts.append((attempt, reason)),
    )

    assert result == 500
    assert network.s.calls == 5
    assert len(sleeps) == 4
    assert attempts == [(2, 500), (3, 500), (4, 500), (5, 500)]
    assert all(1 <= delay <= 2 for delay in sleeps)


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


def test_exhausted_timeouts_raise_for_item_level_handling(monkeypatch):
    network = Network()
    network.s = FakeSession([requests.Timeout("timeout") for _ in range(5)])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(requests.Timeout):
        network.transfer_file(["123", "456", ["789"], ["file"], ["0"]], "target")

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
