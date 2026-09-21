# -*- coding: utf-8 -*-
"""交互会话 helper 入口：在用户会话中执行一次采集，把产物写到文件后退出。

本模块由 `user_session.run_in_user_session` 通过计划任务在**交互用户身份**下启动：

    <程序> --camera-helper <in.json> <out_img> <out_meta>
    <程序> --screenshot-helper <out_png>

打包形态由 exe 自身转发，源码形态由临时引导脚本导入本模块，
因此不依赖目标机上另外安装 Python 或 OpenCV。
"""

import json
import logging
import os
import sys
from pathlib import Path

LOG = logging.getLogger("cherry-remote-app.helper")

_USAGE = (
    "helper 用法：--camera-helper <in.json> <out_img> <out_meta> | "
    "--screenshot-helper <out_png>"
)


def main(argv: list[str]) -> int:
    """helper 入口，返回进程退出码。"""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "--camera-helper":
        if len(rest) < 3:
            print(_USAGE, file=sys.stderr)
            return 2
        return _camera_helper(rest[0], rest[1], rest[2])
    if command == "--screenshot-helper":
        if len(rest) < 1:
            print(_USAGE, file=sys.stderr)
            return 2
        return _screenshot_helper(rest[0])
    print(_USAGE, file=sys.stderr)
    return 2


def _camera_helper(in_json: str, out_img: str, out_meta: str) -> int:
    """按 JSON 参数拍一帧，原图写 out_img、元信息写 out_meta。"""
    from .camera import CameraError, CameraService

    try:
        params = json.loads(Path(in_json).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return _write_meta(out_meta, {"error": f"读取 helper 参数失败: {e}", "error_code": "CameraCaptureFailed"})

    # helper 内不做限流与会话判断（父进程已完成），只负责取帧
    service = CameraService(
        {
            "camera": {
                "enabled": True,
                "min_interval_seconds": 0,
                "max_per_hour": 1_000_000,
                "allow_user_session_fallback": False,
                "archive_dir": "",
                "shutter_sound": False,
            }
        }
    )
    try:
        shot = service._capture_direct(params)  # noqa: SLF001 —— helper 复用同一套采集逻辑
    except CameraError as e:
        LOG.warning("helper 采集失败: %s", e)
        return _write_meta(out_meta, {"error": str(e), "error_code": type(e).__name__})
    except Exception as e:  # noqa: BLE001
        LOG.exception("helper 采集异常")
        return _write_meta(out_meta, {"error": str(e), "error_code": "CameraCaptureFailed"})

    raw = shot.pop("raw")
    # 先写临时图片与元信息，最后一步 rename：父进程以 out_img 出现作为"全部就绪"标志
    part = out_img + ".part"
    Path(part).write_bytes(raw)
    _write_meta(
        out_meta,
        {
            "format": shot.get("format", "jpeg"),
            "width": shot.get("width"),
            "height": shot.get("height"),
            "index": shot.get("index"),
            "backend": f"{shot.get('backend')}(helper)",
            "system_name": shot.get("system_name"),
            "burst": shot.get("burst"),
            "sharpness": shot.get("sharpness"),
        },
    )
    os.replace(part, out_img)
    return 0


def _screenshot_helper(out_png: str) -> int:
    """在交互会话中截取完整桌面（多屏合成），写 PNG。"""
    try:
        from PIL import ImageGrab

        ImageGrab.grab(all_screens=True).save(out_png, "PNG")
        return 0
    except Exception:  # noqa: BLE001
        LOG.exception("helper 截屏失败")
        return 1


def _write_meta(path: str, meta: dict) -> int:
    try:
        Path(path).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return 1 if meta.get("error") else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
