import unittest

from blf import BLFButton
from console import parse_command
from main import (CommandContext, build_blf_manager, dispatch_command,
                  render_blf_table)


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


class FakeSession:
    def __init__(self, start_result=True):
        self.started = []
        self.hung_up = 0
        self.start_result = start_result
        self.state = "idle"

    def start(self, uri):
        self.started.append(uri)
        return self.start_result

    def hangup(self):
        self.hung_up += 1


class FakeClient:
    def create_dialog(self, target_uri):
        raise AssertionError("dispatch must not create dialogs directly")


class FakeManager:
    def __init__(self, buttons):
        self.buttons = buttons
        self.forced = 0

    def snapshot(self):
        return list(self.buttons)

    def get(self, name):
        for button in self.buttons:
            if button.name == name:
                return button
        return None

    def force_refresh(self):
        self.forced += 1


def make_context(buttons=None, log=None):
    log = log or RecordingLog()
    if buttons is None:
        buttons = [BLFButton("101", "101", "default", "Operator 1")]
    return CommandContext(blf=FakeManager(buttons),
                          session=FakeSession(),
                          client=FakeClient(),
                          target_host="192.168.0.89",
                          target_port=5060,
                          wav_file="test.wav",
                          log=log), log


class BuildBlfManagerTest(unittest.TestCase):
    def build(self, config):
        return build_blf_manager(config, FakeClient(), log=RecordingLog())

    def test_none_without_blf_section(self):
        self.assertIsNone(self.build({}))

    def test_none_when_disabled(self):
        self.assertIsNone(
            self.build({"blf": {"enabled": False,
                                "buttons": [{"name": "101"}]}}))

    def test_none_when_enabled_but_no_buttons(self):
        self.assertIsNone(self.build({"blf": {"enabled": True}}))

    def test_none_when_the_button_list_is_empty(self):
        self.assertIsNone(
            self.build({"blf": {"enabled": True, "buttons": []}}))

    def test_builds_manager_when_enabled(self):
        manager = self.build({"blf": {"enabled": True,
                                      "buttons": [{"name": "101"}]}})
        self.assertIsNotNone(manager)
        self.assertEqual([b.name for b in manager.snapshot()], ["101"])

    def test_never_subscribes_itself(self):
        # Subscription belongs to main(), which owns the loop; building the
        # manager must not send anything.
        manager = self.build({"blf": {"enabled": True,
                                      "buttons": [{"name": "101"}]}})
        self.assertEqual(len(manager.snapshot()), 1)

    def test_says_why_it_gave_up_when_there_are_no_buttons(self):
        log = RecordingLog()
        build_blf_manager({"blf": {"enabled": True}}, FakeClient(), log=log)
        self.assertIn("no buttons", log)


class RenderTableTest(unittest.TestCase):
    def test_includes_name_label_state_and_caller(self):
        button = BLFButton("101", "101", "default", "Operator 1")
        button.apply({"rpid": "ringing", "caller_id_number": "5551234"})
        table = render_blf_table(FakeManager([button]))
        self.assertIn("101", table)
        self.assertIn("Operator 1", table)
        self.assertIn("ringing", table)
        self.assertIn("5551234", table)

    def test_handles_no_buttons(self):
        self.assertIn("No BLF buttons", render_blf_table(FakeManager([])))

    def test_shows_a_dash_when_nothing_is_ringing(self):
        button = BLFButton("101", "101", "default", "Operator 1")
        self.assertIn("-", render_blf_table(FakeManager([button])))

    def test_shows_the_number_alone_when_there_is_no_name(self):
        button = BLFButton("101", "101", "default", "Operator 1")
        button.apply({"rpid": "ringing", "caller_id_number": "5551234"})
        table = render_blf_table(FakeManager([button]))
        self.assertIn("5551234", table)
        self.assertNotIn("<>", table)

    def test_shows_name_and_number_together(self):
        button = BLFButton("101", "101", "default", "Operator 1")
        button.apply({"rpid": "ringing", "caller_id_number": "5551234",
                      "caller_id_name": "Alice"})
        table = render_blf_table(FakeManager([button]))
        self.assertIn("Alice <5551234>", table)

    def test_lists_every_configured_button(self):
        buttons = [BLFButton("101", "101", "default", "Operator 1"),
                   BLFButton("102", "102", "default", "Manager")]
        table = render_blf_table(FakeManager(buttons))
        self.assertIn("101", table)
        self.assertIn("102", table)
        self.assertIn("Operator 1", table)
        self.assertIn("Manager", table)


