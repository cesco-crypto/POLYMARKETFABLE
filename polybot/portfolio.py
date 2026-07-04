"""Positions- und PnL-Verfolgung, persistiert als JSON (Paper-Modus)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Position:
    token_id: str
    question: str = ""
    shares: float = 0.0
    cost_basis: float = 0.0  # gezahlte USDC


@dataclass
class Fill:
    ts: float
    token_id: str
    side: str
    price: float
    size: float
    reason: str


@dataclass
class Portfolio:
    cash: float = 1000.0  # Start-Cash im Paper-Modus (USDC)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0
    day_start_ts: float = field(default_factory=lambda: time.time())
    day_start_value: float = 1000.0

    def exposure(self, token_id: str) -> float:
        pos = self.positions.get(token_id)
        return pos.cost_basis if pos else 0.0

    def total_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def apply_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        pos = self.positions.setdefault(fill.token_id, Position(token_id=fill.token_id))
        if fill.side == "BUY":
            pos.shares += fill.size
            pos.cost_basis += fill.price * fill.size
            self.cash -= fill.price * fill.size
        else:
            avg_cost = pos.cost_basis / pos.shares if pos.shares > 0 else 0.0
            sold_cost = avg_cost * fill.size
            self.realized_pnl += fill.price * fill.size - sold_cost
            pos.shares -= fill.size
            pos.cost_basis -= sold_cost
            self.cash += fill.price * fill.size
        if pos.shares <= 1e-9:
            self.positions.pop(fill.token_id, None)

    def value(self, marks: dict[str, float] | None = None) -> float:
        """Cash + Positionen (zu Marktpreisen, sonst zu Einstandskosten)."""
        v = self.cash
        for p in self.positions.values():
            if marks and p.token_id in marks:
                v += p.shares * marks[p.token_id]
            else:
                v += p.cost_basis
        return v

    def daily_pnl(self, marks: dict[str, float] | None = None) -> float:
        # neuer Kalendertag (UTC) -> Basis zurücksetzen
        if time.time() - self.day_start_ts > 86_400:
            self.day_start_ts = time.time()
            self.day_start_value = self.value(marks)
        return self.value(marks) - self.day_start_value

    # ---- Persistenz -------------------------------------------------------

    def save(self, path: str | Path = "paper_state.json") -> None:
        data = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "day_start_ts": self.day_start_ts,
            "day_start_value": self.day_start_value,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "fills": [asdict(f) for f in self.fills[-500:]],
        }
        Path(path).write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: str | Path = "paper_state.json", start_cash: float = 1000.0) -> "Portfolio":
        p = Path(path)
        if not p.exists():
            return cls(cash=start_cash, day_start_value=start_cash)
        data = json.loads(p.read_text())
        pf = cls(
            cash=data["cash"],
            realized_pnl=data.get("realized_pnl", 0.0),
            day_start_ts=data.get("day_start_ts", time.time()),
            day_start_value=data.get("day_start_value", start_cash),
        )
        pf.positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        pf.fills = [Fill(**f) for f in data.get("fills", [])]
        return pf
