"""KPI cards: a number, a comparison, and a sparkline.

Each card states the question it answers in plain language, because a number with no
question attached invites everyone to supply their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0.00")


@dataclass
class Kpi:
    key: str
    # The plain language question this card answers.
    question: str
    label: str
    value: Decimal | int
    kind: str  # "currency" or "count"
    previous: Decimal | int | None = None
    # Where clicking the card goes. None means the card is not a link.
    href: str | None = None
    sparkline: list[float] = field(default_factory=list)
    note: str | None = None
    # True when a fall is the good direction, as with outstanding balances.
    lower_is_better: bool = False
    # Why this card carries no comparison, when the absence is worth explaining.
    # A card that simply never has one (a current headcount, say) leaves it None.
    no_comparison_reason: str | None = None

    @property
    def delta(self) -> Decimal | int | None:
        if self.previous is None:
            return None
        return self.value - self.previous

    @property
    def delta_percent(self) -> Decimal | None:
        if self.previous is None:
            return None
        previous = Decimal(self.previous)
        if previous == 0:
            return None
        return ((Decimal(self.value) - previous) / abs(previous) * 100).quantize(Decimal("0.1"))

    @property
    def direction(self) -> str:
        """up, down, or flat. Purely about the arithmetic."""
        delta = self.delta
        if delta is None or delta == 0:
            return "flat"
        return "up" if delta > 0 else "down"

    @property
    def tone(self) -> str:
        """good, bad, or neutral, which is not the same as up or down.

        Outstanding balances falling is good news and rising is bad, the opposite of
        collections. Colour follows the meaning, not the arrow.
        """
        direction = self.direction
        if direction == "flat":
            return "neutral"
        rising = direction == "up"
        return "bad" if rising == self.lower_is_better else "good"

    @property
    def has_comparison(self) -> bool:
        return self.previous is not None
