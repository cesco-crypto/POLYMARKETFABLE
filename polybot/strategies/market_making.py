"""Einfaches Market Making: beidseitige Quotes um den Mittelkurs.

Verdient den Spread, trägt aber Inventar-Risiko (adverse selection):
Bewegt sich der Markt gegen die Position, verliert der Market Maker.
Deshalb: enge Inventar-Limits und nur liquide Märkte mit stabilem Spread.

Diese Strategie ist bewusst konservativ und standardmäßig deaktiviert —
zuerst im Paper-Modus beobachten.

Order-Lifecycle: Die Quotes tragen replace=True — der LiveBroker cancelt
vor dem Neu-Quoten die zuvor platzierten Orders desselben Tokens, damit
sich keine veralteten GTC-Quotes im Buch stapeln. Der PaperBroker bildet
dieselbe Semantik mit ruhenden Orders nach: nicht-marketable Quotes ruhen
im Portfolio-State und füllen erst bei Preisdurchgang als Maker (Gebühr 0,
optional Rebate über strategy.maker_rebate_rate); ein neues replace-Signal
ersetzt die alte ruhende Order desselben Tokens.
"""

from __future__ import annotations

import logging

from polybot.strategies.base import MarketSnapshot, Signal, Strategy

log = logging.getLogger(__name__)


class MarketMaking(Strategy):
    name = "market_making"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []
        s = self.cfg.strategy

        for m in snap.markets:
            book = snap.books.get(m.yes_token)
            if not book or not book.best_bid or not book.best_ask:
                continue
            mid = book.midpoint
            if mid is None or not (0.10 <= mid <= 0.90):
                continue  # extreme Preise meiden (Auflösungsnähe, Tail-Risiko)

            spread = book.best_ask.price - book.best_bid.price
            if spread < 2 * s.mm_spread:
                continue  # Spread zu eng, kein Platz für unsere Quotes

            bid_px = round(mid - s.mm_spread, 3)
            ask_px = round(mid + s.mm_spread, 3)
            # Nicht-marketable Quotes sind Pflicht: wir verdienen als Maker
            # (Gebühr 0 + Rebate) — eine Quote, die die Gegenseite kreuzt,
            # würde als Taker füllen und Gebühren zahlen. Durch die
            # Spread-Bedingung oben mathematisch garantiert; defensiv gegen
            # Rundungs-/Randfälle trotzdem prüfen.
            if bid_px >= book.best_ask.price or ask_px <= book.best_bid.price:
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
