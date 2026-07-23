"""Read-only Huawei SUN2000 reader over the inverter's built-in WLAN (Modbus 6607).

This is the **validated** telemetry path (confirmed live on a SUN2000-4.6KTL-L1): connect a
client to the inverter's Wi-Fi hotspot, reach it at ``192.168.200.1:6607``, and read via the
``huawei-solar`` library, which speaks the proprietary 6607 handshake. No SDongle, no
installer Modbus-TCP toggle, no Huawei power meter required.

Reads are unauthenticated. *Writes* (curtailment, Phase 3+) require an installer
``login()`` — confirmed working — and the inverter ramps power at
``DEFAULT_ACTIVE_POWER_CHANGE_GRADIENT`` (≈0.277 %/s by default, ~3 min for a 50% swing).

The ``huawei-solar`` dependency is imported lazily inside :func:`read`, so this module (and
the pure :func:`from_values`) import fine without it. Install with ``pip install '.[hw]'``.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

DEFAULT_HOST = "192.168.200.1"   # inverter built-in WLAN AP
DEFAULT_PORT = 6607              # proprietary handshake (handled by huawei-solar)


@dataclass(frozen=True)
class InverterReading:
    """A telemetry + control-state snapshot from the inverter."""

    model_name: str | None = None
    device_status: str | None = None
    input_power_w: float | None = None              # DC input
    active_power_w: float | None = None             # AC output = PV production
    yield_total_kwh: float | None = None            # 32106, lifetime cumulative, inverter's own meter
    grid_frequency_hz: float | None = None
    internal_temperature_c: float | None = None
    control_mode: str | None = None                 # ACTIVE_POWER_CONTROL_MODE
    percentage_derating: float | None = None        # 40125, %
    fixed_value_derating_w: float | None = None     # 40126, W
    p_max_w: float | None = None                    # rated power
    power_change_gradient_pct_s: float | None = None  # 47677, %/s
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def from_values(v: dict[str, Any]) -> InverterReading:
    """Build an :class:`InverterReading` from a plain {register_name: value} dict (pure)."""
    return InverterReading(
        model_name=v.get("model_name"),
        device_status=v.get("device_status"),
        input_power_w=v.get("input_power"),
        active_power_w=v.get("active_power"),
        yield_total_kwh=v.get("yield_total"),
        grid_frequency_hz=v.get("grid_frequency"),
        internal_temperature_c=v.get("internal_temperature"),
        control_mode=v.get("active_power_control_mode"),
        percentage_derating=v.get("active_power_percentage_derating"),
        fixed_value_derating_w=v.get("active_power_fixed_value_derating"),
        p_max_w=v.get("p_max"),
        power_change_gradient_pct_s=v.get("default_active_power_change_gradient"),
        raw=v,
    )


async def read(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> InverterReading:
    """Read telemetry + control state from the inverter (read-only)."""
    from huawei_solar import create_device_instance, create_tcp_client  # noqa: PLC0415
    from huawei_solar import register_names as rn  # noqa: PLC0415

    names = {
        "model_name": rn.MODEL_NAME,
        "device_status": rn.DEVICE_STATUS,
        "input_power": rn.INPUT_POWER,
        "active_power": rn.ACTIVE_POWER,
        "yield_total": rn.ACCUMULATED_YIELD_ENERGY,
        "grid_frequency": rn.GRID_FREQUENCY,
        "internal_temperature": rn.INTERNAL_TEMPERATURE,
        "active_power_control_mode": rn.ACTIVE_POWER_CONTROL_MODE,
        "active_power_percentage_derating": rn.ACTIVE_POWER_PERCENTAGE_DERATING,
        "active_power_fixed_value_derating": rn.ACTIVE_POWER_FIXED_VALUE_DERATING,
        "p_max": rn.P_MAX,
        "default_active_power_change_gradient": rn.DEFAULT_ACTIVE_POWER_CHANGE_GRADIENT,
    }
    client = create_tcp_client(host=host, port=port)
    device = await create_device_instance(client)
    data = await device.batch_update(list(names.values()))
    values = {
        key: (getattr(data[reg], "value", None) if reg in data else None)
        for key, reg in names.items()
    }
    return from_values(values)


def read_sync(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> InverterReading:
    """Synchronous convenience wrapper around :func:`read`."""
    return asyncio.run(read(host, port))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    host = argv[0] if argv else DEFAULT_HOST
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    r = read_sync(host, port)
    print(f"model            : {r.model_name}")
    print(f"device_status    : {r.device_status}")
    print(f"input_power (DC) : {r.input_power_w} W")
    print(f"active_power (AC): {r.active_power_w} W   <- PV production")
    print(f"yield_total      : {r.yield_total_kwh} kWh <- lifetime, inverter's own meter")
    print(f"grid_frequency   : {r.grid_frequency_hz} Hz")
    print(f"internal_temp    : {r.internal_temperature_c} °C")
    print(f"P_MAX (rated)    : {r.p_max_w} W")
    print(f"control_mode     : {r.control_mode}")
    print(f"pct_derating     : {r.percentage_derating} %")
    print(f"fixed_derating   : {r.fixed_value_derating_w} W")
    print(f"power_gradient   : {r.power_change_gradient_pct_s} %/s")


if __name__ == "__main__":
    main()
