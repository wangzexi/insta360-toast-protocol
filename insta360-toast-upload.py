import argparse
import hashlib
import json
import re
import secrets
import shlex
import socket
import struct
import subprocess
import time
import zlib


FRAME_HEADER = struct.Struct("<3sHHH")
FRAME_MAGIC = b"\xfe\xef\x01"
CHUNK_DATA_SIZE = 4079
PANEL_BYTES = 720 * 480 // 2
PALETTE_CODES = frozenset((0, 1, 2, 3, 5, 6))
PLAYBACK_HOURS = (1, 4, 8, 12, 24, 72)
PLAYBACK_MODES = {"upload-date": 4, "manual": 5, "random": 6, "pin": 7}


def crc16(data):
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8408 if value & 1 else 0)
    return value ^ 0xFFFF


def encode_frame(command, flags, body):
    return FRAME_HEADER.pack(FRAME_MAGIC, command, flags, len(body)) + body


def encode_panel(packed_pixels):
    if len(packed_pixels) != PANEL_BYTES:
        raise ValueError("Expected 720 × 480 packed 4-bit pixels")
    if any(value >> 4 not in PALETTE_CODES or value & 15 not in PALETTE_CODES
           for value in packed_pixels):
        raise ValueError("Invalid panel palette code")
    return zlib.compress(packed_pixels)


def encode_chunk(index, data):
    if not 0 < len(data) <= CHUNK_DATA_SIZE:
        raise ValueError("Invalid upload chunk size")
    return struct.pack("<IHH", index, len(data), crc16(data)) + data


def chunk_bodies(data):
    for index, offset in enumerate(range(0, len(data), CHUNK_DATA_SIZE)):
        yield encode_chunk(index, data[offset : offset + CHUNK_DATA_SIZE])


def encode_media_descriptor(media_id, compressed_size):
    return struct.pack("<I", int(media_id[:8], 16)) + b"\x01\x00\xa3\x02\x00" + struct.pack(
        "<I", compressed_size
    )


def encode_playback(hours, mode):
    if hours not in PLAYBACK_HOURS or mode not in PLAYBACK_MODES:
        raise ValueError("Unsupported playback interval or mode")
    return struct.pack("<BHHBBBB", 1, 2, hours * 60, 2, 1, 0, PLAYBACK_MODES[mode])


