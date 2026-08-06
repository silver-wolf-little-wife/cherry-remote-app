# cherry-remote-app

Cherry Remote 远程操控系统 · **C 端执行器**。

部署在目标电脑（C地·家庭局域网内 PC）上的常驻服务，**纯执行器，无任何 AI/LLM 逻辑**。

- 主动外连 B 端 AstrBot 插件的 WebSocket 服务（穿透 NAT）。
- 接收指令并执行：`exec`（shell）、`sys`（系统信息）、`ping`（连通性）。
- 回传**原始结果**给 B 端，由 B 端 AI 研判后回复用户。

## 架构

```
[A] 手机(任意IM) → [B] AstrBot+AI → WebSocket → [C] cherry-remote-app(本机)
                       ↑  研判结果  ←──────执行结果────────┘
```

智能全部在 B 端；本程序只执行、不思考。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建配置
cp config.example.yaml config.yaml
#    编辑 config.yaml：server_url / auth_token / device_id

# 3. 运行
python -m cherry_remote_app -c config.yaml
```

要求 Python 3.10+。

## 配置项

见 [`config.example.yaml`](config.example.yaml)。关键项：

| 键 | 说明 |
|---|---|
| `server_url` | B 端 WebSocket 地址，如 `ws://your-server:8765/ws` |
| `auth_token` | 认证 token，与 B 端插件一致 |
| `device_id` | 本机标识，多设备时用于区分 |
| `allowed_actions` | 指令白名单，白名单外一律拒绝 |

## 通信协议

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（与 cherry-astrbot 共享）。

## 安全

- token 认证握手。
- 指令白名单 `allowed_actions`。
- 建议生产环境使用 wss/TLS。

## 许可

（待主人选择许可证，M7 补全）
