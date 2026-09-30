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
    """One configured button and the last BLF state seen for it."""

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