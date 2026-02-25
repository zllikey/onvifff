# Python ONVIF 虚拟摄像头（Windows / Python 3.12）

这个项目提供一个可在 Windows 上运行的 Python 版 ONVIF 虚拟摄像头服务，主要能力：

- ONVIF 设备服务 (`device_service`)
- ONVIF 媒体服务 (`media_service`)
- ONVIF 事件服务 (`events_service`)
- ONVIF PTZ 服务 (`ptz_service`)
- WS-Discovery（UDP 3702）自动发现
- 自定义 RTSP 转发：`rtsp://admin:a1234567@10.0.0.45:554/stream1`
- 使用 OpenCV 的动态侦测（背景建模 + 轮廓面积阈值）并通过 ONVIF Event PullMessages 输出

> 说明：ONVIF 标准非常大，本实现覆盖了 IPC/NVR 互通时最常用的核心接口（设备信息、能力、Profile、流地址、事件订阅与拉取、基础 PTZ 命令响应）。如果需要对接特定 VMS/平台，可继续扩展 SOAP Action。

## 1. 环境准备

- Windows 10/11
- Python 3.12

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. 启动

```powershell
python virtual_onvif_camera.py --host 0.0.0.0 --port 8000 --rtsp rtsp://admin:a1234567@10.0.0.45:554/stream1
```

默认 ONVIF 账号密码：

- 用户名：`admin`
- 密码：`a1234567`

## 3. 服务地址

- Device Service: `http://<你的IP>:8000/onvif/device_service`
- Media Service: `http://<你的IP>:8000/onvif/media_service`
- Events Service: `http://<你的IP>:8000/onvif/events_service`
- PTZ Service: `http://<你的IP>:8000/onvif/ptz_service`

## 4. 已实现 ONVIF Action

### Device
- `GetDeviceInformation`
- `GetCapabilities`
- `GetServices`
- `GetSystemDateAndTime`
- `GetScopes`

### Media
- `GetProfiles`
- `GetStreamUri`（返回自定义 RTSP）
- `GetSnapshotUri`

### Events
- `GetEventProperties`
- `CreatePullPointSubscription`
- `PullMessages`（带运动状态与置信度）
- `Renew`
- `Unsubscribe`

### PTZ
- `GetConfigurations`
- `ContinuousMove`
- `Stop`

## 5. 动态侦测实现

`cv2` 检测链路：

1. RTSP 解码
2. 灰度化 + 高斯滤波
3. `BackgroundSubtractorMOG2`
4. 二值化 + 形态学
5. 轮廓面积累加得到 `motion_score`
6. 超阈值后输出 `IsMotion=true`，并给出 `Confidence`

## 6. 常见问题

1. **VMS 搜不到设备**
   - 确认 Windows 防火墙放行 TCP `8000`、UDP `3702`。
   - 确认服务启动在 `0.0.0.0`。

2. **能发现但预览失败**
   - 核对 RTSP 原始地址可直接播放。
   - 某些平台要求 RTSP 与 ONVIF 用户一致，可改为同一凭据。

3. **事件不更新**
   - 确认视频里有明显运动。
   - 调低/调高代码中的面积阈值 `1500` 以适配场景。
