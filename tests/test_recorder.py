"""Tests für den Opportunity-Recorder und die Report-Aggregation."""

import json

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.portfolio import Portfolio
from polybot.recorder import (OpportunityRecorder, aggregate,
                              find_opportunities, load_opportunities,
                              required_capital)
from polybot.risk import RiskManager
from polybot.strategies.base import MarketSnapshot


def mk_market(i: int, neg_risk: bool = False, augmented: bool = False) -> Market:
    return Market(
        condition_id=f"cond{i}", question=f"Frage {i}?", slug=f"frage-{i}",
        yes_token=f"yes{i}", no_token=f"no{i}",
        liquidity=50_000, volume_24h=20_000, neg_risk=neg_risk,
        neg_risk_augmented=augmented,
    )


def mk_book(token: str, ask: float, ask_size: float = 100.0) -> OrderBook:
    return OrderBook(token_id=token, asks=[Level(ask, ask_size)])


def cfg_ohne_gebuehren() -> BotConfig:
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.0  # einfache Zahlen in den Assertions
    return cfg


# ---- find_opportunities -----------------------------------------------------


def test_complement_gelegenheit_wird_korrekt_berechnet():
    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.48, 100), "no1": mk_book("no1", 0.49, 80)},
    )
    opps = find_opportunities(cfg_ohne_gebuehren(), snap, ts=123.0)
    assert len(opps) == 1
    o = opps[0]
    assert o.kind == "complement"
    assert o.market == "Frage 1?"
    assert o.ts == 123.0
    assert o.gross == approx(0.97)
    assert o.fees == 0.0
    assert o.net_edge == approx(0.03)
    assert o.depth == 80  # min der Bein-Größen
    assert o.theo_profit == approx(0.03 * 80)
    assert o.above_threshold  # 0.03 >= min_edge 0.01, Tiefe >= 5 Shares


def approx(x, tol=1e-9):
    import pytest
    return pytest.approx(x, abs=tol)


def test_gebuehren_fliessen_in_netto_edge_ein():
    # rate * p * (1-p) je Bein: 0.04 * 0.5 * 0.5 * 2 = 0.02
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.07  # Fallback — tokenspezifische Rate gewinnt
    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.50), "no1": mk_book("no1", 0.47)},
        fee_rates={"yes1": 0.04, "no1": 0.04},
    )
    opps = find_opportunities(cfg, snap)
    assert len(opps) == 1
    assert opps[0].fees == approx(0.04 * 0.5 * 0.5 + 0.04 * 0.47 * 0.53)
    assert opps[0].net_edge == approx(1.0 - 0.97 - opps[0].fees)


def test_gelegenheiten_unter_schwelle_werden_geloggt_aber_markiert():
    # Netto-Edge -0.005: unter min_edge, aber über dem Log-Boden von -0.01
    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.50), "no1": mk_book("no1", 0.505)},
    )
    opps = find_opportunities(cfg_ohne_gebuehren(), snap)
    assert len(opps) == 1
    assert opps[0].net_edge == approx(-0.005)
    assert opps[0].theo_profit == 0.0  # max(0, Edge) * Tiefe
    assert not opps[0].above_threshold


def test_zu_negative_edge_wird_nicht_geloggt():
    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.55), "no1": mk_book("no1", 0.50)},
    )
    assert find_opportunities(cfg_ohne_gebuehren(), snap) == []


def test_edge_ueber_schwelle_aber_zu_wenig_tiefe_ist_nicht_handelbar():
    # min_order_shares = 5 -> Tiefe 3 wäre für den Bot nicht handelbar,
    # geloggt wird sie trotzdem (für die Verteilungs-Analyse).
    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.48, 3), "no1": mk_book("no1", 0.49, 100)},
    )
    opps = find_opportunities(cfg_ohne_gebuehren(), snap)
    assert len(opps) == 1
    assert not opps[0].above_threshold


