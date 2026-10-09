import time
from xml.etree import ElementTree

from call_session import send_simple_response
from sip_client import parse_digest_challenge

KNOWN_STATES = ("idle", "ringing", "busy", "held")


class BLFParseError(Exception):
    """Raised when a NOTIFY body is not usable BLF XML."""


def _local_name(tag):
    """Strip a namespace so 'instance' and '{urn:...}instance' both match."""
    return tag.rsplit("}", 1)[-1]


_DIALOG_STATE_PRIORITY = {"held": 4, "busy": 3, "ringing": 2, "idle": 1}


def _state_of(node):
    """The text of the first <state> child, lower-cased and stripped."""
    for child in node:
        if _local_name(child.tag) == "state":
            return (child.text or "").strip().lower()
    return None


def _dialog_candidate(dialog):
    """Map one RFC 4235 <dialog> element to (state, caller_number).

    Returns (None, None) when the dialog reports no active call, so the caller
    can tell an ignored state from a known one.
    """
    dialog_state = _state_of(dialog)
    if dialog_state not in ("early", "confirmed", "terminated"):
        return None, None

    local_state = None
    identity = None
    for node in dialog.iter():
        name = _local_name(node.tag)
        if name == "local" and local_state is None:
            local_state = _state_of(node)
        elif name == "identity" and identity is None:
            identity = node.text.strip() if node.text else None

    if dialog_state == "terminated":
        return "idle", None
    if dialog_state == "early":
        return "ringing", _user_part(identity)
    if local_state == "onhold":
        return "held", _user_part(identity)
    return "busy", _user_part(identity)


def _user_part(identity):
    """Pull the number out of 'sip:5551234@example.com'."""
    if not identity:
        return None
    text = identity.strip()
    if text.lower().startswith("sip:"):
        text = text[4:]
    return text.split("@", 1)[0] or None


