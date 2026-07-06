"""Up/Down-Latenz-Recorder — RISIKOFREIE Messung des Spät-Fenster-Edges.

Motivation (Forensik 06.07.2026, Wallet followsmartwallet, +44'977 USD in 4
Tagen): Das Konto kauft systematisch 7-30s VOR Schluss der 5-Minuten-
Krypto-Fenster (BTC/ETH/SOL "Up or Down"). Diese Märkte lösen NICHT nach
Binance/Coinbase-Spot auf, sondern nach dem **Chainlink BTC/USD-Data-Stream**
(steht wörtlich in der Marktbeschreibung). Der Edge ist reine Latenz: Wer die
auflösungsrelevante Preisrichtung Sekunden früher kennt als das Polymarket-
Orderbuch sie einpreist, kauft die fast sichere Seite noch unter 1 USDC.

Dieser Recorder HANDELT NICHT. Er beobachtet und protokolliert, damit wir den
Edge ehrlich vermessen, BEVOR echtes Kapital riskiert wird:

  - Referenzsignal:  Coinbase-Spot (ws-feed, Millisekunden-Zeitstempel) — das,
                     worauf WIR live reagieren könnten. Coinbase ist ein enger
                     Proxy für den Chainlink-Stream, aber NICHT identisch; die
                     Abweichung Proxy-vs-Wahrheit ist unser Risiko und wird im
                     Report explizit ausgewiesen (Prinzip Nr. 2: Ehrlichkeit).
  - Orderbuch:       Polymarket-CLOB (best bid/ask der Up-/Down-Token) im
                     Sub-Sekunden-Poll — zeigt, WANN das Buch die Richtung
                     einpreist (und wie lange die günstige Seite offen bleibt).
  - Wahrheit:        Nach Fensterschluss liefert Gamma outcomePrices 1/0 —
                     das echte, Chainlink-basierte Ergebnis, gratis und ohne
                     eigenen Oracle-Zugang.

Der Report (`python -m polybot.main updown-report`) verbindet Snapshots mit
dem echten Ergebnis und beantwortet die EINE Frage: Ist es +EV, X Sekunden vor
Schluss die vom Referenzsignal vorhergesagte Seite zum Buch-Ask zu kaufen?
"""

from __future__ import annotations

import json
import logging
import threading
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from polybot.data.gamma import GammaClient
from polybot.data.orderbook import BookClient

log = logging.getLogger(__name__)

# Assets mit 5-Min-Up/Down-Fenstern. Chainlink löst diese Märkte auf; unser
# handelbares Live-Signal ist der MEDIAN mehrerer Börsen — Chainlink selbst
# ist ein Median vieler Quellen, also nähert ein Börsen-Median den Oracle
# besser an als eine einzelne Börse. Pro Börse das jeweilige Handelspaar:
ASSETS = ["btc", "eth", "sol"]
EXCHANGE_SYMBOLS = {
    "coinbase":  {"btc": "BTC-USD",  "eth": "ETH-USD",  "sol": "SOL-USD"},
    "kraken":    {"btc": "XBT/USD",  "eth": "ETH/USD",  "sol": "SOL/USD"},
    "binanceus": {"btc": "BTCUSDT",  "eth": "ETHUSDT",  "sol": "SOLUSDT"},
}
# Rückwärts-Kompatibilität: einige Aufrufer/Tests referenzieren noch das
# Coinbase-Produkt-Mapping.
ASSET_PRODUCTS = EXCHANGE_SYMBOLS["coinbase"]

WINDOW_S = 300           # 5-Minuten-Fenster
DATA_PATH = Path("data") / "updown.jsonl"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
KRAKEN_WS = "wss://ws.kraken.com"
BINANCEUS_REST = "https://api.binance.us/api/v3/ticker/price"

