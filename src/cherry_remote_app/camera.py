# -*- coding: utf-8 -*-
"""摄像头采集（`camera` method 的实现）。

职责边界与截屏一致：**只采集、只回传，不做任何画面判断**。
- `list`：枚举本机摄像头（OpenCV 逐个 index 试开 + 系统 PnP 设备名交叉验证）；
- `capture`：拍一帧（可选连拍选优），编码为 base64 回传；
- `status`：返回摄像头功能的开关、限流与所在会话，便于 B 端/AI 自查。

隐私设计（见 docs/CAMERA.md §4.7）：默认关闭需显式开启、冷却 + 小时配额限流、
审计日志、可选本地留档与提示音，且**不提供持续录制/推流**。

采集后端：OpenCV（opencv-python-headless）为主，ffmpeg 为可选兜底；
服务会话（Session 0）下经 `user_session` 切到交互用户执行一次采集。
"""

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections import deque
from pathlib import Path

from . import user_session
from .user_session import HelperTimeout, UserSessionUnavailable

LOG = logging.getLogger("cherry-remote-app.camera")

# 传给交互会话 helper 的参数字段
_HELPER_KEYS = (
    "device",
    "width",
    "height",
    "quality",
    "format",
    "warmup_frames",
    "burst",
    "mirror",
    "backend",
)


# ---------- 错误类（类名即回传给 B 端的 error.code） ----------


class CameraError(RuntimeError):
    """摄像头相关错误基类。"""


class CameraDisabled(CameraError):
    """摄像头功能未开启（camera.enabled=false）。"""


class CameraBackendUnavailable(CameraError):
    """既没有可用的 OpenCV，也没有 ffmpeg。"""


class CameraNotFound(CameraError):
    """没有可用的摄像头，或指定的设备不存在。"""


class CameraOpenFailed(CameraError):
    """摄像头打不开（被占用 / 隐私设置禁止 / 驱动异常）。"""


class CameraBusy(CameraError):
    """已有一路拍摄正在进行。"""


class CameraCaptureFailed(CameraError):
    """打开成功但取不到有效画面，或 helper 采集失败/超时。"""


class CameraNoInteractiveSession(CameraError):
    """服务会话（Session 0）下无法访问摄像头，且没有可用的交互会话兜底。"""


class CameraRateLimited(CameraError):
    """触发冷却时间或每小时配额。"""


