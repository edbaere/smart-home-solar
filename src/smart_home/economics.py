"""Pure economic decision engine for dynamic PV curtailment.

Turns a day-ahead BELPEX price (in EUR/MWh) into a curtailment decision, using the
Frank Energie dynamic-contract tariff formulas. No hardware, no I/O — fully testable.

All monetary prices below are in **EURct/kWh** unless noted. BELPEX is in **EUR/MWh**
(see PROJECT_PLAN.md §1 for the unit derivation: the 0.1 feed-in coefficient is exactly
the EUR/MWh -> EURct/kWh conversion).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    """What to do with the inverter for a given market slot."""

    NORMAL = "NORMAL"          # self-consume + export surplus, earn the feed-in price
    ZERO_EXPORT = "ZERO_EXPORT"  # clip surplus so net grid exchange ~= 0 (still self-consume)
    FULL_CURTAIL = "FULL_CURTAIL"  # inverter active power = 0; grid pays us to consume


@dataclass(frozen=True)
class TariffCard:
    """Frank Energie dynamic-contract parameters, in EURct/kWh unless noted.

    Defaults reproduce the September-2024 card. ``grid_*`` are the variable distribution
    ("Afname") components and depend on the DSO; the defaults below are Fluvius Imewo,
    digital meter with peak metering.
    """

    # Energy price:  (energy_market_coeff * BELPEX + energy_fixed) * vat
    energy_market_coeff: float = 0.1068
    energy_fixed: float = 1.500
    vat: float = 1.06

    # Feed-in revenue:  feedin_coeff * BELPEX - feedin_fee   (VAT-exempt)
    feedin_coeff: float = 0.1
    feedin_fee: float = 1.150

    # Certificates (VAT included as shown on the card)
    gsc: float = 1.166
    wkk: float = 0.371

    # Taxes & levies (VAT not applicable)
    accijns: float = 5.0328
    bijdrage_energie: float = 0.2042

    # Variable grid "Afname" — Fluvius Imewo, digital meter w/ peak metering
    grid_day: float = 4.72
    grid_night: float = 3.53

    @property
    def fixed_adders_day(self) -> float:
        """Sum of all per-kWh costs that do NOT move with BELPEX (day rate)."""
        return self.gsc + self.wkk + self.accijns + self.bijdrage_energie + self.grid_day

    @property
    def fixed_adders_night(self) -> float:
        return self.gsc + self.wkk + self.accijns + self.bijdrage_energie + self.grid_night


IMEWO = TariffCard()


def feedin_price(belpex: float, card: TariffCard = IMEWO) -> float:
    """Revenue for exporting 1 kWh, in EURct/kWh (negative = it costs us to inject)."""
    return card.feedin_coeff * belpex - card.feedin_fee


def consume_price(belpex: float, card: TariffCard = IMEWO, *, night: bool = False) -> float:
    """All-in cost to import 1 kWh, in EURct/kWh (negative = grid pays us to consume)."""
    energy = (card.energy_market_coeff * belpex + card.energy_fixed) * card.vat
    adders = card.fixed_adders_night if night else card.fixed_adders_day
    return energy + adders


def feedin_zero_belpex(card: TariffCard = IMEWO) -> float:
    """BELPEX (EUR/MWh) at which feed-in revenue crosses zero. Below this: ZERO_EXPORT."""
    return card.feedin_fee / card.feedin_coeff


def consume_zero_belpex(card: TariffCard = IMEWO, *, night: bool = False) -> float:
    """BELPEX (EUR/MWh) at which import cost crosses zero. Below this: FULL_CURTAIL."""
    adders = card.fixed_adders_night if night else card.fixed_adders_day
    # solve (coeff*B + fixed)*vat + adders = 0  for B
    return (-adders / card.vat - card.energy_fixed) / card.energy_market_coeff


def decide(belpex: float, card: TariffCard = IMEWO, *, night: bool = False) -> Action:
    """Curtailment decision for one market slot.

    Order matters: check the (rare) full-shutdown case first, then export clipping.

      1. consume_price < 0  -> FULL_CURTAIL  (grid pays us to consume; producing is a loss)
      2. feedin_price  < 0  -> ZERO_EXPORT   (exporting surplus costs money; still self-consume)
      3. otherwise          -> NORMAL
    """
    if consume_price(belpex, card, night=night) < 0:
        return Action.FULL_CURTAIL
    if feedin_price(belpex, card) < 0:
        return Action.ZERO_EXPORT
    return Action.NORMAL


@dataclass(frozen=True)
class Slot:
    """A priced market slot and its resulting decision (for schedules / logging)."""

    start: str          # ISO-8601 timestamp (Europe/Brussels)
    belpex: float       # EUR/MWh
    night: bool
    consume_price: float
    feedin_price: float
    action: Action

    @classmethod
    def from_belpex(
        cls, start: str, belpex: float, card: TariffCard = IMEWO, *, night: bool = False
    ) -> "Slot":
        return cls(
            start=start,
            belpex=belpex,
            night=night,
            consume_price=round(consume_price(belpex, card, night=night), 4),
            feedin_price=round(feedin_price(belpex, card), 4),
            action=decide(belpex, card, night=night),
        )
