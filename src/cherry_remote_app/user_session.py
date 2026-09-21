# -*- coding: utf-8 -*-
"""在交互用户会话（Session 1）中执行 helper 的通用机制。

服务形态下本程序通常以 LocalSystem 运行在 Session 0：
- 截屏：Session 0 没有交互桌面，`ImageGrab` 拿不到画面；
- 摄像头：Session 0 没有摄像头访问权，系统「相机」隐私同意记录也属于交互用户。

因此这里用计划任务（schtasks）把 helper 切到**当前登录的交互用户**身份执行，
产物写入临时文件，再由本进程读回。

helper 复用本程序自身的代码（打包形态直接用 exe 的 `--camera-helper` / `--screenshot-helper`；
源码形态用引导脚本导入 `cherry_remote_app.helper`），因此不依赖目标机上另装 Python 或 OpenCV。
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

LOG = logging.getLogger("cherry-remote-app.user_session")

# src 目录：源码形态下引导脚本需要把它加入 sys.path
_SRC_DIR = str(Path(__file__).resolve().parent.parent)


class UserSessionUnavailable(RuntimeError):
    """没有可用的交互用户会话（无人登录，或计划任务创建失败）。"""


class HelperTimeout(RuntimeError):
    """helper 在超时时间内没有产出结果文件。"""


def current_session_id() -> int:
    """返回当前进程所在会话 id（Windows）；非 Windows 或失败返回 -1。

    Session 0 = 服务会话，无法访问交互桌面与摄像头。
    """
    if os.name != "nt":
        return -1
    import ctypes

    sid = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return int(sid.value) if ok else -1


def find_active_user() -> str:
    """返回当前交互（Session 1）登录用户的用户名，取不到时返回空串。"""
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\").GetOwner().User",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        users = (out.stdout or "").strip().splitlines()
        if users and users[0].strip():
            return users[0].strip()
    except Exception:  # noqa: BLE001 —— 取不到就回退到环境变量
        pass
    return os.environ.get("USERNAME", "")


def find_python_exe() -> str:
    """返回可用于运行 helper 的 Python 解释器（仅源码形态需要）。"""
    exe = sys.executable or ""
    if exe and not getattr(sys, "frozen", False):
        return exe
    for name in ("python.exe", "pythonw.exe", "py.exe"):
        found = shutil.which(name)
        if found:
            return found
    return "python"


def build_helper_command(args: list[str]) -> tuple[list[str], str | None]:
    """构造切换会话执行 helper 的命令行。

    返回 (命令参数列表, 临时引导脚本路径或 None)。
    打包形态直接复用 exe；源码形态写一个引导脚本导入 `cherry_remote_app.helper`。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *args], None
    bootstrap = os.path.join(tempfile.gettempdir(), f"cherry_boot_{uuid.uuid4().hex[:8]}.py")
    code = (
        "# -*- coding: utf-8 -*-\n"
        "import sys\n"
        f"sys.path.insert(0, {_SRC_DIR!r})\n"
        "from cherry_remote_app.helper import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    with open(bootstrap, "w", encoding="utf-8") as f:
        f.write(code)
    exe = find_python_exe()
    return [exe, bootstrap, *args], bootstrap


async def run_in_user_session(
    args: list[str], out_path: str, timeout: float, tag: str = "cherry_helper"
) -> None:
    """以交互用户身份执行一次 helper，等待产物文件出现。

    args：传给 helper 的参数（如 `["--camera-helper", in_json, out_img, out_meta]`）。
    out_path：期望的产物文件路径；该文件出现（且非空）即视为成功。

    耗时的子进程调用与等待都放在线程/异步等待中，不阻塞事件循环（心跳不受影响）。
    """
    username = await asyncio.to_thread(find_active_user)
    if not username:
        raise UserSessionUnavailable("未找到交互登录用户，无法切换到用户会话执行")

    command, bootstrap = await asyncio.to_thread(build_helper_command, args)
    task_name = f"{tag}_{uuid.uuid4().hex[:8]}"
    try:
        cmd_line = subprocess.list2cmdline(command)
        create = await asyncio.to_thread(
            subprocess.run,
            [
                "schtasks", "/create", "/tn", task_name, "/tr", cmd_line,
                "/sc", "once", "/st", "23:59", "/ru", username, "/it", "/f",
            ],
            capture_output=True,
            timeout=15,
        )
        if create.returncode != 0:
            detail = (create.stderr or create.stdout or b"").decode("utf-8", "replace").strip()
            raise UserSessionUnavailable(f"创建计划任务失败: {detail or create.returncode}")

        run = await asyncio.to_thread(
            subprocess.run, ["schtasks", "/run", "/tn", task_name], capture_output=True, timeout=15
        )
        if run.returncode != 0:
            detail = (run.stderr or run.stdout or b"").decode("utf-8", "replace").strip()
            raise UserSessionUnavailable(f"计划任务启动失败: {detail or run.returncode}")

        LOG.info("已切换交互用户会话执行 helper（user=%s task=%s）", username, task_name)
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return
            await asyncio.sleep(0.3)
        raise HelperTimeout(f"helper 在 {timeout:.0f} 秒内没有产出结果文件")
    finally:
        await asyncio.to_thread(
            subprocess.run,
            ["schtasks", "/delete", "/tn", task_name, "/f"],
            capture_output=True,
            timeout=15,
        )
        if bootstrap:
            try:
                os.remove(bootstrap)
            except OSError:
                pass


def temp_path(suffix: str, prefix: str = "cherry_helper") -> str:
    """生成临时文件路径（不创建文件）。"""
    return os.path.join(tempfile.gettempdir(), f"{prefix}_{uuid.uuid4().hex[:8]}{suffix}")


def cleanup_paths(*paths: str) -> None:
    """静默删除若干临时文件。"""
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass
