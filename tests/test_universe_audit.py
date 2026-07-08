"""Tests für den Full-Universe-Audit — nur die reinen, deterministischen Kerne
(Netz-Pfade werden nicht getestet)."""

import pytest

from polybot.universe_audit import binom_sign_p, universe_edge, verdict


def test_binom_sign_p_exakte_werte():
    # P(X>=0) = 1 (immer wahr).
    assert binom_sign_p(0, 5) == pytest.approx(1.0)
    # P(X>=n) = 0.5^n (nur der All-Kopf-Ausgang).
    assert binom_sign_p(3, 3) == pytest.approx(1 / 8)
    assert binom_sign_p(10, 10) == pytest.approx(1 / 1024)
    # P(X>=2 | n=3) = (C(3,2)+C(3,3))/8 = 4/8.
    assert binom_sign_p(2, 3) == pytest.approx(0.5)
    # 17/17 positiv (unser Watchlist-Befund): extrem klein — genau die Zahl,
    # die laut Look-ahead-Artikel misstrauisch machen MUSS.
    assert binom_sign_p(17, 17) == pytest.approx(1 / 2**17)
    # Degeneriert: kein n -> kein Test.
    assert binom_sign_p(3, 0) == 1.0


def test_binom_sign_p_normalapprox_grosses_n():
    # Über n=1000 greift die Normalapproximation; k=n/2 -> ~0.5.
    p = binom_sign_p(1000, 2000)
    assert 0.45 < p < 0.55
    # Deutlicher Überschuss -> klein.
    assert binom_sign_p(1200, 2000) < 0.001


def _hist(prices):
    return [{"t": float(i), "p": float(p)} for i, p in enumerate(prices)]


def test_universe_edge_partitioniert_nach_is_vol():
    # Ruhiger Markt: IS (erste 4) flach -> Vol 0 -> selektiert.
    quiet = {"daily_rate": 100.0, "band": 0.03, "comp": 500.0, "size": 100.0,
             "tick": 0.01, "label": "quiet",
             "history": _hist([0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50])}
    # Choppy Markt: IS alterniert stark -> hohe Vol -> NICHT selektiert.
    choppy = {"daily_rate": 100.0, "band": 0.03, "comp": 500.0, "size": 100.0,
              "tick": 0.01, "label": "choppy",
              "history": _hist([0.50, 0.60, 0.50, 0.60, 0.55, 0.55, 0.55, 0.55])}
    rows = universe_edge([quiet, choppy], thresholds=[0.05])
    assert len(rows) == 1
    r = rows[0]
    # Die IS-Vol-Regel trennt sauber: genau der ruhige Markt ist selektiert.
    assert r["selected_n"] == 1
    assert r["rest_n"] == 1
    # sign_p ist konsistent mit der exakten Binomialfunktion.
    assert r["sign_p"] == pytest.approx(binom_sign_p(r["selected_positive"], 1))


def test_universe_edge_eine_zeile_pro_schwelle():
    quiet = {"daily_rate": 100.0, "band": 0.03, "comp": 500.0, "size": 100.0,
             "tick": 0.01, "label": "q",
             "history": _hist([0.5] * 10)}
    rows = universe_edge([quiet], thresholds=[0.001, 0.002, 0.003])
    assert [r["threshold"] for r in rows] == [0.001, 0.002, 0.003]


def test_verdict_haelt_bei_positiver_trennung_und_signifikanz():
    # Über die Schwellen: klar positive Trennung UND kleines sign_p.
    rows = [{"threshold": t, "selected_n": 40, "selected_positive": 34,
             "selected_positive_rate": 0.85, "selected_mean_oos": 0.5,
             "rest_n": 60, "rest_mean_oos": -0.2, "separation": 0.7,
             "sign_p": binom_sign_p(34, 40)}
            for t in (0.0015, 0.002, 0.0025)]
    v = verdict(rows)
    assert v["survives"] is True
    assert v["separation_positive"] == 3
    assert v["sign_significant"] == 3


def test_verdict_faellt_bei_verschwindender_trennung():
    # Gesamtuniversum: keine Trennung, Sign-Test nicht signifikant -> Edge fällt.
    rows = [{"threshold": t, "selected_n": 40, "selected_positive": 20,
             "selected_positive_rate": 0.5, "selected_mean_oos": -0.1,
             "rest_n": 60, "rest_mean_oos": -0.1, "separation": 0.0,
             "sign_p": binom_sign_p(20, 40)}
            for t in (0.0015, 0.002, 0.0025)]
    v = verdict(rows)
    assert v["survives"] is False


def test_verdict_enthaltsam_bei_zu_duennem_universum():
    # Zu wenige selektierte Märkte je Schwelle -> kein Urteil (ehrlich).
    rows = [{"threshold": 0.002, "selected_n": 3, "selected_positive": 3,
             "selected_positive_rate": 1.0, "selected_mean_oos": 1.0,
             "rest_n": 5, "rest_mean_oos": -1.0, "separation": 2.0,
             "sign_p": binom_sign_p(3, 3)}]
    v = verdict(rows)
    assert v["survives"] is False
    assert v["usable_thresholds"] == 0
