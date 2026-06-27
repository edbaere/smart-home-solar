"""Price adapter: ENTSO-E Transparency Platform day-ahead prices -> curtailment schedule.

Uses the ENTSO-E RESTful API (document type A44, "Day-ahead Prices"), which returns the
Belgian day-ahead (BELPEX/EPEX) price directly in **EUR/MWh** — exactly the unit our tariff
formulas in :mod:`smart_home.economics` expect, so no conversion is needed.

Stdlib only (``urllib`` + ``xml.etree``): no third-party dependency, no package index needed.

Split into:
  * pure transforms (``parse_entsoe_xml``, ``build_schedule``, ``default_is_night``) — no I/O,
    fully unit-tested;
  * a thin network fetch (``fetch_raw_prices``).

API ref: https://web-api.tp.entsoe.eu/api  (needs a Web API security token).
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from time import sleep as _sleep
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from smart_home.economics import IMEWO, Slot, TariffCard

BRUSSELS = ZoneInfo("Europe/Brussels")
UTC = ZoneInfo("UTC")

ENTSOE_API = "https://web-api.tp.entsoe.eu/api"
BE_BIDDING_ZONE = "10YBE----------2"  # Belgium (Elia) EIC code
DAY_AHEAD_PRICES = "A44"

_RESOLUTION_MINUTES = {"PT60M": 60, "PT30M": 30, "PT15M": 15, "PT1M": 1}


class EntsoeError(RuntimeError):
    """Raised when ENTSO-E returns an acknowledgement/error instead of price data."""


@dataclass(frozen=True)
class RawPrice:
    """One market slot: its start time and the raw day-ahead BELPEX price (EUR/MWh)."""

    start: datetime          # timezone-aware (UTC)
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


# --- XML parsing ----------------------------------------------------------

def _local(tag: str) -> str:
    """Strip the XML namespace from a tag, leaving the local name."""
    return tag.rsplit("}", 1)[-1]


def _first_text(elem: ET.Element, name: str) -> str | None:
    for e in elem.iter():
        if _local(e.tag) == name:
            return e.text
    return None


def _iter(elem: ET.Element, name: str):
    return (e for e in elem.iter() if _local(e.tag) == name)


def parse_entsoe_xml(xml_text: str) -> list[RawPrice]:
    """Parse an ENTSO-E A44 publication document into raw BELPEX prices (EUR/MWh).

    Handles namespaces, PT60M/PT30M/PT15M resolutions, and ENTSO-E's sparse-point
    convention (a missing position repeats the previous value). Raises
    :class:`EntsoeError` if the platform returned an acknowledgement/error document.
    """
    root = ET.fromstring(xml_text)
    if _local(root.tag).startswith("Acknowledgement"):
        reason = _first_text(root, "text") or "unknown reason"
        raise EntsoeError(f"ENTSO-E returned no data: {reason}")

    prices: list[RawPrice] = []
    for period in _iter(root, "Period"):
        start_text = _first_text(period, "start")
        resolution = _first_text(period, "resolution")
        if not start_text or resolution not in _RESOLUTION_MINUTES:
            continue
        step = timedelta(minutes=_RESOLUTION_MINUTES[resolution])
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))

        points: dict[int, float] = {}
        for pt in _iter(period, "Point"):
            pos = _first_text(pt, "position")
            amt = _first_text(pt, "price.amount")
            if pos is not None and amt is not None:
                points[int(pos)] = float(amt)
        if not points:
            continue

        last = None
        for pos in range(1, max(points) + 1):
            if pos in points:           # sparse points repeat the previous value
                last = points[pos]
            prices.append(RawPrice(start=start + step * (pos - 1), belpex_eur_mwh=last))
    return prices


# --- schedule -------------------------------------------------------------

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


# --- network fetch --------------------------------------------------------

def fetch_raw_prices(
    token: str,
    start_day: date,
    end_day: date,
    zone: str = BE_BIDDING_ZONE,
    *,
    timeout: float = 30.0,
    retries: int = 4,
) -> list[RawPrice]:
    """Fetch day-ahead prices covering the Brussels-local days ``start_day``..``end_day``.

    ENTSO-E wants ``periodStart``/``periodEnd`` in UTC; we derive them from the Brussels
    midnight boundaries so the returned slots line up with local calendar days.

    The Transparency Platform is frequently slow/overloaded right after day-ahead
    publication, so transient network failures (timeouts, dropped connections, 5xx) are
    retried with exponential backoff. Client errors (e.g. 401 bad token) are not retried.
    """
    start_utc = datetime.combine(start_day, time(0)).replace(tzinfo=BRUSSELS).astimezone(UTC)
    end_utc = (
        datetime.combine(end_day + timedelta(days=1), time(0))
        .replace(tzinfo=BRUSSELS)
        .astimezone(UTC)
    )
    params = {
        "securityToken": token,
        "documentType": DAY_AHEAD_PRICES,
        "in_Domain": zone,
        "out_Domain": zone,
        "periodStart": start_utc.strftime("%Y%m%d%H%M"),
        "periodEnd": end_utc.strftime("%Y%m%d%H%M"),
    }
    url = f"{ENTSOE_API}?{urllib.parse.urlencode(params)}"
    xml_text = _get_with_retry(url, timeout=timeout, retries=retries)
    return parse_entsoe_xml(xml_text)


def _get_with_retry(url: str, *, timeout: float, retries: int) -> str:
    """GET ``url`` and return the body, retrying transient failures with backoff."""
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (trusted host)
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Retry only on server-side errors; 4xx (bad token, bad query) won't recover.
            if exc.code < 500 or attempt == retries:
                raise
            reason: Exception = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise
            reason = exc
        backoff = 2.0 * 2 ** (attempt - 1)  # 2s, 4s, 8s, ...
        print(
            f"ENTSO-E fetch attempt {attempt}/{retries} failed ({reason}); "
            f"retrying in {backoff:.0f}s",
            file=sys.stderr,
        )
        _sleep(backoff)
    raise EntsoeError("unreachable")  # pragma: no cover (loop always returns or raises)


def _format(schedule: list[Slot]) -> str:
    lines = ["start (Brussels)      BELPEX   consume   feedin   action", "-" * 64]
    for s in schedule:
        lines.append(
            f"{s.start[:16]:<20}  {s.belpex:7.1f}  {s.consume_price:7.3f}  "
            f"{s.feedin_price:7.3f}   {s.action.value}"
        )
    return "\n".join(lines)


def main() -> None:
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        print("Set ENTSOE_API_TOKEN (your ENTSO-E Web API security token)", file=sys.stderr)
        sys.exit(1)

    today = date.today()
    raw = fetch_raw_prices(token, today, today + timedelta(days=1))
    if not raw:
        print("No day-ahead prices returned (tomorrow's may not be published yet).")
        return
    print(_format(build_schedule(raw)))


if __name__ == "__main__":
    main()
