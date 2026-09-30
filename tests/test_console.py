import io
import queue
import threading
import unittest

from console import HELP_TEXT, KNOWN_COMMANDS, StdinReader, parse_command


class BlockingStream:
    """A stream whose readline parks until close() releases it."""

    def __init__(self):
        self.released = threading.Event()
        self.closed = False

    def readline(self):
        self.released.wait(5)
        return ""

    def close(self):
        self.closed = True
        self.released.set()


class ParseCommandTest(unittest.TestCase):
    def test_parses_command_with_argument(self):
        self.assertEqual(parse_command("press 101"), ("press", ["101"]))

    def test_parses_command_without_argument(self):
        self.assertEqual(parse_command("quit"), ("quit", []))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(parse_command("   press   101  "), ("press", ["101"]))

    def test_lowercases_the_command(self):
        self.assertEqual(parse_command("BLF list"), ("blf", ["list"]))

    def test_keeps_extra_arguments_in_order(self):
        self.assertEqual(parse_command("blf a b"), ("blf", ["a", "b"]))

    def test_blank_line_is_none(self):
        self.assertIsNone(parse_command(""))

    def test_whitespace_only_is_none(self):
        self.assertIsNone(parse_command("   "))

    def test_none_is_none(self):
        self.assertIsNone(parse_command(None))

    def test_every_known_command_round_trips(self):
        for name in KNOWN_COMMANDS:
            self.assertEqual(parse_command(name), (name, []))


class HelpTextTest(unittest.TestCase):
    def test_help_mentions_every_command(self):
        for name in KNOWN_COMMANDS:
            self.assertIn(name, HELP_TEXT)

    def test_help_documents_press_with_its_argument(self):
        self.assertIn("press <name>", HELP_TEXT)

    def test_help_documents_the_blf_subcommands(self):
        self.assertIn("blf list", HELP_TEXT)
        self.assertIn("blf refresh", HELP_TEXT)


class StdinReaderTest(unittest.TestCase):
    def test_start_creates_a_daemon_thread(self):
        thread = StdinReader(queue.Queue(), prompt="test> ").start()
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)

    def test_stop_sets_the_stop_event(self):
        reader = StdinReader(queue.Queue())
        reader.start()
        reader.stop()
        self.assertTrue(reader._stop.is_set())

    def test_delivers_a_typed_line(self):
        q = queue.Queue()
        reader = StdinReader(q, stream=io.StringIO("blf list\n"))
        reader.start()
        reader.join(timeout=5)
        self.assertEqual(q.get_nowait(), "blf list")

    def test_strips_the_line_ending(self):
        q = queue.Queue()
        reader = StdinReader(q, stream=io.StringIO("quit\r\n"))
        reader.start()
        reader.join(timeout=5)
        self.assertEqual(q.get_nowait(), "quit")

    def test_delivers_several_lines_in_order(self):
        q = queue.Queue()
        reader = StdinReader(q, stream=io.StringIO("help\nblf list\npress 101\n"))
        reader.start()
        reader.join(timeout=5)
        self.assertEqual([q.get_nowait() for _ in range(3)],
                         ["help", "blf list", "press 101"])

    def test_ignores_blank_lines(self):
        q = queue.Queue()
        reader = StdinReader(q, stream=io.StringIO("\n   \nquit\n"))
        reader.start()
        reader.join(timeout=5)
        delivered = []
        while not q.empty():
            delivered.append(q.get_nowait())
        # The typed line, then the end-of-stream that asks for a clean exit.
        # The two blank lines between them must not appear at all.
        self.assertEqual(delivered, ["quit", "quit"])

    def test_end_of_stream_asks_for_a_clean_shutdown(self):
        q = queue.Queue()
        reader = StdinReader(q, stream=io.StringIO(""))
        reader.start()
        reader.join(timeout=5)
        self.assertEqual(q.get_nowait(), "quit")

    def test_stop_ends_a_blocked_thread(self):
        stream = BlockingStream()
        reader = StdinReader(queue.Queue(), stream=stream)
        reader.start()
        reader.stop()
        reader.join(timeout=5)
        self.assertFalse(reader._thread.is_alive())

    def test_stop_closes_the_injected_stream(self):
        stream = BlockingStream()
        reader = StdinReader(queue.Queue(), stream=stream)
        reader.start()
        reader.stop()
        self.assertTrue(stream.closed)

    def test_stop_without_start_is_harmless(self):
        StdinReader(queue.Queue(), stream=BlockingStream()).stop()

    def test_join_before_start_is_harmless(self):
        StdinReader(queue.Queue(), stream=io.StringIO("")).join(timeout=5)

    def test_stop_is_idempotent(self):
        reader = StdinReader(queue.Queue(), stream=BlockingStream())
        reader.start()
        reader.stop()
        reader.stop()
        reader.join(timeout=5)
        self.assertFalse(reader._thread.is_alive())


if __name__ == "__main__":
    unittest.main()