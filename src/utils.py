"""
通用的工具函数，主要是一些静态函数。

:author: assassing
:contact: https://github.com/hxz393
:copyright: Copyright 2024, hxz393. 保留所有权利。
"""
import atexit
import base64
import html
import json
import os
import random
import re
import string
import tempfile
import threading
import zlib
from typing import Any, Union, Tuple, Callable, List, Optional, Dict
from urllib.parse import parse_qs, urlsplit

from src.constants import CONFIG_PATH, ICON_BASE64

# 预编译正则表达式
SHARE_ID_REGEX = re.compile(r'"shareid"\s*:\s*"?(\d+)"?\s*[,}]')
USER_ID_REGEX = re.compile(r'"share_uk"\s*:\s*"?(\d+)"?\s*[,}]')
FS_ID_REGEX = re.compile(r'"fs_id"\s*:\s*"?(\d+)"?\s*[,}]')
SERVER_FILENAME_REGEX = re.compile(r'"server_filename"\s*:\s*("(?:[^"\\]|\\.)*")\s*[,}]')
ISDIR_REGEX = re.compile(r'"isdir"\s*:\s*([01])\s*[,}]')
PASS_CODE_LABEL_REGEX = re.compile(r'提取(?:码(?=\s*[\[（(【A-Za-z0-9])|码?\s*[:：])')
SHARE_URL_NOTE_REGEX = re.compile(
    rf'[，。；]|{PASS_CODE_LABEL_REGEX.pattern}|解压(?:密码|码)'
)


