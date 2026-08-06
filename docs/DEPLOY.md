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

产物：`dist\cherry-remote-app.exe`（单文件，约 16MB）。

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
| `emergency_stop` | | 置 `true` 熔断所有指令（本地急停） |

## 4. 注册为 Windows 服务（NSSM，成品形态）

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

## 5. TLS/wss

- B 端插件 WS 服务用 TLS 后，C 端 `server_url` 改为 `wss://...` 即可。
- 证书由 B 端提供（公网可信证书则 `ssl_verify: true` 直接可用）。
- 自签名证书：`ssl_verify: false`，或把 CA 放进 `ca_cert` 并保持 `ssl_verify: true`。

## 6. 管理员权限

- 需要启动「要求管理员权限的应用」时，C 端应以**管理员身份**运行：
  - exe：右键 → 以管理员身份运行
  - 服务：NSSM 服务以 `LocalSystem` 账户运行（默认），天然高权限
- 以管理员运行时，`app.launch` 启动需提权应用**不会弹 UAC**。

## 7. 日志与排障

- 日志：`logs\cherry-remote-app.log`（含每次指令审计）
- exe 索引：`exe_index.json`（启动时自动重建，可删除）
- 常见问题：
  - `invalid_token` → C/B 两端 token 不一致
  - `did not receive a valid HTTP response` → B 端插件未运行或端口不通
  - 指令像卡死 → 确认只有**一个** C 端实例在线
