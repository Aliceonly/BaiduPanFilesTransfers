# Windows EXE 打包指南

本文说明如何将本仓库的程序打包为 `BaiduPanFilesTransfers.exe`，并排查打包后无法启动的问题。

**必须在 Windows 上执行 Windows 打包。** PyInstaller 不是 cross-compiler；在 macOS 或 Linux 上执行它，不会生成可在 Windows 上运行的 `.exe`。如果当前使用 macOS，请在 Windows 电脑或 Windows VM 中按本文操作。

## 1. 准备环境

本文以 **Windows 10/11 x64 + 官方 Python 3.12 x64 + PowerShell** 为示例环境，不保证 Windows 7 兼容性。

1. 从 [Python 官方 Windows 下载页面](https://www.python.org/downloads/windows/) 安装 Python 3.12 x64。安装时保留 Python Launcher、`pip` 和 `Tcl/Tk and IDLE` 组件。
2. 安装 Git，或通过 GitHub 的 `Code > Download ZIP` 下载本仓库并解压。
3. 使用普通权限打开 PowerShell，进入可写的工作目录，不要在 `Program Files` 等受保护目录内构建或运行。

检查 Python Launcher 能找到对应版本：

```powershell
py -3.12 --version
```

如果安装的是其他兼容版本，需要同步替换下文中的 `-3.12`。本仓库的 Release workflow 同样使用 Python 3.12 x64，在 Windows runner 上完成构建和启动检查；目标电脑上的实际网盘操作仍需自行验证。

## 2. 获取包含修复的源码

首次获取源码：

```powershell
git clone https://github.com/Aliceonly/BaiduPanFilesTransfers.git
cd .\BaiduPanFilesTransfers
```

如果已经克隆了本仓库，在工作区干净且当前分支为 `master` 时更新：

```powershell
git pull --ff-only origin master
```

如果使用 ZIP，请从 [Aliceonly 仓库的 master 分支](https://github.com/Aliceonly/BaiduPanFilesTransfers/archive/refs/heads/master.zip) 下载并解压，再进入解压目录。后续所有命令都在包含 `BaiduPanFilesTransfers.py` 和 `requirements.txt` 的仓库根目录执行。

原作者的历史 Release 不一定包含本仓库新增的批量转存修复，不要将历史 `.exe` 当作本次源码的打包产物。

## 3. 创建环境并检查源码

直接调用 virtualenv 中的 Python，无需执行 `Activate.ps1`，也无需修改 PowerShell Execution Policy。

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m tkinter
```

最后一个命令应打开 Tk 测试窗口；关闭窗口后继续：

```powershell
.\venv\Scripts\python.exe -m pytest -q tests src/test.py
.\venv\Scripts\python.exe .\BaiduPanFilesTransfers.py
```

测试使用 API mock，不访问真实百度网盘，也不创建真实网盘目录。源码启动检查应能显示程序主窗口，关闭程序后再打包。测试通过不能代替 Windows GUI 和最终 `.exe` 的运行验证。

## 4. 打包单文件 EXE

在仓库根目录执行以下一整行命令：

```powershell
.\venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name BaiduPanFilesTransfers --icon .\BaiduPanFilesTransfers.ico --hidden-import tkinter --collect-all ttkbootstrap .\BaiduPanFilesTransfers.py
```

参数含义：

- `--onefile`：生成单文件可执行程序。
- `--windowed`：运行 GUI 时不显示 console 窗口。
- `--icon`：设置 Windows 可执行文件图标。
- `--hidden-import tkinter`：显式收集 `tkinter`；Tcl/Tk 仍需在构建环境中正确安装。
- `--collect-all ttkbootstrap`：收集 `ttkbootstrap` 的 submodules、data 和 binaries。
- `--clean`：清理 PyInstaller 缓存；`--noconfirm`：允许替换同名构建输出。

成功后，主要文件的位置如下：

```text
dist\BaiduPanFilesTransfers.exe     最终可执行文件
build\                            构建中间文件
BaiduPanFilesTransfers.spec        PyInstaller 构建配置
```

运行时建议先进入产物目录，让配置写入该目录：

```powershell
cd .\dist
.\BaiduPanFilesTransfers.exe
```

正常运行时不需要额外安装 Python。程序的 runtime 图标已内嵌在源码中，无需另行复制 `BaiduPanFilesTransfers.png`。

## 5. 验证与分发

发布前至少检查以下内容：

1. 主窗口能打开，输入框、选项和暂停/恢复操作正常；批次中断后显示「继续转存」，恢复时保留进度和目录编号。
2. 「批量分享」设置弹窗能打开和关闭。
3. 使用自己有权限的少量测试链接，验证普通转存、指定目录和检测模式；实际转存会修改网盘，请不要直接用重要数据验证。
4. 在目标 Windows 机器上测试，最好包含一台未安装 Python 的机器。

单文件模式下，可将 `dist\BaiduPanFilesTransfers.exe` 复制到一个可写目录使用；对外分发时也应保留 GPL-3.0 所要求的许可和对应源码获取方式。

**不要分发 `config.ini`。** 它包含 Cookie 等个人配置。当前代码通过相对路径读写该文件，即写入进程的 current working directory，不一定是 `.exe` 所在目录。首次运行没有该文件是正常的，执行相关操作时才会保存配置。不要将自己的 Cookie、配置或私有日志上传到 GitHub。

## 6. 无法启动时生成 Debug 版本

`--windowed` 会隐藏 console，排查时改用 `--onedir --console`。在仓库根目录执行：如果刚完成第 4 节的运行命令，先用 `cd ..` 返回根目录。

```powershell
.\venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --console --name BaiduPanFilesTransfersDebug --icon .\BaiduPanFilesTransfers.ico --hidden-import tkinter --collect-all ttkbootstrap .\BaiduPanFilesTransfers.py
.\dist\BaiduPanFilesTransfersDebug\BaiduPanFilesTransfersDebug.exe
```

从 PowerShell 启动后，可以在窗口中看到 traceback。反馈问题时提供 traceback、Windows 版本、Python 版本及 PyInstaller 版本，先去除 Cookie 和其他敏感信息：

```powershell
.\venv\Scripts\python.exe --version
.\venv\Scripts\python.exe -m PyInstaller --version
```

Debug 版本是目录模式，移动或分发时必须保留整个 `dist\BaiduPanFilesTransfersDebug` 目录，不能只复制其中的 `.exe`。

## 7. 常见问题

| 现象 | 处理方法 |
| --- | --- |
| `py` 命令不存在，或找不到 Python 3.12 | 检查 Python Launcher 和对应版本是否安装；也可以用已确认版本的 `python -m venv venv` 创建环境。 |
| PowerShell 禁止运行 `Activate.ps1` | 本文无需 activate，直接使用 `.\venv\Scripts\python.exe`，不用更改 Execution Policy。 |
| 提示找不到 `tkinter`、`_tkinter` 或 Tcl/Tk | 修改或重新安装官方 Python，启用 `Tcl/Tk and IDLE`，重新创建 virtualenv，再运行 `python -m tkinter` 检查。不要尝试 `pip install tkinter`。 |
| 提示找不到 `pyinstaller` 命令 | 使用本文的 `python.exe -m PyInstaller`，确认依赖安装在同一个 virtualenv 中。 |
| `.exe` 启动后闪退，或某个弹窗无法打开 | 按第 6 节构建 console 版本，查看 traceback；先确认同一环境下源码运行正常。 |
| 配置无法保存或出现 `PermissionError` | 将程序放入可写目录，并确认启动时的 current working directory 可写，不要依赖管理员权限解决。 |
| 单文件首次启动较慢 | `--onefile` 启动时会解包依赖，可用 `--onedir` 对比启动速度；这不等同于批量转存速度。 |
| 想在 macOS/Linux 生成 Windows `.exe` | 改在 Windows 主机或 Windows VM 中构建；PyInstaller 不提供跨操作系统编译。 |

不要将 Windows x64 的构建产物直接视为其他 architecture 或旧版 Windows 的兼容产物；不同目标环境需要单独构建和验证。

## 8. 使用 GitHub Actions 自动发布

仓库内的 `.github/workflows/windows-release.yml` 提供远程 Windows 构建，macOS 用户无需安装本地 Windows VM 即可使用：

- 手动构建：进入 GitHub 的 `Actions > Windows EXE Release > Run workflow`，选择分支并执行。成功后下载该 Run 的 `windows-x64-release` Artifact。手动执行只生成构建产物，不创建 Release。
- 发布版本：更新 `src/constants.py` 的 `MAIN_VERSION`，准备对应的 `releases/v版本号.md`，提交并 push 后，再 push 同名 Tag，例如 `v2.8.4`。Tag 必须与代码中的版本一致。
- Tag 构建通过回归测试、源码 GUI 检查和 `.exe` 主窗口启动检查后，workflow 才会创建 Release 并上传 `.exe`、ZIP、构建信息和 SHA-256 校验文件。构建失败时不会发布。

每个 Release 的 `BUILD_INFO.txt` 记录源码 Commit 和实际安装的依赖版本。ZIP 只收集程序、许可、说明及构建信息，不会包含工作目录中的 Cookie 配置。

发布需要启用 GitHub Actions，并允许 workflow 使用 `contents: write`；无需另行提供 Personal Access Token。自动检查不登录百度网盘，也不执行真实转存。

## 官方参考

- [PyInstaller 文档：平台支持及非 cross-compiler 说明](https://pyinstaller.org/en/stable/)
- [PyInstaller Usage：命令行参数](https://pyinstaller.org/en/stable/usage.html)
- [PyInstaller Installation：通过 Python module 调用](https://pyinstaller.org/en/stable/installation.html)
- [Python Windows 下载页面](https://www.python.org/downloads/windows/)
