#!/usr/bin/env python3
"""ONVIF virtual camera emulator with webhook-triggered motion/human detection events."""
from __future__ import annotations

import datetime as dt
import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib import request as urllib_request
from xml.etree import ElementTree as ET

from flask import Flask, Response, jsonify, redirect, render_template_string, request

# ==============================
# Configuration (edit in this file)
# ==============================
CONFIG = {
    "host": "0.0.0.0",
    "port": 80,
    "camera_name": "Virtual-ONVIF-Cam",
    "manufacturer": "VirtualCam",
    "model": "ONVIF-Python-Emulator",
    "firmware_version": "1.1.0",
    "serial_number": "VCAM-0001",
    "hardware_id": "PY-ONVIF",
    "scopes": [
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/Profile/Streaming",
        "onvif://www.onvif.org/name/Virtual-ONVIF-Cam",
        "onvif://www.onvif.org/location/china",
        "onvif://www.onvif.org/hardware/PY-ONVIF",
    ],
    # Default stream urls (main/sub can be changed later)
    "main_stream_uri": "rtsp://10.0.0.20:8554/tpipc45",
    "sub_stream_uri": "rtsp://10.0.0.20:8554/tpipc45",
    # Snapshot uri often queried by NVRs
    "snapshot_uri": "http://10.0.0.20/snapshot.jpg",
    # Optional proactive NVR event callback endpoint.
    "nvr_callback_url": "",
    # Device hostname displayed in Device Management responses.
    "hostname": "virtual-onvif-camera",
    # ONVIF service URLs
    "device_service_path": "/onvif/device_service",
    "media_service_path": "/onvif/media_service",
    "events_service_path": "/onvif/events_service",
    "imaging_service_path": "/onvif/imaging_service",
    "ptz_service_path": "/onvif/ptz_service",
}

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSN = "http://docs.oasis-open.org/wsn/b-2"
WSNT = "http://docs.oasis-open.org/wsn/bw-2"
WSA = "http://www.w3.org/2005/08/addressing"
ONVIF_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
ONVIF_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
ONVIF_EVENTS = "http://www.onvif.org/ver10/events/wsdl"
ONVIF_IMAGING = "http://www.onvif.org/ver20/imaging/wsdl"
ONVIF_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
ONVIF_SCHEMA = "http://www.onvif.org/ver10/schema"

