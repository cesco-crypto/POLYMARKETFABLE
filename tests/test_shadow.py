"""Tests für den Live/Paper-Schattenvergleich (polybot/shadow.py).

Synthetische Daten, kein Netz: voller Live-Fill -> Capture 1.0, kein
Live-Fill -> 0.0, Teil-Fill anteilig, kein Paper-Fill -> Quote undefiniert
(None). Dazu Aggregation (gesamt / pro Strategie / pro Stunde inkl.
Hochrechnung), die tick()-Integration im Live-Modus und das CLI.
"""

import json

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.execution import PaperBroker
from polybot.portfolio import Fill, Portfolio
from polybot.risk import RiskManager
from polybot.shadow import ShadowTracker, aggregate_capture, load_shadow, \
    strategy_from_reason
from polybot.strategies.base import MarketSnapshot, Signal, Strategy

REASON = "Komplement-Arb Edge=0.050"


def make_signal(size: float = 10.0, price: float = 0.50,
                token: str = "t1", reason: str = REASON,
                edge: float = 0.5) -> Signal:
    return Signal(token_id=token, side="BUY", price=price, size=size,
                  reason=reason, market_question="Frage?", expected_edge=edge)


def make_books(token: str = "t1", ask: float = 0.50,
               depth: float = 100.0) -> dict[str, OrderBook]:
    return {token: OrderBook(token, bids=[Level(ask - 0.02, depth)],
                             asks=[Level(ask, depth)])}


def live_fill(size: float, token: str = "t1", price: float = 0.50,
              reason: str = REASON) -> Fill:
    return Fill(ts=1_000.0, token_id=token, side="BUY", price=price,
                size=size, reason=reason)


def tracker(tmp_path) -> ShadowTracker:
    return ShadowTracker(BotConfig(), path=tmp_path / "shadow.jsonl")


# ---- ShadowTracker.observe: die drei Kernfälle -------------------------------


def test_voller_live_fill_capture_1(tmp_path):
    t = tracker(tmp_path)
    recs = t.observe([make_signal(size=10.0)], make_books(),
                     [live_fill(10.0)], ts=1_000.0)
    assert len(recs) == 1
    assert recs[0].paper_fill == pytest.approx(10.0)
    assert recs[0].live_fill == pytest.approx(10.0)
    assert recs[0].capture_ratio == pytest.approx(1.0)
    # Datensatz landet als JSONL auf Disk.
    rows = load_shadow(tmp_path / "shadow.jsonl")
    assert len(rows) == 1
    assert rows[0]["capture_ratio"] == pytest.approx(1.0)
    assert rows[0]["strategy"] == "complement_arb"


def test_kein_live_fill_capture_0(tmp_path):
    t = tracker(tmp_path)
    recs = t.observe([make_signal(size=10.0)], make_books(), [], ts=1_000.0)
    assert recs[0].paper_fill == pytest.approx(10.0)
    assert recs[0].live_fill == 0.0
    assert recs[0].capture_ratio == pytest.approx(0.0)


def test_teil_fill_anteilige_capture(tmp_path):
    t = tracker(tmp_path)
    recs = t.observe([make_signal(size=10.0)], make_books(),
                     [live_fill(4.0)], ts=1_000.0)
    assert recs[0].paper_fill == pytest.approx(10.0)
    assert recs[0].live_fill == pytest.approx(4.0)
    assert recs[0].capture_ratio == pytest.approx(0.4)


def test_kein_paper_fill_quote_undefiniert(tmp_path):
    # Auch der Paper-Schatten bekommt nichts (leeres Buch): 0/0 ist keine
    # Messung — capture_ratio ist None (null im JSONL) und fällt aus der
    # Aggregation heraus.
    t = tracker(tmp_path)
    books = {"t1": OrderBook("t1", bids=[], asks=[])}
    recs = t.observe([make_signal(size=10.0)], books, [live_fill(4.0)],
                     ts=1_000.0)
    assert recs[0].paper_fill == 0.0
    assert recs[0].capture_ratio is None
    line = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
    assert line["capture_ratio"] is None


