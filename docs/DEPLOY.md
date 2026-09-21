# Cherry Remote App 部署指南（C 端）

## 1. 三种运行形态

| 形态 | 方式 | 适用 |
|---|---|---|
| 源码运行（开发） | venv + `python -m cherry_remote_app` | 开发调试 |
| 打包 exe（便携） | `dist\cherry-remote-app.exe` | 免装 Python，手动启动 |
| Windows 服务（成品） | NSSM 注册为系统服务 | 开机自启、崩溃自愈、无人值守 |

## 2. 打包 exe

```bat
rem 在仓库根目录（要求已创建 .venv 并安装依赖）
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install pyinstaller
packaging\build_exe.bat
```

产物：`dist\cherry-remote-app.exe`（单文件）。

> 体积说明：不装 OpenCV 时约 16MB；装上 `opencv-python-headless`（摄像头功能所需，wheel 约 44MB）后
> 约 60~75MB，首次启动解包会慢几秒。不需要摄像头功能时可以不装该依赖，
> 此时 `camera` 指令会回 `CameraBackendUnavailable`，其余指令不受影响。

> 路径解析：exe 会**从自身所在目录**找 `config.yaml`、生成 `exe_index.json`、写 `logs\`。把 exe 和 config.yaml 放同一目录即可，不依赖启动目录。

## 3. 配置（config.yaml）

复制 `config.example.yaml` → 与 exe 同目录 → 改：

| 键 | 必填 | 说明 |
|---|---|---|
| `server_url` | ✅ | B 端插件地址：`ws://<B>:<端口>/ws` 或 `wss://...` |
| `auth_token` | ✅ | 与 B 端插件 `auth_token` 完全一致 |
| `device_id` | ✅ | 本机唯一标识 |
| `allowed_actions` | ✅ | 指令白名单 |
| `ssl_verify` | | wss 时是否校验证书（自签名设 `false`） |
| `ca_cert` | | 自定义 CA 证书路径（可选） |
| `camera.enabled` | | 摄像头拍照总开关，**默认 `false`**（隐私优先，显式开启后才可用） |
| `emergency_stop` | | 置 `true` 熔断所有指令（本地急停） |

## 4. 摄像头功能（`camera`，v1.4.0）

开启方式（两步都要）：

```yaml
allowed_actions: [..., camera, ...]   # 1) 白名单里保留 camera
camera:
  enabled: true                       # 2) 总开关打开
```

### 4.1 部署注意事项

| 事项 | 说明 |
|---|---|
| 依赖 | 需要 `opencv-python-headless`（已打进 exe）；缺 OpenCV 且无 ffmpeg 时回 `CameraBackendUnavailable` |
| 服务形态 | NSSM 以 LocalSystem 跑在 Session 0，**无法直接访问摄像头**；C 端会自动经计划任务切到交互用户会话采集（`source: "user-session"`），因此**需要有用户登录** |
| 系统隐私设置 | 需允许「设置 → 隐私和安全性 → 相机 → 允许桌面应用访问相机」，否则回 `CameraOpenFailed` |
| 设备占用 | 摄像头被视频通话等程序独占时回 `CameraOpenFailed`，C 端不抢占、不重试 |
| 无人值守 | 无用户登录时会话兜底不可用，回 `CameraNoInteractiveSession` |
| 限流 | 冷却 `camera.min_interval_seconds`（默认 5s）+ 每小时 `camera.max_per_hour`（默认 60）；失败的尝试也计数 |
| 本地留档 | `camera.archive_dir` 非空时在本地保留最近 `archive_keep` 张，便于事后核对 AI 拍到了什么 |
| 可听提示 | `camera.shutter_sound: true` 时拍照播放提示音；多数摄像头拍照时硬件 LED 也会亮，软件无法关闭 |

> ⚠️ 隐私告知：照片会离开本机（经 B 端进入多模态模型上下文或直接发给用户）。
> 这也是本功能默认关闭的原因。

## 5. 注册为 Windows 服务（NSSM，成品形态）

前置：
1. 已打包出 `dist\cherry-remote-app.exe`
2. 已下载 `nssm.exe`（https://nssm.cc/download）放入 `packaging\`
3. 已把配置好的 `config.yaml` 放到 `dist\`

```bat
rem 以管理员运行
packaging\install_service.bat
```

服务名 `cherry-remote-app`，开机自启、崩溃自动重启（5 秒延迟）、日志轮转 10MB。

管理命令：
```bat
nssm start cherry-remote-app
nssm stop cherry-remote-app
nssm remove cherry-remote-app confirm
```

## 6. TLS/wss

- B 端插件 WS 服务用 TLS 后，C 端 `server_url` 改为 `wss://...` 即可。
- 证书由 B 端提供（公网可信证书则 `ssl_verify: true` 直接可用）。
- 自签名证书：`ssl_verify: false`，或把 CA 放进 `ca_cert` 并保持 `ssl_verify: true`。

## 7. 管理员权限

- 需要启动「要求管理员权限的应用」时，C 端应以**管理员身份**运行：
  - exe：右键 → 以管理员身份运行
  - 服务：NSSM 服务以 `LocalSystem` 账户运行（默认），天然高权限
- 以管理员运行时，`app.launch` 启动需提权应用**不会弹 UAC**。

## 8. 日志与排障

- 日志：`logs\cherry-remote-app.log`（含每次指令审计，摄像头拍摄会记录时间/设备/尺寸/来源/原因）
- exe 索引：`exe_index.json`（启动时自动重建，可删除）
- 常见问题：
  - `invalid_token` → C/B 两端 token 不一致
  - `did not receive a valid HTTP response` → B 端插件未运行或端口不通
  - 指令像卡死 → 确认只有**一个** C 端实例在线
  - `CameraDisabled` → `camera.enabled` 仍是 `false`，或 `allowed_actions` 里没有 `camera`
  - `CameraBackendUnavailable` → 未装 OpenCV 且系统没有 ffmpeg
  - `CameraOpenFailed` → 摄像头被其他程序占用，或系统隐私设置禁止桌面应用访问相机
  - `CameraNoInteractiveSession` → 服务运行在 Session 0 且当前没有用户登录（请先登录桌面）
  - `CameraRateLimited` → 触发冷却或每小时配额，稍后再试
