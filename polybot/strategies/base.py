"""Strategie-Interface: Strategien erzeugen Signale, die Execution führt aus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import OrderBook


@dataclass
class Signal:
    """Eine gewünschte Order. Größen in Shares, Preise in USDC (0–1)."""

    token_id: str
    side: str                 # "BUY" | "SELL"
    price: float
    size: float               # Anzahl Shares
    reason: str
    market_question: str = ""
    group: str | None = None  # Signale derselben Gruppe gehören zusammen (Arb-Beine)
    expected_edge: float = 0.0  # erwarteter Gewinn in USDC pro Share-Bündel
    neg_risk: bool = False    # Markt läuft über den NegRisk-Exchange (andere Signatur-Domain)

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class MarketSnapshot:
    """Alles, was eine Strategie pro Tick sieht."""

    markets: list[Market] = field(default_factory=list)
    books: dict[str, OrderBook] = field(default_factory=dict)
    negrisk_events: dict[str, list[Market]] = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    @abstractmethod
    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        """Signale für den aktuellen Tick berechnen."""
