# 摄像头工具集方案（`camera`，计划随 v1.4.0 发布）

> 目标：让 B 端 AI 能调用 C 端电脑的摄像头，拍一张（或几张）现场照片，用来了解电脑周围的环境。
> 本文档为**设计方案**，供评审后再进入编码。协议部分改动需与 `cherry-astrbot`（B 端）同步。

---

## 0. 评审结论与落地状态（v1.4.0）

评审确认的四项决策：

| 决策 | 结论 |
|---|---|
| 默认开关 | **默认关闭**（`camera.enabled: false`），需机器主人显式开启 |
| 采集后端 | **OpenCV 主路径 + ffmpeg 可选兜底** |
| v1 范围 | **单帧拍照 + 连拍选优**（不做录像/推流） |
| 落地范围 | C 端 + B 端同时改；代码上传 GitHub，部署由机器主人自行完成 |

已在 C 端落地（本仓库）：

| 文件 | 内容 |
|---|---|
| `src/cherry_remote_app/camera.py` | `CameraService`：设备枚举、采集、连拍选优、限流、留档、提示音、8 个错误类 |
| `src/cherry_remote_app/user_session.py` | 通用「切交互用户会话执行 helper」机制（截屏与拍照共用） |
| `src/cherry_remote_app/helper.py` | helper 入口：`--camera-helper` / `--screenshot-helper` |
| `src/cherry_remote_app/executor.py` | `_exec_camera` 接入 + 截屏兜底改为复用 `user_session` |
| `src/cherry_remote_app/main.py` | 默认白名单加 `camera`；helper 模式入口 |
| `config.example.yaml` | 新增 `camera` 配置段（默认关闭） |
| `test_camera.py` | 19 条假后端/全链路用例 + 1 条真机用例（`--real`） |
| `requirements.txt` / `cherry-remote-app.spec` / `packaging/build_exe.bat` | 增加 `opencv-python-headless` 与 `--collect-all cv2` |

B 端（`cherry-astrbot`，v1.3.0）：`RemoteCameraTool` / `RemoteCameraListTool`、`/camera` 指令、
`_save_image` + `_prune_images` + `_send_image_to_user` + `_camera_error_hint`、三个新配置项、
`metadata.yaml` 与协议文档同步。B 端工具行为已用桩掉 astrbot/mcp/aiohttp 的临时脚本逐项验证
（`CallToolResult + ImageContent` 构造、错误码翻译、forward 直发、落盘裁剪、注册开关，全部通过）。

真机验证（本机 ASUS FHD webcam + ASUS IR camera，OpenCV 5.0.0 headless）：
`camera.list` 探测到 2 个设备且 index↔系统设备名对应正确；`camera.capture` 取帧成功
（640×480、32KB JPEG、`source=direct`、`backend=dshow`）。

---

## 1. 背景与目标

现状：C 端 `cherry-remote-app` 是纯执行器，已支持 `exec / sys / ping / file / file_pull / app / screenshot / system`，
B 端插件把这些 method 包装成 FunctionTool 交给 AI 调用。AI 目前只能看到「屏幕内容」与「文件/命令输出」，
看不到**屏幕之外**的物理环境（房间、设备指示灯、纸质材料、门口情况等）。

本方案新增一个 `camera` 工具集：

| 能力 | AI 能做什么 |
|---|---|
| `camera.list` | 查这台电脑有几个摄像头、是否可用 |
| `camera.capture` | 拍一帧现场照片，返回 base64 图片 + 元数据 |
| （B 端）`remote_camera` | 把照片**注入模型上下文**，让多模态模型真的"看见"，并可转发给用户 |
| （B 端）`/camera` 指令 | 用户手动触发一次拍照（不经过 AI 判断） |

非目标（v1 明确不做）：**不提供持续录制/实时推流**，不做无提示后台监视。原因见 §4.7 隐私设计。

---

## 2. 现状与关键约束

