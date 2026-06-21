"""Read-only Modbus-TCP reader for the Huawei SUN2000 inverter.

Phase 1 is **read-only**: confirm connectivity, dump the register map, and inspect the
current active-power control state — *before* any write is attempted (writes are Phase 3).

Stdlib only: a minimal Modbus-TCP client over a raw socket (function 0x03, read holding
registers). The wire framing and value decoding are separated from socket I/O so they are
testable without hardware.

Register addresses/types/gains are taken from the ``huawei-solar-lib`` definitions.
Key registers:
  * ACTIVE_POWER (32080, I32, W)         — AC output = PV production we curtail
  * INPUT_POWER  (32064, I32, W)         — DC input power
  * 40126 ACTIVE_POWER_FIXED_VALUE_DERATING (U32, W)  — the zero-export actuator (Phase 4)
  * 40125 ACTIVE_POWER_PERCENTAGE_DERATING  (I16, %)
  * 47415 ACTIVE_POWER_CONTROL_MODE         (U16 enum)
  * 35300 ACTIVE_POWER_ADJUSTMENT_MODE      (U16)
"""

from __future__ import annotations

import socket
import struct
import sys
import time
from dataclasses import dataclass

DEFAULT_PORT = 502
DEFAULT_UNIT = 1
_READ_HOLDING = 0x03

# Decoded meaning of ACTIVE_POWER_CONTROL_MODE (reg 47415).
CONTROL_MODE_NAMES = {
    0: "Unlimited (default)",
    1: "DI active scheduling",
    5: "Zero-power grid connection (needs Huawei meter)",
    6: "Power-limited grid connection, Watt (needs Huawei meter)",
    7: "Power-limited grid connection, percent (needs Huawei meter)",
}


class ModbusError(RuntimeError):
    """A Modbus protocol-level error (exception response or malformed frame)."""


@dataclass(frozen=True)
class Reg:
    """A Huawei register definition."""

    name: str
    address: int
    count: int          # number of 16-bit registers
    kind: str           # 'u16' | 'i16' | 'u32' | 'i32' | 'string'
    gain: float = 1
    unit: str | None = None
    writable: bool = False


# Read for telemetry.
TELEMETRY: list[Reg] = [
    Reg("model_name", 30000, 15, "string"),
    Reg("device_status", 32089, 1, "u16"),
    Reg("input_power_w", 32064, 2, "i32", 1, "W"),
    Reg("active_power_w", 32080, 2, "i32", 1, "W"),          # AC output = PV production
    Reg("grid_frequency_hz", 32085, 1, "u16", 100, "Hz"),
    Reg("internal_temperature_c", 32087, 1, "i16", 10, "°C"),
    Reg("pv1_voltage_v", 32016, 1, "i16", 10, "V"),
    Reg("pv1_current_a", 32017, 1, "i16", 100, "A"),
    Reg("power_meter_active_power_w", 37113, 2, "i32", 1, "W"),  # only valid w/ Huawei meter
]

# Read-only here, but these are the writable control registers (inspect current state).
CONTROL_STATE: list[Reg] = [
    Reg("active_power_adjustment_mode", 35300, 1, "u16", 1, None, writable=True),
    Reg("active_power_percentage_derating", 40125, 1, "i16", 10, "%", writable=True),
    Reg("active_power_fixed_value_derating_w", 40126, 2, "u32", 1, "W", writable=True),
    Reg("active_power_control_mode", 47415, 1, "u16", 1, None, writable=True),
]

DIAGNOSTIC: list[Reg] = TELEMETRY + CONTROL_STATE


# --- pure wire framing & decoding (no I/O) --------------------------------

def build_read_request(address: int, count: int, unit: int = DEFAULT_UNIT, tx: int = 1) -> bytes:
    """Build a Modbus-TCP 'read holding registers' (0x03) request frame."""
    # MBAP: tx(2) proto(2)=0 len(2)=6 unit(1); PDU: func(1) addr(2) qty(2)
    return struct.pack(">HHHBBHH", tx, 0, 6, unit, _READ_HOLDING, address, count)


def parse_read_response(frame: bytes, expected_count: int) -> list[int]:
    """Parse a full Modbus-TCP response frame into a list of 16-bit register words."""
    if len(frame) < 9:
        raise ModbusError(f"short response ({len(frame)} bytes)")
    _tx, _proto, _length, _unit, func = struct.unpack(">HHHBB", frame[:8])
    if func & 0x80:                       # exception response
        raise ModbusError(f"modbus exception code {frame[8]}")
    byte_count = frame[8]
    if byte_count != expected_count * 2:
        raise ModbusError(f"byte count {byte_count}, expected {expected_count * 2}")
    data = frame[9 : 9 + byte_count]
    if len(data) != byte_count:
        raise ModbusError("truncated data")
    return [struct.unpack(">H", data[i : i + 2])[0] for i in range(0, byte_count, 2)]


def decode(words: list[int], reg: Reg):
    """Decode register words per the register's type/gain."""
    raw_bytes = b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
    if reg.kind == "string":
        return raw_bytes.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    fmt = {"u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i"}[reg.kind]
    raw = struct.unpack(fmt, raw_bytes)[0]
    return raw / reg.gain if reg.gain not in (1, 1.0) else raw


# --- socket I/O -----------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ModbusError("connection closed mid-frame")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> bytes:
    head = _recv_exact(sock, 6)              # tx, proto, length
    _tx, _proto, length = struct.unpack(">HHH", head)
    return head + _recv_exact(sock, length)


def read_register(sock: socket.socket, reg: Reg, unit: int = DEFAULT_UNIT, tx: int = 1):
    """Read and decode a single register over an open socket."""
    sock.sendall(build_read_request(reg.address, reg.count, unit, tx))
    return decode(parse_read_response(_read_frame(sock), reg.count), reg)


def read_all(
    host: str,
    port: int = DEFAULT_PORT,
    unit: int = DEFAULT_UNIT,
    regs: list[Reg] = DIAGNOSTIC,
    *,
    timeout: float = 10.0,
    connect_delay: float = 1.0,   # SUN2000 needs a moment after connect before first read
    gap: float = 0.1,             # small pause between reads; the inverter dislikes bursts
) -> dict[str, object]:
    """Read every register in ``regs``; per-register errors are captured, not fatal."""
    results: dict[str, object] = {}
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        if connect_delay:
            time.sleep(connect_delay)
        tx = 0
        for reg in regs:
            tx = (tx % 0xFFFF) + 1
            try:
                results[reg.name] = read_register(sock, reg, unit, tx)
            except (ModbusError, OSError) as exc:
                results[reg.name] = f"ERROR: {exc}"
            if gap:
                time.sleep(gap)
    return results


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m smart_home.inverter <inverter-host-or-ip> [port]", file=sys.stderr)
        sys.exit(1)
    host = argv[0]
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    results = read_all(host, port)
    width = max(len(r.name) for r in DIAGNOSTIC)
    by_name = {r.name: r for r in DIAGNOSTIC}
    for name, value in results.items():
        reg = by_name[name]
        unit = f" {reg.unit}" if reg.unit and not isinstance(value, str) else ""
        note = ""
        if name == "active_power_control_mode" and isinstance(value, int):
            note = f"  -> {CONTROL_MODE_NAMES.get(value, 'unknown')}"
        print(f"  {name:<{width}}  ({reg.address})  {value}{unit}{note}")


if __name__ == "__main__":
    main()
