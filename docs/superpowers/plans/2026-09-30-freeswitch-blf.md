# FreeSWITCH BLF Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable BLF buttons to the Python SIP client, show their status changes in the console, and allow pressing a button from a terminal command.

**Architecture:** Split per-dialog SIP state out of `SIPClient` into a new `SIPDialog` class, so a BLF subscription dialog can coexist with a call dialog on one socket. Add `blf.py` for BLF state and NOTIFY XML parsing, `console.py` for terminal command parsing, and `call_session.py` for a non-blocking call state machine. `main.py` becomes a single dispatch loop handling SIP messages and console commands together.

**Tech Stack:** Python 3.14, stdlib only (`socket`, `xml.etree.ElementTree`, `queue`, `threading`, `unittest`). No new dependencies, including pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-freeswitch-blf-design.md`

## Global Constraints

- Stdlib only. No third-party package, including pytest. Tests run under `unittest`.
- Python 3.14 is the interpreter. Run every command from the repository root.
- Header ordering in a built SIP message is not significant. Tests assert header presence and value, never position.
- SIP line separator is CRLF (`\r\n`) everywhere. Do not introduce bare LF.
- `config.json` keys not named in the spec keep their current meaning and default.
- Every BLF status transition is logged as one line beginning `[BLF] `.
- Out of scope: registration, digest auth, `NOTIFY`-based press, per-button domains, full-screen console UI.
- Running with no flags must behave exactly as before. That is the regression contract.

### Deliberate deviation from the spec

The spec's file table puts command parsing in `main.py`. This plan adds `console.py`
for command parsing and the stdin reader, keeping pure testable code out of the entry
point. Dispatch of a parsed command stays in `main.py`, as the spec specifies. The spec
also implies `--blf` can combine with a call but does not say how to get BLF *without*
one; `--no-call` is added for that.

### File map

| File | Responsibility |
|---|---|
| `sip_client.py` | Transport (`SIPClient`) and per-dialog message building (`SIPDialog`) |
| `blf.py` | BLF buttons, NOTIFY XML parsing, subscription lifecycle |
| `call_session.py` | One outbound call as a non-blocking state machine; URI parsing |
| `console.py` | Command line parsing and the stdin reader thread |
| `main.py` | Argument parsing, wiring, the single dispatch loop |

---

### Task 1: Extract SIPDialog from SIPClient

The client keeps one `call_id`, `cseq`, and tag for the whole process today. BLF needs a
second dialog running concurrently on the same socket, so per-dialog state must move out
of the transport object. This task is a pure refactor and must not change any message
that goes on the wire.

**Files:**
- Modify: `sip_client.py`
- Create: `tests/__init__.py` (empty), `tests/test_dialog.py`

**Interfaces:**
- Consumes: nothing. First task.
- Produces:
  - `class SIPClient`: `__init__(config)`, `open(remote_addr)`, `create_dialog(target_uri) -> SIPDialog`, `send(data)`, `receive(timeout=5)`, `parse_sdp(sdp_body)`, `build_sdp()`, `close()`. Attributes `local_ip`, `local_port`, `transport`, `from_user`, `display_name`, `user_agent`, `rtp_port`, `socket`, `remote_addr`, `last_sdp`.
  - `class SIPDialog`: `__init__(client, target_uri)`, `build_invite()`, `build_ack(to_tag=None)`, `build_bye(to_tag=None)`, `send_invite()`, `send_ack(to_tag=None)`, `send_bye(to_tag=None)`. Attributes `client`, `target_uri`, `call_id`, `cseq`, `from_tag`, `to_tag`.
  - Removed: `SIPClient.send_invite`, `send_ack`, `send_bye`, `_parse_uri`, `_create_socket`, `_next_cseq`, `_next_branch`.
  - New: `SIPClient.open(remote_addr)` replaces the implicit socket creation that used to
    live inside `send_invite`. `main.py` must call it. `send()` raises `RuntimeError` if
    the socket is not open.

- [ ] **Step 1: Create the test package and write the failing test**

Create `tests/__init__.py` as an empty file. Create `tests/test_dialog.py`:

```python
import unittest

from sip_client import SIPClient, SIPDialog


def make_client():
    return SIPClient({
        "local_ip": "192.168.0.10",
        "local_port": 5080,
        "transport": "udp",
        "from_user": "100",
        "display_name": "SIP Client",
        "user_agent": "PythonSIP/1.0",
        "rtp_port": 10000,
    })


class DialogMessageShapeTest(unittest.TestCase):
    """Guards the bytes the client puts on the wire. Header order is not asserted."""

    def setUp(self):
        self.client = make_client()
        self.dialog = self.client.create_dialog("sip:101@192.168.0.89:5060")

    def test_invite_carries_expected_headers(self):
        msg = self.dialog.build_invite()
        self.assertEqual(msg.method, "INVITE")
        self.assertEqual(msg.uri, "sip:101@192.168.0.89:5060")
        self.assertIn("SIP/2.0/UDP 192.168.0.10:5080", msg.headers["Via"])
        self.assertIn(";rport", msg.headers["Via"])
        self.assertEqual(msg.headers["Max-Forwards"], "70")
        self.assertIn("<sip:100@192.168.0.10:5080>;tag=", msg.headers["From"])
        self.assertEqual(msg.headers["To"], "<sip:101@192.168.0.89:5060>")
        self.assertEqual(msg.headers["CSeq"], "1 INVITE")
        self.assertEqual(msg.headers["Contact"],
                         "<sip:100@192.168.0.10:5080;transport=udp>")
        self.assertEqual(msg.headers["User-Agent"], "PythonSIP/1.0")
        self.assertEqual(msg.headers["Content-Type"], "application/sdp")
        self.assertTrue(msg.body.startswith("v=0\r\n"))

    def test_ack_reuses_invite_cseq_and_carries_to_tag(self):
        self.dialog.build_invite()
        msg = self.dialog.build_ack(to_tag="remoteTAG")
        self.assertEqual(msg.method, "ACK")
        self.assertEqual(msg.headers["CSeq"], "1 ACK")
        self.assertEqual(msg.headers["To"],
                         "<sip:101@192.168.0.89:5060>;tag=remoteTAG")
        self.assertEqual(msg.headers["Call-ID"], self.dialog.call_id)

    def test_bye_increments_cseq_and_keeps_call_id(self):
        invite = self.dialog.build_invite()
        ack = self.dialog.build_ack(to_tag="remoteTAG")
        msg = self.dialog.build_bye(to_tag="remoteTAG")
        self.assertEqual(msg.method, "BYE")
        self.assertEqual(msg.headers["CSeq"], "2 BYE")
        self.assertEqual(msg.headers["Call-ID"], invite.headers["Call-ID"])
        self.assertEqual(msg.headers["Call-ID"], ack.headers["Call-ID"])

    def test_building_advances_cseq_exactly_once(self):
        self.dialog.build_invite()
        self.assertEqual(self.dialog.cseq, 2)

    def test_every_dialog_has_independent_identity(self):
        other = self.client.create_dialog("sip:102@192.168.0.89:5060")
        self.assertNotEqual(other.call_id, self.dialog.call_id)
        self.assertNotEqual(other.from_tag, self.dialog.from_tag)
        self.assertEqual(other.cseq, 1)


class SIPClientTest(unittest.TestCase):
    def test_sdp_advertises_configured_media_port(self):
        sdp = make_client().build_sdp()
        self.assertIn("m=audio 10000 RTP/AVP 0 8 101", sdp)
        self.assertIn("c=IN IP4 192.168.0.10", sdp)

    def test_parse_sdp_reads_media_address(self):
        parsed = make_client().parse_sdp(
            "v=0\r\nc=IN IP4 10.0.0.5\r\nm=audio 12000 RTP/AVP 0\r\n")
        self.assertEqual(parsed["media_ip"], "10.0.0.5")
        self.assertEqual(parsed["media_port"], 12000)

    def test_create_dialog_binds_to_client(self):
        client = make_client()
        dialog = client.create_dialog("sip:101@192.168.0.89:5060")
        self.assertIsInstance(dialog, SIPDialog)
        self.assertIs(dialog.client, client)

    def test_send_without_open_socket_raises(self):
        with self.assertRaises(RuntimeError):
            make_client().send(b"test")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_dialog -v`
Expected: FAIL with `AttributeError: module 'sip_client' has no attribute 'SIPDialog'`

- [ ] **Step 3: Rewrite sip_client.py**

Replace the whole file with:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_dialog -v`
Expected: PASS, 10 tests OK

- [ ] **Step 5: Commit**

```bash
git add sip_client.py tests/__init__.py tests/test_dialog.py
git commit -m "refactor: extract SIPDialog so multiple dialogs share one socket"
```

---

### Task 2: SUBSCRIBE, refresh and unsubscribe on SIPDialog

**Files:**
- Modify: `sip_client.py` (add methods to `SIPDialog`)
- Modify: `tests/test_dialog.py` (append a class)

**Interfaces:**
- Consumes: `SIPDialog` from Task 1.
- Produces on `SIPDialog`:
  - `build_subscribe(event, accept, expires, event_id) -> SIPMessage`
  - `build_refresh(expires) -> SIPMessage`
  - `build_unsubscribe() -> SIPMessage`
  - `send_subscribe(event, accept, expires, event_id)`
  - `send_refresh(expires)`
  - `send_unsubscribe()`
  - `build_subscribe` records `event`, `accept`, `event_id` on the dialog so a later
    refresh repeats them. `build_refresh` and `build_unsubscribe` reuse those.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dialog.py`, before the `if __name__ == "__main__":` block:

```python
class SubscribeMessageShapeTest(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.dialog = self.client.create_dialog("sip:101@default")

    def test_subscribe_carries_blf_headers(self):
        msg = self.dialog.build_subscribe("dialog", "application/blf+xml",
                                          3600, "blf-101@default")
        self.assertEqual(msg.method, "SUBSCRIBE")
        self.assertEqual(msg.uri, "sip:101@default")
        self.assertEqual(msg.headers["Event"], "dialog")
        self.assertEqual(msg.headers["Accept"], "application/blf+xml")
        self.assertEqual(msg.headers["Expires"], "3600")
        self.assertEqual(msg.headers["Event-ID"], "blf-101@default")
        self.assertEqual(msg.headers["Supported"],
                         "eventlist, dialog, call-info, terminate")
        self.assertEqual(msg.headers["Max-Forwards"], "70")
        # A watcher sends no body, so no Content-Type and zero length.
        self.assertNotIn("Content-Type", msg.headers)
        self.assertEqual(msg.headers["Content-Length"], "0")

    def test_subscribe_uses_cseq_one(self):
        msg = self.dialog.build_subscribe("dialog", "application/blf+xml",
                                          3600, "blf-101@default")
        self.assertEqual(msg.headers["CSeq"], "1 SUBSCRIBE")

    def test_refresh_reuses_call_id_and_increments_cseq(self):
        first = self.dialog.build_subscribe("dialog", "application/blf+xml",
                                            3600, "blf-101@default")
        msg = self.dialog.build_refresh(1800)
        self.assertEqual(msg.method, "SUBSCRIBE")
        self.assertEqual(msg.uri, "sip:101@default")
        self.assertEqual(msg.headers["Call-ID"], first.headers["Call-ID"])
        self.assertEqual(msg.headers["CSeq"], "2 SUBSCRIBE")
        self.assertEqual(msg.headers["Expires"], "1800")
        self.assertEqual(msg.headers["Event-ID"], "blf-101@default")
        self.assertEqual(msg.headers["To"], "<sip:101@default>")
        self.assertEqual(msg.headers["From"].split(";tag=")[1],
                         first.headers["From"].split(";tag=")[1])

    def test_unsubscribe_expires_zero(self):
        self.dialog.build_subscribe("dialog", "application/blf+xml",
                                    3600, "blf-101@default")
        msg = self.dialog.build_unsubscribe()
        self.assertEqual(msg.headers["Expires"], "0")
        self.assertEqual(msg.headers["CSeq"], "2 SUBSCRIBE")

    def test_subscription_dialog_does_not_disturb_a_call_dialog(self):
        sub = self.dialog.build_subscribe("dialog", "application/blf+xml",
                                          3600, "blf-101@default")
        call = self.client.create_dialog("sip:101@192.168.0.89:5060")
        invite = call.build_invite()
        self.assertNotEqual(invite.headers["Call-ID"], sub.headers["Call-ID"])
        self.assertEqual(invite.headers["CSeq"], "1 INVITE")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_dialog.SubscribeMessageShapeTest -v`
Expected: FAIL with `AttributeError: 'SIPDialog' object has no attribute 'build_subscribe'`

- [ ] **Step 3: Implement the methods**

Add to `SIPDialog`, directly after `build_bye`:

```python
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
```

Add the send wrappers directly after `send_bye`:

```python
    def send_subscribe(self, event, accept, expires, event_id):
        return self.client.send(
            self.build_subscribe(event, accept, expires, event_id).build().encode())

    def send_refresh(self, expires):
        return self.client.send(self.build_refresh(expires).build().encode())

    def send_unsubscribe(self):
        return self.client.send(self.build_unsubscribe().build().encode())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_dialog -v`
Expected: PASS, 15 tests OK

- [ ] **Step 5: Commit**

```bash
git add sip_client.py tests/test_dialog.py
git commit -m "feat: SUBSCRIBE, refresh and unsubscribe on SIPDialog"
```

---

### Task 3: BLF button state and NOTIFY XML parsing

**Files:**
- Create: `blf.py`
- Create: `tests/test_blf_xml.py`

**Interfaces:**
- Consumes: nothing. Pure module, no dependency on `sip_client`.
- Produces, importable from `blf`:
  - `KNOWN_STATES = ("idle", "ringing", "busy", "held")`
  - `class BLFParseError(Exception)`
  - `parse_blf_xml(body) -> dict` mapping instance name to its params dict. Raises `BLFParseError` on unusable input.
  - `extract_state(params) -> str`, one of `KNOWN_STATES` or `"unknown"`.
  - `format_change(button) -> str`
  - `class BLFButton`: `__init__(self, name, number, domain, label=None)`, attributes `name`, `number`, `domain`, `label`, `state`, `previous_state`, `caller_name`, `caller_number`, `uuid`, `changed_at`, plus `subscribe_uri` property and `apply(params) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_blf_xml.py`:

```python
import unittest

from blf import (BLFButton, BLFParseError, extract_state, format_change,
                  parse_blf_xml)

NAMESPACED = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n'
    '<event name="dialog" xmlns="urn:freedeswitch:blf">\r\n'
    '  <instance name="101@default">\r\n'
    '    <params>\r\n'
    '      <param name="uuid" value="8f3c1a"/>\r\n'
    '      <param name="caller_id_name" value="Ivan"/>\r\n'
    '      <param name="caller_id_number" value="5551234"/>\r\n'
    '      <param name="rpid" value="ringing"/>\r\n'
    '    </params>\r\n'
    '  </instance>\r\n'
    '</event>\r\n'
)


