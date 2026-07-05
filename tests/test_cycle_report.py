"""Tests für die Zyklus-Selbstauswertung (PnL-Ledger + cycle-report)."""

import json
import time

from pytest import approx

import polybot.main as main
from polybot import cycle_report as cr
from polybot.config import BotConfig
from polybot.cycle_report import CycleLedger
from polybot.data.gamma import Market
from polybot.portfolio import Portfolio, Position
from polybot.risk import RiskManager
from polybot.strategies.base import MarketSnapshot


def mk_market(i: int) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=100_000.0, volume_24h=50_000.0, neg_risk=False)


def counter_row(ts: float, event: str, pnl: float, fees: float = 0.0,
                rebates: float = 0.0, cash: float = 1_000.0) -> dict:
    return {"ts": ts, "event": event, "realized_pnl": pnl, "fees_paid": fees,
            "rebates_earned": rebates, "cash": cash}


def opp_row(ts: float, kind: str = "complement", net_edge: float = 0.02,
            depth: float = 100.0, gross: float = 0.97, fees: float = 0.01,
            above: bool = True, market: str = "Frage?") -> dict:
    return {"ts": ts, "kind": kind, "market": market, "gross": gross,
            "fees": fees, "net_edge": net_edge, "depth": depth,
            "theo_profit": max(0.0, net_edge) * depth, "above_threshold": above}


# ---- CycleLedger ------------------------------------------------------------


def test_ledger_schreibt_start_und_dedupliziert_ticks(tmp_path):
    # Unveränderte Zähler dürfen den Ledger nicht aufblähen (Stream-Ticks
    # laufen alle stream_tick_s) — geschrieben wird nur bei Änderung oder
    # als Heartbeat nach HEARTBEAT_S.
    path = tmp_path / "ledger.jsonl"
    ledger = CycleLedger(path)
    pf = Portfolio(cash=500.0)
    ledger.record_start(pf, ts=1_000.0)
    ledger.record_tick(pf, ts=1_010.0)   # unverändert, < Heartbeat -> nichts
    pf.realized_pnl = 5.0
    ledger.record_tick(pf, ts=1_020.0)   # Zähler geändert -> Zeile
    ledger.record_tick(pf, ts=1_030.0)   # wieder unverändert -> nichts
    ledger.record_tick(pf, ts=1_020.0 + cr.HEARTBEAT_S)  # Heartbeat -> Zeile
    rows = cr.load_ledger(path)
    assert [r["event"] for r in rows] == ["start", "tick", "tick"]
    assert rows[0]["realized_pnl"] == 0.0
    assert rows[1]["realized_pnl"] == 5.0
    assert rows[1]["cash"] == 500.0


