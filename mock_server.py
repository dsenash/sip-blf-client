import socket
import struct
import time
import threading
import sys


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


def main():
    port = 5061
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    print(f"[mock] SIP listening on 127.0.0.1:{port}")

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