import json
import threading

import pytest
import requests

from src.network import Network, ParsedTransferPage, RequestCancelledError, UNKNOWN_RESULT
from src.utils import normalize_link, parse_response, parse_url_and_code


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


@pytest.mark.parametrize("url", [
    "https://example.invalid/x",
    "https://pan.baidu.com.example.invalid/s/1first",
    "https://pan.baidu.com@example.invalid/s/1first",
    "https://example.invalid/?next=https://pan.baidu.com/s/1first",
    "https://pan.baidu.com:444/s/1first",
    "https://user:password@pan.baidu.com/s/1first",
    "http://pan.baidu.com/s/1first",
])
def test_share_page_rejects_untrusted_origin_before_sending_cookies(url):
    network = Network()
    network.headers["Cookie"] = "BAIDUID=dummy"
    network.s = FakeSession([])

    result = network.get_transfer_params(url)

    assert result.parsed == -1
    assert network.s.calls == 0


@pytest.mark.parametrize("location", [
    "https://example.invalid/x", "//example.invalid/x",
    "http://pan.baidu.com/s/1next", "https://pan.baidu.com:444/s/1next",
    "https://pan.baidu.com@example.invalid/x",
])
def test_share_page_blocks_untrusted_redirects(location):
    network = Network()
    redirect = FakeResponse(0, content=SHARE_PAGE)
    redirect.status_code = 302
    redirect.headers = {"Location": location}
    network.s = FakeSession([redirect])

    result = network.get_transfer_params("https://pan.baidu.com/s/1first")

    assert result.parsed == -1
    assert network.s.calls == 1
    assert network.s.requests[0]["allow_redirects"] is False


@pytest.mark.parametrize("location", ["/s/1next", "https://pan.baidu.com/s/1next"])
@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
def test_share_page_follows_trusted_redirects_explicitly(location, status_code):
    network = Network()
    redirect = FakeResponse(0, content=SHARE_PAGE)
    redirect.status_code = status_code
    redirect.headers = {"Location": location}
    network.s = FakeSession([redirect, FakeResponse(0, content=SHARE_PAGE)])

    result = network.get_transfer_params("https://pan.baidu.com/s/1first")

    assert result.parsed == ["123", "456", ["789"], ["file"], ["0"]]
    assert [request["url"] for request in network.s.requests] == [
        "https://pan.baidu.com/s/1first", "https://pan.baidu.com/s/1next",
    ]
    assert all(request["allow_redirects"] is False for request in network.s.requests)


def test_share_page_redirect_loop_stops_after_bounded_retries(monkeypatch):
    network = Network()
    redirect = FakeResponse(0, content=SHARE_PAGE)
    redirect.status_code = 302
    redirect.headers = {"Location": "/s/1first"}
    closed = []
    redirect.close = lambda: closed.append(True)
    network.s = FakeSession([redirect] * 30)
    retries = []
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(requests.TooManyRedirects):
        network.get_transfer_params(
            "https://pan.baidu.com/s/1first",
            on_retry=lambda attempt, reason: retries.append((attempt, reason)),
        )

    assert network.s.calls == 30
    assert len(closed) == 30
    assert retries == [(attempt, "TooManyRedirects") for attempt in range(2, 6)]


def test_share_page_cancellation_during_redirect_stops_the_next_request():
    network = Network()
    cancelled = threading.Event()
    redirect = FakeResponse(0, content=SHARE_PAGE)
    redirect.status_code = 302
    redirect.headers = {"Location": "/s/1next"}
    redirect.close = cancelled.set
    network.s = FakeSession([redirect])

    with pytest.raises(RequestCancelledError):
        network.get_transfer_params(
            "https://pan.baidu.com/s/1first", cancel_event=cancelled,
        )

    assert network.s.calls == 1


