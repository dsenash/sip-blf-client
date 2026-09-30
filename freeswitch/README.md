# FreeSWITCH side of BLF

The client (`blf.py`) is a BLF watcher: it SUBSCRIBEs to each button's extension
and reads the `rpid` state that FreeSWITCH reports in NOTIFY bodies. It also
places calls directly with INVITE when a button is pressed. It does not
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

## Trying it without FreeSWITCH

`mock_server.py` answers SUBSCRIBE and replays a small state script, so the
whole client flow can be exercised locally:

    python3 mock_server.py
    python3 main.py --config mock-config.json --blf --no-call

`mock-config-noblf.json` is the same config with `"enabled": false`, for
checking that a run without BLF behaves as it did before.