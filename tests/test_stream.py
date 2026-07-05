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


def test_stream_tokens_ereignisfenster_schlaegt_volumen():
    # Märkte im Ereignisfenster (endDate nah -> Live-Ereignis läuft) kommen
    # vor volumenstärkeren Dauerläufern ins Abo — dort ballen sich die
    # Preisverwerfungen, und das Abo-Budget ist der Engpass.
    now = 1_000_000.0
    m_vol, m_live, m_fern = market(1, volume_24h=99_000), market(2, volume_24h=100), market(3, volume_24h=50_000)
    m_live.end_ts = now + 3_600       # endet in 1h -> im 4h-Fenster
    m_fern.end_ts = now + 40 * 3_600  # endet in 40h -> außerhalb
    snap = MarketSnapshot(markets=[m_vol, m_live, m_fern])
    toks = main.stream_tokens(snap, cap=6, event_window_s=4 * 3_600, now=now)
    assert toks == ["yes2", "no2", "yes1", "no1", "yes3", "no3"]
    # Fenster aus (0): reine Volumen-Sortierung wie bisher.
    toks = main.stream_tokens(snap, cap=6, event_window_s=0.0, now=now)
    assert toks == ["yes1", "no1", "yes3", "no3", "yes2", "no2"]


def test_stream_tokens_kurzlebige_vor_negrisk_und_nach_end_ts_sortiert():
    # Messbefund 05.07.2026: der Profit lebt in kurzlebigen Up-or-Down/
    # Esports-Märkten (endDate < 2h). Die kommen ZUERST ins Abo — vor den
    # negRisk-Events — und untereinander aufsteigend nach end_ts (die
    # baldigst endenden zuerst), unabhängig vom Volumen.
    now = 1_000_000.0
    m_5min = market(1, volume_24h=100)        # Up-or-Down: endet in 5 Min
    m_1h = market(2, volume_24h=999_000)      # Esports: endet in 1h
    m_daily = market(3, volume_24h=500_000)   # Dauerläufer ohne end_ts
    m_5min.end_ts = now + 300
    m_1h.end_ts = now + 3_600
    snap = MarketSnapshot(markets=[m_daily, m_1h, m_5min],
                          negrisk_events={"ev": [market(10), market(11)]})
    toks = main.stream_tokens(snap, cap=100, event_window_s=2 * 3_600, now=now)
    assert toks == ["yes1", "no1",                    # end_ts +300s zuerst
                    "yes2", "no2",                    # dann +3600s
                    "yes10", "no10", "yes11", "no11",  # dann negRisk komplett
                    "yes3", "no3"]                    # Rest nach Volumen
    # Knapper Deckel: das Abo-Budget gehört zuerst den Kurzlebigen.
    assert main.stream_tokens(snap, cap=4, event_window_s=2 * 3_600,
                              now=now) == ["yes1", "no1", "yes2", "no2"]


def test_stream_tokens_respektiert_min_time_to_end():
    # Märkte unterhalb der Mindest-Restlaufzeit sind nicht mehr handelbar
    # (stale Bücher, Reject-Risiko) — sie dürfen kein Abo-Budget fressen,
    # auch wenn der (alternde) Snapshot sie noch enthält.
    now = 1_000_000.0
    m_tot = market(1, volume_24h=999_000)
    m_ok = market(2, volume_24h=100)
    m_tot.end_ts = now + 60      # unter min_time_to_end_s=120 -> raus
    m_ok.end_ts = now + 600
    snap = MarketSnapshot(markets=[m_tot, m_ok])
    toks = main.stream_tokens(snap, cap=10, event_window_s=2 * 3_600,
                              now=now, min_time_to_end_s=120.0)
    assert toks == ["yes2", "no2"]
    # Auch bei abgeschaltetem Fenster fliegen die Toten raus:
    toks = main.stream_tokens(snap, cap=10, event_window_s=0.0,
                              now=now, min_time_to_end_s=120.0)
    assert toks == ["yes2", "no2"]


