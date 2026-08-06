"""指令执行器（纯执行，无判断）。

每个 method 对应一个 `_exec_<method>` 异步方法，返回可 JSON 序列化的 dict。
指令白名单在构造时从配置注入。
"""

import asyncio
import base64
import locale
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import psutil

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

    # ---------- file 方法 ----------

    async def _exec_file(self, params: dict) -> dict:
        action = params.get("action")
        if not params.get("path"):
            raise ValueError("params.path 必填")
        handler = getattr(self, f"_file_{action}", None)
        if handler is None:
            raise NotImplementedError(f"file action '{action}' 未实现")
        return await handler(params)

    async def _file_list(self, params: dict) -> dict:
        p = Path(params["path"])
        if not p.exists():
            raise FileNotFoundError(f"路径不存在: {p}")
        recursive = bool(params.get("recursive", False))
        iterator = p.rglob("*") if recursive else p.iterdir()
        entries = []
        for item in iterator:
            try:
                st = item.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "type": "dir" if item.is_dir() else "file",
                    "size": st.st_size if item.is_file() else 0,
                    "mtime": st.st_mtime,
                }
            )
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"path": str(p), "count": len(entries), "entries": entries}

    async def _file_read(self, params: dict) -> dict:
        p = Path(params["path"])
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {p}")
        data = p.read_bytes()
        for enc in ("utf-8", locale.getpreferredencoding(False)):
            try:
                text = data.decode(enc)
                return {"path": str(p), "encoding": enc, "size": len(data), "content": text}
            except UnicodeDecodeError:
                continue
        return {
            "path": str(p),
            "encoding": "base64",
            "size": len(data),
            "content": base64.b64encode(data).decode("ascii"),
        }

    async def _file_write(self, params: dict) -> dict:
        p = Path(params["path"])
        content = params.get("content", "")
        p.parent.mkdir(parents=True, exist_ok=True)
        if params.get("encoding") == "base64":
            data = base64.b64decode(content)
            p.write_bytes(data)
            return {"path": str(p), "ok": True, "bytes": len(data)}
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
            return {"path": str(p), "ok": True, "bytes": len(content.encode("utf-8"))}
        raise ValueError("params.content 必须为字符串或 base64")

    async def _file_copy(self, params: dict) -> dict:
        src, dest = params["path"], params.get("dest")
        if not dest:
            raise ValueError("params.dest 必填")
        if Path(src).is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        return {"ok": True, "src": src, "dest": dest}

    async def _file_delete(self, params: dict) -> dict:
        p = Path(params["path"])
        if not p.exists():
            raise FileNotFoundError(f"路径不存在: {p}")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"ok": True, "deleted": str(p)}

    async def _file_info(self, params: dict) -> dict:
        p = Path(params["path"])
        if not p.exists():
            raise FileNotFoundError(f"路径不存在: {p}")
        st = p.stat()
        return {
            "path": str(p),
            "type": "dir" if p.is_dir() else "file",
            "size": st.st_size,
            "mtime": st.st_mtime,
            "absolute": str(p.absolute()),
        }

    # ---------- app 方法 ----------

    async def _exec_app(self, params: dict) -> dict:
        action = params.get("action")
        handler = getattr(self, f"_app_{action}", None)
        if handler is None:
            raise NotImplementedError(f"app action '{action}' 未实现")
        return await handler(params)

    async def _app_launch(self, params: dict) -> dict:
        name = params.get("name")
        if not name:
            raise ValueError("params.name 必填")
        args = [str(a) for a in (params.get("args") or [])]
        cwd = params.get("cwd")
        try:
            proc = subprocess.Popen(
                [name, *args],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "pid": proc.pid, "launched": name}
        except FileNotFoundError:
            if os.name == "nt":
                os.startfile(name)
                return {"ok": True, "launched": name, "note": "os.startfile"}
            raise FileNotFoundError(f"未找到可启动的应用: {name}") from None

    async def _app_terminate(self, params: dict) -> dict:
        pid = params.get("pid")
        name = params.get("name")
        terminated: list[int] = []
        if pid is not None:
            proc = psutil.Process(int(pid))
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except (psutil.TimeoutExpired, Exception):
                proc.kill()
            terminated.append(int(pid))
        elif name:
            target = name.lower()
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                except Exception:
                    continue
                if target in pname:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except (psutil.TimeoutExpired, Exception):
                        proc.kill()
                    terminated.append(proc.pid)
        else:
            raise ValueError("params.pid 或 params.name 必填")
        return {"ok": True, "terminated": terminated}

    # ---------- screenshot 方法 ----------

    async def _exec_screenshot(self, params: dict) -> dict:
        import io

        from PIL import ImageGrab

        # all_screens=True：截取所有显示器组成的完整虚拟桌面，避免多屏时只截主屏
        image = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        raw = buf.getvalue()
        return {
            "image": base64.b64encode(raw).decode("ascii"),
            "format": "png",
            "width": image.width,
            "height": image.height,
            "size": len(raw),
        }