| 约束 | 说明 | 对本方案的影响 |
|---|---|---|
| C 端是纯执行器 | 无任何 AI 逻辑，只执行、只回传原始结果 | 图像不做「是否该拍」的判断，判断在 B 端 |
| WebSocket 单帧上限 16MB | `ws_client.py` 中 `max_size=16 * 1024 * 1024` | 720p JPEG（约 150KB）绰绰有余，仍要限制最大分辨率 |
| 服务形态跑在 Session 0 | NSSM 以 LocalSystem 运行，**Session 0 拿不到摄像头**（与截屏同样的坑，仓库已有 `schtasks` 兜底先例） | 必须复用「切到交互用户会话执行 helper」的兜底链路 |
| Windows 相机隐私开关 | 系统需允许「桌面应用访问相机」；拒绝时底层打开失败且报错模糊 | 需要把失败翻译成人话，并在文档给排障指引 |
| 打包体积 | 当前 onefile exe 约 16MB | 引入 OpenCV 后约 +40~60MB，需在 DEPLOY/README 写明 |
| 协议是双仓库共享 | `docs/PROTOCOL.md` 为权威版本 | 新增 method 需两端同时改，并升版本号 |

参考实现（可直接复用）：
- `executor.py::_exec_screenshot` 与 `user_session.py`（切交互用户会话执行 helper 的通用机制）
- `executor.py::_exec_file` / `_exec_app` 的 **action 分发风格**（`_exec_<method>` → `_<method>_<action>`）
- B 端 `main.py::RemoteScreenshotTool`、`_save_screenshot_image`
- AstrBot `tool_loop_agent_runner.py`：工具返回 `mcp.types.CallToolResult` 中的 `ImageContent` 会被缓存并交给多模态模型（已确认支持）

---

## 3. 总体设计

### 3.1 调用链

```
用户(IM)   「看看家里电脑周围什么情况」
   │
   ▼
[A] AstrBot Agent（多模态 LLM）
   │  选中工具 remote_camera → send_command("camera", {...})
   ▼
[B] cherry-astrbot 插件 ──WebSocket(request)──► [C] cherry-remote-app
   │                                                executor._exec_camera
   │                                                 ├─ camera.list  → 枚举设备
   │                                                └─ camera.capture
   │                                                     ├─ 主路径：OpenCV 开设备取帧
   │                                                     └─ 兜底：schtasks 切交互会话取帧
   │   ◄──response{data:{image:base64,width,height,...}}──┘
   │
   ├─► 返回 CallToolResult[TextContent + ImageContent] → 模型"看到"照片
   └─► （可选）模型/插件把图片直接发给用户
```

### 3.2 职责划分

- **C 端**：设备枚举、开设备、取帧、编码、限流、审计、本地隐私开关。**不判断画面内容**。
- **B 端**：把 `camera` 包装成 FunctionTool；决定图片是「给模型看」还是「发给用户」；写审计日志（谁、何时、因何调用）。

---

## 4. C 端设计（cherry-remote-app）

### 4.1 采集后端选型

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **OpenCV（`opencv-python-headless`）** | Windows 自带 DSHOW/MSMF 后端，`VideoCapture` + `imencode` 三行取帧；多摄像头按 index 枚举方便；能读回真实分辨率 | exe 体积 +40~60MB；onefile 首次启动解包变慢 | ✅ **主路径（推荐）** |
| ffmpeg（`-f dshow -i video="名称"`） | 取帧质量好；设备名可从 `-list_devices` 拿到 | 当前机器未安装，需随包分发（约 80MB） | ⭕ v1 作为**可选兜底**：检测到 `ffmpeg` 才启用 |
| PowerShell + WinRT `Windows.Media.Capture` | 零依赖 | 脚本长且脆、Session 0 同样不可用、枚举/取帧代码难维护 | ❌ 不采用 |

依赖变更：`requirements.txt` 增加 `opencv-python-headless>=4.10`（headless 版不含 GUI，避免 Qt 依赖冲突，`VideoCapture` 功能完整）。

### 4.2 新 method `camera`

沿用 `file` / `app` 的 action 分发风格：`_exec_camera` → `_camera_list` / `_camera_capture`。

**`{"method":"camera","params":{"action":"list"}}`**

响应 `data`：

```json
{
  "count": 1,
  "devices": [
    {
      "index": 0,
      "openable": true,
      "width": 1280,
      "height": 720,
      "backend": "dshow",
      "system_name": "Integrated Camera",
      "system_class": "Camera"
    }
  ],
  "system_devices": [
    {"name": "Integrated Camera", "class": "Camera", "status": "OK"}
  ],
  "note": "system_name 与 index 的对应为尽力而为：Windows 不提供 index↔名称的直接映射"
}
```

