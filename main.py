from __future__ import annotations

import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, List, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
NS_TEV = "http://www.onvif.org/ver10/events/wsdl"
NS_TT = "http://www.onvif.org/ver10/schema"

SOAP_HEADERS = {"Content-Type": "application/soap+xml; charset=utf-8"}


class StreamConfig(BaseModel):
    width: int = Field(default=1920, ge=160, le=3840)
    height: int = Field(default=1080, ge=120, le=2160)
    fps: int = Field(default=25, ge=1, le=60)
    bitrate_kbps: int = Field(default=4096, ge=128, le=20000)
    codec: Literal["H264", "MJPEG"] = "H264"
    stream_uri: str = "rtsp://10.0.0.20:8554/tpipc45"


# 需求：流参数直接在 py 文件中设定，不通过 Web 页面/接口动态修改。
STREAM_CONFIG = StreamConfig()


class WebhookEvent(BaseModel):
    source: str = Field(default="webhook")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_sec: int = Field(default=10, ge=1, le=300)
    metadata: Dict[str, str] = Field(default_factory=dict)


@dataclass
class DetectionState:
    active_until: float = 0.0
    confidence: float = 0.0

    def active(self) -> bool:
        return time.time() < self.active_until


class EventBus:
    def __init__(self, max_events: int = 200) -> None:
        self.events: Deque[dict] = deque(maxlen=max_events)

    def publish(self, topic: str, payload: dict) -> None:
        self.events.append(
            {
                "id": str(uuid.uuid4()),
                "topic": topic,
                "payload": payload,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    def pull(self, limit: int = 10) -> List[dict]:
        return list(self.events)[-limit:]


app = FastAPI(title="ONVIF Virtual Camera", version="0.2.0")
motion_state = DetectionState()
person_state = DetectionState()
event_bus = EventBus()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def soap_envelope(inner_xml: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}">'
        f"<s:Body>{inner_xml}</s:Body>"
        f"</s:Envelope>"
    )


def _service_url(request: Request, path: str) -> str:
    return str(request.base_url).rstrip("/") + path


def build_device_response(method: str, request: Request) -> str:
    if method.endswith("GetDeviceInformation"):
        return soap_envelope(
            f"""
            <tds:GetDeviceInformationResponse xmlns:tds=\"{NS_TDS}\">
              <tds:Manufacturer>VirtualCam Inc.</tds:Manufacturer>
              <tds:Model>Python ONVIF Virtual Camera</tds:Model>
              <tds:FirmwareVersion>0.2.0</tds:FirmwareVersion>
              <tds:SerialNumber>VIRTUAL-001</tds:SerialNumber>
              <tds:HardwareId>SIM-ONVIF</tds:HardwareId>
            </tds:GetDeviceInformationResponse>
            """
        )

    if method.endswith("GetServices"):
        return soap_envelope(
            f"""
            <tds:GetServicesResponse xmlns:tds=\"{NS_TDS}\">
              <tds:Service>
                <tds:Namespace>{NS_TDS}</tds:Namespace>
                <tds:XAddr>{_service_url(request, '/onvif/device_service')}</tds:XAddr>
                <tds:Version><tt:Major xmlns:tt=\"{NS_TT}\">2</tt:Major><tt:Minor xmlns:tt=\"{NS_TT}\">0</tt:Minor></tds:Version>
              </tds:Service>
              <tds:Service>
                <tds:Namespace>{NS_TRT}</tds:Namespace>
                <tds:XAddr>{_service_url(request, '/onvif/media_service')}</tds:XAddr>
                <tds:Version><tt:Major xmlns:tt=\"{NS_TT}\">2</tt:Major><tt:Minor xmlns:tt=\"{NS_TT}\">0</tt:Minor></tds:Version>
              </tds:Service>
              <tds:Service>
                <tds:Namespace>{NS_TEV}</tds:Namespace>
                <tds:XAddr>{_service_url(request, '/onvif/events_service')}</tds:XAddr>
                <tds:Version><tt:Major xmlns:tt=\"{NS_TT}\">2</tt:Major><tt:Minor xmlns:tt=\"{NS_TT}\">0</tt:Minor></tds:Version>
              </tds:Service>
            </tds:GetServicesResponse>
            """
        )

    if method.endswith("GetCapabilities"):
        return soap_envelope(
            f"""
            <tds:GetCapabilitiesResponse xmlns:tds=\"{NS_TDS}\">
              <tds:Capabilities>
                <tt:Device xmlns:tt=\"{NS_TT}\"><tt:XAddr>{_service_url(request, '/onvif/device_service')}</tt:XAddr></tt:Device>
                <tt:Media xmlns:tt=\"{NS_TT}\"><tt:XAddr>{_service_url(request, '/onvif/media_service')}</tt:XAddr></tt:Media>
                <tt:Events xmlns:tt=\"{NS_TT}\">
                  <tt:XAddr>{_service_url(request, '/onvif/events_service')}</tt:XAddr>
                  <tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>
                  <tt:WSPullPointSupport>true</tt:WSPullPointSupport>
                </tt:Events>
              </tds:Capabilities>
            </tds:GetCapabilitiesResponse>
            """
        )

    raise HTTPException(status_code=400, detail=f"Unsupported ONVIF device operation: {method}")


def build_media_response(method: str) -> str:
    if method.endswith("GetProfiles"):
        return soap_envelope(
            f"""
            <trt:GetProfilesResponse xmlns:trt=\"{NS_TRT}\" xmlns:tt=\"{NS_TT}\">
              <trt:Profiles token=\"profile_main\" fixed=\"true\">
                <tt:Name>MainStream</tt:Name>
                <tt:VideoEncoderConfiguration token=\"encoder_main\">
                  <tt:Name>DefaultEncoder</tt:Name>
                  <tt:Encoding>{STREAM_CONFIG.codec}</tt:Encoding>
                  <tt:Resolution><tt:Width>{STREAM_CONFIG.width}</tt:Width><tt:Height>{STREAM_CONFIG.height}</tt:Height></tt:Resolution>
                  <tt:RateControl><tt:FrameRateLimit>{STREAM_CONFIG.fps}</tt:FrameRateLimit><tt:BitrateLimit>{STREAM_CONFIG.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>
                </tt:VideoEncoderConfiguration>
              </trt:Profiles>
            </trt:GetProfilesResponse>
            """
        )

    if method.endswith("GetVideoEncoderConfigurations"):
        return soap_envelope(
            f"""
            <trt:GetVideoEncoderConfigurationsResponse xmlns:trt=\"{NS_TRT}\" xmlns:tt=\"{NS_TT}\">
              <trt:Configurations token=\"encoder_main\">
                <tt:Name>DefaultEncoder</tt:Name>
                <tt:Encoding>{STREAM_CONFIG.codec}</tt:Encoding>
                <tt:Resolution><tt:Width>{STREAM_CONFIG.width}</tt:Width><tt:Height>{STREAM_CONFIG.height}</tt:Height></tt:Resolution>
                <tt:RateControl><tt:FrameRateLimit>{STREAM_CONFIG.fps}</tt:FrameRateLimit><tt:BitrateLimit>{STREAM_CONFIG.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>
              </trt:Configurations>
            </trt:GetVideoEncoderConfigurationsResponse>
            """
        )

    if method.endswith("GetStreamUri"):
        return soap_envelope(
            f"""
            <trt:GetStreamUriResponse xmlns:trt=\"{NS_TRT}\" xmlns:tt=\"{NS_TT}\">
              <trt:MediaUri>
                <tt:Uri>{STREAM_CONFIG.stream_uri}</tt:Uri>
                <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
                <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
                <tt:Timeout>PT60S</tt:Timeout>
              </trt:MediaUri>
            </trt:GetStreamUriResponse>
            """
        )

    raise HTTPException(status_code=400, detail=f"Unsupported ONVIF media operation: {method}")


def build_event_response(method: str) -> str:
    if method.endswith("CreatePullPointSubscription"):
        return soap_envelope(
            f"""
            <tev:CreatePullPointSubscriptionResponse xmlns:tev=\"{NS_TEV}\" xmlns:wsnt=\"http://docs.oasis-open.org/wsn/b-2\">
              <wsnt:CurrentTime>{_now_utc()}</wsnt:CurrentTime>
              <wsnt:TerminationTime>{_now_utc()}</wsnt:TerminationTime>
            </tev:CreatePullPointSubscriptionResponse>
            """
        )

    if method.endswith("PullMessages"):
        items = event_bus.pull(20)
        messages = ""
        for item in items:
            messages += f"""
            <wsnt:NotificationMessage>
              <wsnt:Topic>{item['topic']}</wsnt:Topic>
              <wsnt:Message>
                <tt:Message UtcTime=\"{item['ts']}\" PropertyOperation=\"Changed\" xmlns:tt=\"{NS_TT}\">
                  <tt:Data><tt:SimpleItem Name=\"topic\" Value=\"{item['topic']}\"/><tt:SimpleItem Name=\"id\" Value=\"{item['id']}\"/></tt:Data>
                </tt:Message>
              </wsnt:Message>
            </wsnt:NotificationMessage>
            """

        return soap_envelope(
            f"""
            <tev:PullMessagesResponse xmlns:tev=\"{NS_TEV}\" xmlns:wsnt=\"http://docs.oasis-open.org/wsn/b-2\">
              <wsnt:CurrentTime>{_now_utc()}</wsnt:CurrentTime>
              <wsnt:TerminationTime>{_now_utc()}</wsnt:TerminationTime>
              {messages}
            </tev:PullMessagesResponse>
            """
        )

    raise HTTPException(status_code=400, detail=f"Unsupported ONVIF event operation: {method}")


def extract_action(xml_body: str) -> str:
    root = ET.fromstring(xml_body)
    body = root.find(f"{{{NS_SOAP}}}Body")
    if body is None or not list(body):
        raise HTTPException(status_code=400, detail="Invalid SOAP body")
    return list(body)[0].tag


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "detections": {"motion": motion_state.active(), "person": person_state.active()},
        "stream": STREAM_CONFIG.model_dump(),
    }


