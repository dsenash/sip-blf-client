import os
import subprocess
import sys
import unittest

from blf import BLFButton
from console import parse_command
from main import (CommandContext, build_blf_manager, dispatch_command, run_loop,
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

    def test_question_mark_prints_help_text(self):
        ctx, log = make_context()
        dispatch_command(parse_command("?"), ctx)
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


class LoopClient:
    """A client whose socket misbehaves on cue, the way a real one does.

    Once the scripted events run out it queues a 'quit', which stands in for a
    user typing at the console.
    """

    def __init__(self, events, commands=None, quit_after=3):
        self.events = list(events)
        self.commands = commands
        self.quit_after = quit_after
        self.idle = 0
        self.received = []

    def receive(self, timeout=0.5):
        if not self.events:
            self.idle += 1
            if self.commands is not None and self.idle >= self.quit_after:
                self.commands.put("quit")
                self.commands = None
            return None
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    def close(self):
        self.closed = True


class LoopSession:
    def __init__(self):
        self.state = "idle"
        self.finished = False
        self.ticked = 0
        self.closed = 0

    def tick(self):
        self.ticked += 1

    def close(self):
        self.closed += 1

    def hangup(self):
        pass

    def on_message(self, msg):
        pass


class LoopBlf:
    def __init__(self):
        self.refreshed = 0
        self.unsubscribed = 0
        self.raise_on_unsubscribe = False

    def refresh_due(self):
        self.refreshed += 1

    def handle_notify(self, msg):
        return True

    def handle_response(self, msg):
        return False

    def unsubscribe_all(self):
        self.unsubscribed += 1
        if self.raise_on_unsubscribe:
            raise OSError(9, "Bad file descriptor")


class RunLoopTest(unittest.TestCase):
    """The spec requires a socket error during a send to be logged with the
    loop continuing. Nothing caught it: main() caught KeyboardInterrupt only,
    so one ICMP-unreachable datagram, which a UDP socket reports as ECONNREFUSED
    on the next send, took the whole client down along with its subscriptions.
    """

    def run_it(self, events, commands=(), blf=None, timeout=5):
        import queue
        import threading
        q = queue.Queue()
        for line in commands:
            q.put(line)
        client = LoopClient(events, q)
        session = LoopSession()
        self.session = session
        log = RecordingLog()
        error = []

        def target():
            try:
                run_loop(client, session, blf, q, False, log=log)
            except BaseException as exc:      # noqa: BLE001 - reported below
                error.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "run_loop did not return")
        if error:
            raise error[0]
        return client, self.session, log

    def test_receive_socket_error_is_logged_and_the_loop_continues(self):
        # run_it raises anything that escapes, so returning at all is the
        # assertion that the loop continued rather than propagating.
        client, session, log = self.run_it([OSError(111, "Connection refused")])
        self.assertIn("[!] Socket error: [Errno 111] Connection refused", log)

    def test_the_loop_keeps_working_after_a_socket_error(self):
        client, session, log = self.run_it([OSError(111, "Connection refused")])
        self.assertGreater(session.ticked, 1)

    def test_a_persistently_failing_socket_eventually_gives_up(self):
        # 21 identical failures means the socket is gone, not that the peer is
        # briefly busy. Spinning at full speed would peg a CPU forever.
        client, session, log = self.run_it([OSError(111, "Connection refused")] * 21)
        self.assertIn("Too many socket errors", log)

    def test_refresh_errors_are_logged_and_the_loop_continues(self):
        class BadBlf(LoopBlf):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def refresh_due(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError(111, "Connection refused")

        client, session, log = self.run_it([], blf=BadBlf())
        self.assertIn("Connection refused", log)
        self.assertGreater(session.ticked, 1)

    def test_run_loop_leaves_the_shutdown_to_its_caller(self):
        # main() closes the session, unsubscribes and closes the socket. The
        # loop must not also do it, or the call would be torn down twice.
        client, session, log = self.run_it([])
        self.assertFalse(hasattr(client, "closed"))

    def test_quit_returns_without_waiting_on_the_network(self):
        client, session, log = self.run_it([], commands=("quit",))
        self.assertEqual(client.events, [])

    def test_a_call_that_finishes_without_traffic_ends_the_loop(self):
        session = LoopSession()
        session.finished = True
        q = __import__("queue").Queue()
        client = LoopClient([], q)
        log = RecordingLog()
        run_loop(client, session, None, q, False, log=log)
        self.assertEqual(session.ticked, 1)


class MainProcessTest(unittest.TestCase):
    """Run main.py as a process.

    A NameError in main() took the client down with a traceback while every
    unit test still passed, because nothing ran main() itself.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run_main(self, config_text, args=(), commands="quit", timeout=15):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as handle:
                handle.write(config_text)
            proc = subprocess.run(
                [sys.executable, "main.py", "--config", path, *args],
                cwd=self.ROOT, input=commands, capture_output=True,
                text=True, timeout=timeout)
        return proc

    def test_exits_cleanly_after_quit(self):
        proc = self.run_main('{"target_port": 15099}')
        self.assertEqual(proc.returncode, 0)
        self.assertIn("[+] Exited", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def run_main_holding_stdin(self, config_text, timeout=20):
        """Start main.py and leave stdin open, as a real terminal would.

        Closing stdin makes StdinReader see EOF, which quits by design, so a
        test that means to wait out a timeout has to keep the pipe open.
        """
        import tempfile
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as handle:
                handle.write(config_text)
            proc = subprocess.Popen(
                [sys.executable, "main.py", "--config", path],
                cwd=self.ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            # communicate() would close stdin, which is the one thing this test
            # must not do: EOF quits by design and hides the timeout.
            collected = {}

            def drain(name, stream):
                collected[name] = stream.read()

            threads = [threading.Thread(target=drain, args=("out", proc.stdout)),
                       threading.Thread(target=drain, args=("err", proc.stderr))]
            for thread in threads:
                thread.daemon = True
                thread.start()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("main.py did not exit on its own")
            for thread in threads:
                thread.join(5)
        return proc.returncode, collected.get("out", ""), collected.get("err", "")

    def test_a_call_that_times_out_ends_the_process(self):
        # Nothing is listening on 15099, so the INVITE is never answered. The
        # call must time out and take the process down on its own rather than
        # waiting at the console for a user who has nothing to press.
        code, out, err = self.run_main_holding_stdin(
            '{"target_port": 15099, "sip_call_timeout": 1}')
        self.assertEqual(code, 0)
        self.assertIn("[!] Timeout waiting for SIP response", out)
        self.assertNotIn("Traceback", err)

    def test_an_unanswered_invite_is_cancelled_on_timeout(self):
        code, out, err = self.run_main_holding_stdin(
            '{"target_port": 15099, "sip_call_timeout": 1}')
        self.assertIn("Cancelling the unanswered INVITE", out)

    def test_help_exits_cleanly(self):
        proc = subprocess.run([sys.executable, "main.py", "--help"],
                              cwd=self.ROOT, capture_output=True, text=True,
                              timeout=15)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()