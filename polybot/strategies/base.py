"""Strategie-Interface: Strategien erzeugen Signale, die Execution führt aus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import OrderBook
from polybot.portfolio import Portfolio


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
    replace: bool = False     # eigene ruhende Orders auf dem Token vorher canceln (MM-Quotes)

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class MarketSnapshot:
    """Alles, was eine Strategie pro Tick sieht."""

    markets: list[Market] = field(default_factory=list)
    books: dict[str, OrderBook] = field(default_factory=dict)
    negrisk_events: dict[str, list[Market]] = field(default_factory=dict)
    # Tokenspezifische Taker-Fee-Raten (kategorieabhängig 0.00-0.07);
    # fehlende Tokens fallen auf cfg.risk.taker_fee_rate (Maximum) zurück.
    fee_rates: dict[str, float] = field(default_factory=dict)
    # Aktuelles Portfolio — Strategien mit Inventar (Market Making) brauchen es.
    portfolio: Portfolio | None = None


class Strategy(ABC):
    name: str = "base"

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def fee_rate(self, snap: MarketSnapshot, token_id: str) -> float:
        """Taker-Fee-Rate eines Tokens; Fallback: konfigurierter Maximalsatz."""
        return snap.fee_rates.get(token_id, self.cfg.risk.taker_fee_rate)

    @abstractmethod
    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        """Signale für den aktuellen Tick berechnen."""