def _decode_console(data: bytes) -> str:
    """解码子进程输出（Windows 控制台可能是 GBK，优先 UTF-8）。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class CameraService:
    """摄像头采集服务：由 Executor 持有一个实例，串行化所有拍摄请求。"""

    def __init__(self, config: dict):
        cam = config.get("camera") or {}
        if not isinstance(cam, dict):
            cam = {}
        self.enabled: bool = bool(cam.get("enabled", False))
        self.backend: str = str(cam.get("backend", "auto") or "auto").lower()
        self.max_width: int = max(160, int(cam.get("max_width", 1920)))
        self.max_height: int = max(120, int(cam.get("max_height", 1080)))
        self.jpeg_quality: int = int(cam.get("jpeg_quality", 80))
        self.warmup_frames: int = max(0, int(cam.get("warmup_frames", 5)))
        self.burst: int = max(1, min(5, int(cam.get("burst", 1))))
        self.probe_max_index: int = max(1, int(cam.get("probe_max_index", 4)))
        self.min_interval: float = max(0.0, float(cam.get("min_interval_seconds", 5)))
        self.max_per_hour: int = max(1, int(cam.get("max_per_hour", 60)))
        self.capture_timeout: float = max(3.0, float(cam.get("capture_timeout", 15)))
        self.release_delay: float = max(0.0, float(cam.get("release_delay", 0.2)))
        self.allow_user_session: bool = bool(cam.get("allow_user_session_fallback", True))
        self.shutter_sound: bool = bool(cam.get("shutter_sound", False))
        self.archive_dir: str = str(cam.get("archive_dir") or "")
        self.archive_keep: int = max(0, int(cam.get("archive_keep", 20)))
        self.ffmpeg_path: str = str(cam.get("ffmpeg_path") or "")
        self._lock = asyncio.Lock()
        self._last_capture: float = 0.0
        self._recent: deque[float] = deque()
        self._system_devices_cache: list[dict] | None = None

    # ---------- 入口 ----------

    async def execute(self, action: str, params: dict) -> dict:
        """执行一个 camera action（list / capture / status）。"""
        if not self.enabled:
            raise CameraDisabled("摄像头功能未开启（config: camera.enabled = false）")
        handlers = {"list": self._list, "capture": self._capture, "status": self._status}
        handler = handlers.get(action)
        if handler is None:
            raise NotImplementedError(f"camera action '{action}' 未实现")
        return await handler(params or {})

    # ---------- action: status ----------

    async def _status(self, params: dict) -> dict:
        now = time.monotonic()
        self._prune_recent(now)
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "session_id": user_session.current_session_id(),
            "user_session_fallback": self.allow_user_session,
            "min_interval_seconds": self.min_interval,
            "max_per_hour": self.max_per_hour,
            "captured_last_hour": len(self._recent),
            "cooldown_remaining": round(max(0.0, self.min_interval - (now - self._last_capture)), 1),
            "archive_dir": self.archive_dir or None,
        }

    # ---------- action: list ----------

    async def _list(self, params: dict) -> dict:
        devices = await asyncio.to_thread(self._probe_devices)
        system = await asyncio.to_thread(self._system_devices)
        return {
            "count": len(devices),
            "devices": devices,
            "system_devices": system,
            "note": (
                "devices 为 OpenCV 实际可打开的设备（index 可直接用于 capture.device）；"
                "system_devices 为系统 PnP 设备名，二者顺序仅为尽力对应"
            ),
        }

    # ---------- action: capture ----------

    async def _capture(self, params: dict) -> dict:
        self._check_rate_limit()
        if self._lock.locked():
            raise CameraBusy("已有一次拍摄正在进行，请稍后再试")
        async with self._lock:
            self._mark_attempt()
            start = time.monotonic()
            shot, source = await self._take_shot(params)
            raw: bytes = shot.pop("raw")
            reason = str(params.get("reason") or "")
            # 本地留档：仅在配置了 camera.archive_dir 时生效；可用 save_local=false 跳过
            archived = None
            if bool(params.get("save_local", True)):
                archived = await asyncio.to_thread(self._archive, raw, shot.get("format", "jpeg"))
            if self.shutter_sound:
                await asyncio.to_thread(self._play_shutter_sound)
            result = {
                "image": base64.b64encode(raw).decode("ascii"),
                "format": shot.get("format", "jpeg"),
                "width": int(shot.get("width") or 0),
                "height": int(shot.get("height") or 0),
                "size": len(raw),
                "device": {
                    "index": shot.get("index"),
                    "backend": shot.get("backend"),
                    "system_name": shot.get("system_name"),
                },
                "burst": shot.get("burst", 1),
                "sharpness": shot.get("sharpness"),
                "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "elapsed": round(time.monotonic() - start, 3),
                "archived_path": archived,
            }
            LOG.info(
                "摄像头拍摄完成：index=%s %sx%s %s字节 source=%s 用时=%.2fs reason=%s",
                result["device"]["index"],
                result["width"],
                result["height"],
                result["size"],
                source,
                result["elapsed"],
                reason or "-",
            )
            return result

    async def _take_shot(self, params: dict) -> tuple[dict, str]:
        """采集一帧，返回 (帧信息含 raw 字节, source)。"""
        timeout = self._resolve_timeout(params)
        session_id = user_session.current_session_id()
        if session_id == 0:
            if not self.allow_user_session:
                raise CameraNoInteractiveSession(
                    "服务会话（Session 0）下无法访问摄像头，且已禁用交互会话兜底"
                    "（camera.allow_user_session_fallback = false）"
                )
            return await self._capture_via_user_session(params, timeout), "user-session"
        try:
            shot = await asyncio.wait_for(
                asyncio.to_thread(self._capture_direct, params), timeout=timeout
            )
            return shot, "direct"
        except asyncio.TimeoutError:
            raise CameraCaptureFailed(f"取帧超时（{timeout:.0f} 秒）") from None
        except CameraBackendUnavailable:
            if self.backend == "opencv":
                raise
            return await self._capture_via_ffmpeg(params), "ffmpeg"
        except CameraError:
            raise
        except Exception as e:  # noqa: BLE001 —— 底层异常统一翻译为可判定的错误码
            raise CameraOpenFailed(f"摄像头采集异常：{e}") from None

    # ---------- 直采（OpenCV） ----------

    def _capture_direct(self, params: dict) -> dict:
        """同步采集一帧（在线程中执行，避免阻塞事件循环）。"""
        cv2 = self._import_cv2()
        index = self._resolve_index(params)
        width, height = self._resolve_size(params)
        quality = self._resolve_quality(params)
        fmt, ext = self._resolve_format(params)
        burst = self._resolve_burst(params)
        warmup = max(0, int(self._pick(params, "warmup_frames", self.warmup_frames)))

        cap, backend = self._open_capture(cv2, index)
        if cap is None:
            raise CameraOpenFailed(
                f"无法打开摄像头 index={index}：可能被其他程序占用，"
                "或被系统「隐私和安全性 → 相机」设置为禁止桌面应用访问"
            )
        try:
            if width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
            if height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
            # 丢弃预热帧：等自动曝光/自动对焦稳定，否则首帧常偏暗偏糊
            for _ in range(warmup):
                cap.read()
            frames = []
            for _ in range(burst):
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
            if not frames:
                raise CameraCaptureFailed("摄像头已打开但读不到画面（可能被遮挡或设备异常）")
            best = max(frames, key=self._sharpness) if len(frames) > 1 else frames[0]
            if bool(self._pick(params, "mirror", False)):
                best = cv2.flip(best, 1)
            height_px, width_px = best.shape[:2]
            if fmt == "png":
                ok, buf = cv2.imencode(ext, best)
            else:
                ok, buf = cv2.imencode(ext, best, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if not ok:
                raise CameraCaptureFailed("图像编码失败")
            return {
                "raw": buf.tobytes(),
                "format": fmt,
                "width": int(width_px),
                "height": int(height_px),
                "index": index,
                "backend": backend,
                "system_name": self._system_name_of(index),
                "burst": len(frames),
                "sharpness": round(self._sharpness(best), 1),
            }
        finally:
            cap.release()
            if self.release_delay:
                time.sleep(self.release_delay)

    def _import_cv2(self):
        """惰性导入 OpenCV（未安装时给出明确错误，不影响其它指令）。"""
        if self.backend == "ffmpeg":
            raise CameraBackendUnavailable("camera.backend = ffmpeg，跳过 OpenCV")
        try:
            import cv2  # noqa: PLC0415 —— 惰性导入：未装 OpenCV 时其它指令照常工作
        except Exception as e:  # noqa: BLE001
            raise CameraBackendUnavailable(
                f"未安装 OpenCV（opencv-python-headless），无法打开摄像头：{e}"
            ) from None
        return cv2

    def _open_capture(self, cv2, index: int):
        """按 DSHOW → MSMF → 默认 顺序尝试打开设备，返回 (cap, 后端名)。"""
        if self.backend not in ("auto", "opencv"):
            return None, None
        candidates = [
            ("dshow", getattr(cv2, "CAP_DSHOW", 700)),
            ("msmf", getattr(cv2, "CAP_MSMF", 1400)),
            ("default", cv2.CAP_ANY),
        ]
        for name, flag in candidates:
            try:
                cap = cv2.VideoCapture(index, flag)
            except Exception:  # noqa: BLE001 —— 某些后端在非法 index 上直接抛异常
                continue
            if cap is not None and cap.isOpened():
                return cap, name
            if cap is not None:
                cap.release()
        return None, None

    @staticmethod
    def _sharpness(frame) -> float:
        """拉普拉斯方差：值越大越清晰，用于连拍选帧。"""
        try:
            import cv2
            import numpy as np

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            return float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except Exception:  # noqa: BLE001
            return 0.0

    def _probe_devices(self) -> list[dict]:
        """逐个 index 试开探测可用摄像头（同步，线程中执行）。"""
        cv2 = self._import_cv2()
        found: list[dict] = []
        for index in range(self.probe_max_index):
            cap, backend = self._open_capture(cv2, index)
            if cap is None:
                continue
            try:
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                height_px, width_px = frame.shape[:2]
                found.append(
                    {
                        "index": index,
                        "openable": True,
                        "width": int(width_px),
                        "height": int(height_px),
                        "backend": backend,
                        "system_name": self._system_name_of(index),
                    }
                )
            finally:
                cap.release()
                if self.release_delay:
                    time.sleep(self.release_delay)
        return found

    # ---------- 兜底：交互用户会话 ----------

    async def _capture_via_user_session(self, params: dict, timeout: float) -> dict:
        """Session 0 下切到交互用户执行一次采集（与截屏同一套机制）。"""
        in_json = user_session.temp_path(".json", "cherry_cam_in")
        out_img = user_session.temp_path(".jpg", "cherry_cam_out")
        out_meta = out_img + ".meta.json"
        payload = {k: params[k] for k in _HELPER_KEYS if params.get(k) is not None}
        payload.setdefault("backend", self.backend)
        Path(in_json).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            try:
                await user_session.run_in_user_session(
                    ["--camera-helper", in_json, out_img, out_meta],
                    out_img,
                    timeout + 10,
                    tag="cherry_cam",
                )
            except UserSessionUnavailable as e:
                raise CameraNoInteractiveSession(
                    f"服务会话（Session 0）下无法访问摄像头，且没有可用的交互会话兜底：{e}"
                ) from None
            except HelperTimeout as e:
                raise CameraCaptureFailed(str(e)) from None

            raw = Path(out_img).read_bytes()
            meta: dict = {}
            if os.path.isfile(out_meta):
                try:
                    meta = json.loads(Path(out_meta).read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 —— 元信息损坏不影响图片本身
                    meta = {}
            if meta.get("error"):
                raise self._helper_error(meta)
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            if not width or not height:
                from PIL import Image

                width, height = Image.open(io.BytesIO(raw)).size
            return {
                "raw": raw,
                "format": meta.get("format", "jpeg"),
                "width": width,
                "height": height,
                "index": meta.get("index"),
                "backend": meta.get("backend", "opencv(helper)"),
                "system_name": meta.get("system_name"),
                "burst": meta.get("burst", 1),
                "sharpness": meta.get("sharpness"),
            }
        finally:
            user_session.cleanup_paths(in_json, out_img, out_meta)

    @staticmethod
    def _helper_error(meta: dict) -> CameraError:
        """把 helper 回传的错误码还原成对应异常类。"""
        code = str(meta.get("error_code") or "")
        cls = globals().get(code)
        if not isinstance(cls, type) or not issubclass(cls, CameraError):
            cls = CameraCaptureFailed
        return cls(str(meta.get("error") or "交互会话采集失败"))

    # ---------- 兜底：ffmpeg ----------

    async def _capture_via_ffmpeg(self, params: dict) -> dict:
        return await asyncio.to_thread(self._capture_ffmpeg_sync, params)

    def _capture_ffmpeg_sync(self, params: dict) -> dict:
        exe = self.ffmpeg_path or shutil.which("ffmpeg")
        if not exe:
            raise CameraBackendUnavailable(
                "未安装 OpenCV 且系统中未找到 ffmpeg，无法访问摄像头"
                "（可安装 opencv-python-headless，或在 camera.ffmpeg_path 指定 ffmpeg 路径）"
            )
        name = params.get("device")
        if not isinstance(name, str) or name.strip().isdigit():
            devices = self._system_devices()
            if not devices:
                raise CameraNotFound("未找到摄像头设备（系统 PnP 无 Camera/Image 类设备）")
            name = devices[0]["name"]
        quality = self._resolve_quality(params)
        # ffmpeg 的 JPEG 质量是 2（最好）~31（最差），按质量百分比线性映射
        qscale = max(2, min(31, int(round(31 - quality * 0.29))))
        out = user_session.temp_path(".jpg", "cherry_ffmpeg")
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-f", "dshow",
            "-i", f"video={name}", "-frames:v", "1", "-q:v", str(qscale), "-y", out,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.capture_timeout)
            if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
                detail = _decode_console(proc.stderr or b"").strip()[:300]
                raise CameraOpenFailed(f"ffmpeg 采集失败：{detail or '无输出'}")
            raw = Path(out).read_bytes()
            from PIL import Image

            width, height = Image.open(io.BytesIO(raw)).size
            return {
                "raw": raw,
                "format": "jpeg",
                "width": int(width),
                "height": int(height),
                "index": None,
                "backend": "ffmpeg",
                "system_name": name,
                "burst": 1,
                "sharpness": None,
            }
        finally:
            user_session.cleanup_paths(out)

    # ---------- 设备信息（系统 PnP 名称） ----------

    def _system_devices(self) -> list[dict]:
        """枚举系统里的摄像头设备（Windows PnP），结果缓存。"""
        if self._system_devices_cache is not None:
            return self._system_devices_cache
        self._system_devices_cache = self._query_system_devices()
        return self._system_devices_cache

    @staticmethod
    def _query_system_devices() -> list[dict]:
        if os.name != "nt":
            return []
        script = (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
            "Select-Object Name,PNPClass,Status | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                timeout=20,
            )
            data = json.loads(_decode_console(proc.stdout).strip() or "[]")
        except Exception as e:  # noqa: BLE001 —— 取不到设备名不影响拍照主流程
            LOG.debug("查询系统摄像头设备失败（忽略）: %s", e)
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return [
            {
                "name": str(item.get("Name") or "").strip(),
                "class": str(item.get("PNPClass") or "").strip(),
                "status": str(item.get("Status") or "").strip(),
            }
            for item in data
            if isinstance(item, dict) and item.get("Name")
        ]

    def _system_name_of(self, index: int) -> str | None:
        devices = self._system_devices()
        if 0 <= index < len(devices):
            return devices[index]["name"]
        return None

    # ---------- 参数解析（全部做边界夹取，避免非法参数把设备打挂） ----------

    def _resolve_index(self, params: dict) -> int:
        device = params.get("device", 0)
        if isinstance(device, bool):
            device = int(device)
        if isinstance(device, int):
            if device < 0:
                raise CameraNotFound(f"摄像头 index 不能为负数：{device}")
            return device
        text = str(device or "").strip()
        if not text:
            return 0
        if text.lstrip("-").isdigit():
            index = int(text)
            if index < 0:
                raise CameraNotFound(f"摄像头 index 不能为负数：{index}")
            return index
        # 传了名称：按系统设备名尽力匹配（顺序对应关系为尽力而为）
        devices = self._system_devices()
        for i, item in enumerate(devices):
            if text.lower() in item["name"].lower():
                return i
        names = "、".join(d["name"] for d in devices) or "（未识别到设备名）"
        raise CameraNotFound(f"未找到名称包含「{text}」的摄像头，已知设备：{names}；建议先用 camera.list 查 index")

    def _resolve_size(self, params: dict) -> tuple[int, int]:
        width = int(self._pick(params, "width", 1280) or 0)
        height = int(self._pick(params, "height", 720) or 0)
        width = max(0, min(width, self.max_width))
        height = max(0, min(height, self.max_height))
        return width, height

    def _resolve_quality(self, params: dict) -> int:
        quality = int(self._pick(params, "quality", self.jpeg_quality) or self.jpeg_quality)
        return max(30, min(95, quality))

    @staticmethod
    def _resolve_format(params: dict) -> tuple[str, str]:
        fmt = str(params.get("format") or "jpeg").strip().lower()
        if fmt in ("jpg", "jpeg"):
            return "jpeg", ".jpg"
        if fmt == "png":
            return "png", ".png"
        return "jpeg", ".jpg"

    def _resolve_burst(self, params: dict) -> int:
        burst = int(self._pick(params, "burst", self.burst) or 1)
        return max(1, min(5, burst))

    def _resolve_timeout(self, params: dict) -> float:
        """单次拍摄超时：params.timeout 覆盖配置，夹取到 3~60 秒。"""
        try:
            timeout = float(self._pick(params, "timeout", self.capture_timeout) or self.capture_timeout)
        except (TypeError, ValueError):
            timeout = self.capture_timeout
        return max(3.0, min(60.0, timeout))

    @staticmethod
    def _pick(params: dict, key: str, default):
        value = params.get(key)
        return default if value is None else value

    # ---------- 限流 / 留档 / 提示音 ----------

    def _prune_recent(self, now: float) -> None:
        hour_ago = now - 3600
        while self._recent and self._recent[0] < hour_ago:
            self._recent.popleft()

    def _check_rate_limit(self) -> None:
        now = time.monotonic()
        if self.min_interval and now - self._last_capture < self.min_interval:
            wait = self.min_interval - (now - self._last_capture)
            raise CameraRateLimited(
                f"拍摄过于频繁，请在 {wait:.0f} 秒后重试"
                f"（camera.min_interval_seconds = {self.min_interval:g}）"
            )
        self._prune_recent(now)
        if len(self._recent) >= self.max_per_hour:
            raise CameraRateLimited(f"已达到每小时拍摄上限（camera.max_per_hour = {self.max_per_hour}）")

    def _mark_attempt(self) -> None:
        """记录一次拍摄尝试（含失败尝试，避免失败重试绕过限流）。"""
        now = time.monotonic()
        self._last_capture = now
        self._recent.append(now)

    def _archive(self, raw: bytes, fmt: str) -> str | None:
        """按配置在本地留档一份（便于事后核对 AI 拍到了什么）。"""
        if not self.archive_dir or self.archive_keep <= 0:
            return None
        try:
            directory = Path(self.archive_dir)
            directory.mkdir(parents=True, exist_ok=True)
            ext = ".png" if fmt == "png" else ".jpg"
            stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
            path = directory / f"camera_{stamp}{ext}"
            path.write_bytes(raw)
            shots = sorted(directory.glob("camera_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in shots[self.archive_keep :]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return str(path)
        except Exception as e:  # noqa: BLE001 —— 留档失败不影响回传
            LOG.warning("摄像头留档失败（忽略）: %s", e)
            return None

    @staticmethod
    def _play_shutter_sound() -> None:
        """播放可听提示音（给在场者一个"正在拍照"的提示）。"""
        if os.name != "nt":
            return
        try:
            import winsound

            winsound.Beep(1000, 120)
        except Exception:  # noqa: BLE001
            pass