def test_negrisk_yes_und_no_strukturen():
    ms = [mk_market(i, neg_risk=True) for i in (1, 2, 3)]
    books = {}
    for i, (ya, na, ys, ns) in zip((1, 2, 3), [(0.30, 0.60, 50, 30),
                                               (0.30, 0.60, 40, 20),
                                               (0.30, 0.60, 60, 25)]):
        books[f"yes{i}"] = mk_book(f"yes{i}", ya, ys)
        books[f"no{i}"] = mk_book(f"no{i}", na, ns)
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    opps = {o.kind: o for o in find_opportunities(cfg_ohne_gebuehren(), snap)}
    assert set(opps) == {"negrisk_yes", "negrisk_no"}
    y = opps["negrisk_yes"]
    assert y.market == "ev"
    assert y.gross == approx(0.90)
    assert y.net_edge == approx(0.10)  # Auszahlung 1
    assert y.depth == 40
    n = opps["negrisk_no"]
    assert n.gross == approx(1.80)
    assert n.net_edge == approx(2.0 - 1.80)  # Auszahlung n-1 = 2
    assert n.depth == 20
    assert n.theo_profit == approx(0.2 * 20)


def test_negrisk_augmented_laesst_yes_struktur_aus():
    ms = [mk_market(1, neg_risk=True), mk_market(2, neg_risk=True, augmented=True)]
    books = {t: mk_book(t, 0.30) for t in ("yes1", "yes2", "no1", "no2")}
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    kinds = {o.kind for o in find_opportunities(cfg_ohne_gebuehren(), snap)}
    assert "negrisk_yes" not in kinds
    assert "negrisk_no" in kinds  # NO-Struktur bleibt risikofrei


def test_unvollstaendiges_negrisk_event_wird_ausgelassen():
    # Nur EIN NO-Bein verfügbar -> weder Voll- noch Teilmengen-Struktur.
    ms = [mk_market(1, neg_risk=True), mk_market(2, neg_risk=True)]
    books = {"yes1": mk_book("yes1", 0.10), "no1": mk_book("no1", 0.10)}  # Markt 2 fehlt
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    assert find_opportunities(cfg_ohne_gebuehren(), snap) == []


def test_negrisk_partial_no_misst_teilmengen_bei_unvollstaendigem_event():
    # 2 von 3 NO-Beinen verfügbar: höchstens ein Outcome der Teilmenge kann
    # gewinnen -> Auszahlung >= k-1 = 1. Kosten 0.40+0.45 = 0.85 -> Edge 0.15.
    ms = [mk_market(i, neg_risk=True) for i in (1, 2, 3)]
    books = {
        "yes1": mk_book("yes1", 0.55, 10), "no1": mk_book("no1", 0.40, 30),
        "yes2": mk_book("yes2", 0.60, 10), "no2": mk_book("no2", 0.45, 20),
        # Markt 3: kein Buch -> Event unvollständig, negrisk_arb würde passen
    }
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    opps = find_opportunities(cfg_ohne_gebuehren(), snap)
    assert [o.kind for o in opps] == ["negrisk_partial_no"]
    o = opps[0]
    assert o.market == "ev [2/3 NO]"
    assert o.gross == approx(0.85)
    assert o.net_edge == approx(1.0 - 0.85)  # Auszahlung k-1 = 1
    assert o.depth == 20
    assert o.theo_profit == approx(0.15 * 20)
    # Reine Messung: keine Strategie handelt das -> nie above_threshold.
    assert o.above_threshold is False


def test_negrisk_partial_no_auch_wenn_nur_ein_yes_buch_fehlt():
    # Alle NO-Beine da, aber ein YES-Buch fehlt: negrisk_arb verlangt beide
    # Seiten und passt — die Teilmengen-Messung umfasst dann alle n NO-Beine.
    ms = [mk_market(1, neg_risk=True), mk_market(2, neg_risk=True)]
    books = {
        "no1": mk_book("no1", 0.30), "no2": mk_book("no2", 0.40),
        "yes1": mk_book("yes1", 0.65),  # yes2 fehlt
    }
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    opps = find_opportunities(cfg_ohne_gebuehren(), snap)
    assert [o.kind for o in opps] == ["negrisk_partial_no"]
    assert opps[0].market == "ev [2/2 NO]"
    assert opps[0].net_edge == approx(1.0 - 0.70)


