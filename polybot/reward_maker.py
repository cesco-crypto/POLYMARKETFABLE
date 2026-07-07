"""Watchlist-Shadow-Maker — misst Netto-Yield von Reward-Market-Making.

RISIKOFREI (Paper): simuliert zweiseitige Limit-Quotes INNERHALB des Reward-
Bands auf einer kleinen Watchlist (reward_watchlist.json), verfolgt hypothe-
tische Fills + Inventar und rechnet die pro-rata Reward-Einnahme dazu.

    Netto-Yield = erhaltene Rewards  −  |Adverse-Selection-PnL aus Fills|

Die eine Frage (die über den ganzen Reward-Farming-Weg entscheidet): Überkom-
pensiert die Reward-Einnahme die Adverse Selection, die wir beim Up/Down-Maker
schon gemessen haben (−0.05/Fill)? Erst wenn Netto über längere Zeit positiv
ist, wird über echte Size nachgedacht.

EHRLICHKEITS-VORBEHALTE (im Report ausgewiesen):
- Paper-Maker ohne Queue-Position: Fills werden angenommen, sobald der Markt
  unseren Preis kreuzt → fill_rate/PnL sind eine OBERGRENZE.
- Reward-Anteil pro-rata nach beobachteter Band-Tiefe; echte Konkurrenz kann
  höher sein → Reward-Einnahme ist eher OPTIMISTISCH.
- Inventar wird zum aktuellen Mid markiert (kein Halten bis Auflösung, Märkte
  laufen 20-180 Tage) — das unrealisierte Mark ist ein Momentwert.
- v1 quotet SYMMETRISCH (skew=0): erst die Baseline-Adverse-Selection messen,
  dann ist der Wert eines Modell-Skews als Verbesserung messbar.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from polybot.data.orderbook import BookClient
from polybot.rewards import qualifying_depth

log = logging.getLogger(__name__)

WATCHLIST_PATH = Path("reward_watchlist.json")
STATE_PATH = Path("data") / "reward_maker_state.json"
LOG_PATH = Path("data") / "reward_maker.jsonl"


# ---------------------------------------------------------------------------
# Reine Logik (testbar)
# ---------------------------------------------------------------------------

def round_tick(p: float, tick: float) -> float:
    return round(round(p / tick) * tick, 6)


def quote_prices(mid: float, band: float, tick: float,
                 half_frac: float = 0.8, skew: float = 0.0
                 ) -> tuple[float, float]:
    """Zweiseitige Quotes INNERHALB des Reward-Bands.

    band = max_spread/100 (halbe Bandbreite in Preis). Wir posten Bid/Ask bei
    Mid ± half_frac*band (also knapp INNERHALB, um zu qualifizieren). `skew`
    verschiebt das Zentrum (Modell-Bias; v1 default 0). Auf gültiges Tick-
    Gitter gerundet und in [tick, 1-tick] geklemmt.
    """
    h = half_frac * band
    center = mid + skew
    bid = round_tick(center - h, tick)
    ask = round_tick(center + h, tick)
    bid = max(tick, min(bid, 1 - tick))
    ask = max(tick, min(ask, 1 - tick))
    if ask <= bid:
        ask = round_tick(bid + tick, tick)
    return bid, ask


def detect_fills(bid: float | None, ask: float | None, size: float,
                 best_bid: float | None, best_ask: float | None) -> list[tuple]:
    """Welche unserer ruhenden Quotes hätte der aktuelle Markt gekreuzt?

    Unser Kauf-Gebot (bid) füllt, wenn der best_ask ≤ bid (jemand verkauft in
    uns). Unser Verkaufs-Ask füllt, wenn der best_bid ≥ ask (jemand kauft von
    uns). Rückgabe: Liste von (side, price, size).
    """
    fills = []
    if bid is not None and best_ask is not None and best_ask <= bid:
        fills.append(("buy", bid, size))
    if ask is not None and best_bid is not None and best_bid >= ask:
        fills.append(("sell", ask, size))
    return fills


def reward_accrual(daily_rate: float, our_notional: float,
                   competing_notional: float, dt_s: float) -> float:
    """Pro-rata Reward über dt: rate * our/(competing+our) * dt/Tag."""
    if our_notional <= 0 or dt_s <= 0 or daily_rate <= 0:
        return 0.0
    share = our_notional / (competing_notional + our_notional)
    return daily_rate * share * dt_s / 86400.0


@dataclass
class MakerState:
    """Kumulativer Zustand eines Markts (persistiert über Neustarts)."""
    inv: float = 0.0            # Netto-Shares des Up-Tokens (negativ = long Down)
    cash: float = 0.0          # realisierte Cash-Bewegung aus Fills
    rewards: float = 0.0       # akkumulierte Reward-Einnahme
    buys: int = 0
    sells: int = 0
    max_abs_inv: float = 0.0   # größte |Inventar|-Auslenkung (Risiko-Indikator)
    quote_ticks: int = 0       # Zahl der Ticks mit qualifizierender Quote

    def apply_fill(self, side: str, price: float, size: float) -> None:
        if side == "buy":
            self.inv += size
            self.cash -= size * price
            self.buys += 1
        else:
            self.inv -= size
            self.cash += size * price
            self.sells += 1
        self.max_abs_inv = max(self.max_abs_inv, abs(self.inv))

    def trading_pnl(self, mid: float) -> float:
        """Realisiert + unrealisiert (Inventar zum Mid markiert)."""
        return self.cash + self.inv * mid

    def net(self, mid: float) -> float:
        return self.trading_pnl(mid) + self.rewards


# ---------------------------------------------------------------------------
# Shadow-Loop
# ---------------------------------------------------------------------------

@dataclass
class _Market:
    label: str
    slug: str
    up_token: str
    down_token: str
    daily_rate: float
    min_size: float
    max_spread: float
    min_tick: float
    quotes: tuple[float, float] | None = None   # letzte (bid, ask)
    last_ts: float | None = None


class RewardMakerShadow:
    def __init__(self, watchlist_path: Path = WATCHLIST_PATH,
                 state_path: Path = STATE_PATH, log_path: Path = LOG_PATH,
                 poll_s: float = 5.0, quote_size: float | None = None,
                 half_frac: float = 0.8, skew: float = 0.0,
                 books: BookClient | None = None):
        self.poll_s = poll_s
        self.quote_size = quote_size      # None -> je Markt min_size nutzen
        self.half_frac = half_frac
        self.skew = skew
        self.books = books or BookClient()
        self.state_path = state_path
        self.log_path = log_path
        self.markets = self._load_watchlist(watchlist_path)
        self.state: dict[str, MakerState] = self._load_state()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _load_watchlist(self, path: Path) -> dict[str, _Market]:
        data = json.loads(Path(path).read_text())
        out = {}
        for m in data.get("markets", []):
            out[m["slug"]] = _Market(
                label=m.get("label", m["slug"]), slug=m["slug"],
                up_token=str(m["up_token"]), down_token=str(m["down_token"]),
                daily_rate=float(m["daily_rate"]), min_size=float(m["min_size"]),
                max_spread=float(m["max_spread"]),
                min_tick=float(m.get("min_tick", 0.01)))
        return out

    def _load_state(self) -> dict[str, MakerState]:
        if not self.state_path.exists():
            return {s: MakerState() for s in self.markets}
        raw = json.loads(self.state_path.read_text())
        out = {}
        for slug in self.markets:
            d = raw.get(slug, {})
            out[slug] = MakerState(**{k: d[k] for k in d
                                      if k in MakerState.__dataclass_fields__})
        return out

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {slug: st.__dict__ for slug, st in self.state.items()}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw))
        tmp.replace(self.state_path)

    def _size_for(self, m: _Market) -> float:
        return self.quote_size if self.quote_size is not None else m.min_size

    def tick(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        tokens = [t for m in self.markets.values()
                  for t in (m.up_token, m.down_token)]
        obooks = self.books.get_books(tokens)
        rows = 0
        for slug, m in self.markets.items():
            book = obooks.get(m.up_token)
            if book is None or book.midpoint is None:
                continue
            mid = book.midpoint
            bb = book.best_bid.price if book.best_bid else None
            ba = book.best_ask.price if book.best_ask else None
            st = self.state[slug]
            band = m.max_spread / 100.0
            size = self._size_for(m)

            # 1) Fills unserer VORHERIGEN Quotes am aktuellen Buch prüfen.
            if m.quotes is not None:
                for side, price, sz in detect_fills(m.quotes[0], m.quotes[1],
                                                    size, bb, ba):
                    st.apply_fill(side, price, sz)

            # 2) Reward für das verstrichene Intervall pro-rata gutschreiben.
            if m.last_ts is not None:
                dt = now - m.last_ts
                comp = qualifying_depth(book, mid, m.max_spread)
                our_notional = size * mid * 2      # beidseitig
                st.rewards += reward_accrual(m.daily_rate, our_notional, comp, dt)
                st.quote_ticks += 1

            # 3) Neue Quotes fürs nächste Intervall setzen.
            m.quotes = quote_prices(mid, band, m.min_tick, self.half_frac, self.skew)
            m.last_ts = now

            self._write_log({
                "ts": round(now, 3), "slug": slug, "mid": round(mid, 4),
                "inv": round(st.inv, 2), "cash": round(st.cash, 4),
                "rewards": round(st.rewards, 4), "net": round(st.net(mid), 4),
                "buys": st.buys, "sells": st.sells})
            rows += 1
        self._save_state()
        return rows

    def _write_log(self, row: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def run(self) -> None:  # pragma: no cover — Langläufer
        log.info("Reward-Maker-Shadow gestartet (%d Märkte, size=%s, skew=%.3f)",
                 len(self.markets), self.quote_size or "min_size", self.skew)
        try:
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    self.tick()
                except Exception as e:
                    log.warning("Reward-Maker-Tick-Fehler: %s", e)
                self._stop.wait(max(0.0, self.poll_s - (time.time() - t0)))
        finally:
            self._save_state()

    # -- Report ----------------------------------------------------------
    def summary(self, now_mid: dict[str, float] | None = None) -> list[dict]:
        out = []
        for slug, m in self.markets.items():
            st = self.state[slug]
            mid = (now_mid or {}).get(slug, 0.5)
            capital = self._size_for(m) * 2 * mid    # grob eingesetztes Kapital
            out.append({
                "label": m.label, "slug": slug,
                "rewards": st.rewards, "trading_pnl": st.trading_pnl(mid),
                "net": st.net(mid), "inv": st.inv, "max_abs_inv": st.max_abs_inv,
                "buys": st.buys, "sells": st.sells, "quote_ticks": st.quote_ticks,
                "capital": capital,
                "net_yield_pct": (100.0 * st.net(mid) / capital) if capital else None,
            })
        return out
