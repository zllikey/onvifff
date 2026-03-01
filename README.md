# ONVIF 虚拟摄像头（Python）

这是一个基于 FastAPI 的 ONVIF 虚拟摄像头示例服务，监听端口 **8000**，提供：

- ONVIF Device / Media / Events 基础 SOAP 接口
- 可配置流参数（分辨率、帧率、码率、编码类型、流路径）
- Webhook 触发的移动侦测与人形侦测事件
- MJPEG 虚拟视频流输出（用于调试和联调）

> 说明：该实现覆盖常见联调所需的标准接口子集（GetCapabilities / GetServices / GetProfiles / GetStreamUri / PullMessages 等），可用于平台接入验证和流程联调。

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

### 2) 虚拟视频流

- MJPEG 流地址：`GET /stream.mjpg`

### 3) 侦测 Webhook

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

### 4) 流参数配置

- 查询：`GET /config/stream`
- 更新：`PUT /config/stream`

更新示例：

```json
{
  "width": 1920,
  "height": 1080,
  "fps": 25,
  "bitrate_kbps": 4096,
  "codec": "MJPEG",
  "stream_path": "/stream.mjpg"
}
```

## ONVIF 联调建议

1. 设备发现后调用 `GetCapabilities` 与 `GetServices`
2. 调用 `GetProfiles` / `GetStreamUri` 获取流地址
3. 调用 `CreatePullPointSubscription` + `PullMessages` 拉取侦测事件
4. 通过 webhook 模拟告警输入，验证平台告警链路