NS = {
    "soap": SOAP_ENV,
    "wsnt": WSNT,
    "wsa": WSA,
    "wsn": WSN,
    "tds": ONVIF_DEVICE,
    "trt": ONVIF_MEDIA,
    "tev": ONVIF_EVENTS,
    "timg": ONVIF_IMAGING,
    "tptz": ONVIF_PTZ,
    "tt": ONVIF_SCHEMA,
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

app = Flask(__name__)


@dataclass
class Subscription:
    id: str
    address: str
    created_at: str


subscriptions: List[Subscription] = []
last_events: List[Dict[str, str]] = []


def now_utc() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def local_name(tag: str) -> str:
    return tag.split("}")[-1]


def get_service_url(path: str) -> str:
    scheme = "http"
    host = request.host.split(":")[0] if request else CONFIG["host"]
    return f"{scheme}://{host}:{CONFIG['port']}{path}"


def soap_response(body: ET.Element) -> Response:
    env = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    ET.SubElement(env, f"{{{SOAP_ENV}}}Body").append(body)
    xml_bytes = ET.tostring(env, encoding="utf-8", xml_declaration=True)
    return Response(xml_bytes, content_type="application/soap+xml; charset=utf-8")


def soap_fault(reason: str, code: str = "soap:Sender") -> Response:
    fault = ET.Element(f"{{{SOAP_ENV}}}Fault")
    code_el = ET.SubElement(fault, f"{{{SOAP_ENV}}}Code")
    ET.SubElement(code_el, f"{{{SOAP_ENV}}}Value").text = code
    reason_el = ET.SubElement(fault, f"{{{SOAP_ENV}}}Reason")
    ET.SubElement(reason_el, f"{{{SOAP_ENV}}}Text").text = reason
    return soap_response(fault)


def parse_soap_body(xml_data: bytes) -> ET.Element:
    root = ET.fromstring(xml_data)
    body = root.find("soap:Body", NS)
    if body is None or len(body) == 0:
        raise ValueError("Missing SOAP body")
    return body[0]


def http_post_xml(url: str, payload: bytes, soap_action: str = "") -> None:
    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    if soap_action:
        headers["SOAPAction"] = soap_action
    req = urllib_request.Request(url=url, data=payload, headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=5):
        pass


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def build_service_node(parent: ET.Element, namespace: str, xaddr: str, version_major: int = 1, version_minor: int = 0, include_caps: bool = True) -> None:
    srv = ET.SubElement(parent, f"{{{ONVIF_DEVICE}}}Service")
    ET.SubElement(srv, f"{{{ONVIF_DEVICE}}}Namespace").text = namespace
    ET.SubElement(srv, f"{{{ONVIF_DEVICE}}}XAddr").text = xaddr
    ET.SubElement(srv, f"{{{ONVIF_DEVICE}}}Version")
    version = srv.find(f"{{{ONVIF_DEVICE}}}Version")
    ET.SubElement(version, f"{{{ONVIF_SCHEMA}}}Major").text = str(version_major)
    ET.SubElement(version, f"{{{ONVIF_SCHEMA}}}Minor").text = str(version_minor)
    if include_caps:
        ET.SubElement(srv, f"{{{ONVIF_DEVICE}}}Capabilities")


def build_notification_xml(topic: str, msg: str) -> bytes:
    env = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    body = ET.SubElement(env, f"{{{SOAP_ENV}}}Body")
    notify = ET.SubElement(body, f"{{{WSN}}}Notify")
    message = ET.SubElement(notify, f"{{{WSN}}}NotificationMessage")
    ET.SubElement(message, f"{{{WSN}}}Topic").text = topic
    data = ET.SubElement(message, f"{{{WSN}}}Message")
    simple_item = ET.SubElement(data, f"{{{ONVIF_SCHEMA}}}Message")
    simple_item.set("UtcTime", now_utc())
    simple_item.set("PropertyOperation", "Changed")
    ET.SubElement(simple_item, f"{{{ONVIF_SCHEMA}}}Source").text = CONFIG["camera_name"]
    ET.SubElement(simple_item, f"{{{ONVIF_SCHEMA}}}Data").text = msg
    return ET.tostring(env, encoding="utf-8", xml_declaration=True)


def push_event(event_type: str, message: str) -> None:
    payload = build_notification_xml(topic=f"tns1:RuleEngine/CellMotionDetector/{event_type}", msg=message)
    sent_to: List[str] = []

    for sub in list(subscriptions):
        try:
            http_post_xml(sub.address, payload)
            sent_to.append(sub.address)
        except Exception:
            continue

    if CONFIG["nvr_callback_url"]:
        try:
            http_post_xml(CONFIG["nvr_callback_url"], payload)
            sent_to.append(CONFIG["nvr_callback_url"])
        except Exception:
            pass

    last_events.append(
        {
            "time": now_utc(),
            "event_type": event_type,
            "message": message,
            "targets": ", ".join(sent_to) if sent_to else "none",
        }
    )
    del last_events[:-50]


def find_text(parent: ET.Element, path: str, default: Optional[str] = None) -> Optional[str]:
    found = parent.find(path, NS)
    if found is None or found.text is None:
        return default
    return found.text.strip()


@app.get("/")
def index() -> str:
    html = """
    <html><head><title>Virtual ONVIF Camera</title>
    <style>body{font-family:Arial;margin:30px;max-width:1080px} code{background:#f4f4f4;padding:2px 4px}
    .card{border:1px solid #ddd;padding:16px;margin:12px 0;border-radius:8px}</style></head>
    <body>
      <h1>Virtual ONVIF Camera</h1>
      <div class="card">
        <h3>Status</h3>
        <p><b>Camera:</b> {{cfg['camera_name']}}</p>
        <p><b>Main stream:</b> <code>{{cfg['main_stream_uri']}}</code></p>
        <p><b>Sub stream:</b> <code>{{cfg['sub_stream_uri']}}</code></p>
        <p><b>Subscriptions:</b> {{subs|length}}</p>
      </div>
      <div class="card">
        <h3>Service URLs</h3>
        <ul>
          <li>Device: <code>{{device_url}}</code></li>
          <li>Media: <code>{{media_url}}</code></li>
          <li>Events: <code>{{events_url}}</code></li>
          <li>Imaging: <code>{{imaging_url}}</code></li>
          <li>PTZ: <code>{{ptz_url}}</code></li>
        </ul>
      </div>
      <div class="card">
        <h3>Usage</h3>
        <ul>
          <li>触发移动侦测(GET): <code>/trigger/motion?msg=MotionDetected</code></li>
          <li>触发人形侦测(GET): <code>/trigger/human?msg=HumanDetected</code></li>
          <li>查看状态JSON: <code>/status</code></li>
        </ul>
        <p>Subscribe 必须带 SOAPAction: <code>"http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"</code>.</p>
      </div>
      <div class="card">
        <h3>Recent events</h3>
        <pre>{{events}}</pre>
      </div>
    </body></html>
    """
    return render_template_string(
        html,
        cfg=CONFIG,
        subs=subscriptions,
        events="\n".join(f"{e['time']} | {e['event_type']} | {e['targets']} | {e['message']}" for e in last_events[-10:]),
        device_url=get_service_url(CONFIG["device_service_path"]),
        media_url=get_service_url(CONFIG["media_service_path"]),
        events_url=get_service_url(CONFIG["events_service_path"]),
        imaging_url=get_service_url(CONFIG["imaging_service_path"]),
        ptz_url=get_service_url(CONFIG["ptz_service_path"]),
    )


@app.get("/status")
def status() -> Response:
    return jsonify(
        {
            "camera": CONFIG,
            "subscriptions": [s.__dict__ for s in subscriptions],
            "last_events": last_events[-20:],
        }
    )


@app.get("/trigger/motion")
def trigger_motion() -> Response:
    msg = request.args.get("msg", "MotionDetected")
    threading.Thread(target=push_event, args=("Motion", msg), daemon=True).start()
    return jsonify({"ok": True, "event": "motion", "message": msg})


@app.get("/trigger/human")
def trigger_human() -> Response:
    msg = request.args.get("msg", "HumanDetected")
    threading.Thread(target=push_event, args=("Human", msg), daemon=True).start()
    return jsonify({"ok": True, "event": "human", "message": msg})


@app.post(CONFIG["device_service_path"])
def device_service() -> Response:
    try:
        action = parse_soap_body(request.data)
    except Exception as exc:
        return soap_fault(f"Invalid SOAP request: {exc}")

    local = local_name(action.tag)

    if local == "GetDeviceInformation":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetDeviceInformationResponse")
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Manufacturer").text = CONFIG["manufacturer"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Model").text = CONFIG["model"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}FirmwareVersion").text = CONFIG["firmware_version"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}SerialNumber").text = CONFIG["serial_number"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}HardwareId").text = CONFIG["hardware_id"]
        return soap_response(resp)

    if local == "GetSystemDateAndTime":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetSystemDateAndTimeResponse")
        sdt = ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}SystemDateAndTime")
        ET.SubElement(sdt, f"{{{ONVIF_SCHEMA}}}DateTimeType").text = "NTP"
        ET.SubElement(sdt, f"{{{ONVIF_SCHEMA}}}DaylightSavings").text = "false"
        tz = ET.SubElement(sdt, f"{{{ONVIF_SCHEMA}}}TimeZone")
        ET.SubElement(tz, f"{{{ONVIF_SCHEMA}}}TZ").text = "CST-8"
        utc = ET.SubElement(sdt, f"{{{ONVIF_SCHEMA}}}UTCDateTime")
        now = dt.datetime.utcnow()
        date = ET.SubElement(utc, f"{{{ONVIF_SCHEMA}}}Date")
        ET.SubElement(date, f"{{{ONVIF_SCHEMA}}}Year").text = str(now.year)
        ET.SubElement(date, f"{{{ONVIF_SCHEMA}}}Month").text = str(now.month)
        ET.SubElement(date, f"{{{ONVIF_SCHEMA}}}Day").text = str(now.day)
        time = ET.SubElement(utc, f"{{{ONVIF_SCHEMA}}}Time")
        ET.SubElement(time, f"{{{ONVIF_SCHEMA}}}Hour").text = str(now.hour)
        ET.SubElement(time, f"{{{ONVIF_SCHEMA}}}Minute").text = str(now.minute)
        ET.SubElement(time, f"{{{ONVIF_SCHEMA}}}Second").text = str(now.second)
        return soap_response(resp)

    if local == "GetHostname":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetHostnameResponse")
        host = ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}HostnameInformation")
        ET.SubElement(host, f"{{{ONVIF_SCHEMA}}}FromDHCP").text = "false"
        ET.SubElement(host, f"{{{ONVIF_SCHEMA}}}Name").text = CONFIG["hostname"]
        return soap_response(resp)

    if local == "GetScopes":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetScopesResponse")
        for scope_text in CONFIG["scopes"]:
            scope = ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Scopes")
            ET.SubElement(scope, f"{{{ONVIF_SCHEMA}}}ScopeDef").text = "Fixed"
            ET.SubElement(scope, f"{{{ONVIF_SCHEMA}}}ScopeItem").text = scope_text
        return soap_response(resp)

    if local == "GetServices":
        include_caps = (find_text(action, "tds:IncludeCapability", "false") or "false").lower() == "true"
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetServicesResponse")
        build_service_node(resp, ONVIF_DEVICE, get_service_url(CONFIG["device_service_path"]), 2, 42, include_caps)
        build_service_node(resp, ONVIF_MEDIA, get_service_url(CONFIG["media_service_path"]), 2, 4, include_caps)
        build_service_node(resp, ONVIF_EVENTS, get_service_url(CONFIG["events_service_path"]), 2, 6, include_caps)
        build_service_node(resp, ONVIF_IMAGING, get_service_url(CONFIG["imaging_service_path"]), 2, 1, include_caps)
        build_service_node(resp, ONVIF_PTZ, get_service_url(CONFIG["ptz_service_path"]), 2, 0, include_caps)
        return soap_response(resp)

    if local == "GetCapabilities":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetCapabilitiesResponse")
        caps = ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Capabilities")

        dev = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Device")
        ET.SubElement(dev, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["device_service_path"])

        media = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Media")
        ET.SubElement(media, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["media_service_path"])
        ET.SubElement(media, f"{{{ONVIF_SCHEMA}}}StreamingCapabilities")
        stream_caps = media.find(f"{{{ONVIF_SCHEMA}}}StreamingCapabilities")
        ET.SubElement(stream_caps, f"{{{ONVIF_SCHEMA}}}RTPMulticast").text = "false"
        ET.SubElement(stream_caps, f"{{{ONVIF_SCHEMA}}}RTP_TCP").text = "true"
        ET.SubElement(stream_caps, f"{{{ONVIF_SCHEMA}}}RTP_RTSP_TCP").text = "true"

        events = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Events")
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["events_service_path"])
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}WSSubscriptionPolicySupport").text = "true"
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}WSPullPointSupport").text = "true"
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}WSPausableSubscriptionManagerInterfaceSupport").text = "false"

        imaging = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Imaging")
        ET.SubElement(imaging, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["imaging_service_path"])

        ptz = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}PTZ")
        ET.SubElement(ptz, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["ptz_service_path"])
        return soap_response(resp)

    return soap_fault(f"Unsupported device action: {local}")


