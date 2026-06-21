"""Price adapter: Frank Energie day-ahead prices -> curtailment schedule.

Splits cleanly into:
  * pure transforms (``to_raw_prices``, ``build_schedule``, ``default_is_night``) — no I/O,
    fully unit-tested;
  * a thin async network fetch (``fetch_raw_prices``) that wraps ``python_frank_energie``.

We use ONLY the raw wholesale price (``market_price``, in EUR/kWh -> BELPEX in EUR/MWh) and
apply our own Belgian tariff formulas in :mod:`smart_home.economics`. The library's ``total``
and ``energy_tax_price`` bundle the *Dutch* energy tax (hardcoded), so they are NOT valid for
a Belgian contract.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from smart_home.economics import IMEWO, Slot, TariffCard

BRUSSELS = ZoneInfo("Europe/Brussels")
UTC = ZoneInfo("UTC")

# Frank Energie quotes all prices in EUR/kWh; our tariff formulas use BELPEX in EUR/MWh.
EUR_KWH_TO_EUR_MWH = 1000.0


@runtime_checkable
class _FrankPrice(Protocol):
    """Duck-typed subset of python_frank_energie's Price we depend on."""

    date_from: datetime
    market_price: float


@dataclass(frozen=True)
class RawPrice:
    """One market slot: its start time and the raw day-ahead BELPEX price."""

    start: datetime          # timezone-aware
    belpex_eur_mwh: float

    @property
    def start_local(self) -> datetime:
        return self.start.astimezone(BRUSSELS)


def default_is_night(start_local: datetime) -> bool:
    """Belgian dual-tariff default: night = all weekend + weekdays 22:00-07:00.

    NOTE: the exact day/night split depends on the meter/DSO configuration. This only
    shifts the (very rare) FULL_CURTAIL threshold slightly and never affects ZERO_EXPORT,
    so a sensible default is fine — confirm against your meter before relying on it.
    """
    if start_local.weekday() >= 5:  # Saturday (5), Sunday (6)
        return True
    return start_local.hour >= 22 or start_local.hour < 7


def to_raw_prices(frank_prices: Iterable[_FrankPrice]) -> list[RawPrice]:
    """Extract raw BELPEX (EUR/MWh) from python_frank_energie Price objects."""
    out: list[RawPrice] = []
    for p in frank_prices:
        start = p.date_from
        if start.tzinfo is None:  # API timestamps are UTC ("...Z")
            start = start.replace(tzinfo=UTC)
        out.append(RawPrice(start=start, belpex_eur_mwh=p.market_price * EUR_KWH_TO_EUR_MWH))
    return out


def build_schedule(
    raw_prices: Iterable[RawPrice],
    card: TariffCard = IMEWO,
    is_night: Callable[[datetime], bool] = default_is_night,
) -> list[Slot]:
    """Turn raw prices into a sorted list of priced + decided :class:`Slot`s."""
    schedule = [
        Slot.from_belpex(
            rp.start_local.isoformat(),
            rp.belpex_eur_mwh,
            card,
            night=is_night(rp.start_local),
        )
        for rp in raw_prices
    ]
    schedule.sort(key=lambda s: s.start)
    return schedule


async def fetch_raw_prices(
    email: str,
    password: str,
    start_day: date,
    end_day: date,
) -> list[RawPrice]:
    """Fetch raw day-ahead prices from Frank Energie. Isolated I/O; local import."""
    from python_frank_energie import FrankEnergie  # noqa: PLC0415 (optional dependency)

    async with FrankEnergie() as fe:
        await fe.login(email, password)
        sites = await fe.UserSites()
        prices = await fe.user_prices(sites.reference, start_day, end_day)
        frank: list[_FrankPrice] = []
        if prices.electricity.today:
            frank.extend(prices.electricity.today)
        if prices.electricity.tomorrow:
            frank.extend(prices.electricity.tomorrow)
        return to_raw_prices(frank)


def _format(schedule: list[Slot]) -> str:
    lines = ["start (Brussels)      BELPEX   consume   feedin   action",
             "-" * 64]
    for s in schedule:
        lines.append(
            f"{s.start[:16]:<20}  {s.belpex:7.1f}  {s.consume_price:7.3f}  "
            f"{s.feedin_price:7.3f}   {s.action.value}"
        )
    return "\n".join(lines)


def main() -> None:
    email = os.environ.get("FRANK_ENERGIE_EMAIL")
    password = os.environ.get("FRANK_ENERGIE_PASSWORD")
    if not email or not password:
        print("Set FRANK_ENERGIE_EMAIL and FRANK_ENERGIE_PASSWORD", file=sys.stderr)
        sys.exit(1)

    today = date.today()
    raw = asyncio.run(fetch_raw_prices(email, password, today, today + timedelta(days=1)))
    print(_format(build_schedule(raw)))


if __name__ == "__main__":
    main()
