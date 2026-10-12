"""
所有和网络请求相关函数。

:author: assassing
:contact: https://github.com/hxz393
:copyright: Copyright 2024, hxz393. 保留所有权利。
"""
import json
import random
import threading
import time
from typing import Union, List, Any, Callable, Optional

import requests

from src.constants import HEADERS, BASE_URL
from src.utils import is_permanent_share_error, is_valid_transfer_params, parse_response


REQUEST_ATTEMPTS = 5
TRANSFER_ATTEMPTS = REQUEST_ATTEMPTS
UNKNOWN_RESULT = '__unknown_result__'
NON_RETRYABLE_CODES = frozenset({
    -1, -4, -6, -7, -8, -9, -10, -12, -62, 2, 4, 12, 20, 105, 404,
})
NON_RETRYABLE_TRANSFER_CODES = NON_RETRYABLE_CODES
RetryCallback = Optional[Callable[[int, Any], None]]


class RequestCancelledError(RuntimeError):
    """A cancelled request must not start another retry."""


class RetryExhaustedError(requests.RequestException):
    """A temporary API errno exhausted its bounded retry budget."""

    def __init__(self, errno: int):
        self.errno = errno
        super().__init__(f'临时 API 错误（{errno}），{REQUEST_ATTEMPTS} 次尝试已耗尽')


class ParsedTransferPage(str):
    """String-compatible share HTML carrying its validated parameters."""

    def __new__(cls, value: str, parsed: Union[List[Any], int]):
        page = str.__new__(cls, value)
        page.parsed = parsed
        return page