def test_ledger_record_merge_und_kaputte_zeilen(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = CycleLedger(path)
    ledger.record_merge("Frage 1?", "complement", sets=10.0, pnl=0.5, ts=42.0)
    with path.open("a") as fh:
        fh.write("kaputt{\n")  # append-only Log: kaputte Zeile überspringen
    rows = cr.load_ledger(path)
    assert len(rows) == 1
    assert rows[0] == {"ts": 42.0, "event": "merge", "market": "Frage 1?",
                       "kind": "complement", "sets": 10.0, "pnl": 0.5}


def test_ledger_schreibfehler_crasht_nicht(tmp_path):
    # Der Ledger hängt am Tick — Schreibfehler dürfen nur warnen.
    ledger = CycleLedger(tmp_path)  # Pfad ist ein Verzeichnis -> OSError
    ledger.record_start(Portfolio(cash=1.0), ts=1.0)  # darf nicht werfen


# ---- Fenster-Arithmetik -------------------------------------------------------


def test_delta_since_waehlt_baseline_vor_fensterstart():
    rows = [counter_row(100.0, "tick", pnl=1.0, fees=0.1),
            counter_row(200.0, "tick", pnl=3.0, fees=0.3),
            counter_row(300.0, "tick", pnl=7.0, fees=0.7, rebates=0.2)]
    d = cr.delta_since(rows, start_ts=250.0)
    # Baseline = letzte Zeile VOR dem Fenster (Stand bei ts=250 war der von 200)
    assert d["realized_pnl"] == approx(4.0)
    assert d["fees_paid"] == approx(0.4)
    assert d["rebates_earned"] == approx(0.2)
    assert d["covered_from"] == 250.0


def test_delta_since_ledger_beginnt_im_fenster():
    # Beginnt der Ledger erst im Fenster, ist die erste Zeile die Baseline
    # und covered_from markiert die tatsächliche Abdeckung (für die Rate).
    rows = [counter_row(400.0, "start", pnl=2.0),
            counter_row(500.0, "tick", pnl=5.0)]
    d = cr.delta_since(rows, start_ts=100.0)
    assert d["realized_pnl"] == approx(3.0)
    assert d["covered_from"] == 400.0
    leer = cr.delta_since([], start_ts=100.0)
    assert leer["realized_pnl"] == 0.0 and leer["covered_from"] is None


def test_process_start_ts_nimmt_letzten_start():
    rows = [counter_row(10.0, "start", 0.0), counter_row(20.0, "tick", 1.0),
            counter_row(30.0, "start", 1.0)]
    assert cr.process_start_ts(rows) == 30.0
    assert cr.process_start_ts([counter_row(20.0, "tick", 1.0)]) is None


def test_top_markets_summiert_und_sortiert():
    rows = [
        {"ts": 10.0, "event": "merge", "market": "Alt", "kind": "complement",
         "sets": 1.0, "pnl": 99.0},  # vor dem Fenster -> ignoriert
        {"ts": 110.0, "event": "merge", "market": "A", "kind": "complement",
         "sets": 10.0, "pnl": 1.0},
        {"ts": 120.0, "event": "merge", "market": "B", "kind": "negrisk_no",
         "sets": 5.0, "pnl": 3.0},
        {"ts": 130.0, "event": "merge", "market": "A", "kind": "complement",
         "sets": 4.0, "pnl": 0.5},
    ]
    top = cr.top_markets(rows, start_ts=100.0, n=5)
    assert [(m["market"], m["pnl"], m["merges"]) for m in top] == \
        [("B", 3.0, 1), ("A", 1.5, 2)]


# ---- Engpass-Analyse ----------------------------------------------------------


def test_bottleneck_zerlegt_theo_profit_in_engpaesse():
    cfg = BotConfig()  # min_edge 0.01, min_order_shares 5, max_order_usdc 50
    opps = [
        # handelbar, Tiefe über dem Order-Limit: cap_sets = 50/0.98 ~ 51.0,
        # verloren: 0.02 * (100 - 51.02) ~ 0.98 USDC
        opp_row(0.0, net_edge=0.02, depth=100.0, gross=0.97, fees=0.01, above=True),
        # Edge reicht, aber Tiefe unter Mindestgröße -> komplett verloren
        opp_row(0.0, net_edge=0.02, depth=3.0, above=False),
        # positive Edge unter min_edge
        opp_row(0.0, net_edge=0.005, depth=100.0, above=False),
        # implication: keine handelnde Strategie -> komplett ungenutzt
        opp_row(0.0, kind="implication", net_edge=0.05, depth=100.0, above=False),
        # negative Edge: zählt nirgends
        opp_row(0.0, net_edge=-0.005, depth=100.0, above=False),
    ]
    bn = cr.bottleneck(opps, realized=1.0, cash=1_000.0,
                       min_edge=cfg.risk.min_edge,
                       min_shares=cfg.strategy.min_order_shares,
                       max_order_usdc=cfg.risk.max_order_usdc,
                       enabled=["complement_arb", "negrisk_arb"])
    assert bn["theo_total"] == approx(2.0 + 0.06 + 0.5 + 5.0)
    assert bn["theo_tradeable"] == approx(2.0)
    assert bn["lost_min_size"] == approx(0.06)
    assert bn["lost_min_edge"] == approx(0.5)
    assert bn["lost_no_strategy"] == approx(5.0)
    assert bn["lost_order_cap"] == approx(0.02 * (100.0 - 50.0 / 0.98))
    assert bn["cash_binding"] is False
    assert "%" in bn["text"] and "implication" in bn["text"]


def test_bottleneck_erkennt_cash_engpass_und_leeres_fenster():
    opps = [opp_row(0.0, net_edge=0.02, depth=40.0, gross=0.97, fees=0.01,
                    above=True)]
    bn = cr.bottleneck(opps, realized=0.0, cash=10.0, min_edge=0.01,
                       min_shares=5.0, max_order_usdc=50.0,
                       enabled=["complement_arb"])
    # Größte Gelegenheit braucht 40 * 0.98 = 39.2 USDC > 10 Cash
    assert bn["cash_binding"] is True
    assert bn["max_capital_need"] == approx(39.2)
    assert "Cash bindet" in bn["text"]
    leer = cr.bottleneck([], realized=0.0, cash=10.0, min_edge=0.01,
                         min_shares=5.0, max_order_usdc=50.0, enabled=[])
    assert leer["theo_total"] == 0.0 and "Keine Gelegenheiten" in leer["text"]


# ---- build_report / cmd_cycle_report (synthetische Daten) ---------------------


def synth_now() -> float:
    # 04:00 UTC des heutigen Tages: alle Fenster (1h/3h/heute) liegen sicher
    # im selben UTC-Tag — deterministische Fenstergrenzen für die Asserts.
    return cr.utc_day_start(time.time()) + 4 * 3_600.0


def synth_data(now: float) -> tuple[list[dict], list[dict]]:
    day0 = now - 4 * 3_600.0
    ledger = [
        counter_row(day0 - 100.0, "tick", pnl=0.0, fees=0.0),   # Vortag
        counter_row(now - 9_000.0, "start", pnl=2.0, fees=0.5),  # Prozessstart
        counter_row(now - 3_000.0, "tick", pnl=5.0, fees=1.0, rebates=0.1),
        counter_row(now - 60.0, "tick", pnl=8.0, fees=1.5, rebates=0.2),
        {"ts": day0 - 50.0, "event": "merge", "market": "Gestern?",
         "kind": "complement", "sets": 9.0, "pnl": 9.0},  # nicht "heute"
        {"ts": now - 3_000.0, "event": "merge", "market": "A?",
         "kind": "complement", "sets": 10.0, "pnl": 2.0},
        {"ts": now - 60.0, "event": "merge", "market": "B?",
         "kind": "negrisk_no", "sets": 5.0, "pnl": 3.0},
    ]
    opps = [
        opp_row(day0 - 500.0, above=True),                       # Vortag
        opp_row(now - 1_800.0, above=True),                      # in 1h/3h/heute
        opp_row(now - 7_000.0, net_edge=0.02, depth=3.0, above=False),  # nur 3h/heute
        opp_row(day0 + 100.0, kind="negrisk_no", net_edge=0.05, depth=20.0,
                gross=1.85, fees=0.05, above=True),              # nur heute
        opp_row(now - 1_800.0, kind="implication", net_edge=0.05, depth=100.0,
                above=False),
        opp_row(now - 1_800.0, net_edge=-0.005, above=False),    # Fast-Gelegenheit
    ]
    return ledger, opps


def test_build_report_fenster_raten_und_gelegenheiten():
    cfg = BotConfig()
    now = synth_now()
    ledger, opps = synth_data(now)
    report = cr.build_report(cfg, now, ledger, opps, Portfolio(cash=1_000.0))

    w = report["windows"]
    # heute: Baseline ist die Vortags-Zeile (pnl 0) -> Delta 8 über 4h
    assert w["today"]["pnl"] == approx(8.0)
    assert w["today"]["fees"] == approx(1.5)
    assert w["today"]["rebates"] == approx(0.2)
    assert w["today"]["rate_per_h"] == approx(2.0)
    # seit Prozessstart (2.5h): 8 - 2 = 6 -> 2.4/h
    assert report["process_start_ts"] == now - 9_000.0
    assert w["process"]["pnl"] == approx(6.0)
    assert w["process"]["rate_per_h"] == approx(2.4)
    # letzte 1h: Stand zum Fensterstart war der der start-Zeile (pnl 2)
    assert w["1h"]["pnl"] == approx(6.0)
    assert w["1h"]["rate_per_h"] == approx(6.0)
    # letzte 3h: Stand zum Fensterstart war der der Vortags-Zeile (pnl 0)
    assert w["3h"]["pnl"] == approx(8.0)

    o = report["opportunities"]
    assert o["1h"]["complement"]["n_pos"] == 1
    assert o["1h"]["complement"]["theo_profit"] == approx(2.0)
    assert o["1h"]["complement"]["n_logged"] == 2  # inkl. Fast-Gelegenheit
    assert o["today"]["complement"]["n_pos"] == 2
    assert o["today"]["complement"]["theo_profit"] == approx(2.06)
    assert o["today"]["negrisk_no"] == {"n_pos": 1, "n_logged": 1,
                                        "theo_profit": approx(1.0)}
    assert o["today"]["implication"]["theo_profit"] == approx(5.0)
    assert o["today"]["negrisk_yes"] == {"n_pos": 0, "n_logged": 0,
                                         "theo_profit": 0.0}

    # Top-Märkte heute: nur die Merges des Tages, nach PnL sortiert
    assert [(m["market"], m["pnl"]) for m in report["top_markets_today"]] == \
        [("B?", 3.0), ("A?", 2.0)]
    # Engpass rechnet über dem Tagesfenster (theo 2.0+0.06+1.0+5.0)
    assert report["bottleneck"]["theo_total"] == approx(8.06)
    assert report["bottleneck"]["lost_no_strategy"] == approx(5.0)


def test_cmd_cycle_report_druckt_und_schreibt_json(tmp_path):
    cfg = BotConfig()
    now = synth_now()
    ledger, opps = synth_data(now)
    ledger_path = tmp_path / "pnl_ledger.jsonl"
    opps_path = tmp_path / "opps.jsonl"
    with ledger_path.open("w") as fh:
        for r in ledger:
            fh.write(json.dumps(r) + "\n")
    with opps_path.open("w") as fh:
        for o in opps:
            fh.write(json.dumps(o) + "\n")
    json_path = tmp_path / "out" / "cycle_report.json"

    report = main.cmd_cycle_report(cfg, now=now, opps_path=str(opps_path),
                                   ledger_path=ledger_path,
                                   state_path=str(tmp_path / "state.json"),
                                   json_path=json_path)
    # JSON-Ausgabe ist identisch zum Rückgabewert und maschinenlesbar
    saved = json.loads(json_path.read_text())
    assert saved == json.loads(json.dumps(report))
    assert saved["windows"]["today"]["pnl"] == approx(8.0)
    assert "text" in saved["bottleneck"]
    assert saved["totals"]["cash"] == approx(cfg.risk.paper_start_cash)


def test_cmd_cycle_report_ohne_daten_faellt_weich(tmp_path):
    # Frische Umgebung: kein Ledger, kein Opportunity-Log, kein State —
    # der Report darf nicht crashen und schreibt trotzdem sein JSON.
    cfg = BotConfig()
    json_path = tmp_path / "cycle_report.json"
    report = main.cmd_cycle_report(
        cfg, now=synth_now(),
        opps_path=str(tmp_path / "fehlt.jsonl"),
        ledger_path=tmp_path / "fehlt_ledger.jsonl",
        state_path=str(tmp_path / "fehlt_state.json"),
        json_path=json_path)
    assert json_path.exists()
    assert report["process_start_ts"] is None
    assert "process" not in report["windows"]
    assert report["windows"]["today"]["pnl"] == 0.0
    assert report["top_markets_today"] == []


# ---- Integration: merge-Protokoll und tick-Hook -------------------------------


def test_merge_positions_protokolliert_merge_mit_markt_und_pnl(tmp_path):
    # Der realisierte Merge-PnL (payout - Einstand) landet mit Markt-Label
    # im Ledger — Basis für "Top-Märkte nach realisiertem Profit".
    pf = Portfolio(cash=0.0)
    pf.positions = {
        "yes1": Position(token_id="yes1", shares=10.0, cost_basis=4.0),
        "no1": Position(token_id="no1", shares=10.0, cost_basis=5.0),
    }
    snap = MarketSnapshot(markets=[mk_market(1)])
    ledger = CycleLedger(tmp_path / "ledger.jsonl")
    merged = main.merge_positions(snap, pf, ledger)
    assert merged == approx(10.0)
    rows = cr.load_ledger(ledger.path)
    assert len(rows) == 1
    assert rows[0]["event"] == "merge"
    assert rows[0]["market"] == "Frage 1?"
    assert rows[0]["kind"] == "complement"
    assert rows[0]["sets"] == approx(10.0)
    assert rows[0]["pnl"] == approx(1.0)  # 10 USDC payout - 9 USDC Einstand


def test_tick_schreibt_pnl_ledger(tmp_path):
    # Integration: tick() mit Ledger protokolliert den Zählerstand.
    cfg = BotConfig()

    class NullBroker:
        def execute(self, signals, books, portfolio, fee_rates):
            return 0

    ledger = CycleLedger(tmp_path / "ledger.jsonl")
    fills = main.tick(cfg, MarketSnapshot(), [], RiskManager(cfg), NullBroker(),
                      Portfolio(cash=1_000.0), recorder=None, ledger=ledger)
    assert fills == 0
    rows = cr.load_ledger(ledger.path)
    assert [r["event"] for r in rows] == ["tick"]
    assert rows[0]["cash"] == 1_000.0