def parse_dialog_info(body):
    """Parse an RFC 4235 dialog-info NOTIFY body.

    The watched resource is the root's entity attribute, returned as a
    "number@domain" key like parse_blf_xml uses. An empty dialog-info (no
    <dialog> elements) means the user is free, so it maps to {resource: idle}.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise BLFParseError(str(e)) from e

    entity = root.get("entity") or ""
    if entity.lower().startswith("sip:"):
        entity = entity[4:]
    if not entity:
        return {}

    params = {"state": "idle"}
    best_state = "idle"
    for dialog in root.iter():
        if _local_name(dialog.tag) != "dialog":
            continue
        state, caller = _dialog_candidate(dialog)
        if state is None:
            continue
        if _DIALOG_STATE_PRIORITY.get(state, 0) > \
                _DIALOG_STATE_PRIORITY.get(best_state, 0):
            best_state = state
            params = {"state": state}
        if caller:
            params["caller_id_number"] = caller
    params["state"] = best_state
    return {entity: params}


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

    def reset(self):
        """Forget the last known state, as when the subscription is dropped."""
        self.previous_state = self.state
        self.state = "unknown"
        self.caller_name = None
        self.caller_number = None
        self.uuid = None
        self.changed_at = time.time()


def _caller_text(button):
    """Render the caller, omitting whichever half is absent."""
    name = button.caller_name or ""
    number = button.caller_number or ""
    if name and number:
        return f'caller="{name}" <{number}>'
    if number:
        return f"caller=<{number}>"
    return f'caller="{name}"'


def format_change(button):
    line = f"[BLF] {button.name} ({button.label}): {button.previous_state} -> {button.state}"
    if button.caller_name or button.caller_number:
        line += "  " + _caller_text(button)
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


def _as_int(value, default, key, log):
    """Read a timing value, falling back on anything unusable.

    A typo in the config should cost a warning, not the whole client: this is
    called from build_blf_manager with no handler around it.
    """
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        log(f"[!] blf.{key} ({value!r}) is not a number, using {default}")
        return default
    if parsed <= 0:
        log(f"[!] blf.{key} ({parsed}) must be positive, using {default}")
        return default
    return parsed


def resolve_timing(config, log=print):
    """Return (expires, refresh), clamping refresh so it always precedes expiry."""
    section = config.get("blf") or {}
    expires = _as_int(section.get("expires", 3600), 3600, "expires", log)
    refresh = _as_int(section.get("refresh", 1800), 1800, "refresh", log)
    if refresh >= expires:
        clamped = max(1, expires // 2)
        log(f"[!] blf.refresh ({refresh}) >= blf.expires ({expires}), using {clamped}")
        refresh = clamped
    return expires, refresh


class BLFManager:
    """Owns the BLF subscription dialogs and the current state of each button.

    It reads SIP messages but knows nothing about RTP or the dialplan. Pressing
    a button is not its concern: callers resolve a button with get() and dial it.
    """

    def __init__(self, client, buttons, expires=3600, refresh=1800, log=print,
                 auth=None):
        self.client = client
        self.expires = expires
        self.refresh = refresh
        self.log = log
        self.auth = auth
        self.buttons = list(buttons)
        self._dialogs = {}
        self._next_refresh = {}
        self._parse_failed = False
        self._warned_content_type = False
        self._warned_resource = False
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
        # A notifier echoes the watched AOR with the domain it knows about,
        # which does not have to be the domain the config subscribed with
        # (FreeSWITCH uses its own host here). The full "number@domain" key
        # stays the fast path; the number alone is the fallback so a button is
        # still reached across a domain difference.
        button = self._by_resource.get(resource)
        if button is not None:
            return button
        number = resource.split("@", 1)[0] if "@" in resource else resource
        if number:
            for candidate in self.buttons:
                if candidate.number == number:
                    return candidate
        for candidate in self.buttons:
            if candidate.name == resource or candidate.name == number:
                return candidate
        return None

    def _subscribe(self, button):
        dialog = self._dialogs.get(button.name)
        if dialog is not None and dialog.to_tag is None:
            # The initial SUBSCRIBE is still unanswered, so this is a
            # retransmission of the same request rather than a refresh: keep
            # the dialog and its To tag state, and resend unchanged.
            dialog.send_subscribe("dialog", "application/blf+xml", self.expires,
                                  f"blf-{button.name}@{button.domain}")
            self.log(f"[BLF] RESEND SUBSCRIBE {button.name} "
                     f"-> {button.subscribe_uri}")
            return
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
        # Answer before looking at the body. A notifier retransmits with
        # doubling intervals until it gets a 2xx, so an unanswered NOTIFY
        # arrives roughly seven times over half a minute and the watcher reads
        # as dead. That includes a body we cannot use: silence is not an answer.
        send_simple_response(self.client, msg, 200, "OK")
        if self._subscription_ended(msg):
            return []
        content_type = msg.headers.get("Content-Type", "").lower()
        try:
            if "dialog-info+xml" in content_type:
                instances = parse_dialog_info(msg.body)
            elif "blf+xml" in content_type:
                instances = parse_blf_xml(msg.body)
            else:
                if not self._warned_content_type:
                    self._warned_content_type = True
                    self.log(f"[!] BLF NOTIFY ignored: unsupported "
                             f"Content-Type '{content_type}'")
                return []
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
            elif button is None and not self._warned_resource:
                self._warned_resource = True
                self.log(f"[!] BLF NOTIFY for '{resource}' matches no button")

        for button in changed:
            self.log(format_change(button))
        return changed

    def _subscription_ended(self, msg):
        """Handle Subscription-State: terminated.

        A subscription the server has dropped must not leave the button
        showing its last state forever. Re-subscribe on a fresh dialog: the
        old one is dead, so a refresh on it would only draw another rejection.
        """
        state = msg.headers.get("Subscription-State", "")
        if not state.lower().startswith("terminated"):
            return False
        resource = None
        if msg.body:
            try:
                if "dialog-info+xml" in msg.headers.get("Content-Type", ""):
                    instances = parse_dialog_info(msg.body)
                else:
                    instances = parse_blf_xml(msg.body)
            except BLFParseError:
                instances = {}
            if instances:
                resource = next(iter(instances))
        button = self._find_by_resource(resource) if resource else None
        name = button.name if button is not None else resource
        self.log(f"[!] BLF subscription for {name} {state}")

        if button is not None:
            button.reset()
            self.log(format_change(button))
        button = self.get(name) if name is not None else None
        self._dialogs.pop(name, None)
        self._next_refresh.pop(name, None)
        if button is not None:
            # With the dialog dropped, _subscribe builds a fresh one, which is
            # what a new subscription needs.
            self._subscribe(button)
        return True

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
                # The To tag identifies the dialog. Without it, refresh and
                # unsubscribe create a new dialog rather than continuing this one.
                to_tag = None
                if "tag=" in msg.headers.get("To", ""):
                    to_tag = msg.headers.get("To", "").split(";tag=")[1].split(";")[0]
                dialog.to_tag = to_tag
                self.log(f"[BLF] SUBSCRIBE {name} accepted ({code} {msg.reason})")
            elif code in (401, 407) and self._answer_challenge(
                    name, dialog, msg, code):
                pass
            else:
                self.log(f"[!] BLF SUBSCRIBE {name} rejected: {code} {msg.reason}")
            return True
        return False

    def _answer_challenge(self, name, dialog, msg, code):
        """Retry a challenged SUBSCRIBE once, with digest credentials.

        Returns True when the retry went out. A missing credential set, an
        unusable challenge or a nonce we have already answered all fall
        through to the caller's rejection log: a challenge must not become a
        resend loop.
        """
        challenge_header = "Proxy-Authenticate" if code == 407 else "WWW-Authenticate"
        auth_header = "Proxy-Authorization" if code == 407 else "Authorization"
        if not self.auth:
            return False
        challenge = parse_digest_challenge(msg.headers.get(challenge_header, ""))
        if not challenge.get("nonce"):
            return False
        if not dialog.set_auth(auth_header, challenge,
                               self.auth.get("username", ""),
                               self.auth.get("password", "")):
            return False
        button = self.get(name)
        dialog.send_subscribe("dialog", "application/blf+xml", self.expires,
                              f"blf-{name}@{button.domain}")
        self.log(f"[BLF] SUBSCRIBE {name} authenticating (nonce "
                 f"{challenge['nonce'][:8]}...)")
        return True