def test_worker_rotation_nutzt_fenster_und_mindestrestlaufzeit(monkeypatch):
    # refresh_once muss stream_event_window_s UND min_time_to_end_s aus der
    # Config an stream_tokens durchreichen — sonst abonniert die Rotation
    # tote bzw. falsch priorisierte Tokens.
    cfg = BotConfig()
    now = time.time()
    m_live = market(1, volume_24h=100)
    m_vol = market(2, volume_24h=999_000)
    m_tot = market(3, volume_24h=500_000)
    m_live.end_ts = now + 1_800                          # im 2h-Fenster
    m_tot.end_ts = now + cfg.strategy.min_time_to_end_s / 2  # praktisch tot
    snap = MarketSnapshot(markets=[m_vol, m_live, m_tot])
    monkeypatch.setattr(main, "build_snapshot", lambda *a, **k: snap)
    subscribed: list[list[str]] = []

    class SubSpy:
        def subscribe(self, tokens):
            subscribed.append(tokens)

    worker = main.SnapshotWorker(cfg, None, None, None, streamer=SubSpy())
    assert worker.refresh_once() is True
    assert subscribed == [["yes1", "no1", "yes2", "no2"]]


# ---------------------------------------------------------------------------
# _expiring_tokens: volle Bücher für die kurzlebigen Abo-Märkte
# ---------------------------------------------------------------------------

def test_expiring_tokens_fenster_und_deckel():
    now = 1_000_000.0
    ms = [market(i) for i in range(1, 5)]
    ms[0].end_ts = now + 300          # im Fenster
    ms[1].end_ts = now + 3_600        # im Fenster
    ms[2].end_ts = now + 10 * 3_600   # außerhalb
    # ms[3] ohne end_ts -> nie im Fenster
    toks = main._expiring_tokens(ms, window_s=2 * 3_600, now=now)
    assert toks == {"yes1", "no1", "yes2", "no2"}
    # Fenster 0 = aus:
    assert main._expiring_tokens(ms, window_s=0.0, now=now) == set()
    # Deckel: die baldigst endenden gewinnen (cap=2 -> nur Markt 1).
    assert main._expiring_tokens(ms, window_s=2 * 3_600, now=now,
                                 cap=2) == {"yes1", "no1"}
    assert len(main._expiring_tokens(ms, window_s=2 * 3_600, now=now,
                                     cap=3)) == 2  # halber Markt passt nicht


class FakeStreamer:
    def __init__(self, books_seq):
        self.books_seq = list(books_seq)  # eine Antwort pro Aufruf

    def get_books(self, token_ids):
        return self.books_seq.pop(0) if self.books_seq else {}


class ConstStreamer:
    """Streamer-Fake: liefert bei jedem Aufruf dieselben Bücher (endlos)."""

    def __init__(self, books):
        self.books = books

    def get_books(self, token_ids):
        return dict(self.books)


class StopLoop(BaseException):
    """Testausstieg aus dem endlosen stream_loop.

    Bewusst BaseException: der Loop schluckt gewöhnliche Exceptions aus
    tick() (Robustheit) — der Testausstieg darf davon nicht gefressen werden.
    """


class FakeWorker:
    """SnapshotWorker-Ersatz: liefert ein Drehbuch aus (snap, version)-Paaren
    und beendet den endlosen Loop danach per StopLoop."""

    def __init__(self, states):
        self.states = list(states)

    def snapshot(self):
        if not self.states:
            raise StopLoop
        return self.states.pop(0)


class StubPortfolio:
    def save(self):
        pass

    def value(self, marks=None):
        return 0.0

    def daily_pnl(self, marks=None):
        return 0.0

    def total_exposure(self):
        return 0.0


def run_loop(cfg, worker, streamer, portfolio=None):
    with pytest.raises(StopLoop):
        main.stream_loop(cfg, worker, [], None, None,
                         portfolio or StubPortfolio(), streamer)


