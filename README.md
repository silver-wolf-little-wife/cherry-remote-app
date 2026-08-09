<h3 align="center">⚠️ 双仓库协作项目 / Two-Repo Project</h3>
<p align="center"><b>本项目需要两个仓库共同部署才能完整运作：</b></p>
<p align="center">
<a href="https://github.com/silver-wolf-little-wife/cherry-astrbot"><b>cherry-astrbot</b></a>（B 端 · AstrBot 插件） ↔ <a href="https://github.com/silver-wolf-little-wife/cherry-remote-app"><b>cherry-remote-app</b></a>（C 端 · 执行器）
</p>

> [!IMPORTANT]
> 本仓库是 **C 端执行器**，必须与 **B 端插件 [cherry-astrbot](https://github.com/silver-wolf-little-wife/cherry-astrbot)** 配合部署。请同时获取两个仓库。

---

# cherry-remote-app

Cherry Remote 远程操控系统 · **C 端执行器**。当前版本 **v1.2.0**。

部署在目标电脑（C地·家庭局域网内 PC）上的常驻服务，**纯执行器，无任何 AI/LLM 逻辑**。

- 主动外连 B 端 AstrBot 插件的 WebSocket 服务（穿透 NAT）。
- 接收指令并执行：`exec`（shell）、`sys`（系统信息）、`ping`（连通性）、`file`（文件操作）、`app`（应用启停）、`screenshot`（截屏）、`file_pull`（流式文件拉取）。
- 回传**原始结果**给 B 端，由 B 端 AI 研判后回复用户。

## 架构

```
[A] 手机(任意IM) → [B] AstrBot+AI → WebSocket → [C] cherry-remote-app(本机)
                       ↑  研判结果  ←──────执行结果────────┘
```

智能全部在 B 端；本程序只执行、不思考。

## 部署（推荐：Release 成品包）

无需安装 Python，从 [Releases](https://github.com/silver-wolf-little-wife/cherry-remote-app/releases) 下载最新版 `cherry-remote-app-vX.Y.Z-win64.zip`：

1. **解压** 到任意目录（如 `D:\cherry-remote-app\`），得到两个文件：
   - `cherry-remote-app.exe` —— 主程序
   - `config.example.yaml` —— 配置样例
2. **重命名配置文件**：把 `config.example.yaml` 改名为 `config.yaml`
   ```bash
   ren config.example.yaml config.yaml
   ```
3. **编辑 `config.yaml`**（记事本打开即可），至少填写以下三项：
   ```yaml
   server_url: "ws://你的服务器:8765/ws"   # B 端 AstrBot 插件 WebSocket 地址
   auth_token: "与B端插件一致的token"       # 必须与 B 端插件 auth_token 完全相同
   device_id: "home-pc"                    # 本机标识，多设备时用于区分
   ```
4. **运行**：双击 `cherry-remote-app.exe` 即可启动。

> 开机自启 / 注册 Windows 服务 / wss TLS 部署：见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 从源码运行（开发）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建配置（同样记得先重命名）
cp config.example.yaml config.yaml

# 3. 运行
python -m cherry_remote_app -c config.yaml
```

要求 Python 3.10+。

## 测试

仓库内置两套回归测试（无需 B 端在线，本地模拟全链路）：

```bash
python test_file_pull.py   # file_pull：握手/单帧/流式分块 sha256/缺文件/超限/普通指令回归
python test_smoke.py       # Executor 全方法冒烟：exec/sys/ping/file/app/screenshot/system/白名单
```

全绿输出 `ALL PASS`。改动代码后建议先跑这两套。

## 配置项

见 [`config.example.yaml`](config.example.yaml)。关键项：

| 键 | 说明 |
|---|---|
| `server_url` | B 端 WebSocket 地址，如 `ws://your-server:8765/ws` |
| `auth_token` | 认证 token，与 B 端插件一致 |
| `device_id` | 本机标识，多设备时用于区分 |
| `allowed_actions` | 指令白名单，白名单外一律拒绝（含 `file_pull`） |
| `max_pull_size` | 单次文件拉取大小上限（字节，默认 200MB） |

## 通信协议

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（与 cherry-astrbot 共享）。

## 安全

- token 认证握手。
- 指令白名单 `allowed_actions`。
- 建议生产环境使用 wss/TLS。

## 许可

[MIT](LICENSE)