class TransferParams(list):
    """Backward-compatible transfer params with optional source metadata."""

    def __init__(
        self,
        values: List[Any],
        item_metadata: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__(values)
        self.item_metadata = item_metadata or []


def thread_it(func: Callable, *args: Tuple[Any, ...]) -> threading.Thread:
    """
    多线程防止转存时主界面卡死。

    :param func: 要调用的函数
    :param args: 函数参数
    :return: 新建的 worker thread
    """
    # The window close handler can request cancellation and wait for this
    # thread. Daemon mode is the final safeguard if a third-party request
    # library does not return promptly.
    t = threading.Thread(target=func, args=args, daemon=True)
    t.start()
    return t


def write_config(config: str) -> None:
    """
    写入配置文件，点击批量转存或分享按钮时才运行。

    :param config: 配置文件内容，以换行转义字符 '\n' 拼接多个配置到一行字符串
    :return: 无返回值
    """
    with open(CONFIG_PATH, 'w') as f:
        f.write(config)


def read_config() -> Optional[List[str]]:
    """
    读取配置文件，在 UI 初始化完毕后执行。

    :return: 读取成功时返回配置列表，一个元素一个配置。配置文件不存在时返回 None，什么也不会发生
    """
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = f.read().splitlines()
            return config


def create_icon() -> str:
    """
    从 base64 编码中生成临时图标，在程序结束时自动删除。
    好处是打包时不用导入资源文件。

    :return: 返回图标文件路径
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_file:
        temp_file.write(zlib.decompress(base64.b64decode(ICON_BASE64)))
        temp_file.flush()
        ico_path = temp_file.name

    ico_path = temp_file.name
    # 显式声明程序退出时，删除临时图标文件，避免在个别系统平台自动删除失败
    atexit.register(os.remove, ico_path)
    return ico_path


def normalize_link(url_code: str) -> str:
    """
    预处理链接至标准格式。

    :param url_code: 需要处理的的原始链接格式
    :return: 返回标准格式：链接+空格+提取码
    """
    # Resolve labeled credentials before dropping notes or labels.
    url, code = parse_url_and_code(url_code)
    if url:
        trailing_space = ' ' if code and url_code[-1:].isspace() else ''
        return f'{url} {code}{trailing_space}'
    return re.sub(r'\s+', ' ', url_code)


def is_baidu_url(url: str) -> bool:
    """Allow credentials only on the HTTPS Baidu Pan origin."""
    if not isinstance(url, str) or re.search(r'[\s\\\x00-\x1f\x7f]', url):
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == 'https'
            and parts.hostname == 'pan.baidu.com'
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def parse_url_and_code(url_code: str) -> Tuple[str, str]:
    """
    以空格分割出 URL 和提取码。

    :param url_code: 输入的标准链接格式
    :return: 链接和提取码
    """
    if not isinstance(url_code, str):
        return '', ''

    # The URL can be preceded by a title. Keep the first URL and parse its
    # query before looking at free-form text after it.
    url_match = re.search(r'https?://\S+', url_code, re.IGNORECASE)
    if not url_match:
        return '', ''

    url_token = url_match.group(0).rstrip('.,，。;；)]）')
    # Validate the original authority before separating pasted notes; a label
    # inside an untrusted hostname must never turn it into an allowed origin.
    if not is_baidu_url(re.sub(r'^http://', 'https://', url_token, flags=re.IGNORECASE)):
        return '', ''
    note_match = SHARE_URL_NOTE_REGEX.search(url_token)
    if note_match:
        url_token = url_token[:note_match.start()].rstrip('.,，。;；)]）')
    url_end = url_match.start() + len(url_token)
    url_token = re.sub(r'^http://', 'https://', url_token, flags=re.IGNORECASE)
    url_token = re.sub(r'%3f', '?', url_token, flags=re.IGNORECASE)
    url_token = re.sub(r'%26', '&', url_token, flags=re.IGNORECASE)
    parts = urlsplit(url_token)
    query = parse_qs(parts.query.replace('?pwd=', '&pwd='), keep_blank_values=True)
    query = {key.lower(): value for key, value in query.items()}
    path = parts.path
    if path == '/share/init':
        surl = query.get('surl', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]+', surl):
            return '', ''
        path = f'/s/1{surl}'

    tail = url_code[url_end:]
    labeled_code = PASS_CODE_LABEL_REGEX.search(tail)
    if 'pwd' in query:
        candidate = query['pwd'][0]
    elif labeled_code:
        code_match = re.match(
            r'\s*(?:[\[（(【]\s*)?([A-Za-z0-9]+)', tail[labeled_code.end():],
        )
        candidate = code_match.group(1) if code_match else ''
    else:
        # A bare code must immediately follow the URL, not an archive-password note.
        code_match = re.search(
            r'^[\s:：,，;；(（\[]*([A-Za-z0-9]{4})(?![A-Za-z0-9])',
            tail,
        )
        candidate = code_match.group(1) if code_match else ''
    code = candidate if re.fullmatch(r'[A-Za-z0-9]{4}', candidate) else ''

    # Keep the historical URL length guard for malformed overlong input.
    url = f'https://pan.baidu.com{path}'[:47]
    return url, code


def parse_response(response: str) -> Union[List[Any], int]:
    """
    验证提取码通过后，再次访问网盘地址，此函数解析返回的页面源码并提取所需要参数。
    shareid_list 和 user_id_list 只有一个值，fs_id_list 需要完整返回

    :param response: 响应内容
    :return: 没有获取到足够参数时返回 -1，否则返回完整转存参数及文件信息
    """
    if not isinstance(response, str):
        return -1

    shareid_list = SHARE_ID_REGEX.findall(response)
    user_id_list = USER_ID_REGEX.findall(response)
    fs_id_list = FS_ID_REGEX.findall(response)
    server_filename_list = SERVER_FILENAME_REGEX.findall(response)
    isdir_list = ISDIR_REGEX.findall(response)
    if (
        not all([shareid_list, user_id_list, fs_id_list, server_filename_list, isdir_list])
        or len(fs_id_list) != len(server_filename_list)
        or len(fs_id_list) != len(isdir_list)
    ):
        return -1

    files = {}
    try:
        for fs_id, filename, isdir in zip(fs_id_list, server_filename_list, isdir_list):
            # A page can embed a file list twice; send each fs_id only once.
            files.setdefault(fs_id, (json.loads(filename), isdir))
    except (ValueError, TypeError):
        return -1

    values = [
        shareid_list[0], user_id_list[0], list(files),
        [info[0] for info in files.values()],
        [info[1] for info in files.values()],
    ]
    if not is_valid_transfer_params(values):
        return -1

    # Metadata must belong to the same JSON record, not an unrelated page field.
    metadata_by_id = {}
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        start = response.find('{', offset)
        if start == -1:
            break
        try:
            value, offset = decoder.raw_decode(response, start)
        except (ValueError, RecursionError):
            offset = start + 1
            continue
        pending = [value]
        while pending:
            record = pending.pop()
            if isinstance(record, list):
                pending.extend(record)
                continue
            if not isinstance(record, dict):
                continue
            pending.extend(
                child for child in record.values() if isinstance(child, (dict, list))
            )
            fs_id = str(record.get('fs_id', ''))
            if fs_id not in files or (
                record.get('server_filename'), str(record.get('isdir'))
            ) != files[fs_id]:
                continue
            metadata = {
                name: record[name] for name in ('size', 'md5') if name in record
            }
            if fs_id not in metadata_by_id:
                metadata_by_id[fs_id] = metadata
            elif metadata_by_id[fs_id] != metadata:
                metadata_by_id[fs_id] = {}
    return TransferParams(
        values,
        [metadata_by_id.get(fs_id, {}) for fs_id in files],
    )


def is_valid_directory_list(items: Any) -> bool:
    """Every snapshot entry needs an ID or a complete name/type identity."""
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        file_id = item.get('fs_id')
        if file_id is not None:
            if (
                isinstance(file_id, bool) or not isinstance(file_id, (int, str))
                or not str(file_id).isascii() or not str(file_id).isdigit()
            ):
                return False
        elif (
            not isinstance(item.get('server_filename'), str) or not item['server_filename']
            or str(item.get('isdir')) not in ('0', '1')
        ):
            return False
    return True


def is_valid_transfer_params(params: Any) -> bool:
    """
    Check that transfer parameters contain one complete entry per shared file.

    :param params: Parsed transfer parameters
    :return: True when the structure is safe for transfer and display
    """
    if not isinstance(params, list) or len(params) != 5:
        return False

    share_id, user_id, fs_id_list, filename_list, isdir_list = params
    return (
        isinstance(share_id, str)
        and isinstance(user_id, str)
        and bool(share_id)
        and bool(user_id)
        and isinstance(fs_id_list, list)
        and isinstance(filename_list, list)
        and isinstance(isdir_list, list)
        and bool(fs_id_list)
        and len(fs_id_list) == len(filename_list) == len(isdir_list)
        and all(isinstance(fs_id, str) and fs_id.isascii() and fs_id.isdigit() for fs_id in fs_id_list)
        and all(isinstance(filename, str) and filename for filename in filename_list)
        and all(isdir in ('0', '1') for isdir in isdir_list)
    )


def is_permanent_share_error(response: str) -> bool:
    """Recognize explicit unavailable-link messages, not script templates."""
    visible = re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>', '', response, flags=re.I | re.S)
    visible = html.unescape(re.sub(r'<[^>]+>', '', visible))
    visible = re.sub(r'\s+', '', visible)
    return any(message in visible for message in (
        '分享已取消', '分享已过期', '分享已失效', '链接已失效', '链接不存在',
        '分享的文件已经被删除', '分享的文件已经被取消',
        '提取码错误', '分享密码错误', '此链接分享内容可能因为涉及',
    ))


def update_cookie(bdclnd: str, cookie: str) -> str:
    """
    更新 cookie 字符串，以包含新的 BDCLND 值。

    :param bdclnd: 新的 BDCLND 值
    :param cookie: 当前的 cookie 字符串
    :return: 返回新 cookie 字符串
    """
    cookies_dict = {}
    for field in cookie.split(';'):
        field = field.strip()
        if not field:
            continue
        key, separator, value = field.partition('=')
        key = key.strip()
        if not separator or not key:
            raise ValueError('Cookie 字段格式不正确')
        cookies_dict[key] = value
    # 在 cookie 字典中，更新或添加 BDCLND 的值
    cookies_dict['BDCLND'] = bdclnd
    # 从更新后的字典重新构建 cookie 字符串
    updated_cookie = ';'.join([f'{key}={value}' for key, value in cookies_dict.items()])

    return updated_cookie


def generate_code() -> str:
    """
    生成一个四位的随机提取码，包含大小写字母和数字。

    :return: 随机提取码
    """
    # 包含大小写字母和数字
    characters = string.ascii_letters + string.digits
    # 随机选择四个字符
    code = ''.join(random.choice(characters) for _ in range(4))

    return code