# Chainlink-Preis-Aggregatoren (klassische AggregatorV3) auf Polygon. Das ist
# ECHTES Chainlink — aber der on-chain-Aggregator (Heartbeat/Deviation, ~15-30s)
# ist NICHT der Low-Latency-Data-Stream, der die Märkte final auflöst. Er dient
# hier als zweite Referenz, um die Divergenz Börsen-Median↔Chainlink DIREKT zu
# messen statt sie nur aus Fehltreffern abzuleiten.
CHAINLINK_RPC = "https://polygon-bor-rpc.publicnode.com"
CHAINLINK_FEEDS = {
    "btc": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "eth": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "sol": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dc",
}
_CHAINLINK_ABI = [
    {"inputs": [], "name": "latestRoundData",
     "outputs": [{"name": "roundId", "type": "uint80"},
                 {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"},
                 {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]

# Ein Referenz-Tick älter als das gilt als veraltet (Feed-Hänger) und wird im
# Snapshot als stale markiert — der Report filtert solche Zeilen aus.
REF_STALE_S = 3.0


# ---------------------------------------------------------------------------
# Reine Logik (testbar, ohne I/O)
# ---------------------------------------------------------------------------

def window_start_from_slug(slug: str) -> int | None:
    """`btc-updown-5m-1783370400` -> 1783370400 (Fensterstart, Unix-UTC)."""
    parts = slug.rsplit("-", 1)
    if len(parts) != 2 or "updown-5m" not in slug:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def active_window_starts(now: float) -> list[int]:
    """Fensterstarts, deren Fenster gerade läuft oder als Nächstes beginnt.

    Das laufende Fenster (endet in (now, now+300]) trägt den Spät-Edge; das
    nächste nehmen wir mit, damit der Referenz-Startpreis sauber am echten
    Fensterstart erfasst wird statt erst mitten drin.
    """
    cur = int(now // WINDOW_S) * WINDOW_S
    return [cur, cur + WINDOW_S]


def slugs_for(assets: list[str], starts: list[int]) -> list[str]:
    return [f"{a}-updown-5m-{s}" for s in starts for a in assets]


def predict_direction(ref_now: float | None, ref_start: float | None) -> str:
    """Richtungsvorhersage aus dem Referenzsignal: 'up' | 'down' | 'flat'.

    'flat' (exakt gleich) ist selten, aber ehrlich: dann hat der Proxy keine
    Meinung und die Zeile wird bei der Trefferquote nicht mitgezählt.
    """
    if ref_now is None or ref_start is None:
        return "flat"
    if ref_now > ref_start:
        return "up"
    if ref_now < ref_start:
        return "down"
    return "flat"


def outcome_from_prices(outcome_prices: list | None) -> str | None:
    """Echtes Ergebnis aus Gamma outcomePrices ([Up, Down]) -> 'up'|'down'|None.

    None = noch nicht final aufgelöst (Zwischenstände sind Handelspreise).
    """
    if not outcome_prices or len(outcome_prices) != 2:
        return None
    try:
        up, down = float(outcome_prices[0]), float(outcome_prices[1])
    except (TypeError, ValueError):
        return None
    if up >= 0.999 and down <= 0.001:
        return "up"
    if down >= 0.999 and up <= 0.001:
        return "down"
    return None


@dataclass
class WindowRef:
    """Referenz-Startpreise eines Fensters (einmal am Fensterstart erfasst)."""
    asset: str
    window_start: int
    ref_start: float | None = None      # Börsen-Median am/kurz nach Fensterstart
    ref_start_ts: float | None = None
    chain_start: float | None = None    # Chainlink on-chain am Fensterstart


def build_snapshot(*, ts: float, asset: str, slug: str, window_start: int,
                   up_token: str, down_token: str,
                   up_bid: float | None, up_ask: float | None,
                   down_bid: float | None, down_ask: float | None,
                   ref_now: float | None, ref_ts: float | None,
                   ref_start: float | None,
                   up_ask_size: float | None = None,
                   down_ask_size: float | None = None,
                   ref_sources: int = 0,
                   px_sources: dict | None = None,
                   chain_price: float | None = None,
                   chain_age_s: float | None = None,
                   chain_start: float | None = None) -> dict:
    """Eine Beobachtungszeile bauen (reine Funktion, alle Werte schon gelesen).

    Das Live-Signal `ref_now` ist der MEDIAN der frischen Börsenpreise
    (`ref_sources` = Zahl der eingegangenen Börsen). Die Einzelpreise
    (`px_sources`) und der on-chain Chainlink-Preis (`chain_price`) werden roh
    mitgeführt: `pred` (Median-Richtung) vs `chain_pred` (Chainlink-Richtung)
    misst die Divergenz Börsen↔Chainlink DIREKT statt nur aus Fehltreffern.
    Die Ask-Größen gehören mit ins Protokoll, sonst täuscht ein Edge vor, der
    nur für eine Handvoll Shares an der Spitze existiert.
    """
    window_end = window_start + WINDOW_S
    up_mid = _mid(up_bid, up_ask)
    down_mid = _mid(down_bid, down_ask)
    ref_age = (ts - ref_ts) if ref_ts is not None else None
    return {
        "ts": round(ts, 3),
        "kind": "snapshot",
        "asset": asset,
        "slug": slug,
        "window_start": window_start,
        "window_end": window_end,
        "seconds_to_close": round(window_end - ts, 3),
        "up_token": up_token,
        "down_token": down_token,
        "up_bid": up_bid, "up_ask": up_ask, "up_mid": up_mid,
        "up_ask_size": up_ask_size,
        "down_bid": down_bid, "down_ask": down_ask, "down_mid": down_mid,
        "down_ask_size": down_ask_size,
        "ref_price": ref_now,
        "ref_sources": ref_sources,
        "px": px_sources or {},
        "ref_ts": round(ref_ts, 3) if ref_ts is not None else None,
        "ref_age_s": round(ref_age, 3) if ref_age is not None else None,
        "ref_stale": (ref_age is None or ref_age > REF_STALE_S),
        "ref_start": ref_start,
        "pred": predict_direction(ref_now, ref_start),
        "chain_price": chain_price,
        "chain_age_s": round(chain_age_s, 1) if chain_age_s is not None else None,
        "chain_start": chain_start,
        "chain_pred": predict_direction(chain_price, chain_start),
    }


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round((bid + ask) / 2, 6)


# ---------------------------------------------------------------------------
# Börsen-Median-Referenz (mehrere Feeds, thread-sicher, wirft nie nach außen)
# ---------------------------------------------------------------------------

def median_price(sources: dict, now: float,
                 max_age: float = REF_STALE_S) -> tuple[float | None, float | None, int]:
    """Median der FRISCHEN Börsenpreise -> (median, neuester_ts, n_frisch).

    sources: {börse: (preis, ts)}. Nur Preise jünger als max_age zählen — ein
    hängender Feed darf den Median nicht vergiften. Reine Funktion (testbar).
    """
    fresh = [(p, ts) for (p, ts) in sources.values()
             if p is not None and ts is not None and now - ts <= max_age]
    if not fresh:
        return None, None, 0
    return (statistics.median([p for p, _ in fresh]),
            max(ts for _, ts in fresh), len(fresh))


class MultiExchangeTicker:
    """Live-Median über mehrere Börsen (Coinbase-WS, Kraken-WS, Binance.us-REST).

    Chainlink ist selbst ein Median vieler Quellen — ein Börsen-Median nähert
    den Oracle darum besser an als eine einzelne Börse und ist robuster gegen
    einen einzelnen ausreißenden/hängenden Feed. Jede Börse läuft in einem
    eigenen Daemon-Thread mit Reconnect; keine Methode wirft nach außen.
    """

    EXCHANGES = ("coinbase", "kraken", "binanceus")

    def __init__(self, assets: list[str] | None = None, connect=None,
                 http: requests.Session | None = None):
        self.assets = assets or list(ASSETS)
        self._connect = connect or _default_ws_connect
        self._http = http or requests.Session()
        self._lock = threading.Lock()
        self._prices: dict[tuple[str, str], tuple[float, float]] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        self._threads = [
            threading.Thread(target=self._run_coinbase, daemon=True),
            threading.Thread(target=self._run_kraken, daemon=True),
            threading.Thread(target=self._run_binanceus, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _put(self, exch: str, asset: str, price: float) -> None:
        with self._lock:
            self._prices[(exch, asset)] = (price, time.time())

    def get_sources(self, asset: str) -> dict[str, tuple[float, float]]:
        with self._lock:
            return {e: self._prices[(e, asset)] for e in self.EXCHANGES
                    if (e, asset) in self._prices}

    def snapshot(self, asset: str, now: float | None = None):
        """(median, neuester_ts, n_frisch, {börse: preis}) für einen Asset."""
        now = time.time() if now is None else now
        src = self.get_sources(asset)
        med, ts, n = median_price(src, now)
        px = {e: round(p, 4) for e, (p, _) in src.items()}
        return med, ts, n, px

    # -- Coinbase (WS ticker) --------------------------------------------
    def _run_coinbase(self) -> None:  # pragma: no cover — Netz/Feed
        sym = EXCHANGE_SYMBOLS["coinbase"]
        rev = {v: k for k, v in sym.items()}
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = self._connect(COINBASE_WS)
                ws.send(json.dumps({"type": "subscribe",
                                    "product_ids": [sym[a] for a in self.assets],
                                    "channels": ["ticker"]}))
                backoff = 1.0
                while not self._stop.is_set():
                    m = json.loads(ws.recv())
                    if m.get("type") != "ticker":
                        continue
                    a = rev.get(m.get("product_id"))
                    try:
                        if a:
                            self._put("coinbase", a, float(m["price"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                self._safe_close(ws)
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("Coinbase-Feed getrennt (%s) — Reconnect %.0fs", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    # -- Kraken (WS ticker) ----------------------------------------------
    def _run_kraken(self) -> None:  # pragma: no cover — Netz/Feed
        sym = EXCHANGE_SYMBOLS["kraken"]
        rev = {v: k for k, v in sym.items()}
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = self._connect(KRAKEN_WS)
                ws.send(json.dumps({"event": "subscribe",
                                    "pair": [sym[a] for a in self.assets],
                                    "subscription": {"name": "ticker"}}))
                backoff = 1.0
                while not self._stop.is_set():
                    m = json.loads(ws.recv())
                    # Ticker-Payload ist eine Liste [chanId, {...}, "ticker", pair]
                    if not isinstance(m, list) or len(m) < 4:
                        continue
                    a = rev.get(m[3])
                    try:
                        last = float(m[1]["c"][0])
                        if a:
                            self._put("kraken", a, last)
                    except (KeyError, TypeError, ValueError, IndexError):
                        continue
                self._safe_close(ws)
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("Kraken-Feed getrennt (%s) — Reconnect %.0fs", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    # -- Binance.us (REST-Poll; WS ist von hier geoblockt) ---------------
    def _run_binanceus(self) -> None:  # pragma: no cover — Netz/Feed
        sym = EXCHANGE_SYMBOLS["binanceus"]
        rev = {v: k for k, v in sym.items()}
        # Kompakt ohne Leerzeichen: binance.us' symbols-Regex verbietet Spaces
        # (json.dumps setzt sonst ", " und der Server antwortet 400).
        symbols = json.dumps([sym[a] for a in self.assets], separators=(",", ":"))
        while not self._stop.is_set():
            try:
                r = self._http.get(BINANCEUS_REST, params={"symbols": symbols},
                                   timeout=8)
                r.raise_for_status()
                for row in r.json():
                    a = rev.get(row.get("symbol"))
                    try:
                        if a:
                            self._put("binanceus", a, float(row["price"]))
                    except (KeyError, TypeError, ValueError):
                        continue
            except Exception as e:
                log.debug("Binance.us-Poll fehlgeschlagen: %s", e)
            self._stop.wait(1.0)

    @staticmethod
    def _safe_close(ws) -> None:
        try:
            ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Chainlink-on-chain-Referenz (Polygon, gratis über web3) — zweite Referenz
# ---------------------------------------------------------------------------

class ChainlinkPolygonRef:
    """Pollt die Chainlink-Aggregatoren auf Polygon (BTC/ETH/SOL-USD).

    ECHTES Chainlink, aber der on-chain-Aggregator (Heartbeat/Deviation) läuft
    dem Low-Latency-Data-Stream hinterher, der die Märkte final auflöst — er
    misst die Divergenz Börsen↔Chainlink, ist aber selbst kein Sekundensignal.
    Wirft nie nach außen: fehlt web3/RPC, liefert get_price (None, None).
    """

    def __init__(self, assets: list[str] | None = None,
                 rpc_url: str = CHAINLINK_RPC, poll_s: float = 6.0, w3=None):
        self.assets = assets or list(ASSETS)
        self.rpc_url = rpc_url
        self.poll_s = poll_s
        self._w3 = w3
        self._lock = threading.Lock()
        # asset -> (price, onchain_updated_ts)
        self._prices: dict[str, tuple[float, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._contracts: dict = {}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_price(self, asset: str) -> tuple[float | None, float | None]:
        """(preis, on-chain-updatedAt) oder (None, None)."""
        with self._lock:
            p = self._prices.get(asset)
        return (p[0], p[1]) if p else (None, None)

    def _ensure_contracts(self) -> bool:
        if self._contracts:
            return True
        try:
            from web3 import HTTPProvider, Web3
            if self._w3 is None:
                self._w3 = Web3(HTTPProvider(self.rpc_url))
            for a in self.assets:
                addr = CHAINLINK_FEEDS.get(a)
                if not addr:
                    continue
                c = self._w3.eth.contract(
                    address=self._w3.to_checksum_address(addr), abi=_CHAINLINK_ABI)
                dec = c.functions.decimals().call()
                self._contracts[a] = (c, dec)
            return bool(self._contracts)
        except Exception as e:  # pragma: no cover — web3 fehlt / RPC down
            log.warning("Chainlink-Referenz nicht initialisierbar (%s) — "
                        "on-chain-Spalte bleibt leer", e)
            return False

    def _run(self) -> None:  # pragma: no cover — Netz/RPC
        while not self._stop.is_set():
            if self._ensure_contracts():
                for a, (c, dec) in self._contracts.items():
                    try:
                        rd = c.functions.latestRoundData().call()
                        with self._lock:
                            self._prices[a] = (rd[1] / 10 ** dec, float(rd[3]))
                    except Exception as e:
                        log.debug("Chainlink %s Abruf fehlgeschlagen: %s", a, e)
            self._stop.wait(self.poll_s if self._contracts else 30.0)


def _default_ws_connect(url: str):  # pragma: no cover — echte Netzverbindung
    import os
    from websocket import create_connection
    sslopt = {}
    ca = (os.environ.get("WEBSOCKET_CLIENT_CA_BUNDLE")
          or os.environ.get("SSL_CERT_FILE")
          or os.environ.get("REQUESTS_CA_BUNDLE")
          or os.environ.get("CURL_CA_BUNDLE"))
    if ca and os.path.exists(ca):
        sslopt["ca_certs"] = ca
    return create_connection(url, timeout=15, sslopt=sslopt)


# ---------------------------------------------------------------------------
# Recorder-Loop
# ---------------------------------------------------------------------------

class UpDownRecorder:
    """Beobachtet die aktiven Up/Down-Fenster und schreibt Snapshots als JSONL."""

    def __init__(self, assets: list[str] | None = None,
                 path: Path = DATA_PATH, poll_s: float = 0.5,
                 gamma: GammaClient | None = None,
                 books: BookClient | None = None,
                 ticker: MultiExchangeTicker | None = None,
                 chainlink: ChainlinkPolygonRef | None = None):
        self.assets = assets or list(ASSETS)
        self.path = path
        self.poll_s = poll_s
        self.gamma = gamma or GammaClient()
        self.books = books or BookClient()
        self.ticker = ticker or MultiExchangeTicker(self.assets)
        # Chainlink-Referenz optional: None schaltet die on-chain-Spalte ab.
        self.chainlink = (chainlink if chainlink is not None
                          else ChainlinkPolygonRef(self.assets))
        # slug -> (asset, up_token, down_token, window_start); Marktmetadaten,
        # gecacht bis das Fenster endet (spart Gamma-Abrufe pro Tick).
        self._markets: dict[str, tuple] = {}
        self._refs: dict[str, WindowRef] = {}     # slug -> WindowRef
        self._resolved: set[str] = set()          # Slugs, deren Ergebnis notiert ist
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- Marktmetadaten (Token-IDs) je Fenster über Gamma auflösen --------
    def _ensure_markets(self, now: float) -> None:
        want = set(slugs_for(self.assets, active_window_starts(now)))
        missing = [s for s in want if s not in self._markets]
        for slug in missing:
            try:
                rows = self.gamma._get("/markets", slug=slug)
            except Exception as e:  # pragma: no cover — Netzfehler
                log.warning("Gamma-Slug-Abruf %s fehlgeschlagen: %s", slug, e)
                continue
            if not rows:
                continue
            m = rows[0]
            try:
                tokens = json.loads(m.get("clobTokenIds") or "[]")
            except (TypeError, ValueError):
                tokens = []
            ws = window_start_from_slug(slug)
            if len(tokens) == 2 and ws is not None:
                asset = slug.split("-", 1)[0]
                self._markets[slug] = (asset, tokens[0], tokens[1], ws)
                self._refs.setdefault(slug, WindowRef(asset, ws))
        # Abgelaufene Fenster (Ende < now-60) aus dem Cache räumen.
        for slug in list(self._markets):
            _, _, _, ws = self._markets[slug]
            if ws + WINDOW_S < now - 60:
                self._markets.pop(slug, None)

    def _write(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def tick(self, now: float | None = None) -> int:
        """Ein Beobachtungszyklus. Gibt die Zahl geschriebener Zeilen zurück."""
        now = time.time() if now is None else now
        self._ensure_markets(now)
        if not self._markets:
            return 0

        # Referenz-Startpreise je Fenster einmalig am Start erfassen
        # (Börsen-Median UND Chainlink on-chain).
        for slug, (asset, _, _, ws) in self._markets.items():
            ref = self._refs.get(slug)
            if not ref or now < ws:
                continue
            if ref.ref_start is None:
                med, ts, _n, _px = self.ticker.snapshot(asset, now)
                if med is not None:
                    ref.ref_start, ref.ref_start_ts = med, ts
            if ref.chain_start is None:
                cp, _cts = self.chainlink.get_price(asset)
                if cp is not None:
                    ref.chain_start = cp

        # Alle aktiven Token-Bücher in EINEM Batch holen.
        tokens: list[str] = []
        for _, up, down, _ in self._markets.values():
            tokens += [up, down]
        obooks = self.books.get_books(tokens)

        written = 0
        for slug, (asset, up_t, down_t, ws) in self._markets.items():
            # Nur das laufende Fenster protokollieren (Snapshots vor Fenster-
            # start tragen keinen Edge; das Vorab-Fenster dient nur der
            # Startpreis-Erfassung).
            if now < ws or now >= ws + WINDOW_S:
                continue
            ref = self._refs.get(slug)
            med, ref_ts, n_src, px = self.ticker.snapshot(asset, now)
            cp, c_upd = self.chainlink.get_price(asset)
            c_age = (now - c_upd) if c_upd is not None else None
            ub, ua, uas = _top(obooks.get(up_t))
            db, da, das = _top(obooks.get(down_t))
            row = build_snapshot(
                ts=now, asset=asset, slug=slug, window_start=ws,
                up_token=up_t, down_token=down_t,
                up_bid=ub, up_ask=ua, down_bid=db, down_ask=da,
                up_ask_size=uas, down_ask_size=das,
                ref_now=med, ref_ts=ref_ts, ref_sources=n_src, px_sources=px,
                ref_start=ref.ref_start if ref else None,
                chain_price=cp, chain_age_s=c_age,
                chain_start=ref.chain_start if ref else None)
            self._write(row)
            written += 1

        # Ergebnis frisch abgelaufener Fenster nachtragen (einmal je Slug).
        self._sweep_resolutions(now)
        return written

    def _sweep_resolutions(self, now: float) -> None:
        for slug in list(self._refs):
            if slug in self._resolved:
                continue
            ref = self._refs[slug]
            end = ref.window_start + WINDOW_S
            if now < end + 5:        # kurze Schonfrist nach Fensterschluss
                continue
            try:
                # WICHTIG: closed=true. Gamma wirft geschlossene Märkte aus
                # der Default-Abfrage (open-only) — ohne diesen Param liefert
                # der Slug-Lookup nach Fensterschluss 0 Zeilen, und das
                # aufgelöste Ergebnis (outcomePrices 1/0, uma=resolved) bliebe
                # unsichtbar (Befund 06.07.2026: nur 1 von 15 Fenstern erfasst).
                rows = self.gamma._get("/markets", slug=slug, closed="true")
            except Exception:  # pragma: no cover
                continue
            if not rows:
                continue
            try:
                prices = json.loads(rows[0].get("outcomePrices") or "[]")
            except (TypeError, ValueError):
                prices = None
            outcome = outcome_from_prices(prices)
            if outcome is None:
                continue            # noch nicht final — nächster Tick erneut
            self._write({
                "ts": round(now, 3), "kind": "resolution", "asset": ref.asset,
                "slug": slug, "window_start": ref.window_start,
                "window_end": end, "outcome": outcome,
                "ref_start": ref.ref_start, "ref_start_ts": ref.ref_start_ts,
            })
            self._resolved.add(slug)

    def backfill_refs_from_disk(self) -> int:
        """Nach Neustart offene Fenster aus der Datei nachladen.

        Der Recorder trackt sonst nur aktuelle+nächste Fenster; nach einem
        Neustart (Container flüchtig!) blieben schon beendete, aber noch nicht
        aufgelöste Fenster für immer ohne Ergebnis. Hier werden alle Snapshot-
        Slugs ohne Resolution-Zeile als Ref reaktiviert, damit der normale
        Sweep sie mit closed=true nachträgt. Gibt die Zahl reaktivierter
        Fenster zurück.
        """
        rows = load_rows(self.path)
        resolved = {r["slug"] for r in rows if r.get("kind") == "resolution"}
        self._resolved |= resolved
        seen = 0
        for r in rows:
            if r.get("kind") != "snapshot":
                continue
            slug = r.get("slug")
            if not slug or slug in resolved or slug in self._refs:
                continue
            ws = window_start_from_slug(slug)
            if ws is None:
                continue
            asset = slug.split("-", 1)[0]
            # ref_start aus dem Snapshot übernehmen (für den Report irrelevant,
            # der ihn aus den Snapshot-Zeilen liest — aber ehrlich mitführen).
            ref = WindowRef(asset, ws)
            ref.ref_start = r.get("ref_start")
            ref.chain_start = r.get("chain_start")
            self._refs[slug] = ref
            seen += 1
        return seen

    def run(self) -> None:  # pragma: no cover — Langläufer-Schleife
        self.ticker.start()
        self.chainlink.start()
        n = self.backfill_refs_from_disk()
        log.info("Up/Down-Recorder gestartet (Assets: %s) -> %s "
                 "(%d unaufgelöste Fenster aus Datei reaktiviert)",
                 ", ".join(self.assets), self.path, n)
        try:
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    n = self.tick()
                    if n:
                        log.debug("%d Snapshot-Zeilen", n)
                except Exception as e:
                    log.warning("Recorder-Tick-Fehler: %s", e)
                self._stop.wait(max(0.0, self.poll_s - (time.time() - t0)))
        finally:
            self.ticker.stop()
            self.chainlink.stop()


def _top(book) -> tuple[float | None, float | None, float | None]:
    """(best_bid_price, best_ask_price, best_ask_size) — None-sicher."""
    if book is None:
        return None, None, None
    bb = book.best_bid.price if book.best_bid else None
    ba = book.best_ask.price if book.best_ask else None
    bas = book.best_ask.size if book.best_ask else None
    return bb, ba, bas


# ---------------------------------------------------------------------------
# Report: Snapshots + echtes Ergebnis -> Edge pro Sekunden-vor-Schluss-Bucket
# ---------------------------------------------------------------------------

# Sekunden-vor-Schluss-Buckets (obere Grenze je Bucket).
OFFSET_BUCKETS = [1, 3, 5, 7, 10, 15, 20, 30, 45, 60, 120, 300]


def _bucket(sec_to_close: float) -> int | None:
    if sec_to_close < 0:
        return None
    for b in OFFSET_BUCKETS:
        if sec_to_close <= b:
            return b
    return None


def aggregate_updown(rows: list[dict], fee_rate: float = 0.0) -> dict:
    """Snapshots mit dem echten Ergebnis verbinden und den Edge auswerten.

    Für jeden Snapshot (nur mit frischem Referenz-Tick und beidseitigem Ask):
    Kauf der vom Proxy vorhergesagten Seite zum Buch-Ask. Auszahlung 1, wenn
    die Vorhersage mit dem echten Ergebnis übereinstimmt, sonst 0. Der
    realisierte PnL pro 1-Share-Wette, gemittelt je Sekunden-vor-Schluss-
    Bucket, IST die Antwort: Ab welcher Sekunde vor Schluss ist es +EV?

    Ausgewiesen je Bucket: n, Proxy-Trefferquote (proxy_acc), Anteil, in dem
    das BUCH die Gewinnerseite schon führt (book_leads_winner), mittlerer
    Ask der bespielten Seite und die mittlere realisierte Rendite (ev). Die
    Trefferquote < 100% ist die Proxy-vs-Chainlink-Divergenz — unser Risiko.
    """
    outcomes: dict[str, str] = {}
    for r in rows:
        if r.get("kind") == "resolution" and r.get("outcome"):
            outcomes[r["slug"]] = r["outcome"]

    buckets: dict[int, dict] = {}
    for r in rows:
        if r.get("kind") != "snapshot":
            continue
        slug = r.get("slug")
        truth = outcomes.get(slug)
        if truth is None:
            continue                      # Fenster (noch) ohne echtes Ergebnis
        if r.get("ref_stale"):
            continue                      # veralteter Referenz-Tick -> nicht handelbar
        pred = r.get("pred")
        if pred not in ("up", "down"):
            continue                      # Proxy ohne Meinung (flat/kein Startpreis)
        b = _bucket(r.get("seconds_to_close", -1))
        if b is None:
            continue
        ask = r.get("up_ask") if pred == "up" else r.get("down_ask")
        if ask is None:
            continue                      # bespielte Seite nicht kaufbar
        ask_size = (r.get("up_ask_size") if pred == "up"
                    else r.get("down_ask_size"))
        correct = (pred == truth)
        payout = 1.0 if correct else 0.0
        fee = fee_rate * ask * (1.0 - ask)
        pnl = payout - ask - fee
        # Führt das Buch die Gewinnerseite? (mid der Wahrheits-Seite > 0.5)
        win_mid = r.get("up_mid") if truth == "up" else r.get("down_mid")
        # DIREKTE Divergenz Börsen-Median vs Chainlink-on-chain: stimmt die
        # Median-Richtung mit der Chainlink-Richtung überein? (nur wenn beide
        # eine Meinung haben). So messen wir den Proxy-Fehler direkt, statt ihn
        # nur aus Fehltreffern gegen das Endergebnis abzuleiten.
        chain_pred = r.get("chain_pred")
        chain_has = chain_pred in ("up", "down")

        d = buckets.setdefault(b, {"n": 0, "correct": 0, "ask_sum": 0.0,
                                   "pnl_sum": 0.0, "book_leads": 0,
                                   "book_n": 0, "size_sum": 0.0, "size_n": 0,
                                   "windows": set(), "chain_n": 0,
                                   "chain_agree": 0})
        d["n"] += 1
        d["windows"].add(slug)
        d["correct"] += int(correct)
        d["ask_sum"] += ask
        d["pnl_sum"] += pnl
        if ask_size is not None:
            d["size_sum"] += ask_size
            d["size_n"] += 1
        if win_mid is not None:
            d["book_n"] += 1
            d["book_leads"] += int(win_mid > 0.5)
        if chain_has:
            d["chain_n"] += 1
            d["chain_agree"] += int(chain_pred == pred)

    out_buckets = {}
    for b in sorted(buckets):
        d = buckets[b]
        n = d["n"]
        out_buckets[b] = {
            "n": n,
            "windows": len(d["windows"]),   # ECHTE Stichprobe (Snapshots je
                                            # Fenster sind hochkorreliert — n lügt)
            "proxy_acc": d["correct"] / n if n else None,
            "avg_ask": d["ask_sum"] / n if n else None,
            "avg_depth": d["size_sum"] / d["size_n"] if d["size_n"] else None,
            "ev": d["pnl_sum"] / n if n else None,
            "book_leads_winner": (d["book_leads"] / d["book_n"]
                                  if d["book_n"] else None),
            "chain_agree": (d["chain_agree"] / d["chain_n"]
                            if d["chain_n"] else None),
        }
    return {
        "windows_resolved": len(outcomes),
        "snapshots_used": sum(d["n"] for d in buckets.values()),
        "fee_rate": fee_rate,
        "by_offset": out_buckets,
    }


def load_rows(path: Path = DATA_PATH) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