def test_zwei_signale_teilen_sich_die_live_fills(tmp_path):
    # Zwei Signale auf denselben (token, side, reason)-Schlüssel: die
    # Live-Fill-Menge wird greedy verteilt, nicht doppelt gezählt.
    t = tracker(tmp_path)
    sigs = [make_signal(size=10.0), make_signal(size=10.0)]
    recs = t.observe(sigs, make_books(), [live_fill(15.0)], ts=1_000.0)
    assert recs[0].live_fill == pytest.approx(10.0)
    assert recs[1].live_fill == pytest.approx(5.0)


def test_schatten_portfolio_bleibt_intern_und_merged(tmp_path):
    # Der Schatten führt ein eigenes Portfolio; mit Snapshot werden komplette
    # YES/NO-Paare wie im Paper-Modus gemergt (Kapital-Recycling), damit die
    # Quote nicht an künstlichem Cash-Mangel verhungert.
    t = tracker(tmp_path)
    m = Market(condition_id="c1", question="Frage?", slug="f",
               yes_token="y1", no_token="n1", liquidity=1.0, volume_24h=1.0,
               neg_risk=False)
    books = {
        "y1": OrderBook("y1", bids=[Level(0.53, 100)], asks=[Level(0.55, 100)]),
        "n1": OrderBook("n1", bids=[Level(0.38, 100)], asks=[Level(0.40, 100)]),
    }
    snap = MarketSnapshot(markets=[m], books=books)
    sigs = [
        Signal(token_id="y1", side="BUY", price=0.55, size=10, reason=REASON,
               market_question="Frage?", group="g1"),
        Signal(token_id="n1", side="BUY", price=0.40, size=10, reason=REASON,
               market_question="Frage?", group="g1"),
    ]
    start_cash = t.portfolio.cash
    t.observe(sigs, books, [], snap=snap, ts=1_000.0)
    # Paar gemergt: keine offene Position, Gewinn realisiert (0.05/Set - Fees).
    assert t.portfolio.positions == {}
    assert t.portfolio.cash > start_cash


def test_observe_crasht_nie(tmp_path, caplog):
    # Kaputte Bücher (None) dürfen den Tick nie mitreißen — nur Warnung.
    t = tracker(tmp_path)
    recs = t.observe([make_signal()], None, [], ts=1_000.0)
    assert recs == []


def test_strategy_from_reason():
    assert strategy_from_reason("Komplement-Arb Edge=0.031") == "complement_arb"
    assert strategy_from_reason("NegRisk-YES-Arb Edge=0.020") == "negrisk_arb"
    assert strategy_from_reason("NegRisk-NO-Arb Edge=0.020") == "negrisk_arb"
    assert strategy_from_reason("MM Bid") == "market_making"
    assert strategy_from_reason("Sonstwas Neues") == "Sonstwas"


# ---- Aggregation --------------------------------------------------------------


def row(ts: float, strategy: str, paper: float, live: float,
        size: float = 10.0, price: float = 0.50, edge: float = 1.0) -> dict:
    return {"ts": ts, "token_id": "t", "side": "BUY", "price": price,
            "size": size, "reason": "r", "strategy": strategy, "group": None,
            "expected_edge": edge, "paper_fill": paper, "live_fill": live,
            "capture_ratio": (live / paper if paper > 0 else None)}


def test_aggregate_capture_gesamt_pro_strategie_pro_stunde():
    rows = [
        row(0.0, "complement_arb", paper=10.0, live=5.0),     # Notional 5 / 2.5
        row(3_600.0, "negrisk_arb", paper=10.0, live=10.0),   # Notional 5 / 5
        row(3_700.0, "negrisk_arb", paper=0.0, live=0.0),     # keine Messung
    ]
    agg = aggregate_capture(rows)
    assert agg["n_records"] == 3
    assert agg["duration_s"] == pytest.approx(3_700.0)
    assert agg["overall"]["capture"] == pytest.approx(7.5 / 10.0)
    assert agg["by_strategy"]["complement_arb"]["capture"] == pytest.approx(0.5)
    assert agg["by_strategy"]["negrisk_arb"]["capture"] == pytest.approx(1.0)
    assert set(agg["by_hour"]) == {"1970-01-01 00:00", "1970-01-01 01:00"}
    assert agg["by_hour"]["1970-01-01 00:00"]["capture"] == pytest.approx(0.5)
    # Hochrechnung: Paper-Edge 2.0 USDC in 3700s -> pro Tag skaliert,
    # mal Capture 75% = ehrliche Live-Erwartung.
    paper_per_day = 2.0 * 86_400.0 / 3_700.0
    assert agg["paper_edge_per_day"] == pytest.approx(paper_per_day)
    assert agg["live_edge_per_day"] == pytest.approx(paper_per_day * 0.75)


