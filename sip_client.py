import socket
import struct
import random
import time
import hashlib
import uuid


class SIPMessage:
    def __init__(self, method=None, uri=None, status_code=None, reason=None):
        self.method = method
        self.uri = uri
        self.status_code = status_code
        self.reason = reason
        self.headers = {}
        self.body = ""

    def add_header(self, name, value):
        self.headers[name] = value

    def build(self):
        if self.method:
            lines = [f"{self.method} {self.uri} SIP/2.0"]
        else:
            lines = [f"SIP/2.0 {self.status_code} {self.reason}"]

        for name, value in self.headers.items():
            lines.append(f"{name}: {value}")

        if self.body:
            lines.append(f"Content-Length: {len(self.body)}")
        else:
            lines.append("Content-Length: 0")

        lines.append("")
        lines.append(self.body if self.body else "")
        return "\r\n".join(lines)

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")

        lines = data.split("\r\n")
        if not lines:
            return None

        msg = cls()
        first_line = lines[0]

        if first_line.startswith("SIP/2.0"):
            parts = first_line.split(" ", 2)
            if len(parts) >= 3:
                msg.status_code = int(parts[1])
                msg.reason = parts[2]
        else:
            parts = first_line.split(" ", 2)
            if len(parts) >= 2:
                msg.method = parts[0]
                msg.uri = parts[1]

        i = 1
        while i < len(lines):
            line = lines[i]
            if not line:
                i += 1
                break
            if ": " in line:
                name, value = line.split(": ", 1)
                msg.headers[name] = value
            i += 1

        msg.body = "\r\n".join(lines[i:]) if i < len(lines) else ""
        return msg