- `devices`：OpenCV 探测结果（`index` 从 0 起逐个 `isOpened()` 试开，成功则读一帧确认真能出图，随即 release）。
- `system_devices`：`Get-CimInstance Win32_PnPEntity`（`PNPClass` 为 `Camera`/`Image`）取友好名，作为「这台机器到底装了几个摄像头」的交叉验证。
- 探测上限 `camera.probe_max_index`（默认 4），避免无摄像头机器长时间空转。

**`{"method":"camera","params":{"action":"capture", ...}}`**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `device` | int\|str | `0` | 摄像头 index；也可传名称做尽力匹配（匹配不到明确报错，要求改用 index） |
| `width` / `height` | int | 1280 / 720 | 请求分辨率，按 `camera.max_width` 夹取上限 |
| `quality` | int | 80 | JPEG 质量，夹取到 30~95 |
| `warmup_frames` | int | 5 | 丢弃前 N 帧，等自动曝光/对焦稳定（否则首帧常偏暗偏糊） |
| `burst` | int | 1 | 连拍帧数（1~5），按拉普拉斯方差选最清晰的一帧 |
| `format` | str | `jpeg` | `jpeg` / `png` |
| `mirror` | bool | false | 左右镜像（前置摄像头自拍场景） |
| `save_local` | bool | false | 是否在 C 端本地留档一份（配合 `camera.archive_dir`） |
| `timeout` | float | 15 | 单次拍摄超时 |
| `reason` | str | — | B 端透传的调用原因，仅写入审计日志 |

响应 `data`：

```json
{
  "image": "<base64 JPEG>",
  "format": "jpeg",
  "width": 1280,
  "height": 720,
  "size": 153621,
  "device": {"index": 0, "backend": "dshow", "system_name": "Integrated Camera"},
  "captured_at": "2026-08-27 10:12:33",
  "burst": 3,
  "sharpness": 412.7,
  "source": "direct",
  "elapsed": 1.82,
  "archived_path": null
}
```

- `source`：`direct`（本进程直采）/ `user-session`（走交互会话兜底）/ `ffmpeg`（兜底后端）。
- 与 `screenshot` 的返回结构保持一致的字段命名（`image/format/width/height/size`），B 端可复用现成的保存逻辑。

### 4.3 采集流程

```
1. 检查 camera.enabled（总开关）与方法白名单
2. 检查冷却时间 camera.min_interval_seconds / 小时配额 camera.max_per_hour
3. 取全局 asyncio.Lock（同一时刻只允许一路占用摄像头）
4. await asyncio.to_thread(_capture_sync, ...)   ← 阻塞调用不占事件循环，心跳不受影响
   4.1 依次尝试后端：CAP_DSHOW → CAP_MSMF（Windows 上 DSHOW 通常开得更快更稳）
   4.2 设置宽高 → 丢弃 warmup_frames 帧 → 连拍 burst 帧
   4.3 选帧（burst>1 时取拉普拉斯方差最大者）
   4.4 可选镜像 → imencode(JPEG quality) → bytes
   4.5 finally: cap.release()（绝不能泄漏句柄，否则后续拍摄全部失败）
5. 若直采抛异常且检测到当前在 Session 0 → 走 §4.5 兜底
6. 组装响应（base64 + 元数据），可选本地留档
7. 写审计日志：时间 / 设备 / 尺寸 / 大小 / 来源 / reason / B 端 device_id
```

### 4.4 并发与稳定性

- `asyncio.to_thread` + 实例级 `asyncio.Lock`：用**快速失败**而不是排队——拍摄进行中再次收到请求
  立即回 `CameraBusy`，避免后到的请求干等 30 秒后被 B 端判超时（AI 可稍后重试）。
- 每次 `release()` 后短暂 `asyncio.sleep/sleep(camera.release_delay)`，规避部分 USB 摄像头刚释放不能立刻重开的问题。
- 设备被其它程序占用（如正在视频通话）→ 不抢占、不重试，直接回结构化错误，提示"可能被其他程序占用"。
- 打开失败重试仅一次，避免把 30s 指令超时耗光。

### 4.5 Session 0 兜底（服务形态必需）

复用截屏那套已验证的链路，并已重构为通用机制（`user_session.py`）：

