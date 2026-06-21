"""Read-only reader for the HomeWizard P1 meter (local API).

Measures net exchange with the grid. ``active_power_w`` is the net active power:
**negative = injecting (exporting) to the grid**, positive = importing.

Uses the v1 local API (``GET http://<host>/api/v1/data``, plain HTTP) — enable it in the
HomeWizard Energy app (Settings -> Meters -> your P1 -> Local API). Stdlib only.

Parsing (:func:`parse_p1`) is separated from I/O (:func:`read`) so it is testable offline.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class P1Reading:
    """A snapshot from the P1 meter."""

    active_power_w: float                 # net; negative = exporting to grid
    active_power_l1_w: float | None = None
    active_power_l2_w: float | None = None
    active_power_l3_w: float | None = None
    total_import_kwh: float | None = None
    total_export_kwh: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def exporting_w(self) -> float:
        """Power being injected to the grid (>= 0)."""
        return max(0.0, -self.active_power_w)

    @property
    def importing_w(self) -> float:
        """Power being drawn from the grid (>= 0)."""
        return max(0.0, self.active_power_w)

    @property
    def is_exporting(self) -> bool:
        return self.active_power_w < 0


def _sum_present(d: dict[str, Any], *keys: str) -> float | None:
    vals = [d[k] for k in keys if d.get(k) is not None]
    return sum(vals) if vals else None


def parse_p1(d: dict[str, Any]) -> P1Reading:
    """Parse a HomeWizard ``/api/v1/data`` payload into a :class:`P1Reading`.

    Tolerates firmware differences: import/export totals may be a single summed field
    or split per tariff (``..._t1_kwh`` / ``..._t2_kwh``).
    """
    if "active_power_w" not in d or d["active_power_w"] is None:
        raise ValueError("P1 payload missing 'active_power_w'")
    total_import = d.get("total_power_import_kwh")
    if total_import is None:
        total_import = _sum_present(d, "total_power_import_t1_kwh", "total_power_import_t2_kwh")
    total_export = d.get("total_power_export_kwh")
    if total_export is None:
        total_export = _sum_present(d, "total_power_export_t1_kwh", "total_power_export_t2_kwh")
    return P1Reading(
        active_power_w=float(d["active_power_w"]),
        active_power_l1_w=d.get("active_power_l1_w"),
        active_power_l2_w=d.get("active_power_l2_w"),
        active_power_l3_w=d.get("active_power_l3_w"),
        total_import_kwh=total_import,
        total_export_kwh=total_export,
        raw=d,
    )


def read(host: str, *, timeout: float = 5.0) -> P1Reading:
    """Fetch a live reading from the P1 meter at ``host`` (IP or hostname)."""
    url = f"http://{host}/api/v1/data"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (local device)
        return parse_p1(json.loads(resp.read().decode("utf-8")))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m smart_home.p1 <p1-host-or-ip>", file=sys.stderr)
        sys.exit(1)
    r = read(argv[0])
    arrow = "injecting" if r.is_exporting else "importing"
    print(f"net active power : {r.active_power_w:+.0f} W  ({arrow} {abs(r.active_power_w):.0f} W)")
    for ph, v in (("L1", r.active_power_l1_w), ("L2", r.active_power_l2_w), ("L3", r.active_power_l3_w)):
        if v is not None:
            print(f"  {ph}            : {v:+.0f} W")
    print(f"total import     : {r.total_import_kwh} kWh")
    print(f"total export     : {r.total_export_kwh} kWh")


if __name__ == "__main__":
    main()
