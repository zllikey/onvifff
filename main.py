from __future__ import annotations

import time
import uuid
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, List, Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
NS_TEV = "http://www.onvif.org/ver10/events/wsdl"
NS_TT = "http://www.onvif.org/ver10/schema"
NS_WSNT = "http://docs.oasis-open.org/wsn/b-2"
NS_WSA_2005 = "http://www.w3.org/2005/08/addressing"
NS_WSA_2004 = "http://schemas.xmlsoap.org/ws/2004/08/addressing"

SOAP_HEADERS = {"Content-Type": "application/soap+xml; charset=utf-8"}
SUBSCRIBE_ACTION = "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"


class StreamConfig(BaseModel):
    width: int = Field(default=1920, ge=160, le=3840)
    height: int = Field(default=1080, ge=120, le=2160)
    fps: int = Field(default=25, ge=1, le=60)
    bitrate_kbps: int = Field(default=4096, ge=128, le=20000)
    codec: Literal["H264", "MJPEG"] = "H264"
    stream_uri: str = "rtsp://10.0.0.20:8554/tpipc45"


class EventPushConfig(BaseModel):
    callback_url: str = "http://127.0.0.1:9000/onvif/events/callback"
    enabled: bool = True
    timeout_sec: int = Field(default=3, ge=1, le=30)


