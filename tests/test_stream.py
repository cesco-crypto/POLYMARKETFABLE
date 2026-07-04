"""Tests für den WSS-Orderbuch-Streamer (gefakter Socket, kein Netzwerk)."""

import json
import threading
import time

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import OrderBook
from polybot.data.stream import BookStreamer
from polybot.risk import KillSwitch
from polybot.strategies.base import MarketSnapshot


def market(i: int, volume_24h: float = 10_000.0) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=1_000.0, volume_24h=volume_24h, neg_risk=True)


def book_event(tid: str, bids: list, asks: list) -> str:
    return json.dumps({
        "event_type": "book", "asset_id": tid,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    })


def streamer_offline(*tokens: str) -> BookStreamer:
    """Streamer ohne laufenden Thread — Events werden direkt eingespeist."""
    s = BookStreamer(connect=lambda url, timeout: (_ for _ in ()).throw(
        AssertionError("kein Netzwerk in diesem Test")))
    with s._lock:
        s._tokens = list(tokens)
        s._token_set = set(tokens)
    return s


def cached_books(s: BookStreamer, token_ids) -> dict[str, OrderBook]:
    """Cache-Inhalt unabhängig vom Verbindungszustand (nur für Tests)."""
    with s._lock:
        return {t: s._books[t] for t in token_ids if t in s._books}


# ---------------------------------------------------------------------------
# Cache: book-Snapshots und price_change-Deltas
# ---------------------------------------------------------------------------

def test_book_snapshot_fuellt_cache_sortiert():
    s = streamer_offline("t1")
    # Live kommen die Bids AUFSTEIGEND (bester Bid zuletzt) — der Cache muss
    # die OrderBook-Konvention (Bids absteigend, Asks aufsteigend) herstellen.
    s._handle_message(book_event("t1", bids=[(0.40, 10), (0.42, 5)],
                                 asks=[(0.50, 7), (0.45, 3)]))
    b = cached_books(s, ["t1"])["t1"]
    assert [lv.price for lv in b.bids] == [0.42, 0.40]
    assert [lv.price for lv in b.asks] == [0.45, 0.50]
    assert b.best_bid.size == 5 and b.best_ask.size == 3


def test_price_change_delta_aendert_entfernt_und_ergaenzt_level():
    s = streamer_offline("t1")
    s._handle_message(book_event("t1", bids=[(0.40, 10)], asks=[(0.45, 3)]))
    # Gebündeltes Live-Format: price_changes mit asset_id pro Eintrag.
    # size ist die NEUE Gesamtgröße; 0 entfernt das Level.
    s._handle_message(json.dumps({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "t1", "price": "0.40", "size": "25", "side": "BUY"},
            {"asset_id": "t1", "price": "0.45", "size": "0", "side": "SELL"},
            {"asset_id": "t1", "price": "0.46", "size": "8", "side": "SELL"},
        ],
    }))
    b = cached_books(s, ["t1"])["t1"]
    assert b.best_bid.price == 0.40 and b.best_bid.size == 25
    assert [lv.price for lv in b.asks] == [0.46]


def test_price_change_einzelformat_mit_changes_liste():
    s = streamer_offline("t1")
    s._handle_message(book_event("t1", bids=[(0.40, 10)], asks=[]))
    s._handle_message(json.dumps({
        "event_type": "price_change", "asset_id": "t1",
        "changes": [{"price": "0.41", "size": "4", "side": "BUY"}],
    }))
    b = cached_books(s, ["t1"])["t1"]
    assert [(lv.price, lv.size) for lv in b.bids] == [(0.41, 4), (0.40, 10)]


def test_price_change_ohne_snapshot_wird_ignoriert():
    # Deltas vor dem book-Snapshot dürfen kein Phantom-Buch anlegen.
    s = streamer_offline("tx")
    s._handle_message(json.dumps({
        "event_type": "price_change",
        "price_changes": [{"asset_id": "tx", "price": "0.5", "size": "1",
                           "side": "BUY"}],
    }))
    assert cached_books(s, ["tx"]) == {}


