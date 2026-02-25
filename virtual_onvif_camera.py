import argparse
import datetime as dt
import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

import cv2
from flask import Flask, Response, request


NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
    "wsd": "http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01",
    "dn": "http://www.onvif.org/ver10/network/wsdl",
}


@dataclass
class MotionState:
    detected: bool = False
    confidence: float = 0.0
    changed_at: float = time.time()


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
    def __init__(self, device_uuid: str, host: str, onvif_port: int):
        super().__init__(daemon=True)
        self.device_uuid = device_uuid
        self.host = host
        self.onvif_port = onvif_port
        self._stop_event = threading.Event()

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

            message_id = f"urn:uuid:{uuid.uuid4()}"
            xaddr = f"http://{self.host}:{self.onvif_port}/onvif/device_service"
            resp = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<soap:Envelope xmlns:soap=\"http://www.w3.org/2003/05/soap-envelope\"
               xmlns:wsa=\"http://www.w3.org/2005/08/addressing\"
               xmlns:wsd=\"http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01\"
               xmlns:dn=\"http://www.onvif.org/ver10/network/wsdl\">
  <soap:Header>
    <wsa:MessageID>{message_id}</wsa:MessageID>
    <wsa:RelatesTo>urn:uuid:probe</wsa:RelatesTo>
    <wsa:To>http://www.w3.org/2005/08/addressing/anonymous</wsa:To>
    <wsa:Action>http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01/ProbeMatches</wsa:Action>
  </soap:Header>
  <soap:Body>
    <wsd:ProbeMatches>
      <wsd:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:{self.device_uuid}</wsa:Address></wsa:EndpointReference>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
        <wsd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/PythonVirtualCam</wsd:Scopes>
        <wsd:XAddrs>{xaddr}</wsd:XAddrs>
        <wsd:MetadataVersion>1</wsd:MetadataVersion>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""
            sock.sendto(resp.encode("utf-8"), addr)


def soap_envelope(body: str) -> str:
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\">{body}</s:Envelope>"""


def extract_action(xml_text: str) -> str:
    names = [
        "GetDeviceInformation",
        "GetCapabilities",
        "GetServices",
        "GetSystemDateAndTime",
        "GetScopes",
        "GetProfiles",
        "GetStreamUri",
        "GetSnapshotUri",
        "GetEventProperties",
        "CreatePullPointSubscription",
        "PullMessages",
        "Renew",
        "Unsubscribe",
        "GetConfigurations",
        "ContinuousMove",
        "Stop",
    ]
    for name in names:
        if f":{name}" in xml_text or f"<{name}" in xml_text:
            return name
    return "Unknown"


