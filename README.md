# Python ONVIF 虚拟摄像头（增强标准兼容）

这是一个基于 Python + Flask 的 ONVIF 虚拟摄像头服务，重点增强了 **标准 WSDL 服务命名空间与服务发现能力**，用于提升 NVR 接入兼容性。

## 功能

- ONVIF Device / Media / Events / Imaging / PTZ 服务地址
- Device 服务补充常见标准动作：
  - `GetDeviceInformation`
  - `GetSystemDateAndTime`
  - `GetHostname`
  - `GetScopes`
  - `GetServices`（返回标准 Namespace + XAddr + Version）
  - `GetCapabilities`
- Media 服务补充常见动作：
  - `GetServiceCapabilities`
  - `GetProfiles` / `GetProfile`
  - `GetVideoSources`
  - `GetStreamUri`
  - `GetSnapshotUri`
- Events 服务补充常见动作：
  - `GetServiceCapabilities`
  - `GetEventProperties`
  - `Subscribe`
  - `CreatePullPointSubscription`
  - `PullMessages`
- 移动侦测 / 人形侦测：**GET webhook 触发**
- 事件主动推送至：
  1. 订阅回调地址（`ConsumerReference/Address`）
  2. `CONFIG["nvr_callback_url"]`（可选）
- Web 状态页与使用说明

## 默认参数

在 `app.py` 顶部 `CONFIG` 中可改：

- 监听：`0.0.0.0:80`
- 主码流：`rtsp://10.0.0.20:8554/tpipc45`
- 子码流：`rtsp://10.0.0.20:8554/tpipc45`
- 快照地址：`snapshot_uri`
- NVR 回调地址：`nvr_callback_url`

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo python3 app.py
```

## 服务地址

- Web: `http://<ip>/`
- 状态 JSON: `http://<ip>/status`
- Device: `http://<ip>/onvif/device_service`
- Media: `http://<ip>/onvif/media_service`
- Events: `http://<ip>/onvif/events_service`
- Imaging: `http://<ip>/onvif/imaging_service`
- PTZ: `http://<ip>/onvif/ptz_service`

## 触发检测（GET）

- 移动侦测：`GET /trigger/motion?msg=MotionDetected`
- 人形侦测：`GET /trigger/human?msg=HumanDetected`

## Subscribe 头要求（必须）

订阅时必须携带：

```text
SOAPAction: "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"
```

否则会返回 SOAP Fault。
