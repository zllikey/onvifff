# ONVIF 虚拟摄像头（Python）

这是一个基于 FastAPI 的 ONVIF 虚拟摄像头示例服务，监听端口 **8000**，提供：

- ONVIF Device / Media / Events 基础 SOAP 接口
- Webhook 触发的移动侦测与人形侦测事件
- `GetStreamUri` 默认返回 RTSP 流：`rtsp://10.0.0.20:8554/tpipc45`

> 说明：该实现覆盖常见联调所需的标准接口子集（GetCapabilities / GetServices / GetProfiles / GetStreamUri / PullMessages 等），可用于平台接入验证和流程联调。

## 参数配置（仅 Python 文件内）

按需求，流参数不再提供 Web 配置接口。请直接在 `main.py` 中修改 `STREAM_CONFIG`：

- `width`
- `height`
- `fps`
- `bitrate_kbps`
- `codec`
- `stream_uri`

默认：

- `stream_uri = rtsp://10.0.0.20:8554/tpipc45`

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

### 2) 侦测 Webhook

- 移动侦测触发：`POST /webhook/motion`
- 人形侦测触发：`POST /webhook/person`

请求体示例：

```json
{
  "source": "detector-a",
  "confidence": 0.93,
  "duration_sec": 10,
  "metadata": {
    "zone": "entrance"
  }
}
```

## ONVIF 联调建议

1. 设备发现后调用 `GetCapabilities` 与 `GetServices`
2. 调用 `GetProfiles` / `GetStreamUri` 获取流地址
3. 调用 `CreatePullPointSubscription` + `PullMessages` 拉取侦测事件
4. 通过 webhook 模拟告警输入，验证平台告警链路
