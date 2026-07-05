import logging

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.fees import FeeRateCache
from polybot.data.gamma import Market
from polybot.portfolio import Portfolio
from polybot.risk import KillSwitch
from polybot.strategies.base import MarketSnapshot


def market(i: int, liquidity: float = 1_000.0,
           fee_rate: float | None = None) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=liquidity, volume_24h=10_000.0, neg_risk=True,
                  fee_rate=fee_rate)


class FakeGamma:
    def __init__(self, negrisk: dict[str, list[Market]]):
        self.negrisk = negrisk

    def active_markets(self, min_liquidity=0.0, limit=500):
        return []

    def negrisk_events(self, min_liquidity=0.0, limit=200):
        return self.negrisk


class FakeBooks:
    def __init__(self):
        self.requested: list[str] = []

    def get_books(self, token_ids):
        self.requested = list(token_ids)
        return {}


def test_build_snapshot_deckelt_negrisk_events():
    # Regressionstest: ohne Deckel wurden die Orderbücher ALLER negRisk-Events
    # geladen (live ~70 Events / ~5000 Tokens) — ein Tick dauerte damit länger
    # als poll_interval_s. Nur Top-N nach Summen-Liquidität dürfen bleiben,
    # Events mit zu vielen Teilmärkten fliegen vorab raus.
    cfg = BotConfig()
    cfg.strategy.max_negrisk_events = 2
    cfg.strategy.max_negrisk_submarkets = 3
    negrisk = {
        "riesig": [market(i, 99_999.0) for i in range(100, 104)],  # 4 Teilmärkte -> raus
        "klein": [market(0, 10.0), market(1, 10.0)],
        "mittel": [market(2, 500.0), market(3, 500.0)],
        "gross": [market(4, 9_000.0), market(5, 9_000.0)],
    }
    books = FakeBooks()
    snap = main.build_snapshot(cfg, FakeGamma(negrisk), books)
    assert set(snap.negrisk_events) == {"gross", "mittel"}
    # Bücher werden nur für die verbliebenen Events angefragt:
    assert set(books.requested) == {"yes2", "no2", "yes3", "no3",
                                    "yes4", "no4", "yes5", "no5"}


def test_build_snapshot_befuellt_fee_rates_aus_gamma_maerkten():
    # fee_rates kommt aus den Gamma-Marktobjekten (kategorieabhängig);
    # Märkte ohne Fee-Info fehlen im Dict (Fallback greift downstream).
    cfg = BotConfig()
    negrisk = {"ev": [market(1, fee_rate=0.03), market(2, fee_rate=None)]}
    snap = main.build_snapshot(cfg, FakeGamma(negrisk), FakeBooks())
    assert snap.fee_rates == {"yes1": 0.03, "no1": 0.03}


def test_build_snapshot_fee_cache_ueberlebt_ticks_ohne_fee_info():
    # Regressionstest: liefert Gamma die Fee-Info eines Markts in einem
    # späteren Tick nicht mit, muss die zuletzt gesehene Rate erhalten
    # bleiben — dafür wird der FeeRateCache über Ticks hinweg geteilt.
    cfg = BotConfig()
    fees = FeeRateCache()
    snap1 = main.build_snapshot(
        cfg, FakeGamma({"ev": [market(1, fee_rate=0.04), market(2, fee_rate=0.0)]}),
        FakeBooks(), fees)
    assert snap1.fee_rates == {"yes1": 0.04, "no1": 0.04, "yes2": 0.0, "no2": 0.0}
    snap2 = main.build_snapshot(
        cfg, FakeGamma({"ev": [market(1, fee_rate=None), market(2, fee_rate=None)]}),
        FakeBooks(), fees)
    assert snap2.fee_rates == snap1.fee_rates


class StubPortfolio:
    cash = 1_000.0
    positions: dict = {}
    fills: list = []

    @classmethod
    def load(cls, path="paper_state.json", start_cash=1000.0):
        return cls()

    def save(self, path=None):
        pass

    def value(self, marks=None):
        return self.cash

    def daily_pnl(self, marks=None):
        return 0.0

    def total_exposure(self):
        return 0.0


def test_cmd_run_bricht_bei_unbekannter_strategie_ab(monkeypatch):
    # Regressionstest: ein Tippfehler in strategy.enabled wurde stillschweigend
    # verworfen — der Bot pollte dann endlos ohne je ein Signal zu erzeugen.
    monkeypatch.setattr(main, "Portfolio", StubPortfolio)
    cfg = BotConfig()
    cfg.strategy.enabled = ["complementarb"]
    with pytest.raises(SystemExit, match="complementarb"):
        main.cmd_run(cfg)


def test_cmd_run_bricht_bei_leerer_strategieliste_ab(monkeypatch):
    monkeypatch.setattr(main, "Portfolio", StubPortfolio)
    cfg = BotConfig()
    cfg.strategy.enabled = []
    with pytest.raises(SystemExit, match="leer"):
        main.cmd_run(cfg)