def test_stream_loop_tickt_gegen_gestreamte_buecher(monkeypatch):
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap = MarketSnapshot(books={"t1": OrderBook("t1")})
    fresh = OrderBook("t1")
    seen: list[dict] = []
    monkeypatch.setattr(main, "tick",
                        lambda cfg, s, *a: seen.append(s.books) or 1)

    worker = FakeWorker([(snap, 1)] * 2)
    run_loop(cfg, worker, ConstStreamer({"t1": fresh}))
    # 1. Iteration: frischer Snapshot (voller Tick), 2.: Stream-Tick —
    # beide sehen das gestreamte Buch ÜBER dem REST-Buch:
    assert seen == [{"t1": fresh}, {"t1": fresh}]
    assert seen[0]["t1"] is fresh


def test_stream_loop_ohne_stream_tickt_nur_frische_snapshots(monkeypatch):
    # Leerer/toter Stream: zwischen zwei frischen Snapshots wird NICHT
    # getickt (reiner REST-Betrieb) — erst die neue Version tickt wieder.
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap1 = MarketSnapshot(books={"t1": OrderBook("t1")})
    snap2 = MarketSnapshot(books={"t2": OrderBook("t2")})
    seen: list[dict] = []
    monkeypatch.setattr(main, "tick",
                        lambda cfg, s, *a: seen.append(s.books) or 0)

    worker = FakeWorker([(snap1, 1), (snap1, 1), (snap1, 1), (snap2, 2)])
    run_loop(cfg, worker, FakeStreamer([]))  # Stream liefert immer {}
    assert [set(b) for b in seen] == [{"t1"}, {"t2"}]


def test_stream_loop_reicht_killswitch_durch_und_ueberlebt_andere_fehler(monkeypatch):
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap = MarketSnapshot(books={"t1": OrderBook("t1")})

    def boom(*a):
        raise KillSwitch("Tagesverlust")

    monkeypatch.setattr(main, "tick", boom)
    with pytest.raises(KillSwitch):
        main.stream_loop(cfg, FakeWorker([(snap, 1)] * 10), [], None, None,
                         StubPortfolio(), ConstStreamer({"t1": OrderBook("t1")}))

    # Beliebige andere Tick-Fehler werden geloggt und überlebt — der Loop
    # läuft weiter (alter Zustand: sie beendeten den Inner-Loop).
    calls = []
    monkeypatch.setattr(main, "tick", lambda *a: calls.append(1) or
                        (_ for _ in ()).throw(ValueError("kaputt")))
    run_loop(cfg, FakeWorker([(snap, 1)] * 4),
             ConstStreamer({"t1": OrderBook("t1")}))
    assert len(calls) == 4


def test_stream_loop_ueberlebt_streamer_fehler(monkeypatch):
    # Ein kaputter Streamer degradiert zum reinen REST-Betrieb, crasht nie.
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap = MarketSnapshot(books={"t1": OrderBook("t1")})
    seen: list[dict] = []
    monkeypatch.setattr(main, "tick",
                        lambda cfg, s, *a: seen.append(s.books) or 0)

    class BrokenStreamer:
        def get_books(self, token_ids):
            raise RuntimeError("Socket kaputt")

    run_loop(cfg, FakeWorker([(snap, 1), (snap, 1), (snap, 2)]),
             BrokenStreamer())
    # Nur die frischen Snapshots ticken (Versionen 1 und 2), ohne Overlay:
    assert [set(b) for b in seen] == [{"t1"}, {"t1"}]


# ---------------------------------------------------------------------------
# SnapshotWorker: Doppelpuffer, kein Blindfenster, atomarer Swap
# ---------------------------------------------------------------------------