@app.post(CONFIG["media_service_path"])
def media_service() -> Response:
    try:
        action = parse_soap_body(request.data)
    except Exception as exc:
        return soap_fault(f"Invalid SOAP request: {exc}")

    local = local_name(action.tag)

    if local == "GetServiceCapabilities":
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetServiceCapabilitiesResponse")
        caps = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}Capabilities")
        caps.set("SnapshotUri", "true")
        caps.set("Rotation", "false")
        caps.set("VideoSourceMode", "false")
        caps.set("OSD", "false")
        caps.set("TemporaryOSDText", "false")
        caps.set("Mask", "false")
        caps.set("ProfileCapabilities", "true")
        return soap_response(resp)

    if local == "GetProfiles":
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetProfilesResponse")
        for token, name in (("main", "MainStream"), ("sub", "SubStream")):
            profile = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}Profiles")
            profile.set("token", token)
            ET.SubElement(profile, f"{{{ONVIF_SCHEMA}}}Name").text = name
            venc = ET.SubElement(profile, f"{{{ONVIF_SCHEMA}}}VideoEncoderConfiguration")
            ET.SubElement(venc, f"{{{ONVIF_SCHEMA}}}Name").text = f"{name}Enc"
            ET.SubElement(venc, f"{{{ONVIF_SCHEMA}}}UseCount").text = "1"
        return soap_response(resp)

    if local == "GetProfile":
        token = find_text(action, "trt:ProfileToken", "main") or "main"
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetProfileResponse")
        profile = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}Profile")
        profile.set("token", token)
        ET.SubElement(profile, f"{{{ONVIF_SCHEMA}}}Name").text = "MainStream" if token == "main" else "SubStream"
        return soap_response(resp)

    if local == "GetVideoSources":
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetVideoSourcesResponse")
        vs = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}VideoSources")
        vs.set("token", "VideoSourceToken")
        ET.SubElement(vs, f"{{{ONVIF_SCHEMA}}}Framerate").text = "25"
        res = ET.SubElement(vs, f"{{{ONVIF_SCHEMA}}}Resolution")
        ET.SubElement(res, f"{{{ONVIF_SCHEMA}}}Width").text = "1920"
        ET.SubElement(res, f"{{{ONVIF_SCHEMA}}}Height").text = "1080"
        ET.SubElement(vs, f"{{{ONVIF_SCHEMA}}}Imaging").text = "ImagingToken"
        return soap_response(resp)

    if local == "GetStreamUri":
        token = find_text(action, "trt:ProfileToken", "main") or "main"
        uri = CONFIG["main_stream_uri"] if token == "main" else CONFIG["sub_stream_uri"]
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetStreamUriResponse")
        media_uri = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}MediaUri")
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}Uri").text = uri
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}InvalidAfterConnect").text = "false"
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}InvalidAfterReboot").text = "false"
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}Timeout").text = "PT60S"
        return soap_response(resp)

    if local == "GetSnapshotUri":
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetSnapshotUriResponse")
        media_uri = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}MediaUri")
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}Uri").text = CONFIG["snapshot_uri"]
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}InvalidAfterConnect").text = "false"
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}InvalidAfterReboot").text = "false"
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}Timeout").text = "PT60S"
        return soap_response(resp)

    return soap_fault(f"Unsupported media action: {local}")


