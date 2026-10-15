"""
主要功能实现逻辑，包括批量转存和分享。

:author: assassing
:contact: https://github.com/hxz393
:copyright: Copyright 2024, hxz393. 保留所有权利。
"""

import sys
import inspect
import re
import threading
import time
import traceback
from copy import deepcopy
from collections import Counter
from dataclasses import dataclass, field
from typing import Union, List, Dict, Any, Tuple, Optional

import requests

from src.constants import EXP_MAP, DELAY_SECONDS, INVALID_CHARS, ERROR_CODES, SAVE_LIMIT, COLOR_MAP
from src.network import (
    Network, ParsedTransferPage, RequestCancelledError, RetryExhaustedError, UNKNOWN_RESULT,
)
from src.ui import CustomDialog
from src.utils import (
    thread_it,
    write_config,
    parse_response,
    normalize_link,
    parse_url_and_code,
    update_cookie,
    generate_code,
    is_baidu_url,
    is_valid_directory_list,
    is_valid_transfer_params,
)


class RetryableTaskError(RuntimeError):
    """当前任务未完成或结果无法确认，必须保留 checkpoint 以便恢复。"""


class PermanentTaskError(RuntimeError):
    """当前任务已确定失败，可以记录并推进到下一条。"""


class CancellationRequested(RequestCancelledError):
    """窗口关闭或用户取消了 worker。"""


@dataclass
class CachedShare:
    """Share parameters and the password authentication needed to reuse them."""

    params: List[Any]
    bdclnd: Optional[str] = None


@dataclass
class UnknownTransferState:
    """保存一次结果未知的转存，以便在恢复前后核对目标目录。"""

    params: List[Any]
    folder_name: str
    url_code: str
    baseline_items: List[Dict[str, Any]]
    baseline_known: bool = True


@dataclass
class PrefetchState:
    """保存后台预取的分享页面结果，主 worker 只负责按顺序消费。"""

    event: threading.Event
    cookie_generation: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    network: Optional[Network] = None
    result: Union[List[Any], int, None] = None
    error: Exception = None


