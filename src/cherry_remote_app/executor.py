"""指令执行器（纯执行，无判断）。

每个 method 对应一个 `_exec_<method>` 异步方法，返回可 JSON 序列化的 dict。
指令白名单在构造时从配置注入。
"""

import asyncio
import base64
import json
import locale
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import psutil

LOG = logging.getLogger("cherry-remote-app.executor")

# exe 索引默认扫描根目录
_EXE_INDEX_ROOTS = (
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
)


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
        # exe 索引
        self.build_exe_index: bool = bool(config.get("build_exe_index", True))
        self.exe_index_file: str = str(config.get("exe_index_file", "exe_index.json"))
        self.exe_index: dict[str, str] = {}
        self._index_task: asyncio.Task | None = None

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

    # ---------- exe 索引（后台构建） ----------

    def start_background_tasks(self) -> None:
        """启动后台任务（exe 索引构建等）。在事件循环中调用。"""
        if self.build_exe_index and self._index_task is None:
            self._index_task = asyncio.create_task(self._build_exe_index())

    async def _build_exe_index(self) -> None:
        """扫描常见目录生成 exe 名称→路径索引，写入 exe_index.json。"""
        try:
            LOG.info("正在构建 exe 索引……")
            index: dict[str, str] = {}
            count = 0
            for root, max_depth in self._index_roots():
                for exe_path in self._walk_exes(root, max_depth=max_depth):
                    key = os.path.basename(exe_path).lower()
                    if key not in index:
                        index[key] = exe_path
                        count += 1
                    if count >= 8000:
                        break
                if count >= 8000:
                    LOG.info("exe 索引已达上限 8000 条，停止扫描")
                    break
            self.exe_index = index
            try:
                Path(self.exe_index_file).write_text(
                    json.dumps(index, ensure_ascii=False), encoding="utf-8"
                )
                LOG.info(f"exe 索引构建完成：{len(index)} 条 -> {self.exe_index_file}")
            except Exception as e:
                LOG.warning(f"exe 索引写入失败: {e}")
        except Exception as e:
            LOG.error(f"exe 索引构建失败: {e}")

    def _index_roots(self) -> list[tuple[str, int]]:
        """返回 (根目录, 最大递归深度) 列表。"""
        roots: list[tuple[str, int]] = []
        seen: set[str] = set()
        for p in os.environ.get("PATH", "").split(os.pathsep):
            if p and os.path.isdir(p) and os.path.normpath(p) not in seen:
                seen.add(os.path.normpath(p))
                roots.append((p, 1))
        for d in _EXE_INDEX_ROOTS:
            if d and os.path.isdir(d) and os.path.normpath(d) not in seen:
                seen.add(os.path.normpath(d))
                roots.append((d, 1 if "Windows" in d else 3))
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        programs_dir = os.path.join(local_appdata, "Programs")
        if local_appdata and os.path.isdir(programs_dir):
            roots.append((programs_dir, 3))
        return roots

    @staticmethod
    def _walk_exes(root: str, max_depth: int = 3):
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                if fn.lower().endswith(".exe"):
                    yield os.path.join(dirpath, fn)

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
        resolved = self._resolve_exe(name)
        if resolved:
            name = resolved
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
        except OSError as e:
            # Windows 740：需要管理员权限（"The requested operation requires elevation"）
            if os.name == "nt" and getattr(e, "winerror", None) == 740:
                return await self._launch_elevated(name, args, cwd)
            raise

    def _resolve_exe(self, name: str) -> str | None:
        """通过 exe 索引或 PATH 把应用名解析为完整路径。"""
        low = name.lower()
        if os.sep in name or "/" in name:
            return None  # 本身就是路径
        if not low.endswith(".exe"):
            low += ".exe"
        path = self.exe_index.get(low)
        if path:
            return path
        return shutil.which(name) or shutil.which(low)

    async def _launch_elevated(self, name: str, args: list[str], cwd: str | None) -> dict:
        """用 ShellExecuteW(runas) 提权启动（会触发 C 端 UAC 弹窗）。"""
        import ctypes

        if not os.path.isfile(name):
            name = self.exe_index.get(name.lower(), name)
        params = " ".join(args)
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", name, params, cwd or None, 1
        )
        if rc > 32:
            return {"ok": True, "launched": name, "elevated": True, "note": "ShellExecute runas"}
        return {"ok": False, "error": f"提权启动失败 (code={rc})"}

    async def _app_search(self, params: dict) -> dict:
        """在 exe 索引中按名称模糊搜索应用。"""
        query = (params.get("query") or "").lower().strip()
        matches: list[dict] = []
        for exe_name, exe_path in sorted(self.exe_index.items()):
            if not query or query in exe_name:
                matches.append({"name": exe_name, "path": exe_path})
                if len(matches) >= 50:
                    break
        return {"count": len(matches), "query": query, "matches": matches}

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
