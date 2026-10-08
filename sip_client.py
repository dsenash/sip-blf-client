import hashlib
import re
import socket
import random
import threading
import time
import uuid


# How many distinct digest challenges one dialog will answer before giving
# up. Enough for a nonce rotation, not enough for a resend loop.
MAX_AUTH_ATTEMPTS = 3


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


class SIPHeaders(dict):
    """A dict of SIP headers.

    Header names are case-insensitive per RFC 3261, so a lookup must not
    depend on how the peer cased it: FreeSWITCH sends "Content-Type" while
    other stacks send "content-type", and a case-sensitive miss silently
    dropped NOTIFY bodies. The original casing is kept as the real key so
    that build() still writes headers the way the caller spelled them.

    A header may legally appear more than once and a dict cannot hold that, so
    append() keeps every copy in arrival order while get() answers with the
    last one, which is what the single-valued lookups here expect. Assigning
    with [] replaces the header outright, so builder code that sets a header
    twice does not emit it twice.
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self._spelling = {}
        self._values = []
        self.update(dict(*args, **kwargs))

    def _actual(self, key):
        return self._spelling.get(key.lower(), key)

    def __setitem__(self, key, value):
        if not isinstance(key, str):
            super().__setitem__(key, value)
            return
        actual = self._set_spelling(key)
        copies = [i for i, (name, _) in enumerate(self._values) if name == actual]
        if copies:
            # Keep the position of the header it replaces.
            self._values[copies[-1]] = (actual, value)
            self._values = [(n, v) for i, (n, v) in enumerate(self._values)
                            if i in copies[:-1] or i == copies[-1]]
        else:
            self._values.append((actual, value))
        super().__setitem__(actual, value)

    def append(self, key, value):
        """Add another copy of a header that is allowed to repeat."""
        actual = self._set_spelling(key)
        self._values.append((actual, value))
        super().__setitem__(actual, value)

    def _set_spelling(self, key):
        self._spelling.setdefault(key.lower(), key)
        return self._spelling[key.lower()]

    def __getitem__(self, key):
        return super().__getitem__(self._actual(key))

    def __delitem__(self, key):
        actual = self._actual(key)
        super().__delitem__(actual)
        self._values = [(n, v) for n, v in self._values if n != actual]
        if isinstance(key, str):
            self._spelling.pop(key.lower(), None)

    def __contains__(self, key):
        return super().__contains__(self._actual(key))

    def get(self, key, default=None):
        return super().get(self._actual(key), default)

    def pop(self, key, *args):
        actual = self._actual(key)
        self._values = [(n, v) for n, v in self._values if n != actual]
        return super().pop(actual, *args)

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, other=(), **kwargs):
        for name, value in dict(other, **kwargs).items():
            self[name] = value

    def all_values(self, key):
        """Every copy of one header, in the order it arrived."""
        actual = self._actual(key)
        return [value for name, value in self._values if name == actual]

    def items_in_order(self):
        """Every header copy in wire order, repeats included."""
        return list(self._values)


def parse_digest_challenge(header):
    """Parse a Digest challenge into its parameters.

    Returns {} for a missing or non-Digest header, so callers can treat an
    unusable challenge the same way as no challenge at all.
    """
    if not header:
        return {}
    value = header.strip()
    if not value.lower().startswith("digest"):
        return {}
    value = value[6:].strip()
    params = {}
    for part in re.findall(r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*'
                           r'(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))', value):
        key = part[0].lower()
        raw = part[1] if part[1] else part[2]
        params[key] = raw.replace('\\"', '"')
    return params


def digest_response(method, uri, username, password, challenge, nc=1,
                    cnonce=None):
    """Compute the Digest response for one request.

    Without qop the response is md5(HA1:nonce:HA2); with qop=auth the
    nonce-count, cnonce and qop are folded in (RFC 7616).
    """
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    qop = challenge.get("qop", "")
    if qop:
        if cnonce is None:
            cnonce = uuid.uuid4().hex[:16]
        return md5(f"{ha1}:{nonce}:{nc:08x}:{cnonce}:{qop}:{ha2}")
    return md5(f"{ha1}:{nonce}:{ha2}")


def build_digest_header(method, uri, username, password, challenge, nc=1,
                        cnonce=None):
    """Build the value of an Authorization/Proxy-Authorization header."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    if challenge.get("qop") and cnonce is None:
        cnonce = uuid.uuid4().hex[:16]
    response = digest_response(method, uri, username, password, challenge,
                               nc=nc, cnonce=cnonce)
    fields = [f'username="{username}"', f'realm="{realm}"',
              f'nonce="{nonce}"', f'uri="{uri}"', f'response="{response}"']
    algorithm = challenge.get("algorithm")
    if algorithm:
        fields.append(f"algorithm={algorithm}")
    if challenge.get("qop"):
        fields.append(f"qop={challenge['qop']}")
        fields.append(f"nc={nc:08x}")
        fields.append(f'cnonce="{cnonce}"')
    opaque = challenge.get("opaque")
    if opaque:
        fields.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(fields)


