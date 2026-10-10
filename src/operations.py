"""
主要功能实现逻辑，包括批量转存和分享。

:author: assassing
:contact: https://github.com/hxz393
:copyright: Copyright 2024, hxz393. 保留所有权利。
"""

import sys
import threading
import time
import traceback
from typing import Union, List, Dict, Any

from src.constants import EXP_MAP, DELAY_SECONDS, INVALID_CHARS, ERROR_CODES, SAVE_LIMIT, COLOR_MAP
from src.network import Network
from src.ui import CustomDialog
from src.utils import (
    thread_it,
    write_config,
    parse_response,
    normalize_link,
    parse_url_and_code,
    update_cookie,
    generate_code,
    is_valid_transfer_params,
)


class Operations:
    """
    包括主功能实现流程，涉及部分对 UI 的操作。
    日志文字不额外提取到映射字典，方便定位错误。

    :param root: 主窗口
    """

    def __init__(self, root):
        self.root = root
        self.network = Network()
        self.custom_dir_count = 0
        self.created_dirs = set()
        self.used_custom_dirs = set()
        self.saved_links = set()
        self.link_list = []
        self.link_list_org = []
        self.next_task_index = 0
        self.completed_task_count = 0
        self.total_task_count = 0
        self.resume_available = False
        self.running = False
        self._run_lock = threading.Lock()

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
            self.handle_create_dir(folder_name=self.folder_name)
            if resume:
                self.insert_logs(f'继续转存：已处理 {self.next_task_index}/{self.total_task_count}，从下一条未处理输入继续。')
                self.change_status('running')
            self.handle_process_save()
        except SystemExit:
            # check_condition has already logged the input or API error.
            pass
        except Exception as e:
            self.insert_logs(f'程序出现未预料错误，信息如下：\n{e}\n{traceback.format_exc()}')
        finally:
            self.resume_available = can_resume and self.next_task_index < len(self.link_list)
            try:
                try:
                    self.network.s.close()
                finally:
                    self.change_status('interrupted' if self.resume_available else 'stopped')
            finally:
                self._run_lock.release()

    def share(self) -> None:
        """
        分享主函数，流程为：
        0.弹出设置窗口，设置分享参数；
        1.获取用户输入，更新按钮和标签状态；
        2.检查用户输入，有问题则终止并发送日志；
        3.获取 bdstoken，如果获取失败则终止；
        4.获取目标目录文件列表，如果为空则终止；
        5.批量分享文件。先向链接框插入文件名，再创建分享，插入结果到日志框。
        """
        if not self._run_lock.acquire(blocking=False):
            return
        if self.resume_available:
            self._run_lock.release()
            return
        try:
            # 创建对话框实例，如果对话框没有返回输入值，则忽略
            self.dialog_result = CustomDialog(self.root)
            if self.dialog_result.result:
                self.prepare_run()
                self.handle_input(task_count_check=False)
                self.handle_bdstoken()
                self.handle_list_dir()
                self.setup_share()
                self.handle_process_share()
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
        # 从用户输入获取变量
        self.cookie = "".join(self.root.entry_cookie.get().split())
        if resume:
            self.network.reset_session()
        else:
            self.folder_name = "".join(self.root.entry_folder_name.get().split()).strip('/')
            self.custom_mode = self.root.var_custom_mode.get()
            self.check_mode = self.root.var_check_mode.get()
            self.completed_task_count = 0
            self.custom_dir_count = 0
            self.created_dirs = set()
            self.used_custom_dirs = set()
            self.saved_links = set()
            self.link_list = []
            self.link_list_org = []
            self.next_task_index = 0
            self.resume_available = False
        self.network.s.trust_env = self.root.var_trust_env.get()

        # Keep the original batch's paths, modes and logs when resuming.
        self.network.headers['Cookie'] = self.cookie
        self.change_status('resuming' if resume else 'init')
        write_config(f'{self.cookie}\n{self.folder_name}')

    def setup_save(self) -> None:
        """准备链接，更新状态"""
        # 从文本链接控件获取全部链接，清洗并标准化链接。注意链接后拼接一个空格，是为了后面能统一处理带与不带提取码的链接
        raw_links = [
            link for link in self.root.text_links.get(1.0, 'end').splitlines()
            if link.strip()
        ]
        self.link_list = [normalize_link(f'{link} ') for link in raw_links]
        # Keep the raw and normalized rows aligned. Duplicate rows are valid input.
        self.link_list_org = raw_links
        # 更新任务总数和状态
        self.total_task_count = len(self.link_list)
        self.change_status('running')

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
            self.check_condition(self.total_task_count == 0,
                                 message='无有效链接。')
            self.check_condition(self.total_task_count > SAVE_LIMIT,
                                 message=f'批量转存一次不能超过 {SAVE_LIMIT}，当前链接数：{self.total_task_count}')

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
        self.network.bdstoken = self.network.get_bdstoken()
        self.check_condition(not isinstance(self.network.bdstoken, str) or not self.network.bdstoken,
                             message=f'没获取到 bdstoken 参数，错误代码：{self.network.bdstoken}')

    def handle_list_dir(self) -> None:
        """获取目标目录下的文件和目录列表"""
        self.dir_list_all = self.network.get_dir_list(f'/{self.folder_name}')
        self.check_condition(isinstance(self.dir_list_all, int) or not self.dir_list_all,
                             message=f'目录 {self.folder_name} 中没获取到任何内容，请求返回：{self.dir_list_all}')

    def handle_create_dir(self, folder_name: str, require_empty: bool = False) -> bool:
        """确保目录存在；指定目录必须为空且尚未被本轮任务占用。"""
        if require_empty and folder_name in self.used_custom_dirs:
            return False
        if not require_empty and folder_name in self.created_dirs:
            return True

        if require_empty:
            # New custom directories need only one request. An existing path
            # is checked below before the caller selects another name.
            return_code = self.network.create_dir(folder_name)
            if return_code == 0:
                self.created_dirs.add(folder_name)
                return True
            if return_code not in (-8, 4):
                raise RuntimeError(f'创建目录失败，错误代码：{return_code}')

        result = self.network.get_dir_list(f'/{folder_name}')
        if require_empty:
            if not isinstance(result, list):
                raise RuntimeError(f'无法检查目录 {folder_name}，请求返回：{result}')
            return not result

        # 如果 result 为错误代码数字，代表目标目录不存在
        if folder_name and isinstance(result, int):
            return_code = self.network.create_dir(folder_name)
            if return_code not in (0, -8, 4):
                raise RuntimeError(f'创建目录失败，错误代码：{return_code}')
        elif not isinstance(result, list):
            raise RuntimeError(f'无法检查目录 {folder_name}，请求返回：{result}')
        self.created_dirs.add(folder_name)
        return True

    def handle_process_save(self) -> None:
        """执行批量转存"""
        while self.next_task_index < len(self.link_list):
            task_index = self.next_task_index
            url_code = self.link_list[task_index]
            raw_link = self.link_list_org[task_index]
            self.process_save(url_code, raw_link)
            # Commit the checkpoint before updating UI, which can also fail.
            self.next_task_index = task_index + 1
            self.completed_task_count = self.next_task_index
            self.change_status('progress')

    def handle_process_share(self) -> None:
        """执行批量分享"""
        for info in self.dir_list_all:
            self.process_share(info)

    def process_save(self, url_code: str, raw_link: str = '') -> None:
        """执行转存操作并记录结果"""
        try:
            # 跳过非网盘链接
            if 'https://pan.baidu.com/' not in url_code:
                self.insert_logs(f'不支持的链接：{url_code}')
            else:
                # 执行转存过程，通过简单的循环判断是否要暂停
                self.pause_detection(url_code, raw_link)
        except Exception as e:
            # A malformed response must only fail this item, not the whole batch.
            self.insert_logs(f'转存时发生未预料错误：{e}：{url_code}')

    def process_share(self, info: Dict[str, Any]) -> None:
        """执行分享操作并记录结果"""
        # 插入要分享的文件或文件夹到链接输入框，对文件夹加入 "/" 标记来区别
        is_dir = "/" if info["isdir"] == 1 else ""
        filename = f"{info['server_filename']}{is_dir}"
        msg = f'目录：{filename}' if is_dir else f'文件：{filename}'
        self.insert_logs(msg, alt=True)

        # 处理提取码
        password = self.password if not self.random_password else generate_code()

        # 发送创建分享请求
        r = self.network.create_share(info['fs_id'], EXP_MAP[self.expiry], password)
        if isinstance(r, str):
            result = f'分享成功：{r}?pwd={password} {msg}'
        else:
            result = f'分享失败：错误代码（{r}） {msg}'

        # 记录日志，更改状态
        self.insert_logs(result)
        self.change_status('update')

    # noinspection PyArgumentList
    def change_status(self, status: str) -> None:
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
            self.running = True
            self.root.bottom_share.config(state="disabled")
            self.root.bottom_save.config(text='点击暂停', state="normal", bootstyle='danger', command=lambda: self.change_status('paused'))
        elif status == 'paused':
            self.running = False
            self.root.bottom_save.config(text='点击继续', bootstyle='success', command=lambda: self.change_status('running'))
        elif status == 'update':
            self.completed_task_count += 1
            self.change_status('progress')
        elif status == 'progress':
            self.root.label_status.config(text=f'总进度：{self.completed_task_count}/{self.total_task_count}', bootstyle='success')
        elif status == 'interrupted':
            self.running = False
            self.root.label_status.config(text=f'程序中断，可继续转存：{self.completed_task_count}/{self.total_task_count}', bootstyle='warning')
            self.root.bottom_save.config(text='继续转存', state="normal", bootstyle='warning', command=lambda: thread_it(self.save, True))
            self.root.bottom_share.config(state="disabled")
        elif status == 'sharing':
            self.root.text_links.delete(1.0, 'end')
            self.root.bottom_share.config(state="disabled")
            self.root.bottom_save.config(state="disabled")
        elif status == 'error':
            self.root.label_status.config(text='发生错误：', bootstyle='danger')
        else:
            self.running = False
            self.root.bottom_save.config(text='批量转存', state="normal", bootstyle='primary', command=lambda: thread_it(self.save, ))
            self.root.bottom_share.config(state="normal")

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
        text_box = self.root.text_links if alt else self.root.text_logs
        text_box.insert('end', f'{message}\n')
        text_box.see('end')

    def pause_detection(self, url_code: str, raw_link: str = '') -> None:
        """循环检测暂停逻辑，每个转存任务开始时都会检测"""
        # 只要 self.running 状态没变，会一直等待。而状态变化由用户点击按钮控制
        while not self.running:
            time.sleep(DELAY_SECONDS)

        # 执行验证链接和转存文件
        self.verify_and_save(url_code, raw_link)

    def verify_and_save(self, url_code: str, raw_link: str = '') -> None:
        """验证链接和转存文件"""
        if not self.check_mode and not self.custom_mode and url_code in self.saved_links:
            self.save_file(4, url_code, self.folder_name, raw_link)
            return
        # 验证链接是否有效，返回的数字为错误代码，反之返回参数列表
        result = self.verify_link(*parse_url_and_code(url_code))
        # 如果开启检查模式，插入检查结果，然后结束
        if self.check_mode:
            self.check_only(result, url_code)
        else:
            self.save_file(result, url_code, self.folder_name, raw_link)

    def verify_link(self, url: str, password: str) -> Union[List[Any], int]:
        """验证链接有效性，验证通过返回转存所需参数列表"""
        # 对于有提取码的链接先验证提取码，试图获取更新 bdclnd
        if password:
            bdclnd = self.network.verify_pass_code(url, password)
            # 如果 bdclnd 是错误代码，直接返回
            if isinstance(bdclnd, int):
                return bdclnd
            if not isinstance(bdclnd, str) or not bdclnd:
                return -1

            # 更新 bdclnd 到 cookie
            self.network.headers['Cookie'] = update_cookie(bdclnd, self.network.headers['Cookie'])

        # 直接访问没有提取码的链接，或更新 bdclnd 后再次访问，获取转存必须的 3 个参数
        response = self.network.get_transfer_params(url)
        # 这里不考虑网络异常了，假设请求一定会返回页面内容，对其进行解析
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
        # 对原始输入进行分割
        link_org_sep = raw_link.split()
        # Explicit directory prefixes are preserved; bare URLs use a link counter.
        custom_folder = link_org_sep[0] if link_org_sep else ''
        numeric_name = not custom_folder or 'pan.baidu.com' in custom_folder
        custom_folder = str(directory_number) if numeric_name else custom_folder
        folder_name = f'{folder_name}/{custom_folder}'
        # 此处用替换处理目标目录名非法字符，不报错了
        folder_name = folder_name.translate(str.maketrans({char: '_' for char in INVALID_CHARS}))

        base_path = folder_name
        for offset in range(SAVE_LIMIT):
            if offset:
                folder_name = (
                    f'{base_path.rsplit("/", 1)[0]}/{directory_number + offset}'
                    if numeric_name else f'{base_path}_{offset + 1}'
                )
            if self.handle_create_dir(folder_name, require_empty=True):
                self.used_custom_dirs.add(folder_name)
                if numeric_name:
                    self.custom_dir_count = directory_number + offset
                return folder_name
        raise RuntimeError(f'无法为当前链接创建空目录：{base_path}')

    def save_file(
        self,
        result: Union[List[Any], int],
        url_code: str,
        folder_name: str,
        raw_link: str = '',
    ) -> None:
        """转存文件。返回结果为列表时，执行转存文件，否则跳过转存。"""
        file_info = ""
        if is_valid_transfer_params(result):
            filenames = ', '.join(result[3])
            file_info = f'{"目录" if result[4] == ["1"] else "文件"}：{filenames}'
            if self.custom_mode:
                self.custom_dir_count += 1
                transfer_params = result
                folder_name = self.creat_user_dir(
                    folder_name, raw_link or url_code, self.custom_dir_count
                )
                result = self.transfer_with_logs(transfer_params, folder_name, url_code)
                if result in (-8, 4):
                    # A previous request or another client may have populated
                    # the path. Use a fresh empty directory once.
                    self.insert_logs(f'目录存在同名文件，改用新的空目录重试：{folder_name}')
                    self.custom_dir_count += 1
                    folder_name = self.creat_user_dir(
                        self.folder_name, raw_link or url_code, self.custom_dir_count
                    )
                    result = self.transfer_with_logs(transfer_params, folder_name, url_code)
                file_info = f'{file_info} 转存到：{folder_name}'
            elif url_code in self.saved_links:
                result = 4
            else:
                result = self.transfer_with_logs(result, folder_name, url_code)
                if result == 0:
                    self.saved_links.add(url_code)
        else:
            result = result if isinstance(result, int) else -1

        if not isinstance(result, int):
            result = -1

        # 最后插入转存结果到日志框
        self.insert_logs(f'{ERROR_CODES.get(result, f"转存失败，错误代码（{result}）")}：{url_code} {file_info}')

    def transfer_with_logs(self, params: List[Any], folder_name: str, url_code: str) -> int:
        """记录每次重试；重试期间使用相同的链接与目录。"""
        def on_retry(attempt, reason):
            self.insert_logs(f'转存失败（{reason}），正在第 {attempt} 次尝试：{url_code}')

        return self.network.transfer_file(params, folder_name, on_retry=on_retry)
