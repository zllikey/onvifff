import argparse
import base64
import datetime as dt
import hashlib
import http.client
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, Optional

import cv2
from flask import Flask, Response, request

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
WSA_NS = "http://www.w3.org/2005/08/addressing"
WSA_2004_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSD_NS = "http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01"
WSNT_NS = "http://docs.oasis-open.org/wsn/b-2"
WSTOP_NS = "http://docs.oasis-open.org/wsn/t-1"
TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TEV_NS = "http://www.onvif.org/ver10/events/wsdl"
TPTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
DN_NS = "http://www.onvif.org/ver10/network/wsdl"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"


@dataclass
class MotionState:
    detected: bool = False
    confidence: float = 0.0
    changed_at: float = field(default_factory=time.time)


@dataclass
class PullPointSubscription:
    token: str
    expires_at: float


@dataclass
class PushSubscription:
    token: str
    consumer_url: str
    topic_expression: str
    expires_at: float


def format_utc_xml(ts: Optional[float] = None) -> str:
    base = dt.datetime.utcfromtimestamp(ts if ts is not None else time.time())
    return base.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_duration_seconds(duration_text: Optional[str], default_seconds: int = 3600) -> int:
    if not duration_text:
        return default_seconds
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_text.strip())
    if not m:
        return default_seconds
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else default_seconds


class MotionDetector(threading.Thread):
    def __init__(self, rtsp_url: str, debug_window: bool = False):
        super().__init__(daemon=True)
        self.rtsp_url = rtsp_url
        self.debug_window = debug_window
        self.state = MotionState()
        self.state_lock = threading.Lock()
        self._stop_event = threading.Event()

    def get_state(self) -> MotionState:
        with self.state_lock:
            return MotionState(self.state.detected, self.state.confidence, self.state.changed_at)

    def run(self):
        logging.info("Starting motion detector from RTSP: %s", self.rtsp_url)
        cap = cv2.VideoCapture(self.rtsp_url)
        subtractor = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)

        if not cap.isOpened():
            logging.error("Unable to open RTSP stream for motion detection.")
            return

        if self.debug_window:
            cv2.namedWindow("motion-debug", cv2.WINDOW_NORMAL)
            cv2.namedWindow("motion-mask", cv2.WINDOW_NORMAL)

        while not self._stop_event.is_set():
            ok, frame = cap.read()
            if not ok:
                logging.warning("RTSP frame read failed; retrying...")
                time.sleep(0.3)
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            fg = subtractor.apply(gray)
            _, th = cv2.threshold(fg, 180, 255, cv2.THRESH_BINARY)
            th = cv2.erode(th, None, iterations=1)
            th = cv2.dilate(th, None, iterations=2)

            contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            motion_score = sum(cv2.contourArea(c) for c in contours)
            detected = motion_score > 1500
            confidence = min(1.0, motion_score / 15000)

            with self.state_lock:
                if detected != self.state.detected:
                    self.state.changed_at = time.time()
                self.state.detected = detected
                self.state.confidence = confidence

            if self.debug_window:
                frame_dbg = frame.copy()
                cv2.putText(frame_dbg, f"motion={detected} conf={confidence:.2f} score={motion_score:.0f}", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow("motion-debug", frame_dbg)
                cv2.imshow("motion-mask", th)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    self._stop_event.set()

            time.sleep(0.05)

        if self.debug_window:
            cv2.destroyAllWindows()
        cap.release()


class WSDiscoveryResponder(threading.Thread):
    def __init__(self, endpoint_uuid: str, xaddr: str):
        super().__init__(daemon=True)
        self.endpoint_uuid = endpoint_uuid
        self.xaddr = xaddr
        self._stop_event = threading.Event()

    @staticmethod
    def _extract_message_id(xml_text: str) -> str:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return ""
        for ns in (WSA_NS, WSA_2004_NS):
            node = root.find(f".//{{{ns}}}MessageID")
            if node is not None and node.text:
                return node.text.strip()
        return ""

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 3702))
        mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        logging.info("WS-Discovery responder listening on UDP/3702")
        while not self._stop_event.is_set():
            try:
                sock.settimeout(1.0)
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue

            text = data.decode("utf-8", errors="ignore")
            if "Probe" not in text:
                continue

            relates_to = self._extract_message_id(text) or "urn:uuid:probe"
            message_id = f"urn:uuid:{uuid.uuid4()}"
            resp = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{SOAP_NS}" xmlns:wsa="{WSA_NS}" xmlns:wsd="{WSD_NS}" xmlns:dn="{DN_NS}">
  <soap:Header>
    <wsa:MessageID>{message_id}</wsa:MessageID>
    <wsa:RelatesTo>{relates_to}</wsa:RelatesTo>
    <wsa:To>http://www.w3.org/2005/08/addressing/anonymous</wsa:To>
    <wsa:Action>{WSD_NS}/ProbeMatches</wsa:Action>
  </soap:Header>
  <soap:Body>
    <wsd:ProbeMatches>
      <wsd:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:{self.endpoint_uuid}</wsa:Address></wsa:EndpointReference>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
        <wsd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/PythonVirtualCam</wsd:Scopes>
        <wsd:XAddrs>{self.xaddr}</wsd:XAddrs>
        <wsd:MetadataVersion>1</wsd:MetadataVersion>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""
            sock.sendto(resp.encode("utf-8"), addr)


