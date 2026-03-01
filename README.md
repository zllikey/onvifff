# ONVIF 虚拟摄像头（Python）

这是一个基于 FastAPI 的 ONVIF 虚拟摄像头示例服务，监听端口 **8000**，提供：

- ONVIF Device / Media / Events 基础 SOAP 接口
- 通过 **GET** 触发移动侦测与人形侦测
- `GetStreamUri` 默认返回 RTSP：`rtsp://10.0.0.20:8554/tpipc45`
- 支持 ONVIF 事件主动推送到 NVR 回调地址
- 支持 NVR 使用 `SOAPAction: "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"` 发起订阅

## 参数配置（仅 Python 文件内）

在 `main.py` 里修改：

- `STREAM_CONFIG`：流参数与默认 `stream_uri`
- `EVENT_PUSH_CONFIG`：主动推送配置
  - `callback_url`
  - `enabled`
  - `timeout_sec`

## 关键功能说明

### 1) 事件订阅（SubscribeRequest）

NVR 可向 `POST /onvif/events_service` 发送 WS-Notification `Subscribe` 请求，并携带头：

- `SOAPAction: "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"`

服务会解析 `Subscribe` 消息中的回调地址（`ConsumerReference/Address`），保存订阅目标。

### 2) 事件主动推送

触发移动/人形侦测后，服务会把 ONVIF SOAP `Notify` 主动推送到：

1. `EVENT_PUSH_CONFIG.callback_url`（默认回调地址）
2. 所有通过 `SubscribeRequest` 注册的回调地址

### 3) 侦测触发（GET）

- 移动侦测：`GET /webhook/motion?source=detector-a&confidence=0.93&duration_sec=10`
- 人形侦测：`GET /webhook/person?source=detector-a&confidence=0.98&duration_sec=10`

### 4) ONVIF SOAP 服务

- Device Service: `POST /onvif/device_service`
- Media Service: `POST /onvif/media_service`
- Events Service: `POST /onvif/events_service`

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

服务默认地址：`http://127.0.0.1:8000`
