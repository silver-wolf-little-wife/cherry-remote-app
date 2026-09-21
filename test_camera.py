# -*- coding: utf-8 -*-
"""camera 工具集回归测试（默认不依赖真实摄像头：注入假采集后端）。

覆盖：白名单 / 总开关 / 无设备 / 正常采集 / 冷却 / 小时配额 / 参数夹取 /
并发互斥 / 后端异常翻译 / Session 0 路由 / 留档 / system.status。

用法：
    python test_camera.py          # 假后端（CI 与无摄像头机器均可全绿）
    python test_camera.py --real   # 额外跑一条真实摄像头采集（需要设备）
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import websockets  # noqa: E402

from cherry_remote_app import user_session  # noqa: E402
from cherry_remote_app.camera import (  # noqa: E402
    CameraBackendUnavailable,
    CameraBusy,
    CameraDisabled,
    CameraNoInteractiveSession,
    CameraNotFound,
    CameraOpenFailed,
    CameraRateLimited,
    CameraService,
)
from cherry_remote_app.executor import Executor  # noqa: E402

RESULTS: dict[str, bool] = {}
TMP = tempfile.mkdtemp(prefix="cherry_camera_")


def _fake_jpeg() -> bytes:
    """生成一张极小的真 JPEG（供假后端回传，验证 base64 链路）。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 2), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _cam_config(**overrides) -> dict:
    cam = {
        "enabled": True,
        "backend": "opencv",
        "min_interval_seconds": 0,
        "max_per_hour": 1000,
        "warmup_frames": 0,
        "allow_user_session_fallback": False,
    }
    cam.update(overrides)
    return {"camera": cam}


class FakeCamera(CameraService):
    """假采集后端：完全绕开真实设备与系统命令。"""

    def __init__(self, config, error: Exception | None = None, delay: float = 0.0, session_id: int = 1):
        super().__init__(config)
        self.error = error
        self.delay = delay
        self.session_id = session_id
        self.devices: list[dict] = []
        self.system_devices: list[dict] = []
        self.seen_params: dict = {}
        self.user_session_calls = 0

    # --- 覆盖真实后端 ---
    def _capture_direct(self, params: dict) -> dict:
        self.seen_params = dict(params)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return {
            "raw": _fake_jpeg(),
            "format": "jpeg",
            "width": 4,
            "height": 2,
            "index": int(params.get("device") or 0),
            "backend": "dshow",
            "system_name": "Fake Camera",
            "burst": int(params.get("burst") or 1),
            "sharpness": 12.5,
        }

    def _probe_devices(self) -> list[dict]:
        return list(self.devices)

    def _system_devices(self) -> list[dict]:
        return list(self.system_devices)

    async def _capture_via_user_session(self, params: dict, timeout: float) -> dict:
        self.user_session_calls += 1
        return {
            "raw": _fake_jpeg(),
            "format": "jpeg",
            "width": 4,
            "height": 2,
            "index": 0,
            "backend": "opencv(helper)",
            "system_name": "Fake Camera",
            "burst": 1,
            "sharpness": 1.0,
        }


