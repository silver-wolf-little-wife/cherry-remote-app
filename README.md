<h3 align="center">⚠️ 双仓库协作项目 / Two-Repo Project</h3>
<p align="center"><b>本项目需要两个仓库共同部署才能完整运作：</b></p>
<p align="center">
<a href="https://github.com/silver-wolf-little-wife/cherry-astrbot"><b>cherry-astrbot</b></a>（B 端 · AstrBot 插件） ↔ <a href="https://github.com/silver-wolf-little-wife/cherry-remote-app"><b>cherry-remote-app</b></a>（C 端 · 执行器）
</p>

> [!IMPORTANT]
> 本仓库是 **C 端执行器**，必须与 **B 端插件 [cherry-astrbot](https://github.com/silver-wolf-little-wife/cherry-astrbot)** 配合部署。请同时获取两个仓库。

---

# cherry-remote-app

Cherry Remote 远程操控系统 · **C 端执行器**。当前版本 **v1.4.0**。

部署在目标电脑（C地·家庭局域网内 PC）上的常驻服务，**纯执行器，无任何 AI/LLM 逻辑**。

- 主动外连 B 端 AstrBot 插件的 WebSocket 服务（穿透 NAT）。
- 接收指令并执行：`exec`（shell）、`sys`（系统信息）、`ping`（连通性）、`file`（文件操作）、`app`（应用启停）、`screenshot`（截屏）、`camera`（摄像头拍照）、`file_pull`（流式文件拉取）。
- **摄像头工具集（v1.4.0 新增）**：让 B 端 AI 拍摄电脑**周围环境**（屏幕之外）的照片。
  默认**关闭**（`camera.enabled: false`），含冷却 + 每小时配额限流、审计日志、
  可选本地留档与提示音，服务会话（Session 0）下自动切交互用户会话采集。
  详见 [`docs/CAMERA.md`](docs/CAMERA.md)。
- **exe 索引（含显示名）**：启动时后台扫描生成 `exe_index.json`（文件名→路径 + 产品名/文件说明→文件与路径）。`app` 的启动/搜索支持按**用户认知的应用名**解析，如「打开米哈游启动器」→ 产品名为“米哈游启动器”的 `HYP.exe`、`ZenlessZoneZero.exe` 的产品名“绝区零”等。
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
python test_smoke.py       # Executor 全方法冒烟：exec/sys/ping/file/app/screenshot/camera/system/白名单
python test_exe_index.py   # exe 索引：版本资源读取/显示名回退链/索引 JSON/显示名解析/app search/旧索引合并
python test_camera.py      # 摄像头：白名单/总开关/限流/参数夹取/并发/Session 0 路由/留档（假后端）
python test_camera.py --real   # 额外跑一条真机取帧（需要摄像头）
```

全绿输出 `ALL PASS`。改动代码后建议先跑这两套。

## 配置项

见 [`config.example.yaml`](config.example.yaml)。关键项：

| 键 | 说明 |
|---|---|
| `server_url` | B 端 WebSocket 地址，如 `ws://your-server:8765/ws` |
| `auth_token` | 认证 token，与 B 端插件一致 |
| `device_id` | 本机标识，多设备时用于区分 |
| `build_exe_index` | 启动时构建 exe 索引（文件名 + 产品名/文件说明显示名） |
| `exe_index_file` | 索引输出路径（格式 v2，含 `_product` 显示名映射与 `_meta`） |
| `allowed_actions` | 指令白名单，白名单外一律拒绝（含 `file_pull`） |
| `max_pull_size` | 单次文件拉取大小上限（字节，默认 200MB） |
| `camera.enabled` | 摄像头拍照总开关，**默认 `false`**（隐私优先，显式开启后才可用） |
| `camera.min_interval_seconds` / `camera.max_per_hour` | 拍摄冷却与小时配额（默认 5 秒 / 60 次） |
| `camera.archive_dir` / `camera.shutter_sound` | 可选本地留档目录 / 可选提示音（默认关闭） |

## 摄像头（`camera`）

让 AI「看一眼电脑周围」的指令，与 `screenshot`（看屏幕内容）互补：

```json
{"method": "camera", "params": {"action": "capture", "device": 0, "reason": "用户想看看家里情况"}}
```

- `action`：`capture`（默认，拍一帧）/ `list`（枚举摄像头）/ `status`（开关与限流状态）。
- 参数：`device`、`width`/`height`、`quality`、`burst`（连拍选最清晰帧）、`mirror`、`format`、`save_local` 等。
- 响应结构与 `screenshot` 对齐（`image`/`format`/`width`/`height`/`size`），另含 `device`、`source`、`sharpness` 等元数据。

开启方式：`camera.enabled: true` 且 `allowed_actions` 含 `camera`。协议细节见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md) §6.7。

> 隐私提示：照片会经 B 端进入多模态模型上下文（或直接发给用户），即**离开本机**；
> 默认关闭 + 限流 + 审计日志 + 可选本地留档，且不提供持续录制/推流。

## 通信协议

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（与 cherry-astrbot 共享）。

## 安全

- token 认证握手。
- 指令白名单 `allowed_actions`。
- 摄像头功能默认关闭，另有限流、审计日志与可选留档（见 [`docs/CAMERA.md`](docs/CAMERA.md)）。
- 建议生产环境使用 wss/TLS。

## 许可

[MIT](LICENSE)