class PushEventNotifier(threading.Thread):
    def __init__(self, motion: MotionDetector, subscriptions: Dict[str, PushSubscription]):
        super().__init__(daemon=True)
        self.motion = motion
        self.subscriptions = subscriptions
        self._stop_event = threading.Event()
        self._last_changed_at = 0.0

    def run(self):
        while not self._stop_event.is_set():
            state = self.motion.get_state()
            if state.changed_at > self._last_changed_at:
                self._last_changed_at = state.changed_at
                now = time.time()
                expired = [k for k, v in self.subscriptions.items() if v.expires_at < now]
                for k in expired:
                    self.subscriptions.pop(k, None)
                for sub in list(self.subscriptions.values()):
                    try:
                        self._notify(sub, state)
                    except Exception as exc:  # 防御性兜底，避免线程因单次通知异常退出
                        logging.warning("Push notify unexpected error to %s: %s", sub.consumer_url, exc)
            time.sleep(0.2)

    @staticmethod
    def _notify(sub: PushSubscription, state: MotionState):
        value = "true" if state.detected else "false"
        changed = format_utc_xml(state.changed_at)
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:wsa="{WSA_NS}" xmlns:wsnt="{WSNT_NS}" xmlns:tt="{TT_NS}">
  <s:Header>
    <wsa:Action>{WSNT_NS}/NotificationConsumer/Notify</wsa:Action>
    <wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:To>{sub.consumer_url}</wsa:To>
  </s:Header>
  <s:Body>
    <wsnt:Notify>
      <wsnt:NotificationMessage>
        <wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="{changed}" PropertyOperation="Changed">
            <tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vsc1"/></tt:Source>
            <tt:Data><tt:SimpleItem Name="IsMotion" Value="{value}"/><tt:SimpleItem Name="Confidence" Value="{state.confidence:.2f}"/></tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
    </wsnt:Notify>
  </s:Body>
