from types import SimpleNamespace
import json

import pytest

from src.operations import Operations


VALID_RESULT = ["123", "456", ["789"], ["file"], ["0"]]


class DummyText:
    def __init__(self, value=""):
        self.value = value

    def get(self, *_args):
        return self.value

    def insert(self, _position, value):
        self.value += value

    def see(self, position):
        assert position == "end"

    def delete(self, *_args):
        self.value = ""

    def config(self, **_kwargs):
        pass


class DummyWidget:
    def __init__(self):
        self.options = {}

    def config(self, **kwargs):
        self.options.update(kwargs)

    def unbind(self, *_args):
        pass


class DummyVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def make_root(raw_links):
    return SimpleNamespace(
        entry_cookie=DummyText("BAIDUID=test"),
        entry_folder_name=DummyText("target"),
        var_trust_env=DummyVar(False),
        var_custom_mode=DummyVar(True),
        var_check_mode=DummyVar(False),
        text_links=DummyText(raw_links),
        text_logs=DummyText(),
        label_status=DummyWidget(),
        bottom_share=DummyWidget(),
        bottom_save=DummyWidget(),
    )


def make_operation(raw_links):
    operation = Operations(make_root(raw_links))
    operation.network.get_dir_list = lambda *_args: []
    operation.network.create_dir = lambda *_args: 0
    operation.network.transfer_file = lambda *_args, **_kwargs: 0
    operation.verify_link = lambda *_args: VALID_RESULT
    operation.prepare_run()
    operation.setup_save()
    return operation


def test_setup_save_keeps_duplicate_input_rows_aligned():
    operation = make_operation(
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1same 1234"
    )

    assert len(operation.link_list) == 2
    assert len(operation.link_list_org) == 2


