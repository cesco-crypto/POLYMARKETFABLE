"""Tests für den Cross-Market-Implikations-Detektor (reine Beobachtung)."""

import json

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.recorder import OpportunityRecorder, find_opportunities
from polybot.strategies.base import MarketSnapshot
from polybot.strategies.implication_detector import (find_implication_pairs,
                                                     find_violations,
                                                     pair_tokens)


def approx(x, tol=1e-9):
    return pytest.approx(x, abs=tol)


def mk_market(i: int, question: str) -> Market:
    return Market(
        condition_id=f"cond{i}", question=question, slug=f"m-{i}",
        yes_token=f"yes{i}", no_token=f"no{i}",
        liquidity=50_000, volume_24h=20_000, neg_risk=False,
    )


def mk_book(token: str, ask: float, ask_size: float = 100.0) -> OrderBook:
    return OrderBook(token_id=token, asks=[Level(ask, ask_size)])


def cfg_ohne_gebuehren() -> BotConfig:
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.0  # einfache Zahlen in den Assertions
    return cfg


# ---- Paar-Erkennung (nur Titel-Parsing) -------------------------------------


def test_over_under_kette_wird_erkannt():
    ev = {"spiel": [mk_market(1, "Real vs Barca: O/U 2.5 goals?"),
                    mk_market(2, "Real vs Barca: O/U 3.5 goals?")]}
    pairs = find_implication_pairs(ev)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.relation == "over_under"
    assert p.specific.condition_id == "cond2"  # höhere Linie = spezifischer
    assert p.general.condition_id == "cond1"
    assert p.event_slug == "spiel"


def test_over_under_kette_mit_drei_linien_liefert_alle_paare():
    ev = {"spiel": [mk_market(1, "X vs Y: Over/Under 1.5 goals?"),
                    mk_market(2, "X vs Y: Over/Under 2.5 goals?"),
                    mk_market(3, "X vs Y: Over/Under 3.5 goals?")]}
    pairs = find_implication_pairs(ev)
    got = {(p.specific.condition_id, p.general.condition_id) for p in pairs}
    assert got == {("cond2", "cond1"), ("cond3", "cond1"), ("cond3", "cond2")}


def test_over_under_verschiedene_schablonen_werden_nie_gepaart():
    # Gleiche Linie/Struktur, aber anderes Zählobjekt -> keine Implikation.
    ev = {"spiel": [mk_market(1, "X vs Y: O/U 2.5 goals?"),
                    mk_market(2, "X vs Y: O/U 3.5 corners?")]}
    assert find_implication_pairs(ev) == []


def test_over_under_gleiche_linie_doppelt_ist_kein_paar():
    ev = {"spiel": [mk_market(1, "X vs Y: O/U 2.5 goals?"),
                    mk_market(2, "X vs Y: O/U 2.5 goals?")]}
    assert find_implication_pairs(ev) == []


def test_over_under_nur_innerhalb_desselben_events():
    ev = {"spiel-a": [mk_market(1, "X vs Y: O/U 2.5 goals?")],
          "spiel-b": [mk_market(2, "X vs Y: O/U 3.5 goals?")]}
    assert find_implication_pairs(ev) == []


def test_kaputte_titel_werden_ignoriert():
    ev = {"spiel": [mk_market(1, "Total goals O/U?"),          # keine Linie
                    mk_market(2, "O/U 3 goals?"),              # ganzzahlige Linie
                    mk_market(3, ""),                          # leer
                    mk_market(4, "Will it rain tomorrow?")]}   # gar kein Muster
    assert find_implication_pairs(ev) == []


def test_win_reach_final_paar_wird_erkannt():
    ev = {"turnier": [mk_market(1, "Will Alcaraz win the tournament?"),
                      mk_market(2, "Will Alcaraz reach the final?")]}
    pairs = find_implication_pairs(ev)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.relation == "win_reach"
    assert p.specific.condition_id == "cond1"  # Sieg impliziert Finale
    assert p.general.condition_id == "cond2"


def test_win_reach_final_nur_bei_gleichem_subjekt():
    ev = {"turnier": [mk_market(1, "Will Alcaraz win the tournament?"),
                      mk_market(2, "Will Sinner reach the final?")]}
    assert find_implication_pairs(ev) == []