</s:Envelope>"""
        req = urllib.request.Request(sub.consumer_url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/soap+xml; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                logging.debug("Push notify sent to %s status=%s", sub.consumer_url, resp.status)
        except (urllib.error.URLError, http.client.RemoteDisconnected, TimeoutError, OSError) as exc:
            logging.warning("Push notify failed to %s: %s", sub.consumer_url, exc)


def soap_envelope(body: str) -> str:
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><s:Envelope xmlns:s=\"{SOAP_NS}\">{body}</s:Envelope>"


def extract_action(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "Unknown"
    body = root.find(f".//{{{SOAP_NS}}}Body")
    if body is None or not list(body):
        return "Unknown"
    tag = list(body)[0].tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def wsse_auth_ok(xml_text: str, username: str, password: str) -> bool:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False

    token = root.find(f".//{{{WSSE_NS}}}UsernameToken")
    if token is None:
        return False

    un = token.find(f"{{{WSSE_NS}}}Username")
    pw = token.find(f"{{{WSSE_NS}}}Password")
    if un is None or pw is None or un.text != username or not pw.text:
        return False

    pw_type = (pw.attrib.get("Type") or "").lower()
    if "passworddigest" in pw_type:
        nonce_node = token.find(f"{{{WSSE_NS}}}Nonce")
        created_node = token.find(f"{{{WSU_NS}}}Created")
        if nonce_node is None or created_node is None or not nonce_node.text or not created_node.text:
            return False
        try:
            nonce_raw = base64.b64decode(nonce_node.text)
        except Exception:
            return False
        expected = base64.b64encode(hashlib.sha1(nonce_raw + created_node.text.encode("utf-8") + password.encode("utf-8")).digest()).decode("ascii")
        return expected == pw.text

    return pw.text == password


def parse_subscribe_request(xml_text: str) -> tuple[Optional[str], str, int]:
    root = ET.fromstring(xml_text)
    consumer = root.find(f".//{{{WSNT_NS}}}ConsumerReference")
    addr = None
    if consumer is not None:
        for ns in (WSA_NS, WSA_2004_NS):
            node = consumer.find(f"{{{ns}}}Address")
            if node is not None and node.text:
                addr = node.text.strip()
                break
    topic_node = root.find(f".//{{{WSNT_NS}}}TopicExpression")
    topic = topic_node.text.strip() if topic_node is not None and topic_node.text else "tns1:RuleEngine/CellMotionDetector/Motion"
    ttl_node = root.find(f".//{{{WSNT_NS}}}InitialTerminationTime")
    ttl_sec = parse_duration_seconds(ttl_node.text if ttl_node is not None else None, default_seconds=60)
    return addr, topic, ttl_sec


def create_app(bind_host: str, service_host: str, port: int, rtsp_url: str, username: str, password: str, debug_window: bool):
    app = Flask(__name__)
    endpoint_uuid = str(uuid.uuid4())
    endpoint = f"http://{service_host}:{port}"

    motion = MotionDetector(rtsp_url, debug_window=debug_window)
    motion.start()

    discovery = WSDiscoveryResponder(endpoint_uuid=endpoint_uuid, xaddr=f"{endpoint}/onvif/device_service")
    discovery.start()

    pull_subscriptions: Dict[str, PullPointSubscription] = {}
    push_subscriptions: Dict[str, PushSubscription] = {}
    notifier = PushEventNotifier(motion=motion, subscriptions=push_subscriptions)
    notifier.start()

    def check_auth(xml_text: str) -> Optional[Response]:
        auth = request.authorization
        basic_ok = auth and auth.username == username and auth.password == password
        if basic_ok or wsse_auth_ok(xml_text, username, password):
            return None
        return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="ONVIF"'})

    def event_message_xml(state: MotionState) -> str:
        value = "true" if state.detected else "false"
        changed = format_utc_xml(state.changed_at)
        return f"""
<wsnt:NotificationMessage>
<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
<wsnt:Message><tt:Message UtcTime="{changed}" PropertyOperation="Changed">
<tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vsc1"/></tt:Source>
<tt:Data><tt:SimpleItem Name="IsMotion" Value="{value}"/><tt:SimpleItem Name="Confidence" Value="{state.confidence:.2f}"/></tt:Data>
</tt:Message></wsnt:Message>
</wsnt:NotificationMessage>"""

    @app.post("/onvif/events_service")
    def events_service():
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed

        action = extract_action(xml_text)
        if action == "GetEventProperties":
            body = f"""
<s:Body><tev:GetEventPropertiesResponse xmlns:tev="{TEV_NS}" xmlns:wstop="{WSTOP_NS}" xmlns:wsnt="{WSNT_NS}">
<tev:TopicNamespaceLocation>http://www.onvif.org/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>
<tev:TopicExpressionDialect>http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet</tev:TopicExpressionDialect>
<tev:TopicExpressionDialect>http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</tev:TopicExpressionDialect>
<tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>
<tev:ProducerPropertiesFilterDialect>http://www.onvif.org/ver10/tev/producerPropertiesFilter/ItemFilter</tev:ProducerPropertiesFilterDialect>
<tev:FixedTopicSet>true</tev:FixedTopicSet>
<wstop:TopicSet><tns1:RuleEngine xmlns:tns1="http://www.onvif.org/ver10/topics"><tns1:CellMotionDetector><tns1:Motion/></tns1:CellMotionDetector></tns1:RuleEngine></wstop:TopicSet>
</tev:GetEventPropertiesResponse></s:Body>"""
        elif action == "CreatePullPointSubscription":
            token = str(uuid.uuid4())
            pull_subscriptions[token] = PullPointSubscription(token=token, expires_at=time.time() + 3600)
            body = f"""
