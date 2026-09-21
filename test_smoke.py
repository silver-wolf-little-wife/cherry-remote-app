# -*- coding: utf-8 -*-
"""C 端 Executor 全方法冒烟测试：验证 v1.2.0 补丁未破坏既有指令。"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from cherry_remote_app.executor import Executor  # noqa: E402

CONFIG = {
    "allowed_actions": ["exec", "sys", "ping", "file", "file_pull", "app", "screenshot", "camera", "system"],
    "build_exe_index": False,
    "exe_index_file": "exe_index.json",
    "default_timeout": 30,
    "max_pull_size": 200 * 1024 * 1024,
}

TMP = tempfile.mkdtemp(prefix="cherry_smoke_")
RESULTS = {}


async def main():
    ex = Executor(CONFIG)

    # ping
    r = await ex.execute("ping", {})
    RESULTS["ping"] = r.get("pong") is True

    # sys
    r = await ex.execute("sys", {})
    RESULTS["sys"] = bool(r.get("hostname")) and "percent" in (r.get("cpu") or {})

    # exec（写文件并回读，验证 exec + file 链路）
    probe = os.path.join(TMP, "probe.txt")
    r = await ex.execute("exec", {"command": f"echo smoke-test > \"{probe}\""})
    RESULTS["exec"] = r.get("exit_code") == 0 and os.path.exists(probe)

    # file.list / info / read / write / copy / delete
    r = await ex.execute("file", {"action": "write", "path": os.path.join(TMP, "w.txt"), "content": "hello"})
    RESULTS["file_write"] = r.get("ok")
    r = await ex.execute("file", {"action": "read", "path": os.path.join(TMP, "w.txt")})
    RESULTS["file_read"] = r.get("content") == "hello"
    r = await ex.execute("file", {"action": "info", "path": TMP})
    RESULTS["file_info"] = r.get("type") == "dir"
    r = await ex.execute("file", {"action": "copy", "path": os.path.join(TMP, "w.txt"), "dest": os.path.join(TMP, "c.txt")})
    RESULTS["file_copy"] = r.get("ok") and os.path.exists(os.path.join(TMP, "c.txt"))
    r = await ex.execute("file", {"action": "delete", "path": os.path.join(TMP, "c.txt")})
    RESULTS["file_delete"] = r.get("ok") and not os.path.exists(os.path.join(TMP, "c.txt"))
    r = await ex.execute("file", {"action": "list", "path": TMP})
    RESULTS["file_list"] = isinstance(r.get("entries"), list)

    # app.search（索引不存在时不应崩溃）
    r = await ex.execute("app", {"action": "search", "query": "python"})
    RESULTS["app_search"] = isinstance(r, dict)

    # screenshot（真实截屏，验证补丁未破坏）
    r = await ex.execute("screenshot", {})
    RESULTS["screenshot"] = bool(r.get("image"))

    # camera：默认关闭（配置无 camera 段）时必须拒绝，采集逻辑见 test_camera.py
    try:
        await ex.execute("camera", {})
        RESULTS["camera_disabled"] = False
    except Exception as e:  # noqa: BLE001
        RESULTS["camera_disabled"] = type(e).__name__ == "CameraDisabled"

    # system.status
    r = await ex.execute("system", {"action": "status"})
    RESULTS["system_status"] = r.get("status") == "ok"

    # 白名单外指令应被拒
    try:
        await ex.execute("rm_rf", {})
        RESULTS["allowlist"] = False
    except PermissionError:
        RESULTS["allowlist"] = True

    print("\n===== SMOKE TEST REPORT =====")
    all_ok = True
    for k in sorted(RESULTS):
        v = RESULTS[k]
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
        all_ok = all_ok and v
    print("===== " + ("ALL PASS" if all_ok else "HAS FAILURES") + " =====")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