class ParseBlfXmlTest(unittest.TestCase):
    def test_parses_namespaced_instance(self):
        instances = parse_blf_xml(NAMESPACED)
        self.assertEqual(list(instances), ["101@default"])
        params = instances["101@default"]
        self.assertEqual(params["rpid"], "ringing")
        self.assertEqual(params["caller_id_number"], "5551234")
        self.assertEqual(params["uuid"], "8f3c1a")

    def test_parses_without_namespace(self):
        body = ('<event name="dialog"><instance name="101@default">'
                '<param name="rpid" value="busy"/></instance></event>')
        self.assertEqual(parse_blf_xml(body)["101@default"]["rpid"], "busy")

    def test_parses_multiple_instances(self):
        body = ('<event xmlns="urn:freedeswitch:blf">'
                '<instance name="101@default"><param name="rpid" value="idle"/></instance>'
                '<instance name="102@default"><param name="rpid" value="busy"/></instance>'
                '</event>')
        instances = parse_blf_xml(body)
        self.assertEqual(instances["101@default"]["rpid"], "idle")
        self.assertEqual(instances["102@default"]["rpid"], "busy")

    def test_instance_without_name_is_skipped(self):
        body = '<event><instance><param name="rpid" value="idle"/></instance></event>'
        self.assertEqual(parse_blf_xml(body), {})

    def test_instance_without_params_is_empty_dict(self):
        body = '<event><instance name="101@default"></instance></event>'
        self.assertEqual(parse_blf_xml(body), {"101@default": {}})

    def test_malformed_xml_raises(self):
        with self.assertRaises(BLFParseError):
            parse_blf_xml("<event><instance></event>")

    def test_empty_body_raises(self):
        with self.assertRaises(BLFParseError):
            parse_blf_xml("")


class ExtractStateTest(unittest.TestCase):
    def test_reads_rpid(self):
        self.assertEqual(extract_state({"rpid": "ringing"}), "ringing")

    def test_falls_back_to_rpid_value(self):
        self.assertEqual(extract_state({"rpid_value": "busy"}), "busy")

    def test_falls_back_to_state_param(self):
        self.assertEqual(extract_state({"state": "held"}), "held")

    def test_rpid_wins_over_fallbacks(self):
        self.assertEqual(extract_state({"rpid": "idle", "rpid_value": "busy"}), "idle")

    def test_unknown_value_becomes_unknown(self):
        self.assertEqual(extract_state({"rpid": "probing"}), "unknown")

    def test_missing_value_becomes_unknown(self):
        self.assertEqual(extract_state({}), "unknown")

    def test_value_is_case_insensitive(self):
        self.assertEqual(extract_state({"rpid": "BUSY"}), "busy")


class BLFButtonTest(unittest.TestCase):
    def test_starts_unknown(self):
        button = BLFButton("101", "101", "default")
        self.assertEqual(button.state, "unknown")
        self.assertEqual(button.previous_state, "unknown")
        self.assertIsNone(button.caller_name)
        self.assertIsNone(button.caller_number)
        self.assertIsNone(button.uuid)

    def test_label_defaults_to_number(self):
        self.assertEqual(BLFButton("101", "555", "default").label, "555")

    def test_subscribe_uri_uses_number_and_domain(self):
        self.assertEqual(BLFButton("operator", "101", "default").subscribe_uri,
                         "sip:101@default")

    def test_apply_returns_false_when_nothing_changed(self):
        button = BLFButton("101", "101", "default")
        self.assertTrue(button.apply({"rpid": "idle"}))
        self.assertFalse(button.apply({"rpid": "idle"}))

    def test_apply_records_previous_state(self):
        button = BLFButton("101", "101", "default")
        button.apply({"rpid": "idle"})
        button.apply({"rpid": "ringing"})
        self.assertEqual(button.previous_state, "idle")
        self.assertEqual(button.state, "ringing")

    def test_apply_notices_caller_change_without_state_change(self):
        button = BLFButton("101", "101", "default")
        button.apply({"rpid": "ringing", "caller_id_number": "111"})
        self.assertTrue(button.apply({"rpid": "ringing", "caller_id_number": "222"}))
        self.assertEqual(button.caller_number, "222")

    def test_apply_clears_caller_when_absent(self):
        button = BLFButton("101", "101", "default")
        button.apply({"rpid": "ringing", "caller_id_number": "111"})
        button.apply({"rpid": "idle"})
        self.assertIsNone(button.caller_number)

    def test_empty_param_values_become_none(self):
        button = BLFButton("101", "101", "default")
        button.apply({"caller_id_name": "", "caller_id_number": ""})
        self.assertIsNone(button.caller_name)
        self.assertIsNone(button.caller_number)


class FormatChangeTest(unittest.TestCase):
    def test_includes_transition_name_and_caller(self):
        button = BLFButton("101", "101", "default", "Operator 1")
        button.apply({"rpid": "ringing", "caller_id_name": "Ivan",
                      "caller_id_number": "5551234"})
        line = format_change(button)
        self.assertTrue(line.startswith("[BLF] 101 (Operator 1): unknown -> ringing"))
        self.assertIn('caller="Ivan"', line)
        self.assertIn("<5551234>", line)

    def test_includes_uuid_when_present(self):
        button = BLFButton("101", "101", "default")
        button.apply({"rpid": "busy", "uuid": "8f3c1a"})
        self.assertIn("uuid=8f3c1a", format_change(button))

    def test_omits_caller_when_unknown(self):
        button = BLFButton("101", "101", "default")
        button.apply({"rpid": "idle"})
        self.assertNotIn("caller=", format_change(button))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_blf_xml -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'blf'`

- [ ] **Step 3: Create blf.py**

```python
import time
from xml.etree import ElementTree

KNOWN_STATES = ("idle", "ringing", "busy", "held")


class BLFParseError(Exception):
    """Raised when a NOTIFY body is not usable BLF XML."""


def _local_name(tag):
    """Strip a namespace so 'instance' and '{urn:...}instance' both match."""
    return tag.rsplit("}", 1)[-1]


def parse_blf_xml(body):
    """Parse a BLF NOTIFY body into {instance_name: {param_name: param_value}}.

    FreeSWITCH varies the body between versions, so this is deliberately
    tolerant: namespaces are ignored and unknown elements are skipped.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise BLFParseError(str(e)) from e

    instances = {}
    for node in root.iter():
        if _local_name(node.tag) != "instance":
            continue
        name = node.get("name")
        if not name:
            continue
        params = {}
        # Iterate the whole subtree: FreeSWITCH nests the params in a <params>
        # wrapper, so the param elements are grandchildren of <instance>, not
        # direct children.
        for child in node.iter():
            if _local_name(child.tag) != "param":
                continue
            param_name = child.get("name")
            if param_name:
                params[param_name] = child.get("value", "")
        instances[name] = params
    return instances


def extract_state(params):
    """Read the BLF state, tolerating FreeSWITCH's differing param names."""
    for key in ("rpid", "rpid_value", "state"):
        raw = params.get(key)
        if raw:
            value = raw.strip().lower()
            return value if value in KNOWN_STATES else "unknown"
    return "unknown"


class BLFButton:
    def __init__(self, name, number, domain, label=None):
        self.name = name
        self.number = number
        self.domain = domain
        self.label = label or number
        self.state = "unknown"
        self.previous_state = "unknown"
        self.caller_name = None
        self.caller_number = None
        self.uuid = None
        self.changed_at = time.time()

    @property
    def subscribe_uri(self):
        return f"sip:{self.number}@{self.domain}"

    def apply(self, params):
        """Update from one NOTIFY instance's params. True if anything changed."""
        incoming = (extract_state(params),
                    params.get("caller_id_name") or None,
                    params.get("caller_id_number") or None,
                    params.get("uuid") or None)
        current = (self.state, self.caller_name, self.caller_number, self.uuid)
        if current == incoming:
            return False

        self.previous_state = self.state
        self.state = incoming[0]
        self.caller_name = incoming[1]
        self.caller_number = incoming[2]
        self.uuid = incoming[3]
        self.changed_at = time.time()
        return True


def format_change(button):
    line = f"[BLF] {button.name} ({button.label}): {button.previous_state} -> {button.state}"
    if button.caller_name or button.caller_number:
        line += f'  caller="{button.caller_name or ""}" <{button.caller_number or ""}>'
    if button.uuid:
        line += f"  uuid={button.uuid}"
    return line
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_blf_xml -v`
Expected: PASS, 24 tests OK

- [ ] **Step 5: Commit**

```bash
git add blf.py tests/test_blf_xml.py
git commit -m "feat: BLF button state and tolerant NOTIFY XML parsing"
```

---

### Task 4: BLF configuration loading

**Files:**
- Modify: `blf.py` (append)
- Create: `tests/test_blf_config.py`

**Interfaces:**
- Consumes: `BLFButton` from Task 3.
- Produces, importable from `blf`:
  - `load_buttons(config, log=print) -> list[BLFButton]`
  - `resolve_timing(config, log=print) -> tuple[int, int]` returning `(expires, refresh)`

- [ ] **Step 1: Write the failing test**

Create `tests/test_blf_config.py`:

```python
import unittest

from blf import load_buttons, resolve_timing


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


