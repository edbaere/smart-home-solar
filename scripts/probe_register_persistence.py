"""One-shot probe: is ACTIVE_POWER_PERCENTAGE_DERATING (40125) volatile (RAM) or
persisted to non-volatile memory (flash/EEPROM)?

Why: our curtailment loop writes 40125 frequently. If the register lives in RAM,
frequent writes are harmless (RAM has no write-cycle limit). If it is committed to
flash, frequent writes wear the inverter out and we must throttle hard. Huawei does
not document this for 40125, so we test *this* unit + firmware directly.

Method (must run in DAYLIGHT, inverter awake, and as the SOLE Modbus client — stop
the controller/publisher first; the wrapper `run_persistence_probe.sh` does that):

  1. Connect, snapshot the current state (records the baseline derating to restore).
  2. Log in (installer) and write a distinctive PROBE value (55%). Read it back to
     confirm the write path worked.
  3. Prompt YOU to power-cycle the *inverter itself* (DC isolator + AC breaker off,
     wait, back on) — NOT the Pi. Wait for confirmation.
  4. Reconnect (retry while the Wi-Fi AP comes back) and read 40125 again.
  5. Classify:
       - reads ~55%  -> the write SURVIVED a power cycle -> PERSISTENT (non-volatile).
         Frequent writes are a real wear risk. Throttle writes hard.
       - reads ~100% (or anything != 55) -> the write did NOT survive -> the setpoint
         is held in volatile RAM. Frequent writes are almost certainly safe.
  6. ALWAYS restore the inverter to 100% (full production) on the way out.

Caveat: "did not survive reboot" is a strong proxy for RAM-backed, not a hardware
guarantee that nothing is flashed. But "survived reboot" is conclusive that it IS
persisted. So a 55%-readback is a hard STOP signal; a 100%-readback is a strong
all-clear.

Usage (on the Pi, via the wrapper which also sources HUAWEI_PW):
    python -m smart_home... no — run directly:
    HUAWEI_PW=... python scripts/probe_register_persistence.py
    python scripts/probe_register_persistence.py --restore-only   # safety escape hatch
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

DEFAULT_HOST = "192.168.200.1"
DEFAULT_PORT = 6607
PROBE_PCT = 55.0        # distinctive: not 100 (default) and not a value the plan sets
FULL_PCT = 100.0
TOL = 3.0               # readback tolerance (%)


async def _connect(host: str, port: int):
    from huawei_solar import create_device_instance, create_tcp_client  # noqa: PLC0415
    client = create_tcp_client(host=host, port=port)
    return await create_device_instance(client)


async def _read_derating(device) -> float | None:
    from huawei_solar import register_names as rn  # noqa: PLC0415
    data = await device.batch_update([rn.ACTIVE_POWER_PERCENTAGE_DERATING])
    reg = data.get(rn.ACTIVE_POWER_PERCENTAGE_DERATING)
    return getattr(reg, "value", None) if reg is not None else None


async def _snapshot(device) -> dict:
    from huawei_solar import register_names as rn  # noqa: PLC0415
    names = {
        "status": rn.DEVICE_STATUS,
        "active_power": rn.ACTIVE_POWER,
        "p_max": rn.P_MAX,
        "derating": rn.ACTIVE_POWER_PERCENTAGE_DERATING,
    }
    data = await device.batch_update(list(names.values()))
    return {k: (getattr(data[r], "value", None) if r in data else None) for k, r in names.items()}


async def _write_derating(device, user: str, pw: str, pct: float) -> None:
    from huawei_solar import register_names as rn  # noqa: PLC0415
    ok = await device.login(user, pw)
    if not ok:
        raise SystemExit("ERROR: installer login failed — cannot write. Check HUAWEI_PW.")
    await device.set(rn.ACTIVE_POWER_PERCENTAGE_DERATING, pct)


async def _reconnect(host: str, port: int, attempts: int = 40, delay: float = 15.0):
    """Retry connecting while the inverter's Wi-Fi AP comes back after the power cycle."""
    last = None
    for i in range(1, attempts + 1):
        try:
            device = await _connect(host, port)
            await _read_derating(device)  # prove it actually talks
            print(f"  reconnected on attempt {i}")
            return device
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  attempt {i}/{attempts} failed ({type(e).__name__}); retrying in {delay:.0f}s…")
            await asyncio.sleep(delay)
    raise SystemExit(f"ERROR: could not reconnect after {attempts} attempts. Last error: {last!r}")


async def _restore_full(host: str, port: int, user: str, pw: str) -> None:
    """Best-effort restore to 100% so we never strand the inverter curtailed."""
    print("\n>>> Restoring inverter to 100% (full production)…")
    try:
        device = await _connect(host, port)
        await _write_derating(device, user, pw, FULL_PCT)
        back = await _read_derating(device)
        print(f">>> Restore confirmed: derating now reads {back}% "
              f"({'OK' if back is not None and abs(back - FULL_PCT) <= TOL else 'CHECK MANUALLY'})")
    except Exception as e:  # noqa: BLE001
        print(f">>> !!! RESTORE FAILED ({e!r}). "
              f"Manually verify the inverter is at 100% (or just restart the controller service).")