def test_negrisk_partial_no_unter_edge_floor_bleibt_still():
    # Fair bepreiste Teilmenge (Summe 1.20 >> Auszahlung 1) wäre nur Rauschen.
    ms = [mk_market(i, neg_risk=True) for i in (1, 2, 3)]
    books = {"no1": mk_book("no1", 0.60), "no2": mk_book("no2", 0.60)}
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    assert find_opportunities(cfg_ohne_gebuehren(), snap) == []


def test_vollstaendiges_event_erzeugt_keine_partial_messung():
    # Komplette Events gehören negrisk_yes/negrisk_no — kein Doppel-Logging.
    ms = [mk_market(1, neg_risk=True), mk_market(2, neg_risk=True)]
    books = {t: mk_book(t, 0.30) for t in ("yes1", "no1", "yes2", "no2")}
    snap = MarketSnapshot(negrisk_events={"ev": ms}, books=books)
    kinds = {o.kind for o in find_opportunities(cfg_ohne_gebuehren(), snap)}
    assert kinds == {"negrisk_yes", "negrisk_no"}


def test_complement_ueberspringt_negrisk_teilmaerkte():
    # Wie in complement_arb: Teilmärkte eines negRisk-Events zählen nicht
    # doppelt (Event-Betrachtung übernimmt sie).
    m = mk_market(1, neg_risk=True)
    snap = MarketSnapshot(
        markets=[m], negrisk_events={"ev": [m, mk_market(2, neg_risk=True)]},
        books={t: mk_book(t, 0.30) for t in ("yes1", "no1", "yes2", "no2")},
    )
    kinds = [o.kind for o in find_opportunities(cfg_ohne_gebuehren(), snap)]
    assert "complement" not in kinds


# ---- OpportunityRecorder (JSONL-Persistenz) --------------------------------


def test_recorder_schreibt_jsonl_und_legt_verzeichnis_an(tmp_path):
    path = tmp_path / "data" / "opportunities.jsonl"
    rec = OpportunityRecorder(cfg_ohne_gebuehren(), path=path)
    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.48), "no1": mk_book("no1", 0.49)},
    )
    rec.observe(snap, ts=100.0)
    rec.observe(snap, ts=200.0)  # append-only: zweiter Tick, zweite Zeile
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["ts"] for r in rows] == [100.0, 200.0]
    assert rows[0]["kind"] == "complement"
    assert rows[0]["net_edge"] == approx(0.03)
    assert rows[0]["above_threshold"] is True


def test_recorder_haengt_am_tick_ohne_signal_logik_zu_aendern(tmp_path):
    # Integration: tick() mit Recorder loggt die Gelegenheit, auch wenn
    # keine Strategie läuft (Recorder rechnet selbst auf dem Snapshot).
    cfg = cfg_ohne_gebuehren()
    path = tmp_path / "opps.jsonl"

    class NullBroker:
        def execute(self, signals, books, portfolio, fee_rates):
            return 0

    snap = MarketSnapshot(
        markets=[mk_market(1)],
        books={"yes1": mk_book("yes1", 0.48), "no1": mk_book("no1", 0.49)},
    )
    fills = main.tick(cfg, snap, [], RiskManager(cfg), NullBroker(),
                      Portfolio(cash=1000.0),
                      recorder=OpportunityRecorder(cfg, path=path))
    assert fills == 0
    assert len(path.read_text().splitlines()) == 1


# ---- Report-Aggregation -----------------------------------------------------