class DispatchTest(unittest.TestCase):
    def test_press_dials_the_button_number(self):
        ctx, log = make_context()
        dispatch_command(parse_command("press 101"), ctx)
        self.assertEqual(ctx.session.started, ["sip:101@192.168.0.89:5060"])

    def test_press_uses_the_number_not_the_name(self):
        ctx, log = make_context([BLFButton("front", "101", "default", "Front")])
        dispatch_command(parse_command("press front"), ctx)
        self.assertEqual(ctx.session.started, ["sip:101@192.168.0.89:5060"])

    def test_press_unknown_name_lists_valid_names(self):
        ctx, log = make_context()
        dispatch_command(parse_command("press 777"), ctx)
        self.assertEqual(ctx.session.started, [])
        self.assertIn("Unknown BLF button", log)
        self.assertIn("101", log)

    def test_press_without_blf_is_refused(self):
        ctx, log = make_context()
        ctx.blf = None
        dispatch_command(parse_command("press 101"), ctx)
        self.assertEqual(ctx.session.started, [])
        self.assertIn("BLF is not active", log)

    def test_press_without_argument_shows_usage(self):
        ctx, log = make_context()
        dispatch_command(parse_command("press"), ctx)
        self.assertEqual(ctx.session.started, [])
        self.assertIn("usage: press", log)

    def test_press_reports_a_refused_call(self):
        ctx, log = make_context()
        ctx.session = FakeSession(start_result=False)
        dispatch_command(parse_command("press 101"), ctx)
        self.assertIn("already", log)

    def test_blf_list_prints_the_table(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf list"), ctx)
        self.assertIn("Operator 1", log)

    def test_blf_list_without_blf_is_refused(self):
        ctx, log = make_context()
        ctx.blf = None
        dispatch_command(parse_command("blf list"), ctx)
        self.assertIn("BLF is not active", log)

    def test_blf_refresh_forces_refresh(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf refresh"), ctx)
        self.assertEqual(ctx.blf.forced, 1)

    def test_blf_bad_subcommand_shows_usage(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf wat"), ctx)
        self.assertIn("usage: blf", log)

    def test_blf_without_a_subcommand_shows_usage(self):
        ctx, log = make_context()
        dispatch_command(parse_command("blf"), ctx)
        self.assertIn("usage: blf", log)

    def test_help_prints_help_text(self):
        ctx, log = make_context()
        dispatch_command(parse_command("help"), ctx)
        self.assertIn("press <name>", log)

    def test_hangup_calls_the_session(self):
        ctx, log = make_context()
        dispatch_command(parse_command("hangup"), ctx)
        self.assertEqual(ctx.session.hung_up, 1)

    def test_quit_signals_exit(self):
        ctx, log = make_context()
        self.assertFalse(dispatch_command(parse_command("quit"), ctx))

    def test_quit_does_not_hang_up(self):
        ctx, log = make_context()
        dispatch_command(parse_command("quit"), ctx)
        self.assertEqual(ctx.session.hung_up, 0)

    def test_unknown_command_is_reported(self):
        ctx, log = make_context()
        self.assertTrue(dispatch_command(parse_command("frobnicate"), ctx))
        self.assertIn("Unknown command", log)

    def test_blank_line_is_ignored(self):
        ctx, log = make_context()
        self.assertTrue(dispatch_command(None, ctx))
        self.assertEqual(log.lines, [])

    def test_dispatch_never_touches_the_client(self):
        # FakeClient.create_dialog raises; any direct call attempt fails.
        ctx, log = make_context()
        for line in ("help", "blf list", "blf refresh", "hangup", "press 101",
                     "frobnicate"):
            dispatch_command(parse_command(line), ctx)


if __name__ == "__main__":
    unittest.main()