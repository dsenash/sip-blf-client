"""Terminal command parsing and the background reader for stdin."""

import sys
import threading

KNOWN_COMMANDS = ("press", "blf", "help", "hangup", "quit")

HELP_TEXT = """Commands:
  press <name>    Call the extension on a BLF button
  blf list        Show the current state of every BLF button
  blf refresh     Re-subscribe to all BLF buttons now
  help            Show this help
  hangup          Hang up the active call
  quit            Hang up, unsubscribe and exit"""


def parse_command(line):
    """Split a console line into (command, args). Returns None for a blank line."""
    if not line:
        return None
    parts = line.split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


class StdinReader:
    """Reads console lines on a daemon thread and puts them on a queue.

    The main loop is parked in a socket receive, so input has to be read
    elsewhere. A daemon thread means process exit is never blocked on input().

    `stream` is injectable so tests can drive the reader without a terminal;
    when it is None the real stdin is used and input() prints the prompt.
    """

    def __init__(self, queue, prompt="blf> ", stream=None):
        self.queue = queue
        self.prompt = prompt
        self.stream = stream
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        # Closing an injected stream is what unblocks a readline parked in the
        # kernel. The process-wide stdin is left alone: the thread is a daemon
        # and dies with the process.
        if self.stream is not None and self.stream is not sys.stdin:
            try:
                self.stream.close()
            except (ValueError, OSError):
                pass

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _read_line(self):
        if self.stream is None:
            # input() prints the prompt itself.
            return input(self.prompt)
        return self.stream.readline()

    def _run(self):
        while not self._stop.is_set():
            try:
                line = self._read_line()
            except EOFError:
                self.queue.put("quit")
                return
            except KeyboardInterrupt:
                continue
            except (ValueError, OSError):
                return

            if not line:
                # EOF on the stream: nothing more will ever arrive, so ask the
                # dispatcher to shut down cleanly instead of hanging.
                self.queue.put("quit")
                return

            text = line.rstrip("\r\n")
            if text.strip():
                self.queue.put(text)