1. `user_session.current_session_id()` 判定当前是否在 Session 0（服务会话）。
2. Session 0 → 由 `user_session.run_in_user_session()` 生成临时引导脚本，用
   `schtasks /create ... /ru <交互用户名> /it` + `/run` 以交互用户身份执行 helper
   （`helper.py --camera-helper <in.json> <out_img> <out_meta>`）。
3. helper 复用本程序自身（打包形态直接用 exe，源码形态用引导脚本导入），
   因此**不依赖目标机另装 Python 或 OpenCV**。
4. 父进程轮询等待图片出现（上限 `capture_timeout + 10` 秒）→ 读回图片与元信息 → 清理任务与临时文件。
5. helper 内部把「先写 `.part` 再 `os.replace`」作为就绪标志，父进程不会读到半张图。

> 优势：截屏与拍照共用同一套机制，消灭两份几乎相同的 schtasks 代码；
> 且 helper 跑到交互用户会话，顺带绕开「LocalSystem 没有桌面用户相机同意记录」的坑。
> 无交互用户登录（纯无人值守服务器）时，明确回 `CameraNoInteractiveSession`，而不是干等超时。

### 4.6 配置项（`config.example.yaml` 新增）

```yaml
# ── 摄像头（隐私敏感功能，默认关闭，需显式开启）──
camera:
  enabled: false              # 总开关：false 时 camera 指令一律拒绝
  backend: "auto"             # auto=OpenCV 优先、无则试 ffmpeg；opencv / ffmpeg
  max_width: 1920             # 分辨率上限（同时受 WS 帧大小约束）
  max_height: 1080
  jpeg_quality: 80
  warmup_frames: 5
  burst: 1                    # 默认连拍帧数（取最清晰帧）
  probe_max_index: 4          # list 时最多探测的 index
  min_interval_seconds: 5     # 两次拍摄之间的最小间隔（冷却）
  max_per_hour: 60            # 每小时拍摄次数上限
  capture_timeout: 15         # 单次拍摄超时（秒）
  release_delay: 0.2
  allow_user_session_fallback: true   # Session 0 时是否允许切交互会话取帧
  shutter_sound: false        # 拍照时播放系统提示音（可听提示，P1）
  archive_dir: ""             # 非空则在 C 端留档最近 N 张（留档=本地可核对，默认关闭）
  archive_keep: 20
```

`allowed_actions` 增加 `camera`（与其它 method 一致，示例配置中列出）。

`system.status` 增加两个只读字段，便于 B 端/AI 自查能力：
`camera_enabled: bool`、`camera_devices: [{"index":0,"openable":true}]`（惰性探测，不阻塞启动）。

### 4.7 隐私与安全设计（重点）

摄像头是**物理世界隐私**入口，比截屏更敏感。设计原则：**可感知、可关闭、有额度、留痕迹、不做持续监视**。

| 措施 | 说明 |
|---|---|
| 默认关闭 | `camera.enabled: false`，必须由机器主人主动改配置开启（明示同意） |
| 双闸门 | `allowed_actions` 白名单 + `camera.enabled` 双重约束；`emergency_stop` 依然全局熔断 |
| 速率限制 | 冷却 5s + 每小时 60 次，防止"连续偷拍"式滥用；超限回 `RateLimited` |
| 审计日志 | 本地日志记录每次拍摄（时间、设备、尺寸、来源、B 端透传的 `reason`）；文档提示可用日志倒查 |
| 可选留档 | `archive_dir` 打开后本地保留最近 N 张，事后可核对 AI 到底拍到了什么 |
| 可选提示音 | `shutter_sound: true` 时拍照播系统快门音，给在场者可听提示 |
| 硬件 LED | 多数摄像头拍照时硬件 LED 会亮，软件无法关闭——这是天然的可见提示，文档如实说明 |
| 不做持续录制 | v1 不提供录屏/推流，杜绝"静默长时监控"场景 |
| 不做画面加工 | C 端不裁切、不美化、不做人脸识别，保持"纯执行器"定位 |

⚠️ **必须写进文档的告知**：图片会经 B 端上传到所配置的多模态模型服务商，属于「离开本机」的数据。
B 端提供开关（§5.4），允许只把照片发给人、不进模型上下文。

### 4.8 错误处理

现状：`ws_client._handle_request` 把 `error.code` 填成**异常类名**（如 `PermissionError`）。
为让 B 端/AI 能稳定判定，新增一组语义明确的异常类：