@pytest.mark.parametrize("text, expected_code", [
    ("https://pan.baidu.com/s/1first 解压密码：9999 提取码：abcd", "abcd"),
    ("https://pan.baidu.com/s/1first 解压密码：9999 提取: abcd", "abcd"),
    ("https://pan.baidu.com/s/1first?pwd=abcd 提取码：efgh 解压密码：9999", "abcd"),
    ("https://pan.baidu.com/s/1first 解压密码：9999", ""),
    ("https://pan.baidu.com/s/1first abcd 解压密码：9999", "abcd"),
    ("https://pan.baidu.com/s/1first 提取码：12345 解压密码：9999", ""),
    ("https://pan.baidu.com/share/init?surl=first&pwd=abcd 解压密码：9999", "abcd"),
])
def test_passcode_priority_survives_normalization(text, expected_code):
    assert parse_url_and_code(normalize_link(text))[1] == expected_code


@pytest.mark.parametrize("suffix, expected_code", [
    ("提取码：abcd", "abcd"),
    ("，提取码：abcd", "abcd"),
    ("。提取码:abcd", "abcd"),
    ("提取码： abcd", "abcd"),
    (" 提取码：[abcd]", "abcd"),
    (" 提取码：（abcd）", "abcd"),
    (" 提取码：( abcd )", "abcd"),
    (" 提取码：【abcd】", "abcd"),
    (" 提取码abcd", "abcd"),
    (" 提取码[abcd]", "abcd"),
    (" abcd 解压密码与提取码不同", "abcd"),
    ("提取:abcd 解压密码：9999", "abcd"),
    (" 提取码：[12345] 解压密码：9999", ""),
    (" 提取码：[abc] 解压密码：9999", ""),
    (" 提取码：[] 解压密码：9999", ""),
    ("解压密码：9999", ""),
    ("解压密码：9999 提取码：[abcd]", "abcd"),
    ("?pwd=efgh，提取码：[abcd] 解压密码：9999", "efgh"),
    ("?pwd=efgh。解压密码：9999", "efgh"),
])
@pytest.mark.parametrize("share_path", ["/s/1first", "/share/init?surl=first"])
def test_share_url_and_labeled_code_are_separated_without_whitespace(share_path, suffix, expected_code):
    raw = f"https://pan.baidu.com{share_path}{suffix}"
    expected = ("https://pan.baidu.com/s/1first", expected_code)

    assert parse_url_and_code(raw) == expected
    assert parse_url_and_code(normalize_link(raw)) == expected


@pytest.mark.parametrize("scheme", ["http", "HTTP", "hTtPs"])
def test_attached_passcode_uses_raw_offsets_before_http_upgrade(scheme):
    raw = f"链接：{scheme}://pan.baidu.com/s/1first提取码：[abcd] 解压密码：9999"
    expected = ("https://pan.baidu.com/s/1first", "abcd")

    assert parse_url_and_code(raw) == expected
    assert parse_url_and_code(normalize_link(raw)) == expected


@pytest.mark.parametrize("raw", [
    "https://example.invalid/x提取码：abcd https://pan.baidu.com/s/1first",
    "https://pan.baidu.com.example.invalid/x，提取码：abcd",
    "https://pan.baidu.com提取码：abcd/x",
    "https://pan.baidu.com，example.invalid/s/1first 提取码：abcd",
    "https://pan.baidu.com提取码：abcd@example.invalid/x",
])
def test_labeled_code_splitting_does_not_relax_first_url_origin_validation(raw):
    assert parse_url_and_code(raw) == ("", "")
    assert parse_url_and_code(normalize_link(raw)) == ("", "")


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


@pytest.mark.parametrize("invalid_item", [
    None, {}, [], "corrupt", 1,
    {"server_filename": "file"}, {"isdir": 0},
    {"server_filename": "", "isdir": 0},
    {"server_filename": "file", "isdir": True},
    {"server_filename": "file", "isdir": 2},
    {"fs_id": True}, {"fs_id": -1}, {"fs_id": 1.5}, {"fs_id": "invalid"},
    {"fs_id": "", "server_filename": "file", "isdir": 0},
])
def test_directory_response_retries_malformed_identity_without_filtering_items(monkeypatch, invalid_item):
    network = Network()
    valid_item = {"fs_id": 900, "server_filename": "file", "isdir": 0}
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": [valid_item, invalid_item]}),
        FakeResponse(0, {"errno": 0, "list": [valid_item]}),
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    assert network.get_dir_list("/target") == [valid_item]
    assert network.s.calls == 2
    assert [call["params"]["page"] for call in network.s.requests] == ["1", "1"]


