# Python ONVIF 虚拟摄像头（Windows / Python 3.12）

这个项目提供一个可在 Windows 上运行的 Python ONVIF 虚拟摄像头服务，支持：

- ONVIF 设备服务 (`device_service`)
- ONVIF 媒体服务 (`media_service`)
- ONVIF 事件服务 (`events_service`) + PullPoint 订阅地址
- ONVIF PTZ 服务 (`ptz_service`)
- WS-Discovery（UDP 3702）自动发现
- 自定义 RTSP：`rtsp://admin:a1234567@10.0.0.45:554/stream1`
- OpenCV 动态侦测，并通过 ONVIF Event 输出
- OpenCV 调试窗口（可选）

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

启用 cv2 调试窗口：

```powershell
python virtual_onvif_camera.py --host 0.0.0.0 --service-host 10.0.0.45 --debug-window
```

默认账号：
- 用户名：`admin`
- 密码：`a1234567`

## 3. NVR 事件订阅失败排查（重点）

1. **CreatePullPointSubscription 成功但 PullMessages 失败**
   - 订阅成功后，客户端应访问返回的 `SubscriptionReference` 地址（`/onvif/pullpoint/{token}`）拉取消息。
   - 订阅默认有效期 1 小时，过期后会返回订阅不存在/过期错误。

2. **时间格式导致兼容问题**
   - 事件时间字段 (`CurrentTime` / `TerminationTime` / `UtcTime`) 使用 `YYYY-MM-DDTHH:MM:SS.mmmZ` 格式（UTC + 毫秒）。

3. **事件 Topic 不匹配**
   - 已提供 `RuleEngine/CellMotionDetector/Motion` 主题以及 `GetEventProperties` 中常用 Dialect/FixedTopicSet 字段。

4. **网络与认证问题**
   - 放行防火墙：UDP `3702`、TCP `8000`。
   - 服务支持 HTTP Basic 和常见 WS-Security UsernameToken。

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