| 异常类 / `error.code` | 场景 | 建议 B 端话术 |
|---|---|---|
| `CameraDisabled` | `camera.enabled=false` 或不在白名单 | "该电脑未开启摄像头功能" |
| `CameraNotFound` | 没有可用摄像头 / index 越界 | "没找到摄像头" |
| `CameraOpenFailed` | 设备打不开（被占用、隐私开关拒绝、驱动异常） | "摄像头无法打开：可能被其他程序占用或在系统隐私设置中被禁止" |
| `CameraBusy` | 已有一次拍摄在进行 | "上一次拍摄还没结束，请稍后再试" |
| `CameraNoInteractiveSession` | Session 0 且无交互用户可兜底 | "服务会话下无法访问摄像头，请确认有用户登录" |
| `CameraCaptureFailed` | 打开成功但取不到有效帧 | "取帧失败，请检查摄像头是否被遮挡或禁用" |
| `RateLimited` | 触发冷却/小时配额 | "拍摄太频繁，请稍后再试" |

同时更新 `docs/PROTOCOL.md` §7 错误码表。

### 4.9 打包与发布影响

- `requirements.txt`：+ `opencv-python-headless>=4.10`（实测装的是 `opencv_python_headless-5.0.0.93`，wheel 约 44MB）+ `numpy>=1.26`。
- `cherry-remote-app.spec` / `packaging/build_exe.bat`：`collect_all('cv2')` / `--collect-all cv2`
  （`pyinstaller-hooks-contrib` 已装，cv2 hook 可用；未装 OpenCV 时仍可打包，只是 `camera` 不可用）。
- 体积与启动：onefile 预计 16MB → 约 60~75MB，首次启动解包时间增加数秒；`docs/DEPLOY.md` 明确标注，并给出"不需要摄像头功能时如何不装 OpenCV"的说明（不加依赖也能打包，只是 `camera` 不可用并回 `CameraBackendUnavailable`）。
- 版本号：C 端 `client_version` 与 `system.status.version` 1.3.0 → **1.4.0**；README 工具列表加 `camera`。

---

## 5. B 端设计（cherry-astrbot，需另开提交）

### 5.1 新增工具

| 工具名 | 说明 | 参数 |
|---|---|---|
| `remote_camera` | 用远程电脑摄像头拍一张现场照片，用于了解电脑周围环境 | `device_id`、`device`、`reason`、`send_to_user`(默认 true) |
| `remote_camera_list` | 列出远程电脑的摄像头设备 | `device_id` |

`description` 建议写成（供 LLM 选型，务必说明"看不见屏幕外的东西时才用"）：

> 用远程电脑（C端）的摄像头拍摄一张现场照片，用于了解电脑周围的环境（房间、设备状态、纸质材料等）。
> 适合回答"家里什么情况""桌上有什么"这类需要看物理环境的问题；只看屏幕内容请用 `remote_screenshot`。

### 5.2 让模型真正"看见"（关键实现）

`ToolExecResult` 允许 `mcp.types.CallToolResult`，AstrBot 的 `tool_loop_agent_runner` 对 `ImageContent` 会：
缓存图片 → 把 `Image returned and cached at path=...` 作为工具结果文本 → 同时把图片作为多模态输入交给模型。
因此 `call()` 返回：

```python
from mcp.types import CallToolResult, ImageContent, TextContent

return CallToolResult(
    content=[
        TextContent(type="text", text=json.dumps({...元数据、cached 提示...}, ensure_ascii=False)),
        ImageContent(type="image", data=data["image"], mimeType="image/jpeg"),  # base64，不带 data: 前缀
    ]
)
```

模型随后可用内置 `send_message_to_user(type='image', path=...)` 把照片转发给用户（`vision` 模式下由模型自行决定是否转发）。

### 5.3 非多模态模型的降级

B 端配置新增 `camera_mode`：

| 取值 | 行为 |
|---|---|
| `vision`（默认） | 返回 `CallToolResult`（图片进模型上下文），模型可转述画面内容并按需转发给用户 |
| `forward` | 不进模型上下文：插件直接把图片发给用户 + 文本摘要（`Comp.Image` 直发，落盘复用 `_save_image`） |
| `both` | 既进模型上下文又直接发用户 |

模型不支持视觉时用 `forward`，避免"工具报错/模型瞎猜"。

### 5.4 其它 B 端改动

