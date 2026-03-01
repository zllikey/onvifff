# Python ONVIF 虚拟摄像头

这是一个基于 Python + Flask 的 ONVIF 虚拟摄像头服务，支持：

- ONVIF Device / Media / Events 基础 SOAP 接口
- 主码流/子码流 URI 可配置（默认都为 `rtsp://10.0.0.20:8554/tpipc45`）
- 移动侦测和人形侦测，使用 **GET** webhook 触发
- ONVIF 订阅（Subscribe）并向订阅回调地址主动推送事件
- 可选地向 NVR 事件回调地址主动推送事件
- Web 页面查看运行状态与使用说明

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo python3 app.py
```

> 默认监听 `0.0.0.0:80`，如需修改请直接编辑 `app.py` 中 `CONFIG`。

## 关键地址

- Web 状态页: `http://<ip>/`
- 状态 JSON: `http://<ip>/status`
- Device Service: `http://<ip>/onvif/device_service`
- Media Service: `http://<ip>/onvif/media_service`
- Events Service: `http://<ip>/onvif/events_service`

## 触发侦测（GET）

- 移动侦测：
  `GET /trigger/motion?msg=MotionDetected`
- 人形侦测：
  `GET /trigger/human?msg=HumanDetected`

触发后将主动推送 ONVIF 事件给：

1. 已订阅的 `ConsumerReference/Address`
2. `CONFIG["nvr_callback_url"]`（如已配置）

## Subscribe 要求

订阅接口要求 Header 含：

```text
SOAPAction: "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"
```

且 SOAP Body 中需要包含 `ConsumerReference/Address`。

## 参数配置

在 `app.py` 顶部 `CONFIG` 中可设置：

- 监听地址与端口
- 摄像头信息
- 主/子码流地址
- NVR 回调地址
- ONVIF 各服务路径