@pytest.mark.parametrize("invalid_item", [{}, None])
def test_malformed_directory_page_exhaustion_never_returns_partial_items(monkeypatch, invalid_item):
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, {"errno": 0, "list": [{"fs_id": 900}], "has_more": 1}),
        *[FakeResponse(0, {"errno": 0, "list": [{"fs_id": 901}, invalid_item]}) for _ in range(5)],
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    with pytest.raises(ValueError, match="目录列表"):
        network.get_dir_list("/target")

    assert network.s.calls == 6
    assert [call["params"]["page"] for call in network.s.requests] == ["1", "2", "2", "2", "2", "2"]


@pytest.mark.parametrize("item", [
    {"fs_id": 900}, {"fs_id": "900"},
    {"server_filename": "file", "isdir": 0},
    {"server_filename": "folder", "isdir": "1", "fs_id": None},
])
def test_directory_identity_does_not_require_content_metadata(item):
    network = Network()
    network.s = FakeSession([FakeResponse(0, {"errno": 0, "list": [item]})])

    assert network.get_dir_list("/target") == [item]
    assert network.s.calls == 1


@pytest.mark.parametrize("method, arguments, payload, content", [
    ("get_bdstoken", (), {"errno": 0, "result": {"bdstoken": "token"}}, b""),
    ("get_dir_list", ("/target",), {"errno": 0, "list": []}, b""),
    ("create_dir", ("target",), {"errno": 0}, b""),
    ("verify_pass_code", ("https://pan.baidu.com/s/1first", "abcd"), {"errno": 0, "randsk": "dummy"}, b""),
    ("get_transfer_params", ("https://pan.baidu.com/s/1first",), {"errno": 0},
     b'{"shareid":123,"share_uk":"456","fs_id":789,"server_filename":"file","isdir":0}'),
    ("transfer_file", (["123", "456", ["789"], ["file"], ["0"]], "target"), {"errno": 0}, b""),
    ("create_share", (789, 0, "abcd"), {"errno": 0, "link": "https://pan.baidu.com/s/1first"}, b""),
    ("create_dir_compat", ("target",), {"errno": 0}, b""),
])
def test_every_network_endpoint_enables_tls_certificate_verification(method, arguments, payload, content):
    network = Network()
    network.s = FakeSession([FakeResponse(0, payload, content)])

    if method == "create_dir_compat":
        Network.create_dir.__wrapped__(network, *arguments)
    else:
        getattr(network, method)(*arguments)

    assert network.s.calls == 1
    assert network.s.requests[0]["verify"] is True


@pytest.mark.parametrize("method, arguments", [
    ("get_bdstoken", ()),
    ("get_transfer_params", ("https://pan.baidu.com/s/1first",)),
    ("create_dir", ("target",)),
])
def test_certificate_errors_never_retry_with_verification_disabled(monkeypatch, method, arguments):
    network = Network()
    network.s = FakeSession([requests.exceptions.SSLError("certificate verify failed")] * 5)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    if method == "create_dir":
        assert network.create_dir(*arguments) == UNKNOWN_RESULT
    else:
        with pytest.raises(requests.exceptions.SSLError):
            getattr(network, method)(*arguments)

    assert network.s.calls == 5
    assert all(call["verify"] is True for call in network.s.requests)


