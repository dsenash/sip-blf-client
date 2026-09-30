"""Entry point: one loop that serves the call, the BLF buttons and the console."""

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
    """Build the subscription manager, or None when BLF should stay off.

    This never subscribes: the loop in main() owns the traffic, so building
    the manager must not put anything on the wire.
    """
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
        name = button.caller_name or ""
        number = button.caller_number or ""
        if name and number:
            caller = f"{name} <{number}>"
        elif number:
            caller = number
        elif name:
            caller = name
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
            # Checked here, before receive(): a call can end in tick() without
            # any further SIP traffic arriving, and the `msg is None` branch
            # below would otherwise skip this and spin forever.
            if session.finished and not blf_mode:
                break

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