@app.post("/webhook/motion")
def webhook_motion(payload: WebhookEvent) -> dict:
    motion_state.active_until = time.time() + payload.duration_sec
    motion_state.confidence = payload.confidence
    event_bus.publish("tns1:RuleEngine/Motion", payload.model_dump())
    return {"accepted": True, "motion_until": motion_state.active_until}


@app.post("/webhook/person")
def webhook_person(payload: WebhookEvent) -> dict:
    person_state.active_until = time.time() + payload.duration_sec
    person_state.confidence = payload.confidence
    event_bus.publish("tns1:Analytics/HumanDetection", payload.model_dump())
    return {"accepted": True, "person_until": person_state.active_until}


@app.post("/onvif/device_service")
async def onvif_device_service(request: Request) -> Response:
    method = extract_action((await request.body()).decode("utf-8"))
    xml = build_device_response(method, request)
    return Response(content=xml, media_type="application/soap+xml", headers=SOAP_HEADERS)


@app.post("/onvif/media_service")
async def onvif_media_service(request: Request) -> Response:
    method = extract_action((await request.body()).decode("utf-8"))
    xml = build_media_response(method)
    return Response(content=xml, media_type="application/soap+xml", headers=SOAP_HEADERS)


@app.post("/onvif/events_service")
async def onvif_events_service(request: Request) -> Response:
    method = extract_action((await request.body()).decode("utf-8"))
    xml = build_event_response(method)
    return Response(content=xml, media_type="application/soap+xml", headers=SOAP_HEADERS)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
