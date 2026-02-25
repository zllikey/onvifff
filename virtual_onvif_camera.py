import argparse
import datetime as dt
import logging
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import cv2
from flask import Flask, Response, request

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
WSA_NS = "http://www.w3.org/2005/08/addressing"
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


@dataclass
class MotionState:
    detected: bool = False
    confidence: float = 0.0
    changed_at: float = field(default_factory=time.time)


class MotionDetector(threading.Thread):
    def __init__(self, rtsp_url: str):
        super().__init__(daemon=True)
        self.rtsp_url = rtsp_url
        self.state = MotionState()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        logging.info("Starting motion detector from RTSP: %s", self.rtsp_url)
        cap = cv2.VideoCapture(self.rtsp_url)
        subtractor = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)

        if not cap.isOpened():
            logging.error("Unable to open RTSP stream for motion detection.")
            return

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

            if detected != self.state.detected:
                self.state.changed_at = time.time()
            self.state.detected = detected
            self.state.confidence = confidence
            time.sleep(0.05)

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
        node = root.find(f".//{{{WSA_NS}}}MessageID")
        return node.text.strip() if node is not None and node.text else ""

    def stop(self):
        self._stop_event.set()

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
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:{self.endpoint_uuid}</wsa:Address>
        </wsa:EndpointReference>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
        <wsd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/PythonVirtualCam</wsd:Scopes>
        <wsd:XAddrs>{self.xaddr}</wsd:XAddrs>
        <wsd:MetadataVersion>1</wsd:MetadataVersion>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""
            sock.sendto(resp.encode("utf-8"), addr)


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
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def wsse_auth_ok(xml_text: str, username: str, password: str) -> bool:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False
    un = root.find(f".//{{{WSSE_NS}}}Username")
    pw = root.find(f".//{{{WSSE_NS}}}Password")
    return bool(un is not None and pw is not None and un.text == username and pw.text == password)


