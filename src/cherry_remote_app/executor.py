"""指令执行器（纯执行，无判断）。

每个 method 对应一个 `_exec_<method>` 异步方法，返回可 JSON 序列化的 dict。
指令白名单在构造时从配置注入。
"""

import asyncio
import locale
import logging
import os
import time

LOG = logging.getLogger("cherry-remote-app.executor")


def _decode_output(data: bytes) -> str:
    """解码子进程输出。

    Windows 控制台命令（ping/dir 等）按系统 ANSI 代码页输出（中文系统为 GBK），
    若硬按 UTF-8 解码会产生乱码。此处 Windows 优先用 locale 编码，其他平台用 UTF-8。
    """
    if not data:
        return ""
    if os.name == "nt":
        enc = locale.getpreferredencoding(False) or "utf-8"
    else:
        enc = "utf-8"
    try:
        return data.decode(enc)
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


class Executor:
    def __init__(self, config: dict):
        self.allowed_actions: set[str] = set(
            config.get("allowed_actions", ["exec", "sys", "ping"])
        )
        self.default_timeout: float = float(config.get("default_timeout", 30))

    async def execute(self, method: str, params: dict) -> dict:
        """执行一条指令。method 不在白名单或未实现时抛异常。"""
        if method not in self.allowed_actions:
            raise PermissionError(f"action '{method}' 不在白名单内")
        handler = getattr(self, f"_exec_{method}", None)
        if handler is None:
            raise NotImplementedError(f"method '{method}' 未实现")
        return await handler(params or {})

    # ---------- method 实现 ----------

    async def _exec_ping(self, params: dict) -> dict:
        return {"pong": True}

    async def _exec_exec(self, params: dict) -> dict:
        """执行 shell 命令。"""
        command = params.get("command")
        if not command or not isinstance(command, str):
            raise ValueError("params.command 必填且为字符串")
        timeout = float(params.get("timeout", self.default_timeout))
        cwd = params.get("cwd")
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (params.get("env") or {}).items()})

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        start = time.monotonic()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            timed_out = True
        elapsed = round(time.monotonic() - start, 3)

        return {
            "stdout": _decode_output(stdout),
            "stderr": _decode_output(stderr),
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "elapsed": elapsed,
        }

    async def _exec_sys(self, params: dict) -> dict:
        """采集系统信息（psutil）。"""
        import platform

        import psutil

        vm = psutil.virtual_memory()
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            disks.append(
                {
                    "mount": part.mountpoint,
                    "device": part.device,
                    "total": usage.total,
                    "used": usage.used,
                    "percent": usage.percent,
                }
            )

        cpu_freq = None
        try:
            freq = psutil.cpu_freq()
            if freq:
                cpu_freq = {"current": freq.current, "max": freq.max, "min": freq.min}
        except Exception:
            pass

        return {
            "hostname": platform.node(),
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpu": {
                "percent": psutil.cpu_percent(interval=0.3),
                "count": psutil.cpu_count(logical=True),
                "freq": cpu_freq,
            },
            "memory": {
                "total": vm.total,
                "available": vm.available,
                "used": vm.used,
                "percent": vm.percent,
            },
            "disk": disks,
            "boot_time": psutil.boot_time(),
        }
