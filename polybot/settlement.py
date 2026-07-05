"""Resolution-Sweeper: Kapital aufgelöster Märkte ausbuchen (Deadlock-Fix).

Befund der Agenten-Flotte (05.07.2026, mehrfach verifiziert): Es gab
KEINEN Code-Pfad, der Exposure oder Cash je freigab — live_auto_merge ist
aus (Deposit-Wallet), Redeem war nicht gebaut. portfolio.total_exposure()
wuchs monoton; nach ~7-8 gefüllten Paaren (150er-Exposure-Cap) bzw. ~30
Paaren (Cash) stoppte der Live-Bot DAUERHAFT, obwohl die Chain längst
ausgezahlt hatte (verifiziertes REDEEM-Event +5 USDC, ~36 min nach
Auflösung).

Der Sweeper prüft gedrosselt (SWEEP_INTERVAL_S) alle gehaltenen Tokens
per Gamma-Token-Lookup und bucht Positionen FINAL aufgelöster Märkte zur
Auszahlung aus: Gewinner 1 USDC/Share, Verlierer 0 (realisierter Verlust
— ehrlich, kein ewiger Bestand). Final heisst closed + umaResolutionStatus
'resolved' + outcomePrices eindeutig 0/1 (Market.resolved_payouts).

Ehrliche Grenze im Live-Modus: Die Cash-Gutschrift setzt voraus, dass die
Auszahlung on-chain tatsächlich ankommt (Polymarket-Auto-Redeem ist beim
Betreiber aktiv und wurde per Data-API-REDEEM-Event verifiziert). Bucht
der Sweeper schneller als die Chain, lehnt die Börse überschüssige Orders
per Balance-Check ab (Reject-Cooldown fängt das) — die Buchhaltung
konvergiert mit der Chain, sobald das Redeem durch ist. Ein aktiver
Deposit-Wallet-Batch-Redeem ist der Folgeausbau.

Wie alle Messpfad-/Recycling-Komponenten gilt: darf den Tick NIE crashen.
"""

from __future__ import annotations

import logging
import time

from polybot.portfolio import Portfolio

log = logging.getLogger(__name__)


class SettlementSweeper:
    """Bucht Positionen final aufgelöster Märkte zur Auszahlung aus."""

    # Auflösungs-Status ändert sich im Minutentakt, nicht pro 0.5s-Tick —
    # und jeder Sweep kostet 1-2 Gamma-Requests fürs gesamte Inventar.
    SWEEP_INTERVAL_S = 60.0

    def __init__(self, gamma, interval_s: float | None = None):
        self.gamma = gamma
        self.interval_s = self.SWEEP_INTERVAL_S if interval_s is None else interval_s
        self._last_sweep = 0.0

    def sweep(self, portfolio: Portfolio, ledger=None,
              now: float | None = None) -> float:
        """Aufgelöste Positionen ausbuchen; Rückgabe: ausgezahlte USDC.

        Crasht nie den Tick: Gamma-Fehler werden geloggt und beim nächsten
        Intervall erneut versucht.
        """
        now = time.time() if now is None else now
        # Auch Tokens ruhender Orders prüfen: Quotes auf aufgelösten Märkten
        # binden sonst für immer Cash-Reservierung (Befund 3/22).
        tokens = set(portfolio.positions) | {o.token_id
                                             for o in portfolio.resting_orders}
        tokens -= set(portfolio.settled)  # nie doppelt auszahlen (Befund 2/20)
        if now - self._last_sweep < self.interval_s or not tokens:
            return 0.0
        self._last_sweep = now
        try:
            markets = self.gamma.markets_by_tokens(sorted(tokens))
        except Exception as e:  # noqa: BLE001 — Sweep nie tick-kritisch
            log.warning("Settlement-Sweep: Gamma-Lookup fehlgeschlagen: %s", e)
            return 0.0
        paid_total = 0.0
        for m in markets:
            payouts = m.resolved_payouts()
            if payouts is None:
                continue
            for token, payout_per_share in ((m.yes_token, payouts[0]),
                                            (m.no_token, payouts[1])):
                if token not in tokens:
                    continue
                before = portfolio.realized_pnl
                paid = portfolio.settle_position(token, payout_per_share,
                                                 question=m.question)
                paid_total += paid
                if ledger is not None:
                    try:
                        ledger.record_merge(m.question, "settlement",
                                            0.0, portfolio.realized_pnl - before)
                    except Exception:  # noqa: BLE001 — Reporting nie kritisch
                        log.debug("Settlement-Ledger-Eintrag fehlgeschlagen")
        return paid_total