def test_worker_doppelpuffer_kein_blindfenster_und_atomarer_swap(monkeypatch):
    # Kernbefund 05.07.2026: build_snapshot (39-52s) blockierte den einzigen
    # Thread -> 56% Blindzeit. Hier blockiert der 2. Refresh, bis der
    # Inner-Loop 5 Stream-Ticks geschafft hat — die MÜSSEN also während des
    # laufenden Refreshs passieren (kein Blindfenster). Der Swap ist atomar:
    # kein Tick sieht je eine Mischung aus altem und neuem Snapshot.
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.0001
    snap1 = MarketSnapshot(books={"t1": OrderBook("t1")})
    snap2 = MarketSnapshot(books={"t2": OrderBook("t2")})
    release = threading.Event()
    builds = []

    def fake_build(cfg, gamma, books, fees=None):
        builds.append(1)
        if len(builds) == 1:
            return snap1
        release.wait(timeout=10.0)  # simuliert den langsamen REST-Refresh
        return snap2

    monkeypatch.setattr(main, "build_snapshot", fake_build)
    seen: list[set] = []

    def fake_tick(cfg, s, *a):
        seen.append(set(s.books))
        if len(seen) == 5:
            release.set()  # erst JETZT darf der 2. Refresh fertig werden
        if "t2" in s.books:
            raise StopLoop  # frischer Snapshot angekommen -> Test fertig
        return 0

    monkeypatch.setattr(main, "tick", fake_tick)
    worker = main.SnapshotWorker(cfg, None, None, None, min_sleep_s=0.001)
    worker.start()
    try:
        with pytest.raises(StopLoop):
            main.stream_loop(cfg, worker, [], None, None, StubPortfolio(),
                             ConstStreamer({"s1": OrderBook("s1")}))
    finally:
        worker.stop()
    # Kein Blindfenster: mindestens 5 Ticks liefen, BEVOR der 2. Refresh
    # fertig werden durfte; danach kam der frische Snapshot trotzdem an.
    assert len(seen) >= 6
    assert seen[0] == {"t1", "s1"}
    assert seen[-1] == {"t2", "s1"}
    # Atomarer Referenz-Swap: nie alte und neue REST-Bücher gemischt.
    assert not any({"t1", "t2"} <= s for s in seen)


def test_worker_fehler_crasht_bot_nicht_alter_snapshot_bleibt(monkeypatch):
    cfg = BotConfig()
    snap1 = MarketSnapshot(books={"t1": OrderBook("t1")})
    results = [snap1]

    def fake_build(cfg, gamma, books, fees=None):
        if results:
            return results.pop(0)
        raise RuntimeError("Gamma down")

    monkeypatch.setattr(main, "build_snapshot", fake_build)
    worker = main.SnapshotWorker(cfg, None, None, None,
                                 initial_backoff_s=0.001, max_backoff_s=0.01,
                                 min_sleep_s=0.001)
    worker.start()
    try:
        wait_for(lambda: worker.snapshot()[0] is snap1)
        # Ab jetzt schlägt jeder Aufbau fehl: der Worker lebt weiter und der
        # letzte gute Snapshot bleibt für den Inner-Loop gültig.
        time.sleep(0.05)
        assert worker.is_alive()
        assert worker.snapshot() == (snap1, 1)
    finally:
        worker.stop()


def test_worker_rotiert_wss_abo_nach_jedem_frischen_snapshot(monkeypatch):
    cfg = BotConfig()
    snap = MarketSnapshot(markets=[market(1)])
    monkeypatch.setattr(main, "build_snapshot", lambda *a, **k: snap)
    subscribed: list[list[str]] = []

    class SubSpy:
        def subscribe(self, tokens):
            subscribed.append(tokens)

    worker = main.SnapshotWorker(cfg, None, None, None, streamer=SubSpy())
    assert worker.refresh_once() is True
    assert worker.refresh_once() is True
    assert subscribed == [["yes1", "no1"], ["yes1", "no1"]]


def test_worker_abo_fehler_verhindert_swap_nicht(monkeypatch):
    cfg = BotConfig()
    snap = MarketSnapshot(markets=[market(1)])
    monkeypatch.setattr(main, "build_snapshot", lambda *a, **k: snap)

    class BrokenSub:
        def subscribe(self, tokens):
            raise RuntimeError("kaputt")

    worker = main.SnapshotWorker(cfg, None, None, None, streamer=BrokenSub())
    assert worker.refresh_once() is True  # Snapshot ist trotzdem getauscht
    assert worker.snapshot() == (snap, 1)