<s:Body><tev:CreatePullPointSubscriptionResponse xmlns:tev="{TEV_NS}" xmlns:wsnt="{WSNT_NS}" xmlns:wsa="{WSA_NS}">
<wsnt:SubscriptionReference><wsa:Address>{endpoint}/onvif/pullpoint/{token}</wsa:Address></wsnt:SubscriptionReference>
<wsnt:CurrentTime>{format_utc_xml()}</wsnt:CurrentTime>
<wsnt:TerminationTime>{format_utc_xml(time.time() + 3600)}</wsnt:TerminationTime>
</tev:CreatePullPointSubscriptionResponse></s:Body>"""
        elif action == "PullMessages":
            state = motion.get_state()
            body = f"""
<s:Body><tev:PullMessagesResponse xmlns:tev="{TEV_NS}" xmlns:wsnt="{WSNT_NS}" xmlns:tt="{TT_NS}">
<wsnt:CurrentTime>{format_utc_xml()}</wsnt:CurrentTime>
<wsnt:TerminationTime>{format_utc_xml(time.time() + 3600)}</wsnt:TerminationTime>
{event_message_xml(state)}
</tev:PullMessagesResponse></s:Body>"""
        elif action == "Subscribe":
            try:
                consumer_url, topic, ttl_sec = parse_subscribe_request(xml_text)
            except ET.ParseError:
                consumer_url, topic, ttl_sec = None, "", 60
            if not consumer_url:
                fault = """