@app.post(CONFIG["events_service_path"])
def events_service() -> Response:
    soap_action = request.headers.get("SOAPAction", "").strip('"')
    try:
        action = parse_soap_body(request.data)
    except Exception as exc:
        return soap_fault(f"Invalid SOAP request: {exc}")

    local = local_name(action.tag)

    if local == "GetServiceCapabilities":
        resp = ET.Element(f"{{{ONVIF_EVENTS}}}GetServiceCapabilitiesResponse")
        caps = ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}Capabilities")
        caps.set("WSSubscriptionPolicySupport", "true")
        caps.set("WSPullPointSupport", "true")
        caps.set("WSPausableSubscriptionManagerInterfaceSupport", "false")
        return soap_response(resp)

    if local == "GetEventProperties":
        resp = ET.Element(f"{{{ONVIF_EVENTS}}}GetEventPropertiesResponse")
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}TopicNamespaceLocation").text = "http://www.onvif.org/ver10/topics/topicns.xml"
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}FixedTopicSet").text = "true"
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}TopicSet")
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}TopicExpressionDialect").text = "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete"
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}MessageContentFilterDialect").text = "http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter"
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}ProducerPropertiesFilterDialect").text = "http://www.onvif.org/ver10/tev/producerPropertiesFilter/ItemFilter"
        return soap_response(resp)

    if local == "CreatePullPointSubscription":
        sub_id = str(uuid.uuid4())
        subscriptions.append(Subscription(id=sub_id, address="pullpoint", created_at=now_utc()))
        resp = ET.Element(f"{{{ONVIF_EVENTS}}}CreatePullPointSubscriptionResponse")
        sub_ref = ET.SubElement(resp, f"{{{WSNT}}}SubscriptionReference")
        ET.SubElement(sub_ref, f"{{{WSA}}}Address").text = get_service_url(CONFIG["events_service_path"]) + f"/subscription/{sub_id}"
        ET.SubElement(resp, f"{{{WSNT}}}CurrentTime").text = now_utc()
        ET.SubElement(resp, f"{{{WSNT}}}TerminationTime").text = "9999-12-31T23:59:59Z"
        return soap_response(resp)

    if local == "PullMessages":
        resp = ET.Element(f"{{{ONVIF_EVENTS}}}PullMessagesResponse")
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}CurrentTime").text = now_utc()
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}TerminationTime").text = "9999-12-31T23:59:59Z"
        return soap_response(resp)

    if local == "Subscribe":
        required_action = "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"
        if soap_action != required_action:
            return soap_fault(f"Subscribe requires SOAPAction: {required_action}")

        consumer_ref = action.find("wsnt:ConsumerReference/wsa:Address", NS)
        if consumer_ref is None or not consumer_ref.text:
            return soap_fault("Subscribe requires ConsumerReference/Address")

        sub_id = str(uuid.uuid4())
        subscriptions.append(Subscription(id=sub_id, address=consumer_ref.text.strip(), created_at=now_utc()))

        resp = ET.Element(f"{{{WSNT}}}SubscribeResponse")
        sub_ref = ET.SubElement(resp, f"{{{WSNT}}}SubscriptionReference")
        ET.SubElement(sub_ref, f"{{{WSA}}}Address").text = get_service_url(CONFIG["events_service_path"]) + f"/subscription/{sub_id}"
        ET.SubElement(resp, f"{{{WSNT}}}CurrentTime").text = now_utc()
        ET.SubElement(resp, f"{{{WSNT}}}TerminationTime").text = "9999-12-31T23:59:59Z"
        return soap_response(resp)

    return soap_fault(f"Unsupported events action: {local}")