class LoadButtonsTest(unittest.TestCase):
    def test_loads_buttons_with_domain_from_config(self):
        config = {"blf": {"domain": "example.com",
                          "buttons": [{"name": "101", "number": "101",
                                       "label": "Operator 1"}]}}
        buttons = load_buttons(config)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].name, "101")
        self.assertEqual(buttons[0].label, "Operator 1")
        self.assertEqual(buttons[0].domain, "example.com")
        self.assertEqual(buttons[0].subscribe_uri, "sip:101@example.com")

    def test_number_defaults_to_name(self):
        buttons = load_buttons({"blf": {"buttons": [{"name": "555"}]}})
        self.assertEqual(buttons[0].number, "555")
        self.assertEqual(buttons[0].subscribe_uri, "sip:555@default")

    def test_label_defaults_to_number(self):
        buttons = load_buttons({"blf": {"buttons": [{"name": "op", "number": "101"}]}})
        self.assertEqual(buttons[0].label, "101")

    def test_default_domain_is_default(self):
        buttons = load_buttons({"blf": {"buttons": [{"name": "101"}]}})
        self.assertEqual(buttons[0].domain, "default")

    def test_missing_blf_section_yields_no_buttons(self):
        self.assertEqual(load_buttons({}), [])

    def test_null_blf_section_yields_no_buttons(self):
        self.assertEqual(load_buttons({"blf": None}), [])

    def test_button_without_name_is_dropped_with_warning(self):
        log = RecordingLog()
        buttons = load_buttons(
            {"blf": {"buttons": [{"number": "101"}, {"name": "102"}]}}, log=log)
        self.assertEqual([b.name for b in buttons], ["102"])
        self.assertIn("has no 'name'", log)

    def test_duplicate_name_is_dropped_with_warning(self):
        log = RecordingLog()
        buttons = load_buttons(
            {"blf": {"buttons": [{"name": "101"},
                                 {"name": "101", "number": "999"}]}}, log=log)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].number, "101")
        self.assertIn("duplicate", log)

    def test_non_object_button_is_dropped_with_warning(self):
        log = RecordingLog()
        buttons = load_buttons(
            {"blf": {"buttons": ["101", {"name": "102"}]}}, log=log)
        self.assertEqual([b.name for b in buttons], ["102"])
        self.assertIn("not an object", log)

    def test_order_is_preserved(self):
        buttons = load_buttons(
            {"blf": {"buttons": [{"name": n} for n in ("102", "101", "103")]}})
        self.assertEqual([b.name for b in buttons], ["102", "101", "103"])


