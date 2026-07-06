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
import time
from dataclasses import dataclass, field
from pathlib import Path

from polybot.data.gamma import GammaClient
from polybot.data.orderbook import BookClient

log = logging.getLogger(__name__)

# Assets mit 5-Min-Up/Down-Fenstern und ihrem Coinbase-Spot-Produkt.
# Chainlink löst diese Feeds auf; Coinbase-Spot ist unser handelbarer Proxy.
ASSET_PRODUCTS = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
}

WINDOW_S = 300           # 5-Minuten-Fenster
DATA_PATH = Path("data") / "updown.jsonl"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"

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
    """Referenz-Startpreis eines Fensters (einmal am Fensterstart erfasst)."""
    asset: str
    window_start: int
    ref_start: float | None = None      # Coinbase-Spot am/kurz nach Fensterstart
    ref_start_ts: float | None = None


def build_snapshot(*, ts: float, asset: str, slug: str, window_start: int,
                   up_token: str, down_token: str,
                   up_bid: float | None, up_ask: float | None,
                   down_bid: float | None, down_ask: float | None,
                   ref_now: float | None, ref_ts: float | None,
                   ref_start: float | None,
                   up_ask_size: float | None = None,
                   down_ask_size: float | None = None) -> dict:
    """Eine Beobachtungszeile bauen (reine Funktion, alle Werte schon gelesen).

    Die Ask-Größen (kaufbare Shares am besten Ask) gehören mit ins Protokoll:
    ohne sie täuscht ein Edge vor, der nur für eine Handvoll Shares an der
    Spitze existiert. Der Report weist die Tiefe je Bucket aus.
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
        "ref_ts": round(ref_ts, 3) if ref_ts is not None else None,
        "ref_age_s": round(ref_age, 3) if ref_age is not None else None,
        "ref_stale": (ref_age is None or ref_age > REF_STALE_S),
        "ref_start": ref_start,
        "pred": predict_direction(ref_now, ref_start),
    }


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round((bid + ask) / 2, 6)


# ---------------------------------------------------------------------------
# Coinbase-Referenz-Feed (Hintergrund-Thread, thread-sicher, wirft nie nach außen)
# ---------------------------------------------------------------------------

class CoinbaseTicker:
    """Hält den letzten Coinbase-Ticker-Preis je Produkt aus dem WSS-Feed.

    Spiegelt die Robustheit von data/stream.py: eigener Thread, Reconnect mit
    Backoff, thread-sicherer Cache. get_price liefert (preis, feed_ts) oder
    (None, None) bei totem Feed — der Aufrufer entscheidet (stale-Markierung).
    """

    def __init__(self, products: list[str], url: str = COINBASE_WS, connect=None):
        self.products = products
        self.url = url
        self._connect = connect or _default_ws_connect
        self._lock = threading.Lock()
        self._prices: dict[str, tuple[float, float]] = {}   # prod -> (price, ts)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_price(self, product: str) -> tuple[float | None, float | None]:
        with self._lock:
            p = self._prices.get(product)
        return (p[0], p[1]) if p else (None, None)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = self._connect(self.url)
                ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": self.products,
                    "channels": ["ticker"],
                }))
                backoff = 1.0
                while not self._stop.is_set():
                    msg = json.loads(ws.recv())
                    if msg.get("type") != "ticker":
                        continue
                    prod = msg.get("product_id")
                    try:
                        price = float(msg["price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    # Eigene Empfangszeit statt Feed-"time": Latenz messen wir
                    # gegen unsere Uhr (dieselbe wie die Snapshot-Zeit).
                    with self._lock:
                        self._prices[prod] = (price, time.time())
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as e:  # pragma: no cover — Netz/Feed-Fehler
                if self._stop.is_set():
                    break
                log.warning("Coinbase-Feed getrennt (%s) — Reconnect in %.0fs",
                            e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)


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
                 ticker: CoinbaseTicker | None = None):
        self.assets = assets or list(ASSET_PRODUCTS)
        self.path = path
        self.poll_s = poll_s
        self.gamma = gamma or GammaClient()
        self.books = books or BookClient()
        self.ticker = ticker or CoinbaseTicker(
            [ASSET_PRODUCTS[a] for a in self.assets])
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

        # Referenz-Startpreis je Fenster einmalig am Start erfassen.
        for slug, (asset, _, _, ws) in self._markets.items():
            ref = self._refs.get(slug)
            if ref and ref.ref_start is None and now >= ws:
                price, ts = self.ticker.get_price(ASSET_PRODUCTS[asset])
                if price is not None:
                    ref.ref_start, ref.ref_start_ts = price, ts

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
            price, ref_ts = self.ticker.get_price(ASSET_PRODUCTS[asset])
            ub, ua, uas = _top(obooks.get(up_t))
            db, da, das = _top(obooks.get(down_t))
            row = build_snapshot(
                ts=now, asset=asset, slug=slug, window_start=ws,
                up_token=up_t, down_token=down_t,
                up_bid=ub, up_ask=ua, down_bid=db, down_ask=da,
                up_ask_size=uas, down_ask_size=das,
                ref_now=price, ref_ts=ref_ts,
                ref_start=ref.ref_start if ref else None)
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
            self._refs[slug] = ref
            seen += 1
        return seen

    def run(self) -> None:  # pragma: no cover — Langläufer-Schleife
        self.ticker.start()
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

        d = buckets.setdefault(b, {"n": 0, "correct": 0, "ask_sum": 0.0,
                                   "pnl_sum": 0.0, "book_leads": 0,
                                   "book_n": 0, "size_sum": 0.0, "size_n": 0,
                                   "windows": set()})
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
