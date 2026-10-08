import socket
import struct
import time
import threading
import sys
import hashlib
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def digest_ok(method, uri, headers, credentials, challenge):
    """Check a Digest Authorization/Proxy-Authorization header.

    Returns False for a missing or mismatched header, which is what makes the
    mock challenge an unauthenticated SUBSCRIBE the way FreeSWITCH does.
    """
    if not credentials:
        return True
    raw = (headers.get("Proxy-Authorization") or headers.get("Authorization")
           or "")
    if not raw.lower().startswith("digest"):
        return False
    from sip_client import parse_digest_challenge
    params = parse_digest_challenge(raw)
    username, password = credentials
    if params.get("username") != username:
        return False
    expect = parse_digest_challenge(challenge)
    if params.get("nonce") != expect.get("nonce"):
        return False
    uri_value = params.get("uri", uri)
    ha1 = md5(f"{username}:{expect.get('realm', '')}:{password}")
    ha2 = md5(f"{method}:{uri_value}")
    qop = expect.get("qop") or params.get("qop")
    if qop:
        nc = params.get("nc", "00000001")
        cnonce = params.get("cnonce", "")
        response = md5(f"{ha1}:{expect.get('nonce')}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        response = md5(f"{ha1}:{expect.get('nonce')}:{ha2}")
    return params.get("response") == response


def make_response(recv_msg, status, reason, extra_headers=None, body=""):
    lines = []
    lines.append(f"SIP/2.0 {status} {reason}")
    negate = False
    for line in recv_msg:
        if line == "":
            break
        if line.lower().startswith("cseq:"):
            lines.append(line)
        elif line.lower().startswith("from:"):
            lines.append(line)
        elif line.lower().startswith("to:"):
            lines.append(line + ";tag=remoteTAG")
        elif line.lower().startswith("call-id:"):
            lines.append(line)
        elif line.lower().startswith("via:"):
            lines.append(line)
        elif line.lower().startswith("max-forwards:"):
            lines.append(line)
        elif line.lower().startswith("contact:"):
            continue
    if extra_headers:
        lines.extend(extra_headers)
    lines.append("Content-Type: application/sdp")
    if body:
        lines.append(f"Content-Length: {len(body.encode())}")
        lines.append("")
        lines.append(body)
    else:
        lines.append("Content-Length: 0")
        lines.append("")
    return "\r\n".join(lines)


SDP_BODY = (
    "v=0\r\n"
    "o=remote 1 1 IN IP4 127.0.0.1\r\n"
    "s=MockServer\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 12000 RTP/AVP 0 8 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
)


# The fixed challenge the mock hands out when started with --auth.
DIGEST_CHALLENGE = ('Digest realm="default", nonce="mocknonce1", '
                    'algorithm=MD5, qop="auth"')


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
        parts.append(f'      <param name="rpid" value={quoteattr(rpid or "")}/>')
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


def main():
    credentials = None
    if "--auth" in sys.argv:
        spec = sys.argv[sys.argv.index("--auth") + 1]
        user, _, password = spec.partition(":")
        credentials = (user, password)
    port = 5061
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    print(f"[mock] SIP listening on 127.0.0.1:{port}")
    if credentials:
        print(f"[mock] digest auth required for {credentials[0]}")

    # RTP rx listener
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 12000))
    rx.settimeout(1)
    rtp_count = [0]
    stop = [False]

    def rtp_listener():
        print("[mock] RTP listening on 127.0.0.1:12000")
        while not stop[0]:
            try:
                data, addr = rx.recvfrom(4096)
                rtp_count[0] += 1
                if rtp_count[0] % 25 == 0:
                    print(f"[mock] RTP pkt #{rtp_count[0]} from {addr} len={len(data)}")
            except socket.timeout:
                pass

    t = threading.Thread(target=rtp_listener, daemon=True)
    t.start()

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.bind(("127.0.0.1", 12002))
    txssrc = 0xABCDEF01
    txseq = 5000
    tsts = 1000
    rtp_tx = [False]

    def send_rtp_back():
        while not rtp_tx[0]:
            try:
                pkt = struct.pack("!BBHII", 0x80, 0, txseq & 0xFFFF, tsts, txssrc) + b"\x55" * 160
                tx.sendto(pkt, ("127.0.0.1", 10000))
                rtp_tx[0] = True
            except OSError:
                pass

    sock.settimeout(30)
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            msg = data.decode(errors="replace")
            first = msg.split("\r\n")[0]
            print(f"[mock] Rx {addr}: {first}")
            if first.startswith("INVITE"):
                resp_m = msg.split("\r\n")
                r = make_response(resp_m, 100, "Trying")
                sock.sendto(r.encode(), addr)
                time.sleep(0.5)
                r = make_response(resp_m, 180, "Ringing")
                sock.sendto(r.encode(), addr)
                time.sleep(0.5)
                r = make_response(resp_m, 200, "OK", body=SDP_BODY)
                sock.sendto(r.encode(), addr)
                print("[mock] Sent 200 OK with SDP")
            elif first.startswith("ACK"):
                print("[mock] Got ACK")
                send_rtp_back()
            elif first.startswith("SUBSCRIBE"):
                lines = msg.split("\r\n")
                headers = {}
                for line in lines[1:]:
                    if not line:
                        break
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        headers[key] = value
                if credentials and not digest_ok(
                        "SUBSCRIBE", first.split(" ")[1], headers,
                        credentials, DIGEST_CHALLENGE):
                    # The second SUBSCRIBE repeats the call, so an unchanged
                    # nonce must not turn this into an infinite challenge.
                    if headers.get("CSeq", "").split(" ")[0] == "2":
                        sock.sendto(make_response(
                            lines, 403, "Forbidden").encode(), addr)
                        print("[mock] SUBSCRIBE with bad credentials, 403")
                        continue
                    sock.sendto(make_response(
                        lines, 407, "Proxy Authentication Required",
                        extra_headers=[f"Proxy-Authenticate: {DIGEST_CHALLENGE}"]
                    ).encode(), addr)
                    print("[mock] SUBSCRIBE challenged, 407")
                    continue
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
            elif first.startswith("BYE"):
                r = make_response(msg.split("\r\n"), 200, "OK")
                sock.sendto(r.encode(), addr)
                print("[mock] Sent 200 OK to BYE")
                stop[0] = True
                break
            elif first.startswith("2"):
                pass
    except socket.timeout:
        print("[mock] Timeout")
    print(f"[mock] Total RTP packets received: {rtp_count[0]}")


if __name__ == "__main__":
    main()