class Network:
    """
    网络请求相关类。
    网络异常、无效 response 和临时 API 错误使用有界重试。
    """

    def __init__(self):
        """初始化会话、请求头和 bdstoken。"""
        self.s = requests.Session()
        self.headers = HEADERS.copy()
        self.bdstoken = ''
        # 保留现有兼容行为：程序目前仍关闭 TLS certificate verification。
        requests.packages.urllib3.disable_warnings()

    def reset_session(self) -> None:
        """恢复中断批次时重建 Session，保留 headers 中的运行时 Cookie。"""
        self.s.close()
        self.s = requests.Session()
        self.bdstoken = ''

    @staticmethod
    def _notify_retry(on_retry: RetryCallback, attempt: int, reason: Any) -> None:
        if on_retry:
            on_retry(attempt + 1, reason)

    @staticmethod
    def _sleep_before_retry() -> None:
        time.sleep(random.uniform(1, 2))

    @staticmethod
    def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError()

    def _wait_before_retry(self, cancel_event: Optional[threading.Event]) -> None:
        if cancel_event is None:
            self._sleep_before_retry()
        elif cancel_event.wait(random.uniform(1, 2)):
            raise RequestCancelledError()

    def _request_json(
        self,
        request: Callable[[], Any],
        *,
        on_retry: RetryCallback = None,
        unknown_on_exhaustion: bool = False,
        validator: Optional[Callable[[dict], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[dict, int, str]:
        """
        发送 JSON API 请求并统一处理 retry。

        `unknown_on_exhaustion` 仅供可能产生副作用的 POST 使用：请求已经
        发出但 response 不可确认时，调用方不能把它当成普通失败继续换目录。
        """
        result_unknown = False
        try:
            for attempt in range(1, REQUEST_ATTEMPTS + 1):
                self._check_cancelled(cancel_event)
                try:
                    response = request()
                    raise_for_status = getattr(response, 'raise_for_status', None)
                    if raise_for_status:
                        raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or type(payload.get('errno')) is not int:
                        raise ValueError('API response 缺少有效的 errno')
                    if payload['errno'] == 0 and validator:
                        validator(payload)
                except (requests.RequestException, ValueError, TypeError, KeyError,
                        AttributeError, UnicodeError) as error:
                    result_unknown = unknown_on_exhaustion
                    self._check_cancelled(cancel_event)
                    if attempt == REQUEST_ATTEMPTS:
                        if unknown_on_exhaustion:
                            return UNKNOWN_RESULT
                        raise
                    self._notify_retry(on_retry, attempt, type(error).__name__)
                    self._wait_before_retry(cancel_event)
                    continue

                errno = payload['errno']
                if not unknown_on_exhaustion:
                    self._check_cancelled(cancel_event)
                # A definite write response still counts even if cancellation
                # arrived in flight; do not replay a confirmed success on resume.
                if errno == 0:
                    return payload
                if errno in NON_RETRYABLE_CODES:
                    return UNKNOWN_RESULT if result_unknown else payload
                self._check_cancelled(cancel_event)
                if attempt == REQUEST_ATTEMPTS:
                    if result_unknown:
                        return UNKNOWN_RESULT
                    raise RetryExhaustedError(errno)

                self._notify_retry(on_retry, attempt, errno)
                self._wait_before_retry(cancel_event)
        except RequestCancelledError:
            if result_unknown:
                return UNKNOWN_RESULT
            raise

        raise RuntimeError('API retry loop unexpectedly ended')

    def _request_text(
        self,
        request: Callable[[], Any],
        *,
        on_retry: RetryCallback = None,
        validator: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """发送文本请求并在网络或 response 无效时 retry。"""
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            self._check_cancelled(cancel_event)
            try:
                response = request()
                self._check_cancelled(cancel_event)
                raise_for_status = getattr(response, 'raise_for_status', None)
                if raise_for_status:
                    raise_for_status()
                content = response.content
                if not isinstance(content, (bytes, bytearray)):
                    raise ValueError('API response content 无效')
                text = content.decode('utf-8')
                if not text:
                    raise ValueError('API response 为空')
                if validator:
                    validator(text)
                return text
            except (requests.RequestException, ValueError, TypeError,
                    AttributeError, UnicodeError) as error:
                self._check_cancelled(cancel_event)
                if attempt == REQUEST_ATTEMPTS:
                    raise
                self._notify_retry(on_retry, attempt, type(error).__name__)
                self._wait_before_retry(cancel_event)

        raise RuntimeError('API retry loop unexpectedly ended')

    def get_bdstoken(
        self,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[str, int]:
        """获取 bdstoken。"""
        url = f'{BASE_URL}/api/gettemplatevariable'
        params = {
            'clienttype': '0',
            'app_id': '38824127',
            'web': '1',
            'fields': '["bdstoken","token","uk","isdocuser","servertime"]'
        }

        def validate(payload):
            result = payload.get('result')
            if (
                not isinstance(result, dict)
                or not isinstance(result.get('bdstoken'), str)
                or not result['bdstoken']
            ):
                raise ValueError('bdstoken response 无效')

        payload = self._request_json(
            lambda: self.s.get(
                url=url, params=params, headers=self.headers, timeout=10,
                allow_redirects=False, verify=False,
            ),
            on_retry=on_retry,
            validator=validate,
            cancel_event=cancel_event,
        )
        if payload == UNKNOWN_RESULT:
            return -1
        if isinstance(payload, int):
            return payload
        if payload['errno'] != 0:
            return payload['errno']
        return payload['result']['bdstoken']

    def get_dir_list(
        self,
        folder_name: str,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[List[Any], int]:
        """获取目录的完整列表，自动请求所有 pagination pages。"""
        url = f'{BASE_URL}/api/list'
        page_size = 1000
        all_items = []
        page = 1
        seen_pages = set()
        seen_items = set()

        def item_key(item):
            if isinstance(item, dict) and item.get('fs_id') is not None:
                return ('fs_id', str(item['fs_id']))
            return ('record', json.dumps(item, sort_keys=True))

        def needs_next_page(payload, item_count):
            total = payload.get('total')
            has_more = payload.get('has_more')
            try:
                total_count = int(total) if total is not None else None
            except (TypeError, ValueError):
                total_count = None
            return (
                (total_count is not None and item_count < total_count)
                or has_more in (True, 1, '1')
                or (has_more is None and total is None and len(payload['list']) >= page_size)
            )

        while True:
            params = {
                'order': 'time',
                'desc': '1',
                'showempty': '0',
                'web': '1',
                'page': str(page),
                'num': str(page_size),
                'dir': folder_name,
                'bdstoken': self.bdstoken,
            }

            def validate(payload):
                current = payload.get('list')
                if not isinstance(current, list):
                    raise ValueError('目录列表 response 无效')
                if current:
                    if json.dumps(current, sort_keys=True) in seen_pages:
                        raise ValueError(f'目录 pagination 返回了重复页面：{page}')
                    if (
                        not any(item_key(item) not in seen_items for item in current)
                        and needs_next_page(payload, len(all_items))
                    ):
                        raise ValueError(f'目录 pagination 没有新增内容但仍标记有后续数据：{page}')
                elif needs_next_page(payload, len(all_items)):
                    raise ValueError(f'目录 pagination 返回空页面但仍标记有后续数据：{page}')

            payload = self._request_json(
                lambda params=params: self.s.get(
                    url=url, params=params, headers=self.headers, timeout=15,
                    allow_redirects=False, verify=False,
                ),
                on_retry=on_retry,
                validator=validate,
                cancel_event=cancel_event,
            )
            if payload == UNKNOWN_RESULT:
                return -1
            if isinstance(payload, int):
                return payload

            if payload['errno'] != 0:
                return payload['errno']

            current = payload['list']
            if current:
                seen_pages.add(json.dumps(current, sort_keys=True))
            for item in current:
                key = item_key(item)
                if key not in seen_items:
                    seen_items.add(key)
                    all_items.append(item)
            if needs_next_page(payload, len(all_items)):
                page += 1
                continue
            return all_items

    def create_dir(
        self,
        folder_name: str,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[int, str]:
        """新建指定目录。"""
        url = f'{BASE_URL}/api/create'
        params = {
            'a': 'commit',
            'bdstoken': self.bdstoken,
        }
        data = {
            'path': f'/{folder_name.strip("/")}',
            'isdir': '1',
            'block_list': '[]',
            'rtype': '0',
        }

        def validate(payload):
            created_path = payload.get('path')
            if created_path and created_path.rstrip('/') != data['path'].rstrip('/'):
                raise ValueError(f'创建目录返回的路径与请求不一致：{created_path}')

        payload = self._request_json(
            lambda: self.s.post(
                url=url, params=params, headers=self.headers, data=data, timeout=15,
                allow_redirects=False, verify=False,
            ),
            on_retry=on_retry,
            unknown_on_exhaustion=True,
            validator=validate,
            cancel_event=cancel_event,
        )
        if payload == UNKNOWN_RESULT:
            return UNKNOWN_RESULT
        if isinstance(payload, int):
            return payload
        return payload['errno']

    def verify_pass_code(
        self,
        link_url: str,
        pass_code: str,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[str, int]:
        """验证提取码并返回 bdclnd。"""
        url = f'{BASE_URL}/share/verify'
        params = {
            'surl': link_url[25:48],
            'bdstoken': self.bdstoken,
            't': str(int(round(time.time() * 1000))),
            'channel': 'chunlei',
            'web': '1',
            'clienttype': '0',
        }
        data = {'pwd': pass_code, 'vcode': '', 'vcode_str': ''}

        def validate(payload):
            if not isinstance(payload.get('randsk'), str) or not payload['randsk']:
                raise ValueError('提取码 response 缺少 randsk')

        payload = self._request_json(
            lambda: self.s.post(
                url=url, params=params, headers=self.headers, data=data, timeout=10,
                allow_redirects=False, verify=False,
            ),
            on_retry=on_retry,
            validator=validate,
            cancel_event=cancel_event,
        )
        if payload == UNKNOWN_RESULT:
            return -1
        if isinstance(payload, int):
            return payload
        if payload['errno'] != 0:
            return payload['errno']
        return payload['randsk']

    def get_transfer_params(
        self,
        url: str,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """请求分享页面，获取转存所需的原始 response。"""
        parsed = -1

        def validate(text):
            nonlocal parsed
            parsed = parse_response(text)
            if not is_valid_transfer_params(parsed) and not is_permanent_share_error(text):
                raise ValueError('分享页面缺少有效的转存参数')

        text = self._request_text(
            lambda: self.s.get(url=url, headers=self.headers, timeout=15, verify=False),
            on_retry=on_retry,
            validator=validate,
            cancel_event=cancel_event,
        )
        return ParsedTransferPage(text, parsed)

    def transfer_file(
        self,
        params_list: List[Any],
        folder_name: str,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[int, str]:
        """
        转存百度网盘文件。

        发生 timeout、连接异常或 malformed response 后，最终结果可能已经
        在服务端生效。此时返回 UNKNOWN_RESULT，调用方必须保留原目录。
        """
        if not is_valid_transfer_params(params_list):
            return -1

        url = f'{BASE_URL}/share/transfer'
        params = {
            'shareid': params_list[0],
            'from': params_list[1],
            'bdstoken': self.bdstoken,
            'channel': 'chunlei',
            'web': '1',
            'clienttype': '0',
        }
        data = {
            'fsidlist': f"[{','.join(dict.fromkeys(params_list[2]))}]",
            'path': f'/{folder_name.strip("/")}',
        }
        payload = self._request_json(
            lambda: self.s.post(
                url=url, params=params, headers=self.headers, data=data,
                timeout=30, allow_redirects=False, verify=False,
            ),
            on_retry=on_retry,
            unknown_on_exhaustion=True,
            cancel_event=cancel_event,
        )
        if payload == UNKNOWN_RESULT:
            return UNKNOWN_RESULT
        if isinstance(payload, int):
            return payload
        return payload['errno']

    def create_share(
        self,
        fs_id: int,
        expiry: str,
        password: str,
        on_retry: RetryCallback = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Union[str, int]:
        """生成百度网盘分享链接。"""
        url = f'{BASE_URL}/share/set'
        params = {
            'channel': 'chunlei',
            'bdstoken': self.bdstoken,
            'clienttype': '0',
            'app_id': '250528',
            'web': '1',
        }
        data = {
            'period': expiry,
            'pwd': password,
            'eflag_disable': 'true',
            'channel_list': '[]',
            'schannel': '4',
            'fid_list': f'[{fs_id}]',
        }

        def validate(payload):
            if not isinstance(payload.get('link'), str) or not payload['link']:
                raise ValueError('分享 response 缺少 link')

        payload = self._request_json(
            lambda: self.s.post(
                url=url, params=params, headers=self.headers, data=data, timeout=15,
                allow_redirects=False, verify=False,
            ),
            on_retry=on_retry,
            unknown_on_exhaustion=True,
            validator=validate,
            cancel_event=cancel_event,
        )
        if payload == UNKNOWN_RESULT:
            return UNKNOWN_RESULT
        if isinstance(payload, int):
            return payload
        if payload['errno'] != 0:
            return payload['errno']
        return payload['link']


def _create_dir_once(network: Network, folder_name: str) -> int:
    """Compatibility hook for old callers that explicitly bypassed retry."""
    url = f'{BASE_URL}/api/create'
    params = {'a': 'commit', 'bdstoken': network.bdstoken}
    data = {
        'path': f'/{folder_name.strip("/")}',
        'isdir': '1',
        'block_list': '[]',
        'rtype': '0',
    }
    response = network.s.post(
        url=url, params=params, headers=network.headers, data=data, timeout=15,
        allow_redirects=False, verify=False,
    )
    payload = response.json()
    errno = payload['errno']
    created_path = payload.get('path')
    if errno == 0 and created_path and created_path.rstrip('/') != data['path'].rstrip('/'):
        raise ValueError(f'创建目录返回的路径与请求不一致：{created_path}')
    return errno


# Keep compatibility with callers that used retrying's introspection hook.
Network.create_dir.__wrapped__ = _create_dir_once
