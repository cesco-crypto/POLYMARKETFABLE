"""Risk-Management: harte Limits und Kill-Switch.

Jedes Signal muss hier durch, bevor es zur Ausführung geht. Bei Arbitrage-
Gruppen gilt: alle Beine oder keines — ein halb ausgeführter Arb ist eine
offene Wette, keine Arbitrage.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from polybot.config import BotConfig
from polybot.portfolio import Portfolio
from polybot.strategies.base import Signal

log = logging.getLogger(__name__)


class KillSwitch(Exception):
    """Tagesverlustgrenze erreicht — Bot stoppt sofort."""


class RiskManager:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def check_daily_loss(self, portfolio: Portfolio,
                         marks: dict[str, float] | None = None) -> None:
        # Mit marks (Midpoints) zählen auch unrealisierte Verluste — ohne
        # marks würden Positionen zum Einstandswert bewertet und der
        # Kill-Switch wäre blind für Mark-to-Market-Verluste.
        pnl = portfolio.daily_pnl(marks)
        if pnl <= -self.cfg.risk.daily_loss_limit_usdc:
            raise KillSwitch(
                f"Tagesverlust {pnl:.2f} USDC — "
                f"Limit {self.cfg.risk.daily_loss_limit_usdc:.2f} erreicht. Bot stoppt."
            )

    def filter(self, signals: list[Signal], portfolio: Portfolio) -> list[Signal]:
        r = self.cfg.risk
        approved: list[Signal] = []
        rejected_groups: set[str] = set()
        # Ungruppierte Signale (group=None, z.B. MM-Quotes) sind unabhängig
        # und bekommen je einen eigenen Schlüssel — die Alles-oder-nichts-
        # Semantik gilt nur für echte Arb-Gruppen.
        grouped: dict[object, list[Signal]] = defaultdict(list)
        for i, s in enumerate(signals):
            grouped[s.group if s.group is not None else ("_solo_", i)].append(s)

        planned_exposure = 0.0
        planned_per_token: dict[str, float] = defaultdict(float)
        for group, group_signals in grouped.items():
            reasons = []
            # Gruppenlimits über die SUMME aller Beine prüfen — Bein für Bein
            # würde eine n-Bein-Gruppe das Gesamtlimit n-fach überschreiten.
            group_buy_notional = sum(s.notional for s in group_signals if s.side == "BUY")
            total = portfolio.total_exposure() + planned_exposure + group_buy_notional
            if total > r.max_total_exposure_usdc:
                reasons.append(f"Gesamt-Exposure-Limit {r.max_total_exposure_usdc} überschritten")
            if planned_exposure + group_buy_notional > portfolio.cash:
                reasons.append(f"Unzureichendes Cash ({portfolio.cash:.2f} USDC)")
            group_per_token: dict[str, float] = defaultdict(float)
            for s in group_signals:
                if s.size <= 0:
                    reasons.append(f"Ungültige Ordergröße {s.size}")
                if s.side == "BUY":
                    if s.notional > r.max_order_usdc * 1.01:
                        reasons.append(f"Ordergröße {s.notional:.2f} > Limit {r.max_order_usdc}")
                    # Bereits gefüllte Position + in diesem Tick schon geplante
                    # Käufe desselben Tokens (andere Gruppen UND frühere Beine
                    # dieser Gruppe) anrechnen.
                    pos = (portfolio.exposure(s.token_id)
                           + planned_per_token[s.token_id] + group_per_token[s.token_id])
                    if pos + s.notional > r.max_position_usdc:
                        reasons.append(f"Positionslimit {r.max_position_usdc} überschritten")
                    group_per_token[s.token_id] += s.notional
                else:
                    # SELL nur gegen tatsächlichen Bestand — Polymarket erlaubt
                    # kein Shorting von Conditional Tokens.
                    pos = portfolio.positions.get(s.token_id)
                    held = pos.shares if pos else 0.0
                    if s.size > held + 1e-9:
                        reasons.append(f"SELL {s.size:.2f} > Bestand {held:.2f} Shares (kein Shorting)")
                if not (0.001 <= s.price <= 0.999):
                    reasons.append(f"Preis {s.price} außerhalb des gültigen Bereichs")
            if reasons:
                if isinstance(group, str):
                    rejected_groups.add(group)
                log.debug("Signalgruppe %s abgelehnt: %s", group, "; ".join(reasons))
                continue
            approved.extend(group_signals)
            planned_exposure += group_buy_notional
            for token_id, notional in group_per_token.items():
                planned_per_token[token_id] += notional

        # Sicherheitsnetz: keine Signale aus abgelehnten Gruppen durchlassen
        return [s for s in approved if s.group not in rejected_groups]
