# Python ONVIF 虚拟摄像头（Windows / Python 3.12）

这个项目提供一个可在 Windows 上运行的 Python ONVIF 虚拟摄像头服务，支持：

- ONVIF 设备服务 (`device_service`)
- ONVIF 媒体服务 (`media_service`)
- ONVIF 事件服务 (`events_service`)
- ONVIF PTZ 服务 (`ptz_service`)
- WS-Discovery（UDP 3702）自动发现
- 自定义 RTSP：`rtsp://admin:a1234567@10.0.0.45:554/stream1`
- 使用 OpenCV 的动态侦测并通过 ONVIF Event 输出

## 1. 安装（Windows）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. 启动

> 关键：如果客户端发现后连接失败，请显式设置 `--service-host` 为你的 Windows 实际网卡 IP（不要填 0.0.0.0）。

```powershell
python virtual_onvif_camera.py --host 0.0.0.0 --service-host 10.0.0.45 --port 8000 --rtsp rtsp://admin:a1234567@10.0.0.45:554/stream1
```

默认账号：
- 用户名：`admin`
- 密码：`a1234567`

## 3. 发现/连接失败排查

1. **ONVIF 不能被发现**
   - 确认 Windows 防火墙放行 **UDP 3702**。
   - 确认程序正在运行，日志中有 `WS-Discovery responder listening on UDP/3702`。

2. **发现了但不能连接 ONVIF 服务**
   - 确认 Windows 防火墙放行 **TCP 8000**。
   - 启动时设置 `--service-host` 为设备实际 IP（如 `10.0.0.45`）。
   - 在浏览器访问 `http://10.0.0.45:8000/` 确认 XAddr 正确。

3. **认证失败**
   - 服务支持 HTTP Basic。
   - 同时兼容常见 ONVIF 客户端发送的 WS-Security `Username/Password`（明文 UsernameToken）。

## 4. 已实现 ONVIF Action

### Device
- `GetDeviceInformation`
- `GetCapabilities`
- `GetServices`
- `GetSystemDateAndTime`
- `GetScopes`

### Media
- `GetProfiles`
- `GetStreamUri`
- `GetSnapshotUri`

### Events
- `GetEventProperties`
- `CreatePullPointSubscription`
- `PullMessages`
- `Renew`
- `Unsubscribe`

### PTZ
- `GetConfigurations`
- `ContinuousMove`
- `Stop`