def test_kaputte_nachrichten_crashen_nicht():
    s = streamer_offline("t1")
    for raw in ("PONG", "kein json", "[1, 2]", json.dumps({"event_type": "book"}),
                json.dumps({"event_type": "book", "asset_id": "t1",
                            "bids": [{"price": "abc"}]}),
                json.dumps({"event_type": "last_trade_price", "asset_id": "t1"})):
        s._handle_message(raw)  # darf nie werfen
    assert cached_books(s, ["t1"]) == {}


def test_events_als_json_array_werden_verarbeitet():
    s = streamer_offline("a", "b")
    s._handle_message(json.dumps([
        json.loads(book_event("a", bids=[(0.1, 1)], asks=[])),
        json.loads(book_event("b", bids=[], asks=[(0.9, 2)])),
    ]))
    assert set(cached_books(s, ["a", "b"])) == {"a", "b"}


def test_get_books_leer_ohne_verbindung():
    # Tote Verbindung -> leeres Dict, damit der Aufrufer auf REST zurückfällt
    # statt mit veralteten Büchern zu handeln.
    s = streamer_offline("t1")
    s._handle_message(book_event("t1", bids=[(0.4, 1)], asks=[]))
    assert s.get_books(["t1"]) == {}
    assert not s.connected


# ---------------------------------------------------------------------------
# Thread: Subscribe, PING, Reconnect mit Backoff
# ---------------------------------------------------------------------------

class FakeSocket:
    """Socket-Fake: liefert vorgegebene Nachrichten, danach Timeout/Abbruch."""

    def __init__(self, messages: list[str], sent: list[str], die: bool):
        self.messages = list(messages)
        self.sent = sent
        self.die = die  # True: nach den Nachrichten Verbindungsabbruch

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if self.die:
            raise ConnectionError("Verbindung weg")
        time.sleep(0.005)
        raise TimeoutError  # wie ein recv()-Timeout: Verbindung lebt weiter

    def close(self) -> None:
        pass


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("Bedingung nicht rechtzeitig erfüllt")


def test_run_loop_subscribed_cached_und_reconnected_mit_neuem_subscribe():
    sent: list[str] = []
    sockets = [
        FakeSocket([book_event("t1", bids=[(0.40, 10)], asks=[(0.45, 3)])],
                   sent, die=True),   # 1. Verbindung stirbt nach dem Snapshot
        FakeSocket([book_event("t1", bids=[(0.41, 7)], asks=[(0.45, 3)])],
                   sent, die=False),  # 2. Verbindung bleibt stehen
    ]
    connects: list[str] = []

    def connect(url, timeout):
        connects.append(url)
        return sockets.pop(0)

    s = BookStreamer(connect=connect, initial_backoff_s=0.01, max_backoff_s=0.05,
                     ping_interval_s=999.0)
    try:
        s.subscribe(["t1", "t2"])
        # Reconnect: 2. Verbindung liefert den aktualisierten Snapshot.
        wait_for(lambda: s.get_books(["t1"]).get("t1") is not None
                 and s.get_books(["t1"])["t1"].best_bid.price == 0.41)
    finally:
        s.stop()
    assert len(connects) == 2
    # Beide Verbindungen haben korrekt subscribed:
    subs = [json.loads(m) for m in sent if m != "PING"]
    assert subs == [{"assets_ids": ["t1", "t2"], "type": "market"}] * 2


def test_ping_wird_periodisch_gesendet():
    sent: list[str] = []
    s = BookStreamer(connect=lambda url, t: FakeSocket([], sent, die=False),
                     ping_interval_s=0.02)
    try:
        s.subscribe(["t1"])
        wait_for(lambda: sent.count("PING") >= 2)
    finally:
        s.stop()


