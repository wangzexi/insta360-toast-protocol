import argparse
from collections import defaultdict

from scapy.all import IP, TCP, PcapReader


def streams(packets, host, port):
    segments = defaultdict(list)
    for packet in packets:
        if not packet.haslayer(IP) or not packet.haslayer(TCP):
            continue
        ip, tcp = packet[IP], packet[TCP]
        if (ip.dst, tcp.dport) == (host, port):
            key = (ip.src, tcp.sport, "upload")
        elif (ip.src, tcp.sport) == (host, port):
            key = (ip.dst, tcp.dport, "reply")
        else:
            continue
        payload = bytes(tcp.payload)
        if payload:
            segments[key].append((tcp.seq, payload))
    return segments


def assemble(segments):
    result = bytearray()
    start = min(sequence for sequence, _ in segments)
    for sequence, payload in sorted(segments):
        offset = sequence - start
        if offset > len(result):
            raise ValueError(f"TCP capture has a gap of {offset - len(result)} bytes")
        overlap = min(len(payload), len(result) - offset)
        if result[offset : offset + overlap] != payload[:overlap]:
            raise ValueError("Conflicting TCP retransmissions")
        result.extend(payload[overlap:])
    return bytes(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize Toast TCP capture streams")
    parser.add_argument("capture")
    parser.add_argument("--host", default="192.168.4.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    with PcapReader(args.capture) as packets:
        captured = streams(packets, args.host, args.port)
    if not captured:
        raise SystemExit("No Toast TCP payload captured")
    for (address, port, direction), segments in sorted(captured.items()):
        payload = assemble(segments)
        print(f"{address}:{port} {direction}: {len(payload)} bytes")
