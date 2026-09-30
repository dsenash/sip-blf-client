import os
import struct
import tempfile
import unittest
import unittest.mock
import wave

from rtp_sender import RPTPacket, WAVStreamer


class QuietLog:
    """WAVStreamer prints progress; keep the test output clean."""

    def __call__(self, *args, **kwargs):
        pass


class FakeSocket:
    def __init__(self):
        self.packets = []

    def sendto(self, data, addr):
        self.packets.append(data)


def make_wav(path, seconds=0.2, rate=8000):
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x01" * int(rate * seconds))


class RPTPacketTest(unittest.TestCase):
    def test_header_is_twelve_bytes(self):
        packet = RPTPacket.build(0, 0, 1, b"payload")
        self.assertEqual(len(packet), 12 + len(b"payload"))

    def test_round_trips_the_header(self):
        parsed = RPTPacket.parse(RPTPacket.build(7, 160, 99, b"abc"))
        self.assertEqual(parsed["sequence"], 7)
        self.assertEqual(parsed["timestamp"], 160)
        self.assertEqual(parsed["ssrc"], 99)
        self.assertEqual(parsed["payload"], b"abc")

    def test_short_data_is_rejected(self):
        self.assertIsNone(RPTPacket.parse(b"\x80\x00\x00"))

    def test_sequence_wraps_instead_of_raising(self):
        # The sender increments seq for every packet and starts from a random
        # 16-bit value, so it crosses 65535 mid-call. struct.pack("!H") raised
        # struct.error there and killed the streaming thread.
        packet = RPTPacket.build(65536, 0, 1, b"x")
        self.assertEqual(RPTPacket.parse(packet)["sequence"], 0)

    def test_timestamp_wraps_instead_of_raising(self):
        packet = RPTPacket.build(0, 2**32, 1, b"x")
        self.assertEqual(RPTPacket.parse(packet)["timestamp"], 0)

    def test_payload_type_lands_in_the_type_bits(self):
        parsed = RPTPacket.parse(RPTPacket.build(0, 0, 1, b"", payload_type=8))
        self.assertEqual(parsed["payload_type"], 8)


class SeqWrapTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.wav = os.path.join(self.dir, "t.wav")
        make_wav(self.wav)

    def tearDown(self):
        os.remove(self.wav)
        os.rmdir(self.dir)

    def test_sending_across_the_wrap_stays_in_range(self):
        sock = FakeSocket()
        streamer = WAVStreamer(self.wav, {"sample_rate": 8000})
        streamer.seq = 65530
        with unittest.mock.patch("builtins.print", QuietLog()):
            self.assertTrue(streamer.send(sock, ("127.0.0.1", 9), duration=5))
        self.assertGreater(len(sock.packets), 6)
        seqs = [RPTPacket.parse(p)["sequence"] for p in sock.packets]
        for seq in seqs:
            self.assertLess(seq, 65536)
        self.assertEqual(seqs, [(65530 + i) % 65536 for i in range(len(seqs))])

    def test_initial_seq_is_within_range(self):
        for _ in range(50):
            streamer = WAVStreamer(self.wav, {"sample_rate": 8000})
            self.assertTrue(0 <= streamer.seq <= 65535)


if __name__ == "__main__":
    unittest.main()

class ClosedSocketTest(unittest.TestCase):
    """_finish() closes the RTP socket while these threads may still hold it.

    A remote BYE or a hangup during playback is the normal end of every call,
    so an unhandled OSError here printed a traceback on a routine path.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.wav = os.path.join(self.dir, "t.wav")
        make_wav(self.wav)

    def tearDown(self):
        os.remove(self.wav)
        os.rmdir(self.dir)

    def test_sender_survives_a_socket_closed_mid_stream(self):
        class ClosingSocket(FakeSocket):
            def sendto(self, data, addr):
                if len(self.packets) >= 3:
                    raise OSError(9, "Bad file descriptor")
                super().sendto(data, addr)

        sock = ClosingSocket()
        streamer = WAVStreamer(self.wav, {"sample_rate": 8000})
        with unittest.mock.patch("builtins.print", QuietLog()):
            result = streamer.send(sock, ("127.0.0.1", 9), duration=5)
        self.assertFalse(result)
        self.assertEqual(len(sock.packets), 3)

    def test_monitor_survives_a_socket_closed_before_it_starts(self):
        class DeadSocket:
            def settimeout(self, value):
                raise OSError(9, "Bad file descriptor")

            def getsockname(self):
                raise OSError(9, "Bad file descriptor")

        from rtp_sender import RTPMonitor
        monitor = RTPMonitor(DeadSocket(), {})
        with unittest.mock.patch("builtins.print", QuietLog()):
            monitor._monitor()  # must return instead of raising

    def test_monitor_survives_the_socket_closing_while_it_blocks(self):
        class VanishingSocket:
            def __init__(self):
                self.calls = 0

            def settimeout(self, value):
                pass

            def getsockname(self):
                return ("127.0.0.1", 10000)

            def recvfrom(self, size):
                self.calls += 1
                if self.calls > 1:
                    raise OSError(9, "Bad file descriptor")
                import socket as s
                raise s.timeout

        from rtp_sender import RTPMonitor
        sock = VanishingSocket()
        monitor = RTPMonitor(sock, {})
        monitor.stop()
        with unittest.mock.patch("builtins.print", QuietLog()):
            monitor._monitor()  # must return instead of raising


class HeaderBitLayoutTest(unittest.TestCase):
    """Byte 0 is V(2) P(1) X(1) CC(4) and must not carry the payload type.

    Packing the payload type into byte 0 set CC=8 for PT=8, so a receiver
    skipped 32 bytes of CSRC, and set V=3 P=1 for PT=101. The existing
    round-trip test cannot catch this because parse() ignores byte 0.
    """

    def test_byte_zero_is_version_two_with_no_csrc(self):
        packet = RPTPacket.build(0, 0, 1, b"", payload_type=0)
        self.assertEqual(packet[0], 0x80)
        self.assertEqual(packet[0] >> 6, 2)      # version
        self.assertEqual(packet[0] & 0x0F, 0)    # CC

    def test_byte_zero_is_independent_of_payload_type(self):
        for pt in (0, 8, 101):
            packet = RPTPacket.build(0, 0, 1, b"", payload_type=pt)
            self.assertEqual(packet[0], 0x80, f"payload type {pt}")

    def test_byte_one_carries_the_payload_type(self):
        for pt in (0, 8, 101):
            packet = RPTPacket.build(0, 0, 1, b"", payload_type=pt)
            self.assertEqual(packet[1] & 0x7F, pt)
            self.assertEqual(packet[1] >> 7, 0)   # marker bit clear
