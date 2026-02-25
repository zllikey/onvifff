# Python ONVIF 虚拟摄像头（Windows / Python 3.12）

本项目提供 Windows + Python 3.12 的 ONVIF 虚拟摄像头，支持：

- WS-Discovery 发现
- Device / Media / Events / PTZ 服务
- PullPoint 事件订阅
- WS-Notification `wsnt:Subscribe` 主动推送事件（给 NVR 的 ConsumerReference）
- OpenCV 动态侦测 + 可选调试窗口

## 安装

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 启动

```powershell
python virtual_onvif_camera.py --host 0.0.0.0 --service-host 10.0.0.45 --port 8000 --rtsp rtsp://admin:a1234567@10.0.0.45:554/stream1
```

调试窗口：

```powershell
python virtual_onvif_camera.py --host 0.0.0.0 --service-host 10.0.0.45 --debug-window
```

## 针对 NVR `wsnt:Subscribe` 的兼容点

- 支持 `wsnt:Subscribe`（不是只支持 PullPoint）
- 解析 `ConsumerReference/wsa:Address` 与 `wsa5:Address`
- 解析 `InitialTerminationTime`（如 `PT60S`）
- 收到订阅后，Python 服务会在检测到运动变化时主动 POST `wsnt:Notify` 到 NVR 提供的回调地址
- 支持 WS-Security `PasswordDigest`（Nonce + Created + Password 的 SHA1 Base64）与明文密码
- 事件时间统一 UTC 毫秒格式：`YYYY-MM-DDTHH:MM:SS.mmmZ`

## 默认账号

- 用户名：`admin`
- 密码：`a1234567`