def test_win_ohne_turnier_objekt_ist_kein_paar():
    # 'win their next match' impliziert NICHT das Finale -> konservativ auslassen.
    ev = {"turnier": [mk_market(1, "Will Alcaraz win their next match?"),
                      mk_market(2, "Will Alcaraz reach the final?")]}
    assert find_implication_pairs(ev) == []


def test_advance_to_the_final_zaehlt_als_allgemeineres_bein():
    ev = {"turnier": [mk_market(1, "Will Germany win the World Cup?"),
                      mk_market(2, "Will Germany advance to the final?")]}
    pairs = find_implication_pairs(ev)
    assert len(pairs) == 1
    assert pairs[0].specific.condition_id == "cond1"


def test_pair_tokens_liefert_no_des_spezifischen_und_yes_des_allgemeinen():
    ev = {"spiel": [mk_market(1, "X: O/U 2.5 goals?"),
                    mk_market(2, "X: O/U 3.5 goals?")]}
    assert pair_tokens(find_implication_pairs(ev)) == {"no2", "yes1"}


# ---- Verletzungs-Erkennung (Preise) -----------------------------------------


def snap_over_under(no2_ask: float, yes1_ask: float,
                    no2_size: float = 40.0, yes1_size: float = 60.0,
                    **snap_kwargs) -> MarketSnapshot:
    """Event mit O/U-Kette; Verletzung, wenn P(3.5) > P(2.5) + fees + 0.005.

    P(3.5) implizit über die NO-Ask-Seite: 1 - no2_ask. yes1 ist der
    YES-Ask des allgemeineren Markts (O/U 2.5).
    """
    ev = {"spiel": [mk_market(1, "X vs Y: O/U 2.5 goals?"),
                    mk_market(2, "X vs Y: O/U 3.5 goals?")]}
    books = {"no2": mk_book("no2", no2_ask, no2_size),
             "yes1": mk_book("yes1", yes1_ask, yes1_size)}
    return MarketSnapshot(events=ev, books=books, **snap_kwargs)


def test_verletzung_wird_erkannt_und_korrekt_berechnet():
    # P(3.5) = 1 - 0.35 = 0.65 > P(2.5) = 0.60 + 0.005 -> Verletzung.
    snap = snap_over_under(no2_ask=0.35, yes1_ask=0.60)
    violations = find_violations(cfg_ohne_gebuehren(), snap)
    assert len(violations) == 1
    v = violations[0]
    assert v.gross == approx(0.95)
    assert v.fees == 0.0
    assert v.net_edge == approx(0.05)
    assert v.depth == 40  # min der Bein-Tiefen
    assert "O/U 3.5" in v.label and "O/U 2.5" in v.label


def test_konsistente_preise_bleiben_still():
    # P(3.5) = 0.40 <= P(2.5) = 0.60 -> keine Verletzung, kein Eintrag.
    snap = snap_over_under(no2_ask=0.60, yes1_ask=0.60)
    assert find_violations(cfg_ohne_gebuehren(), snap) == []


def test_verletzung_unter_der_marge_bleibt_still():
    # net_edge 0.004 < Marge 0.005 -> still (kleine Verletzungen sind Rauschen).
    snap = snap_over_under(no2_ask=0.396, yes1_ask=0.60)
    assert find_violations(cfg_ohne_gebuehren(), snap) == []


def test_gebuehren_fliessen_in_die_verletzungs_schwelle_ein():
    # Ohne Gebühren wäre net_edge 0.05 eine Verletzung; mit Fallback-Rate
    # 0.07 kosten beide Beine zusammen genug, um sie unter die Marge zu drücken?
    # fees = 0.07*(0.35*0.65 + 0.60*0.40) = 0.07*0.4675 = 0.0327 -> Edge 0.0173.
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.07
    snap = snap_over_under(no2_ask=0.35, yes1_ask=0.60)
    violations = find_violations(cfg, snap)
    assert len(violations) == 1
    assert violations[0].fees == approx(0.07 * (0.35 * 0.65 + 0.60 * 0.40))
    assert violations[0].net_edge == approx(0.05 - violations[0].fees)


def test_fehlende_buecher_werden_uebersprungen():
    ev = {"spiel": [mk_market(1, "X: O/U 2.5 goals?"),
                    mk_market(2, "X: O/U 3.5 goals?")]}
    snap = MarketSnapshot(events=ev, books={"yes1": mk_book("yes1", 0.10)})
    assert find_violations(cfg_ohne_gebuehren(), snap) == []


