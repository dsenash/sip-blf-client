import socket
import random
import threading
import time
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


class SIPDialog:
    """One SIP dialog: its own Call-ID, CSeq sequence and From tag.

    A client can hold several at once, which is what lets a BLF subscription
    share a socket with a live call.
    """

    def __init__(self, client, target_uri):
        self.client = client
        self.target_uri = target_uri
        self.call_id = str(uuid.uuid4())
        self.cseq = 1
        self.from_tag = "".join(random.choices("0123456789", k=8))
        self.to_tag = None
        self.event = None
        self.accept = None
        self.event_id = None

    def _next_cseq(self):
        value = self.cseq
        self.cseq += 1
        return value

    def _next_branch(self):
        return "z9hG4bK" + "".join(random.choices("0123456789abcdef", k=16))

    def _to_header(self, to_tag=None):
        header = f"<{self.target_uri}>"
        if to_tag:
            header += f";tag={to_tag}"
        return header

    def _build(self, method, cseq, to_tag=None, extra=None, body=None):
        client = self.client
        msg = SIPMessage(method, self.target_uri)
        msg.add_header(
            "Via",
            f"SIP/2.0/{client.transport.upper()} {client.local_ip}:{client.local_port}"
            f";branch={self._next_branch()};rport",
        )
        msg.add_header("Max-Forwards", "70")
        msg.add_header(
            "From",
            f'"{client.display_name}" <sip:{client.from_user}@{client.local_ip}'
            f':{client.local_port}>;tag={self.from_tag}',
        )
        msg.add_header("To", self._to_header(to_tag))
        msg.add_header("Call-ID", self.call_id)
        msg.add_header("CSeq", f"{cseq} {method}")
        msg.add_header(
            "Contact",
            f"<sip:{client.from_user}@{client.local_ip}:{client.local_port}"
            f";transport={client.transport}>",
        )
        msg.add_header("User-Agent", client.user_agent)
        for name, value in extra or []:
            msg.add_header(name, value)
        if body:
            msg.add_header("Content-Type", "application/sdp")
            msg.body = body
        # Content-Length is a real header, not just a build() artifact, so that
        # callers and tests can read it back off the message.
        msg.add_header("Content-Length", str(len((body or "").encode())))
        return msg

    def build_invite(self):
        return self._build(
            "INVITE",
            self._next_cseq(),
            extra=[("Accept", "application/sdp, application/dtmf-relay")],
            body=self.client.build_sdp(),
        )

    def build_ack(self, to_tag=None):
        # An ACK repeats the INVITE's CSeq, which is one behind the counter
        # now that _next_cseq has advanced.
        return self._build("ACK", self.cseq - 1, to_tag=to_tag)

    def build_bye(self, to_tag=None):
        return self._build("BYE", self._next_cseq(), to_tag=to_tag)

    def send_invite(self):
        return self.client.send(self.build_invite().build().encode())

    def send_ack(self, to_tag=None):
        return self.client.send(self.build_ack(to_tag).build().encode())

    def send_bye(self, to_tag=None):
        return self.client.send(self.build_bye(to_tag).build().encode())


class SIPClient:
    def __init__(self, config):
        self.config = config
        self.local_ip = config.get("local_ip", "0.0.0.0")
        self.local_port = config.get("local_port", 5080)
        self.transport = config.get("transport", "udp").lower()
        self.from_user = config.get("from_user", "100")
        self.display_name = config.get("display_name", "SIP Client")
        self.user_agent = config.get("user_agent", "PythonSIP/1.0")
        self.rtp_port = config.get("rtp_port", 10000)
        self.socket = None
        self.remote_addr = None
        self.last_sdp = ""
        self._send_lock = threading.Lock()

    def open(self, remote_addr):
        self.remote_addr = remote_addr
        if self.transport == "tcp":
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.connect(remote_addr)
        else:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.local_ip, self.local_port))
        return self

    def create_dialog(self, target_uri):
        return SIPDialog(self, target_uri)

    def build_sdp(self):
        now = int(time.time())
        sdp = "v=0\r\n"
        sdp += f"o={self.from_user} {now} {now} IN IP4 {self.local_ip}\r\n"
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

    def send(self, data):
        if self.socket is None:
            raise RuntimeError("SIP socket is not open, call open() first")
        with self._send_lock:
            if self.transport == "tcp":
                self.socket.send(data)
            else:
                self.socket.sendto(data, self.remote_addr)

    @staticmethod
    def _is_sdp(msg):
        ctype = msg.headers.get("Content-Type", "")
        return ctype.split(";")[0].strip().lower() == "application/sdp"

    def receive(self, timeout=5):
        self.socket.settimeout(timeout)
        try:
            if self.transport == "tcp":
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = self.socket.recv(65535)
                    if not chunk:
                        return None
                    buf += chunk
                data = buf
            else:
                data, _ = self.socket.recvfrom(65535)
        except socket.timeout:
            return None
        msg = SIPMessage.parse(data)
        # Only SDP counts as media state. A NOTIFY body must never clobber it.
        if msg and msg.body and self._is_sdp(msg):
            self.last_sdp = msg.body
        return msg

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