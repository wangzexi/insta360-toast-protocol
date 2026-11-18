import argparse
import asyncio
import ipaddress
from pathlib import Path
import shlex
import struct
import sys

from bleak import BleakClient, BleakScanner


WRITE_UUID = "0000ce81-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ce82-0000-1000-8000-00805f9b34fb"
BATTERY_COMMAND = 0x1028
HOTSPOT_COMMAND = 0x1001
CLEAR_WIFI_COMMAND = 0x101A
HEADER = struct.Struct("<3sHHH")
SIGNATURE = bytes.fromhex("feef01")


def encode_request(command, sequence):
    return HEADER.pack(SIGNATURE, command, 0, 2) + struct.pack("<H", sequence)


def decode_response(frame, command, sequence):
    if len(frame) < HEADER.size + 3:
        return None
    signature, reply_command, _, size = HEADER.unpack_from(frame)
    reply_sequence, status = struct.unpack_from("<HB", frame, HEADER.size)
    if (signature, reply_command, reply_sequence) != (SIGNATURE, command, sequence):
        return None
    if len(frame) != HEADER.size + size:
        return None
    return status, bytes(frame[HEADER.size + 3 :])


def decode_hotspot(payload):
    if len(payload) != 83 or payload[0] != 0:
        raise ValueError("Unexpected hotspot response layout")
    ssid, password, gateway, port = struct.unpack("<32s32s16sH", payload[1:])
    return (
        ssid.split(b"\0", 1)[0].decode(),
        password.split(b"\0", 1)[0].decode(),
        gateway.split(b"\0", 1)[0].decode(),
        port,
    )


async def check_android_tcp(serial, ssid, password, gateway, port, action=None):
    async def shell(*args):
        process = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, "shell", shlex.join(args),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait()

    if await shell("cmd", "wifi", "connect-network", ssid, "wpa2", password):
        raise RuntimeError("Android rejected the Wi-Fi join request")
    if action is not None:
        network = ipaddress.ip_network(f"{gateway}/24", strict=False)
        deadline = asyncio.get_running_loop().time() + 35
        while asyncio.get_running_loop().time() < deadline:
            address_check = await asyncio.create_subprocess_exec(
                "adb", "-s", serial, "shell", "ip -4 -o addr show dev wlan0",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await address_check.communicate()
            addresses = [
                ipaddress.ip_interface(field.decode()).ip
                for field in output.split() if b"/" in field
            ]
            if any(address in network for address in addresses):
                print("Android joined the Toast hotspot", flush=True)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name("insta360-toast-upload.py")),
                    "--host", gateway, "--port", str(port), "--android-serial", serial,
                    *action,
                )
                if await process.wait():
                    raise RuntimeError("Toast TCP operation failed")
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("Android did not acquire an address on the Toast hotspot")
    for attempt in range(6):
        await asyncio.sleep(2)
        if await shell("nc", "-z", "-w", "2", gateway, str(port)) == 0:
            print("Android reached the Toast TCP endpoint", flush=True)
            return
    raise TimeoutError("Android did not reach the Toast TCP endpoint")


async def probe(name, command, android_serial=None, scan_timeout=12, action=None):
    device = await BleakScanner.find_device_by_filter(
        lambda device, advertisement: name.casefold()
        == (advertisement.local_name or device.name or "").casefold(),
        timeout=scan_timeout,
    )
    if device is None:
        raise SystemExit(f"{name} is not advertising")
    reply = asyncio.get_running_loop().create_future()
    sequence = 4

    def on_notify(sender, frame):
        decoded = decode_response(frame, command, sequence)
        if decoded is not None and not reply.done():
            reply.set_result(decoded)

    async with BleakClient(device, timeout=10) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await client.write_gatt_char(
            WRITE_UUID, encode_request(command, sequence), response=False
        )
        status, payload = await asyncio.wait_for(reply, timeout=5)
        if status:
            raise SystemExit(f"Toast returned status {status}")
        if command == HOTSPOT_COMMAND:
            ssid, password, gateway, port = decode_hotspot(payload)
            print(f"Toast hotspot: {ssid}, TCP endpoint: {gateway}:{port}", flush=True)
            if android_serial:
                await check_android_tcp(android_serial, ssid, password, gateway, port, action)
        elif command == CLEAR_WIFI_COMMAND:
            if payload:
                raise ValueError("Unexpected clear-Wi-Fi response")
            print("Toast acknowledged clearing its home Wi-Fi configuration", flush=True)
        else:
            if len(payload) != 2 or payload[0] > 100:
                raise ValueError("Unexpected battery response")
            print(f"Battery: {payload[0]}%", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Insta360 Toast BLE probe")
    parser.add_argument("name", help="Exact advertised Bluetooth name")
    parser.add_argument("--hotspot", action="store_true", help="Request the Toast hotspot")
    parser.add_argument("--android-serial", help="Use this ADB device to check hotspot TCP")
    parser.add_argument("--scan-timeout", type=float, default=12)
    image_group = parser.add_mutually_exclusive_group()
    image_group.add_argument("--upload-test", action="store_true")
    image_group.add_argument("--image", nargs="+", help="Upload photos; requires Pillow")
    image_group.add_argument("--list", action="store_true")
    image_group.add_argument("--self-test", action="store_true")
    image_group.add_argument("--tcp", nargs=argparse.REMAINDER, help="Pass options to the TCP client")
    parser.add_argument("--battery", action="store_true")
    parser.add_argument("--clear-wifi", action="store_true", help="Remove the Toast home Wi-Fi configuration")
    parser.add_argument("--confirm-clear-wifi", action="store_true")
    args = parser.parse_args()
    action = (["--image", *args.image] if args.image else ["--list"] if args.list else
              ["--self-test"] if args.self_test else [] if args.upload_test else args.tcp)
    if args.tcp == [] or (action is not None and not args.android_serial):
        parser.error("TCP actions require an operation and --android-serial")
    if args.battery and (args.hotspot or action is not None):
        parser.error("--battery cannot be combined with hotspot or TCP operations")
    if args.clear_wifi != args.confirm_clear_wifi:
        parser.error("--clear-wifi requires --confirm-clear-wifi")
    if args.clear_wifi and (args.battery or args.hotspot or args.android_serial or action is not None):
        parser.error("--clear-wifi must be used on its own")
    command = (CLEAR_WIFI_COMMAND if args.clear_wifi else
               BATTERY_COMMAND if args.battery or not (args.hotspot or args.android_serial) else
               HOTSPOT_COMMAND)
    asyncio.run(probe(args.name, command, args.android_serial, args.scan_timeout, action))