def test_custom_directory_number_counts_only_valid_links():
    operation = make_operation(
        "title without a link\n"
        "https://pan.baidu.com/s/1first 1234\n"
        "another unsupported title\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    created_paths = []
    operation.handle_create_dir = lambda path, **_kwargs: created_paths.append(path) or True

    operation.handle_process_save()

    assert created_paths == ["target/1", "target/2"]
    assert operation.completed_task_count == 4


def test_one_link_failure_does_not_stop_the_batch():
    operation = make_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    processed = []

    def process(url_code, raw_link=''):
        processed.append((url_code, raw_link))
        if len(processed) == 1:
            raise RuntimeError("broken response")

    operation.pause_detection = process
    operation.handle_process_save()

    assert len(processed) == 2
    assert operation.completed_task_count == 2
    assert "broken response" in operation.root.text_logs.value


def test_duplicate_title_rows_cannot_shift_a_link_to_the_next_title():
    operation = make_operation(
        "83. repeated title\n"
        "https://pan.baidu.com/s/1first 1234\n"
        "83. repeated title\n"
        "https://pan.baidu.com/s/1second 1234\n"
        "84. next title\n"
        "https://pan.baidu.com/s/1third 1234"
    )
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/1", "target/2", "target/3"]
    assert operation.completed_task_count == 6


def test_duplicate_custom_links_get_distinct_numbered_directories():
    operation = make_operation(
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1same 1234"
    )
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/1", "target/2"]


def test_explicit_directory_prefix_is_bound_to_its_own_link():
    operation = make_operation(
        "unsupported title\n"
        "first https://pan.baidu.com/s/1same 1234\n"
        "another title\n"
        "second https://pan.baidu.com/s/1same 1234"
    )
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/first", "target/second"]


def test_nonempty_existing_directory_is_not_reused():
    operation = make_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    paths = []
    operation.network.create_dir = lambda path: -8 if path == "target/1" else 0
    operation.network.get_dir_list = lambda *_args: [{"server_filename": "existing"}]
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/2", "target/3"]


def test_existing_empty_directory_can_be_reused():
    operation = make_operation("https://pan.baidu.com/s/1first 1234")
    paths = []
    operation.network.create_dir = lambda _path: -8
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/1"]


def test_same_explicit_directory_is_allocated_once_per_link():
    operation = make_operation(
        "files https://pan.baidu.com/s/1first 1234\n"
        "files https://pan.baidu.com/s/1second 1234"
    )
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/files", "target/files_2"]


def test_collision_retries_in_a_new_empty_directory():
    operation = make_operation("https://pan.baidu.com/s/1first 1234")
    paths = []

    def transfer(_params, path, **_kwargs):
        paths.append(path)
        return 4 if len(paths) == 1 else 0

    operation.network.transfer_file = transfer
    operation.handle_process_save()

    assert paths == ["target/1", "target/2"]
    assert "target/2" in operation.root.text_logs.value
    assert operation.completed_task_count == 1


def test_directory_creation_error_is_isolated_to_one_item():
    operation = make_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    paths = []
    operation.network.create_dir = lambda path: -7 if path == "target/1" else 0
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/2"]
    assert operation.completed_task_count == 2
    assert "-7" in operation.root.text_logs.value


def test_failed_normal_transfer_does_not_suppress_a_duplicate_link():
    operation = make_operation(
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1same 1234"
    )
    operation.custom_mode = False
    calls = []

    def transfer(*_args, **_kwargs):
        calls.append(True)
        return 500 if len(calls) == 1 else 0

    operation.network.transfer_file = transfer
    operation.handle_process_save()

    assert len(calls) == 2
    assert operation.completed_task_count == 3


@pytest.mark.parametrize("invalid", [None, [], ["123"], ["123", "456", None, [], []]])
def test_malformed_transfer_params_do_not_crash_the_batch(invalid):
    operation = make_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    results = iter([invalid, VALID_RESULT])
    paths = []
    operation.verify_link = lambda *_args: next(results)
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/1"]
    assert operation.completed_task_count == 2


def test_custom_transfer_uses_one_create_request_without_list_or_sleep(monkeypatch):
    operation = make_operation("https://pan.baidu.com/s/1first 1234")
    calls = []
    operation.network.create_dir = lambda path: calls.append(("create", path)) or 0
    operation.network.get_dir_list = lambda path: calls.append(("list", path)) or []
    monkeypatch.setattr("src.operations.time.sleep", lambda _seconds: calls.append(("sleep",)))

    operation.handle_process_save()

    assert calls == [("create", "target/1")]


def test_null_passcode_result_is_rejected_before_updating_cookie():
    operation = make_operation("")
    operation.network.verify_pass_code = lambda *_args: None

    result = Operations.verify_link(operation, "https://pan.baidu.com/s/1same", "1234")

    assert result == -1
    assert operation.network.headers["Cookie"] == "BAIDUID=test"


def test_retry_exhaustion_logs_failure_and_continues_the_real_batch(monkeypatch):
    operation = make_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    del operation.network.transfer_file
    paths = []

    def post(**kwargs):
        paths.append(kwargs["data"]["path"])
        errno = 500 if len(paths) <= 5 else 0
        return SimpleNamespace(json=lambda: {"errno": errno}, raise_for_status=lambda: None)

    operation.network.s = SimpleNamespace(post=post)
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    operation.handle_process_save()

    assert paths == ["/target/1"] * 5 + ["/target/2"]
    assert operation.completed_task_count == 2
    assert "500" in operation.root.text_logs.value
    assert "第 5 次" in operation.root.text_logs.value


@pytest.mark.parametrize("invalid", [None, [], ["123"], ["123", "456", None, [], []]])
def test_check_mode_handles_malformed_params_without_stopping(invalid):
    operation = make_operation("https://pan.baidu.com/s/1first 1234")
    operation.check_mode = True
    operation.verify_link = lambda *_args: invalid

    operation.handle_process_save()

    assert operation.completed_task_count == 1
    assert "链接无效" in operation.root.text_logs.value


def test_verifying_a_duplicate_success_does_not_make_more_requests():
    operation = make_operation(
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1same 1234"
    )
    operation.custom_mode = False
    verifications = []
    operation.verify_link = lambda *args: verifications.append(args) or VALID_RESULT

    operation.handle_process_save()

    assert len(verifications) == 1
    assert operation.completed_task_count == 2


def test_save_runs_all_network_stages_with_titles_duplicates_and_retry(monkeypatch):
    root = make_root(
        "repeated title\n"
        "https://pan.baidu.com/s/1same 1234\n"
        "repeated title\n"
        "https://pan.baidu.com/s/1same 1234"
    )
    operation = Operations(root)
    requests = []
    transfer_attempts = 0
    closed = []
    record = {"fs_id": 789, "server_filename": "file", "isdir": 0}
    page = json.dumps({"shareid": 123, "share_uk": "456", "list": [record, record]})

    def response(payload=None, content=b""):
        return SimpleNamespace(
            json=lambda: payload, content=content, raise_for_status=lambda: None,
        )

    def get(**kwargs):
        url = kwargs["url"]
        requests.append(("get", url))
        if url.endswith("/api/gettemplatevariable"):
            return response({"errno": 0, "result": {"bdstoken": "token"}})
        if url.endswith("/api/list"):
            return response({"errno": 0, "list": []})
        return response(content=page.encode())

    def post(**kwargs):
        nonlocal transfer_attempts
        url = kwargs["url"]
        requests.append(("post", url))
        if url.endswith("/share/verify"):
            return response({"errno": 0, "randsk": "share-cookie"})
        if url.endswith("/api/create"):
            assert kwargs["data"]["rtype"] == "0"
            return response({"errno": 0, "path": kwargs["data"]["path"]})
        if url.endswith("/share/transfer"):
            transfer_attempts += 1
            assert kwargs["data"]["fsidlist"] == "[789]"
            expected_path = "/target/1" if transfer_attempts <= 2 else "/target/2"
            assert kwargs["data"]["path"] == expected_path
            return response({"errno": 500 if transfer_attempts == 1 else 0})
        raise AssertionError(f"Unexpected test request: {url}")

    operation.network.s = SimpleNamespace(
        get=get, post=post, close=lambda: closed.append(True),
    )
    monkeypatch.setattr("src.network.time.sleep", lambda _seconds: None)

    operation.save()

    assert operation.completed_task_count == 4
    assert transfer_attempts == 3
    assert sum(url.endswith("/api/list") for _method, url in requests) == 1
    assert sum(url.endswith("/api/create") for _method, url in requests) == 2
    assert root.text_logs.value.count("转存成功") == 2
    assert "未预料错误" not in root.text_logs.value
    assert closed == [True]
    assert operation.running is False


def test_slashes_in_the_base_directory_do_not_change_allocated_paths():
    operation = make_operation("https://pan.baidu.com/s/1first 1234")
    operation.root.entry_folder_name.value = "/target/"
    operation.prepare_run()
    operation.setup_save()
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == ["target/1"]


def make_save_operation(raw_links):
    operation = Operations(make_root(raw_links))
    operation.network.get_bdstoken = lambda: "token"
    operation.network.get_dir_list = lambda *_args: []
    operation.network.create_dir = lambda *_args: 0
    operation.network.transfer_file = lambda *_args, **_kwargs: 0
    operation.verify_link = lambda *_args: VALID_RESULT
    return operation


def interrupt_before_second_item(operation):
    process_save = operation.process_save
    interrupted = False

    def process(url_code, raw_link=""):
        nonlocal interrupted
        if operation.next_task_index == 1 and not interrupted:
            interrupted = True
            raise RuntimeError("batch interrupted")
        process_save(url_code, raw_link)

    operation.process_save = process


def test_interrupted_save_button_resumes_pending_items_and_preserves_directories(monkeypatch):
    operation = make_save_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234\n"
        "https://pan.baidu.com/s/1third 1234"
    )
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0
    interrupt_before_second_item(operation)

    operation.save()

    assert operation.next_task_index == operation.completed_task_count == 1
    assert operation.resume_available is True
    assert operation.custom_dir_count == 1
    assert operation.used_custom_dirs == {"target/1"}
    assert operation.root.bottom_save.options["text"] == "继续转存"
    assert operation.root.bottom_save.options["state"] == "normal"
    assert operation.root.bottom_share.options["state"] == "disabled"
    assert "1/3" in operation.root.label_status.options["text"]
    logs_before_resume = operation.root.text_logs.value
    old_session = operation.network.s
    refreshed_tokens = []
    operation.network.get_bdstoken = lambda: refreshed_tokens.append(True) or "new-token"
    operation.root.entry_cookie.value = "BAIDUID=refreshed"
    operation.root.var_trust_env.value = True
    # Editing the UI must not change the pending batch's task snapshot.
    operation.root.text_links.value = "https://pan.baidu.com/s/1replacement 4321"
    operation.root.entry_folder_name.value = "different-target"
    operation.root.var_custom_mode.value = False
    operation.root.var_check_mode.value = True
    monkeypatch.setattr("src.operations.thread_it", lambda func, *args: func(*args))

    operation.root.bottom_save.options["command"]()

    assert paths == ["target/1", "target/2", "target/3"]
    assert operation.next_task_index == operation.completed_task_count == 3
    assert operation.custom_dir_count == 3
    assert operation.used_custom_dirs == {"target/1", "target/2", "target/3"}
    assert "target" in operation.created_dirs
    assert operation.root.text_logs.value.startswith(logs_before_resume)
    assert operation.root.text_logs.value.count("转存成功") == 3
    assert operation.network.s is not old_session
    assert operation.network.s.trust_env is True
    assert operation.network.headers["Cookie"] == "BAIDUID=refreshed"
    assert operation.network.bdstoken == "new-token"
    assert refreshed_tokens == [True]
    assert operation.resume_available is False
    assert operation.running is False
    assert operation.root.bottom_save.options["text"] == "批量转存"
    assert operation.root.bottom_share.options["state"] == "normal"