def test_neues_subscribe_triggert_reconnect_und_verwirft_alte_tokens():
    sent: list[str] = []
    connected = threading.Event()

    def connect(url, timeout):
        connected.set()
        return FakeSocket([book_event("t1", bids=[(0.4, 1)], asks=[])],
                          sent, die=False)

    s = BookStreamer(connect=connect, ping_interval_s=999.0)
    try:
        s.subscribe(["t1"])
        wait_for(lambda: s.get_books(["t1"]) != {})
        connected.clear()
        s.subscribe(["t3"])
        wait_for(connected.is_set)
        # Buch des abbestellten Tokens fliegt aus dem Cache:
        wait_for(lambda: cached_books(s, ["t1"]) == {})
    finally:
        s.stop()
    subs = [json.loads(m)["assets_ids"] for m in sent if m != "PING"]
    assert subs == [["t1"], ["t3"]]


def test_subscribe_dedupliziert_und_deckelt():
    s = streamer_offline()
    s.subscribe(["a", "b", "a", "c"])
    assert s._tokens == ["a", "b", "c"]
    s.stop()
    s2 = BookStreamer(connect=lambda u, t: None, max_tokens=2)
    s2.subscribe([f"t{i}" for i in range(10)])
    assert s2._tokens == ["t0", "t1"]
    s2.stop()


# ---------------------------------------------------------------------------
# Integration: stream_tokens und stream_loop in main
# ---------------------------------------------------------------------------

def test_stream_tokens_negrisk_komplett_dann_maerkte_nach_volumen():
    m_low, m_high = market(1), market(2)
    m_low.volume_24h, m_high.volume_24h = 100.0, 9_999.0
    snap = MarketSnapshot(markets=[m_low, m_high],
                          negrisk_events={"ev": [market(10), market(11)]})
    toks = main.stream_tokens(snap, cap=6)
    # Event zuerst (komplett), dann der volumenstärkste Binärmarkt:
    assert toks == ["yes10", "no10", "yes11", "no11", "yes2", "no2"]
    # Deckel 5: das Event (4 Tokens) passt, ein halber Markt nicht mehr.
    assert main.stream_tokens(snap, cap=5) == ["yes10", "no10", "yes11", "no11"]
    # Deckel 3: Event passt nicht KOMPLETT -> nur der Top-Markt.
    assert main.stream_tokens(snap, cap=3) == ["yes2", "no2"]


class FakeStreamer:
    def __init__(self, books_seq):
        self.books_seq = list(books_seq)  # eine Antwort pro Aufruf

    def get_books(self, token_ids):
        return self.books_seq.pop(0) if self.books_seq else {}


def test_stream_loop_tickt_gegen_gestreamte_buecher_und_stoppt_bei_leer(monkeypatch):
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap = MarketSnapshot(books={"t1": OrderBook("t1")})
    fresh = OrderBook("t1")
    seen: list[dict] = []
    monkeypatch.setattr(main, "tick",
                        lambda cfg, s, *a: seen.append(s.books) or 1)

    class P:
        def save(self):
            pass

    streamer = FakeStreamer([{"t1": fresh}, {}])  # 2. Aufruf: Stream leer/tot
    fills = main.stream_loop(cfg, snap, [], None, None, P(), streamer,
                             deadline=time.time() + 30)
    assert fills == 1
    # Gestreamtes Buch überlagert das REST-Buch:
    assert seen == [{"t1": fresh}] and seen[0]["t1"] is fresh


def test_stream_loop_reicht_killswitch_durch_und_schluckt_andere_fehler(monkeypatch):
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap = MarketSnapshot(books={"t1": OrderBook("t1")})
    streamer = FakeStreamer([{"t1": OrderBook("t1")}] * 10)

    def boom(*a):
        raise KillSwitch("Tagesverlust")

    monkeypatch.setattr(main, "tick", boom)
    with pytest.raises(KillSwitch):
        main.stream_loop(cfg, snap, [], None, None, None, streamer,
                         deadline=time.time() + 30)

    monkeypatch.setattr(main, "tick",
                        lambda *a: (_ for _ in ()).throw(ValueError("kaputt")))
    streamer = FakeStreamer([{"t1": OrderBook("t1")}] * 10)
    # Beliebige andere Fehler beenden nur den Inner-Loop (kein Crash):
    assert main.stream_loop(cfg, snap, [], None, None, None, streamer,
                            deadline=time.time() + 30) == 0