class Operations:
    """
    包括主功能实现流程，涉及部分对 UI 的操作。
    日志文字不额外提取到映射字典，方便定位错误。

    :param root: 主窗口
    """

    PREFETCH_WORKERS = 3
    CUSTOM_TRANSFER_ATTEMPTS = 3

    def __init__(self, root):
        self.root = root
        self.network = Network()
        self.custom_dir_count = 0
        self.created_dirs = set()
        self.used_custom_dirs = set()
        self.pending_custom_dirs = {}
        self.custom_directory_numbers = {}
        self.custom_transfer_paths = {}
        self.directory_number_hints = {}
        self.unknown_transfer_tasks = set()
        self.unknown_transfer_states: Dict[int, UnknownTransferState] = {}
        self.destination_baselines: Dict[str, List[Dict[str, Any]]] = {}
        self.transfer_params_cache: Dict[Tuple[str, str], CachedShare] = {}
        self.prefetch_states: Dict[Tuple[str, str], PrefetchState] = {}
        self.cookie_generation = 0
        self.saved_links = set()
        self.share_results: List[str] = []
        self.link_list = []
        self.link_list_org = []
        self.next_task_index = 0
        self.completed_task_count = 0
        self.total_task_count = 0
        self.resume_available = False
        self.running = False
        self._run_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._ui_thread = threading.current_thread()
        self.cancel_event = threading.Event()
        self.worker_thread = None
        self.batch_cookie = ''
        self._ui_snapshot = None

    def start_save(self, resume: bool = False) -> None:
        """从 UI thread 启动转存 worker。"""
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.cancel_event.clear()
        self._ui_snapshot = self._capture_ui_snapshot()
        self.worker_thread = thread_it(self.save, resume)

    def start_share(self) -> None:
        """在 UI thread 创建设置对话框，再启动分享 worker。"""
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if self.resume_available:
            return
        dialog_result = CustomDialog(self.root)
        if dialog_result.result:
            self._ui_snapshot = self._capture_ui_snapshot()
            self.worker_thread = thread_it(self.share, dialog_result)

    def is_worker_alive(self) -> bool:
        return bool(self.worker_thread and self.worker_thread.is_alive())

    def copy_share_results(self) -> None:
        """复制本轮成功生成的分享链接，避免把 retry 或失败日志混入结果。"""
        if not self.share_results:
            return
        content = '\n'.join(self.share_results)
        result_count = len(self.share_results)

        def copy_to_clipboard():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(content)
            except Exception:
                self.root.label_status.config(text='复制失败，请重试', bootstyle='danger')
                return
            self.root.label_status.config(
                text=f'已复制 {result_count} 条分享结果', bootstyle='success',
            )

        self._ui_call(copy_to_clipboard)

    def request_cancel(self) -> None:
        """通知 worker 尽快退出，暂停等待也会被立即唤醒。"""
        self.cancel_event.set()
        self.running = False
        self._cancel_prefetch()
        try:
            self.network.s.close()
        except Exception:
            pass

    def _cancel_prefetch(self) -> None:
        """Cancel each worker independently so a resume cannot revive old retries."""
        with self._prefetch_lock:
            states = list(self.prefetch_states.values())
            for state in states:
                state.cancel_event.set()
        for state in states:
            if state.network is not None:
                try:
                    state.network.s.close()
                except Exception:
                    pass

    def _ui_call(self, callback, *args) -> None:
        """把 Tk 操作切回创建窗口的 UI thread。"""
        if threading.current_thread() is self._ui_thread:
            callback(*args)
            return
        after = getattr(self.root, 'after', None)
        if not callable(after):
            callback(*args)
            return

        def invoke_on_ui():
            try:
                callback(*args)
            except Exception:
                # A close callback may destroy Tk before queued updates run.
                pass

        try:
            after(0, invoke_on_ui)
        except Exception:
            # The window may already be closing.
            pass

    def _retry_log(self, attempt: int, reason: Any) -> None:
        self.insert_logs(f'请求失败（{reason}），正在第 {attempt} 次尝试')

    def _call_network_method(self, method, *args, on_retry=None):
        """Forward supported callbacks without changing legacy mock interfaces."""
        self._check_cancelled()
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        options = {}
        if 'on_retry' in parameters:
            options['on_retry'] = self._retry_log if on_retry is None else on_retry
        if 'cancel_event' in parameters:
            options['cancel_event'] = self.cancel_event
        try:
            return method(*args, **options)
        except RequestCancelledError as error:
            raise CancellationRequested() from error

    def _network_call(self, method, *args):
        """兼容旧测试或外部替换的无 callback mock。"""
        try:
            return self._call_network_method(method, *args)
        except (requests.RequestException, ValueError, TypeError, KeyError,
                AttributeError) as error:
            raise RetryableTaskError(f'API 请求失败：{error}') from error

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancellationRequested()

    def _capture_ui_snapshot(self) -> dict:
        """在 UI thread 读取一次输入，避免 worker 直接访问 Tk。"""
        return {
            'cookie': self.root.entry_cookie.get(),
            'folder_name': self.root.entry_folder_name.get(),
            'trust_env': self.root.var_trust_env.get(),
            'custom_mode': self.root.var_custom_mode.get(),
            'check_mode': self.root.var_check_mode.get(),
            'links': self.root.text_links.get(1.0, 'end'),
            'placeholder_text': getattr(self.root.text_links, 'placeholder_text', ''),
            'is_placeholder': getattr(self.root.text_links, 'is_placeholder', False),
        }

    def _input_value(self, name: str, getter) -> Any:
        """读取 worker 快照；同步调用仍使用实时 UI 值以兼容测试和 API。"""
        if threading.current_thread() is self._ui_thread or self._ui_snapshot is None:
            return getter()
        return self._ui_snapshot[name]

    def save(self, resume: bool = False) -> None:
        """
        转存主函数，流程为：
        1.获取用户输入，更新按钮和标签状态；
        2.检查用户输入，有问题则终止并发送日志；
        3.获取 bdstoken，如果获取失败则终止；
        4.获取根目录文件列表，建立转存目录；
        5.批量转存链接。先验证链接有效性，再转存文件，插入结果到日志框。

        :param resume: 从内存中保留的断点继续，不重新读取链接或重置任务状态。
        """
        if not self._run_lock.acquire(blocking=False):
            return
        if resume and not self.resume_available:
            self._run_lock.release()
            return
        resume = resume or self.resume_available
        can_resume = resume
        try:
            self.prepare_run(resume=resume)
            if not resume:
                self.setup_save()
            self.handle_input()
            can_resume = True
            self.handle_bdstoken()
            if not self.check_mode:
                self.handle_create_dir(folder_name=self.folder_name)
            if resume:
                self.insert_logs(f'继续转存：已处理 {self.next_task_index}/{self.total_task_count}，从下一条未处理输入继续。')
                self.change_status('running')
            self.handle_process_save()
        except SystemExit:
            # check_condition has already logged the input or API error.
            pass
        except CancellationRequested:
            pass
        except RetryableTaskError as error:
            self.insert_logs(f'当前批次暂未完成，保留断点：{error}')
        except Exception as e:
            self.insert_logs(f'程序出现未预料错误，信息如下：\n{e}\n{traceback.format_exc()}')
        finally:
            self.resume_available = can_resume and self.next_task_index < len(self.link_list)
            self._cancel_prefetch()
            try:
                try:
                    self.network.s.close()
                finally:
                    self.change_status('interrupted' if self.resume_available else 'stopped')
            finally:
                self._run_lock.release()

    def share(self, dialog_result=None) -> None:
        """
        分享主函数，流程为：
        0.弹出设置窗口，设置分享参数；
        1.获取用户输入，更新按钮和标签状态；
        2.检查用户输入，有问题则终止并发送日志；
        3.获取 bdstoken，如果获取失败则终止；
        4.获取目标目录文件列表，如果为空则终止；
        5.批量分享文件。先向链接框插入文件名，再创建分享，插入结果到日志框。
        """
        if self.resume_available:
            return
        if dialog_result is None:
            # Direct callers may still use share() synchronously. The normal
            # UI path creates this dialog in start_share on the main thread.
            dialog_result = CustomDialog(self.root)
        if not dialog_result.result:
            return
        if not self._run_lock.acquire(blocking=False):
            return
        if self.resume_available:
            self._run_lock.release()
            return
        try:
            self.dialog_result = dialog_result
            self.prepare_run()
            self.handle_input(task_count_check=False)
            self.handle_bdstoken()
            self.handle_list_dir()
            self.setup_share()
            self.handle_process_share()
        except CancellationRequested:
            pass
        except RetryableTaskError as error:
            self.insert_logs(f'批量分享暂未完成，请稍后重试：{error}')
        except Exception as e:
            self.insert_logs(f'程序出现未预料错误，信息如下：\n{e}\n{traceback.format_exc()}')
        finally:
            try:
                try:
                    self.network.s.close()
                finally:
                    self.change_status('stopped')
            finally:
                self._run_lock.release()

    def prepare_run(self, resume: bool = False) -> None:
        """获取变量，准备运行。转存和分享共用逻辑"""
        self._cancel_prefetch()
        with self._prefetch_lock:
            self.prefetch_states.clear()
        self.transfer_params_cache.clear()
        self.cookie_generation += 1
        # Cookie 输入框只去除外层空白，Cookie 内部的分隔符和值不能被改写。
        user_cookie = self._input_value(
            'cookie', lambda: self.root.entry_cookie.get()
        ).strip()
        if resume:
            if user_cookie != self.batch_cookie:
                self.network.headers['Cookie'] = user_cookie
                self.cookie = user_cookie
                self.batch_cookie = user_cookie
            else:
                self.cookie = self.network.headers.get('Cookie', user_cookie)
            self.network.reset_session()
        else:
            self.cookie = user_cookie
            self.batch_cookie = user_cookie
            self.folder_name = self._input_value(
                'folder_name', lambda: self.root.entry_folder_name.get()
            ).strip().strip('/')
            self.custom_mode = self._input_value(
                'custom_mode', lambda: self.root.var_custom_mode.get()
            )
            self.check_mode = self._input_value(
                'check_mode', lambda: self.root.var_check_mode.get()
            )
            self.completed_task_count = 0
            self.custom_dir_count = 0
            self.created_dirs = set()
            self.used_custom_dirs = set()
            self.pending_custom_dirs = {}
            self.custom_directory_numbers = {}
            self.custom_transfer_paths = {}
            self.directory_number_hints = {}
            self.unknown_transfer_tasks = set()
            self.unknown_transfer_states = {}
            self.destination_baselines = {}
            self.saved_links = set()
            self.share_results = []
            self.link_list = []
            self.link_list_org = []
            self.next_task_index = 0
            self.resume_available = False
        self.network.s.trust_env = self._input_value(
            'trust_env', lambda: self.root.var_trust_env.get()
        )

        # Keep the original batch's paths, modes and logs when resuming. If
        # the user did not edit Cookie, headers retain the runtime BDCLND.
        if not resume:
            self.network.headers['Cookie'] = self.cookie
        self.change_status('resuming' if resume else 'init')
        write_config(f'{self.cookie}\n{self.folder_name}')

    def setup_save(self) -> None:
        """准备链接，更新状态"""
        text_value = self._input_value(
            'links', lambda: self.root.text_links.get(1.0, 'end')
        )
        placeholder = self._input_value(
            'placeholder_text',
            lambda: getattr(self.root.text_links, 'placeholder_text', ''),
        )
        if placeholder and text_value.strip() == placeholder.strip():
            raw_links = []
        else:
            raw_links = [link for link in text_value.splitlines() if link.strip()]

        self.link_list = [normalize_link(f'{link} ') for link in raw_links]
        # Keep the raw and normalized rows aligned. Duplicate rows are valid input.
        self.link_list_org = raw_links
        self.directory_number_hints = {}
        pending_title_number = None
        for index, (raw_link, url_code) in enumerate(zip(raw_links, self.link_list)):
            has_baidu_link = bool(parse_url_and_code(url_code)[0])
            if has_baidu_link:
                # A numbered title on the preceding row belongs to the next
                # URL when users paste title and link on separate lines.
                if not self._explicit_directory_name(raw_link):
                    title_number = self._extract_title_number(raw_link)
                    if title_number is None:
                        title_number = pending_title_number
                    if title_number is not None:
                        self.directory_number_hints[index] = title_number
                pending_title_number = None
            else:
                pending_title_number = self._extract_title_number(raw_link)
        # Unsupported/title rows remain aligned for logging, but only actual
        # Baidu links count toward SAVE_LIMIT.
        self.valid_link_count = sum(
            bool(parse_url_and_code(link)[0]) for link in self.link_list
        )
        self.total_task_count = len(self.link_list)
        self.change_status('running')

    @staticmethod
    def _extract_title_number(raw_link: str) -> Optional[int]:
        """Extract numbered titles without treating a decimal filename as a title."""
        match = re.match(
            r'^\s*(?:\[([0-9]+)\]|([0-9]+)\s*(?:[.．](?!\d)|[、)）]))',
            raw_link or '',
        )
        return int(match.group(1) or match.group(2)) if match else None

    @classmethod
    def _explicit_directory_name(cls, raw_link: str) -> str:
        """Return a directory prefix explicitly written on the URL row."""
        cleaned = re.sub(r'^\s*链接\s*[:：]\s*', '', raw_link or '')
        url_match = re.search(r'https?://\S+', cleaned, re.IGNORECASE)
        if not url_match:
            return ''
        # Only a whitespace-separated prefix names an explicit directory.
        # Attached text is a title; the URL itself must never enter the path.
        prefix_match = re.match(r'\s*(\S+)\s+', cleaned[:url_match.start()])
        if not prefix_match:
            return ''
        first_token = prefix_match.group(1)
        if '/' in first_token:
            return first_token
        # Only standalone numbering markers (or a bracketed title) override
        # a URL-row prefix; names such as "2026.backup" remain explicit paths.
        if re.fullmatch(r'(?:\[[0-9]+\]|[0-9]+[.．、)）])', first_token) or re.fullmatch(
            r'[0-9]+[.．、)）](?:\[[^\]]*\])+', first_token,
        ):
            return ''
        return first_token

    def _assign_custom_directory_number(self, task_key: int, raw_link: str) -> int:
        """Assign one stable numeric identity to the current custom directory."""
        explicit_name = self._explicit_directory_name(raw_link)
        title_number = None if explicit_name else self.directory_number_hints.get(task_key)
        if title_number is None:
            self.custom_dir_count += 1
            title_number = self.custom_dir_count
        else:
            self.custom_dir_count = max(self.custom_dir_count, title_number)
        self.custom_directory_numbers[task_key] = title_number
        return title_number

    def setup_share(self) -> None:
        """准备参数，更新状态"""
        # 从设置对话框获取参数变量
        self.expiry, self.password, self.random_password = self.dialog_result.result
        # 更新任务总数和状态
        self.total_task_count = len(self.dir_list_all)
        self.change_status('sharing')

    def handle_input(self, task_count_check: bool = True) -> None:
        """输入检查，如链接数限制和 cookie 格式"""
        # 转存时才检查输入链接数量
        if task_count_check:
            self.check_condition(self.valid_link_count == 0,
                                 message='无有效链接。')
            self.check_condition(self.valid_link_count > SAVE_LIMIT,
                                 message=f'批量转存一次不能超过 {SAVE_LIMIT}，当前有效链接数：{self.valid_link_count}')

        # cookie 带非 ascii 字符，或不包含 BAIDUID 时，铁定不对
        self.check_condition(not self.cookie.isascii() or self.cookie.find('BAIDUID') == -1,
                             message='百度网盘 cookie 输入不正确，请查看使用帮助。')
        # 非法字符由官方规定的。符号 / 可以使用，作为子目录的分隔符
        self.check_condition(any(char in self.folder_name for char in INVALID_CHARS),
                             message=r'转存目录名有非法字符，不能包含：< > | * ? \ :')
        if task_count_check and self.custom_mode and not self.check_mode:
            self.check_condition(not self.folder_name, message='必须输入转存目录')

    def handle_bdstoken(self) -> None:
        """获取 bdstoken 相关逻辑"""
        self.network.bdstoken = self._network_call(self.network.get_bdstoken)
        self.check_condition(not isinstance(self.network.bdstoken, str) or not self.network.bdstoken,
                             message=f'没获取到 bdstoken 参数，错误代码：{self.network.bdstoken}')

    def handle_list_dir(self) -> None:
        """获取目标目录下的文件和目录列表"""
        self.dir_list_all = self._network_call(
            self.network.get_dir_list, f'/{self.folder_name}'
        )
        if self.dir_list_all == UNKNOWN_RESULT:
            raise RetryableTaskError(f'无法确认目录 {self.folder_name} 的内容')
        self.check_condition(not isinstance(self.dir_list_all, list) or not self.dir_list_all,
                             message=f'目录 {self.folder_name} 中没获取到任何内容，请求返回：{self.dir_list_all}')

    def handle_create_dir(self, folder_name: str, require_empty: bool = False) -> bool:
        """确保目录存在；指定目录必须为空且尚未被本轮任务占用。"""
        if require_empty and folder_name in self.used_custom_dirs:
            return False
        if not require_empty and folder_name in self.created_dirs:
            return True

        if require_empty:
            # A successful create response does not prove the path was new.
            # Re-list it before transferring so an existing non-empty path is
            # never mistaken for the current task's empty directory.
            return_code = self._network_call(self.network.create_dir, folder_name)
            if return_code == 0:
                self.created_dirs.add(folder_name)
            elif return_code == UNKNOWN_RESULT:
                raise RetryableTaskError(f'创建目录结果未知：{folder_name}')
            elif return_code not in (-8, 4):
                raise PermanentTaskError(f'创建目录失败，错误代码：{return_code}')

        result = self._network_call(self.network.get_dir_list, f'/{folder_name}')
        if result == UNKNOWN_RESULT:
            raise RetryableTaskError(f'无法确认目录状态：{folder_name}')
        if require_empty:
            if not isinstance(result, list):
                raise RetryableTaskError(f'无法检查目录 {folder_name}，请求返回：{result}')
            return not result

        # Only a not-found response permits creating the requested directory.
        if folder_name and isinstance(result, int):
            if result not in (-9, 2):
                raise PermanentTaskError(f'无法检查目录 {folder_name}，错误代码：{result}')
            return_code = self._network_call(self.network.create_dir, folder_name)
            if return_code == UNKNOWN_RESULT:
                raise RetryableTaskError(f'创建目录结果未知：{folder_name}')
            if return_code not in (0, -8, 4):
                raise PermanentTaskError(f'创建目录失败，错误代码：{return_code}')
        elif not isinstance(result, list):
            raise RetryableTaskError(f'无法检查目录 {folder_name}，请求返回：{result}')
        if not require_empty:
            # Keep one snapshot for lazy verification of an ambiguous transfer.
            # It avoids an extra list request for every normal-mode transfer.
            self.destination_baselines[folder_name] = list(result) if isinstance(result, list) else []
        self.created_dirs.add(folder_name)
        return True

    def handle_process_save(self) -> None:
        """执行批量转存"""
        while self.next_task_index < len(self.link_list):
            self._check_cancelled()
            task_index = self.next_task_index
            self.schedule_prefetch(task_index + 1)
            url_code = self.link_list[task_index]
            raw_link = self.link_list_org[task_index]
            if self.process_save(url_code, raw_link) is False:
                self.resume_available = True
                self.change_status('interrupted')
                return
            # Commit the checkpoint before updating UI, which can also fail.
            self.next_task_index = task_index + 1
            self.completed_task_count = self.next_task_index
            self.change_status('progress')

    def handle_process_share(self) -> None:
        """执行批量分享"""
        for info in self.dir_list_all:
            self._check_cancelled()
            self.process_share(info)

    def process_save(self, url_code: str, raw_link: str = '') -> bool:
        """执行转存操作并记录结果"""
        try:
            # 跳过非网盘链接
            if not parse_url_and_code(url_code)[0]:
                self.insert_logs(f'不支持的链接：{url_code}')
                return True
            else:
                # 执行转存过程，通过简单的循环判断是否要暂停
                return self.pause_detection(url_code, raw_link)
        except CancellationRequested:
            raise
        except RetryableTaskError as e:
            self.insert_logs(f'当前任务暂未完成，保留断点：{e}：{url_code}')
            return False
        except PermanentTaskError as e:
            if self.next_task_index in self.unknown_transfer_tasks:
                self.insert_logs(f'无法确认先前的转存结果，保留断点：{e}：{url_code}')
                return False
            self.insert_logs(f'转存失败：{e}：{url_code}')
            return True
        except Exception as e:
            # Unexpected item-level errors are isolated so one malformed row
            # cannot abort the remaining batch.
            self.insert_logs(f'转存时发生未预料错误：{e}：{url_code}')
            return self.next_task_index not in self.unknown_transfer_tasks

    def process_share(self, info: Dict[str, Any]) -> None:
        """执行分享操作并记录结果"""
        try:
            # 插入要分享的文件或文件夹到链接输入框，对文件夹加入 "/" 标记来区别
            is_dir = "/" if info["isdir"] == 1 else ""
            filename = f"{info['server_filename']}{is_dir}"
            msg = f'目录：{filename}' if is_dir else f'文件：{filename}'
            self.insert_logs(msg, alt=True)

            password = self.password if not self.random_password else generate_code()
            r = self._network_call(
                self.network.create_share,
                info['fs_id'], EXP_MAP[self.expiry], password,
            )
            if r == UNKNOWN_RESULT:
                result = f'分享结果未知：{msg}'
            elif isinstance(r, str):
                share_link = f'{r}?pwd={password}'
                self.share_results.append(share_link)
                result = f'分享成功：{share_link} {msg}'
            else:
                result = f'分享失败：错误代码（{r}） {msg}'
            self.insert_logs(result)
        except Exception as e:
            self.insert_logs(f'分享时发生未预料错误：{e}：{info}')
        finally:
            self.change_status('update')

    def change_status(self, status: str) -> None:
        """更新内部运行状态，并把 Tk 控件更新排入 UI thread。"""
        if status == 'running':
            self.running = True
        elif status in ('paused', 'interrupted', 'stopped', 'error'):
            self.running = False
        elif status == 'update':
            self.completed_task_count += 1
            status = 'progress'
        self._ui_call(self._apply_status, status)

    # noinspection PyArgumentList
    def _apply_status(self, status: str) -> None:
        """运行状态变化更新函数"""
        if status == 'init':
            self.root.label_status.config(text='准备就绪！', font=('', 10), bootstyle='primary', cursor="arrow")
            self.root.label_status.unbind("<Button-1>")
            self.root.text_logs.config(fg=COLOR_MAP['text'])
            self.root.text_logs.delete(1.0, 'end')
            self.root.bottom_share.config(state="disabled")
            self.root.bottom_save.config(state="disabled")
        elif status == 'resuming':
            self.root.label_status.config(text=f'正在恢复：{self.completed_task_count}/{self.total_task_count}', bootstyle='warning')
            self.root.bottom_share.config(state="disabled")
            self.root.bottom_save.config(state="disabled")
        elif status == 'running':
            self.root.bottom_share.config(state="disabled")
            self.root.bottom_save.config(text='点击暂停', state="normal", bootstyle='danger', command=lambda: self.change_status('paused'))
        elif status == 'paused':
            self.root.bottom_save.config(text='点击继续', bootstyle='success', command=lambda: self.change_status('running'))
        elif status == 'update':
            self.change_status('progress')
        elif status == 'progress':
            self.root.label_status.config(text=f'总进度：{self.completed_task_count}/{self.total_task_count}', bootstyle='success')
        elif status == 'interrupted':
            self.root.label_status.config(text=f'程序中断，可继续转存：{self.completed_task_count}/{self.total_task_count}', bootstyle='warning')
            self.root.bottom_save.config(text='继续转存', state="normal", bootstyle='warning', command=lambda: self.start_save(True))
            self.root.bottom_share.config(state="disabled")
        elif status == 'sharing':
            self.root.text_links.delete(1.0, 'end')
            self.root.text_links.is_placeholder = False
            self.root.text_links.config(fg=COLOR_MAP['text'])
            self.root.bottom_share.config(state="disabled")
            self.root.bottom_save.config(state="disabled")
        elif status == 'error':
            self.root.label_status.config(text='发生错误：', bootstyle='danger')
        else:
            self.root.bottom_save.config(
                text='批量转存', state="normal", bootstyle='primary',
                command=self.start_save,
            )
            self.root.bottom_share.config(state="normal", command=self.start_share)

        self.root.button_copy_share.config(
            state='normal' if status == 'stopped' and self.share_results else 'disabled',
        )

    def check_condition(self, condition: bool, message: str) -> None:
        """
        用户输入或函数返回检查。
        如果条件 condition 为 True，表示错误不可恢复，中断当前任务流程。
        单个链接处理出错，应该直接调用 insert_logs 函数记录错误，不中断任务。

        :param condition: 条件表达式
        :param message: 插入到日志框的内容
        """
        if condition:
            self.change_status('error')
            self.insert_logs(message)
            sys.exit()

    def insert_logs(self, message: str, alt: bool = False) -> None:
        """
        在文本框末尾插入内容

        :param message: 插入到日志框的内容
        :param alt: 如果为 True，插入到链接输入框，用于批量分享时记录文件名
        """
        def append_log():
            text_box = self.root.text_links if alt else self.root.text_logs
            text_box.insert('end', f'{message}\n')
            text_box.see('end')

        self._ui_call(append_log)

    def pause_detection(self, url_code: str, raw_link: str = '') -> bool:
        """循环检测暂停逻辑，每个转存任务开始时都会检测"""
        # 只要 self.running 状态没变，会一直等待。而状态变化由用户点击按钮控制
        while not self.running:
            if self.cancel_event.wait(DELAY_SECONDS):
                raise CancellationRequested()

        self._check_cancelled()
        # 执行验证链接和转存文件
        return self.verify_and_save(url_code, raw_link)

    def verify_and_save(self, url_code: str, raw_link: str = '') -> bool:
        """验证链接和转存文件"""
        task_key = self.next_task_index
        state = self.unknown_transfer_states.get(task_key)
        if not self.check_mode and state is not None and self.verify_unknown_transfer(task_key):
            self.pending_custom_dirs.pop(task_key, None)
            if not self.custom_mode:
                self.saved_links.add(url_code)
            self.insert_logs(
                f'{ERROR_CODES[0]}：{state.url_code} 转存到：{state.folder_name}'
            )
            return True
        if not self.check_mode and not self.custom_mode and url_code in self.saved_links:
            return self.save_file(4, url_code, self.folder_name, raw_link)
        # 验证链接是否有效，返回的数字为错误代码，反之返回参数列表
        result = self.get_cached_transfer_params(url_code)
        # 如果开启检查模式，插入检查结果，然后结束
        if self.check_mode:
            self.check_only(result, url_code)
            return True
        else:
            return self.save_file(result, url_code, self.folder_name, raw_link)

    def get_cached_transfer_params(self, url_code: str) -> Union[List[Any], int]:
        """按分享 URL 和提取码缓存验证结果，减少重复页面请求。"""
        url, password = parse_url_and_code(url_code)
        if not url:
            return -1
        cache_key = (url, password)
        prefetch = self.prefetch_states.get(cache_key)
        if prefetch is not None:
            while not prefetch.event.wait(DELAY_SECONDS):
                self._check_cancelled()
            self._check_cancelled()
            with self._prefetch_lock:
                self.prefetch_states.pop(cache_key, None)
            if prefetch.cookie_generation != self.cookie_generation:
                # A password verification changed BDCLND while this page was
                # being fetched. Retry it with the current main Session.
                return self.get_cached_transfer_params(url_code)
            if prefetch.error is not None:
                raise RetryableTaskError(
                    f'获取分享页面请求失败：{prefetch.error}'
                ) from prefetch.error
            if not is_valid_transfer_params(prefetch.result):
                return prefetch.result
            self.transfer_params_cache[cache_key] = CachedShare(prefetch.result)
        if cache_key not in self.transfer_params_cache:
            result = self.verify_link(url, password)
            if not is_valid_transfer_params(result):
                return result
            bdclnd = None
            if password:
                bdclnd = next((
                    field.strip().partition('=')[2]
                    for field in self.network.headers.get('Cookie', '').split(';')
                    if field.strip().partition('=')[0] == 'BDCLND'
                ), None)
            self.transfer_params_cache[cache_key] = CachedShare(result, bdclnd)
        cached = self.transfer_params_cache[cache_key]
        if cached.bdclnd is not None:
            cookie = self.network.headers['Cookie']
            restored = update_cookie(cached.bdclnd, cookie)
            if restored != cookie:
                self.network.headers['Cookie'] = restored
                self.cookie_generation += 1
        return cached.params

    def schedule_prefetch(self, start_index: int) -> None:
        """后台预取后续无提取码链接，主 worker 仍按输入顺序转存。"""
        if not self.network.bdstoken or self.cancel_event.is_set():
            return

        pending_count = sum(
            not state.event.is_set() for state in self.prefetch_states.values()
        )
        if pending_count >= self.PREFETCH_WORKERS:
            return

        scan_end = min(len(self.link_list), start_index + self.PREFETCH_WORKERS * 2)
        for index in range(start_index, scan_end):
            if pending_count >= self.PREFETCH_WORKERS:
                break
            url, password = parse_url_and_code(self.link_list[index])
            if password or not url:
                continue
            cache_key = (url, password)
            if cache_key in self.transfer_params_cache or cache_key in self.prefetch_states:
                continue
            state = PrefetchState(
                event=threading.Event(),
                cookie_generation=self.cookie_generation,
            )
            with self._prefetch_lock:
                if self.cancel_event.is_set():
                    return
                self.prefetch_states[cache_key] = state
            thread_it(self._prefetch_transfer_params, url, state)
            pending_count += 1

    def _prefetch_transfer_params(self, url: str, state: PrefetchState) -> None:
        """用独立 Session 预取无提取码分享页，避免并发操作主 Session。"""
        network = None
        try:
            with self._prefetch_lock:
                if state.cancel_event.is_set() or self.cancel_event.is_set():
                    raise CancellationRequested()
                network = Network()
                state.network = network
                network.headers = self.network.headers.copy()
                network.bdstoken = self.network.bdstoken
                network.s.trust_env = self.network.s.trust_env
            response = network.get_transfer_params(
                url,
                on_retry=self._retry_log,
                cancel_event=state.cancel_event,
            )
            if state.cancel_event.is_set():
                raise CancellationRequested()
            state.result = self._share_page_params(response)
        except Exception as error:
            state.error = error
        finally:
            try:
                if network is not None:
                    network.s.close()
            finally:
                state.event.set()

    def verify_link(self, url: str, password: str) -> Union[List[Any], int]:
        """验证链接有效性，验证通过返回转存所需参数列表"""
        if not is_baidu_url(url):
            return -1
        # 对于有提取码的链接先验证提取码，试图获取更新 bdclnd
        if password:
            try:
                bdclnd = self._network_call(
                    self.network.verify_pass_code, url, password
                )
            except CancellationRequested:
                raise
            except Exception as error:
                raise RetryableTaskError(f'验证提取码请求失败：{error}') from error
            # 如果 bdclnd 是错误代码，直接返回
            if isinstance(bdclnd, int):
                return bdclnd
            if not isinstance(bdclnd, str) or not bdclnd:
                return -1

            # 更新 bdclnd 到 cookie
            self.network.headers['Cookie'] = update_cookie(bdclnd, self.network.headers['Cookie'])
            self.cookie_generation += 1

        # 直接访问没有提取码的链接，或更新 bdclnd 后再次访问，获取转存必须的 3 个参数
        try:
            response = self._network_call(self.network.get_transfer_params, url)
        except CancellationRequested:
            raise
        except Exception as error:
            raise RetryableTaskError(f'获取分享页面请求失败：{error}') from error
        # 这里不考虑网络异常了，假设请求一定会返回页面内容，对其进行解析
        return self._share_page_params(response)

    @staticmethod
    def _share_page_params(response: str) -> Union[List[Any], int]:
        if isinstance(response, ParsedTransferPage):
            return response.parsed
        return parse_response(response)

    def check_only(self, result: Union[List[Any], int], url_code: str) -> None:
        """开启检查模式时，只管判断返回值类型，并输出结果到日志"""
        if is_valid_transfer_params(result):
            file_type = "目录" if result[4] == ["1"] else "文件"
            self.insert_logs(f'链接有效：{url_code} {file_type}：{result[3]}')
        else:
            error_code = result if isinstance(result, int) else -1
            self.insert_logs(f'链接无效：{url_code} 原因：{ERROR_CODES.get(error_code, f"错误代码（{error_code}）")}')

    def creat_user_dir(self, folder_name: str, raw_link: str, directory_number: int) -> str:
        """按当前链接创建空目录，不依赖输入行数或其他行的标题。"""
        if not folder_name:
            raise ValueError('必须输入转存目录')
        # Explicit directory prefixes are preserved; bare URLs use a stable
        # number assigned to the current task.
        custom_folder = self._explicit_directory_name(raw_link)
        numeric_name = not custom_folder
        custom_folder = str(directory_number) if numeric_name else custom_folder
        folder_name = f'{folder_name}/{custom_folder}'
        # 此处用替换处理目标目录名非法字符，不报错了
        folder_name = folder_name.translate(str.maketrans({char: '_' for char in INVALID_CHARS}))

        base_path = folder_name
        for offset in range(SAVE_LIMIT):
            if offset:
                folder_name = (
                    f'{base_path.rsplit("/", 1)[0]}/{directory_number}_{offset + 1}'
                    if numeric_name else f'{base_path}_{offset + 1}'
                )
            task_key = self.next_task_index
            self.pending_custom_dirs[task_key] = folder_name
            try:
                directory_available = self.handle_create_dir(folder_name, require_empty=True)
            except PermanentTaskError:
                self.pending_custom_dirs.pop(task_key, None)
                raise
            if directory_available:
                self.used_custom_dirs.add(folder_name)
                return folder_name
        self.pending_custom_dirs.pop(self.next_task_index, None)
        raise RuntimeError(f'无法为当前链接创建空目录：{base_path}')

    def save_file(
        self,
        result: Union[List[Any], int],
        url_code: str,
        folder_name: str,
        raw_link: str = '',
    ) -> bool:
        """转存文件。返回结果为列表时，执行转存文件，否则跳过转存。"""
        file_info = ""
        if is_valid_transfer_params(result):
            filenames = ', '.join(result[3])
            file_info = f'{"目录" if result[4] == ["1"] else "文件"}：{filenames}'
            if self.custom_mode:
                transfer_params = result
                task_key = self.next_task_index
                directory_number = self.custom_directory_numbers.get(task_key)
                if directory_number is None:
                    directory_number = self._assign_custom_directory_number(
                        task_key, raw_link or url_code,
                    )
                pending_folder = self.pending_custom_dirs.get(task_key)
                if pending_folder and pending_folder not in self.used_custom_dirs:
                    # A pending path may have been recorded before creation
                    # was confirmed. Never transfer until it is ready.
                    try:
                        directory_available = self.handle_create_dir(
                            pending_folder, require_empty=True,
                        )
                    except PermanentTaskError:
                        self.pending_custom_dirs.pop(task_key, None)
                        raise
                    if directory_available:
                        self.used_custom_dirs.add(pending_folder)
                    else:
                        self.pending_custom_dirs.pop(task_key, None)
                        pending_folder = None
                if pending_folder:
                    folder_name = pending_folder
                else:
                    folder_name = self.creat_user_dir(
                        folder_name, raw_link or url_code, directory_number
                    )
                    self.pending_custom_dirs[task_key] = folder_name
                attempted_paths = self.custom_transfer_paths.setdefault(task_key, set())
                while True:
                    if folder_name not in attempted_paths and len(attempted_paths) >= self.CUSTOM_TRANSFER_ATTEMPTS:
                        result = 4
                        break
                    attempted_paths.add(folder_name)
                    result = self.transfer_with_logs(transfer_params, folder_name, url_code)
                    if result not in (-8, 4) or len(attempted_paths) >= self.CUSTOM_TRANSFER_ATTEMPTS:
                        break
                    # Another client can write between the empty check and
                    # the transfer. Retry only definite collisions, never an
                    # ambiguous write, and keep the task's stable number.
                    self.insert_logs(
                        f'目录存在同名文件，改用新的空目录重试：{folder_name}'
                        f'（第 {len(attempted_paths) + 1}/{self.CUSTOM_TRANSFER_ATTEMPTS} 个目录）'
                    )
                    folder_name = self.creat_user_dir(
                        self.folder_name, raw_link or url_code, directory_number
                    )
                    self.pending_custom_dirs[task_key] = folder_name
                if result == UNKNOWN_RESULT:
                    self.insert_logs(
                        f'转存结果未知，保留当前目录，不自动更换目录：{url_code} 转存到：{folder_name}'
                    )
                    return False
                self.pending_custom_dirs.pop(task_key, None)
                file_info = f'{file_info} 转存到：{folder_name}'
            elif url_code in self.saved_links:
                result = 4
            else:
                result = self.transfer_with_logs(result, folder_name, url_code)
                if result == UNKNOWN_RESULT:
                    self.insert_logs(f'转存结果未知，保留当前目录：{url_code}')
                    return False
                if result == 0:
                    self.saved_links.add(url_code)
        else:
            if self.next_task_index in self.unknown_transfer_tasks:
                self.insert_logs(f'无法确认先前的转存结果，保留断点：{url_code}')
                return False
            result = result if isinstance(result, int) else -1

        if not isinstance(result, int):
            result = -1

        # 最后插入转存结果到日志框
        self.insert_logs(f'{ERROR_CODES.get(result, f"转存失败，错误代码（{result}）")}：{url_code} {file_info}')
        return True

    def transfer_with_logs(self, params: List[Any], folder_name: str, url_code: str) -> Union[int, str]:
        """记录每次重试；重试期间使用相同的链接与目录。"""
        task_key = self.next_task_index
        baseline_known = True
        if task_key in self.unknown_transfer_states:
            if self.verify_unknown_transfer(task_key):
                return 0
            original = self.unknown_transfer_states[task_key].params
            if not self._same_transfer_source(original, params):
                self.insert_logs(
                    f'分享内容已变化或 metadata 不完整，保留原任务和目录：{folder_name}'
                )
                return UNKNOWN_RESULT
        elif not self.custom_mode and self._expected_transfer_items(params) is not None:
            # The batch-start snapshot may predate writes from another client.
            # Custom paths already have a confirmed empty baseline at creation.
            try:
                baseline = self._network_call(
                    self.network.get_dir_list, f'/{folder_name}',
                )
            except RetryableTaskError:
                baseline = None
            baseline_known = is_valid_directory_list(baseline)
            if baseline_known:
                self.destination_baselines[folder_name] = list(baseline)

        def on_retry(attempt, reason):
            self.insert_logs(f'转存失败（{reason}），正在第 {attempt} 次尝试：{url_code}')

        try:
            result = self._call_network_method(
                self.network.transfer_file, params, folder_name, on_retry=on_retry,
            )
        except RetryExhaustedError as error:
            # Explicit errno responses do not imply that a write was applied.
            # Preserve a previous ambiguity, but do not invent a new one.
            raise RetryableTaskError(f'转存请求失败：{error}') from error
        except requests.RequestException:
            result = UNKNOWN_RESULT
        if result == UNKNOWN_RESULT:
            self._remember_unknown_transfer(
                task_key, params, folder_name, url_code, baseline_known=baseline_known,
            )
            if self.verify_unknown_transfer(task_key):
                return 0
        elif result == 0:
            self.unknown_transfer_tasks.discard(task_key)
            self.unknown_transfer_states.pop(task_key, None)
            self._remember_transfer_items(folder_name, params)
        elif task_key in self.unknown_transfer_tasks:
            # A failed new attempt cannot disprove an earlier ambiguous write.
            return UNKNOWN_RESULT
        return result

    def _remember_unknown_transfer(
        self,
        task_key: int,
        params: List[Any],
        folder_name: str,
        url_code: str,
        baseline_known: bool = True,
    ) -> None:
        self.unknown_transfer_tasks.add(task_key)
        self.unknown_transfer_states.setdefault(
            task_key,
            UnknownTransferState(
                params=deepcopy(params),
                folder_name=folder_name,
                url_code=url_code,
                baseline_items=deepcopy(self.destination_baselines.get(folder_name, [])),
                baseline_known=baseline_known,
            ),
        )

    @staticmethod
    def _same_transfer_source(original: List[Any], current: List[Any]) -> bool:
        """A new request must still refer to every item in the unknown write."""
        if original[:2] != current[:2]:
            return False
        if Counter(zip(*original[2:])) != Counter(zip(*current[2:])):
            return False
        original_items = Operations._source_transfer_items(original)
        current_items = dict(zip(current[2], Operations._source_transfer_items(current)))
        for file_id, item in zip(original[2], original_items):
            refreshed = current_items[file_id]
            for field in ('size', 'md5'):
                if item.get(field) is None:
                    continue
                if refreshed.get(field) is None:
                    return False
                before, after = str(item[field]), str(refreshed[field])
                if field == 'md5':
                    before, after = before.lower(), after.lower()
                if before != after:
                    return False
        return True

    @staticmethod
    def _transfer_item_key(item: Dict[str, Any]) -> Tuple[str, str]:
        """Normalize a directory-list item for conservative comparison."""
        return (
            str(item.get('server_filename', '')),
            str(item.get('isdir', '0')),
        )

    @staticmethod
    def _source_transfer_items(params: List[Any]) -> List[Dict[str, Any]]:
        metadata = getattr(params, 'item_metadata', [])
        if not isinstance(metadata, list) or len(metadata) != len(params[2]):
            metadata = [{} for _ in params[2]]
        return [
            {**(extra if isinstance(extra, dict) else {}), 'server_filename': filename, 'isdir': isdir}
            for filename, isdir, extra in zip(params[3], params[4], metadata)
        ]

    @classmethod
    def _transfer_item_signature(cls, item: Dict[str, Any]) -> Optional[Tuple]:
        """Names alone, including top-level directories, cannot prove completion."""
        name, isdir = cls._transfer_item_key(item)
        size = item.get('size')
        md5 = item.get('md5')
        if (
            not isinstance(item.get('server_filename'), str) or not name
            or 'isdir' not in item or isdir != '0'
            or isinstance(size, bool) or not isinstance(size, (int, str))
            or len(str(size)) > 20 or not str(size).isascii() or not str(size).isdigit()
            or not isinstance(md5, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', md5)
        ):
            return None
        return name, isdir, int(size), md5.lower()

    def _expected_transfer_items(self, params: List[Any]) -> Optional[Counter]:
        signatures = [
            self._transfer_item_signature(item)
            for item in self._source_transfer_items(params)
        ]
        if not signatures or any(signature is None for signature in signatures):
            return None
        return Counter(signatures)

    def _remember_transfer_items(self, folder_name: str, params: List[Any]) -> None:
        """Update the baseline after a confirmed transfer without another list call."""
        baseline = self.destination_baselines.setdefault(folder_name, [])
        baseline.extend(self._source_transfer_items(params))

    def verify_unknown_transfer(self, task_key: int) -> bool:
        """Check whether an ambiguous transfer is already visible in its target."""
        state = self.unknown_transfer_states.get(task_key)
        if (
            state is None or not state.baseline_known
            or not is_valid_directory_list(state.baseline_items)
        ):
            return False
        expected_counts = self._expected_transfer_items(state.params)
        if expected_counts is None:
            return False

        try:
            current = self._network_call(
                self.network.get_dir_list,
                f'/{state.folder_name}',
            )
        except (RetryableTaskError, PermanentTaskError):
            return False
        if not is_valid_directory_list(current):
            return False

        baseline_counts = Counter(
            self._transfer_item_key(item)
            for item in state.baseline_items
            if isinstance(item, dict)
        )
        baseline_ids = {
            str(item['fs_id']) for item in state.baseline_items
            if isinstance(item, dict) and item.get('fs_id') is not None
        }
        new_counts = Counter()
        seen_ids = set()
        for item in current:
            if not isinstance(item, dict):
                continue
            fs_id = item.get('fs_id')
            if (
                isinstance(fs_id, bool) or not isinstance(fs_id, (int, str))
                or not str(fs_id).isascii() or not str(fs_id).isdigit()
            ):
                continue
            signature = self._transfer_item_signature(item)
            identity = str(fs_id)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            if str(fs_id) in baseline_ids:
                baseline_counts[self._transfer_item_key(item)] -= 1
                continue
            if signature is not None:
                new_counts[signature] += 1

        # Baseline entries without a target fs_id include confirmed earlier
        # transfers. Reserve their name counts rather than reusing them as proof.
        confirmed = all(
            new_counts[signature] >= count + max(baseline_counts[signature[:2]], 0)
            for signature, count in expected_counts.items()
        )
        if not confirmed:
            return False

        self.unknown_transfer_tasks.discard(task_key)
        self.unknown_transfer_states.pop(task_key, None)
        self.destination_baselines[state.folder_name] = list(current)
        self.insert_logs(
            f'已自动核对到转存结果：{state.url_code} 转存到：{state.folder_name}'
        )
        return True
