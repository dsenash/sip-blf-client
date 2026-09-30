import socket
import struct
import time
import wave
import threading
import array
import random


class RPTPacket:
    @staticmethod
    def build(seq, timestamp, ssrc, payload, payload_type=0):
        # seq is 16-bit and timestamp 32-bit on the wire, so both must wrap.
        # The sender counts past the limit mid-call, and struct.pack raises
        # there rather than truncating, which killed the streaming thread.
        #
        # Byte 0 is V(2) P(1) X(1) CC(4): always 0x80 for plain PCMU, and it
        # must not carry the payload type or CC becomes a CSRC count. Byte 1
        # is M(1) PT(7).
        header = struct.pack("!BBHII", 0x80, payload_type & 0x7F,
                             seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)
        return header + payload

    @staticmethod
    def parse(data):
        if len(data) < 12:
            return None
        first_byte, pt_byte, seq, timestamp, ssrc = struct.unpack("!BBHII", data[:12])
        return {
            "sequence": seq,
            "timestamp": timestamp,
            "ssrc": ssrc,
            "payload_type": pt_byte & 0x7F,
            "payload": data[12:],
        }


class WAVStreamer:
    def __init__(self, wav_path, config):
        self.wav_path = wav_path
        self.config = config
        self.sample_rate = config.get("sample_rate", 8000)
        self.ssrc = ((int(time.time()) << 4) | random.randint(0, 15)) & 0xFFFFFFFF
        self.seq = random.randint(0, 65535)
        self.timestamp = random.randint(0, 2**31)

    def load_wav_data(self):
        try:
            with wave.open(self.wav_path, "rb") as wav:
                params = wav.getparams()
                frames = wav.readframes(wav.getnframes())
                framerate = wav.getframerate()
                channels = wav.getnchannels()
                sampwidth = wav.getsampwidth()
            return frames, framerate, channels, sampwidth, params
        except Exception as e:
            print(f"[RTP] Could not read WAV file '{self.wav_path}': {e}")
            return None

    def send(self, rtp_socket, dest_addr, duration):
        wav_data = self.load_wav_data()
        if not wav_data:
            return False

        frames, framerate, channels, sampwidth, params = wav_data
        print(f"[RTP] Sending WAV: {self.wav_path}, framerate={framerate}, channels={channels}, duration={params.nframes / framerate:.2f}s")

        samples = self._read_as_16bit_mono(frames, channels, sampwidth)
        samples = self._resample(samples, framerate, self.sample_rate)

        payload_size = int(self.sample_rate * 0.02)
        start_time = time.time()
        sent_packets = 0

        for i in range(0, len(samples) - payload_size + 1, payload_size):
            chunk = samples[i:i + payload_size]
            payload = self._linear_to_ulaw(chunk)
            packet = RPTPacket.build(self.seq, self.timestamp, self.ssrc, payload, payload_type=0)

            try:
                rtp_socket.sendto(packet, dest_addr)
            except OSError as e:
                # The session closes this socket on hangup or a remote BYE,
                # which is the normal end of a call and can land mid-stream.
                # The WAV did not play to the end, so this is a failed send
                # even though some packets went out.
                print(f"[RTP] Socket closed, stopping stream: {e}")
                return False
            self.seq += 1
            self.timestamp += payload_size
            sent_packets += 1

            next_time = start_time + ((sent_packets + 1) * payload_size / self.sample_rate)
            sleep_time = max(0, next_time - time.time())
            time.sleep(sleep_time)

            if time.time() - start_time > duration:
                break

        print(f"[RTP] Sent {sent_packets} RTP packets")
        return True

    def _read_as_16bit_mono(self, raw_bytes, channels, sampwidth):
        if channels > 1:
            mixed = []
            for i in range(0, len(raw_bytes) - channels * sampwidth + 1, channels * sampwidth):
                ch_bytes = raw_bytes[i:i + channels * sampwidth]
                vals = []
                for c in range(channels):
                    if sampwidth == 2:
                        vals.append(struct.unpack_from("<h", ch_bytes, c * sampwidth)[0])
                    elif sampwidth == 1:
                        vals.append(struct.unpack_from("<B", ch_bytes, c * sampwidth)[0] - 128)
                    else:
                        vals.append(0)
                mixed.append(int(sum(vals) // len(vals)))
            return mixed

        if sampwidth == 2:
            arr = array.array("h")
            arr.frombytes(raw_bytes)
            return list(arr)
        elif sampwidth == 1:
            return [b - 128 for b in raw_bytes]
        else:
            n = len(raw_bytes) // sampwidth
            return [0] * n

    def _resample(self, samples, src_rate, dst_rate):
        if src_rate == dst_rate:
            return samples
        ratio = src_rate / dst_rate
        return [samples[min(int(i * ratio), len(samples) - 1)] for i in range(int(len(samples) / ratio))]

    @staticmethod
    def _linear_to_ulaw(samples):
        result = bytearray()
        for sample in samples:
            result.append(WAVStreamer.encode_ulaw(sample))
        return bytes(result)

    @staticmethod
    def encode_ulaw(sample):
        BIAS = 0x84
        CLIP = 32635

        sign = 0
        if sample < 0:
            sample = CLIP if sample < -CLIP else -sample
            sign = 0x80
        else:
            sample = CLIP if sample > CLIP else sample

        sample = sample + BIAS
        exponent = 7
        for shift in range(7, -1, -1):
            if sample & (0x4000 << (7 - shift)):
                break

        mantissa = (sample >> (exponent + 3)) & 0x0F
        ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
        return ulaw


class RTPMonitor:
    def __init__(self, rtp_socket, config):
        self.rtp_socket = rtp_socket
        self.config = config
        self.running = True
        self.packet_count = 0
        self.last_packet_time = time.time()
        self.last_seq = None

    def stop(self):
        self.running = False

    def start(self):
        thread = threading.Thread(target=self._monitor, daemon=True)
        thread.start()
        return thread

    def _monitor(self):
        try:
            local_port = self.rtp_socket.getsockname()[1]
        except OSError:
            local_port = "?"
        try:
            self.rtp_socket.settimeout(1)
        except OSError:
            # The session may already have closed the socket between
            # monitor.start() and this thread getting scheduled.
            return
        while self.running:
            try:
                data, addr = self.rtp_socket.recvfrom(65535)
                packet = RPTPacket.parse(data)
                if packet:
                    self.packet_count += 1
                    self.last_packet_time = time.time()
                    if self.last_seq is not None and packet["sequence"] != (self.last_seq + 1) & 0xFFFF:
                        print(f"  [RTP] Sequence gap: {self.last_seq} -> {packet['sequence']}")
                    self.last_seq = packet["sequence"]
                    print(f"  [RTP] RX {addr[0]}:{addr[1]} -> local:{local_port} seq={packet['sequence']} ts={packet['timestamp']} pt={packet['payload_type']} len={len(packet['payload'])}")
            except socket.timeout:
                continue
            except OSError:
                break
