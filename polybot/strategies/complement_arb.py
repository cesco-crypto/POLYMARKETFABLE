"""Komplement-Arbitrage in binären Märkten.

In jedem binären Markt gilt bei Auflösung: 1 YES-Share + 1 NO-Share = 1 USDC.
Kostet (bester YES-Ask + bester NO-Ask) < 1 USDC minus Puffer, kauft der Bot
beide Seiten und hält bis zur Auflösung (oder merged die Paare zu USDC).
Das ist — abgesehen von Ausführungsrisiko — eine risikofreie Struktur.

Solche Fenster sind selten und klein, weil viele Bots sie jagen. Genau
deshalb prüft der Bot die Tiefe des Orderbuchs und nicht nur Top-of-Book.
"""

from __future__ import annotations

import logging

from polybot.strategies.base import MarketSnapshot, Signal, Strategy

log = logging.getLogger(__name__)


class ComplementArb(Strategy):
    name = "complement_arb"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []
        min_edge = self.cfg.risk.min_edge
        max_order = self.cfg.risk.max_order_usdc

        # NegRisk-Teilmärkte, die negrisk_arb bereits bearbeitet, auslassen:
        # sonst dimensionieren beide Strategien im selben Tick gegen dieselbe
        # Top-of-Book-Liquidität (doppelt verplante Asks, halb gefüllte Arbs).
        negrisk_tokens = {
            t
            for ev_markets in snap.negrisk_events.values()
            for nm in ev_markets
            for t in (nm.yes_token, nm.no_token)
        }

        for m in snap.markets:
            if m.yes_token in negrisk_tokens or m.no_token in negrisk_tokens:
                continue
            yes_book = snap.books.get(m.yes_token)
            no_book = snap.books.get(m.no_token)
            if not yes_book or not no_book:
                continue
            ya, na = yes_book.best_ask, no_book.best_ask
            if not ya or not na:
                continue

            cost = ya.price + na.price
            # Taker-Gebühr (2026): rate * p * (1-p) pro Share, auf beiden
            # Beinen — mit der tokenspezifischen (kategorieabhängigen) Rate.
            fees = (
                self.fee_rate(snap, m.yes_token) * ya.price * (1 - ya.price)
                + self.fee_rate(snap, m.no_token) * na.price * (1 - na.price)
            )
            edge = 1.0 - cost - fees
            if edge < min_edge:
                continue

            # Größe: begrenzt durch beide Ask-Level und das Order-Limit —
            # inkl. Gebühren, damit der reale Cash-Abfluss max_order nicht sprengt
            size = min(ya.size, na.size, max_order / max(cost + fees, 1e-9))
            if size < 5:  # Mindestgröße, sonst lohnt es sich nicht
                continue

            group = f"comp:{m.condition_id[:12]}"
            log.info(
                "Komplement-Arb: '%s' YES@%.3f + NO@%.3f = %.3f (Edge %.3f, Größe %.0f)",
                m.question[:60], ya.price, na.price, cost, edge, size,
            )
            signals.append(Signal(
                token_id=m.yes_token, side="BUY", price=ya.price, size=size,
                reason=f"Komplement-Arb Edge={edge:.3f}", market_question=m.question,
                group=group, expected_edge=edge * size, neg_risk=m.neg_risk,
            ))
            signals.append(Signal(
                token_id=m.no_token, side="BUY", price=na.price, size=size,
                reason=f"Komplement-Arb Edge={edge:.3f}", market_question=m.question,
                group=group, expected_edge=0.0, neg_risk=m.neg_risk,
            ))
        return signals