def create_app(host: str, port: int, rtsp_url: str, username: str, password: str):
    app = Flask(__name__)
    device_uuid = str(uuid.uuid4())
    endpoint = f"http://{host}:{port}"

    motion = MotionDetector(rtsp_url)
    motion.start()

    discovery = WSDiscoveryResponder(device_uuid=device_uuid, host=host, onvif_port=port)
    discovery.start()

    subscriptions: Dict[str, float] = {}

    def check_auth() -> Optional[Response]:
        auth = request.authorization
        if not auth or auth.username != username or auth.password != password:
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="ONVIF"'})
        return None

    @app.get("/")
    def index():
        return {
            "service": "Python ONVIF Virtual Camera",
            "device_uuid": device_uuid,
            "device_service": f"{endpoint}/onvif/device_service",
            "media_service": f"{endpoint}/onvif/media_service",
            "events_service": f"{endpoint}/onvif/events_service",
            "ptz_service": f"{endpoint}/onvif/ptz_service",
        }

    @app.post("/onvif/device_service")
    def device_service():
        auth_failed = check_auth()
        if auth_failed:
            return auth_failed

        action = extract_action(request.data.decode("utf-8", errors="ignore"))

        if action == "GetDeviceInformation":
            body = """
<s:Body><tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
<tds:Manufacturer>OpenAI-Lab</tds:Manufacturer><tds:Model>PyVirtualCam-ONVIF</tds:Model>
<tds:FirmwareVersion>1.0.0</tds:FirmwareVersion><tds:SerialNumber>PY-ONVIF-0001</tds:SerialNumber>
<tds:HardwareId>Windows-Python312</tds:HardwareId></tds:GetDeviceInformationResponse></s:Body>"""
        elif action == "GetCapabilities":
            body = f"""
<s:Body><tds:GetCapabilitiesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<tds:Capabilities>
<tt:Device><tt:XAddr>{endpoint}/onvif/device_service</tt:XAddr></tt:Device>
<tt:Media><tt:XAddr>{endpoint}/onvif/media_service</tt:XAddr><tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>
<tt:Events><tt:XAddr>{endpoint}/onvif/events_service</tt:XAddr><tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport><tt:WSPullPointSupport>true</tt:WSPullPointSupport></tt:Events>
<tt:PTZ><tt:XAddr>{endpoint}/onvif/ptz_service</tt:XAddr></tt:PTZ>
</tds:Capabilities></tds:GetCapabilitiesResponse></s:Body>"""
        elif action == "GetServices":
            body = f"""
<s:Body><tds:GetServicesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace><tds:XAddr>{endpoint}/onvif/device_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace><tds:XAddr>{endpoint}/onvif/media_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>40</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>http://www.onvif.org/ver10/events/wsdl</tds:Namespace><tds:XAddr>{endpoint}/onvif/events_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace><tds:XAddr>{endpoint}/onvif/ptz_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>42</tt:Minor></tds:Version></tds:Service>
</tds:GetServicesResponse></s:Body>"""
        elif action == "GetSystemDateAndTime":
            now = dt.datetime.utcnow()
            body = f"""
<s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>
<tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>
<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime>
</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body>"""
        elif action == "GetScopes":
            body = """
<s:Body><tds:GetScopesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/type/video_encoder</tt:ScopeItem></tds:Scopes>
<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/name/PythonVirtualCam</tt:ScopeItem></tds:Scopes>
</tds:GetScopesResponse></s:Body>"""
        else:
            body = "<s:Body/>"

        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/media_service")
    def media_service():
        auth_failed = check_auth()
        if auth_failed:
            return auth_failed

        action = extract_action(request.data.decode("utf-8", errors="ignore"))

        if action == "GetProfiles":
            body = """
<s:Body><trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<trt:Profiles token="profile_1" fixed="true"><tt:Name>Profile1</tt:Name>
<tt:VideoSourceConfiguration token="vsc1"><tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>source_1</tt:SourceToken><tt:Bounds x="0" y="0" width="1920" height="1080"/></tt:VideoSourceConfiguration>
</trt:Profiles></trt:GetProfilesResponse></s:Body>"""
        elif action == "GetStreamUri":
            body = f"""
<s:Body><trt:GetStreamUriResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<trt:MediaUri><tt:Uri>{rtsp_url}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout></trt:MediaUri>
</trt:GetStreamUriResponse></s:Body>"""
        elif action == "GetSnapshotUri":
            body = f"""
<s:Body><trt:GetSnapshotUriResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<trt:MediaUri><tt:Uri>{endpoint}/snapshot.jpg</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout></trt:MediaUri>
</trt:GetSnapshotUriResponse></s:Body>"""
        else:
            body = "<s:Body/>"

        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/events_service")
    def events_service():
        auth_failed = check_auth()
        if auth_failed:
            return auth_failed

        action = extract_action(request.data.decode("utf-8", errors="ignore"))

        if action == "GetEventProperties":
            body = """
<s:Body><tev:GetEventPropertiesResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">
<tev:TopicNamespaceLocation>http://www.onvif.org/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>
<wstop:TopicSet><tns1:RuleEngine xmlns:tns1="http://www.onvif.org/ver10/topics"><tns1:CellMotionDetector/></tns1:RuleEngine></wstop:TopicSet>
</tev:GetEventPropertiesResponse></s:Body>"""
        elif action == "CreatePullPointSubscription":
            token = str(uuid.uuid4())
            subscriptions[token] = time.time() + 3600
            body = f"""
<s:Body><tev:CreatePullPointSubscriptionResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:wsa="http://www.w3.org/2005/08/addressing">
<wsnt:SubscriptionReference><wsa:Address>{endpoint}/onvif/events_service?token={token}</wsa:Address></wsnt:SubscriptionReference>
<wsnt:CurrentTime>{dt.datetime.utcnow().isoformat()}Z</wsnt:CurrentTime>
<wsnt:TerminationTime>{(dt.datetime.utcnow() + dt.timedelta(hours=1)).isoformat()}Z</wsnt:TerminationTime>
</tev:CreatePullPointSubscriptionResponse></s:Body>"""
        elif action == "PullMessages":
            state = motion.state
            changed = dt.datetime.utcfromtimestamp(state.changed_at).isoformat() + "Z"
            value = "true" if state.detected else "false"
            confidence = f"{state.confidence:.2f}"
            body = f"""
<s:Body><tev:PullMessagesResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema">
<wsnt:NotificationMessage>
<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
<wsnt:Message><tt:Message UtcTime="{changed}" PropertyOperation="Changed"><tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vsc1"/></tt:Source><tt:Data><tt:SimpleItem Name="IsMotion" Value="{value}"/><tt:SimpleItem Name="Confidence" Value="{confidence}"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage></tev:PullMessagesResponse></s:Body>"""
        elif action in {"Renew", "Unsubscribe"}:
            body = f"""
<s:Body><wsnt:{action}Response xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"/></s:Body>"""
        else:
            body = "<s:Body/>"

        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.post("/onvif/ptz_service")
    def ptz_service():
        auth_failed = check_auth()
        if auth_failed:
            return auth_failed

        action = extract_action(request.data.decode("utf-8", errors="ignore"))
        if action == "GetConfigurations":
            body = """
<s:Body><tptz:GetConfigurationsResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<tptz:PTZConfiguration token="ptz1"><tt:Name>VirtualPTZ</tt:Name><tt:UseCount>1</tt:UseCount><tt:NodeToken>ptznode1</tt:NodeToken><tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout></tptz:PTZConfiguration>
</tptz:GetConfigurationsResponse></s:Body>"""
        else:
            body = f"""
<s:Body><tptz:{action}Response xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"/></s:Body>"""

        return Response(soap_envelope(body), content_type="application/soap+xml; charset=utf-8")

    @app.get("/snapshot.jpg")
    def snapshot():
        auth_failed = check_auth()
        if auth_failed:
            return auth_failed

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


def main():
    parser = argparse.ArgumentParser(description="Python ONVIF virtual camera with OpenCV motion detection")
    parser.add_argument("--host", default="0.0.0.0", help="bind host")
    parser.add_argument("--port", type=int, default=8000, help="ONVIF HTTP port")
    parser.add_argument("--rtsp", default="rtsp://admin:a1234567@10.0.0.45:554/stream1", help="RTSP stream URL")
    parser.add_argument("--username", default="admin", help="ONVIF username")
    parser.add_argument("--password", default="a1234567", help="ONVIF password")
    parser.add_argument("--log-level", default="INFO", help="log level")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
    app = create_app(host=args.host, port=args.port, rtsp_url=args.rtsp, username=args.username, password=args.password)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
