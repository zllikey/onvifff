#!/usr/bin/env python3
"""Simple ONVIF virtual camera with webhook-triggered motion/human events."""
from __future__ import annotations

import datetime as dt
import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List
from urllib import request as urllib_request

from flask import Flask, Response, jsonify, redirect, render_template_string, request
from xml.etree import ElementTree as ET

# ==============================
# Configuration (edit in this file)
# ==============================
CONFIG = {
    "host": "0.0.0.0",
    "port": 80,
    "camera_name": "Virtual-ONVIF-Cam",
    "manufacturer": "VirtualCam",
    "model": "ONVIF-Python-Emulator",
    "firmware_version": "1.0.0",
    "serial_number": "VCAM-0001",
    "hardware_id": "PY-ONVIF",
    "uuid": "urn:uuid:4f0e5b5b-3c91-4f70-997a-e89be9b6f777",
    # Default stream urls (main/sub can be changed later)
    "main_stream_uri": "rtsp://10.0.0.20:8554/tpipc45",
    "sub_stream_uri": "rtsp://10.0.0.20:8554/tpipc45",
    # Optional proactive NVR event callback endpoint.
    # Example: "http://10.0.0.30:8899/onvif_event_callback"
    "nvr_callback_url": "",
    # Device service URLs shown in capabilities.
    "device_service_path": "/onvif/device_service",
    "media_service_path": "/onvif/media_service",
    "events_service_path": "/onvif/events_service",
}

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSN = "http://docs.oasis-open.org/wsn/b-2"
WSNT = "http://docs.oasis-open.org/wsn/bw-2"
WSA = "http://www.w3.org/2005/08/addressing"
ONVIF_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
ONVIF_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
ONVIF_EVENTS = "http://www.onvif.org/ver10/events/wsdl"
ONVIF_SCHEMA = "http://www.onvif.org/ver10/schema"

NS = {
    "soap": SOAP_ENV,
    "wsnt": WSNT,
    "wsa": WSA,
    "wsn": WSN,
    "tds": ONVIF_DEVICE,
    "trt": ONVIF_MEDIA,
    "tev": ONVIF_EVENTS,
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
    headers = {
        "Content-Type": "application/soap+xml; charset=utf-8",
    }
    if soap_action:
        headers["SOAPAction"] = soap_action
    req = urllib_request.Request(url=url, data=payload, headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=5):
        pass


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
    payload = build_notification_xml(
        topic=f"tns1:RuleEngine/CellMotionDetector/{event_type}",
        msg=message,
    )
    sent_to: List[str] = []

    for sub in list(subscriptions):
        try:
            http_post_xml(sub.address, payload)
            sent_to.append(sub.address)
        except Exception:
            continue

    if CONFIG["nvr_callback_url"]:
        try:
            # 主动推送到NVR事件回调地址
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


@app.get("/")
def index() -> str:
    html = """
    <html><head><title>Virtual ONVIF Camera</title>
    <style>body{font-family:Arial;margin:30px;max-width:980px} code{background:#f4f4f4;padding:2px 4px}
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
        <h3>Usage</h3>
        <ul>
          <li>ONVIF Device Service: <code>{{device_url}}</code></li>
          <li>ONVIF Media Service: <code>{{media_url}}</code></li>
          <li>ONVIF Events Service: <code>{{events_url}}</code></li>
          <li>触发移动侦测(GET): <code>/trigger/motion?msg=MotionDetected</code></li>
          <li>触发人形侦测(GET): <code>/trigger/human?msg=HumanDetected</code></li>
          <li>查看状态JSON: <code>/status</code></li>
        </ul>
        <p>订阅必须带 SOAPAction: <code>"http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"</code>.</p>
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

    local = action.tag.split("}")[-1]
    if local == "GetDeviceInformation":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetDeviceInformationResponse")
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Manufacturer").text = CONFIG["manufacturer"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Model").text = CONFIG["model"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}FirmwareVersion").text = CONFIG["firmware_version"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}SerialNumber").text = CONFIG["serial_number"]
        ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}HardwareId").text = CONFIG["hardware_id"]
        return soap_response(resp)

    if local == "GetCapabilities":
        resp = ET.Element(f"{{{ONVIF_DEVICE}}}GetCapabilitiesResponse")
        caps = ET.SubElement(resp, f"{{{ONVIF_DEVICE}}}Capabilities")

        dev = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Device")
        ET.SubElement(dev, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["device_service_path"])

        media = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Media")
        ET.SubElement(media, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["media_service_path"])

        events = ET.SubElement(caps, f"{{{ONVIF_SCHEMA}}}Events")
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}XAddr").text = get_service_url(CONFIG["events_service_path"])
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}WSSubscriptionPolicySupport").text = "true"
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}WSPullPointSupport").text = "false"
        ET.SubElement(events, f"{{{ONVIF_SCHEMA}}}WSPausableSubscriptionManagerInterfaceSupport").text = "false"
        return soap_response(resp)

    return soap_fault(f"Unsupported device action: {local}")


@app.post(CONFIG["media_service_path"])
def media_service() -> Response:
    try:
        action = parse_soap_body(request.data)
    except Exception as exc:
        return soap_fault(f"Invalid SOAP request: {exc}")

    local = action.tag.split("}")[-1]
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

    if local == "GetStreamUri":
        token = action.findtext("trt:ProfileToken", default="main", namespaces=NS)
        uri = CONFIG["main_stream_uri"] if token == "main" else CONFIG["sub_stream_uri"]
        resp = ET.Element(f"{{{ONVIF_MEDIA}}}GetStreamUriResponse")
        media_uri = ET.SubElement(resp, f"{{{ONVIF_MEDIA}}}MediaUri")
        ET.SubElement(media_uri, f"{{{ONVIF_SCHEMA}}}Uri").text = uri
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

    local = action.tag.split("}")[-1]

    if local == "GetEventProperties":
        resp = ET.Element(f"{{{ONVIF_EVENTS}}}GetEventPropertiesResponse")
        topic_ns = ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}TopicNamespaceLocation")
        topic_ns.text = "http://www.onvif.org/ver10/topics/topicns.xml"
        ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}FixedTopicSet").text = "true"
        topic_set = ET.SubElement(resp, f"{{{ONVIF_EVENTS}}}TopicSet")
        ET.SubElement(topic_set, "RuleEngine")
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


@app.post(f"{CONFIG['events_service_path']}/subscription/<sub_id>")
def subscription_manager(sub_id: str) -> Response:
    # Stub subscription manager endpoint.
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
