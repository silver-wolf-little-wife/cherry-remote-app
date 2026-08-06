"""cherry-remote-app 入口。

用法：
    python -m cherry_remote_app -c config.yaml
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from .ws_client import WsClient

LOG = logging.getLogger("cherry-remote-app")


def load_config(path: str) -> dict:
    """读取 YAML 配置并合并默认值。"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("配置文件顶层必须是键值对（yaml 映射）")
    defaults = {
        "server_url": "ws://127.0.0.1:8765/ws",
        "auth_token": "change-me",
        "device_id": "home-pc",
        "heartbeat_interval": 15,
        "max_reconnect_delay": 60,
        "default_timeout": 30,
        "allowed_actions": ["exec", "sys", "ping"],
    }
    defaults.update({k: v for k, v in cfg.items() if v is not None})
    return defaults


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_path = Path(log_file)
        if log_path.parent != Path("."):
            log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cherry-remote-app",
        description="Cherry Remote 远程操控执行器（纯执行器，无 AI）",
    )
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="配置文件路径（默认 config.yaml）"
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument(
        "--log-file",
        default="logs/cherry-remote-app.log",
        help="日志文件路径（默认 logs/cherry-remote-app.log；设空则仅输出控制台）",
    )
    args = parser.parse_args()

    setup_logging(args.log_level, log_file=args.log_file or None)
    if not Path(args.config).is_file():
        LOG.error("配置文件不存在: %s（可参考 config.example.yaml 创建）", args.config)
        sys.exit(1)

    try:
        cfg = load_config(args.config)
    except Exception as e:
        LOG.error("加载配置失败: %s", e)
        sys.exit(1)

    client = WsClient(cfg)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        LOG.info("收到中断信号，退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
