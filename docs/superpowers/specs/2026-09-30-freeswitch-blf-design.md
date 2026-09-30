# FreeSWITCH BLF — Design

**Date:** 2026-09-30
**Status:** Approved for implementation

## Goal

Add BLF (Busy Lamp Field) support to the Python SIP client so that:

1. Programmable BLF buttons are declared in `config.json`.
2. BLF status changes are visible in the console.
3. A button can be "pressed" from a terminal command.

Pressing a button is a **direct INVITE** to the watched extension, not an
RFC 3903 `NOTIFY` with `rpid=received`.

## Context

The existing client (`main.py`, `sip_client.py`, `rtp_sender.py`, `mock_server.py`,
~600 lines) is a one-shot softphone: it sends a single INVITE, streams a WAV file
over RTP, and terminates on BYE or Ctrl+C. It does not register, has no SUBSCRIBE
or NOTIFY support, and its receive loop is blocking.

Three decisions were taken with the user and are binding:

| Decision | Choice |
|---|---|
| Press mechanism | Direct INVITE (originate) to the watched extension |
| Application shape | New `--blf` flag; the existing auto-call mode is unchanged |
| Registration | None. Same unregistered style as today |

## Architecture

Three units, each with one responsibility.

### 1. `sip_client.py` — add `SIPDialog`

Today `SIPClient` owns a single `call_id`, `cseq`, and `tag` for the whole process.
BLF requires a second SIP dialog (the subscription) that lives **concurrently**
with a call dialog, so per-dialog state must be separated from transport state.

`SIPClient` keeps: socket creation, `_send`, `receive`, `parse_sdp`, `close`.
New: `create_dialog(target_uri)` factory, and a `threading.Lock` inside `_send` so
that the stdin thread and the main receive loop cannot interleave a datagram.

New class `SIPDialog` owns `call_id`, `cseq`, `from_tag`, `local_uri`, `remote_uri`
and the request builders:

- `invite(sdp)` / `ack(to_tag)` / `bye(to_tag)` — moved from `SIPClient`
- `subscribe(event, accept, expires, event_id)` — new
- `refresh(expires)` — re-SUBSCRIBE on the same dialog, new CSeq
- `unsubscribe()` — SUBSCRIBE with `Expires: 0`

The three existing methods keep their current message shape. Only their location
and call site change. `main.py` currently calls `client.send_invite(uri)`; it
becomes `dialog = client.create_dialog(uri)` then `dialog.invite()`.

### 2. `blf.py` (new) — subscription and state only

This module must not know about RTP, WAV files, or the dialplan. It is the only
place that parses BLF XML.

```python
class BLFButton:
    name          # short key used in terminal commands
    number        # extension being watched
    domain        # FreeSWITCH presence domain
    label         # human-readable text for console output
    state         # "idle" | "ringing" | "busy" | "held" | "unknown"; starts "unknown"
    caller_name   # last reported caller_id_name, or None
    caller_number # last reported caller_id_number, or None
    uuid          # last reported channel uuid, or None
    changed_at    # time.time() at construction and on each change

    def subscribe_uri() -> str   # "sip:{number}@{domain}"

class BLFManager:
    def __init__(self, client, buttons, config, log)
    def subscribe_all()          # one SUBSCRIBE per button, logs results
    def refresh_due()            # re-SUBSCRIBE dialogs past their refresh deadline
    def handle_notify(msg)       # parse body, update buttons, log changes
    def snapshot()               # ordered list of BLFButton for rendering
    def get(name)                # BLFButton or None
```

`BLFManager` does **not** implement pressing. It only resolves a button.

### 3. `main.py` — one dispatch loop plus a console

The current `while True: receive()` loop with `break` statements is replaced by a
single loop that dispatches by message type, so that NOTIFY traffic on the shared
socket is handled alongside call traffic.

- Main thread: receive loop, dispatch, render.
- Stdin thread: reads lines, pushes them onto a `queue.Queue`.
- Each loop iteration drains the command queue first, then does a
  `receive(timeout=0.5)`.

