"""WebSocket 客户端：主动外连 B 端，心跳保活，断线指数退避重连。"""

import asyncio
import json
import logging
import time

import websockets

from .executor import Executor

LOG = logging.getLogger("cherry-remote-app.ws")


class _ShutdownSignal(Exception):
    """内部信号：收到 system stop 后触发，用于干净退出主循环。"""


def _truncate(text, limit: int = 200) -> str:
    """将文本压成一行并截断，用于日志摘要。"""
    text = str(text).replace("\n", "\\n").replace("\r", "")
    return text[:limit] + ("…" if len(text) > limit else "")


def _summarize_data(data, method: str) -> str:
    """生成执行结果的紧凑日志摘要（避免把完整 stdout 刷进日志）。"""
    if not isinstance(data, dict):
        return _truncate(data)
    if method == "exec":
        return (
            f"exit={data.get('exit_code')} timed_out={data.get('timed_out')} "
            f"truncated={data.get('truncated')} "
            f"stdout={_truncate(data.get('stdout', ''), 120)} "
            f"stderr={_truncate(data.get('stderr', ''), 120)}"
        )
    if method == "sys":
        cpu = (data.get("cpu") or {}).get("percent")
        mem = (data.get("memory") or {}).get("percent")
        return f"hostname={data.get('hostname')} cpu={cpu}% mem={mem}%"
    return _truncate(json.dumps(data, ensure_ascii=False, default=str), 200)


class WsClient:
    def __init__(self, config: dict):
        self.url: str = config["server_url"]
        self.token: str = config["auth_token"]
        self.device_id: str = config["device_id"]
        self.heartbeat_interval: float = float(config.get("heartbeat_interval", 15))
        self.max_reconnect_delay: float = float(config.get("max_reconnect_delay", 60))
        self.executor = Executor(config)
        self._last_recv: float = 0.0
        self._ssl = self._build_ssl_context(config)

    async def run(self) -> None:
        """主循环：连接 → 服务 → 异常重连。"""
        self.executor.start_background_tasks()  # 启动 exe 索引构建等后台任务
        delay = 1.0
        while True:
            try:
                await self._connect_once()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except _ShutdownSignal:
                LOG.info("收到停机指令，C 端退出。")
                return
            except Exception as e:  # noqa: BLE001 —— 断线重连属预期路径
                LOG.error("连接异常: %s", e)
            delay = min(delay * 2, self.max_reconnect_delay)
            LOG.info("将在 %.1f 秒后重连……", delay)
            await asyncio.sleep(delay)

    def _build_ssl_context(self, config: dict):
        """wss 时构建 ssl 上下文；支持关闭校验与自定义 CA。"""
        if not self.url.startswith("wss://"):
            return None
        import ssl

        ctx = ssl.create_default_context()
        if not config.get("ssl_verify", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ca = config.get("ca_cert")
        if ca:
            ctx.load_verify_locations(ca)
        return ctx

    async def _connect_once(self) -> None:
        LOG.info("连接 %s ...", self.url)
        async with websockets.connect(
            self.url, ping_interval=None, max_size=16 * 1024 * 1024, ssl=self._ssl
        ) as ws:
            await self._handshake(ws)
            self._last_recv = time.monotonic()
            LOG.info("已连接 B 端服务端，device_id=%s", self.device_id)
            recv_task = asyncio.create_task(self._recv_loop(ws))
            hb_task = asyncio.create_task(self._heartbeat_loop(ws))
            done, _ = await asyncio.wait(
                {recv_task, hb_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    raise exc
            for task in (recv_task, hb_task):
                task.cancel()

    async def _handshake(self, ws) -> None:
        hello = {
            "type": "hello",
            "token": self.token,
            "device_id": self.device_id,
            "client_version": "1.2.0",
        }
        await ws.send(json.dumps(hello, ensure_ascii=False))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if not ack.get("ok"):
            raise RuntimeError(f"握手失败: {ack.get('error')}")

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            self._last_recv = time.monotonic()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "request":
                await self._handle_request(ws, msg)
            elif mtype == "pong":
                pass
            else:
                LOG.debug("忽略未知消息类型: %s", mtype)

    async def _handle_request(self, ws, msg: dict) -> None:
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        start = time.monotonic()
        LOG.info(
            "收到指令 id=%s method=%s params=%s",
            rid,
            method,
            json.dumps(params, ensure_ascii=False, default=str),
        )
        try:
            if method == "file_pull":
                # 流式方法：把 ws.send 包装成回调，executor 分块推送 file_data 帧
                async def _send_frame(frame: dict) -> None:
                    frame["id"] = rid  # file_data 帧必须携带请求 id，供 B 端关联传输
                    await ws.send(json.dumps(frame, ensure_ascii=False, default=str))

                data = await self.executor.execute(method, params, send_frame=_send_frame)
            else:
                data = await self.executor.execute(method, params)
            elapsed = round(time.monotonic() - start, 3)
            resp = {"type": "response", "id": rid, "ok": True, "data": data, "error": None}
            LOG.info(
                "指令完成 id=%s method=%s ok=True 耗时=%.2fs 结果=%s",
                rid,
                method,
                elapsed,
                _summarize_data(data, method),
            )
        except Exception as e:  # noqa: BLE001 —— 指令错误需回传而非中断
            elapsed = round(time.monotonic() - start, 3)
            LOG.error("指令失败 id=%s method=%s 耗时=%.2fs 错误=%s", rid, method, elapsed, e)
            resp = {
                "type": "response",
                "id": rid,
                "ok": False,
                "data": None,
                "error": {"code": type(e).__name__, "message": str(e)},
            }
        await ws.send(json.dumps(resp, ensure_ascii=False, default=str))
        if getattr(self.executor, "shutdown_requested", False):
            raise _ShutdownSignal()

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await ws.send(json.dumps({"type": "ping"}))
                if time.monotonic() - self._last_recv > self.heartbeat_interval * 3:
                    LOG.warning("长时间未收到服务端帧，主动断开重连")
                    await ws.close()
                    return
            except Exception:  # noqa: BLE001
                return
