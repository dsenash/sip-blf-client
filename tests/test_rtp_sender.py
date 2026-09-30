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