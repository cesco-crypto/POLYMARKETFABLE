from polybot.strategies.base import Signal, Strategy
from polybot.strategies.complement_arb import ComplementArb
from polybot.strategies.negrisk_arb import NegRiskArb
from polybot.strategies.market_making import MarketMaking

REGISTRY: dict[str, type[Strategy]] = {
    "complement_arb": ComplementArb,
    "negrisk_arb": NegRiskArb,
    "market_making": MarketMaking,
}

__all__ = ["Signal", "Strategy", "ComplementArb", "NegRiskArb", "MarketMaking", "REGISTRY"]
