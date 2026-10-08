# FreeSWITCH side of BLF

The client (`blf.py`) is a BLF watcher: it SUBSCRIBEs to each button's extension
and reads the state FreeSWITCH reports in NOTIFY bodies. It understands two
flavours of NOTIFY and picks one by `Content-Type`:

- `application/blf+xml` — the `rpid` in `instance`/`param` elements used by some
  FreeSWITCH setups.
- `application/dialog-info+xml` — the RFC 4235 `dialog-info` that newer
  FreeSWITCH sends. There, an empty `<dialog-info>` (no `<dialog>` element)
  means the user is free, `early` means ringing, `confirmed` means busy and
  `confirmed` with `<local><state>onhold</state></local>` means held. The
  remote `<identity>` is shown as the caller number.

It also places calls directly with INVITE when a button is pressed. It does not
register, so no `blf` / `blf_reg_*` directory parameters are needed.

Nothing here is applied automatically. Copy the files by hand.

Only `presence/blf-presence.xml` is a standalone document you install as-is.
`modules.conf.xml.snippet` and `dialplan/presence-actions.xml` are fragments:
they have several top-level elements on purpose, so merge their lines into an
existing file rather than loading them whole.

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

## Digest auth

A FreeSWITCH that challenges SUBSCRIBE answers `407 Proxy Authentication
Required` (or `401`) with a `Proxy-Authenticate: Digest ...` header. The client
answers it once per nonce with `Proxy-Authorization`, then re-sends the
SUBSCRIBE. Credentials live in `config.json`:

    "auth": {
        "username": "200",
        "password": "secret"
    }

Without that section the rejection is logged as before and nothing is retried.
A nonce that has already been answered is never retried, so wrong credentials
cost one extra SUBSCRIBE, not a loop. Only SUBSCRIBE is authenticated; the
INVITE path does not send credentials yet.

## Trying it without FreeSWITCH

`mock_server.py` answers SUBSCRIBE and replays a small state script, so the
whole client flow can be exercised locally:

    python3 mock_server.py
    python3 main.py --config mock-config.json --blf --no-call

Add `--auth 100:secret123` to the mock and the matching `"auth"` section to
`mock-config.json` (it is already there) to exercise the challenge path:

    python3 mock_server.py --auth 100:secret123
    python3 main.py --config mock-config.json --blf --no-call

Add `--dialog-info` to the mock to send RFC 4235 `dialog-info` bodies instead
of the `blf+xml` format:

    python3 mock_server.py --auth 100:secret123 --dialog-info

Keep stdin open while checking it (`(echo blf; sleep 10; echo quit) | ...`):
a pipe that reaches EOF makes the console quit before the 407 is read. A bare
Enter in the interactive console is ignored (it is a blank line, not EOF).

`mock-config-noblf.json` is the same config with `"enabled": false`, for
checking that a run without BLF behaves as it did before.