def test_aggregate_capture_ohne_paper_fills_capture_none():
    agg = aggregate_capture([row(0.0, "complement_arb", paper=0.0, live=0.0),
                             row(10.0, "complement_arb", paper=0.0, live=0.0)])
    assert agg["overall"]["capture"] is None
    assert agg["live_edge_per_day"] is None


def test_aggregate_capture_zeitraum_zu_kurz_keine_hochrechnung():
    agg = aggregate_capture([row(0.0, "complement_arb", paper=10.0, live=10.0)])
    assert agg["overall"]["capture"] == pytest.approx(1.0)
    assert agg["paper_edge_per_day"] is None
    assert agg["live_edge_per_day"] is None


# ---- Integration: tick() im Live-Modus ----------------------------------------


class OneSignalStrategy(Strategy):
    name = "one_signal"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        return [make_signal(size=10.0)]


def live_cfg() -> BotConfig:
    cfg = BotConfig()
    cfg.mode = "live"
    return cfg


def test_tick_live_speist_shadow_tracker(tmp_path):
    # PaperBroker als Live-Broker-Stand-in (füllt voll): der ShadowTracker
    # bekommt genau die in diesem Tick gebuchten Fills und misst Capture 1.0.
    cfg = live_cfg()
    shadow = ShadowTracker(cfg, path=tmp_path / "shadow.jsonl")
    pf = Portfolio(cash=1_000.0)
    snap = MarketSnapshot(books=make_books(), fee_rates={"t1": 0.0})
    fills = main.tick(cfg, snap, [OneSignalStrategy(cfg)], RiskManager(cfg),
                      PaperBroker(cfg), pf, shadow=shadow)
    assert fills == 1
    rows = load_shadow(tmp_path / "shadow.jsonl")
    assert len(rows) == 1
    assert rows[0]["paper_fill"] == pytest.approx(10.0)
    assert rows[0]["live_fill"] == pytest.approx(10.0)
    assert rows[0]["capture_ratio"] == pytest.approx(1.0)
    # Das Schatten-Portfolio ist vom echten getrennt.
    assert shadow.portfolio is not pf


def test_tick_paper_modus_ignoriert_shadow_tracker(tmp_path):
    # Im Paper-Modus wäre der Schatten identisch zum Lauf selbst — tick()
    # darf ihn nur bei mode == "live" füttern.
    cfg = BotConfig()  # mode == "paper"
    shadow = ShadowTracker(cfg, path=tmp_path / "shadow.jsonl")
    pf = Portfolio(cash=1_000.0)
    snap = MarketSnapshot(books=make_books(), fee_rates={"t1": 0.0})
    main.tick(cfg, snap, [OneSignalStrategy(cfg)], RiskManager(cfg),
              PaperBroker(cfg), pf, shadow=shadow)
    assert not (tmp_path / "shadow.jsonl").exists()


# ---- CLI: capture-report -------------------------------------------------------


def test_cmd_capture_report_mit_synthetischer_jsonl(tmp_path, capsys):
    p = tmp_path / "shadow.jsonl"
    rows = [row(0.0, "complement_arb", paper=10.0, live=5.0),
            row(3_600.0, "negrisk_arb", paper=10.0, live=10.0)]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    agg = main.cmd_capture_report(BotConfig(), shadow_path=p)
    out = capsys.readouterr().out
    assert agg["overall"]["capture"] == pytest.approx(0.75)
    assert "Capture gesamt" in out
    assert "75.0%" in out
    assert "Hochrechnung" in out
    assert "complement_arb" in out


def test_cmd_capture_report_ohne_daten_bricht_freundlich_ab(tmp_path, capsys):
    assert main.cmd_capture_report(BotConfig(),
                                   shadow_path=tmp_path / "fehlt.jsonl") is None
    assert "Keine Schattenvergleichs-Daten" in capsys.readouterr().out