class NullLedger:
    def __init__(self, path=None):
        self.path = path

    """CycleLedger-Ersatz: cmd_run-Tests dürfen keinen echten PnL-Ledger
    nach data/pnl_ledger.jsonl schreiben (würde reale Messdaten verfälschen)."""

    def record_start(self, portfolio, ts=None):
        pass

    def record_tick(self, portfolio, ts=None):
        pass

    def record_merge(self, market, kind, sets, pnl, ts=None):
        pass


class FakeTime:
    """time-Ersatz: jeder time()-Aufruf springt 30s vor -> Refresh-Überlauf."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        self.now += 30.0
        return self.now

    def strftime(self, fmt):
        return "00:00:00"


def test_worker_warnt_bei_refresh_ueberlauf_und_wartet_mindestens_1s(monkeypatch, caplog):
    # Regressionstest (Nachfolger des alten Tick-Überlauf-Tests): dauert der
    # REST-Refresh länger als poll_interval_s, muss der Worker warnen und
    # trotzdem mindestens 1s pausieren — keine lückenlose Anfragekette gegen
    # die API (Rate-Limit-Schutz).
    monkeypatch.setattr(main, "time", FakeTime())
    monkeypatch.setattr(main, "build_snapshot",
                        lambda *a, **kw: MarketSnapshot())
    cfg = BotConfig()  # poll_interval_s=10, Refresh "dauert" 30s
    worker = main.SnapshotWorker(cfg, None, None, None)
    with caplog.at_level(logging.WARNING, logger="polybot"):
        wait, backoff = worker._cycle(worker.initial_backoff_s)
    assert wait == 1.0
    assert backoff == worker.initial_backoff_s  # Erfolg -> Backoff zurückgesetzt
    assert "Snapshot-Aufbau dauerte" in caplog.text
    assert worker.snapshot()[1] == 1  # der Snapshot wurde trotzdem getauscht


def test_worker_fehler_backoff_waechst_exponentiell(monkeypatch, caplog):
    # Worker-Fehler crashen den Bot nicht: _cycle liefert die Backoff-Wartezeit,
    # der Backoff verdoppelt sich bis zum Deckel, Snapshot/Version unverändert.
    monkeypatch.setattr(main, "build_snapshot",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Gamma down")))
    cfg = BotConfig()
    worker = main.SnapshotWorker(cfg, None, None, None,
                                 initial_backoff_s=2.0, max_backoff_s=5.0)
    with caplog.at_level(logging.ERROR, logger="polybot"):
        assert worker._cycle(2.0) == (2.0, 4.0)
        assert worker._cycle(4.0) == (4.0, 5.0)
        assert worker._cycle(5.0) == (5.0, 5.0)  # Deckel
    assert "alter Snapshot bleibt gültig" in caplog.text
    assert worker.snapshot() == (None, 0)


def test_cmd_run_stoppt_kompletten_bot_bei_killswitch(monkeypatch):
    # KillSwitch aus tick() (Tagesverlustgrenze) muss durch den endlosen
    # Inner-Loop propagieren und cmd_run beenden — trotz Worker-Thread.
    monkeypatch.setattr(main, "Portfolio", StubPortfolio)
    monkeypatch.setattr(main, "CycleLedger", NullLedger)
    monkeypatch.setattr(main, "build_snapshot",
                        lambda *a, **kw: MarketSnapshot())

    def boom(*a, **kw):
        raise KillSwitch("Tagesverlustgrenze erreicht")

    monkeypatch.setattr(main, "tick", boom)
    cfg = BotConfig()
    cfg.strategy.stream_tick_s = 0.001
    main.cmd_run(cfg)  # kehrt zurück statt endlos weiterzulaufen


# ---- Verifikations-Flotte Runde 2: Prozess-Befunde 23/36 --------------------


def test_doppelstart_wird_verweigert(tmp_path, monkeypatch):
    """Befund 23: zwei Prozesse auf demselben State zerschreiben sich
    gegenseitig die Buchhaltung."""
    import fcntl

    monkeypatch.chdir(tmp_path)
    lock = open("paper_state.json.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # "anderer Prozess"
    cfg = BotConfig()
    with pytest.raises(SystemExit, match="Doppelstart"):
        main.cmd_run(cfg)
    lock.close()


def test_snapshot_altersdeckel_pausiert_handel(monkeypatch):
    """Befund 36: fällt der REST-Refresh dauerhaft aus, dürfen Stream-Ticks
    nicht ewig gegen den alternden Snapshot handeln (Phantom-Klasse)."""
    calls = []

    class OldWorker:
        def snapshot(self):
            return MarketSnapshot(), 1

        def snapshot_age(self, now=None):
            return 9_999.0  # weit über dem Deckel

    class CountingBroker:
        def execute(self, *a, **k):
            calls.append(1)
            return 0

    cfg = BotConfig()
    monkeypatch.setattr(main.time, "sleep",
                        lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.stream_loop(cfg, OldWorker(), [], main.RiskManager(cfg),
                         CountingBroker(), Portfolio(cash=100.0), None)
    assert calls == []  # kein Tick gegen den veralteten Snapshot
