# ONVIF 虚拟摄像头（Python）

这是一个基于 FastAPI 的 ONVIF 虚拟摄像头示例服务，监听端口 **8000**，提供：

- ONVIF Device / Media / Events 基础 SOAP 接口
- 通过 **GET** 触发移动侦测与人形侦测
- `GetStreamUri` 默认返回 RTSP：`rtsp://10.0.0.20:8554/tpipc45`
- 支持 ONVIF 事件主动推送到 NVR 回调地址

## 参数配置（仅 Python 文件内）

按需求，参数都在 `main.py` 里配置：

- `STREAM_CONFIG`：流参数与默认 `stream_uri`
- `EVENT_PUSH_CONFIG`：主动推送回调配置
  - `callback_url`
  - `enabled`
  - `timeout_sec`

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

服务默认地址：`http://127.0.0.1:8000`

## 关键接口

### 1) ONVIF SOAP 服务

- Device Service: `POST /onvif/device_service`
- Media Service: `POST /onvif/media_service`
- Events Service: `POST /onvif/events_service`

### 2) 侦测触发（GET）

- 移动侦测：`GET /webhook/motion?source=detector-a&confidence=0.93&duration_sec=10`
- 人形侦测：`GET /webhook/person?source=detector-a&confidence=0.98&duration_sec=10`

调用后会：
1. 写入本地事件队列（可由 `PullMessages` 拉取）
2. 尝试主动 POST SOAP Notify 到 `EVENT_PUSH_CONFIG.callback_url`

## ONVIF 联调建议

1. 设备发现后调用 `GetCapabilities` 与 `GetServices`
2. 调用 `GetProfiles` / `GetStreamUri` 获取流地址
3. 若平台支持回调，配置 `EVENT_PUSH_CONFIG.callback_url`，触发 GET 事件验证主动推送
4. 若平台使用拉模式，调用 `CreatePullPointSubscription` + `PullMessages` 验证拉取事件
