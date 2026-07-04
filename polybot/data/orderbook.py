"""Orderbuch-Daten vom öffentlichen CLOB-Endpunkt (kein API-Key nötig)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from polybot.config import CLOB_HOST

log = logging.getLogger(__name__)


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level] = field(default_factory=list)  # absteigend sortiert
    asks: list[Level] = field(default_factory=list)  # aufsteigend sortiert

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid.price + self.best_ask.price) / 2
        return None

    def buyable(self, max_price: float) -> float:
        """Wie viele Shares sind bis max_price kaufbar?"""
        return sum(lv.size for lv in self.asks if lv.price <= max_price)


def _parse_book(token_id: str, data: dict) -> OrderBook:
    """Rohdaten (bids/asks mit String-Preisen) in ein OrderBook überführen.

    Wirft bei fehlerhaften Rohdaten (fehlende Keys, nicht-numerische Werte)
    ValueError/KeyError/TypeError — der Aufrufer entscheidet über den Umgang.
    """
    bids = sorted(
        (Level(float(x["price"]), float(x["size"])) for x in data.get("bids", [])),
        key=lambda lv: -lv.price,
    )
    asks = sorted(
        (Level(float(x["price"]), float(x["size"])) for x in data.get("asks", [])),
        key=lambda lv: lv.price,
    )
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


class BookClient:
    def __init__(self, session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "polybot/0.1"
        # Taker-Fee-Raten sind kategorieabhängig (0.00-0.07) und ändern sich
        # praktisch nie -> pro Token einmal holen und cachen.
        self._fee_rate_cache: dict[str, float] = {}

    def get_fee_rate(self, token_id: str) -> float | None:
        """Taker-Fee-Rate eines Tokens (GET /fee-rate); None bei Fehler."""
        if token_id in self._fee_rate_cache:
            return self._fee_rate_cache[token_id]
        try:
            r = self.http.get(f"{CLOB_HOST}/fee-rate", params={"token_id": token_id}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Fee-Rate für %s nicht abrufbar: %s", token_id[:16], e)
            return None
        raw = data.get("taker_fee_rate", data.get("fee_rate")) if isinstance(data, dict) else data
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            log.warning("Fee-Rate für %s nicht interpretierbar: %r", token_id[:16], raw)
            return None
        if rate > 1.0:  # Endpunkt liefert Basispunkte statt Dezimalrate
            rate /= 10_000.0
        self._fee_rate_cache[token_id] = rate
        return rate

    def get_fee_rates(self, token_ids: list[str]) -> dict[str, float]:
        """Fee-Raten für mehrere Tokens; nicht abrufbare Tokens fehlen im Ergebnis."""
        out: dict[str, float] = {}
        for t in token_ids:
            rate = self.get_fee_rate(t)
            if rate is not None:
                out[t] = rate
        return out

    def get_book(self, token_id: str) -> OrderBook | None:
        try:
            r = self.http.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=15)
            r.raise_for_status()
            # Parsing gehört mit in den try-Block: der Vertrag der Funktion
            # ist "None + Warnung bei Problemen", auch bei kaputten Rohdaten.
            return _parse_book(token_id, r.json())
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("Orderbuch für %s nicht abrufbar/parsebar: %s", token_id[:16], e)
            return None

    def get_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Mehrere Orderbücher in einem Request (POST /books)."""
        out: dict[str, OrderBook] = {}
        for chunk_start in range(0, len(token_ids), 50):
            chunk = token_ids[chunk_start : chunk_start + 50]
            try:
                r = self.http.post(
                    f"{CLOB_HOST}/books",
                    json=[{"token_id": t} for t in chunk],
                    timeout=30,
                )
                r.raise_for_status()
                rows = r.json()
            except (requests.RequestException, ValueError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 429:
                    # Rate-Limit: Einzel-Fallback würde das Limit nur weiter
                    # verschärfen (bis zu ~5000 GETs pro Snapshot) -> restlichen
                    # Batch-Lauf abbrechen und das Teilergebnis zurückgeben.
                    log.warning(
                        "CLOB rate-limitiert (429) — Batch-Lauf abgebrochen, "
                        "%d Bücher als Teilergebnis", len(out),
                    )
                    break
                log.warning("Batch-Orderbücher fehlgeschlagen, fallback einzeln: %s", e)
                for t in chunk:
                    b = self.get_book(t)
                    if b:
                        out[t] = b
                continue
            for row in rows:
                # Eine fehlerhafte Row darf nicht den ganzen Batch verwerfen:
                # loggen und überspringen statt die Funktion abbrechen.
                try:
                    tid = row.get("asset_id") or ""
                    if not tid:
                        log.warning("Batch-Orderbuch-Row ohne asset_id übersprungen")
                        continue
                    out[tid] = _parse_book(tid, row)
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    log.warning("Batch-Orderbuch-Row nicht parsebar, übersprungen: %s", e)
        return out