def opp_row(ts: float, net_edge: float, depth: float, gross: float,
            above: bool, fees: float = 0.0) -> dict:
    return {"ts": ts, "kind": "complement", "market": "Frage?", "gross": gross,
            "fees": fees, "net_edge": net_edge, "depth": depth,
            "theo_profit": max(0.0, net_edge) * depth, "above_threshold": above}


def test_load_opportunities_ueberspringt_kaputte_zeilen(tmp_path):
    p = tmp_path / "opps.jsonl"
    p.write_text(json.dumps(opp_row(1.0, 0.02, 10, 0.9, True)) + "\n"
                 + "KAPUTT{{{\n\n"
                 + json.dumps(opp_row(2.0, -0.005, 10, 1.0, False)) + "\n")
    rows = load_opportunities(p)
    assert [r["ts"] for r in rows] == [1.0, 2.0]
    assert load_opportunities(tmp_path / "fehlt.jsonl") == []


def test_aggregate_zaehlt_und_rechnet_auf_24h_hoch():
    # 1h Beobachtung, theoretischer Profit 3.0 -> 72 USDC/Tag hochgerechnet
    rows = [opp_row(0.0, 0.02, 50, 0.9, True),     # theo 1.0
            opp_row(1800.0, -0.005, 99, 1.0, False),  # theo 0.0, unter Schwelle
            opp_row(3600.0, 0.04, 50, 0.9, True)]  # theo 2.0
    stats = aggregate(rows)
    assert stats.duration_s == 3600.0
    assert (stats.n_above, stats.n_below) == (2, 1)
    assert stats.theo_profit_total == approx(3.0)
    assert stats.theo_profit_per_day == approx(72.0)


def test_aggregate_leer_und_ohne_zeitraum():
    assert aggregate([]).theo_profit_per_day == 0.0
    stats = aggregate([opp_row(5.0, 0.02, 10, 0.9, True)])
    assert stats.duration_s == 0.0
    assert stats.theo_profit_per_day == 0.0  # keine Hochrechnung möglich


def test_required_capital_findet_minimales_kapital():
    # Eine Gelegenheit pro Stunde: Edge 0.02, Tiefe 100 Sets, Kosten 0.95/Set.
    # Unbegrenzt: 0.02 * 100 * 24 = 48 USDC/Tag. Ziel 24 USDC/Tag ->
    # 50 Sets pro Gelegenheit noetig -> Kapital 50 * 0.95 = 47.5 USDC.
    rows = [opp_row(0.0, 0.02, 100, 0.95, True)]
    capital, max_daily = required_capital(rows, duration_s=3600.0, target_per_day=24.0)
    assert max_daily == approx(48.0)
    assert capital == approx(47.5, tol=1e-3)


def test_required_capital_meldet_unerreichbares_ziel():
    rows = [opp_row(0.0, 0.02, 100, 0.95, True)]
    capital, max_daily = required_capital(rows, duration_s=3600.0,
                                          target_per_day=1000.0)
    assert capital is None
    assert max_daily == approx(48.0)  # Tiefe ist der Engpass


def test_required_capital_ignoriert_negative_edges():
    rows = [opp_row(0.0, -0.005, 1_000_000, 1.0, False)]
    capital, max_daily = required_capital(rows, duration_s=3600.0,
                                          target_per_day=1.0)
    assert capital is None
    assert max_daily == 0.0


# ---- CLI-Report (Smoke-Test) ------------------------------------------------


def test_cmd_report_laeuft_mit_synthetischer_jsonl(tmp_path, capsys):
    p = tmp_path / "opps.jsonl"
    p.write_text("".join(json.dumps(opp_row(float(i * 600), 0.02, 100, 0.95, True)) + "\n"
                         for i in range(7)))  # 1h Beobachtung, 7 Gelegenheiten
    main.cmd_report(BotConfig(), target=24.0, opps_path=str(p),
                    state_path=str(tmp_path / "paper_state.json"))
    out = capsys.readouterr().out
    assert "Beobachteter Zeitraum" in out
    assert "Hochrechnung auf 24h" in out
    assert "Kapitalfrage" in out