def test_resume_preserves_successful_normal_link_deduplication():
    operation = make_save_operation(
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1same 1234\n"
        "https://pan.baidu.com/s/1last 1234"
    )
    operation.root.var_custom_mode.value = False
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0
    interrupt_before_second_item(operation)

    operation.save()
    saved_links = operation.saved_links.copy()
    operation.save(resume=True)

    assert saved_links <= operation.saved_links
    assert paths == ["target", "target"]
    assert operation.completed_task_count == 3
    assert operation.resume_available is False


@pytest.mark.parametrize("first_result", [None, -6, RuntimeError("token API unavailable")])
def test_token_failure_can_be_resumed_after_cookie_is_updated(first_result):
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")

    def token_failure():
        if isinstance(first_result, Exception):
            raise first_result
        return first_result

    operation.network.get_bdstoken = token_failure
    operation.save()

    assert operation.resume_available is True
    assert operation.next_task_index == operation.completed_task_count == 0
    assert operation.root.bottom_save.options["text"] == "继续转存"
    operation.root.entry_cookie.value = "BAIDUID=new-cookie"
    operation.network.get_bdstoken = lambda: "refreshed-token"

    operation.save(resume=True)

    assert operation.completed_task_count == 1
    assert operation.network.headers["Cookie"] == "BAIDUID=new-cookie"
    assert operation.resume_available is False