- `main.py`：`_save_screenshot_image` 泛化为 `_save_image(data, subdir, default_ext, keep)`（截屏与摄像头共用），
  新增 `_prune_images`（按数量保留最近 N 张）、`_send_image_to_user`、`_camera_error_hint`（错误码 → 人话）；
  新增 `/camera [index]` 指令（用户手动拍照，与管理指令 `/screenshot` 并列）。
- `_build_tools()` 注册表加入 `RemoteCameraTool`、`RemoteCameraListTool`，并注入 `camera_mode` / `camera_keep`。
- `_conf_schema.json`：新增 `camera_enabled`（bool，默认 true，仅控制是否注册这两个工具）、
  `camera_mode`（string，默认 vision）、`camera_keep`（int，默认 50）。
- `metadata.yaml` / `@register` 版本：B 端插件 1.2.0 → **1.3.0**。
- 审计：C 端记录每次拍摄（含 `reason`）；B 端记录谁在哪个会话调用了摄像头，两端日志互为佐证。

---

## 6. 协议变更（`docs/PROTOCOL.md`，两端同步）

已落地：新增 §6.7 `camera`（`capture` / `list` / `status` 的参数与响应示例）、§7 错误码表补充 8 个 camera 错误码、
文档头版本号 v1.0.0 → **v1.1.0**；`cherry-astrbot/docs/PROTOCOL.md` 同步为同一版本，
并补上 §9 流式文件拉取（原 B 端 3.4 节）与 §10 B 端工具映射表。

---

## 7. 测试计划

### 7.1 `test_camera.py`（20 条用例，无摄像头也能全绿）

用**假后端注入**（子类覆盖 `_capture_direct` / 探测函数）避免依赖真机：

| # | 用例 | 断言 |
|---|---|---|
| 1 | `camera` 不在 `allowed_actions` | `PermissionError`（Executor 层） |
| 2 | `camera.enabled=false` | `CameraDisabled`，错误信息含配置键名 |
| 3 | `camera.list` 无设备 | 不崩，`count==0`、`devices==[]` |
| 4 | 采集成功 | base64 可解、magic bytes `FF D8`、`format/width/height/size` 齐全、`source=direct` |
| 5 | 冷却 `min_interval_seconds=5` | 第二次 `CameraRateLimited` |
| 6 | 小时配额 `max_per_hour=2` | 第 3 次 `CameraRateLimited` |
| 7 | 参数越界 `width=9999 / quality=200 / burst=99` | 夹取到上限（1920×1080 / 95 / 5） |
| 8 | 拍摄进行中再次请求 | `CameraBusy`（快速失败） |
| 9 | 后端抛 `OSError` | 统一翻译为 `CameraOpenFailed` |
| 10 | Session 0 且禁用兜底 | `CameraNoInteractiveSession` |
| 11 | Session 0 且允许兜底 | 走交互会话路径，`source=user-session` |
| 12 | 设备名匹配 / 未命中 | 命中返回 index；未命中 `CameraNotFound` 并列出已知设备 |
| 13 | 留档与裁剪 | `archive_dir` 产出文件；`keep=2` 时只留 2 张；`save_local=false` 不留档 |
| 14 | 缺 OpenCV | `CameraBackendUnavailable` |
| 15 | Executor 集成 | 默认 action=capture；`system.status` 暴露 `camera_enabled` |
| 16-18 | **WS 全链路**（假 B 端 + 真实 WsClient） | 关闭时 `error.code=CameraDisabled`；开启时响应含 `FF D8` base64 与元数据；`camera.list` 通链 |
| 19 | 真机用例（`--real`） | 真实探测设备 + 取帧，`width>0`、JPEG 可解码 |

### 7.2 回归

- `test_smoke.py`：`allowed_actions` 增加 `camera`，并断言默认关闭时回 `CameraDisabled`。
- **顺带修复**：`test_file_pull.py` 里写死的 `client_version == "1.2.0"` 早已失效（C 端已是 1.3.0），
  导致握手断言抛错后**后续用例静默跳过、报告仍显示 ALL PASS**。已改为只校验字段存在，
  并新增 T6 覆盖率自检（关键用例缺失即判 FAIL）。
- 全套结果：`test_smoke.py` / `test_file_pull.py` / `test_exe_index.py` / `test_camera.py`（含 `--real`）
  **全部 exit 0、ALL PASS**。

