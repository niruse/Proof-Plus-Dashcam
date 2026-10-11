"""A minimal MPEG-TS muxer for one H.264 stream.

go2rtc reads the RTSP feed as MPEG-TS: unlike a bare H.264 byte stream it
carries a timestamp with every frame, so the NVR records at the pace the frames
were taken rather than the pace they happened to arrive.

Adapted from the muxer the CuboAI integration feeds go2rtc with: PAT and PMT
repeated so a reader locks on quickly, one PES per access unit with its PTS,
a PCR on the video PID, and the random-access flag on keyframes.
"""
from __future__ import annotations

_PID_PAT = 0x0000
_PID_PMT = 0x1000
_PID_VIDEO = 0x0100
_STREAM_TYPE_H264 = 0x1B
_STREAM_ID_VIDEO = 0xE0
_PSI_INTERVAL_MS = 100


def _crc32_mpeg(data: bytes) -> int:
    """MPEG-2 PSI CRC-32 (poly 0x04C11DB7, MSB first, no final xor)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def _pts_field(pts: int) -> bytes:
    """The 5-byte PTS of a PTS-only PES header."""
    pts &= 0x1FFFFFFFF
    return bytes(
        (
            0x20 | (((pts >> 30) & 0x07) << 1) | 0x01,
            (pts >> 22) & 0xFF,
            (((pts >> 15) & 0x7F) << 1) | 0x01,
            (pts >> 7) & 0xFF,
            ((pts & 0x7F) << 1) | 0x01,
        )
    )


def _pcr_field(pcr: int) -> bytes:
    """A 6-byte PCR: 33-bit 90 kHz base, reserved bits, zero extension."""
    pcr &= 0x1FFFFFFFF
    return bytes(
        (
            (pcr >> 25) & 0xFF,
            (pcr >> 17) & 0xFF,
            (pcr >> 9) & 0xFF,
            (pcr >> 1) & 0xFF,
            ((pcr & 0x01) << 7) | 0x7E,
            0x00,
        )
    )


def _psi_packet(pid: int, table: bytes, counter: int) -> bytes:
    """One 188-byte packet holding a short PSI section."""
    section = table + _crc32_mpeg(table).to_bytes(4, "big")
    payload = b"\x00" + section
    payload += b"\xff" * (184 - len(payload))
    header = bytes((0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10 | counter))
    return header + payload


class TSMuxer:
    """Wraps timestamped H.264 access units into a 188-byte packet stream."""

    def __init__(self) -> None:
        self._counters = {_PID_PAT: 0, _PID_PMT: 0, _PID_VIDEO: 0}
        self._last_psi_ms: int | None = None

    def _bump(self, pid: int) -> int:
        counter = self._counters[pid]
        self._counters[pid] = (counter + 1) & 0x0F
        return counter

    def _pat(self) -> bytes:
        # transport_stream_id 1, version 0/current, section 0 of 0, then
        # program 1 -> PMT PID.
        body = bytes((0x00, 0x01, 0xC1, 0x00, 0x00))
        body += bytes((0x00, 0x01, 0xE0 | (_PID_PMT >> 8), _PID_PMT & 0xFF))
        length = len(body) + 4
        table = bytes((0x00, 0xB0 | (length >> 8), length & 0xFF)) + body
        return _psi_packet(_PID_PAT, table, self._bump(_PID_PAT))

    def _pmt(self) -> bytes:
        body = bytes(
            (
                0x00, 0x01, 0xC1, 0x00, 0x00,
                0xE0 | (_PID_VIDEO >> 8), _PID_VIDEO & 0xFF,  # PCR PID
                0xF0, 0x00,
                _STREAM_TYPE_H264,
                0xE0 | (_PID_VIDEO >> 8), _PID_VIDEO & 0xFF,
                0xF0, 0x00,
            )
        )
        length = len(body) + 4
        table = bytes((0x02, 0xB0 | (length >> 8), length & 0xFF)) + body
        return _psi_packet(_PID_PMT, table, self._bump(_PID_PMT))

    def _pes(self, access_unit: bytes, pts: int) -> bytes:
        # The length field may only be left at 0 (unbounded) for video, which
        # is what a keyframe bigger than 64 KiB needs.
        after_length = 3 + 5 + len(access_unit)
        length = after_length if after_length <= 0xFFFF else 0
        header = b"\x00\x00\x01" + bytes(
            (_STREAM_ID_VIDEO, length >> 8, length & 0xFF, 0x80, 0x80, 0x05)
        )
        return header + _pts_field(pts) + access_unit

    def _packetize(self, pes: bytes, keyframe: bool, pcr: int) -> bytes:
        out = bytearray()
        position, first, total = 0, True, len(pes)
        while position < total:
            counter = self._bump(_PID_VIDEO)
            adaptation = b""
            if first:
                flags = (0x40 if keyframe else 0x00) | 0x10
                body = bytes((flags,)) + _pcr_field(pcr)
                adaptation = bytes((len(body),)) + body
            room = 184 - len(adaptation)
            chunk = pes[position : position + room]
            if len(chunk) < room:
                # Last packet: pad to 188 bytes with adaptation-field stuffing.
                pad = room - len(chunk)
                if adaptation:
                    adaptation = (
                        bytes((adaptation[0] + pad,)) + adaptation[1:] + b"\xff" * pad
                    )
                elif pad == 1:
                    adaptation = b"\x00"
                else:
                    adaptation = bytes((pad - 1, 0x00)) + b"\xff" * (pad - 2)
                chunk = pes[position : position + 184 - len(adaptation)]
            control = 0x30 if adaptation else 0x10
            out += bytes(
                (
                    0x47,
                    (0x40 if first else 0x00) | (_PID_VIDEO >> 8),
                    _PID_VIDEO & 0xFF,
                    control | counter,
                )
            )
            out += adaptation + chunk
            position += len(chunk)
            first = False
        return bytes(out)

    def mux(self, access_unit: bytes, pts: int, keyframe: bool, now_ms: int) -> bytes:
        """Return the packets for one access unit, PAT/PMT first when due."""
        out = b""
        if (
            keyframe
            or self._last_psi_ms is None
            or now_ms - self._last_psi_ms >= _PSI_INTERVAL_MS
        ):
            self._last_psi_ms = now_ms
            out = self._pat() + self._pmt()
        return out + self._packetize(self._pes(access_unit, pts), keyframe, pts)