def test_negrisk_events_werden_ebenfalls_geprueft():
    ev = {"turnier": [mk_market(1, "Will Alcaraz win the tournament?"),
                      mk_market(2, "Will Alcaraz reach the final?")]}
    # P(win) implizit 1 - 0.30 = 0.70 > P(final) 0.50 -> Verletzung.
    snap = MarketSnapshot(negrisk_events=ev,
                          books={"no1": mk_book("no1", 0.30),
                                 "yes2": mk_book("yes2", 0.50)})
    violations = find_violations(cfg_ohne_gebuehren(), snap)
    assert len(violations) == 1
    assert violations[0].net_edge == approx(0.20)


# ---- Recorder-Integration (kind='implication', keine Signale) ---------------


def test_verletzung_landet_als_implication_opportunity_im_recorder(tmp_path):
    snap = snap_over_under(no2_ask=0.35, yes1_ask=0.60)
    opps = find_opportunities(cfg_ohne_gebuehren(), snap, ts=42.0)
    impl = [o for o in opps if o.kind == "implication"]
    assert len(impl) == 1
    o = impl[0]
    assert o.ts == 42.0
    assert o.net_edge == approx(0.05)
    assert o.depth == 40
    assert o.theo_profit == approx(0.05 * 40)
    assert o.above_threshold is False  # NUR beobachten, nie handeln

    # ... und per OpportunityRecorder auf Disk (JSONL, append-only).
    path = tmp_path / "opps.jsonl"
    OpportunityRecorder(cfg_ohne_gebuehren(), path=path).observe(snap, ts=42.0)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["implication"]


def test_config_flag_schaltet_den_detektor_ab():
    cfg = cfg_ohne_gebuehren()
    cfg.strategy.detect_implications = False
    snap = snap_over_under(no2_ask=0.35, yes1_ask=0.60)
    assert find_opportunities(cfg, snap) == []


def test_konsistentes_paar_erzeugt_keinen_log_eintrag():
    # Anders als complement/negrisk gibt es für Implikationen KEIN Logging
    # unterhalb der Verletzungs-Schwelle — konsistente Paare sind still.
    snap = snap_over_under(no2_ask=0.60, yes1_ask=0.60)
    assert find_opportunities(cfg_ohne_gebuehren(), snap) == []


# ---- build_snapshot-Integration ----------------------------------------------


class FakeGamma:
    def __init__(self, events: dict):
        self.events = events

    def active_markets(self, min_liquidity=0.0, limit=500):
        return []

    def negrisk_events(self, min_liquidity=0.0, limit=200):
        return {}

    def all_events(self, min_liquidity=0.0, limit=200):
        return self.events


class FakeBooks:
    def __init__(self):
        self.requested: list[str] = []

    def get_books(self, token_ids):
        self.requested = list(token_ids)
        return {}


def test_build_snapshot_laedt_implikations_events_und_bein_tokens():
    cfg = BotConfig()
    ev = {"spiel": [mk_market(1, "X: O/U 2.5 goals?"),
                    mk_market(2, "X: O/U 3.5 goals?")],
          "ohne-struktur": [mk_market(3, "Will it rain?"),
                            mk_market(4, "Will it snow?")]}
    books = FakeBooks()
    snap = main.build_snapshot(cfg, FakeGamma(ev), books)
    # Nur das Event mit erkannter Struktur landet im Snapshot ...
    assert set(snap.events) == {"spiel"}
    # ... und die Bein-Tokens der Paare werden mit angefragt.
    assert {"no2", "yes1"} <= set(books.requested)
    assert "yes3" not in books.requested


def test_build_snapshot_mit_flag_aus_laedt_keine_events():
    cfg = BotConfig()
    cfg.strategy.detect_implications = False
    ev = {"spiel": [mk_market(1, "X: O/U 2.5 goals?"),
                    mk_market(2, "X: O/U 3.5 goals?")]}
    books = FakeBooks()
    snap = main.build_snapshot(cfg, FakeGamma(ev), books)
    assert snap.events == {}
    assert books.requested == []


def test_build_snapshot_ueberlebt_gamma_fehler_im_event_pfad():
    class BrokenGamma(FakeGamma):
        def all_events(self, min_liquidity=0.0, limit=200):
            raise RuntimeError("Gamma down")

    snap = main.build_snapshot(BotConfig(), BrokenGamma({}), FakeBooks())
    assert snap.events == {}  # Messpfad fällt still aus, Tick lebt weiter
