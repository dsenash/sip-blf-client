"""The state machine for a single outbound call.

The main loop parks in a socket receive, so a call cannot be run as
"send INVITE, then block until the answer arrives". start() sends the
INVITE, on_message() advances the state machine, tick() enforces the
answer timeout, and nothing here blocks.
"""

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
        # A host-only URI such as sip:192.168.0.89 puts the whole thing in
        # hostport; treating it as the user produced sip:192.168.0.89@:5060.
        user, hostport = "", uri

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


class CallSession:
    """A single outbound call, driven one event at a time by the main loop."""

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
        # The session object is reused for the next call, so a late answer or
        # a stray BYE from an earlier dialog would otherwise be applied to the
        # live call: adopting its To tag, its SDP, or ending the call outright.
        if not self._belongs_to_this_dialog(msg):
            return False
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

    def _belongs_to_this_dialog(self, msg):
        if self.dialog is None:
            return False
        call_id = msg.headers.get("Call-ID", "")
        if not call_id:
            # Nothing to match against, so do not guess: a bare datagram is not
            # evidence about this dialog.
            return False
        return call_id == self.dialog.call_id

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
            # hangup(), not _finish(): the INVITE is still outstanding, so it
            # has to be cancelled rather than just forgotten.
            self.hangup()

    def hangup(self):
        if self.state == self.IDLE:
            return
        if self.state == self.ACTIVE:
            self.log("[*] Sending BYE")
            self.dialog.send_bye(self.to_tag)
        elif self.state == self.CALLING:
            # Nothing was established yet, so there is no BYE to send: a BYE
            # on an unanswered INVITE is a protocol error. CANCEL is what
            # stops the far end from ringing on with nobody there.
            self.log("[*] Cancelling the unanswered INVITE")
            self.dialog.send_cancel()
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