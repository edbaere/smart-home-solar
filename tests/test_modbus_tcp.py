"""Tests for the generic raw Modbus-TCP reader: framing + decoding (no hardware)."""

import struct

import pytest

from smart_home.modbus_tcp import (
    DIAGNOSTIC,
    ModbusError,
    Reg,
    build_read_request,
    decode,
    parse_read_response,
    read_register,
)


# --- decoding -------------------------------------------------------------

def test_decode_i32_negative():
    words = list(struct.unpack(">HH", struct.pack(">i", -1500)))
    assert decode(words, Reg("active_power_w", 32080, 2, "i32", 1, "W")) == -1500


def test_decode_u32_large():
    words = list(struct.unpack(">HH", struct.pack(">I", 4600)))
    assert decode(words, Reg("p", 40126, 2, "u32", 1, "W")) == 4600


def test_decode_i16_with_gain():
    assert decode([235], Reg("t", 32087, 1, "i16", 10, "°C")) == 23.5


def test_decode_u16_with_gain():
    assert decode([5000], Reg("f", 32085, 1, "u16", 100, "Hz")) == 50.0


def test_decode_string_strips_nulls():
    words = list(struct.unpack(">HHHH", b"SUN2\x00\x00\x00\x00"))
    assert decode(words, Reg("model", 30000, 4, "string")) == "SUN2"


# --- framing --------------------------------------------------------------

def test_build_read_request_bytes():
    req = build_read_request(address=32080, count=2, unit=1, tx=7)
    assert req == struct.pack(">HHHBBHH", 7, 0, 6, 1, 0x03, 32080, 2)


def _response(words: list[int], unit: int = 1, func: int = 0x03, tx: int = 1) -> bytes:
    data = b"".join(struct.pack(">H", w) for w in words)
    length = 3 + len(data)
    return struct.pack(">HHHBBB", tx, 0, length, unit, func, len(data)) + data


def test_parse_read_response_ok():
    frame = _response([0x0000, 0x05DC])
    assert parse_read_response(frame, 2) == [0x0000, 0x05DC]


def test_parse_read_response_exception_raises():
    frame = struct.pack(">HHHBBB", 1, 0, 3, 1, 0x83, 0x02)
    with pytest.raises(ModbusError, match="exception code 2"):
        parse_read_response(frame, 2)


def test_parse_read_response_count_mismatch():
    with pytest.raises(ModbusError, match="byte count"):
        parse_read_response(_response([1, 2, 3]), expected_count=2)


# --- full read path over a fake socket ------------------------------------

class FakeSocket:
    def __init__(self, response: bytes):
        self._buf = response
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


def test_read_register_end_to_end():
    reg = Reg("active_power_w", 32080, 2, "i32", 1, "W")
    sock = FakeSocket(_response(list(struct.unpack(">HH", struct.pack(">i", -800)))))
    assert read_register(sock, reg) == -800
    assert sock.sent == build_read_request(32080, 2, tx=1)


# --- register map guards --------------------------------------------------

def test_key_register_addresses_locked():
    by_name = {r.name: r for r in DIAGNOSTIC}
    assert by_name["active_power_w"].address == 32080
    assert by_name["active_power_fixed_value_derating_w"].address == 40126
    assert by_name["active_power_control_mode"].address == 47415