STREAM_CONFIG = StreamConfig()
EVENT_PUSH_CONFIG = EventPushConfig()


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

    def publish(self, topic: str, payload: dict) -> dict:
        item = {
            "id": str(uuid.uuid4()),
            "topic": topic,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.events.append(item)
        return item

    def pull(self, limit: int = 10) -> List[dict]:
        return list(self.events)[-limit:]


class SubscriptionStore:
    def __init__(self) -> None:
        self._targets: Dict[str, str] = {}

    def add(self, callback_url: str) -> str:
        sub_id = str(uuid.uuid4())
        self._targets[sub_id] = callback_url
        return sub_id

    def all_targets(self) -> List[str]:
        return list(self._targets.values())

    def count(self) -> int:
        return len(self._targets)


app = FastAPI(title="ONVIF Virtual Camera", version="0.4.0")
motion_state = DetectionState()
person_state = DetectionState()
event_bus = EventBus()
subscription_store = SubscriptionStore()


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
              <tds:FirmwareVersion>0.4.0</tds:FirmwareVersion>
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
                  <tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport>
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


def _extract_subscribe_callback(xml_body: str) -> Optional[str]:
    root = ET.fromstring(xml_body)
    for ns in (NS_WSA_2005, NS_WSA_2004):
        node = root.find(f".//{{{ns}}}Address")
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _build_subscribe_response(request: Request, callback_url: str) -> str:
    subscription_id = subscription_store.add(callback_url)
    return soap_envelope(
        f"""
        <wsnt:SubscribeResponse xmlns:wsnt=\"{NS_WSNT}\" xmlns:wsa=\"{NS_WSA_2005}\">
          <wsnt:SubscriptionReference>
            <wsa:Address>{_service_url(request, f'/onvif/events_service/subscriptions/{subscription_id}')}</wsa:Address>
          </wsnt:SubscriptionReference>
          <wsnt:CurrentTime>{_now_utc()}</wsnt:CurrentTime>
          <wsnt:TerminationTime>{_now_utc()}</wsnt:TerminationTime>
        </wsnt:SubscribeResponse>
        """
    )


def build_event_response(method: str, request: Request, xml_body: str) -> str:
    if method.endswith("CreatePullPointSubscription"):
        return soap_envelope(
            f"""
            <tev:CreatePullPointSubscriptionResponse xmlns:tev=\"{NS_TEV}\" xmlns:wsnt=\"{NS_WSNT}\">
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
            <wsnt:NotificationMessage xmlns:wsnt=\"{NS_WSNT}\">
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
            <tev:PullMessagesResponse xmlns:tev=\"{NS_TEV}\" xmlns:wsnt=\"{NS_WSNT}\">
              <wsnt:CurrentTime>{_now_utc()}</wsnt:CurrentTime>
              <wsnt:TerminationTime>{_now_utc()}</wsnt:TerminationTime>
              {messages}
            </tev:PullMessagesResponse>
            """
        )

    if method.endswith("Subscribe"):
        callback_url = _extract_subscribe_callback(xml_body)
        if not callback_url:
            raise HTTPException(status_code=400, detail="Subscribe request missing callback address")
        return _build_subscribe_response(request, callback_url)

    raise HTTPException(status_code=400, detail=f"Unsupported ONVIF event operation: {method}")


def build_notify_soap(event_item: dict) -> str:
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<s:Envelope xmlns:s=\"{NS_SOAP}\" xmlns:wsnt=\"{NS_WSNT}\" xmlns:tt=\"{NS_TT}\">
  <s:Body>
    <wsnt:Notify>
      <wsnt:NotificationMessage>
        <wsnt:Topic>{event_item['topic']}</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime=\"{event_item['ts']}\" PropertyOperation=\"Changed\">
            <tt:Data>
              <tt:SimpleItem Name=\"id\" Value=\"{event_item['id']}\"/>
              <tt:SimpleItem Name=\"source\" Value=\"{event_item['payload'].get('source', 'webhook')}\"/>
            </tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
    </wsnt:Notify>
  </s:Body>
</s:Envelope>
"""


def _post_soap(url: str, soap_xml: str) -> str:
    req = urllib.request.Request(
        url=url,
        data=soap_xml.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EVENT_PUSH_CONFIG.timeout_sec) as resp:
            return f"{url}:http_{resp.status}"
    except urllib.error.URLError as exc:
        return f"{url}:push_failed:{exc.reason}"


def push_event_to_callbacks(event_item: dict) -> List[str]:
    if not EVENT_PUSH_CONFIG.enabled:
        return ["disabled"]

    results: List[str] = []
    default_url = EVENT_PUSH_CONFIG.callback_url.strip()
    if default_url:
        results.append(_post_soap(default_url, build_notify_soap(event_item)))

    for callback in subscription_store.all_targets():
        if callback and callback != default_url:
            results.append(_post_soap(callback, build_notify_soap(event_item)))
    return results


def extract_action(xml_body: str, soap_action: Optional[str]) -> str:
    if soap_action:
        action = soap_action.strip().strip('"')
        if action == SUBSCRIBE_ACTION:
            return "Subscribe"

    root = ET.fromstring(xml_body)
    body = root.find(f"{{{NS_SOAP}}}Body")
    if body is None or not list(body):
        raise HTTPException(status_code=400, detail="Invalid SOAP body")

    tag = list(body)[0].tag
    if tag.endswith("Subscribe"):
        return "Subscribe"
    return tag


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "detections": {"motion": motion_state.active(), "person": person_state.active()},
        "stream": STREAM_CONFIG.model_dump(),
        "event_push": EVENT_PUSH_CONFIG.model_dump(),
        "subscriptions": {"count": subscription_store.count()},
    }


@app.get("/webhook/motion")
def webhook_motion_get(
    source: str = Query(default="webhook"),
    confidence: float = Query(default=1.0, ge=0.0, le=1.0),
    duration_sec: int = Query(default=10, ge=1, le=300),
) -> dict:
    payload = WebhookEvent(source=source, confidence=confidence, duration_sec=duration_sec)
    motion_state.active_until = time.time() + payload.duration_sec
    motion_state.confidence = payload.confidence
    event_item = event_bus.publish("tns1:RuleEngine/Motion", payload.model_dump())
    push_result = push_event_to_callbacks(event_item)
    return {
        "accepted": True,
        "trigger": "motion",
        "motion_until": motion_state.active_until,
        "event_id": event_item["id"],
        "push_result": push_result,
    }


@app.get("/webhook/person")
def webhook_person_get(
    source: str = Query(default="webhook"),
    confidence: float = Query(default=1.0, ge=0.0, le=1.0),
    duration_sec: int = Query(default=10, ge=1, le=300),
) -> dict:
    payload = WebhookEvent(source=source, confidence=confidence, duration_sec=duration_sec)
    person_state.active_until = time.time() + payload.duration_sec
    person_state.confidence = payload.confidence
    event_item = event_bus.publish("tns1:Analytics/HumanDetection", payload.model_dump())
    push_result = push_event_to_callbacks(event_item)
    return {
        "accepted": True,
        "trigger": "person",
        "person_until": person_state.active_until,
        "event_id": event_item["id"],
        "push_result": push_result,
    }


@app.post("/onvif/device_service")
async def onvif_device_service(request: Request) -> Response:
    body = (await request.body()).decode("utf-8")
    method = extract_action(body, request.headers.get("SOAPAction"))
    xml = build_device_response(method, request)
    return Response(content=xml, media_type="application/soap+xml", headers=SOAP_HEADERS)


@app.post("/onvif/media_service")
async def onvif_media_service(request: Request) -> Response:
    body = (await request.body()).decode("utf-8")
    method = extract_action(body, request.headers.get("SOAPAction"))
    xml = build_media_response(method)
    return Response(content=xml, media_type="application/soap+xml", headers=SOAP_HEADERS)


@app.post("/onvif/events_service")
async def onvif_events_service(request: Request, soapaction: Optional[str] = Header(default=None)) -> Response:
    body = (await request.body()).decode("utf-8")
    method = extract_action(body, soapaction)
    xml = build_event_response(method, request, body)
    return Response(content=xml, media_type="application/soap+xml", headers=SOAP_HEADERS)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