<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>
<s:Reason><s:Text xml:lang=\"en\">Subscribe requires ConsumerReference/Address</s:Text></s:Reason></s:Fault></s:Body>"""
                return Response(soap_envelope(fault), content_type="application/soap+xml; charset=utf-8", status=400)
            token = str(uuid.uuid4())
            push_subscriptions[token] = PushSubscription(token=token, consumer_url=consumer_url, topic_expression=topic, expires_at=time.time() + ttl_sec)
            body = f"""
<s:Body><wsnt:SubscribeResponse xmlns:wsnt="{WSNT_NS}" xmlns:wsa="{WSA_NS}">
<wsnt:SubscriptionReference><wsa:Address>{endpoint}/onvif/events_service/subscription/{token}</wsa:Address></wsnt:SubscriptionReference>
<wsnt:CurrentTime>{format_utc_xml()}</wsnt:CurrentTime>
<wsnt:TerminationTime>{format_utc_xml(time.time() + ttl_sec)}</wsnt:TerminationTime>
</wsnt:SubscribeResponse></s:Body>"""
        elif action == "Renew":
            body = f"""
<s:Body><wsnt:RenewResponse xmlns:wsnt="{WSNT_NS}"><wsnt:TerminationTime>{format_utc_xml(time.time() + 3600)}</wsnt:TerminationTime></wsnt:RenewResponse></s:Body>"""
        elif action == "Unsubscribe":
            body = f"""
<s:Body><wsnt:UnsubscribeResponse xmlns:wsnt="{WSNT_NS}"/></s:Body>"""
        else:
            body = "<s:Body/>"

        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/events_service/subscription/<token>")
    def wsnt_subscription_service(token: str):
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed

        sub = push_subscriptions.get(token)
        if sub is None:
            return Response(soap_envelope("<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code><s:Reason><s:Text xml:lang=\"en\">Subscription not found</s:Text></s:Reason></s:Fault></s:Body>"), content_type="application/soap+xml; charset=utf-8", status=404)

        action = extract_action(xml_text)
        if action == "Renew":
            sub.expires_at = time.time() + 3600
            body = f"<s:Body><wsnt:RenewResponse xmlns:wsnt=\"{WSNT_NS}\"><wsnt:TerminationTime>{format_utc_xml(sub.expires_at)}</wsnt:TerminationTime></wsnt:RenewResponse></s:Body>"
        elif action == "Unsubscribe":
            push_subscriptions.pop(token, None)
            body = f"<s:Body><wsnt:UnsubscribeResponse xmlns:wsnt=\"{WSNT_NS}\"/></s:Body>"
        else:
            body = "<s:Body/>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/pullpoint/<token>")
    def pullpoint_service(token: str):
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed

        sub = pull_subscriptions.get(token)
        if not sub or sub.expires_at < time.time():
            fault = """
<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>
<s:Reason><s:Text xml:lang=\"en\">Subscription not found or expired</s:Text></s:Reason></s:Fault></s:Body>"""
            return Response(soap_envelope(fault), content_type="application/soap+xml; charset=utf-8", status=404)

        action = extract_action(xml_text)
        if action == "Renew":
            sub.expires_at = time.time() + 3600
            body = f"<s:Body><wsnt:RenewResponse xmlns:wsnt=\"{WSNT_NS}\"><wsnt:TerminationTime>{format_utc_xml(sub.expires_at)}</wsnt:TerminationTime></wsnt:RenewResponse></s:Body>"
        elif action == "Unsubscribe":
            pull_subscriptions.pop(token, None)
            body = f"<s:Body><wsnt:UnsubscribeResponse xmlns:wsnt=\"{WSNT_NS}\"/></s:Body>"
        else:
            state = motion.get_state()
            body = f"""
<s:Body><tev:PullMessagesResponse xmlns:tev="{TEV_NS}" xmlns:wsnt="{WSNT_NS}" xmlns:tt="{TT_NS}">
<wsnt:CurrentTime>{format_utc_xml()}</wsnt:CurrentTime>
<wsnt:TerminationTime>{format_utc_xml(sub.expires_at)}</wsnt:TerminationTime>
{event_message_xml(state)}
</tev:PullMessagesResponse></s:Body>"""
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    def check_auth_wrapper():
        xml_text = request.data.decode("utf-8", errors="ignore")
        return xml_text, check_auth(xml_text)

    @app.post("/onvif/device_service")
    def device_service():
        xml_text, auth_failed = check_auth_wrapper()
        if auth_failed:
            return auth_failed
        action = extract_action(xml_text)
        if action == "GetDeviceInformation":
            body = f"<s:Body><tds:GetDeviceInformationResponse xmlns:tds=\"{TDS_NS}\"><tds:Manufacturer>OpenAI-Lab</tds:Manufacturer><tds:Model>PyVirtualCam-ONVIF</tds:Model><tds:FirmwareVersion>1.3.0</tds:FirmwareVersion><tds:SerialNumber>PY-ONVIF-0001</tds:SerialNumber><tds:HardwareId>Windows-Python312</tds:HardwareId></tds:GetDeviceInformationResponse></s:Body>"
        elif action == "GetCapabilities":
            body = f"<s:Body><tds:GetCapabilitiesResponse xmlns:tds=\"{TDS_NS}\" xmlns:tt=\"{TT_NS}\"><tds:Capabilities><tt:Device><tt:XAddr>{endpoint}/onvif/device_service</tt:XAddr></tt:Device><tt:Media><tt:XAddr>{endpoint}/onvif/media_service</tt:XAddr></tt:Media><tt:Events><tt:XAddr>{endpoint}/onvif/events_service</tt:XAddr><tt:WSPullPointSupport>true</tt:WSPullPointSupport><tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport></tt:Events><tt:PTZ><tt:XAddr>{endpoint}/onvif/ptz_service</tt:XAddr></tt:PTZ></tds:Capabilities></tds:GetCapabilitiesResponse></s:Body>"
        elif action == "GetServices":
            body = f"<s:Body><tds:GetServicesResponse xmlns:tds=\"{TDS_NS}\" xmlns:tt=\"{TT_NS}\"><tds:Service><tds:Namespace>{TDS_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/device_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service><tds:Service><tds:Namespace>{TRT_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/media_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>40</tt:Minor></tds:Version></tds:Service><tds:Service><tds:Namespace>{TEV_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/events_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service><tds:Service><tds:Namespace>{TPTZ_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/ptz_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service></tds:GetServicesResponse></s:Body>"
        elif action == "GetSystemDateAndTime":
            now = dt.datetime.utcnow()
            body = f"<s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds=\"{TDS_NS}\" xmlns:tt=\"{TT_NS}\"><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings><tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time><tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body>"
        elif action == "GetScopes":
            body = f"<s:Body><tds:GetScopesResponse xmlns:tds=\"{TDS_NS}\" xmlns:tt=\"{TT_NS}\"><tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/type/video_encoder</tt:ScopeItem></tds:Scopes><tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/name/PythonVirtualCam</tt:ScopeItem></tds:Scopes></tds:GetScopesResponse></s:Body>"
        else:
            body = "<s:Body/>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/media_service")
    def media_service():
        xml_text, auth_failed = check_auth_wrapper()
        if auth_failed:
            return auth_failed
        action = extract_action(xml_text)
        if action == "GetProfiles":
            body = f"<s:Body><trt:GetProfilesResponse xmlns:trt=\"{TRT_NS}\" xmlns:tt=\"{TT_NS}\"><trt:Profiles token=\"profile_1\" fixed=\"true\"><tt:Name>Profile1</tt:Name></trt:Profiles></trt:GetProfilesResponse></s:Body>"
        elif action == "GetStreamUri":
            body = f"<s:Body><trt:GetStreamUriResponse xmlns:trt=\"{TRT_NS}\" xmlns:tt=\"{TT_NS}\"><trt:MediaUri><tt:Uri>{rtsp_url}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse></s:Body>"
        elif action == "GetSnapshotUri":
            body = f"<s:Body><trt:GetSnapshotUriResponse xmlns:trt=\"{TRT_NS}\" xmlns:tt=\"{TT_NS}\"><trt:MediaUri><tt:Uri>{endpoint}/snapshot.jpg</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:GetSnapshotUriResponse></s:Body>"
        else:
            body = "<s:Body/>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/ptz_service")
    def ptz_service():
        xml_text, auth_failed = check_auth_wrapper()
        if auth_failed:
            return auth_failed
        action = extract_action(xml_text)
        if action == "GetConfigurations":
            body = f"<s:Body><tptz:GetConfigurationsResponse xmlns:tptz=\"{TPTZ_NS}\" xmlns:tt=\"{TT_NS}\"><tptz:PTZConfiguration token=\"ptz1\"><tt:Name>VirtualPTZ</tt:Name></tptz:PTZConfiguration></tptz:GetConfigurationsResponse></s:Body>"
        else:
            body = f"<s:Body><tptz:{action}Response xmlns:tptz=\"{TPTZ_NS}\"/></s:Body>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.get("/snapshot.jpg")
    def snapshot():
        auth = request.authorization
        if not auth or auth.username != username or auth.password != password:
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="ONVIF"'})
        cap = cv2.VideoCapture(rtsp_url)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return Response("Unable to capture snapshot", status=500)
        ok, jpg = cv2.imencode(".jpg", frame)
        if not ok:
            return Response("Failed to encode snapshot", status=500)
        return Response(jpg.tobytes(), content_type="image/jpeg")

    @app.get("/")
    def index():
        return {
            "device_service": f"{endpoint}/onvif/device_service",
            "media_service": f"{endpoint}/onvif/media_service",
            "events_service": f"{endpoint}/onvif/events_service",
            "ptz_service": f"{endpoint}/onvif/ptz_service",
        }

    return app


def guess_service_host(bind_host: str) -> str:
    if bind_host != "0.0.0.0":
        return bind_host
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Python ONVIF virtual camera with OpenCV motion detection")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--service-host", default="")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--rtsp", default="rtsp://admin:a1234567@10.0.0.45:554/stream1")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="a1234567")
    parser.add_argument("--debug-window", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
    service_host = args.service_host or guess_service_host(args.host)
    logging.info("Advertised ONVIF host: %s", service_host)

    app = create_app(
        bind_host=args.host,
        service_host=service_host,
        port=args.port,
        rtsp_url=args.rtsp,
        username=args.username,
        password=args.password,
        debug_window=args.debug_window,
    )
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