def test_cmd_report_ohne_daten_bricht_freundlich_ab(tmp_path, capsys):
    main.cmd_report(BotConfig(), opps_path=str(tmp_path / "fehlt.jsonl"),
                    state_path=str(tmp_path / "fehlt.json"))
    assert "Keine Beobachtungen" in capsys.readouterr().out


# ---- Größendeckel (Platte-voll-Befund 05.07.2026) ---------------------------

def test_rotation_kuerzt_zu_grosse_datei_an_zeilengrenze(tmp_path):
    from polybot.config import BotConfig
    from polybot.recorder import OpportunityRecorder

    p = tmp_path / "opps.jsonl"
    rec = OpportunityRecorder(BotConfig(), path=p)
    rec.MAX_BYTES = 10_000
    rec.CHECK_EVERY = 1
    lines = [json.dumps({"i": i, "pad": "x" * 80}) for i in range(200)]
    p.write_text("\n".join(lines) + "\n")
    assert p.stat().st_size > rec.MAX_BYTES
    rec._maybe_rotate()
    assert p.stat().st_size <= rec.MAX_BYTES // 2 + 100
    kept = p.read_text().splitlines()
    assert all(json.loads(l) for l in kept)          # nur ganze Zeilen
    assert json.loads(kept[-1])["i"] == 199          # jüngste Daten überleben


def test_rotation_laesst_kleine_datei_in_ruhe(tmp_path):
    from polybot.config import BotConfig
    from polybot.recorder import OpportunityRecorder

    p = tmp_path / "opps.jsonl"
    rec = OpportunityRecorder(BotConfig(), path=p)
    rec.CHECK_EVERY = 1
    p.write_text('{"a": 1}\n')
    before = p.read_text()
    rec._maybe_rotate()
    assert p.read_text() == before


# ---- Retention-Sampling (Flotten-Befund 06.07.2026) -------------------------

def test_negativ_edge_wird_gesampelt_positiv_immer_geschrieben(tmp_path):
    import json as _json
    from polybot.config import BotConfig
    from polybot.recorder import Opportunity, OpportunityRecorder

    rec = OpportunityRecorder(BotConfig(), path=tmp_path / "opps.jsonl")
    rec.NEG_SAMPLE_RATE = 10

    def opp(edge, above=False):
        return Opportunity(ts=1.0, kind="complement", market="m", gross=1.0,
                           fees=0.0, net_edge=edge, depth=5.0,
                           theo_profit=max(0.0, edge) * 5, above_threshold=above)

    # 1000 Negativ-Zeilen -> ~100 geschrieben; 3 positive -> alle 3
    keep_neg = sum(rec._keep(opp(-0.05)) for _ in range(1000))
    assert 90 <= keep_neg <= 110
    assert rec._keep(opp(0.01))              # >= break-even immer
    assert rec._keep(opp(-0.05, above=True))  # above_threshold immer


def test_observe_gibt_alle_opps_zurueck_schreibt_nur_gesampelt(tmp_path, monkeypatch):
    from polybot.config import BotConfig
    from polybot.recorder import Opportunity, OpportunityRecorder
    import polybot.recorder as rmod

    negs = [Opportunity(ts=1.0, kind="complement", market="m", gross=1.2,
                        fees=0.0, net_edge=-0.2, depth=1.0, theo_profit=0.0,
                        above_threshold=False) for _ in range(50)]
    monkeypatch.setattr(rmod, "find_opportunities", lambda *a, **k: negs)
    rec = OpportunityRecorder(BotConfig(), path=tmp_path / "opps.jsonl")
    rec.NEG_SAMPLE_RATE = 50
    out = rec.observe(object())
    assert len(out) == 50                     # Aufrufer sieht alles
    written = (tmp_path / "opps.jsonl").read_text().count("\n") if (tmp_path / "opps.jsonl").exists() else 0
    assert written == 1                        # aber nur 1:50 auf Disk
