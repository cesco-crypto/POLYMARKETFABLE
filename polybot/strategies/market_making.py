"""Einfaches Market Making: passive Quotes hinter dem Best-Bid/Ask.

Verdient den Spread, trägt aber Inventar-Risiko (adverse selection):
Bewegt sich der Markt gegen die Position, verliert der Market Maker.
Deshalb: enge Inventar-Limits und nur die liquidesten Märkte — gequotet
wird ausschließlich in den Top-mm_max_markets nach 24h-Volumen und nur,
wenn der Spread eng ist (höchstens mm_spread; ein weiter Spread heißt
illiquide/unsicher bepreist, dort ist die adverse selection am größten).

Preissetzung: immer mindestens 1 Tick HINTER dem Best-Bid/Ask (Bid unter
dem besten Bid, Ask über dem besten Ask) — nie aggressiv. So ist jeder
Fill garantiert ein Maker-Fill (Gebühr 0 + Rebate); eine Quote am oder im
Touch könnte als Taker füllen und Gebühren zahlen.

Diese Strategie ist bewusst konservativ und standardmäßig deaktiviert —
zuerst im Paper-Modus beobachten.

Order-Lifecycle: Die Quotes tragen replace=True — der LiveBroker cancelt
vor dem Neu-Quoten die zuvor platzierten Orders desselben Tokens, damit
sich keine veralteten GTC-Quotes im Buch stapeln. Der PaperBroker bildet
dieselbe Semantik mit ruhenden Orders nach: nicht-marketable Quotes ruhen
im Portfolio-State und füllen erst bei Preisdurchgang als Maker (Gebühr 0,
Rebate tokenspezifisch 20% der Taker-Fee, Fallback
strategy.maker_rebate_rate); ein neues replace-Signal ersetzt die alte
ruhende Order desselben Tokens.
"""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.strategies.base import MarketSnapshot, Signal, Strategy

log = logging.getLogger(__name__)

# Standard-Tick auf Polymarket für Preise im mittleren Bereich; näher an
# 0/1 wechselt der Tick auf 0.001, dort quoten wir wegen des Mittelkurs-
# Filters (0.10-0.90) ohnehin nicht.
TICK = 0.01


def candidate_markets(cfg: BotConfig, markets: list[Market]) -> list[Market]:
    """MM-Kandidaten: die Top-N-Märkte nach 24h-Volumen.

    Auch von main._load_books genutzt: der Zweistufen-Scan lädt volle
    Bücher genau für diese Kandidaten zusätzlich zu den Arb-Kandidaten —
    NICHT für alle Märkte (das würde den Tick massiv verlangsamen).
    """
    n = cfg.strategy.mm_max_markets
    return sorted(markets, key=lambda m: m.volume_24h, reverse=True)[:n]


class MarketMaking(Strategy):
    name = "market_making"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []
        s = self.cfg.strategy

        for m in candidate_markets(self.cfg, snap.markets):
            book = snap.books.get(m.yes_token)
            if not book or not book.best_bid or not book.best_ask:
                continue
            mid = book.midpoint
            if mid is None or not (0.10 <= mid <= 0.90):
                continue  # extreme Preise meiden (Auflösungsnähe, Tail-Risiko)

            spread = book.best_ask.price - book.best_bid.price
            if spread > s.mm_spread + 1e-9:
                continue  # weiter Spread = illiquide/unsicher bepreist — auslassen

            # Nie aggressiv: mindestens 1 Tick hinter dem Touch quoten —
            # garantiert Maker-Fills und kreuzt mathematisch nie die
            # Gegenseite (Bid < Best-Bid < Best-Ask < Ask).
            bid_px = round(book.best_bid.price - TICK, 3)
            ask_px = round(book.best_ask.price + TICK, 3)
            if not (0.0 < bid_px and ask_px < 1.0):
                continue
            size = s.mm_size_usdc / max(mid, 0.05)

            pf = snap.portfolio
            held = pf.positions.get(m.yes_token) if pf else None
            held_shares = held.shares if held else 0.0

            # Inventar-Limit: Bid nur, solange gebundenes Kapital im Markt
            # unter mm_max_inventory_usdc bleibt; sonst kappen bzw. auslassen.
            exposure = pf.exposure(m.yes_token) if pf else 0.0
            room = s.mm_max_inventory_usdc - exposure
            bid_size = min(size, room / max(bid_px, 1e-9))
            if bid_size >= s.min_order_shares:
                signals.append(Signal(
                    token_id=m.yes_token, side="BUY", price=bid_px, size=bid_size,
                    reason="MM Bid", market_question=m.question, replace=True,
                ))

            # Ask nur gegen tatsächlich gehaltene Shares — Polymarket erlaubt
            # kein Shorting, und ein ungedeckter Paper-SELL wäre Phantom-Gewinn.
            ask_size = min(size, held_shares)
            if ask_size >= s.min_order_shares:
                signals.append(Signal(
                    token_id=m.yes_token, side="SELL", price=ask_px, size=ask_size,
                    reason="MM Ask", market_question=m.question, replace=True,
                ))
        return signals
