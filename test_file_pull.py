# -*- coding: utf-8 -*-
"""C 端 file_pull 全链路模拟测试（本地假 B 端 + 真实 WsClient）。

覆盖场景：
  T1 file.info / file.read 小文件单帧读取正常
  T2 file_pull 大文件流式分块：index 连续、total 正确、sha256 与源文件一致
  T3 file_pull 不存在的文件 -> ok=False
  T4 file_pull 超 max_pull_size -> 拒绝
  T5 普通 method（ping）不受 file_pull 改造影响
"""
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import websockets  # noqa: E402

from cherry_remote_app.executor import Executor  # noqa: E402
from cherry_remote_app.ws_client import WsClient  # noqa: E402

SRV = "ws://127.0.0.1:9999/ws"
TOKEN = "test-token"
RESULTS = {}


def make_file(path: str, size: int) -> str:
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    return path


def sha_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def server_handler(ws):
    # ---- 握手 ----
    hello = json.loads(await ws.recv())
    assert hello["type"] == "hello", "握手帧类型错误"
    assert hello["token"] == TOKEN, "token 不匹配"
    # 只校验版本字段存在：避免每次 C 端升版本都要改测试（历史上这里写死 1.2.0 已失效）
    assert hello.get("client_version"), "client_version 缺失"
    await ws.send(
        json.dumps({"type": "hello_ack", "ok": True, "session_id": "test", "server_version": "1.2.0"})
    )
    RESULTS["T0_handshake"] = f"PASS (client_version={hello['client_version']})"

    def send_request(req_id, method, params):
        return ws.send(json.dumps({"type": "request", "id": req_id, "method": method, "params": params}))

    async def recv_response():
        while True:
            m = json.loads(await ws.recv())
            if m["type"] == "response":
                return m

    # ---- T1: file.info + file.read（小文件单帧路径的 C 端侧）----
    await send_request("t1-info", "file", {"action": "info", "path": SMALL})
    r = await recv_response()
    assert r["id"] == "t1-info" and r["ok"], f"file.info 失败: {r}"
    assert r["data"]["size"] == SMALL_SIZE, "info size 不匹配"
    await send_request("t1-read", "file", {"action": "read", "path": SMALL})
    r = await recv_response()
    assert r["ok"] and r["data"]["encoding"] == "base64", "file.read 应为 base64"
    content = base64.b64decode(r["data"]["content"])
    assert hashlib.sha256(content).hexdigest() == sha_of(SMALL), "file.read 内容校验失败"
    RESULTS["T1_single_read"] = "PASS"

    # ---- T2: file_pull 大文件流式 ----
    await send_request("t2-pull", "file_pull", {"path": BIG, "chunk_size": 1024 * 1024})
    chunks = {}
    total = None
    while True:
        m = json.loads(await ws.recv())
        if m["type"] == "file_data":
            assert m["id"] == "t2-pull", "file_data 帧 id 未关联请求"
            total = m["total"]
            assert m["index"] == len(chunks), f"分块乱序: 期望 {len(chunks)} 实际 {m['index']}"
            chunks[m["index"]] = base64.b64decode(m["data"])
        elif m["type"] == "response":
            meta = m
            break
    assert meta["ok"], f"file_pull 失败: {meta}"
    assert total == meta["data"]["chunks"], "total/chunks 不一致"
    assert len(chunks) == total, f"分块缺失: {len(chunks)}/{total}"
    recombined = b"".join(chunks[i] for i in range(total))
    assert hashlib.sha256(recombined).hexdigest() == sha_of(BIG), "重组文件 sha256 与源文件不一致"
    assert meta["data"]["sha256"] == sha_of(BIG), "返回 sha256 与源文件不一致"
    RESULTS["T2_stream_pull"] = f"PASS ({total} chunks, {len(recombined)} bytes)"

    # ---- T3: file_pull 不存在的文件 ----
    await send_request("t3-miss", "file_pull", {"path": os.path.join(TMP, "no_such_file.bin")})
    r = await recv_response()
    assert not r["ok"], "不存在的文件应返回失败"
    RESULTS["T3_missing_file"] = "PASS"

    # ---- T5: 普通 method 不受影响 ----
    await send_request("t5-ping", "ping", {})
    r = await recv_response()
    assert r["ok"] and (r["data"] or {}).get("pong") is True, "ping 异常"
    RESULTS["T5_normal_method"] = "PASS"

    await ws.close()


async def main():
    global SMALL, BIG, TMP, SMALL_SIZE
    TMP = tempfile.mkdtemp(prefix="cherry_pull_test_")
    SMALL = make_file(os.path.join(TMP, "small.bin"), 100 * 1024)
    SMALL_SIZE = 100 * 1024
    BIG = make_file(os.path.join(TMP, "big.bin"), 12 * 1024 * 1024)  # 12MB > 8MB 阈值，触发流式

    config = {
        "server_url": SRV,
        "auth_token": TOKEN,
        "device_id": "test-dev",
        "heartbeat_interval": 30,
        "max_reconnect_delay": 5,
        "default_timeout": 60,
        "build_exe_index": False,
        "allowed_actions": ["exec", "sys", "ping", "file", "file_pull", "app", "screenshot", "system"],
        "max_pull_size": 200 * 1024 * 1024,
    }

    async with websockets.serve(server_handler, "127.0.0.1", 9999, max_size=16 * 1024 * 1024):
        client = WsClient(config)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ---- T4: 超 max_pull_size 拒绝（executor 直测）----
    exec_small = Executor({**config, "max_pull_size": 1024 * 1024})
    try:
        await exec_small.execute("file_pull", {"path": BIG}, send_frame=lambda f: asyncio.sleep(0))
        RESULTS["T4_over_limit"] = "FAIL: 未拒绝超限文件"
    except ValueError as e:
        assert "max_pull_size" in str(e), f"错误信息不含 max_pull_size: {e}"
        RESULTS["T4_over_limit"] = "PASS"

    # ---- 覆盖率自检：握手失败会导致后续用例静默跳过，必须显式报错 ----
    expected = {
        "T0_handshake",
        "T1_single_read",
        "T2_stream_pull",
        "T3_missing_file",
        "T4_over_limit",
        "T5_normal_method",
    }
    missing = sorted(expected - set(RESULTS))
    RESULTS["T6_case_coverage"] = (
        f"FAIL: 未执行/未上报: {', '.join(missing)}" if missing else "PASS"
    )

    # ---- 汇总 ----
    print("\n===== FILE_PULL TEST REPORT =====")
    all_ok = True
    for k in sorted(RESULTS):
        v = RESULTS[k]
        print(f"[{'PASS' if str(v).startswith('PASS') else 'FAIL'}] {k}: {v}")
        if not str(v).startswith("PASS"):
            all_ok = False
    print("===== " + ("ALL PASS" if all_ok else "HAS FAILURES") + " =====")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