class ResolveTimingTest(unittest.TestCase):
    def test_reads_values(self):
        self.assertEqual(resolve_timing({"blf": {"expires": 1200, "refresh": 600}}),
                         (1200, 600))

    def test_defaults(self):
        self.assertEqual(resolve_timing({}), (3600, 1800))

    def test_refresh_above_expires_is_clamped_with_warning(self):
        log = RecordingLog()
        expires, refresh = resolve_timing(
            {"blf": {"expires": 600, "refresh": 900}}, log=log)
        self.assertEqual((expires, refresh), (600, 300))
        self.assertIn("blf.refresh", log)

    def test_refresh_equal_to_expires_is_clamped(self):
        _, refresh = resolve_timing({"blf": {"expires": 600, "refresh": 600}})
        self.assertEqual(refresh, 300)

    def test_tiny_expires_still_yields_positive_refresh(self):
        _, refresh = resolve_timing({"blf": {"expires": 1, "refresh": 5}})
        self.assertGreaterEqual(refresh, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_blf_config -v`
Expected: FAIL with `ImportError: cannot import name 'load_buttons' from 'blf'`

- [ ] **Step 3: Append to blf.py**

```python
def load_buttons(config, log=print):
    """Build the button list from the config's blf section, dropping bad entries."""
    section = config.get("blf") or {}
    domain = section.get("domain", "default")
    buttons = []
    seen = set()

    for index, raw in enumerate(section.get("buttons") or []):
        if not isinstance(raw, dict):
            log(f"[!] blf.buttons[{index}] is not an object, skipped")
            continue
        name = raw.get("name")
        if not name:
            log(f"[!] blf.buttons[{index}] has no 'name', skipped")
            continue
        if name in seen:
            log(f"[!] blf.buttons[{index}] duplicate name '{name}', skipped")
            continue
        seen.add(name)
        buttons.append(
            BLFButton(name, raw.get("number") or name, domain, raw.get("label")))

    return buttons


def resolve_timing(config, log=print):
    """Return (expires, refresh), clamping refresh so it always precedes expiry."""
    section = config.get("blf") or {}
    expires = int(section.get("expires", 3600))
    refresh = int(section.get("refresh", 1800))
    if refresh >= expires:
        clamped = max(1, expires // 2)
        log(f"[!] blf.refresh ({refresh}) >= blf.expires ({expires}), using {clamped}")
        refresh = clamped
    return expires, refresh
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_blf_config -v`
Expected: PASS, 14 tests OK

- [ ] **Step 5: Commit**

```bash
git add blf.py tests/test_blf_config.py
git commit -m "feat: load BLF buttons and subscription timing from config"
```

---

### Task 5: BLFManager subscription lifecycle

**Files:**
- Modify: `blf.py` (append)
- Create: `tests/test_blf_manager.py`

**Interfaces:**
- Consumes: `BLFButton`, `parse_blf_xml`, `extract_state`, `format_change`, `BLFParseError` from Task 3; `SIPClient.create_dialog` and `SIPDialog.send_subscribe` / `send_refresh` / `send_unsubscribe` from Tasks 1 and 2.
- Produces, importable from `blf`:
  - `class BLFManager`: `__init__(self, client, buttons, expires=3600, refresh=1800, log=print)`, `get(name)`, `snapshot()`, `subscribe_all()`, `refresh_due()`, `force_refresh()`, `unsubscribe_all()`, `handle_notify(msg) -> list[BLFButton]`.
  - `handle_notify` reads only `msg.method`, `msg.headers` and `msg.body`, so any object
    with those attributes works. Tests use fakes.
  - Instance methods `_subscribe(button)`, `_find_by_resource(resource)`, and attribute
    `_next_refresh` (dict name to deadline timestamp) are used by tests to force expiry.

- [ ] **Step 1: Write the failing test**

Create `tests/test_blf_manager.py`:

```python
import time
import unittest

from blf import BLFButton, BLFManager


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


class FakeDialog:
    def __init__(self, target_uri):
        self.target_uri = target_uri
        self.call_id = f"call-{target_uri}"
        self.subscribes = []
        self.refreshes = []
        self.unsubscribes = []

    def send_subscribe(self, event, accept, expires, event_id):
        self.subscribes.append((event, accept, expires, event_id))

    def send_refresh(self, expires):
        self.refreshes.append(expires)

    def send_unsubscribe(self):
        self.unsubscribes.append(True)


class FakeClient:
    def __init__(self):
        self.dialogs = []

    def create_dialog(self, target_uri):
        dialog = FakeDialog(target_uri)
        self.dialogs.append(dialog)
        return dialog


class FakeMessage:
    def __init__(self, method, body="", headers=None):
        self.method = method
        self.body = body
        self.headers = headers or {}


class Response:
    def __init__(self, code, reason, headers=None):
        self.method = None
        self.status_code = code
        self.reason = reason
        self.body = ""
        self.headers = headers or {}


def notify(instances_xml):
    return FakeMessage("NOTIFY", body=instances_xml,
                       headers={"Content-Type": "application/blf+xml"})


def instance(name, rpid, **extra):
    params = [f'<param name="rpid" value="{rpid}"/>']
    for key, value in extra.items():
        params.append(f'<param name="{key}" value="{value}"/>')
    return f'<instance name="{name}"><params>{"".join(params)}</params></instance>'


def body(*instances):
    return ('<event name="dialog" xmlns="urn:freedeswitch:blf">'
            f'{"".join(instances)}</event>')


def make_manager(buttons=None, **kwargs):
    client = FakeClient()
    if buttons is None:
        buttons = [BLFButton("101", "101", "default", "Operator 1"),
                   BLFButton("102", "102", "default")]
    log = RecordingLog()
    return client, BLFManager(client, buttons, log=log, **kwargs), log


class SubscribeTest(unittest.TestCase):
    def test_one_dialog_per_button(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertEqual([d.target_uri for d in client.dialogs],
                         ["sip:101@default", "sip:102@default"])
        self.assertEqual(client.dialogs[0].subscribes,
                         [("dialog", "application/blf+xml", 3600, "blf-101@default")])

    def test_uses_configured_timing(self):
        client, manager, log = make_manager(expires=1200, refresh=600)
        manager.subscribe_all()
        self.assertEqual(client.dialogs[0].subscribes[0][2], 1200)

    def test_logs_each_subscription(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        self.assertIn("SUBSCRIBE 101", log)
        self.assertIn("sip:101@default", log)

    def test_no_buttons_sends_nothing(self):
        client, manager, log = make_manager(buttons=[])
        manager.subscribe_all()
        self.assertEqual(client.dialogs, [])

    def test_refresh_due_is_quiet_before_the_deadline(self):
        client, manager, log = make_manager(refresh=1800)
        manager.subscribe_all()
        manager.refresh_due()
        self.assertEqual(client.dialogs[0].refreshes, [])

    def test_refresh_due_refreshes_only_expired_buttons(self):
        client, manager, log = make_manager(refresh=1800)
        manager.subscribe_all()
        manager._next_refresh["101"] = time.time() - 1
        manager.refresh_due()
        # A re-SUBSCRIBE re-sends the original Expires. blf.refresh only decides
        # WHEN the client re-subscribes; it is never the Expires value.
        self.assertEqual(client.dialogs[0].refreshes, [3600])
        self.assertEqual(client.dialogs[1].refreshes, [])

    def test_force_refresh_hits_every_button(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.force_refresh()
        self.assertEqual(len(client.dialogs[0].refreshes), 1)
        self.assertEqual(len(client.dialogs[1].refreshes), 1)

    def test_force_refresh_before_subscribe_is_harmless(self):
        client, manager, log = make_manager()
        manager.force_refresh()
        self.assertEqual(client.dialogs, [])

    def test_unsubscribe_all_sends_for_every_button(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.unsubscribe_all()
        self.assertEqual(len(client.dialogs[0].unsubscribes), 1)
        self.assertEqual(len(client.dialogs[1].unsubscribes), 1)

    def test_unsubscribe_before_subscribe_is_harmless(self):
        _, manager, log = make_manager()
        manager.unsubscribe_all()


class HandleNotifyTest(unittest.TestCase):
    def test_updates_matching_button_state(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        changes = manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        self.assertEqual([b.name for b in changes], ["101"])
        self.assertEqual(manager.get("101").state, "ringing")

    def test_logs_the_transition(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        self.assertIn("101 (Operator 1): unknown -> ringing", log)

    def test_matches_by_name_when_number_differs(self):
        button = BLFButton("op-one", "101", "default")
        _, manager, log = make_manager(buttons=[button])
        manager.subscribe_all()
        manager.handle_notify(notify(body(instance("op-one", "busy"))))
        self.assertEqual(button.state, "busy")

    def test_updates_several_buttons_from_one_notify(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(
            notify(body(instance("101@default", "ringing"),
                       instance("102@default", "busy"))))
        self.assertEqual(manager.get("101").state, "ringing")
        self.assertEqual(manager.get("102").state, "busy")

    def test_ignores_unknown_instance(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        self.assertEqual(
            manager.handle_notify(notify(body(instance("999@default", "ringing")))), [])

    def test_unchanged_state_logs_nothing(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        before = len(log.lines)
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        self.assertEqual(len(log.lines), before)

    def test_ignores_non_notify(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        msg = FakeMessage("BYE", body=body(instance("101@default", "ringing")))
        self.assertEqual(manager.handle_notify(msg), [])

    def test_ignores_other_content_type(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        msg = FakeMessage("NOTIFY", body=body(instance("101@default", "ringing")),
                          headers={"Content-Type": "application/simple-message-summary"})
        self.assertEqual(manager.handle_notify(msg), [])

    def test_malformed_body_is_logged_once(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify("<event><instance></event>"))
        manager.handle_notify(notify("<event><instance></event>"))
        hits = [line for line in log.lines if "could not be parsed" in line]
        self.assertEqual(len(hits), 1)

    def test_parse_logging_resumes_after_a_good_notify(self):
        _, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify("<event><instance></event>"))
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        manager.handle_notify(notify("<event><instance></event>"))
        hits = [line for line in log.lines if "could not be parsed" in line]
        self.assertEqual(len(hits), 2)

    def test_snapshot_preserves_order(self):
        _, manager, log = make_manager()
        self.assertEqual([b.name for b in manager.snapshot()], ["101", "102"])

    def test_get_unknown_name_returns_none(self):
        _, manager, log = make_manager()
        self.assertIsNone(manager.get("777"))


class SubscribeResponseTest(unittest.TestCase):
    def test_accepted_subscription_is_logged(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        response = Response(200, "OK",
                            headers={"Call-ID": client.dialogs[0].call_id})
        self.assertTrue(manager.handle_response(response))
        self.assertIn("SUBSCRIBE 101 accepted (200 OK)", log)

    def test_rejected_subscription_is_logged_and_state_stays_unknown(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        response = Response(404, "Not Found",
                            headers={"Call-ID": client.dialogs[0].call_id})
        self.assertTrue(manager.handle_response(response))
        self.assertIn("SUBSCRIBE 101 rejected: 404 Not Found", log)
        self.assertEqual(manager.get("101").state, "unknown")

    def test_rejection_is_reported_once_per_response(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_response(
            Response(503, "Service Unavailable",
                     headers={"Call-ID": client.dialogs[1].call_id}))
        hits = [line for line in log.lines if "rejected" in line]
        self.assertEqual(len(hits), 1)
        self.assertIn("SUBSCRIBE 102 rejected: 503", hits[0])

    def test_call_traffic_is_not_claimed(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        stray = Response(200, "OK", headers={"Call-ID": "some-call-dialog"})
        self.assertFalse(manager.handle_response(stray))

    def test_refresh_responses_reuse_the_subscription_call_id(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.force_refresh()
        response = Response(200, "OK",
                            headers={"Call-ID": client.dialogs[0].call_id})
        self.assertTrue(manager.handle_response(response))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_blf_manager -v`
Expected: FAIL with `ImportError: cannot import name 'BLFManager' from 'blf'`

- [ ] **Step 3: Append to blf.py**

```python
class BLFManager:
    """Owns the BLF subscription dialogs and the current state of each button.

    It reads SIP messages but knows nothing about RTP or the dialplan. Pressing
    a button is not its concern: callers resolve a button with get() and dial it.
    """

    def __init__(self, client, buttons, expires=3600, refresh=1800, log=print):
        self.client = client
        self.expires = expires
        self.refresh = refresh
        self.log = log
        self.buttons = list(buttons)
        self._dialogs = {}
        self._next_refresh = {}
        self._parse_failed = False
        # NOTIFY bodies are keyed by the watched resource, which is what a real
        # BLF watcher keys on. Fall back to the button name for entries whose
        # name and number differ.
        self._by_resource = {f"{b.number}@{b.domain}": b for b in self.buttons}

    def get(self, name):
        for button in self.buttons:
            if button.name == name:
                return button
        return None

    def snapshot(self):
        return list(self.buttons)

    def _find_by_resource(self, resource):
        button = self._by_resource.get(resource)
        if button is not None:
            return button
        for candidate in self.buttons:
            if candidate.name == resource:
                return candidate
        return None

    def _subscribe(self, button):
        dialog = self._dialogs.get(button.name)
        if dialog is None:
            dialog = self.client.create_dialog(button.subscribe_uri)
            self._dialogs[button.name] = dialog
            dialog.send_subscribe("dialog", "application/blf+xml", self.expires,
                                  f"blf-{button.name}@{button.domain}")
            self.log(f"[BLF] SUBSCRIBE {button.name} -> {button.subscribe_uri} "
                     f"(expires={self.expires})")
        else:
            dialog.send_refresh(self.expires)
            self.log(f"[BLF] REFRESH {button.name} (expires={self.expires})")
        self._next_refresh[button.name] = time.time() + self.refresh

    def subscribe_all(self):
        for button in self.buttons:
            self._subscribe(button)

    def force_refresh(self):
        for button in self.buttons:
            dialog = self._dialogs.get(button.name)
            if dialog is not None:
                dialog.send_refresh(self.expires)
                self._next_refresh[button.name] = time.time() + self.refresh

    def refresh_due(self):
        now = time.time()
        for button in self.buttons:
            if now >= self._next_refresh.get(button.name, 0.0):
                self._subscribe(button)

    def unsubscribe_all(self):
        for button in self.buttons:
            dialog = self._dialogs.get(button.name)
            if dialog is not None:
                dialog.send_unsubscribe()
                self.log(f"[BLF] UNSUBSCRIBE {button.name}")

    def handle_notify(self, msg):
        """Update button state from a NOTIFY. Returns the buttons that changed."""
        if msg.method != "NOTIFY":
            return []
        if "blf+xml" not in msg.headers.get("Content-Type", ""):
            return []

        try:
            instances = parse_blf_xml(msg.body)
        except BLFParseError as e:
            if not self._parse_failed:
                self._parse_failed = True
                self.log(f"[!] BLF NOTIFY body could not be parsed: {e}")
            return []
        self._parse_failed = False

        changed = []
        for resource, params in instances.items():
            button = self._find_by_resource(resource)
            if button is not None and button.apply(params):
                changed.append(button)

        for button in changed:
            self.log(format_change(button))
        return changed

    def handle_response(self, msg):
        """Route a subscription response.

        Returns True when the response belongs to a BLF subscription, so the
        caller can keep it away from the call state machine. A rejected
        SUBSCRIBE is logged with its status code and the button keeps whatever
        state it had, which for a never-delivered subscription is "unknown".
        """
        if msg.status_code is None:
            return False
        call_id = msg.headers.get("Call-ID", "")
        for name, dialog in self._dialogs.items():
            if dialog.call_id != call_id:
                continue
            code = msg.status_code
            if 200 <= code < 300:
                self.log(f"[BLF] SUBSCRIBE {name} accepted ({code} {msg.reason})")
            else:
                self.log(f"[!] BLF SUBSCRIBE {name} rejected: {code} {msg.reason}")
            return True
        return False
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_blf_manager -v`
Expected: PASS, 24 tests OK

- [ ] **Step 5: Commit**

```bash
git add blf.py tests/test_blf_manager.py
git commit -m "feat: BLFManager subscription lifecycle and NOTIFY state updates"
```

---

### Task 6: Console command parsing and stdin reader

**Files:**
- Create: `console.py`
- Create: `tests/test_console.py`

**Interfaces:**
- Consumes: nothing.
- Produces, importable from `console`:
  - `KNOWN_COMMANDS = ("press", "blf", "help", "hangup", "quit")`
  - `HELP_TEXT = str`
  - `parse_command(line) -> tuple[str, list[str]] | None`, `None` for a blank line
  - `class StdinReader`: `__init__(self, queue, prompt="blf> ")`, `start()`, `stop()`.
    Attribute `_thread` is set by `start()`; `_stop` is a `threading.Event`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_console.py`:

```python
import queue
import unittest

from console import HELP_TEXT, KNOWN_COMMANDS, StdinReader, parse_command


class ParseCommandTest(unittest.TestCase):
    def test_parses_command_with_argument(self):
        self.assertEqual(parse_command("press 101"), ("press", ["101"]))

    def test_parses_command_without_argument(self):
        self.assertEqual(parse_command("quit"), ("quit", []))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(parse_command("   press   101  "), ("press", ["101"]))

    def test_lowercases_the_command(self):
        self.assertEqual(parse_command("PRESS 101"), ("press", ["101"]))

    def test_preserves_argument_case(self):
        self.assertEqual(parse_command("press 101A"), ("press", ["101A"]))

    def test_keeps_extra_arguments(self):
        self.assertEqual(parse_command("blf list extra"), ("blf", ["list", "extra"]))

    def test_blank_line_returns_none(self):
        self.assertIsNone(parse_command(""))
        self.assertIsNone(parse_command("   "))
        self.assertIsNone(parse_command("\n"))

    def test_every_known_command_round_trips(self):
        for name in KNOWN_COMMANDS:
            self.assertEqual(parse_command(name), (name, []))


class HelpTextTest(unittest.TestCase):
    def test_help_mentions_every_command(self):
        for name in KNOWN_COMMANDS:
            self.assertIn(name, HELP_TEXT)


class StdinReaderTest(unittest.TestCase):
    def test_start_creates_a_daemon_thread(self):
        reader = StdinReader(queue.Queue(), prompt="test> ")
        thread = reader.start()
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)

    def test_stop_sets_the_stop_event(self):
        reader = StdinReader(queue.Queue())
        reader.start()
        reader.stop()
        self.assertTrue(reader._stop.is_set())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_console -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'console'`

- [ ] **Step 3: Create console.py**

```python
import threading

KNOWN_COMMANDS = ("press", "blf", "help", "hangup", "quit")

HELP_TEXT = """Commands:
  press <name>    Call the extension on a BLF button
  blf list        Show the current state of every BLF button
  blf refresh     Re-subscribe to all BLF buttons now
  help            Show this help
  hangup          Hang up the active call
  quit            Hang up, unsubscribe and exit"""


def parse_command(line):
    """Split a console line into (command, args). Returns None for a blank line."""
    parts = line.strip().split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


class StdinReader:
    """Reads console lines on a daemon thread and puts them on a queue.

    The main loop is parked in a socket receive, so input has to be read
    elsewhere. A daemon thread means process exit is never blocked on input().
    """

    def __init__(self, queue, prompt="blf> "):
        self.queue = queue
        self.prompt = prompt
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                line = input(self.prompt)
            except EOFError:
                self.queue.put("quit")
                return
            except KeyboardInterrupt:
                continue
            self.queue.put(line)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_console -v`
Expected: PASS, 12 tests OK

- [ ] **Step 5: Commit**

```bash
git add console.py tests/test_console.py
git commit -m "feat: console command parsing and stdin reader thread"
```

---

### Task 7: CallSession state machine

Task 8 rewires `main.py` onto a single loop, which requires the blocking "send INVITE then
wait for the answer" code to become a state machine first. Isolating it here keeps
`main.py`'s rewrite reviewable on its own.

**Files:**
- Create: `call_session.py`
- Create: `tests/test_call_session.py`

**Interfaces:**
- Consumes: `SIPClient.create_dialog` and `SIPDialog.send_invite` / `send_ack` / `send_bye` from Task 1; `RTPMonitor` and `WAVStreamer` from the existing `rtp_sender.py`.
- Produces, importable from `call_session`:
  - `extract_tag(header_value) -> str | None`
  - `send_simple_response(client, msg, status, reason)`
  - `get_local_ip(target_host) -> str` (moved out of `main.py`)
  - `class CallSession`: `__init__(self, client, config, rtp_port, wav_file, call_duration, log=print, media_enabled=True)`. Attributes `state` (one of `CallSession.IDLE`, `CALLING`, `ACTIVE`, `DONE`), `dialog`, `uri`, `to_tag`. Methods `start(uri) -> bool`, `on_message(msg) -> bool`, `tick()`, `hangup()`, `close()`. Properties `active`, `finished`.
  - Class constants `IDLE = "idle"`, `CALLING = "calling"`, `ACTIVE = "active"`, `DONE = "done"`.
  - `media_enabled=False` skips all RTP work, which is how the tests avoid sockets.

- [ ] **Step 1: Write the failing test**

Create `tests/test_call_session.py`:

```python
import unittest

from call_session import CallSession, extract_tag

SERVER_REQ_HEADERS = {
    "Via": "SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK1",
    "From": "<sip:101@10.0.0.1>;tag=abc",
    "To": "<sip:100@10.0.0.10>;tag=def",
    "Call-ID": "server-call-id",
    "CSeq": "2 BYE",
}


class FakeDialog:
    def __init__(self, target_uri):
        self.target_uri = target_uri
        self.invites = []
        self.acks = []
        self.byes = []

    def send_invite(self):
        self.invites.append(True)

    def send_ack(self, to_tag=None):
        self.acks.append(to_tag)

    def send_bye(self, to_tag=None):
        self.byes.append(to_tag)


class FakeClient:
    def __init__(self):
        self.dialogs = []
        self.sent = []
        self.last_sdp = ""
        self.remote_addr = ("10.0.0.1", 5060)

    def create_dialog(self, target_uri):
        dialog = FakeDialog(target_uri)
        self.dialogs.append(dialog)
        return dialog

    def send(self, data):
        self.sent.append(data)

    def parse_sdp(self, sdp):
        return {"media_port": 12000, "media_ip": "10.0.0.5", "codecs": [0]}


class Response:
    def __init__(self, code, reason="OK", headers=None, body=""):
        self.method = None
        self.status_code = code
        self.reason = reason
        self.headers = headers or {}
        self.body = body


class Request:
    def __init__(self, method, headers=None):
        self.method = method
        self.uri = "sip:100@10.0.0.10"
        self.status_code = None
        self.reason = None
        self.headers = dict(SERVER_REQ_HEADERS, **(headers or {}))
        self.body = ""


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


def make_session(**overrides):
    config = {"local_ip": "192.168.0.10", "sip_call_timeout": 30}
    config.update(overrides)
    client = FakeClient()
    log = RecordingLog()
    session = CallSession(client, config, 10000, "test.wav", 5,
                          log=log, media_enabled=False)
    return client, session, log


def ok_response():
    return Response(200, "OK", headers={"To": "<sip:101@x>;tag=remoteTAG"})


class ExtractTagTest(unittest.TestCase):
    def test_reads_tag(self):
        self.assertEqual(extract_tag("<sip:101@x>;tag=abc123"), "abc123")

    def test_tag_before_other_params(self):
        self.assertEqual(extract_tag("<sip:101@x>;tag=abc123;extra=1"), "abc123")

    def test_returns_none_without_tag(self):
        self.assertIsNone(extract_tag("<sip:101@x>"))

    def test_returns_none_for_empty(self):
        self.assertIsNone(extract_tag(""))


class StartTest(unittest.TestCase):
    def test_start_sends_invite_and_becomes_calling(self):
        client, session, log = make_session()
        self.assertTrue(session.start("sip:101@10.0.0.1:5060"))
        self.assertEqual(session.state, CallSession.CALLING)
        self.assertTrue(session.active)
        self.assertEqual(len(client.dialogs[0].invites), 1)

    def test_start_refuses_while_a_call_is_active(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        self.assertFalse(session.start("sip:102@10.0.0.1:5060"))
        self.assertEqual(len(client.dialogs), 1)

    def test_start_allowed_again_after_the_call_ends(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(Request("BYE"))
        self.assertTrue(session.start("sip:102@10.0.0.1:5060"))


class ResponseHandlingTest(unittest.TestCase):
    def test_provisional_responses_are_ignored(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(Response(100, "Trying"))
        session.on_message(Response(180, "Ringing"))
        self.assertEqual(session.state, CallSession.CALLING)
        self.assertEqual(client.dialogs[0].acks, [])

    def test_200_answers_with_ack_and_becomes_active(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        self.assertEqual(session.state, CallSession.ACTIVE)
        self.assertEqual(client.dialogs[0].acks, ["remoteTAG"])
        self.assertEqual(session.to_tag, "remoteTAG")

    def test_rejection_finishes_the_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(Response(404, "Not Found"))
        self.assertEqual(session.state, CallSession.DONE)
        self.assertTrue(session.finished)
        self.assertIn("Call failed: 404", log)

    def test_redirection_finishes_the_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(Response(302, "Moved"))
        self.assertIn("not following", log)
        self.assertEqual(session.state, CallSession.DONE)

    def test_response_while_idle_is_ignored(self):
        _, session, log = make_session()
        session.on_message(Response(200, "OK"))
        self.assertEqual(session.state, CallSession.IDLE)

    def test_bye_is_answered_and_finishes(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.on_message(Request("BYE"))
        self.assertEqual(session.state, CallSession.DONE)
        self.assertTrue(any(b"200 OK" in data for data in client.sent))

    def test_reinvite_while_active_is_answered(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        client.sent.clear()
        session.on_message(Request("INVITE"))
        self.assertTrue(any(b"200 OK" in data for data in client.sent))
        self.assertEqual(session.state, CallSession.ACTIVE)

    def test_invite_while_idle_is_ignored(self):
        client, session, log = make_session()
        session.on_message(Request("INVITE"))
        self.assertEqual(session.state, CallSession.IDLE)
        self.assertEqual(client.sent, [])


class HangupAndTimeoutTest(unittest.TestCase):
    def test_hangup_sends_bye_when_active(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.hangup()
        self.assertEqual(client.dialogs[0].byes, ["remoteTAG"])
        self.assertEqual(session.state, CallSession.DONE)

    def test_hangup_while_calling_sends_no_bye(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.hangup()
        self.assertEqual(client.dialogs[0].byes, [])
        self.assertEqual(session.state, CallSession.DONE)

    def test_hangup_when_idle_is_a_no_op(self):
        client, session, log = make_session()
        session.hangup()
        self.assertEqual(session.state, CallSession.IDLE)
        self.assertEqual(client.dialogs, [])

    def test_tick_finishes_a_timed_out_call(self):
        _, session, log = make_session(sip_call_timeout=0)
        session.start("sip:101@10.0.0.1:5060")
        session.tick()
        self.assertEqual(session.state, CallSession.DONE)
        self.assertIn("Timeout", log)

    def test_tick_does_nothing_while_active(self):
        _, session, log = make_session(sip_call_timeout=0)
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.tick()
        self.assertEqual(session.state, CallSession.ACTIVE)

    def test_close_hangs_up_an_active_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.close()
        self.assertEqual(client.dialogs[0].byes, ["remoteTAG"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_call_session -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'call_session'`

- [ ] **Step 3: Create call_session.py**

```python
import socket
import threading
import time

from rtp_sender import RTPMonitor, WAVStreamer
from sip_client import SIPMessage


def extract_tag(header_value):
    """Pull the To-tag out of a SIP header value, or None if there is none."""
    if header_value and ";tag=" in header_value:
        return header_value.split(";tag=")[1].split(";")[0]
    return None


def send_simple_response(client, msg, status, reason):
    """Answer a server-initiated request (BYE, re-INVITE) with a bare response."""
    rsp = SIPMessage(status_code=status, reason=reason)
    for name in ("Via", "From", "To", "Call-ID", "CSeq"):
        rsp.add_header(name, msg.headers.get(name, ""))
    client.send(rsp.build().encode())


def get_local_ip(target_host):
    """Determine the local IP address that would be used to reach target_host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class CallSession:
    """A single outbound call, driven one event at a time by the main loop.

    start() sends the INVITE, on_message() advances the state machine, tick()
    enforces the answer timeout. Nothing here blocks.
    """

    IDLE = "idle"
    CALLING = "calling"
    ACTIVE = "active"
    DONE = "done"

    def __init__(self, client, config, rtp_port, wav_file, call_duration,
                 log=print, media_enabled=True):
        self.client = client
        self.config = config
        self.rtp_port = rtp_port
        self.wav_file = wav_file
        self.call_duration = call_duration
        self.log = log
        self.media_enabled = media_enabled

        self.state = self.IDLE
        self.dialog = None
        self.uri = None
        self.to_tag = None
        self.monitor = None
        self.rtp_sock = None
        self._deadline = None

    @property
    def active(self):
        return self.state in (self.CALLING, self.ACTIVE)

    @property
    def finished(self):
        return self.state == self.DONE

    def start(self, uri):
        if self.active:
            self.log(f"[!] A call is already {self.state}, ignoring request to {uri}")
            return False
        self.uri = uri
        self.to_tag = None
        self.monitor = None
        self.rtp_sock = None
        self.dialog = self.client.create_dialog(uri)
        self.dialog.send_invite()
        self.log(f"[-] Sending INVITE to {uri}")
        self.state = self.CALLING
        self._deadline = time.time() + int(self.config.get("sip_call_timeout", 30))
        return True

    def on_message(self, msg):
        if msg.method == "BYE":
            self.log("[*] Received BYE, sending 200 OK")
            send_simple_response(self.client, msg, 200, "OK")
            self._finish()
            return True
        if msg.method == "INVITE":
            if self.state == self.ACTIVE:
                self.log("[SIP] re-INVITE answered 200 OK")
                send_simple_response(self.client, msg, 200, "OK")
                return True
            self.log("[SIP] Rx INVITE outside a call, ignored")
            return False
        if msg.status_code is None or self.state != self.CALLING:
            return False
        return self._on_response(msg)

    def _on_response(self, msg):
        code = msg.status_code
        if 100 <= code < 200:
            return False
        if code == 200:
            self.to_tag = extract_tag(msg.headers.get("To", ""))
            self.client.last_sdp = msg.body
            self.dialog.send_ack(self.to_tag)
            self.log("[+] Call connected, ACK sent")
            if self.media_enabled:
                self._start_media()
            self.state = self.ACTIVE
            return True
        if 300 <= code < 400:
            self.log(f"[!] Redirection {code}, not following")
            self._finish()
            return True
        self.log(f"[!] Call failed: {code} {msg.reason}")
        self._finish()
        return True

    def _start_media(self):
        sdp_info = self.client.parse_sdp(self.client.last_sdp)
        if sdp_info["media_port"] and sdp_info["media_ip"]:
            remote = (sdp_info["media_ip"], sdp_info["media_port"])
            self.log(f"[RTP] Remote media: {sdp_info['media_ip']}:{sdp_info['media_port']}")
        else:
            remote = self.client.remote_addr
            self.log(f"[RTP] No media in SDP, assuming SIP addr: {remote}")

        local_bind_ip = self.config.get("local_ip", "0.0.0.0")
        if local_bind_ip == "0.0.0.0":
            local_bind_ip = get_local_ip(self.client.remote_addr[0])

        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtp_sock.bind((local_bind_ip, self.rtp_port))
        self.log(f"[RTP] Local: {local_bind_ip}:{self.rtp_port} "
                 f"-> Remote: {remote[0]}:{remote[1]}")

        self.monitor = RTPMonitor(self.rtp_sock, self.config)
        self.monitor.start()

        streamer = WAVStreamer(self.wav_file, self.config)
        threading.Thread(target=streamer.send,
                         args=(self.rtp_sock, remote, self.call_duration),
                         daemon=True).start()

    def tick(self):
        # `>=`, not `>`: with sip_call_timeout 0 the deadline equals the start
        # time, and a strict comparison would depend on clock granularity.
        if self.state == self.CALLING and time.time() >= self._deadline:
            self.log("[!] Timeout waiting for SIP response")
            self._finish()

    def hangup(self):
        if self.state == self.IDLE:
            return
        if self.state == self.ACTIVE:
            self.log("[*] Sending BYE")
            self.dialog.send_bye(self.to_tag)
        self._finish()

    def _finish(self):
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        if self.rtp_sock is not None:
            self.rtp_sock.close()
            self.rtp_sock = None
        self.state = self.DONE

    def close(self):
        if self.active:
            self.hangup()
        elif self.rtp_sock is not None:
            self.rtp_sock.close()
            self.rtp_sock = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_call_session -v`
Expected: PASS, 22 tests OK

- [ ] **Step 5: Commit**

```bash
git add call_session.py tests/test_call_session.py
git commit -m "refactor: extract non-blocking CallSession state machine"
```

---

### Task 8: Rewire main.py onto one loop with BLF and the console

**Files:**
- Modify: `call_session.py` (add `parse_sip_uri`)
- Modify: `main.py` (full rewrite)
- Modify: `config.json`
- Create: `tests/test_main.py`

**Interfaces:**
- Consumes: `SIPClient` from Task 1; `BLFManager`, `load_buttons`, `resolve_timing` from Tasks 4 and 5; `parse_command`, `StdinReader`, `HELP_TEXT` from Task 6; `CallSession`, `get_local_ip`, `parse_sip_uri` from Task 7.
- Produces from `main.py`:
  - `load_config(path) -> dict` (unchanged behaviour)
  - `build_blf_manager(config, client, log=print) -> BLFManager | None`, `None` when the `blf` section is absent, not enabled, or has no usable buttons
  - `render_blf_table(manager) -> str`
  - `class CommandContext(blf, session, client, target_host, target_port, wav_file, log=print)`
  - `dispatch_command(parsed, ctx) -> bool`, `False` means exit the loop
  - `main()`
- Produces from `call_session.py`:
  - `parse_sip_uri(uri) -> dict` with keys `user`, `host`, `port`, `transport` (moved verbatim out of `main.py`)
- Removed from `main.py`: `parse_sip_uri`, `get_local_ip`, `handle_call`, `send_simple_response`.
- `main.py` gains flags `--blf` and `--no-call`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_main.py`:

```python
import unittest

from blf import BLFButton
from console import parse_command
from main import (CommandContext, build_blf_manager, dispatch_command,
                  render_blf_table)


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


class FakeSession:
    def __init__(self, start_result=True):
        self.started = []
        self.hung_up = 0
        self.start_result = start_result
        self.state = "idle"

    def start(self, uri):
        self.started.append(uri)
        return self.start_result

    def hangup(self):
        self.hung_up += 1


class FakeClient:
    def create_dialog(self, target_uri):
        raise AssertionError("dispatch must not create dialogs directly")


class FakeManager:
    def __init__(self, buttons):
        self.buttons = buttons
        self.forced = 0

    def snapshot(self):
        return list(self.buttons)

    def get(self, name):
        for button in self.buttons:
            if button.name == name:
                return button
        return None

    def force_refresh(self):
        self.forced += 1


def make_context(buttons=None, log=None):
    log = log or RecordingLog()
    if buttons is None:
        buttons = [BLFButton("101", "101", "default", "Operator 1")]
    return CommandContext(blf=FakeManager(buttons),
                          session=FakeSession(),
                          client=FakeClient(),
                          target_host="192.168.0.89",
                          target_port=5060,
                          wav_file="test.wav",
                          log=log), log


class BuildBlfManagerTest(unittest.TestCase):
    def test_none_without_blf_section(self):
        self.assertIsNone(build_blf_manager({}, FakeClient()))

    def test_none_when_disabled(self):
        self.assertIsNone(
            build_blf_manager({"blf": {"enabled": False,
                                       "buttons": [{"name": "101"}]}},
                              FakeClient()))

    def test_none_when_enabled_but_no_buttons(self):
        self.assertIsNone(
            build_blf_manager({"blf": {"enabled": True}}, FakeClient()))

    def test_builds_manager_when_enabled(self):
        config = {"blf": {"enabled": True, "buttons": [{"name": "101"}]}}
        manager = build_blf_manager(config, FakeClient(), log=RecordingLog())
        self.assertIsNotNone(manager)
        self.assertEqual([b.name for b in manager.snapshot()], ["101"])


class RenderTableTest(unittest.TestCase):
    def test_includes_name_label_state_and_caller(self):
        button = BLFButton("101", "101", "default", "Operator 1")
        button.apply({"rpid": "ringing", "caller_id_number": "5551234"})
        table = render_blf_table(FakeManager([button]))
        self.assertIn("101", table)
        self.assertIn("Operator 1", table)
        self.assertIn("ringing", table)
        self.assertIn("5551234", table)

    def test_handles_no_buttons(self):
        self.assertIn("No BLF buttons", render_blf_table(FakeManager([])))


class DispatchTest(unittest.TestCase):
    def test_press_dials_the_button_number(self):
        ctx, log = make_context()
        dispatch_command(parse_command("press 101"), ctx)
        self.assertEqual(ctx.session.started, ["sip:101@192.168.0.89:5060"])

    def test_press_unknown_name_lists_valid_names(self):
        ctx, log = make_context()
        dispatch_command(parse_command("press 777"), ctx)
        self.assertEqual(ctx.session.started, [])
        self.assertIn("Unknown BLF button", log)
        self.assertIn("101", log)

    def test_press_without_blf_is_refused(self):
        ctx, log = make_context()
        ctx.blf = None
        dispatch_command(parse_command("press 101"), ctx)
        self.assertEqual(ctx.session.started, [])
        self.assertIn("BLF is not active", log)

    def test_press_without_argument_shows_usage(self):
        ctx, log = make_context()
        dispatch_command(parse_command("press"), ctx)
        self.assertEqual(ctx.session.started, [])
        self.assertIn("usage: press", log)

    def test_press_reports_a_refused_call(self):
        ctx, log = make_context()
        ctx.session = FakeSession(start_result=False)
        dispatch_command(parse_command("press 101"), ctx)
        self.assertIn("already", log)

    def test_blf_list_prints_the_table(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf list"), ctx)
        self.assertIn("Operator 1", log)

    def test_blf_list_without_blf_is_refused(self):
        ctx, log = make_context()
        ctx.blf = None
        dispatch_command(parse_command("blf list"), ctx)
        self.assertIn("BLF is not active", log)

    def test_blf_refresh_forces_refresh(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf refresh"), ctx)
        self.assertEqual(ctx.blf.forced, 1)

    def test_blf_bad_subcommand_shows_usage(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf wat"), ctx)
        self.assertIn("usage: blf", log)

    def test_help_prints_help_text(self):
        ctx, log = make_context()
        dispatch_command(parse_command("help"), ctx)
        self.assertIn("press <name>", log)

    def test_hangup_calls_the_session(self):
        ctx, log = make_context()
        dispatch_command(parse_command("hangup"), ctx)
        self.assertEqual(ctx.session.hung_up, 1)

    def test_quit_signals_exit(self):
        ctx, log = make_context()
        self.assertFalse(dispatch_command(parse_command("quit"), ctx))

    def test_unknown_command_is_reported(self):
        ctx, log = make_context()
        self.assertTrue(dispatch_command(parse_command("frobnicate"), ctx))
        self.assertIn("Unknown command", log)

    def test_blank_line_is_ignored(self):
        ctx, log = make_context()
        self.assertTrue(dispatch_command(None, ctx))
        self.assertEqual(log.lines, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_main -v`
Expected: FAIL with `ImportError: cannot import name 'CommandContext' from 'main'`

- [ ] **Step 3: Move parse_sip_uri into call_session.py**

Add to `call_session.py`, above `class CallSession`:

```python
def parse_sip_uri(uri):
    """Parse a SIP URI like sip:101@192.168.0.89:5060;transport=udp."""
    uri = uri.strip()
    if uri.startswith("sip:"):
        uri = uri[4:]

    transport = None
    if ";transport=" in uri:
        uri, params = uri.split(";", 1)
        for param in params.split(";"):
            if param.startswith("transport="):
                transport = param.split("=")[1].lower()

    if "@" in uri:
        user, hostport = uri.split("@", 1)
    else:
        user, hostport = uri, ""

    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        try:
            port = int(port)
        except ValueError:
            port = 5060
    else:
        host = hostport
        port = 5060

    return {"user": user, "host": host, "port": port, "transport": transport}
```

- [ ] **Step 4: Rewrite main.py**

Replace the whole file with:

```python
import argparse
import json
import queue

from blf import BLFManager, load_buttons, resolve_timing
from call_session import CallSession, get_local_ip, parse_sip_uri
from console import HELP_TEXT, StdinReader, parse_command
from sip_client import SIPClient

DEFAULT_CONFIG = "config.json"
PROMPT = "blf> "


def load_config(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[!] Config file '{path}' not found, using defaults")
        return {}
    except json.JSONDecodeError as e:
        print(f"[!] Error parsing config '{path}': {e}")
        return {}


def build_blf_manager(config, client, log=print):
    section = config.get("blf") or {}
    if not section.get("enabled"):
        return None
    buttons = load_buttons(config, log=log)
    if not buttons:
        log("[!] BLF is enabled but no buttons are configured")
        return None
    expires, refresh = resolve_timing(config, log=log)
    return BLFManager(client, buttons, expires=expires, refresh=refresh, log=log)


def render_blf_table(manager):
    buttons = manager.snapshot()
    if not buttons:
        return "No BLF buttons configured."
    header = f"{'NAME':<10} {'LABEL':<16} {'STATE':<9} CALLER"
    lines = [header, "-" * len(header)]
    for button in buttons:
        if button.caller_number or button.caller_name:
            caller = f"{button.caller_name or ''} <{button.caller_number or ''}>"
        else:
            caller = "-"
        lines.append(f"{button.name:<10} {button.label:<16} {button.state:<9} {caller}")
    return "\n".join(lines)


class CommandContext:
    def __init__(self, blf, session, client, target_host, target_port,
                 wav_file, log=print):
        self.blf = blf
        self.session = session
        self.client = client
        self.target_host = target_host
        self.target_port = target_port
        self.wav_file = wav_file
        self.log = log


def dispatch_command(parsed, ctx):
    """Run one console command. Returns False when the caller should exit."""
    if parsed is None:
        return True
    name, args = parsed

    if name == "quit":
        return False

    if name == "help":
        ctx.log(HELP_TEXT)
        return True

    if name == "hangup":
        ctx.session.hangup()
        return True

    if name == "press":
        if not args:
            ctx.log("usage: press <name>")
            return True
        if ctx.blf is None:
            ctx.log("[!] BLF is not active, cannot press a button")
            return True
        button = ctx.blf.get(args[0])
        if button is None:
            names = ", ".join(b.name for b in ctx.blf.snapshot())
            ctx.log(f"[!] Unknown BLF button '{args[0]}'. Configured: {names}")
            return True
        uri = f"sip:{button.number}@{ctx.target_host}:{ctx.target_port}"
        ctx.log(f"[*] Pressing {button.name} ({button.label}) -> {uri}")
        if not ctx.session.start(uri):
            ctx.log(f"[!] Cannot place call, a call is already {ctx.session.state}")
        return True

    if name == "blf":
        if ctx.blf is None:
            ctx.log("[!] BLF is not active")
            return True
        sub = args[0] if args else ""
        if sub == "list":
            ctx.log(render_blf_table(ctx.blf))
        elif sub == "refresh":
            ctx.blf.force_refresh()
            ctx.log("[BLF] Re-subscribed all buttons")
        else:
            ctx.log("usage: blf list | blf refresh")
        return True

    ctx.log(f"[!] Unknown command '{name}'. Try 'help'.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="SIP client for sending INVITE and streaming WAV over RTP")
    parser.add_argument("sip_uri", nargs="?",
                        help="SIP URI (e.g. sip:101@192.168.0.89:5060;transport=udp)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"Path to config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--blf", action="store_true",
                        help="Subscribe to the BLF buttons in the config and enable the console")
    parser.add_argument("--no-call", action="store_true",
                        help="Do not place the startup call, only watch BLF")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.sip_uri:
        target = parse_sip_uri(args.sip_uri)
        protocol = target["transport"] or config.get("transport", "udp").lower()
    else:
        target = {
            "user": config.get("target_user", "101"),
            "host": config.get("target_host", "127.0.0.1"),
            "port": config.get("target_port", 5060),
        }
        protocol = config.get("transport", "udp").lower()

    uri = f"sip:{target['user']}@{target['host']}:{target['port']}"
    print(f"[+] Target: {uri} (protocol: {protocol})")

    wav_file = config.get("wav_file", "test.wav")
    call_duration = config.get("call_duration", 30)
    rtp_port = config.get("rtp_port", 10000)

    local_ip = config.get("local_ip", "0.0.0.0")
    if local_ip == "0.0.0.0":
        local_ip = get_local_ip(target["host"]) if target["host"] else "127.0.0.1"

    client = SIPClient(config)
    client.local_ip = local_ip
    client.local_port = config.get("local_port", 5080)
    client.transport = protocol
    client.open((target["host"], target["port"]))

    blf_mode = args.blf or bool((config.get("blf") or {}).get("enabled"))
    blf = None
    if blf_mode:
        forced = dict(config)
        forced["blf"] = dict(config.get("blf") or {}, enabled=True)
        blf = build_blf_manager(forced, client)
        if blf is not None:
            blf.subscribe_all()
        else:
            print("[!] BLF mode requested but no buttons are available")

    session = CallSession(client, config, rtp_port, wav_file, call_duration)

    if args.no_call and not blf_mode:
        print("[!] --no-call without BLF, placing the call anyway")
    if not args.no_call or not blf_mode:
        session.start(uri)

    commands = queue.Queue()
    reader = StdinReader(commands, prompt=PROMPT)
    reader.start()

    ctx = CommandContext(blf, session, client, target["host"], target["port"],
                         wav_file)

    print("[*] Console ready. Type 'help' for commands, Ctrl+C or 'quit' to exit.")

    running = True
    try:
        while running:
            while True:
                try:
                    line = commands.get_nowait()
                except queue.Empty:
                    break
                if not dispatch_command(parse_command(line), ctx):
                    running = False
                    break
            if not running:
                break

            if blf is not None:
                blf.refresh_due()
            session.tick()

            msg = client.receive(timeout=0.5)
            if msg is None:
                continue
            if msg.method == "NOTIFY":
                if blf is not None:
                    blf.handle_notify(msg)
                continue
            if blf is not None and blf.handle_response(msg):
                continue
            session.on_message(msg)
            if session.finished and not blf_mode:
                break
    except KeyboardInterrupt:
        print("\n[*] Interrupted")
    finally:
        reader.stop()
        session.close()
        if blf is not None:
            blf.unsubscribe_all()
        client.close()
        print("[+] Exited")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Add the blf section to config.json**

Replace `config.json` with:

```json
{
    "local_ip": "0.0.0.0",
    "local_port": 5080,
    "transport": "udp",
    "target_user": "101",
    "target_host": "192.168.0.89",
    "target_port": 5060,
    "wav_file": "piano2.wav",
    "rtp_port": 10000,
    "codec": "PCMU",
    "sample_rate": 8000,
    "call_duration": 30,
    "user_agent": "PythonSIP/1.0",
    "from_user": "100",
    "display_name": "SIP Client",
    "sip_call_timeout": 30,
    "blf": {
        "enabled": false,
        "domain": "default",
        "expires": 3600,
        "refresh": 1800,
        "buttons": [
            {"name": "101", "number": "101", "label": "Operator 1"},
            {"name": "102", "number": "102", "label": "Manager"}
        ]
    }
}
```

`blf.enabled` is `false` so the default no-flag run is unchanged. `--blf` turns it on
regardless. Every pre-existing key keeps its value.

- [ ] **Step 6: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_main -v`
Expected: PASS, 20 tests OK

- [ ] **Step 7: Run the full suite**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: PASS, all tests OK

- [ ] **Step 8: Verify the CLI still wires up**

Run: `python3 main.py --help`
Expected: usage text listing `sip_uri`, `--config`, `--blf`, `--no-call`

Run: `python3 -c "import main, call_session, blf, console, sip_client"`
Expected: no output, exit code 0

- [ ] **Step 9: Commit**

```bash
git add main.py call_session.py config.json tests/test_main.py
git commit -m "feat: single dispatch loop with BLF watching and console commands"
```

---

### Task 9: Teach mock_server.py the BLF flow

Lets the whole subscribe to notify to status change to press path be exercised locally
without a real FreeSWITCH.

**Files:**
- Modify: `mock_server.py`
- Create: `tests/test_mock_server.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. Independent of the client code.
- Produces, importable from `mock_server`:
  - `make_notify_body(instances) -> str` where `instances` is a list of
    `(name, rpid, caller_id_number)` tuples. Pure, so it can be tested.
  - `make_notify(call_id, from_tag, to_tag, cseq, event_id, body) -> str`
  - `BLF_SCRIPT = ((0.0, "idle", None), (3.0, "ringing", "5551234"), (8.0, "idle", None))`
    as `(delay_seconds, rpid, caller_number)` steps replayed after each SUBSCRIBE.
  - The main loop answers `SUBSCRIBE` with 200 OK and starts a daemon thread per
    subscription that walks `BLF_SCRIPT`, sending a NOTIFY at each step.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mock_server.py`:

```python
import unittest
from xml.etree import ElementTree

from mock_server import BLF_SCRIPT, make_notify, make_notify_body


class MakeNotifyBodyTest(unittest.TestCase):
    def test_body_parses_and_carries_rpid(self):
        body = make_notify_body([("101@default", "ringing", "5551234")])
        root = ElementTree.fromstring(body)
        instances = [n for n in root.iter() if n.tag.endswith("instance")]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].get("name"), "101@default")
        params = {p.get("name"): p.get("value")
                  for p in instances[0].iter() if p.tag.endswith("param")}
        self.assertEqual(params["rpid"], "ringing")
        self.assertEqual(params["caller_id_number"], "5551234")

    def test_body_with_no_instances_is_still_valid_xml(self):
        root = ElementTree.fromstring(make_notify_body([]))
        self.assertTrue(root.tag.endswith("event"))

    def test_body_escapes_xml_in_values(self):
        body = make_notify_body([("101@default", "idle", '"><evil>')])
        # The raw body must carry escaped markup, so a caller_id_number cannot
        # break out of its attribute and inject elements.
        self.assertNotIn("<evil>", body)
        self.assertIn("&lt;evil&gt;", body)
        # Round-tripping restores the original characters as inert text.
        params = {p.get("name"): p.get("value")
                  for p in ElementTree.fromstring(body).iter()
                  if p.tag.endswith("param")}
        self.assertEqual(params["caller_id_number"], '"><evil>')


class MakeNotifyTest(unittest.TestCase):
    def test_notify_carries_dialog_and_subscription_headers(self):
        raw = make_notify("call-1", "ftag", "ttag", 1, "blf-101@default",
                          make_notify_body([("101@default", "idle", None)]))
        self.assertTrue(raw.startswith("NOTIFY sip:101@default SIP/2.0"))
        self.assertIn("Event: dialog", raw)
        self.assertIn("Subscription-State: active;expires=3600", raw)
        self.assertIn("Event-ID: blf-101@default", raw)
        self.assertIn("Call-ID: call-1", raw)
        self.assertIn(";tag=ftag", raw)
        self.assertIn(";tag=ttag", raw)
        self.assertIn("CSeq: 1 NOTIFY", raw)
        self.assertIn("Content-Type: application/blf+xml", raw)

    def test_notify_declares_the_body_length(self):
        body = make_notify_body([("101@default", "idle", None)])
        raw = make_notify("c", "f", "t", 1, "e", body)
        self.assertIn(f"Content-Length: {len(body.encode())}", raw)


class BlfScriptTest(unittest.TestCase):
    def test_script_starts_idle_and_ends_idle(self):
        self.assertEqual(BLF_SCRIPT[0][1], "idle")
        self.assertEqual(BLF_SCRIPT[-1][1], "idle")

    def test_script_delays_increase(self):
        delays = [step[0] for step in BLF_SCRIPT]
        self.assertEqual(delays, sorted(delays))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_mock_server -v`
Expected: FAIL with `ImportError: cannot import name 'BLF_SCRIPT' from 'mock_server'`

- [ ] **Step 3: Add the BLF helpers to mock_server.py**

Add `import threading`, `from xml.etree import ElementTree`, and
`from xml.sax.saxutils import quoteattr` to the imports at the top of `mock_server.py`,
then add above `def main():`:

```python
# After each SUBSCRIBE, walk this script: (delay_seconds, rpid, caller_number).
BLF_SCRIPT = (
    (0.0, "idle", None),
    (3.0, "ringing", "5551234"),
    (8.0, "idle", None),
)


def make_notify_body(instances):
    """Build a BLF NOTIFY body from (name, rpid, caller_number) tuples."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<event name="dialog" xmlns="urn:freedeswitch:blf">']
    for name, rpid, caller_number in instances:
        parts.append(f"  <instance name={quoteattr(name)}>")
        parts.append("    <params>")
        parts.append(f'      <param name="rpid" value={quoteattr(rpid)}/>')
        if caller_number:
            parts.append("      <param name=\"caller_id_number\" "
                         f"value={quoteattr(caller_number)}/>")
        parts.append("    </params>")
        parts.append("  </instance>")
    parts.append("</event>")
    return "\r\n".join(parts)


def _first_resource(body):
    """The NOTIFY Request-URI is the watched resource, taken from the body."""
    for node in ElementTree.fromstring(body).iter():
        if node.tag.endswith("instance") and node.get("name"):
            return node.get("name")
    return ""


def make_notify(call_id, from_tag, to_tag, cseq, event_id, body):
    """Build a complete NOTIFY request reusing an existing subscription dialog."""
    lines = [
        f"NOTIFY sip:{_first_resource(body)} SIP/2.0",
        f"Via: SIP/2.0/UDP 127.0.0.1:5061;branch=z9hG4bKmock{cseq};rport",
        "Max-Forwards: 70",
        f"From: <sip:100@127.0.0.1>;tag={from_tag}",
        f"To: <sip:100@127.0.0.1>;tag={to_tag}",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} NOTIFY",
        "Contact: <sip:100@127.0.0.1:5061>",
        "Event: dialog",
        f"Event-ID: {event_id}",
        "Subscription-State: active;expires=3600",
        "Content-Type: application/blf+xml",
        f"Content-Length: {len(body.encode())}",
        "",
        body,
    ]
    return "\r\n".join(lines)
```

- [ ] **Step 4: Answer SUBSCRIBE in the main loop**

Inside `main()`, in the `while True:` loop, add a branch for SUBSCRIBE. Place it
immediately after the `elif first.startswith("ACK"):` branch:

```python
            elif first.startswith("SUBSCRIBE"):
                lines = msg.split("\r\n")
                headers = {}
                for line in lines[1:]:
                    if not line:
                        break
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        headers[key] = value
                sock.sendto(make_response(lines, 200, "OK").encode(), addr)
                print("[mock] Got SUBSCRIBE, answered 200 OK")

                # The Request-URI names the WATCHED resource (sip:101@default).
                # The To header names the watcher. NOTIFY bodies must describe the
                # watched resource, so the request line is the only correct source.
                resource = first.split(" ")[1].replace("sip:", "").split(";")[0]
                from_tag = headers.get("From", "").split("tag=")[-1].split(";")[0]
                to_tag = "mockTAG"
                event_id = headers.get("Event-ID", f"blf-{resource}")

                def blf_thread(call_id=headers.get("Call-ID", ""),
                               from_tag=from_tag,
                               to_tag=to_tag,
                               event_id=event_id,
                               resource=resource):
                    for index, (delay, rpid, caller) in enumerate(BLF_SCRIPT):
                        time.sleep(delay)
                        body = make_notify_body([(resource, rpid, caller)])
                        notify = make_notify(call_id, from_tag, to_tag,
                                             index + 1, event_id, body)
                        sock.sendto(notify.encode(), addr)
                        print(f"[mock] NOTIFY {resource} rpid={rpid}")

                threading.Thread(target=blf_thread, daemon=True).start()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_mock_server -v`
Expected: PASS, 6 tests OK

- [ ] **Step 6: Run the full suite**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: PASS, all tests OK

- [ ] **Step 7: Commit**

```bash
git add mock_server.py tests/test_mock_server.py
git commit -m "feat: mock server answers SUBSCRIBE and replays a BLF state script"
```

---

### Task 10: FreeSWITCH-side configuration snippets

The client cannot work alone. These files are the server side of the feature.

**Files:**
- Create: `freeswitch/README.md`
- Create: `freeswitch/modules.conf.xml.snippet`
- Create: `freeswitch/presence/blf-presence.xml`
- Create: `freeswitch/dialplan/presence-actions.xml`

**Interfaces:**
- Consumes: the `blf.buttons` list from `config.json`. The `number` and `domain` of
  each button become the `id` of a presence profile entry.
- Produces: no code. Documentation and XML snippets.
- Does NOT touch `modules.conf.xml`, `conf/`, or any live FreeSWITCH path. These are
  files to copy by hand.

- [ ] **Step 1: Create freeswitch/modules.conf.xml.snippet**

```xml
<!-- Merge into conf/autoload_configs/modules.conf.xml -->
<module name="mod_presence" />
<module name="mod_presence_xml" />
```

- [ ] **Step 2: Create freeswitch/presence/blf-presence.xml**

Replace the numbers below with the ones from your own `blf.buttons`.

```xml
<!-- Install as conf/presence/blf.xml, then: fs_cli -x "reloadxml" -->
<presence-group name="blf-watchers">
  <presence id="101@default">
    <rpid id="101@default" value="idle" status="active"/>
    <rpid id="101@default" value="ringing" status="active"/>
    <rpid id="101@default" value="busy" status="active"/>
    <rpid id="101@default" value="held" status="active"/>
  </presence>
  <presence id="102@default">
    <rpid id="102@default" value="idle" status="active"/>
    <rpid id="102@default" value="ringing" status="active"/>
    <rpid id="102@default" value="busy" status="active"/>
    <rpid id="102@default" value="held" status="active"/>
  </presence>
</presence-group>
```

- [ ] **Step 3: Create freeswitch/dialplan/presence-actions.xml**

Without this the watched extension stays `idle` forever: nothing tells FreeSWITCH that a
call is up. The `uuid_setvar` form below is used because its argument order is
documented and certain.

```xml
<!--
Merge these actions into the extension that each watched user dials.
The variable ${uuid} is the current channel.

The <presence> application below is an ALTERNATIVE to the uuid_setvar pair.
Its argument order is NOT verified by this project. Before using it, check
your own build:

    fs_cli
    show application presence

Then keep whichever form that output matches, and delete the other.
-->
<extension name="blf-presence">
  <condition field="${sip_gateway_name}" expression="^$">
    <!-- Outbound leg of a call to a watched user: this channel is the one
         a BLF watcher wants to see as busy. -->
    <action application="set" data="presence_id=${dialed_user}@${domain}"/>
    <action application="uuid_setvar" data="${dialed_user}@${domain} presence_id"/>
    <action application="log" data="INFO BLF presence: set idle on ${dialed_user}@${domain}"/>
  </condition>
</extension>

<!-- Ring state, on the alerting channel. -->
<action application="uuid_setvar" data="${presence_id} rpid ringing"/>

<!-- Handed-up state, after answer. -->
<action application="uuid_setvar" data="${presence_id} rpid busy"/>

<!-- Back to idle, in the hangup handler or after the bridge ends. -->
<action application="uuid_setvar" data="${presence_id} rpid idle"/>
```

- [ ] **Step 4: Create freeswitch/README.md**

```markdown
# FreeSWITCH side of BLF

The client (`blf.py`) is a BLF watcher: it SUBSCRIBEs to each button's extension
and reads the `rpid` state that FreeSWITCH reports in NOTIFY bodies. It also
places calls directly with INVITE when a button is pressed. It does not
register, so no `blf` / `blf_reg_*` directory parameters are needed.

Nothing here is applied automatically. Copy the files by hand.

## 1. Load the modules

Merge `modules.conf.xml.snippet` into `conf/autoload_configs/modules.conf.xml`,
then `fs_cli -x "reloadxml"`. Confirm with `fs_cli -x "module_exists mod_presence"`
returning `true`.

## 2. Declare the watched users

Install `presence/blf.xml` as `conf/presence/blf.xml`. The `id` attributes must
match the `number@domain` of the buttons in the client's `config.json`. Then
`fs_cli -x "reloadxml"`.

## 3. Move the state during a call

This is the step that is easy to miss. A subscription with no state change
never leaves `idle`.

Take the `uuid_setvar` actions from `dialplan/presence-actions.xml` and merge
them into the extension that the watched users dial. The pattern is:

- when a call to a watched user starts, set that user's `presence_id`
- while it alerts, `rpid ringing`
- once answered, `rpid busy`
- when it ends, `rpid idle`

Check it worked with:

    fs_cli -x "sofia_presence_data rpid 101@default"

## Verifying the `presence` application

`dialplan/presence-actions.xml` also shows a `<action application="presence">`
form. Its argument order is not verified by this project: FreeSWITCH's own
references disagree, and the API appears to be defined by more than one module.
Do not copy it on trust. Run:

    fs_cli
    show application presence

and match the snippet to whatever that prints. Keep one form and delete the
other.

## Trying it without FreeSWITCH

`mock_server.py` answers SUBSCRIBE and replays a small state script, so the
whole client flow can be exercised locally:

    python3 mock_server.py
    python3 main.py --config mock-config.json --blf --no-call
```

- [ ] **Step 5: Add a mock config for local testing**

Create `mock-config.json`:

```json
{
    "local_ip": "127.0.0.1",
    "local_port": 5080,
    "transport": "udp",
    "target_user": "100",
    "target_host": "127.0.0.1",
    "target_port": 5061,
    "wav_file": "test.wav",
    "rtp_port": 10000,
    "sample_rate": 8000,
    "call_duration": 5,
    "user_agent": "PythonSIP/1.0",
    "from_user": "100",
    "display_name": "SIP Client",
    "blf": {
        "enabled": true,
        "domain": "default",
        "expires": 3600,
        "refresh": 1800,
        "buttons": [
            {"name": "101", "number": "101", "label": "Operator 1"},
            {"name": "102", "number": "102", "label": "Manager"}
        ]
    }
}
```

Create `mock-config-noblf.json`, identical except `"enabled": false`:

```json
{
    "local_ip": "127.0.0.1",
    "local_port": 5080,
    "transport": "udp",
    "target_user": "100",
    "target_host": "127.0.0.1",
    "target_port": 5061,
    "wav_file": "test.wav",
    "rtp_port": 10000,
    "sample_rate": 8000,
    "call_duration": 5,
    "user_agent": "PythonSIP/1.0",
    "from_user": "100",
    "display_name": "SIP Client",
    "blf": {
        "enabled": false,
        "domain": "default",
        "expires": 3600,
        "refresh": 1800,
        "buttons": [
            {"name": "101", "number": "101", "label": "Operator 1"},
            {"name": "102", "number": "102", "label": "Manager"}
        ]
    }
}
```

- [ ] **Step 6: Commit**

```bash
git add freeswitch mock-config.json mock-config-noblf.json
git commit -m "docs: FreeSWITCH-side BLF configuration snippets and mock configs"
```

---

### Task 11: End-to-end manual verification

Automated tests cover every unit. This task proves the pieces work wired together, which
no unit test can show. Nothing is committed by this task; it is a gate, not a change.

**Files:** none. Read-only verification.

**Interfaces:**
- Consumes: everything from Tasks 1 through 10.
- Produces: confirmation that the flow works, or a bug to fix.

- [ ] **Step 1: Confirm the full suite passes from a clean checkout**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -5`
Expected: `OK`, with no failures or errors

- [ ] **Step 2: Start the mock server**

Run in one terminal: `python3 mock_server.py`
Expected: `[mock] SIP listening on 127.0.0.1:5061`

- [ ] **Step 3: Watch BLF with no call**

Run in a second terminal: `python3 main.py --config mock-config.json --blf --no-call`
Expected within a few seconds:

```
[BLF] SUBSCRIBE 101 -> sip:101@default (expires=3600)
[BLF] SUBSCRIBE 102 -> sip:102@default (expires=3600)
[BLF] 101 (Operator 1): unknown -> idle
[BLF] 102 (Manager): unknown -> idle
```

Then, about 3 seconds later:

```
[BLF] 101 (Operator 1): idle -> ringing  caller="" <5551234>
```

And about 5 seconds after that, back to `idle`.

Requirement 2 is verified if those lines appear.

- [ ] **Step 4: Check blf list**

Type `blf list` at the `blf> ` prompt.
Expected: a table with a NAME, LABEL, STATE, CALLER header and one row per button.

- [ ] **Step 5: Press a button**

Type `press 101`.
Expected on the client:

```
[*] Pressing 101 (Operator 1) -> sip:101@127.0.0.1:5061
```

Expected in the mock server log: a line starting `[mock] Rx` with a first line beginning
`INVITE`, then `[mock] Sent 200 OK with SDP`. Requirement 3 is verified if the INVITE
reaches the server.

- [ ] **Step 6: Press again while a call is up**

Type `press 102` before the first call ends.
Expected: `Cannot place call, a call is already calling` or `... already active`, and no
second `INVITE` line in the mock server log.

- [ ] **Step 7: Press an unknown button**

Type `press 777`.
Expected: `Unknown BLF button '777'. Configured: 101, 102`

- [ ] **Step 8: Check refresh**

Type `blf refresh`.
Expected: `[BLF] Re-subscribed all buttons`, and the mock server logs a second
`Got SUBSCRIBE`.

- [ ] **Step 9: Check that the `enabled` flag alone activates BLF**

Run: `python3 main.py --config mock-config.json --no-call`
Expected: no `--blf` flag, but `blf.enabled` is `true`, so the client subscribes and
prints the same `[BLF] SUBSCRIBE ...` lines. Type `quit` to exit.

- [ ] **Step 10: Check that `enabled: false` really disables BLF**

Run: `python3 main.py --config mock-config-noblf.json --no-call`
Expected: `--no-call without BLF, placing the call anyway`, no `[BLF] SUBSCRIBE` line,
and the mock server log shows no `Got SUBSCRIBE`. At the prompt, `press 101` answers
`BLF is not active` and `blf list` answers `BLF is not active`. Type `quit` to exit.

- [ ] **Step 11: Check the untouched no-flag path still works**

Run: `python3 main.py --config mock-config-noblf.json`
Expected: the startup call is placed exactly as before, WAV streaming starts, and `quit`
hangs up cleanly with a BYE. The mock server logs the INVITE, the 200 OK, the ACK, then
the BYE. This is the regression contract from the Global Constraints.

- [ ] **Step 12: Confirm the shipped config.json stays off by default**

Run: `python3 -c "import json; print(json.load(open('config.json'))['blf']['enabled'])"`
Expected: `False`

---

## Self-review results

- **Spec coverage.** Requirement 1 (config-driven buttons): Tasks 4, 8, 10. Requirement 2
  (status changes in the console): Tasks 3, 5, 8, 9. Requirement 3 (press from the
  terminal): Tasks 7, 8. SIP refactor: Task 1. SUBSCRIBE: Task 2. Error handling: each
  case in the spec's Error handling list has a named test, listed below. Testing: every
  task, plus Task 11 for the wiring the unit tests cannot reach.
- **Error handling coverage.** Missing config: existing `load_config`, unchanged. Button
  without `name`: `test_button_without_name_is_dropped_with_warning`. Duplicate `name`:
  `test_duplicate_name_is_dropped_with_warning`. `refresh >= expires`:
  `test_refresh_above_expires_is_clamped_with_warning`. SUBSCRIBE rejected:
  `BLFManager.handle_response` matches the response Call-ID against the subscription
  dialogs and logs `[!] BLF SUBSCRIBE <name> rejected: <code> <reason>` while the button
  keeps its state, covered by `test_rejected_subscription_is_logged_and_state_stays_unknown`
  and `test_rejection_is_reported_once_per_response`. `main.py` routes such a response away
  from `CallSession` via `test_call_traffic_is_not_claimed`. Malformed NOTIFY:
  `test_malformed_body_is_logged_once`. Unknown press name:
  `test_press_unknown_name_lists_valid_names`. Press during a call:
  `test_start_refuses_while_a_call_is_active`. Send with no socket:
  `test_send_without_open_socket_raises`.
- **Placeholder scan.** No TBD, TODO, or "similar to". Every code step contains literal
  code. The one deliberately unverified item, the `presence` application argument order,
  is flagged in three places and carries an explicit verification command instead of a
  guess.
- **Type consistency.** `BLFButton` fields used by `format_change`, `render_blf_table`
  and `BLFManager` all match their definitions in Task 3. `BLFManager.get`,
  `.snapshot`, `.force_refresh`, `.handle_notify`, `.handle_response` are called with the
  signatures from Task 5. `CallSession.start/on_message/tick/hangup/close` match Task 7.
  `CommandContext` fields match Task 8's constructor. `parse_command` returns the
  `(name, args)` tuple that `dispatch_command` unpacks.
- **Machine-checked.** Before implementation, every code block in this plan was extracted,
  assembled into a throwaway tree, and executed. That pass found and fixed six real
  defects, all of which would have shipped as failing or flaky tests:
  `hangup()` on an idle session wrongly transitioned to `done`;
  `parse_blf_xml` looked for `param` among the direct children of `instance`, but
  FreeSWITCH nests them in a `<params>` wrapper, so every state parsed as `unknown`;
  `tick()` used `>` against the deadline, making the timeout test depend on clock
  granularity; `_build` omitted `Content-Length` from `msg.headers`;
  the re-SUBSCRIBE test asserted the refresh *interval* where the Expires header was
  correct; and the NOTIFY Request-URI and the instance name were both derived from the
  To header, which names the watcher rather than the watched resource. The finished
  assembly ran 135 tests green, clean across 25 consecutive runs, and the client was
  driven live against `mock_server.py`: SUBSCRIBE, two NOTIFY state changes, a press
  producing INVITE/200/ACK/RTP/BYE, and UNSUBSCRIBE on exit, plus the `--no-call`,
  `enabled: false`, and no-flag regression paths from Task 11.
- **Known gap, stated rather than hidden.** The client never registers, so FreeSWITCH
  sends NOTIFYs to the source address of the SUBSCRIBE. This is the agreed design
  (registration is out of scope) but it depends on FreeSWITCH accepting an
  unregistered watcher. If NOTIFYs never arrive against a real server, the fix is
  registration, which is a new task, not a defect in this plan.
