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

    def check_daily_loss(self, portfolio: Portfolio) -> None:
        if portfolio.daily_pnl() <= -self.cfg.risk.daily_loss_limit_usdc:
            raise KillSwitch(
                f"Tagesverlust {portfolio.daily_pnl():.2f} USDC — "
                f"Limit {self.cfg.risk.daily_loss_limit_usdc:.2f} erreicht. Bot stoppt."
            )

    def filter(self, signals: list[Signal], portfolio: Portfolio) -> list[Signal]:
        r = self.cfg.risk
        approved: list[Signal] = []
        rejected_groups: set[str] = set()
        grouped: dict[str | None, list[Signal]] = defaultdict(list)
        for s in signals:
            grouped[s.group].append(s)

        planned_exposure = 0.0
        for group, group_signals in grouped.items():
            reasons = []
            for s in group_signals:
                if s.side == "BUY":
                    if s.notional > r.max_order_usdc * 1.01:
                        reasons.append(f"Ordergröße {s.notional:.2f} > Limit {r.max_order_usdc}")
                    pos = portfolio.exposure(s.token_id)
                    if pos + s.notional > r.max_position_usdc:
                        reasons.append(f"Positionslimit {r.max_position_usdc} überschritten")
                    total = portfolio.total_exposure() + planned_exposure + s.notional
                    if total > r.max_total_exposure_usdc:
                        reasons.append(f"Gesamt-Exposure-Limit {r.max_total_exposure_usdc} überschritten")
                if not (0.001 <= s.price <= 0.999):
                    reasons.append(f"Preis {s.price} außerhalb des gültigen Bereichs")
            if reasons:
                if group:
                    rejected_groups.add(group)
                log.debug("Signalgruppe %s abgelehnt: %s", group, "; ".join(reasons))
                continue
            approved.extend(group_signals)
            planned_exposure += sum(s.notional for s in group_signals if s.side == "BUY")

        # Sicherheitsnetz: keine Signale aus abgelehnten Gruppen durchlassen
        return [s for s in approved if s.group not in rejected_groups]