@app.post(CONFIG["imaging_service_path"])
def imaging_service() -> Response:
    try:
        action = parse_soap_body(request.data)
    except Exception as exc:
        return soap_fault(f"Invalid SOAP request: {exc}")

    local = local_name(action.tag)
    if local == "GetServiceCapabilities":
        resp = ET.Element(f"{{{ONVIF_IMAGING}}}GetServiceCapabilitiesResponse")
        ET.SubElement(resp, f"{{{ONVIF_IMAGING}}}Capabilities")
        return soap_response(resp)
    return soap_fault(f"Unsupported imaging action: {local}")


@app.post(CONFIG["ptz_service_path"])
def ptz_service() -> Response:
    try:
        action = parse_soap_body(request.data)
    except Exception as exc:
        return soap_fault(f"Invalid SOAP request: {exc}")

    local = local_name(action.tag)
    if local == "GetServiceCapabilities":
        resp = ET.Element(f"{{{ONVIF_PTZ}}}GetServiceCapabilitiesResponse")
        ET.SubElement(resp, f"{{{ONVIF_PTZ}}}Capabilities")
        return soap_response(resp)
    return soap_fault(f"Unsupported ptz action: {local}")


@app.post(f"{CONFIG['events_service_path']}/subscription/<sub_id>")
def subscription_manager(sub_id: str) -> Response:
    existing = any(s.id == sub_id for s in subscriptions)
    if not existing:
        return soap_fault("Unknown subscription", code="soap:Receiver")
    resp = ET.Element(f"{{{WSNT}}}RenewResponse")
    ET.SubElement(resp, f"{{{WSNT}}}TerminationTime").text = "9999-12-31T23:59:59Z"
    return soap_response(resp)


@app.get("/favicon.ico")
def favicon() -> Response:
    return redirect("/")


if __name__ == "__main__":
    app.run(host=CONFIG["host"], port=CONFIG["port"], debug=False)