class ToastConnection:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0
        self.chunk_sequence = 0

    def receive(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.connection.recv(size - len(data))
            if not part:
                raise EOFError("Toast closed the TCP connection")
            data.extend(part)
        return bytes(data)

    def reply_frame(self, expected):
        while True:
            magic, command, flags, size = FRAME_HEADER.unpack(self.receive(FRAME_HEADER.size))
            if magic != FRAME_MAGIC:
                raise ValueError("Invalid Toast frame signature")
            body = self.receive(size)
            if command == expected:
                return flags, body
            if command != 0x1019:
                raise ValueError(f"Unexpected Toast command {command:#06x}")

    def reply(self, expected):
        return self.reply_frame(expected)[1]

    def request(self, command, payload=b""):
        self.sequence += 1
        sequence = struct.pack("<H", self.sequence)
        self.connection.sendall(encode_frame(command, 0, sequence + payload))
        flags, response = self.reply_frame(command)
        if len(response) < 3 or response[:2] != sequence or response[2] != 0:
            raise RuntimeError(f"Toast rejected or mismatched command {command:#06x}")
        result = response[3:]
        if command == 0x100A and result and len(result) < 1 + result[0] * 32 and flags:
            expected_size = 1 + result[0] * 32
            while len(result) < expected_size:
                continuation_flags, continuation = self.reply_frame(command)
                if not continuation_flags or not continuation:
                    raise ValueError("Unexpected playlist continuation")
                if continuation.startswith(sequence + b"\0"):
                    continuation = continuation[3:]
                if not re.fullmatch(rb"[0-9a-f]+", continuation):
                    raise ValueError("Invalid playlist continuation")
                result += continuation
        return result

    def playlist(self):
        payload = self.request(0x100A)
        if not payload or len(payload) != 1 + payload[0] * 32:
            count = payload[0] if payload else None
            widths = [len(item) for item in re.findall(rb"[0-9a-f]+", payload[1:])]
            raise ValueError(f"Unexpected playlist layout: count={count}, bytes={len(payload)}, hex runs={widths}")
        entries = [payload[offset : offset + 32] for offset in range(1, len(payload), 32)]
        if any(not re.fullmatch(rb"[0-9a-f]{32}", entry) for entry in entries):
            raise ValueError("Unexpected playlist entry format")
        return entries

    def send_image(self, pixels, media_id, epoch, transfer_id):
        compressed = encode_panel(pixels)
        chunks = list(chunk_bodies(compressed))
        epoch = int(time.time())
        transfer_base = secrets.randbits(31)
        file_key = struct.pack("<BI", 2, transfer_id & 0xFFFFFFFF)
        self.request(0x1007, media_id + struct.pack("<QQ", epoch, epoch))
        self.request(0x102A, encode_media_descriptor(media_id, len(compressed)))
        self.request(0x0020, file_key + struct.pack(
            "<IHHI", len(compressed), crc16(compressed), 50, len(chunks)
        ))
        for body in chunks:
            self.chunk_sequence += 1
            self.connection.sendall(encode_frame(0x0021, self.chunk_sequence, body))
        if self.reply(0x0021) != struct.pack("<II", len(chunks) - 1, 0):
            raise RuntimeError("Toast did not acknowledge the complete file")
        self.request(0x0022, file_key)

    def set_order(self, ordered):
        self.request(0x100B, bytes([len(ordered)]) + b"".join(ordered))
        self.request(0x100D)
        if self.playlist() != ordered:
            raise RuntimeError("Toast playlist readback did not match")

    def reorder(self, ordered):
        prior = self.playlist()
        if sorted(ordered) != sorted(prior):
            raise ValueError("Reorder requires every existing ID exactly once")
        self.set_order(ordered)

    def delete(self, media_ids):
        prior = self.playlist()
        targets = set(media_ids)
        if len(targets) != len(media_ids) or not targets.issubset(prior):
            raise ValueError("Delete requires unique existing media IDs")
        self.request(0x1009, struct.pack("<I", len(media_ids)) + b"".join(media_ids))
        if self.playlist() != [entry for entry in prior if entry not in targets]:
            raise RuntimeError("Toast deletion readback did not match")

    def show(self, media_id):
        self.request(0x101D, media_id)
        deadline = time.monotonic() + 45
        while True:
            observed = self.request(0x101C)
            if observed == media_id:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Toast did not report the image as current")
            time.sleep(1)

    def upload_many(self, images, select=True):
        media_ids = [hashlib.md5(pixels).hexdigest().encode("ascii") for pixels in images]
        prior = self.playlist()
        if len(set(media_ids + prior)) != len(media_ids) + len(prior) or len(prior) + len(media_ids) > 30:
            raise ValueError("Duplicate image or local playlist capacity exceeded")
        epoch = int(time.time())
        self.request(0x1003, struct.pack("<H", len(images)))
        for index, (pixels, media_id) in enumerate(zip(images, media_ids)):
            self.send_image(pixels, media_id, epoch, transfer_base + index)
        self.request(0x1027)
        current = self.playlist()
        self.set_order(list(dict.fromkeys([*media_ids, *prior, *current])))
        if select:
            self.show(media_ids[0])
        return media_ids

    def playback(self, hours, mode):
        self.request(0x100F, encode_playback(hours, mode))


def media_id_arg(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise argparse.ArgumentTypeError("Expected a 32-character lowercase hex media ID")
    return value.encode("ascii")


def test_pattern():
    table = (0x00, 0x11, 0x22, 0x33, 0x55, 0x66)
    return b"".join(bytes([color]) * 360 * 80 for color in table)


def photo_pixels(path):
    from PIL import Image, ImageOps

    colors = ((0, 0, 0), (255, 255, 255), (255, 255, 0),
              (255, 0, 0), (0, 0, 255), (0, 128, 0))
    codes = (0, 1, 2, 3, 5, 6)
    palette = Image.new("P", (1, 1))
    palette.putpalette([channel for color in colors for channel in color] +
                       list(colors[0]) * (256 - len(colors)))
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    scaled = ImageOps.fit(image, (720, 480), method=Image.Resampling.LANCZOS)
    indices = iter(scaled.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG).tobytes())
    return bytes(codes[high] << 4 | codes[low] for high, low in zip(indices, indices))


class PipeConnection:
    def __init__(self, process):
        self.process = process

    def recv(self, size):
        return self.process.stdout.read1(size)

    def sendall(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Control a Toast over TCP")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--image", nargs="+", help="Upload one or more photos; requires Pillow")
    action.add_argument("--list", action="store_true")
    action.add_argument("--show", type=media_id_arg)
    action.add_argument("--delete", type=media_id_arg, nargs="+")
    action.add_argument("--reorder", type=media_id_arg, nargs="+")
    action.add_argument("--playback", nargs=2, metavar=("HOURS", "MODE"),
                        help="Hours: 1, 4, 8, 12, 24, 72. Mode: upload-date, manual, random, pin")
    action.add_argument("--self-test", action="store_true", help="Use only generated fixtures; restore the list on success")
    action.add_argument("--acceptance", action="store_true",
                        help="Run image self-test plus playback writes; restore 1h/manual")
    parser.add_argument("--yes", action="store_true", help="Confirm deletion of the specified IDs")
    parser.add_argument("--resume-fixtures", type=media_id_arg, nargs=4, metavar="ID")
    parser.add_argument("--restore-current", type=media_id_arg)
    parser.add_argument("--host", default="192.168.4.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--android-serial")
    args = parser.parse_args()
    if args.resume_fixtures and not args.acceptance:
        parser.error("--resume-fixtures requires --acceptance")
    if args.delete and not args.yes:
        parser.error("--delete requires --yes")
    if args.playback:
        try:
            hours, mode = int(args.playback[0]), args.playback[1]
            encode_playback(hours, mode)
        except ValueError:
            parser.error("--playback requires a supported interval and mode; see --help")

    def run(connection):
        toast = ToastConnection(connection)
        if args.list:
            current = toast.request(0x101C)
            print(json.dumps({"playlist": [item.decode() for item in toast.playlist()],
                              "current": current.decode() if len(current) == 32 else None}))
        elif args.show:
            if args.show not in toast.playlist():
                raise ValueError("Image is not in the playlist")
            toast.show(args.show)
            print("Display selection verified")
        elif args.delete:
            toast.delete(args.delete)
            print("Deletion verified")
        elif args.reorder:
            toast.reorder(args.reorder)
            print("Playlist order verified")
        elif args.playback:
            toast.playback(hours, mode)
            print(f"Playback settings acknowledged: {hours}h, {mode} (not read back)")
        elif args.self_test or args.acceptance:
            battery = toast.request(0x1028)
            if len(battery) == 2 and battery[0] <= 100:
                print(f"Battery: {battery[0]}% (TCP)", flush=True)
            prior = toast.playlist()
            current = args.restore_current or toast.request(0x101C)
            if args.resume_fixtures:
                fixtures_to_remove = set(args.resume_fixtures)
                if len(fixtures_to_remove) != 4 or not fixtures_to_remove.issubset(prior):
                    raise ValueError("Resume requires all four distinct test IDs in the playlist")
                prior = [entry for entry in prior if entry not in fixtures_to_remove]
                if current not in prior:
                    raise ValueError("Resume requires the original current image")
            pattern = test_pattern()
            salt = time.time_ns()
            fixtures = [
                bytes((0x00, 0x11, 0x22, 0x33)[(salt >> shift) & 3] for shift in range(0, 64, 2))
                + pattern[33:] + bytes([marker])
                for marker in (0x11, 0x22, 0x33, 0x55)
            ]
            if not args.resume_fixtures and len(prior) + len(fixtures) > 30:
                raise ValueError("Not enough free playlist slots for the test")
            print(json.dumps({
                "original_playlist": [item.decode() for item in prior],
                "fixture_ids": ([item.decode() for item in args.resume_fixtures]
                                if args.resume_fixtures else
                                [hashlib.md5(image).hexdigest() for image in fixtures]),
            }), flush=True)
            if args.resume_fixtures:
                single, ids = args.resume_fixtures[:1], args.resume_fixtures[1:]
                print("Resumed with four existing test images", flush=True)
            else:
                single = toast.upload_many(fixtures[:1], select=args.acceptance)
                print("Single upload verified", flush=True)
                ids = toast.upload_many(fixtures[1:], select=False)
                print("Batch upload verified", flush=True)
            if args.acceptance:
                failed = []
                for media_id in ids:
                    try:
                        toast.show(media_id)
                    except RuntimeError as error:
                        failed.append((media_id.decode(), str(error)))
                        print(f"Selection failed for {media_id.decode()}: {error}", flush=True)
                if not failed:
                    print("Every batch image selected and read back", flush=True)
            toast.reorder([*reversed(ids), *single, *prior])
            print("Reorder verified", flush=True)
            toast.delete(ids[:1])
            print("Single deletion verified", flush=True)
            toast.delete([*ids[1:], *single])
            if toast.playlist() != prior:
                raise RuntimeError("Original playlist was not restored")
            print("Batch deletion verified; original list restored", flush=True)
            if args.acceptance:
                try:
                    for interval in PLAYBACK_HOURS:
                        toast.playback(interval, "manual")
                        print(f"Interval {interval}h acknowledged", flush=True)
                    for play_mode in PLAYBACK_MODES:
                        toast.playback(1, play_mode)
                        print(f"Mode {play_mode} acknowledged", flush=True)
                finally:
                    toast.playback(1, "manual")
                    print("1h/manual restore acknowledged", flush=True)
            if current in prior:
                toast.show(current)
                print("Original current image restored", flush=True)
            if args.acceptance and failed:
                raise RuntimeError("One or more batch images failed selection readback")
        else:
            images = [photo_pixels(path) for path in args.image] if args.image else [test_pattern()]
            print(*(item.decode() for item in toast.upload_many(images)), sep="\n")
    if args.android_serial:
        command = shlex.join(["nc", "-w", "5", "-W", "10", args.host, str(args.port)])
        process = subprocess.Popen(
            ["adb", "-s", args.android_serial, "shell", "-T", command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        try:
            run(PipeConnection(process))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()
    else:
        with socket.create_connection((args.host, args.port), timeout=8) as connection:
            run(connection)
