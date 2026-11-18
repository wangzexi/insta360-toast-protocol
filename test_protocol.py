import runpy
import struct
import unittest
import zlib
from pathlib import Path


protocol = runpy.run_path(str(Path(__file__).with_name("insta360-toast-upload.py")))


class ProtocolTests(unittest.TestCase):
    def test_crc_and_chunk(self):
        self.assertEqual(protocol["crc16"](b"123456789"), 0x906E)
        self.assertEqual(protocol["encode_chunk"](2, b"123456789"),
                         bytes.fromhex("0200000009006e90") + b"123456789")

    def test_panel_round_trip(self):
        pattern = protocol["test_pattern"]()
        self.assertEqual(len(pattern), 172800)
        self.assertEqual(zlib.decompress(protocol["encode_panel"](pattern)), pattern)

    def test_playback_vector(self):
        self.assertEqual(protocol["encode_playback"](72, "pin"),
                         bytes.fromhex("010200e01002010007"))

    def test_segmented_playlist(self):
        entries = [f"{index:032x}".encode() for index in range(8)]
        payload = bytes([len(entries)]) + b"".join(entries)
        frame = protocol["encode_frame"]

        class Connection:
            def sendall(self, request):
                assert request == frame(0x100A, 0, struct.pack("<H", 1))
                self.data = (frame(0x100A, 0x4000, b"\x01\x00\x00" + payload[:228])
                             + frame(0x100A, 0xC001, payload[228:]))

            def recv(self, size):
                size = min(size, 5)
                part, self.data = self.data[:size], self.data[size:]
                return part

        self.assertEqual(protocol["ToastConnection"](Connection()).playlist(), entries)


if __name__ == "__main__":
    unittest.main()