### 7.3 真机手测清单

1. 源码运行：笔记本内置摄像头拍一张，画面正常、不偏暗。
2. 外接 USB 摄像头：`camera.list` 能看到 index 0/1，指定 `device` 分别拍摄正确设备。
3. 服务形态（NSSM + 有用户登录）：验证走 `user-session` 兜底成功。
4. 服务形态 + 无用户登录：回 `CameraNoInteractiveSession`（不干等超时）。
5. 系统隐私设置关闭相机：回 `CameraOpenFailed` 且提示指向隐私设置。
6. 摄像头被占用（开个视频通话）→ 回 `CameraOpenFailed` 且提示"可能被占用"。
7. 拔掉全部摄像头 → `camera.list` 空、`camera.capture` 回 `CameraNotFound`。
8. 拍照时 `screenshot` / `exec` 仍正常（不阻塞事件循环）。

---

## 8. 实施步骤与文件清单（M1~M4 均已完成）

| 里程碑 | 内容 | 涉及文件 | 状态 |
|---|---|---|---|
| **M1** | C 端 `camera.list` / `camera.capture`（OpenCV 主路径）+ 配置 + 白名单 + 限流 + 异常类 | `src/cherry_remote_app/camera.py`（新增）、`executor.py`、`main.py`、`config.example.yaml` | ✅ |
| **M2** | 通用交互会话 helper 重构（截屏/拍照共用）+ Session 0 兜底 + 错误码落地 | `src/cherry_remote_app/user_session.py`、`helper.py`（新增）、`executor.py`、`docs/PROTOCOL.md` | ✅ |
| **M3** | B 端工具（`CallToolResult` + `ImageContent`）+ 降级模式 + `/camera` 指令 | `cherry-astrbot/main.py`、`_conf_schema.json`、`metadata.yaml`、`README.md`、`docs/PROTOCOL.md` | ✅ |
| **M4** | 测试、文档、打包、版本号 | `test_camera.py`（新增）、`test_smoke.py`、`test_file_pull.py`、`docs/CAMERA.md`（本文）、`docs/DEPLOY.md`、`README.md`、`requirements.txt`、`cherry-remote-app.spec`（本地文件，被 `.gitignore` 忽略）、`packaging/build_exe.bat` | ✅ |

实际新增：C 端约 640 行（`camera.py` + `user_session.py` + `helper.py` + 接线与测试），B 端约 220 行。

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| Session 0 无法访问摄像头 | 服务形态默认不可用 | schtasks 切交互会话兜底 + 无交互用户时明确报错；文档写清 |
| Windows 隐私开关 / 企业策略禁止 | 打开必失败 | 失败信息指向「设置 → 隐私和安全性 → 相机 → 允许桌面应用访问相机」；文档给排障步骤 |
| OpenCV 让 exe 膨胀到 60MB+ | 分发体积、首启变慢 | 文档标注；提供"不装 OpenCV 打包"的口子；后续可评估 onedir |
| 多摄像头 index 漂移（USB 插拔顺序） | 拍错设备 | `camera.list` 先探测 + 支持按名称匹配 + 返回 `system_name` 供 AI 确认 |
| 摄像头被其他程序独占 | 拍摄随机失败 | 不抢占、明确报错、提示"可能被占用" |
| 隐私争议（远程偷拍） | 信任风险 | 默认关闭 + 限流 + 审计日志 + 可选留档/提示音 + 不做持续录制；文档明示图片会上传模型服务商 |
| 图片过大撑爆 WS 帧 | 指令失败 | 默认 720p/q80（约 150KB），分辨率与质量双重夹取，上限 1920×1080 |

---

## 10. 评审结论（已确认）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 默认开关 | `camera.enabled: false`（隐私优先，需显式开启） |
| 2 | 后端选型 | `opencv-python-headless` 主路径 + ffmpeg 可选兜底 |
| 3 | v1 范围 | 单帧拍照 + 连拍选优（录像/推流放 v2 再议） |
| 4 | 本地留档与快门音 | 都做成配置项，默认关闭 |

编码已完成 M1~M4（见 §0 落地清单）。后续可做（按需再议）：
`camera.clip`（3~10 秒短视频）、多帧变化检测（判断"有没有人/有没有动静"）、
画面脱敏（人脸/证件区域模糊后再传给模型）。
