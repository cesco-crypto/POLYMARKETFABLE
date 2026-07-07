"""Reward-Band-Scanner — welche Märkte zahlen echtes Liquidity-Yield?

Befund 07.07.2026: Die 5-Min-Krypto-Up/Down-Märkte zahlen NULL Rewards; der
kapital-skalierbare Weg (Flotten-Empfehlung) ist Model-skewed Market-Making
auf den ~7000 reward-tragenden Märkten (CLOB /sampling-markets, Tagesraten
0.001–1584 USDC/Tag). Dieser Scanner ist Schritt 1: die lohnenden Bänder
finden, die kurzlebigen/in-play-Fallen aussortieren, und den YIELD PRO KAPITAL
schätzen — Rewards werden PRO RATA nach Anteil an der qualifizierenden
Liquidität ausgezahlt, also zählt nicht die Tagesrate allein, sondern
Tagesrate / bereits liegende Konkurrenz-Tiefe im Reward-Band.

WICHTIG (Ehrlichkeit): eine hohe Tagesrate auf einem LEEREN Band ist ein
Trugbild — das Band ist leer, WEIL ruhende Maker dort abgeschossen werden
(Adverse Selection). Yield-Schätzung UND spätere Netto-Messung (Shadow-Maker
+ Inventar-PnL) gehören zusammen; dieser Scanner liefert nur die Kandidaten.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from polybot.config import CLOB_HOST
from polybot.data.orderbook import BookClient, OrderBook

log = logging.getLogger(__name__)

SAMPLING_PATH = "/sampling-markets"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # pUSD auf Polygon


@dataclass
class RewardMarket:
    condition_id: str
    slug: str
    question: str
    up_token: str
    down_token: str
    daily_rate: float          # Summe rewards_daily_rate (USDC/Tag) des Markts
    min_size: float            # min. qualifizierende Ordergröße (Shares)
    max_spread: float          # max. Abstand vom Mid in CENTS zum Qualifizieren
    min_tick: float
    end_ts: float | None
    seconds_delay: float
    game_start_ts: float | None
    accepting: bool
    prices: tuple[float, float] = (0.0, 0.0)  # (up, down) letzter Preis

    def inplay(self, now: float) -> bool:
        """Läuft ein Spiel bereits UND matcht der Markt nur verzögert?"""
        if self.seconds_delay <= 0 or self.game_start_ts is None:
            return False
        return now >= self.game_start_ts

    def tradeable(self, now: float, min_time_to_end_s: float) -> bool:
        if not self.accepting:
            return False
        if self.inplay(now):
            return False
        if self.end_ts is not None and self.end_ts - now <= min_time_to_end_s:
            return False
        return True


def _to_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def daily_rate(m: dict) -> float:
    """Summe der rewards_daily_rate (nur USDC-Asset) eines sampling-market."""
    rw = m.get("rewards") or {}
    total = 0.0
    for rt in rw.get("rates") or []:
        # nur USDC-denominierte Rewards zählen (andere Assets ignorieren)
        if rt.get("asset_address", USDC).lower() == USDC.lower():
            try:
                total += float(rt.get("rewards_daily_rate", 0) or 0)
            except (TypeError, ValueError):
                continue
    return total


def parse_reward_market(m: dict) -> RewardMarket | None:
    """Ein sampling-market-Objekt in RewardMarket überführen (None wenn untauglich)."""
    if m.get("closed") or not m.get("enable_order_book", True):
        return None
    toks = m.get("tokens") or []
    if len(toks) != 2:
        return None
    rw = m.get("rewards") or {}
    dr = daily_rate(m)
    if dr <= 0:
        return None
    prices = (0.0, 0.0)
    try:
        prices = (float(toks[0].get("price", 0)), float(toks[1].get("price", 0)))
    except (TypeError, ValueError):
        pass
    return RewardMarket(
        condition_id=m.get("condition_id", ""),
        slug=m.get("market_slug", ""),
        question=m.get("question", ""),
        up_token=str(toks[0].get("token_id", "")),
        down_token=str(toks[1].get("token_id", "")),
        daily_rate=dr,
        min_size=float(rw.get("min_size", 0) or 0),
        max_spread=float(rw.get("max_spread", 0) or 0),
        min_tick=float(m.get("minimum_tick_size", 0.01) or 0.01),
        end_ts=_to_ts(m.get("end_date_iso")),
        seconds_delay=float(m.get("seconds_delay", 0) or 0),
        game_start_ts=_to_ts(m.get("game_start_time")),
        accepting=bool(m.get("accepting_orders", False)),
        prices=prices,
    )


def fetch_reward_markets(session: requests.Session | None = None,
                         max_pages: int = 20) -> list[RewardMarket]:
    """Alle reward-tragenden Märkte über /sampling-markets paginieren."""
    http = session or requests.Session()
    out: list[RewardMarket] = []
    cursor = None
    for _ in range(max_pages):
        params = {"next_cursor": cursor} if cursor else {}
        try:
            r = http.get(f"{CLOB_HOST}{SAMPLING_PATH}", params=params, timeout=20)
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("sampling-markets Abruf fehlgeschlagen: %s", e)
            break
        for m in j.get("data", []):
            rm = parse_reward_market(m)
            if rm:
                out.append(rm)
        cursor = j.get("next_cursor")
        if not cursor or cursor == "LTE=":
            break
    return out


def qualifying_depth(book: OrderBook | None, mid: float,
                     max_spread_cents: float) -> float:
    """Notional (USDC) der Gebote/Asks INNERHALB des Reward-Bands (beidseitig).

    Das ist die Konkurrenz-Liquidität, mit der du dir die Tagesrate teilst.
    max_spread ist in Cents vom Mid (Polymarket-Konvention).
    """
    if book is None or mid <= 0:
        return 0.0
    band = max_spread_cents / 100.0
    lo, hi = mid - band, mid + band
    notion = 0.0
    for lv in book.bids:
        if lv.price >= lo:
            notion += lv.size * lv.price
    for lv in book.asks:
        if lv.price <= hi:
            notion += lv.size * lv.price
    return notion


@dataclass
class RankedMarket:
    rm: RewardMarket
    depth_notional: float      # Konkurrenz-Liquidität im Band (USDC)
    yield_pct_day: float       # geschätzter Tages-Yield bei kleinem Einsatz (%)
    fields: dict = field(default_factory=dict)


def rank_markets(markets: list[RewardMarket], books: dict[str, OrderBook],
                 min_capital: float = 100.0) -> list[RankedMarket]:
    """Nach geschätztem Tages-Yield pro Kapital ranken.

    Yield-Modell (kleiner Einsatz): wer `s` USDC ins Band legt, bekommt
    ~ daily_rate * s/(depth+s). Bei s << depth ≈ daily_rate/depth pro USDC.
    Als %/Tag: 100 * daily_rate / max(depth, min_capital). Der min_capital-
    Boden verhindert, dass ein LEERES Band (~0 Tiefe) einen absurden Yield
    vortäuscht — genau das «2463 USDC/Tag»-Trugbild der Flotte.
    """
    ranked = []
    for rm in markets:
        up_mid = _book_mid(books.get(rm.up_token))
        mid = up_mid if up_mid is not None else (rm.prices[0] or 0.5)
        depth = (qualifying_depth(books.get(rm.up_token), mid, rm.max_spread)
                 + qualifying_depth(books.get(rm.down_token), 1 - mid, rm.max_spread))
        denom = max(depth, min_capital)
        y = 100.0 * rm.daily_rate / denom if denom > 0 else 0.0
        ranked.append(RankedMarket(rm=rm, depth_notional=depth, yield_pct_day=y,
                                   fields={"mid": round(mid, 4)}))
    ranked.sort(key=lambda x: x.yield_pct_day, reverse=True)
    return ranked


def _book_mid(book: OrderBook | None) -> float | None:
    return book.midpoint if book is not None else None


def scan(session: requests.Session | None = None,
         books_client: BookClient | None = None,
         now: float | None = None,
         min_time_to_end_s: float = 3600.0,
         top_book: int = 40,
         min_capital: float = 100.0) -> list[RankedMarket]:
    """Kompletter Scan: fetch -> handelbar filtern -> Top nach Rate mit Buch
    anreichern -> nach Yield ranken. Gibt die gerankte Liste zurück."""
    now = time.time() if now is None else now
    http = session or requests.Session()
    books_client = books_client or BookClient(http)
    markets = fetch_reward_markets(http)
    tradeable = [m for m in markets if m.tradeable(now, min_time_to_end_s)]
    tradeable.sort(key=lambda m: m.daily_rate, reverse=True)
    # Nur für die Top-Rate-Kandidaten die (teuren) Bücher holen.
    head = tradeable[:top_book]
    tokens = [t for m in head for t in (m.up_token, m.down_token)]
    books = books_client.get_books(tokens) if tokens else {}
    return rank_markets(head, books, min_capital=min_capital)