Reusing the existing call path means extracting the blocking response wait into a
small state machine that the loop can advance. `press` therefore does not dial
inline; it enqueues a call request that the loop picks up.

The console is always available, not only under `--blf`, because it is the same
loop. Without `--blf`, BLF commands answer "BLF is not active".

## Configuration

```json
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
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Allows BLF to be turned on from config; `--blf` also enables it |
| `domain` | `"default"` | Presence domain for all subscriptions |
| `expires` | `3600` | `Expires` in the SUBSCRIBE, seconds |
| `refresh` | `1800` | Re-SUBSCRIBE interval, seconds. Must be < `expires` |
| `buttons` | `[]` | List of programmable buttons |

Button keys: `name` is required and must be unique. `number` defaults to `name`.
`label` defaults to `number`. A button with no `name` is dropped with a warning at
load time.

**Two different addressings, deliberately:**

- **Subscription** goes to `sip:{number}@{blf.domain}` — this is what `mod_presence`
  resolves.
- **Press** goes to `sip:{number}@{target_host}:{target_port}` — this is an INVITE
  that lands in the FreeSWITCH dialplan, so it uses the existing `target_*` config,
  not `blf.domain`.

The watcher identity is the existing `from_user` (default `100`).

## Requirement 2 — statuses in the console

SUBSCRIBE request headers:

```
Event: dialog
Accept: application/blf+xml
Expires: 3600
Event-ID: blf-{name}@{domain}
Supported: eventlist, dialog, call-info, terminate
Max-Forwards: 70
```

The SUBSCRIBE carries no body, so no `Content-Type` is sent.

The NOTIFY parser is deliberately tolerant, because FreeSWITCH varies the body
between versions:

- Strip XML namespaces before matching tag names, so both `instance` and
  `{urn:freedeswitch:blf}instance` match.
- For each `<instance name="...">` collect its `<param name=... value=...>` pairs
  into a dict.
- State is the `rpid` param, falling back to `rpid_value`, then to `state`. A value
  outside the known set becomes `unknown` rather than an exception.
- An `<instance>` whose `name` does not match any configured button is ignored.
- Malformed XML is logged, then suppressed for that button until a later NOTIFY
  parses successfully, so a repeated bad body cannot flood the console.

Every transition is logged as a single line:

```
[BLF] 101 (Operator 1): idle -> ringing  caller="Ivan" <5551234>  uuid=8f3c1a...
```

The initial NOTIFY after subscribing is logged the same way, from `unknown`.
Repeated NOTIFYs that do not change state are silent, except that a changed
`caller_number` or `uuid` is also logged.

The `blf list` command prints a snapshot table of all buttons with their current
state. There is no live full-screen table: interleaving a redrawn table with log
lines makes both unreadable.

## Requirement 3 — pressing from the terminal

`--blf` enables the console. Commands:

| Command | Action |
|---|---|
| `press <name>` | Place a call to the button's extension |
| `blf list` | Print a table of current button states |
| `blf refresh` | Force a re-SUBSCRIBE for all buttons now |
| `help` | List commands |
| `hangup` | Send BYE for the active call |
| `quit` | BYE, unsubscribe, close, exit |

`press 101` enqueues a call request for the main loop. The loop runs the **same**
call path as the startup auto-call: `invite` → 200 OK → `ack` → `handle_call` for
WAV streaming. The call path is not duplicated.

If a call is already active, `press` is refused with a message. Pressing an
unknown name lists the configured button names.

## FreeSWITCH-side requirements

The client cannot work alone. These are part of the deliverable, delivered as
snippet files under `freeswitch/`:

- `mod_presence` enabled in `modules.conf.xml`.
- A presence profile under `conf/presence/` with a `<presence id="{number}@{domain}"/>`
  entry per button.
- The dialplan must move the channel's presence as the call progresses, otherwise
  state never leaves `idle`. Two mechanisms exist and the snippets will show the
  unambiguous one first:
  - `uuid_setvar <uuid> presence_id <number>@<domain>` followed by
    `uuid_setvar <uuid> rpid <state>`. This form is documented and its argument
    order is certain.
  - The `presence` dialplan application, whose argument order is **not** verified
    here. FreeSWITCH's own references disagree (`<rpid> <status> [<id>]` in the
    dptools reference versus `[in | out]` in the API list), and it appears to be
    defined by more than one module. Before shipping the snippet, confirm against
    the target build with `fs_cli` → `show application presence`. Do not take the
    argument order from this document.
  - Typical states to move through: `idle` on entry, `busy` once answered,
    `idle` on hangup. `ringing` is what a BLF watcher expects while the call is
    alerting, so set it on the channel that is alerting rather than the one being
    alerted.
- `blf` / `blf_reg_*` directory parameters are not required for this client, since
  it does not register. They are documented but not relied upon.

## Files

| File | Change |
|---|---|
| `sip_client.py` | Add `SIPDialog`; move `invite`/`ack`/`bye` onto it; add `subscribe`/`refresh`/`unsubscribe`; add `create_dialog`; lock `_send` |
| `blf.py` | New. `BLFButton`, `BLFManager`, NOTIFY XML parser |
| `main.py` | Single dispatch loop, stdin thread, command dispatch, `--blf` flag, call state machine |
| `config.json` | Add `blf` section |
| `mock_server.py` | Answer SUBSCRIBE with 200 OK, send BLF NOTIFY on state change |
| `tests/` | New. `unittest` only, no new dependencies |
| `freeswitch/` | New. Presence profile, modules.conf, dialplan snippets |

## Error handling

- Config file missing or invalid: existing `load_config` behaviour is kept —
  print a warning and fall back to defaults, which means an empty `blf.buttons`
  and therefore no BLF.
- A button without `name`: dropped with a warning naming the offending index.
- Duplicate button `name`: the later entry is dropped with a warning.
- `refresh` >= `expires`: clamped to `expires / 2` with a warning.
- SUBSCRIBE rejected (any non-2xx): logged with the status, the button stays
  `unknown`, and the loop continues. One failed subscription does not abort the
  others.
- NOTIFY with malformed XML: logged once, then suppressed until a NOTIFY parses
  successfully, as described above.
- `press` on an unknown name: error plus the list of valid names.
- `press` while a call is active: refused, no packet sent.
- Socket errors during a send: caught at the loop level, logged, loop continues.

## Testing

No tests exist in the repository today. The plan uses stdlib `unittest`; no new
dependencies are added.

Pure-function tests, no network required:

- BLF XML parsing: known rpid, unknown rpid, namespaced tags, multiple instances,
  missing `rpid`, malformed XML, instance name not matching any button.
- Config loading: buttons parsed, `number`/`label` defaults, missing `name`
  dropped, duplicate `name` dropped, `refresh` clamped.
- SUBSCRIBE construction: presence and correctness of `Event`, `Accept`, `Expires`,
  `Event-ID`, and that `refresh` reuses the dialog's Call-ID with an incremented
  CSeq.
- INVITE/ACK/BYE construction: unchanged from current behaviour (regression guard
  for the `SIPDialog` refactor).
- Command parsing: `press 101`, `press` with no argument, unknown command,
  `blf list`, `blf` with a bad subcommand.

Integration aid: `mock_server.py` gains SUBSCRIBE handling and a periodic BLF
NOTIFY, so the full subscribe → notify → status-change → press flow can be
exercised locally without a real FreeSWITCH.

Manual acceptance against real FreeSWITCH: the console shows `idle → ringing →
idle` when the watched extension is called, and `press` rings that extension.

## Out of scope

- Registration, digest auth, re-REGISTER.
- `NOTIFY`-based press with `rpid=received`.
- Multiple domains per button (a single `blf.domain` for all buttons).
- Live full-screen console UI.
- Watching state of the client's own `from_user` extension.