async def _phase_write(host: str, port: int, user: str, pw: str) -> None:
    """Snapshot baseline, write the probe value, confirm. Leaves inverter at PROBE_PCT."""
    print("=== probe phase: WRITE ===")
    device = await _connect(host, port)
    snap = await _snapshot(device)
    print(f"Baseline: status={snap['status']} active_power={snap['active_power']}W "
          f"p_max={snap['p_max']}W derating={snap['derating']}%")
    if snap["active_power"] in (0, None):
        print("WARNING: inverter is producing ~0 W — it may be asleep. The AP can drop when "
              "asleep, breaking the reconnect step. Prefer running this in good daylight.")
    print(f"\nWriting probe value {PROBE_PCT}% to 40125…")
    await _write_derating(device, user, pw, PROBE_PCT)
    back = await _read_derating(device)
    print(f"Read-back: {back}%")
    if back is None or abs(back - PROBE_PCT) > TOL:
        raise SystemExit(f"ERROR: write did not take (read {back}, expected ~{PROBE_PCT}).")
    print(f"\nWrite path OK — 40125 now holds {PROBE_PCT}%.")
    print(">>> NOW POWER-CYCLE THE INVERTER (DC isolator + AC breaker off ~2 min, then on),")
    print(">>> then run the READ phase.")


async def _phase_read(host: str, port: int, user: str, pw: str) -> None:
    """Reconnect after the power cycle, read 40125, classify, then restore 100%."""
    print("=== probe phase: READ ===")
    try:
        print("Reconnecting to the inverter…")
        device = await _reconnect(host, port)
        after = await _read_derating(device)
        print(f"\nAfter power cycle, 40125 reads: {after}%")
        print("\n" + "=" * 70)
        if after is not None and abs(after - PROBE_PCT) <= TOL:
            print("RESULT: PERSISTENT (non-volatile).")
            print(f"  The {PROBE_PCT}% write SURVIVED a full power cycle -> 40125 is committed to")
            print("  non-volatile memory. Frequent writes ARE a wear risk.")
            print("  ==> STOP the high-frequency write loop; ship the write-throttling redesign")
            print("      before re-enabling curtailment.")
        elif after is not None and abs(after - FULL_PCT) <= TOL:
            print("RESULT: VOLATILE (RAM-backed).")
            print("  The write did NOT survive the power cycle (reset to 100%) -> the setpoint")
            print("  is held in RAM. Frequent writes are almost certainly harmless.")
            print("  ==> The redesign is a nice-to-have (economics/robustness), not urgent.")
        else:
            print(f"RESULT: INCONCLUSIVE — reads {after}% (neither ~{PROBE_PCT} nor ~{FULL_PCT}).")
            print("  Possibly a partial ramp or a different default. Re-run, or read again in a")
            print("  minute. Treat as 'assume persistent' (conservative) until clarified.")
        print("=" * 70)
    finally:
        await _restore_full(host, port, user, pw)


async def run(host: str, port: int, user: str, pw: str, phase: str, restore_only: bool) -> None:
    if restore_only:
        await _restore_full(host, port, user, pw)
        return
    if phase == "write":
        await _phase_write(host, port, user, pw)
        return
    if phase == "read":
        await _phase_read(host, port, user, pw)
        return

    # phase == "full": interactive single-session flow (solo use)
    print("=== 40125 persistence probe (interactive) ===")
    print("Preconditions: daylight, inverter awake, and NO other Modbus client running.\n")
    await _phase_write(host, port, user, pw)
    try:
        try:
            input("\nPress ENTER once the inverter is powered back up… ")
        except EOFError:
            raise SystemExit("No TTY for confirmation. Run interactively (ssh -t), or use "
                             "--phase write / --phase read. Restoring 100%.")
        await _phase_read(host, port, user, pw)
    finally:
        # _phase_read already restores; this covers an abort before it ran.
        await _restore_full(host, port, user, pw)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Probe whether inverter register 40125 is volatile or persisted.")
    ap.add_argument("--inverter-host", default=DEFAULT_HOST)
    ap.add_argument("--inverter-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--phase", choices=("full", "write", "read"), default="full",
                    help="'write' then (after power-cycle) 'read' for a co-driven run; "
                         "'full' (default) is the interactive single-session flow")
    ap.add_argument("--restore-only", action="store_true",
                    help="skip the test; just log in and set 100%% (safety escape hatch)")
    args = ap.parse_args(argv)

    user = os.environ.get("HUAWEI_USER", "installer")
    pw = os.environ.get("HUAWEI_PW", "")
    if not pw:
        print("Set HUAWEI_PW (installer password) in the environment.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(args.inverter_host, args.inverter_port, user, pw, args.phase, args.restore_only))


if __name__ == "__main__":
    main()