def test_network_does_not_globally_suppress_tls_warnings(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.packages.urllib3.disable_warnings", lambda: calls.append(True))
    network = Network()

    assert calls == []
    network.s.close()


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


@pytest.mark.parametrize("partial_page", [False, True])
def test_get_dir_list_preserves_unknown_result_instead_of_errno_or_partial_items(
    monkeypatch, partial_page,
):
    network = Network()
    responses = ([{"errno": 0, "list": [{"fs_id": 1}], "has_more": 1}]
                 if partial_page else []) + [UNKNOWN_RESULT]
    payloads = iter(responses)
    monkeypatch.setattr(network, "_request_json", lambda *_args, **_kwargs: next(payloads))

    assert network.get_dir_list("/target") == UNKNOWN_RESULT


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


JSON_REQUEST_ARGUMENTS = {
    "get_bdstoken": (),
    "get_dir_list": ("/target",),
    "verify_pass_code": ("https://pan.baidu.com/s/1first", "1234"),
    "create_dir": ("target/1",),
    "transfer_file": (["123", "456", ["789"], ["file"], ["0"]], "target/1"),
    "create_share": (789, 0, "1234"),
}


@pytest.mark.parametrize("method", JSON_REQUEST_ARGUMENTS)
def test_cancelled_json_call_never_sends_a_request(method):
    network = Network()
    network.s = FakeSession([])
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(RequestCancelledError):
        getattr(network, method)(*JSON_REQUEST_ARGUMENTS[method], cancel_event=cancelled)

    assert network.s.calls == 0


@pytest.mark.parametrize("method", JSON_REQUEST_ARGUMENTS)
def test_main_json_retry_wait_is_interruptible(method):
    network = Network()
    network.s = FakeSession([FakeResponse(500)])
    cancelled = threading.Event()

    with pytest.raises(RequestCancelledError):
        getattr(network, method)(
            *JSON_REQUEST_ARGUMENTS[method], cancel_event=cancelled,
            on_retry=lambda *_args: cancelled.set(),
        )

    assert network.s.calls == 1


@pytest.mark.parametrize("method", ["create_dir", "transfer_file", "create_share"])
def test_cancellation_during_ambiguous_write_retry_preserves_unknown_result(method):
    network = Network()
    network.s = FakeSession([requests.Timeout("ambiguous write")])
    cancelled = threading.Event()

    result = getattr(network, method)(
        *JSON_REQUEST_ARGUMENTS[method], cancel_event=cancelled,
        on_retry=lambda *_args: cancelled.set(),
    )

    assert result == UNKNOWN_RESULT
    assert network.s.calls == 1


def test_cancelled_pagination_does_not_request_the_next_page():
    network = Network()
    cancelled = threading.Event()

    def first_page(**_kwargs):
        cancelled.set()
        return FakeResponse(0, {"errno": 0, "list": [{"fs_id": 1}], "has_more": 1})

    from unittest.mock import Mock
    network.s = Mock(get=Mock(side_effect=first_page))

    with pytest.raises(RequestCancelledError):
        network.get_dir_list("/target", cancel_event=cancelled)

    assert network.s.get.call_count == 1


@pytest.mark.parametrize("method", ["get_bdstoken", "transfer_file"])
def test_main_json_worker_wakes_when_cancelled_during_retry_wait(monkeypatch, method):
    network = Network()
    ambiguous = method == "transfer_file"
    network.s = FakeSession([requests.Timeout("in flight") if ambiguous else FakeResponse(500)])
    waiting = threading.Event()
    results = []
    errors = []

    class CancellationEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    cancelled = CancellationEvent()
    monkeypatch.setattr("src.network.random.uniform", lambda *_args: 60)

    def request():
        try:
            results.append(getattr(network, method)(
                *JSON_REQUEST_ARGUMENTS[method], cancel_event=cancelled,
            ))
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
    assert network.s.calls == 1
    assert results == ([UNKNOWN_RESULT] if ambiguous else [])
    assert len(errors) == (0 if ambiguous else 1)


def test_share_page_wrapper_contains_only_the_final_validated_response(monkeypatch):
    network = Network()
    network.s = FakeSession([
        FakeResponse(0, content=b"<p>Busy</p>"), FakeResponse(0, content=SHARE_PAGE),
    ])
    monkeypatch.setattr("src.network.time.sleep", lambda *_args: None)
    parsed = []

    def parse(text):
        parsed.append(text)
        return parse_response(text)

    monkeypatch.setattr("src.network.parse_response", parse)
    result = network.get_transfer_params("https://pan.baidu.com/s/1first")

    assert isinstance(result, str)
    assert isinstance(result, ParsedTransferPage)
    assert result == SHARE_PAGE.decode()
    assert result.parsed == ["123", "456", ["789"], ["file"], ["0"]]
    assert parsed == ["<p>Busy</p>", SHARE_PAGE.decode()]