async def run_fake_tests() -> None:
    # 1) 白名单：未列入 allowed_actions 时 Executor 直接拒绝
    ex = Executor(
        {
            "allowed_actions": ["exec"],
            "build_exe_index": False,
            "camera": {"enabled": True},
        }
    )
    try:
        await ex.execute("camera", {})
        RESULTS["allowlist_reject"] = False
    except PermissionError:
        RESULTS["allowlist_reject"] = True

    # 2) 总开关：enabled=false 一律拒绝
    service = FakeCamera(_cam_config(enabled=False))
    try:
        await service.execute("capture", {})
        RESULTS["disabled_reject"] = False
    except CameraDisabled as e:
        RESULTS["disabled_reject"] = "camera.enabled" in str(e)

    # 3) list：无摄像头时返回空列表且不崩
    service = FakeCamera(_cam_config())
    listed = await service.execute("list", {})
    RESULTS["list_empty"] = listed["count"] == 0 and listed["devices"] == []

    # 4) 正常采集：base64 可解、JPEG magic、元数据齐全、source=direct
    service = FakeCamera(_cam_config())
    shot = await service.execute("capture", {"device": 0, "reason": "回归测试"})
    raw = base64.b64decode(shot["image"])
    RESULTS["capture_ok"] = (
        raw[:2] == b"\xff\xd8"
        and shot["format"] == "jpeg"
        and shot["width"] == 4
        and shot["height"] == 2
        and shot["size"] == len(raw)
        and shot["source"] == "direct"
        and shot["device"]["backend"] == "dshow"
    )

    # 5) 冷却：min_interval_seconds 内第二次立即拒绝
    service = FakeCamera(_cam_config(min_interval_seconds=5))
    await service.execute("capture", {})
    try:
        await service.execute("capture", {})
        RESULTS["rate_limit_interval"] = False
    except CameraRateLimited as e:
        RESULTS["rate_limit_interval"] = "min_interval_seconds" in str(e)

    # 6) 小时配额：超过 max_per_hour 拒绝
    service = FakeCamera(_cam_config(max_per_hour=2))
    await service.execute("capture", {})
    await service.execute("capture", {})
    try:
        await service.execute("capture", {})
        RESULTS["rate_limit_hourly"] = False
    except CameraRateLimited as e:
        RESULTS["rate_limit_hourly"] = "max_per_hour" in str(e)

    # 7) 参数夹取：分辨率/质量/连拍全部限幅
    service = FakeCamera(_cam_config(max_width=1920, max_height=1080, jpeg_quality=80, burst=1))
    size = service._resolve_size({"width": 9999, "height": 9999})
    quality = service._resolve_quality({"quality": 200})
    low_quality = service._resolve_quality({"quality": 1})
    burst = service._resolve_burst({"burst": 99})
    RESULTS["param_clamp"] = size == (1920, 1080) and quality == 95 and low_quality == 30 and burst == 5

    # 8) 并发互斥：拍摄进行中再次请求 → CameraBusy（快速失败，不排队）
    service = FakeCamera(_cam_config(), delay=0.6)
    first = asyncio.create_task(service.execute("capture", {}))
    await asyncio.sleep(0.15)
    try:
        await service.execute("capture", {})
        busy = False
    except CameraBusy:
        busy = True
    await first
    RESULTS["concurrent_busy"] = busy

    # 9) 后端异常翻译：非 CameraError 的异常统一为 CameraOpenFailed
    service = FakeCamera(_cam_config(), error=OSError("device is busy"))
    try:
        await service.execute("capture", {})
        RESULTS["error_translate"] = False
    except CameraOpenFailed as e:
        RESULTS["error_translate"] = "device is busy" in str(e)
    except Exception:
        RESULTS["error_translate"] = False

    # 10) Session 0：禁用兜底 → CameraNoInteractiveSession
    service = FakeCamera(_cam_config(allow_user_session_fallback=False), session_id=0)
    original_session = user_session.current_session_id
    user_session.current_session_id = lambda: 0
    try:
        try:
            await service.execute("capture", {})
            RESULTS["session0_no_fallback"] = False
        except CameraNoInteractiveSession:
            RESULTS["session0_no_fallback"] = True
    finally:
        user_session.current_session_id = original_session

    # 11) Session 0：允许兜底 → 走交互会话路径（source=user-session）
    service = FakeCamera(_cam_config(allow_user_session_fallback=True), session_id=0)
    user_session.current_session_id = lambda: 0
    try:
        shot = await service.execute("capture", {})
        RESULTS["session0_fallback"] = shot["source"] == "user-session" and service.user_session_calls == 1
    finally:
        user_session.current_session_id = original_session

    # 12) 设备名匹配：命中 → index；未命中 → CameraNotFound
    service = FakeCamera(_cam_config())
    service.system_devices = [{"name": "Integrated Camera", "class": "Camera", "status": "OK"}]
    matched = service._resolve_index({"device": "integrated"})
    try:
        service._resolve_index({"device": "不存在的摄像头"})
        missing = False
    except CameraNotFound as e:
        missing = "Integrated Camera" in str(e)
    RESULTS["device_name_match"] = matched == 0 and missing

    # 13) 留档：archive_dir 生效且产出文件
    archive_dir = os.path.join(TMP, "archive")
    service = FakeCamera(_cam_config(archive_dir=archive_dir, archive_keep=2))
    shot = await service.execute("capture", {})
    archived = shot.get("archived_path")
    RESULTS["archive"] = bool(archived) and os.path.isfile(archived)
    # 超过保留数时裁剪最旧
    await asyncio.sleep(1.1)
    await service.execute("capture", {})
    await asyncio.sleep(1.1)
    await service.execute("capture", {})
    kept = [p for p in os.listdir(archive_dir) if p.startswith("camera_")]
    RESULTS["archive_keep"] = len(kept) <= 2
    # save_local=false 时不留档
    shot = await service.execute("capture", {"save_local": False})
    RESULTS["archive_skip"] = shot.get("archived_path") is None

    # 14) 缺 OpenCV 时的兜底错误：backend=opencv 且无 cv2 → CameraBackendUnavailable
    service = CameraService(_cam_config(ffmpeg_path="", backend="opencv"))
    service._import_cv2 = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        CameraBackendUnavailable("未安装 OpenCV")
    )
    try:
        await service.execute("capture", {})
        RESULTS["backend_unavailable"] = False
    except CameraBackendUnavailable:
        RESULTS["backend_unavailable"] = True
    except CameraOpenFailed:
        RESULTS["backend_unavailable"] = False

    # 15) Executor 集成：默认 action=capture，且 system.status 暴露摄像头开关
    ex = Executor(
        {
            "allowed_actions": ["camera", "system"],
            "build_exe_index": False,
            "system": {},
            **_cam_config(),
        }
    )
    ex.camera = FakeCamera(_cam_config())
    shot = await ex.execute("camera", {})
    status = await ex.execute("system", {"action": "status"})
    RESULTS["executor_integration"] = bool(shot.get("image")) and status.get("camera_enabled") is True


