"""WebSocket-Streaming der Orderbücher (CLOB Market-Channel).

Reaktionszeit in Millisekunden statt Polling: ein Hintergrund-Thread hält die
Verbindung zu wss://ws-subscriptions-clob.polymarket.com/ws/market, abonniert
eine Token-Liste und pflegt aus den Events einen thread-sicheren Cache
token_id -> OrderBook:

  - "book":         voller Snapshot (bids/asks) — ersetzt das gecachte Buch
  - "price_change": Deltas (size = neue Gesamtgröße des Levels, 0 = Level weg)
  - "last_trade_price" u.a.: bewusst ignoriert

Der Server erwartet alle ~10s ein "PING" (Text-Frame), sonst trennt er die
Verbindung. Bei Abbruch reconnected der Thread mit exponentiellem Backoff und
abonniert neu — der Server schickt dann frische book-Snapshots. Ein Ausfall
des Streams darf den Bot nie crashen: get_books liefert bei toter Verbindung
ein leeres Dict, der Aufrufer fällt dann auf den REST-Pfad zurück.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from polybot.data.orderbook import OrderBook, _parse_book

log = logging.getLogger(__name__)

try:
    import websocket
    # recv()-Timeout der Bibliothek + Socket-Timeout der Standardbibliothek
    # (socket.timeout ist seit Python 3.10 ein Alias von TimeoutError).
    _TIMEOUT_EXC: tuple[type[BaseException], ...] = (
        websocket.WebSocketTimeoutException, TimeoutError)
except ImportError:  # pragma: no cover — nur ohne installierte Abhängigkeit
    websocket = None
    _TIMEOUT_EXC = (TimeoutError,)

WSS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 10.0
# Sinnvolle Obergrenze pro Verbindung: der Server akzeptiert zwar mehr, aber
# jenseits ~500 Tokens dominieren Snapshot-Fluten den Nutzen.
MAX_STREAM_TOKENS = 500
# Stream-Buch ohne Updates älter als so viele Sekunden gilt als stale: für
# tote Tokens schickt der Server keine Deltas mehr, ohne dass die Verbindung
# abbricht (recv-Timeout merkt davon nichts). Der Aufrufer fällt für solche
# Tokens auf den REST-Stand zurück — gleiche Größenordnung wie der
# Snapshot-Altersdeckel in main.stream_loop (max(poll_interval*3, 300s)).
DEFAULT_BOOK_MAX_AGE_S = 300.0


def _default_connect(url: str, timeout_s: float):
    """Echte WSS-Verbindung aufbauen (in Tests durch Fakes ersetzt).

    websocket-client liest https_proxy selbst aus der Umgebung; nur das
    CA-Bundle für TLS-abfangende Proxys (Agent-Umgebungen) muss explizit
    gesetzt werden, weil die Bibliothek REQUESTS_CA_BUNDLE/SSL_CERT_FILE
    nicht von sich aus auswertet.
    """
    if websocket is None:
        raise ImportError("websocket-client ist nicht installiert "
                          "(pip install websocket-client)")
    sslopt = {}
    ca = (os.environ.get("WEBSOCKET_CLIENT_CA_BUNDLE")
          or os.environ.get("SSL_CERT_FILE")
          or os.environ.get("REQUESTS_CA_BUNDLE")
          or os.environ.get("CURL_CA_BUNDLE"))
    if ca and os.path.exists(ca):
        sslopt["ca_certs"] = ca
    return websocket.create_connection(url, timeout=timeout_s, sslopt=sslopt)


class BookStreamer:
    """Hintergrund-Thread mit Orderbuch-Cache aus dem Market-WSS-Feed.

    Öffentliche API (alle Methoden thread-sicher, keine wirft nach außen):
    subscribe(tokens) setzt die Abo-Liste (startet den Thread lazy und
    triggert einen Reconnect mit neuem Subscribe), get_books(tokens) liest
    den Cache, stop() beendet den Thread.
    """

    def __init__(self, url: str = WSS_MARKET_URL,
                 connect=None,
                 ping_interval_s: float = PING_INTERVAL_S,
                 recv_timeout_s: float = 2.0,
                 initial_backoff_s: float = 1.0,
                 max_backoff_s: float = 60.0,
                 max_tokens: int = MAX_STREAM_TOKENS,
                 book_max_age_s: float = DEFAULT_BOOK_MAX_AGE_S):
        if connect is None and websocket is None:
            # Früh scheitern statt im Thread endlos zu reconnecten.
            raise ImportError("websocket-client ist nicht installiert")
        self.url = url
        self._connect = connect or _default_connect
        self.ping_interval_s = ping_interval_s
        self.recv_timeout_s = recv_timeout_s
        self.initial_backoff_s = initial_backoff_s
        self.max_backoff_s = max_backoff_s
        self.max_tokens = max_tokens
        self.book_max_age_s = book_max_age_s

        self._lock = threading.Lock()
        self._books: dict[str, OrderBook] = {}
        self._updated: dict[str, float] = {}  # Token -> letztes Buch-Update (monotonic)
        self._tokens: list[str] = []
        self._token_set: set[str] = set()
        self._stop = threading.Event()
        self._resubscribe = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Öffentliche API (Aufrufer-Thread)
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True, solange die WSS-Verbindung steht und der Thread lebt."""
        return (self._connected and self._thread is not None
                and self._thread.is_alive())

    def start(self) -> None:
        """Hintergrund-Thread starten (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="BookStreamer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Thread beenden; blockiert höchstens kurz."""
        self._stop.set()
        self._resubscribe.set()  # Wartende Schleifen sofort wecken
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def subscribe(self, token_ids: list[str]) -> None:
        """Abo-Liste setzen (dedupliziert, auf max_tokens gedeckelt).

        Reine Zustandsänderung — kein Netzwerk im Aufrufer-Thread, kann also
        nicht fehlschlagen. Bei geänderter Liste reconnected der Thread und
        subscribed neu; unveränderte Listen sind ein No-Op (kein unnötiger
        Reconnect pro REST-Tick).
        """
        tokens = list(dict.fromkeys(t for t in token_ids if t))[: self.max_tokens]
        with self._lock:
            if tokens == self._tokens:
                self.start()
                return
            self._tokens = tokens
            self._token_set = set(tokens)
            # Bücher nicht mehr abonnierter Tokens würden nur veralten.
            self._books = {t: b for t, b in self._books.items()
                           if t in self._token_set}
            self._updated = {t: ts for t, ts in self._updated.items()
                             if t in self._token_set}
        self._resubscribe.set()
        self.start()

    def get_books(self, token_ids: list[str] | set[str]) -> dict[str, OrderBook]:
        """Gecachte Bücher für die angefragten Tokens.

        Leeres Dict bei toter Verbindung — Signal an den Aufrufer, auf den
        REST-Pfad zurückzufallen, statt mit veralteten Büchern zu handeln.
        Dasselbe gilt pro Token für Bücher, deren letztes Update älter als
        book_max_age_s ist: schickt der Server für einen Token nichts mehr,
        darf das stille Stream-Buch den frischeren REST-Stand nicht
        überschreiben. Die zurückgegebenen OrderBook-Objekte werden nie
        mutiert (Updates ersetzen sie komplett), Lesen ohne Lock ist danach
        sicher.
        """
        if not self.connected:
            return {}
        now = time.monotonic()
        with self._lock:
            return {t: self._books[t] for t in token_ids
                    if t in self._books
                    and now - self._updated.get(t, 0.0) <= self.book_max_age_s}

    # ------------------------------------------------------------------
    # Hintergrund-Thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        backoff = self.initial_backoff_s
        while not self._stop.is_set():
            # Flag VOR dem Lesen der Token-Liste löschen: kommt ein subscribe()
            # dazwischen, bleibt das Flag gesetzt und die Verbindung wird
            # sofort mit der neuen Liste neu aufgebaut.
            self._resubscribe.clear()
            with self._lock:
                tokens = list(self._tokens)
            if not tokens:
                # Noch nichts abonniert: auf subscribe() warten.
                self._resubscribe.wait(timeout=1.0)
                continue
            ws = None
            failed = False
            try:
                ws = self._connect(self.url, self.recv_timeout_s)
                ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                # Alte Bücher verwerfen: der Server schickt nach dem Subscribe
                # frische book-Snapshots für alle Tokens.
                with self._lock:
                    self._books.clear()
                    self._updated.clear()
                self._connected = True
                backoff = self.initial_backoff_s
                last_ping = time.monotonic()
                while not (self._stop.is_set() or self._resubscribe.is_set()):
                    if time.monotonic() - last_ping >= self.ping_interval_s:
                        ws.send("PING")
                        last_ping = time.monotonic()
                    try:
                        raw = ws.recv()
                    except _TIMEOUT_EXC:
                        continue  # nur Gelegenheit für PING/Stop-Check
                    if raw:
                        self._handle_message(raw)
            except Exception as e:  # noqa: BLE001 — Reconnect statt Crash
                failed = True
                log.warning("Orderbuch-Stream abgebrochen: %s — Reconnect in %.0fs",
                            e, backoff)
            finally:
                self._connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass
            if failed and not self._stop.is_set() and not self._resubscribe.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)
            # Bei _resubscribe: sofort neu verbinden (neue Token-Liste).

    # ------------------------------------------------------------------
    # Event-Verarbeitung (auch direkt testbar)
    # ------------------------------------------------------------------

    def _handle_message(self, raw: str) -> None:
        """Eine WSS-Textnachricht verarbeiten — wirft nie nach außen."""
        if raw in ("PONG", "PING"):
            return
        try:
            data = json.loads(raw)
        except ValueError:
            log.debug("Nicht-JSON-Nachricht im Stream ignoriert: %.60s", raw)
            return
        # Der Server bündelt Events teils als JSON-Array.
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                self._apply_event(ev)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                log.warning("Stream-Event nicht verarbeitbar (%s): %s",
                            ev.get("event_type"), e)

    def _apply_event(self, ev: dict) -> None:
        et = ev.get("event_type")
        if et == "book":
            self._apply_book(ev)
        elif et == "price_change":
            self._apply_price_change(ev)
        # last_trade_price, tick_size_change etc.: für den Cache irrelevant.

    def _apply_book(self, ev: dict) -> None:
        """Voller Snapshot: gecachtes Buch komplett ersetzen."""
        tid = ev.get("asset_id")
        if not tid:
            return
        book = _parse_book(tid, ev)  # sortiert bids absteigend, asks aufsteigend
        with self._lock:
            # Nur abonnierte Tokens cachen: nach einem Reconnect können noch
            # Events abbestellter Tokens eintreffen — die dürfen den Cache
            # nicht wieder befüllen (Speicher bleibt so auch beschränkt).
            if tid in self._token_set:
                self._books[tid] = book
                self._updated[tid] = time.monotonic()

    def _apply_price_change(self, ev: dict) -> None:
        """Deltas anwenden. Zwei live beobachtete Formate:

        gebündelt:  {"price_changes": [{"asset_id": ..., "price": ...,
                     "size": ..., "side": "BUY"}, ...]}
        einzeln:    {"asset_id": ..., "changes": [{"price": ..., "size": ...,
                     "side": ...}, ...]}

        size ist die NEUE Gesamtgröße des Levels; 0 entfernt das Level.
        """
        for pc in ev.get("price_changes") or []:
            tid = pc.get("asset_id")
            if tid:
                self._apply_changes(tid, [pc])
        tid = ev.get("asset_id")
        if tid and ev.get("changes"):
            self._apply_changes(tid, ev["changes"])

    def _apply_changes(self, tid: str, changes: list[dict]) -> None:
        with self._lock:
            book = self._books.get(tid)
            if book is None:
                # Ohne Snapshot keine Deltas — das Buch kommt mit dem
                # nächsten book-Event (der Server schickt eins pro Subscribe).
                return
            bids = {lv.price: lv.size for lv in book.bids}
            asks = {lv.price: lv.size for lv in book.asks}
            for ch in changes:
                price = float(ch["price"])
                size = float(ch["size"])
                side = str(ch.get("side", "")).upper()
                levels = bids if side == "BUY" else asks if side == "SELL" else None
                if levels is None:
                    continue
                if size <= 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
            # Neues Objekt statt In-Place-Mutation: Leser (get_books) halten
            # evtl. noch Referenzen auf das alte Buch.
            self._books[tid] = _parse_book(tid, {
                "bids": [{"price": p, "size": s} for p, s in bids.items()],
                "asks": [{"price": p, "size": s} for p, s in asks.items()],
            })
            self._updated[tid] = time.monotonic()