class SIPMessage:
    def __init__(self, method=None, uri=None, status_code=None, reason=None):
        self.method = method
        self.uri = uri
        self.status_code = status_code
        self.reason = reason
        self.headers = SIPHeaders()
        self.body = ""

    def add_header(self, name, value):
        """Add a header. Repeating the name adds another copy, per RFC 3261."""
        self.headers.append(name, value)

    def build(self):
        if self.method:
            lines = [f"{self.method} {self.uri} SIP/2.0"]
        else:
            lines = [f"SIP/2.0 {self.status_code} {self.reason}"]

        # Content-Length is emitted exactly once. An explicit header set by the
        # builder wins; otherwise it is computed from the body.
        content_length = self.headers.get("Content-Length")

        # Repeated headers are written as one line per copy: RFC 3261 does not
        # allow them to be joined with commas, and a response must carry every
        # Via of the request it answers.
        for name, value in self.headers.items_in_order():
            if name == "Content-Length":
                continue
            lines.append(f"{name}: {value}")

        if content_length is None:
            content_length = len(self.body.encode()) if self.body else 0
        lines.append(f"Content-Length: {content_length}")

        lines.append("")
        lines.append(self.body if self.body else "")
        return "\r\n".join(lines)

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")

        lines = data.split("\r\n")
        if not lines or not lines[0].strip():
            return None

        msg = cls()
        first_line = lines[0]

        if first_line.startswith("SIP/2.0"):
            parts = first_line.split(" ", 2)
            if len(parts) < 3:
                return None
            try:
                msg.status_code = int(parts[1])
            except ValueError:
                # A junk datagram must not take down a client that is holding
                # live subscriptions and a console open.
                return None
            msg.reason = parts[2]
        else:
            parts = first_line.split(" ", 2)
            if len(parts) < 2:
                return None
            msg.method = parts[0]
            msg.uri = parts[1]

        i = 1
        while i < len(lines):
            line = lines[i]
            if not line:
                i += 1
                break
            # HCOLON does not require the space, and real servers omit it.
            if ":" in line:
                name, value = line.split(":", 1)
                # append, not assignment: Via is routinely sent once per hop.
                msg.headers.append(name.strip(), value.strip())
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
        self.invite_branch = None
        self.event = None
        self.accept = None
        self.event_id = None
        # Digest credentials for this dialog, learned from a 401/407. Once set
        # every request built here carries the Authorization header, and the
        # nonce is remembered so the same challenge cannot loop forever.
        self.auth = None
        self.auth_nc = 0
        self.auth_cnonce = None
        self.auth_attempts = 0

    def set_auth(self, header, challenge, username, password, cnonce=None):
        """Record a challenge so future requests answer it.

        Returns False when this exact nonce has already been answered, or
        when the dialog has answered as many challenges as it should: a
        server that mints a fresh nonce on every attempt must not be able to
        keep the client resending forever.
        """
        nonce = challenge.get("nonce", "")
        if self.auth and self.auth["challenge"].get("nonce") == nonce:
            return False
        if self.auth_attempts >= MAX_AUTH_ATTEMPTS:
            return False
        self.auth = {
            "header": header,
            "challenge": challenge,
            "username": username,
            "password": password,
        }
        self.auth_nc = 1
        self.auth_cnonce = cnonce
        self.auth_attempts += 1
        return True

    def auth_header(self, method, uri):
        """Build the Authorization header value for one request, or None."""
        if not self.auth:
            return None
        value = build_digest_header(
            method, uri, self.auth["username"], self.auth["password"],
            self.auth["challenge"], nc=self.auth_nc, cnonce=self.auth_cnonce)
        self.auth_nc += 1
        return value

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

    def _build(self, method, cseq, to_tag=None, extra=None, body=None,
               branch=None):
        client = self.client
        msg = SIPMessage(method, self.target_uri)
        branch = branch or self._next_branch()
        msg.add_header(
            "Via",
            f"SIP/2.0/{client.transport.upper()} {client.local_ip}:{client.local_port}"
            f";branch={branch};rport",
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
        # Digest credentials apply to the requests that were challenged: the
        # subscription and its refreshes. ACK and CANCEL are never challenged
        # on their own, and the INVITE dialog has no auth of its own yet.
        if self.auth and method == "SUBSCRIBE":
            msg.add_header(self.auth["header"],
                           self.auth_header(method, self.target_uri))
        if body:
            msg.add_header("Content-Type", "application/sdp")
            msg.body = body
        # Content-Length is a real header, not just a build() artifact, so that
        # callers and tests can read it back off the message.
        msg.add_header("Content-Length", str(len((body or "").encode())))
        return msg

    def build_invite(self):
        msg = self._build(
            "INVITE",
            self._next_cseq(),
            extra=[("Accept", "application/sdp, application/dtmf-relay")],
            body=self.client.build_sdp(),
        )
        self.invite_branch = msg.headers["Via"].split("branch=")[1].split(";")[0]
        return msg

    def build_cancel(self):
        # A CANCEL is matched to the request it cancels by the Via branch, so
        # it has to repeat the INVITE's branch rather than mint a new one.
        # RFC 3261 9.1: a CANCEL with a fresh branch is a request in its own
        # right and the transaction is left dangling.
        return self._build(
            "CANCEL",
            self._next_cseq(),
            branch=self.invite_branch,
        )

    def send_cancel(self):
        return self.client.send(self.build_cancel().build().encode())

    def build_ack(self, to_tag=None):
        # An ACK repeats the INVITE's CSeq, which is one behind the counter
        # now that _next_cseq has advanced.
        return self._build("ACK", self.cseq - 1, to_tag=to_tag)

    def build_bye(self, to_tag=None):
        return self._build("BYE", self._next_cseq(), to_tag=to_tag)

    def _subscription_extra(self, event, accept, expires, event_id):
        return [
            ("Event", event),
            ("Accept", accept),
            ("Expires", str(expires)),
            ("Event-ID", event_id),
            ("Supported", "eventlist, dialog, call-info, terminate"),
        ]

    def build_subscribe(self, event, accept, expires, event_id):
        self.event = event
        self.accept = accept
        self.event_id = event_id
        return self._build(
            "SUBSCRIBE",
            self._next_cseq(),
            extra=self._subscription_extra(event, accept, expires, event_id),
        )

    def build_refresh(self, expires):
        return self._build(
            "SUBSCRIBE",
            self._next_cseq(),
            extra=self._subscription_extra(self.event, self.accept, expires,
                                          self.event_id),
        )

    def build_unsubscribe(self):
        return self._build(
            "SUBSCRIBE",
            self._next_cseq(),
            extra=self._subscription_extra(self.event, self.accept, 0,
                                          self.event_id),
        )

    def send_invite(self):
        return self.client.send(self.build_invite().build().encode())

    def send_ack(self, to_tag=None):
        return self.client.send(self.build_ack(to_tag).build().encode())

    def send_bye(self, to_tag=None):
        return self.client.send(self.build_bye(to_tag).build().encode())

    def send_subscribe(self, event, accept, expires, event_id):
        return self.client.send(
            self.build_subscribe(event, accept, expires, event_id).build().encode())

    def send_refresh(self, expires):
        return self.client.send(self.build_refresh(expires).build().encode())

    def send_unsubscribe(self):
        return self.client.send(self.build_unsubscribe().build().encode())


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