class SIPClient:
    def __init__(self, config):
        self.config = config
        self.local_ip = config.get("local_ip", "0.0.0.0")
        self.local_port = config.get("local_port", 5080)
        self.transport = config.get("transport", "udp").lower()
        self.from_user = config.get("from_user", "100")
        self.display_name = config.get("display_name", "SIP Client")
        self.user_agent = config.get("user_agent", "PythonSIP/1.0")
        self.call_id = str(uuid.uuid4())
        self.cseq = 1
        self.branch = "z9hG4bK" + "".join(random.choices("0123456789abcdef", k=16))
        self.tag = "".join(random.choices("0123456789", k=8))
        self.rtp_port = config.get("rtp_port", 10000)
        self.socket = None
        self.remote_addr = None
        self.remote_rtp_port = None
        self.remote_rtp_addr = None
        self.connected = False
        self.running = True
        self.last_sdp = ""

    def _create_socket(self):
        if self.transport == "tcp":
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.connect(self.remote_addr)
        else:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.local_ip, self.local_port))

    def _next_cseq(self):
        val = self.cseq
        self.cseq += 1
        return val

    def _next_branch(self):
        self.branch = "z9hG4bK" + "".join(random.choices("0123456789abcdef", k=16))
        return self.branch

    def _build_sdp(self):
        sdp = "v=0\r\n"
        sdp += f"o={self.from_user} {int(time.time())} {int(time.time())} IN IP4 {self.local_ip}\r\n"
        sdp += "s=SIP Client\r\n"
        sdp += f"c=IN IP4 {self.local_ip}\r\n"
        sdp += "t=0 0\r\n"
        sdp += f"m=audio {self.rtp_port} RTP/AVP 0 8 101\r\n"
        sdp += "a=rtpmap:0 PCMU/8000\r\n"
        sdp += "a=rtpmap:8 PCMA/8000\r\n"
        sdp += "a=rtpmap:101 telephone-event/8000\r\n"
        sdp += "a=fmtp:101 0-16\r\n"
        sdp += "a=ptime:20\r\n"
        sdp += "a=sendonly\r\n"
        return sdp

    def send_invite(self, target_uri):
        self.remote_addr = self._parse_uri(target_uri)
        self._create_socket()

        msg = SIPMessage("INVITE", target_uri)
        msg.add_header("Via", f"SIP/2.0/{self.transport.upper()} {self.local_ip}:{self.local_port};branch={self._next_branch()};rport")
        msg.add_header("Max-Forwards", "70")
        msg.add_header("From", f'"{self.display_name}" <sip:{self.from_user}@{self.local_ip}:{self.local_port}>;tag={self.tag}')
        msg.add_header("To", f"<{target_uri}>")
        msg.add_header("Call-ID", self.call_id)
        msg.add_header("CSeq", f"{self._next_cseq()} INVITE")
        msg.add_header("Contact", f"<sip:{self.from_user}@{self.local_ip}:{self.local_port};transport={self.transport}>")
        msg.add_header("User-Agent", self.user_agent)
        msg.add_header("Content-Type", "application/sdp")
        msg.add_header("Accept", "application/sdp, application/dtmf-relay")
        msg.body = self._build_sdp()

        self._send(msg.build().encode())
        return msg

    def send_ack(self, uri, to_tag=None):
        msg = SIPMessage("ACK", uri)
        msg.add_header("Via", f"SIP/2.0/{self.transport.upper()} {self.local_ip}:{self.local_port};branch={self._next_branch()};rport")
        msg.add_header("Max-Forwards", "70")
        msg.add_header("From", f'"{self.display_name}" <sip:{self.from_user}@{self.local_ip}:{self.local_port}>;tag={self.tag}')
        to_hdr = f"<{uri}>"
        if to_tag:
            to_hdr += f";tag={to_tag}"
        msg.add_header("To", to_hdr)
        msg.add_header("Call-ID", self.call_id)
        msg.add_header("CSeq", f"{self.cseq - 1} ACK")

        self._send(msg.build().encode())
        return msg

    def send_bye(self, uri, to_tag=None):
        msg = SIPMessage("BYE", uri)
        msg.add_header("Via", f"SIP/2.0/{self.transport.upper()} {self.local_ip}:{self.local_port};branch={self._next_branch()};rport")
        msg.add_header("Max-Forwards", "70")
        msg.add_header("From", f'"{self.display_name}" <sip:{self.from_user}@{self.local_ip}:{self.local_port}>;tag={self.tag}')
        to_hdr = f"<{uri}>"
        if to_tag:
            to_hdr += f";tag={to_tag}"
        msg.add_header("To", to_hdr)
        msg.add_header("Call-ID", self.call_id)
        msg.add_header("CSeq", f"{self._next_cseq()} BYE")

        self._send(msg.build().encode())
        return msg

    def _send(self, data):
        if self.transport == "tcp":
            self.socket.send(data)
        else:
            self.socket.sendto(data, self.remote_addr)

    def receive(self, timeout=5, tcp_stream_data=b""):
        self.socket.settimeout(timeout)
        try:
            if self.transport == "tcp":
                buf = tcp_stream_data or b""
                while b"\r\n\r\n" not in buf:
                    chunk = self.socket.recv(65535)
                    if not chunk:
                        return None
                    buf += chunk
                if buf:
                    msg = SIPMessage.parse(buf)
                    if msg and msg.body:
                        self.last_sdp = msg.body
                    return msg
                return None
            else:
                data, addr = self.socket.recvfrom(65535)
        except socket.timeout:
            return None
        msg = SIPMessage.parse(data)
        if msg and msg.body:
            self.last_sdp = msg.body
        return msg

    def _parse_uri(self, uri):
        uri = uri.replace("sip:", "")
        transport = "udp"
        if ";transport=" in uri:
            uri, tparam = uri.rsplit(";", 1)
            if tparam.startswith("transport="):
                transport = tparam.split("=", 1)[1].lower()
            self.transport = transport

        if "@" in uri:
            host_port = uri.split("@", 1)[1]
        else:
            host_port = uri

        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            port = int(port)
        else:
            host = host_port
            port = 5060

        return (host, port)

    def parse_sdp(self, sdp_body):
        result = {"media_port": None, "media_ip": None, "codecs": []}
        for line in sdp_body.replace("\r\n", "\n").split("\n"):
            if line.startswith("c=IN IP4 "):
                result["media_ip"] = line.split(" ")[2]
            elif line.startswith("c=IN IP6 "):
                result["media_ip"] = line.split(" ")[2]
            elif line.startswith("m=audio "):
                parts = line.split(" ")
                result["media_port"] = int(parts[1])
                result["codecs"] = [int(x) for x in parts[3:]]
        return result

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = None