def test_repeated_resume_failures_do_not_lose_the_checkpoint():
    operation = make_save_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    interrupt_before_second_item(operation)
    operation.save()
    logs = operation.root.text_logs.value
    operation.root.entry_cookie.value = "invalid cookie"

    operation.save(resume=True)

    assert operation.next_task_index == operation.completed_task_count == 1
    assert operation.custom_dir_count == 1
    assert operation.root.text_logs.value.startswith(logs)
    assert operation.resume_available is True
    assert operation.root.bottom_save.options["text"] == "继续转存"
    operation.root.entry_cookie.value = "BAIDUID=updated"
    operation.save(resume=True)

    assert operation.completed_task_count == 2
    assert operation.custom_dir_count == 2
    assert operation.resume_available is False


def test_progress_display_error_does_not_replay_a_completed_transfer():
    operation = make_save_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0
    label_config = operation.root.label_status.config

    def fail_progress_once(**kwargs):
        if kwargs.get("text") == "总进度：1/2":
            operation.root.label_status.config = label_config
            raise RuntimeError("progress display failed")
        label_config(**kwargs)

    operation.root.label_status.config = fail_progress_once
    operation.save()

    assert operation.next_task_index == operation.completed_task_count == 1
    assert operation.resume_available is True
    operation.save(resume=True)

    assert paths == ["target/1", "target/2"]
    assert operation.completed_task_count == 2


def test_pause_button_continues_the_existing_worker_without_a_new_session():
    operation = make_operation("https://pan.baidu.com/s/1first 1234")
    session = operation.network.s

    operation.root.bottom_save.options["command"]()
    assert operation.running is False
    assert operation.root.bottom_save.options["text"] == "点击继续"
    operation.root.bottom_save.options["command"]()

    assert operation.running is True
    assert operation.network.s is session
    assert operation.next_task_index == 0


def test_new_batch_resets_the_checkpoint_after_completion():
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0
    operation.save()
    operation.root.text_links.value = "https://pan.baidu.com/s/1new 1234"

    operation.save()

    assert paths == ["target/1", "target/1"]
    assert operation.next_task_index == operation.completed_task_count == 1
    assert operation.custom_dir_count == 1
    assert operation.root.text_logs.value.count("转存成功") == 1