def create_app(bind_host: str, service_host: str, port: int, rtsp_url: str, username: str, password: str):
    app = Flask(__name__)
    endpoint_uuid = str(uuid.uuid4())
    endpoint = f"http://{service_host}:{port}"

    motion = MotionDetector(rtsp_url)
    motion.start()

    discovery = WSDiscoveryResponder(endpoint_uuid=endpoint_uuid, xaddr=f"{endpoint}/onvif/device_service")
    discovery.start()

    def check_auth(xml_text: str) -> Optional[Response]:
        auth = request.authorization
        basic_ok = auth and auth.username == username and auth.password == password
        if basic_ok or wsse_auth_ok(xml_text, username, password):
            return None
        return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="ONVIF"'})

    @app.get("/")
    def index():
        return {
            "service": "Python ONVIF Virtual Camera",
            "device_uuid": endpoint_uuid,
            "device_service": f"{endpoint}/onvif/device_service",
            "media_service": f"{endpoint}/onvif/media_service",
            "events_service": f"{endpoint}/onvif/events_service",
            "ptz_service": f"{endpoint}/onvif/ptz_service",
        }

    @app.post("/onvif/device_service")
    def device_service():
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed

        action = extract_action(xml_text)
        if action == "GetDeviceInformation":
            body = f"""
<s:Body><tds:GetDeviceInformationResponse xmlns:tds="{TDS_NS}">
<tds:Manufacturer>OpenAI-Lab</tds:Manufacturer><tds:Model>PyVirtualCam-ONVIF</tds:Model>
<tds:FirmwareVersion>1.1.0</tds:FirmwareVersion><tds:SerialNumber>PY-ONVIF-0001</tds:SerialNumber>
<tds:HardwareId>Windows-Python312</tds:HardwareId></tds:GetDeviceInformationResponse></s:Body>"""
        elif action == "GetCapabilities":
            body = f"""
<s:Body><tds:GetCapabilitiesResponse xmlns:tds="{TDS_NS}" xmlns:tt="{TT_NS}"><tds:Capabilities>
<tt:Device><tt:XAddr>{endpoint}/onvif/device_service</tt:XAddr></tt:Device>
<tt:Media><tt:XAddr>{endpoint}/onvif/media_service</tt:XAddr><tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>
<tt:Events><tt:XAddr>{endpoint}/onvif/events_service</tt:XAddr><tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport><tt:WSPullPointSupport>true</tt:WSPullPointSupport></tt:Events>
<tt:PTZ><tt:XAddr>{endpoint}/onvif/ptz_service</tt:XAddr></tt:PTZ>
</tds:Capabilities></tds:GetCapabilitiesResponse></s:Body>"""
        elif action == "GetServices":
            body = f"""
<s:Body><tds:GetServicesResponse xmlns:tds="{TDS_NS}" xmlns:tt="{TT_NS}">
<tds:Service><tds:Namespace>{TDS_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/device_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>{TRT_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/media_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>40</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>{TEV_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/events_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>{TPTZ_NS}</tds:Namespace><tds:XAddr>{endpoint}/onvif/ptz_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service>
</tds:GetServicesResponse></s:Body>"""
        elif action == "GetSystemDateAndTime":
            now = dt.datetime.utcnow()
            body = f"""
<s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds="{TDS_NS}" xmlns:tt="{TT_NS}"><tds:SystemDateAndTime>
<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>
<tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>
<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime>
</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body>"""
        elif action == "GetScopes":
            body = f"""
<s:Body><tds:GetScopesResponse xmlns:tds="{TDS_NS}" xmlns:tt="{TT_NS}">
<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/type/video_encoder</tt:ScopeItem></tds:Scopes>
<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/name/PythonVirtualCam</tt:ScopeItem></tds:Scopes>
</tds:GetScopesResponse></s:Body>"""
        else:
            body = "<s:Body/>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/media_service")
    def media_service():
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed
        action = extract_action(xml_text)

        if action == "GetProfiles":
            body = f"""
<s:Body><trt:GetProfilesResponse xmlns:trt="{TRT_NS}" xmlns:tt="{TT_NS}">
<trt:Profiles token="profile_1" fixed="true"><tt:Name>Profile1</tt:Name>
<tt:VideoSourceConfiguration token="vsc1"><tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>source_1</tt:SourceToken><tt:Bounds x="0" y="0" width="1920" height="1080"/></tt:VideoSourceConfiguration>
</trt:Profiles></trt:GetProfilesResponse></s:Body>"""
        elif action == "GetStreamUri":
            body = f"""
<s:Body><trt:GetStreamUriResponse xmlns:trt="{TRT_NS}" xmlns:tt="{TT_NS}"><trt:MediaUri>
<tt:Uri>{rtsp_url}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout>
</trt:MediaUri></trt:GetStreamUriResponse></s:Body>"""
        elif action == "GetSnapshotUri":
            body = f"""
<s:Body><trt:GetSnapshotUriResponse xmlns:trt="{TRT_NS}" xmlns:tt="{TT_NS}"><trt:MediaUri>
<tt:Uri>{endpoint}/snapshot.jpg</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout>
</trt:MediaUri></trt:GetSnapshotUriResponse></s:Body>"""
        else:
            body = "<s:Body/>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/events_service")
    def events_service():
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed

        action = extract_action(xml_text)
        if action == "GetEventProperties":
            body = f"""
<s:Body><tev:GetEventPropertiesResponse xmlns:tev="{TEV_NS}" xmlns:wstop="{WSTOP_NS}">
<tev:TopicNamespaceLocation>http://www.onvif.org/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>
<wstop:TopicSet><tns1:RuleEngine xmlns:tns1="http://www.onvif.org/ver10/topics"><tns1:CellMotionDetector/></tns1:RuleEngine></wstop:TopicSet>
</tev:GetEventPropertiesResponse></s:Body>"""
        elif action == "CreatePullPointSubscription":
            token = str(uuid.uuid4())
            body = f"""
<s:Body><tev:CreatePullPointSubscriptionResponse xmlns:tev="{TEV_NS}" xmlns:wsnt="{WSNT_NS}" xmlns:wsa="{WSA_NS}">
<wsnt:SubscriptionReference><wsa:Address>{endpoint}/onvif/events_service?token={token}</wsa:Address></wsnt:SubscriptionReference>
<wsnt:CurrentTime>{dt.datetime.utcnow().isoformat()}Z</wsnt:CurrentTime>
<wsnt:TerminationTime>{(dt.datetime.utcnow() + dt.timedelta(hours=1)).isoformat()}Z</wsnt:TerminationTime>
</tev:CreatePullPointSubscriptionResponse></s:Body>"""
        elif action == "PullMessages":
            state = motion.state
            changed = dt.datetime.utcfromtimestamp(state.changed_at).isoformat() + "Z"
            value = "true" if state.detected else "false"
            body = f"""
<s:Body><tev:PullMessagesResponse xmlns:tev="{TEV_NS}" xmlns:wsnt="{WSNT_NS}" xmlns:tt="{TT_NS}">
<wsnt:NotificationMessage>
<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
<wsnt:Message><tt:Message UtcTime="{changed}" PropertyOperation="Changed">
<tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vsc1"/></tt:Source>
<tt:Data><tt:SimpleItem Name="IsMotion" Value="{value}"/><tt:SimpleItem Name="Confidence" Value="{state.confidence:.2f}"/></tt:Data>
</tt:Message></wsnt:Message>
</wsnt:NotificationMessage></tev:PullMessagesResponse></s:Body>"""
        elif action in {"Renew", "Unsubscribe"}:
            body = f"<s:Body><wsnt:{action}Response xmlns:wsnt=\"{WSNT_NS}\"/></s:Body>"
        else:
            body = "<s:Body/>"
        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/ptz_service")
    def ptz_service():
        xml_text = request.data.decode("utf-8", errors="ignore")
        auth_failed = check_auth(xml_text)
        if auth_failed:
            return auth_failed

        action = extract_action(xml_text)
        if action == "GetConfigurations":
            body = f"""
<s:Body><tptz:GetConfigurationsResponse xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}">
<tptz:PTZConfiguration token="ptz1"><tt:Name>VirtualPTZ</tt:Name><tt:UseCount>1</tt:UseCount><tt:NodeToken>ptznode1</tt:NodeToken><tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout></tptz:PTZConfiguration>
</tptz:GetConfigurationsResponse></s:Body>"""
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
    parser.add_argument("--host", default="0.0.0.0", help="Flask bind host")
    parser.add_argument("--service-host", default="", help="Host/IP advertised in ONVIF XAddr, default auto")
    parser.add_argument("--port", type=int, default=8000, help="ONVIF HTTP port")
    parser.add_argument("--rtsp", default="rtsp://admin:a1234567@10.0.0.45:554/stream1", help="RTSP stream URL")
    parser.add_argument("--username", default="admin", help="ONVIF username")
    parser.add_argument("--password", default="a1234567", help="ONVIF password")
    parser.add_argument("--log-level", default="INFO", help="log level")
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
    )
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
