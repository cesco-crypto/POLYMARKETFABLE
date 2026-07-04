"""Gecachter Lookup der Taker-Fee-Raten pro Token.

Quelle sind die Gamma-Marktobjekte (`feesEnabled` + `feeSchedule.rate`,
siehe gamma._parse_fee_rate) — sie kommen mit jedem Snapshot ohnehin mit,
der Lookup kostet also keine zusätzlichen Requests. Der Cache lebt über
Ticks hinweg: fehlt die Fee-Info eines Markts in einem späteren Tick,
bleibt die zuletzt gesehene Rate erhalten. Tokens ohne bekannte Rate
fehlen im Ergebnis — Aufrufer fallen auf cfg.risk.taker_fee_rate
(konservativ das Kategorien-Maximum) zurück.
"""

from __future__ import annotations

from collections.abc import Iterable

from polybot.data.gamma import Market


class FeeRateCache:
    """Cache token_id -> Taker-Fee-Rate, gespeist aus Gamma-Märkten."""

    def __init__(self) -> None:
        self._rates: dict[str, float] = {}

    def update_from_markets(self, markets: Iterable[Market]) -> None:
        """Raten aus Marktobjekten übernehmen (Rate gilt für YES wie NO)."""
        for m in markets:
            if m.fee_rate is None:
                continue
            self._rates[m.yes_token] = m.fee_rate
            self._rates[m.no_token] = m.fee_rate

    def rates_for(self, token_ids: Iterable[str]) -> dict[str, float]:
        """Bekannte Raten für die angefragten Tokens; Unbekannte fehlen."""
        return {t: self._rates[t] for t in token_ids if t in self._rates}