async def run_real_test() -> None:
    """真实摄像头采集（--real）。"""
    service = CameraService(_cam_config(min_interval_seconds=0))
    try:
        listed = await service.execute("list", {})
        print(f"[INFO] camera.list → {json.dumps(listed, ensure_ascii=False)}")
        if listed["count"] == 0:
            RESULTS["real_capture"] = False
            print("[INFO] 未探测到可用摄像头")
            return
        shot = await service.execute("capture", {"width": 640, "height": 480})
        raw = base64.b64decode(shot["image"])
        RESULTS["real_capture"] = raw[:2] == b"\xff\xd8" and shot["width"] > 0 and shot["height"] > 0
        print(
            f"[INFO] 采集成功 {shot['width']}x{shot['height']} {shot['size']}字节 "
            f"source={shot['source']} backend={shot['device']['backend']}"
        )
    except Exception as e:  # noqa: BLE001
        RESULTS["real_capture"] = False
        print(f"[INFO] 真实采集失败（可能被占用或隐私设置禁止）: {e}")


async def run_wire_test() -> None:
    """WS 全链路：假 B 端下发 camera 指令，验证响应帧结构与 error.code 映射。"""
    from cherry_remote_app.ws_client import WsClient

    server_url = "ws://127.0.0.1:9998/ws"
    token = "camera-test-token"

    async def handler(ws):
        hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "hello_ack", "ok": True, "session_id": "t"}))

        async def send_request(req_id, method, params):
            await ws.send(json.dumps({"type": "request", "id": req_id, "method": method, "params": params}))

        async def recv_response():
            while True:
                msg = json.loads(await ws.recv())
                if msg["type"] == "response":
                    return msg

        # 关闭摄像头 → 必须回 CameraDisabled
        await send_request("c1-off", "camera", {})
        resp = await recv_response()
        RESULTS["wire_disabled_error_code"] = (
            resp["ok"] is False and (resp.get("error") or {}).get("code") == "CameraDisabled"
        )

        # 开启摄像头（假后端）→ 回 base64 图片与元数据
        enabled = FakeCamera(_cam_config())
        client.executor.camera = enabled
        await send_request("c2-shot", "camera", {"device": 0, "width": 640, "height": 480})
        resp = await recv_response()
        data = resp.get("data") or {}
        raw = base64.b64decode(data.get("image") or "")
        RESULTS["wire_capture_ok"] = (
            resp["ok"] is True
            and raw[:2] == b"\xff\xd8"
            and data.get("width") == 4
            and data.get("source") == "direct"
        )

        # camera.list 全链路
        await send_request("c3-list", "camera", {"action": "list"})
        resp = await recv_response()
        RESULTS["wire_list"] = resp["ok"] is True and resp["data"]["count"] == 0
        await ws.close()

    config = {
        "server_url": server_url,
        "auth_token": token,
        "device_id": "camera-test",
        "heartbeat_interval": 30,
        "max_reconnect_delay": 5,
        "build_exe_index": False,
        "allowed_actions": ["camera", "system"],
        "camera": {"enabled": False},
    }
    async with websockets.serve(handler, "127.0.0.1", 9998, max_size=16 * 1024 * 1024):
        client = WsClient(config)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def main() -> int:
    await run_fake_tests()
    await run_wire_test()
    if "--real" in sys.argv:
        await run_real_test()

    print("\n===== CAMERA TEST REPORT =====")
    all_ok = True
    for key in sorted(RESULTS):
        ok = RESULTS[key]
        print(f"[{'PASS' if ok else 'FAIL'}] {key}")
        all_ok = all_ok and ok
    print("===== " + ("ALL PASS" if all_ok else "HAS FAILURES") + " =====")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