def test_resume_without_a_pending_batch_does_not_start_a_new_transfer():
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")
    calls = []
    operation.network.get_bdstoken = lambda: calls.append(True) or "token"

    operation.save(resume=True)

    assert calls == []
    assert operation.root.text_logs.value == ""


def test_initial_input_error_does_not_offer_resume_for_an_invalid_batch():
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")
    operation.root.entry_cookie.value = "invalid cookie"

    operation.save()

    assert operation.resume_available is False
    assert operation.root.bottom_save.options["text"] == "批量转存"


def test_duplicate_start_during_a_running_batch_is_ignored():
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")
    calls = []

    def get_token():
        calls.append(True)
        if len(calls) == 1:
            operation.save()
        return "token"

    operation.network.get_bdstoken = get_token
    operation.save()

    assert calls == [True]
    assert operation.completed_task_count == 1


def test_duplicate_resume_during_a_running_batch_is_ignored():
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")
    operation.network.get_bdstoken = lambda: -6
    operation.save()
    calls = []

    def get_token():
        calls.append(True)
        if len(calls) == 1:
            operation.save(resume=True)
        return "token"

    operation.network.get_bdstoken = get_token
    operation.save(resume=True)

    assert calls == [True]
    assert operation.completed_task_count == 1


def test_sharing_cannot_clear_an_interrupted_save_checkpoint(monkeypatch):
    operation = make_save_operation(
        "https://pan.baidu.com/s/1first 1234\n"
        "https://pan.baidu.com/s/1second 1234"
    )
    interrupt_before_second_item(operation)
    operation.save()
    logs = operation.root.text_logs.value

    def unexpected_dialog(_root):
        pytest.fail("Sharing must not start while a save checkpoint is pending")

    monkeypatch.setattr("src.operations.CustomDialog", unexpected_dialog)
    operation.share()

    assert operation.next_task_index == operation.completed_task_count == 1
    assert operation.root.text_logs.value == logs
    assert operation.resume_available is True
    operation.save(resume=True)
    assert operation.completed_task_count == 2


def test_resuming_a_directory_setup_failure_keeps_the_pending_tasks():
    operation = make_save_operation("https://pan.baidu.com/s/1first 1234")
    operation.network.get_dir_list = lambda *_args: -9
    operation.network.create_dir = lambda *_args: -6
    operation.save()

    assert operation.resume_available is True
    assert operation.next_task_index == 0
    assert operation.custom_dir_count == 0
    assert operation.created_dirs == set()
    operation.network.get_dir_list = lambda *_args: []
    operation.network.create_dir = lambda *_args: 0

    operation.save(resume=True)

    assert operation.completed_task_count == 1
    assert operation.used_custom_dirs == {"target/1"}


def test_finished_failures_and_titles_are_not_replayed_after_an_interruption():
    operation = make_save_operation(
        "unsupported title\n"
        "https://pan.baidu.com/s/1failed 1234\n"
        "https://pan.baidu.com/s/1pending 1234"
    )
    calls = []

    def verify(url, _code):
        calls.append(url)
        return -4 if url.endswith("1failed") else VALID_RESULT

    operation.verify_link = verify
    process_save = operation.process_save
    interrupted = False

    def process(url_code, raw_link=""):
        nonlocal interrupted
        if operation.next_task_index == 2 and not interrupted:
            interrupted = True
            raise RuntimeError("batch interrupted")
        process_save(url_code, raw_link)

    operation.process_save = process
    operation.save()

    assert operation.next_task_index == operation.completed_task_count == 2
    assert operation.custom_dir_count == 0
    operation.save(resume=True)

    assert calls == ["https://pan.baidu.com/s/1failed", "https://pan.baidu.com/s/1pending"]
    assert operation.root.text_logs.value.count("不支持的链接") == 1
    assert operation.used_custom_dirs == {"target/1"}
    assert operation.completed_task_count == 3


def test_large_batch_stays_aligned_after_duplicate_titles_and_links():
    links = ["repeated title", "https://pan.baidu.com/s/1same 1234"] * 500
    operation = make_operation("\n".join(links))
    paths = []
    operation.network.transfer_file = lambda _params, path, **_kwargs: paths.append(path) or 0

    operation.handle_process_save()

    assert paths == [f"target/{number}" for number in range(1, 501)]
    assert operation.completed_task_count == 1000
