"""NegRisk-Arbitrage in Multi-Outcome-Events.

In einem Event mit n sich gegenseitig ausschließenden Outcomes gewinnt genau
eines. Daraus folgen zwei Arbitrage-Strukturen:

1. Summe aller besten YES-Asks < 1.00  →  alle YES kaufen.
   Auszahlung bei Auflösung: genau 1 USDC (das gewinnende Outcome).

2. Summe aller besten NO-Asks < (n - 1)  →  alle NO kaufen.
   Auszahlung: n-1 USDC (alle NO außer dem des Gewinners zahlen aus).

Voraussetzung: wirklich ALLE Outcomes des Events sind handelbar erfasst —
fehlt eines, ist Struktur (1) nicht mehr risikofrei. Der Bot handelt deshalb
nur Events, bei denen Polymarket das negRisk-Flag setzt und alle Teilmärkte
offen sind (gamma.negrisk_events überspringt Events mit geschlossenen
Teilmärkten komplett). Bei negRiskAugmented-Events kann Polymarket später
Outcomes hinzufügen — dort ist nur Struktur (2) risikofrei (Auszahlung
>= n-1 auch bei neuem Gewinner-Outcome), Struktur (1) wird ausgelassen.
"""

from __future__ import annotations

import logging

from polybot.strategies.base import MarketSnapshot, Signal, Strategy

log = logging.getLogger(__name__)


class NegRiskArb(Strategy):
    name = "negrisk_arb"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []
        for slug, markets in snap.negrisk_events.items():
            signals.extend(self._check_event(slug, markets, snap))
        return signals

    def _check_event(self, slug: str, markets, snap: MarketSnapshot) -> list[Signal]:
        min_edge = self.cfg.risk.min_edge
        max_order = self.cfg.risk.max_order_usdc
        min_shares = self.cfg.strategy.min_order_shares
        n = len(markets)

        yes_asks, no_asks = [], []
        for m in markets:
            yb = snap.books.get(m.yes_token)
            nb = snap.books.get(m.no_token)
            if not yb or not yb.best_ask or not nb or not nb.best_ask:
                return []  # unvollständiges Event -> nicht risikofrei -> auslassen
            yes_asks.append((m, yb.best_ask))
            no_asks.append((m, nb.best_ask))

        out: list[Signal] = []

        # Struktur 1: alle YES kaufen — nur risikofrei, wenn die Outcome-Menge
        # garantiert vollständig bleibt; bei negRiskAugmented-Events kann
        # Polymarket Outcomes nachschieben -> YES-Struktur auslassen.
        yes_complete = not any(m.neg_risk_augmented for m in markets)
        yes_cost = sum(a.price for _, a in yes_asks)
        yes_fees = sum(
            self.fee_rate(snap, m.yes_token) * a.price * (1 - a.price)
            for m, a in yes_asks
        )
        edge = 1.0 - yes_cost - yes_fees
        if yes_complete and edge >= min_edge:
            # Gebühren im Nenner: realer Cash-Abfluss <= max_order_usdc
            size = min(min(a.size for _, a in yes_asks),
                       max_order / max(yes_cost + yes_fees, 1e-9))
            if size >= min_shares:
                log.info("NegRisk-Arb (YES) in '%s': Kosten %.3f, Edge %.3f, Größe %.0f",
                         slug, yes_cost, edge, size)
                group = f"nrY:{slug[:20]}"
                for i, (m, a) in enumerate(yes_asks):
                    out.append(Signal(
                        token_id=m.yes_token, side="BUY", price=a.price, size=size,
                        reason=f"NegRisk-YES-Arb Edge={edge:.3f}", market_question=m.question,
                        group=group, expected_edge=edge * size if i == 0 else 0.0,
                        neg_risk=True,
                    ))

        # Struktur 2: alle NO kaufen (auch bei negRiskAugmented risikofrei:
        # gewinnt ein später hinzugefügtes Outcome, zahlen sogar alle n NO aus)
        no_cost = sum(a.price for _, a in no_asks)
        no_fees = sum(
            self.fee_rate(snap, m.no_token) * a.price * (1 - a.price)
            for m, a in no_asks
        )
        edge_no = (n - 1) - no_cost - no_fees
        if edge_no >= min_edge:
            size = min(min(a.size for _, a in no_asks),
                       max_order / max(no_cost + no_fees, 1e-9))
            if size >= min_shares:
                log.info("NegRisk-Arb (NO) in '%s': Kosten %.3f, Auszahlung %d, Edge %.3f, Größe %.0f",
                         slug, no_cost, n - 1, edge_no, size)
                group = f"nrN:{slug[:20]}"
                for i, (m, a) in enumerate(no_asks):
                    out.append(Signal(
                        token_id=m.no_token, side="BUY", price=a.price, size=size,
                        reason=f"NegRisk-NO-Arb Edge={edge_no:.3f}", market_question=m.question,
                        group=group, expected_edge=edge_no * size if i == 0 else 0.0,
                        neg_risk=True,
                    ))
        return out
