import json
import argparse
import socket
import time
import threading

from sip_client import SIPClient, SIPMessage
from rtp_sender import WAVStreamer, RTPMonitor

DEFAULT_CONFIG = "config.json"


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


def send_simple_response(client, msg, status, reason):
    resp_headers = {
        "Via": msg.headers.get("Via", ""),
        "From": msg.headers.get("From", ""),
        "To": msg.headers.get("To", ""),
        "Call-ID": msg.headers.get("Call-ID", ""),
        "CSeq": msg.headers.get("CSeq", ""),
    }
    rsp = SIPMessage(status_code=status, reason=reason)
    for k, v in resp_headers.items():
        rsp.add_header(k, v)
    client._send(rsp.build().encode())


def handle_call(client, config, rtp_port, wav_file, call_duration, uri, to_tag):
    sdp_info = client.parse_sdp(client.last_sdp)
    if sdp_info["media_port"] and sdp_info["media_ip"]:
        client.remote_rtp_addr = (sdp_info["media_ip"], sdp_info["media_port"])
        print(f"[RTP] Remote media: {sdp_info['media_ip']}:{sdp_info['media_port']}")
    else:
        client.remote_rtp_addr = (client.remote_addr[0], client.remote_addr[1])
        print(f"[RTP] No media in SDP, assuming SIP addr: {client.remote_rtp_addr}")

    local_bind_ip = config.get("local_ip", "0.0.0.0")
    if local_bind_ip == "0.0.0.0":
        local_bind_ip = get_local_ip(client.remote_addr[0])

    rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rtp_sock.bind((local_bind_ip, rtp_port))

    monitor = RTPMonitor(rtp_sock, config)
    monitor.start()

    streamer = WAVStreamer(wav_file, config)
    sender_thread = threading.Thread(
        target=streamer.send,
        args=(rtp_sock, client.remote_rtp_addr, call_duration),
        daemon=True,
    )
    sender_thread.start()
    print(f"[RTP] Local: {local_bind_ip}:{rtp_port} -> Remote: {client.remote_rtp_addr[0]}:{client.remote_rtp_addr[1]}")

    client.rtp_sock = rtp_sock
    client.monitor = monitor


def main():
    parser = argparse.ArgumentParser(description="SIP client for sending INVITE and streaming WAV over RTP")
    parser.add_argument("sip_uri", nargs="?", help="SIP URI (e.g. sip:101@192.168.0.89:5060;transport=udp)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"Path to config file (default: {DEFAULT_CONFIG})")
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
    client.remote_addr = (target["host"], target["port"])

    print(f"[-] Sending INVITE to {uri}")
    client.send_invite(uri)

    connected = False
    to_tag = None

    while True:
        msg = client.receive(timeout=10)
        if msg is None:
            print("[!] Timeout waiting for SIP response")
            break

        if msg.method:
            print(f"[SIP] Rx method: {msg.method}")
            if msg.method == "INVITE":
                print("         -> re-INVITE, answering 200 OK")
            elif msg.method == "BYE":
                print("[*] Received BYE, sending 200 OK")
                send_simple_response(client, msg, 200, "OK")
                break
            continue

        print(f"[SIP] {msg.status_code} {msg.reason}")

        if 100 <= msg.status_code < 200:
            continue

        if msg.status_code == 200:
            print("[+] INVITE accepted! Call connected.")
            to_tag = None
            if "To" in msg.headers and ";tag=" in msg.headers["To"]:
                to_tag = msg.headers["To"].split(";tag=")[1].split(";")[0]
            client.last_sdp = msg.body
            client.send_ack(uri, to_tag)
            print("[+] ACK sent")
            handle_call(client, config, rtp_port, wav_file, call_duration, uri, to_tag)
            connected = True
            break

        if 300 <= msg.status_code < 400:
            print(f"[!] Redirection {msg.status_code}, not following")
            break

        print(f"[!] Call failed: {msg.status_code} {msg.reason}")
        break

    if connected:
        print("[*] Monitoring RTP and SIP traffic. Ctrl+C to hang up.")
        try:
            while True:
                msg = client.receive(timeout=1)
                if msg:
                    if msg.method == "BYE":
                        print("[*] Received BYE, sending 200 OK")
                        send_simple_response(client, msg, 200, "OK")
                        break
                    elif msg.method == "INVITE":
                        print("[SIP] re-INVITE ignored")
        except KeyboardInterrupt:
            print("\n[*] Interrupted")
        finally:
            print("[*] Sending BYE")
            client.send_bye(uri, to_tag)
            time.sleep(1)
            if getattr(client, "monitor", None):
                client.monitor.stop()
            client.close()
            print("[+] Call ended")


if __name__ == "__main__":
    main()