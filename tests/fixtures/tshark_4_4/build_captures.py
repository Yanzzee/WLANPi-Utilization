"""Generate original synthetic radiotap/802.11 captures; no real network data."""
from pathlib import Path
import struct

AP = bytes.fromhex('020000000010')
CLIENT = bytes.fromhex('020000000020')
DEST = bytes.fromhex('020000000030')
GROUP = b'\xff' * 6


def tag(number, value):
    return bytes([number, len(value)]) + value


def header(fc, ra, ta=AP, third=AP, sequence=1):
    return struct.pack('<HH', fc, 0) + ra + ta + third + struct.pack('<H', sequence << 4)


def capture(frequency, operation):
    # Radiotap CHANNEL and DBM_ANTSIGNAL, aligned channel field at offset 8.
    radio = struct.pack('<BBHIHHb', 0, 0, 13, 0x28, frequency, 0x0040, -47)
    beacon = (header(0x80, GROUP) + struct.pack('<QHH', 0, 100, 0x0411)
              + tag(0, b'Trixie-Test') + tag(1, b'\x8c')
              + tag(11, struct.pack('<HBH', 12, 128, 42)) + operation)
    frames = [
        beacon,
        header(0x0908, AP, CLIENT, DEST, 2) + b'\xaa\xaa\x03\x00\x00\x00\x08\x00',
        header(0x0908, AP, CLIENT, DEST, 2) + b'\xaa\xaa\x03\x00\x00\x00\x08\x00',
        header(0x0108, AP, CLIENT, DEST, 3) + b'\xaa\xaa\x03\x00\x00\x00\x08\x00',
        header(0x0808, GROUP, AP, AP, 4),
        header(0x0840, GROUP, CLIENT, GROUP, 5) + tag(0, b''),
        struct.pack('<HH', 0x08d4, 0) + CLIENT,
        header(0x08e0, CLIENT, AP, AP, 6) + b'\x7f\x00',
        header(0x08b0, CLIENT, AP, AP, 7) + struct.pack('<HHH', 0, 1, 0),
    ]
    data = bytearray(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 127))
    for index, frame in enumerate(frames):
        packet = radio + frame
        data += struct.pack('<IIII', 1700000000, index * 100000, len(packet), len(packet)) + packet
    return data


def he_operation(primary, width, center):
    # HE Operation extension 36: 6 GHz operation present (parameter bit 17).
    return tag(255, b'\x24\x00\x00\x02\x00\xff\xff'
               + bytes([primary, width, center, 0, 0]))


def main():
    root = Path(__file__).parent
    ht = tag(61, bytes([36, 1]) + bytes(20))
    cases = {
        'ht40': (2412, tag(3, b'\x01') + tag(61, bytes([1, 1]) + bytes(20))),
        'vht160': (5180, ht + tag(192, bytes([1, 42, 50, 255, 255]))),
        'he80': (5975, he_operation(5, 2, 7)),
        'eht320': (5955, he_operation(1, 2, 7)
                   + tag(255, bytes([106, 3, 255, 255, 255, 255, 4, 31, 0, 4, 0]))),
    }
    for name, (frequency, operation) in cases.items():
        (root / f'{name}.pcap').write_bytes(capture(frequency, operation))


if __name__ == '__main__':
    main()
