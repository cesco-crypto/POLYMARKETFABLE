"""Tests für den Paper-Merge zu USDC (Pendant zum on-chain CTF-Merge).

Ein YES/NO-Paar (bzw. ein vollständiges NegRisk-Set) kann jederzeit zu
Collateral gemerged werden — im Paper-Modus realisiert das den
Arbitragegewinn sofort, statt die Positionen bis zur Auflösung zu halten.
"""

import time

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.execution import PaperBroker
from polybot.portfolio import Fill, Portfolio
from polybot.risk import RiskManager
from polybot.strategies import REGISTRY
from polybot.strategies.base import MarketSnapshot


def buy(pf: Portfolio, token: str, price: float, size: float, fee: float = 0.0) -> None:
    pf.apply_fill(Fill(ts=time.time(), token_id=token, side="BUY",
                       price=price, size=size, reason="test", fee=fee))


# ---- Portfolio.merge_pairs ---------------------------------------------------

def test_merge_pairs_realisiert_arbitragegewinn():
    pf = Portfolio(cash=100.0)
    buy(pf, "yes", 0.55, 20)  # 11.00 USDC
    buy(pf, "no", 0.40, 20)   #  8.00 USDC
    assert pf.merge_pairs("yes", "no") == pytest.approx(20)
    # 20 Paare -> 20 USDC; Einstand 19.00 -> +1.00 realisiert
    assert pf.cash == pytest.approx(100.0 - 19.0 + 20.0)
    assert pf.realized_pnl == pytest.approx(1.0)
    assert pf.positions == {}


def test_merge_pairs_nur_min_beider_beine():
    pf = Portfolio(cash=100.0)
    buy(pf, "yes", 0.50, 30)
    buy(pf, "no", 0.40, 20)
    assert pf.merge_pairs("yes", "no") == pytest.approx(20)
    # 10 YES-Shares bleiben offen, Einstandskosten anteilig (10 * 0.50)
    assert pf.positions["yes"].shares == pytest.approx(10)
    assert pf.positions["yes"].cost_basis == pytest.approx(5.0)
    assert "no" not in pf.positions
    assert pf.realized_pnl == pytest.approx(20.0 - (20 * 0.50 + 20 * 0.40))


def test_merge_pairs_ohne_gegenbein_tut_nichts():
    pf = Portfolio(cash=100.0)
    buy(pf, "yes", 0.50, 10)
    assert pf.merge_pairs("yes", "no") == pytest.approx(0.0)
    assert pf.cash == pytest.approx(95.0)
    assert pf.realized_pnl == pytest.approx(0.0)
    assert pf.positions["yes"].shares == pytest.approx(10)


# ---- Portfolio.merge_negrisk_yes / merge_negrisk_no --------------------------

def test_merge_negrisk_yes_vollstaendiges_set():
    pf = Portfolio(cash=100.0)
    for t in ("y1", "y2", "y3"):
        buy(pf, t, 0.30, 10)  # Set-Kosten 0.90 < 1.00
    assert pf.merge_negrisk_yes(["y1", "y2", "y3"]) == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 9.0 + 10.0)
    assert pf.realized_pnl == pytest.approx(10 * (1.0 - 0.90))
    assert pf.positions == {}


def test_merge_negrisk_yes_unvollstaendiges_set_tut_nichts():
    pf = Portfolio(cash=100.0)
    buy(pf, "y1", 0.30, 10)
    buy(pf, "y2", 0.30, 10)  # y3 fehlt
    assert pf.merge_negrisk_yes(["y1", "y2", "y3"]) == pytest.approx(0.0)
    assert pf.realized_pnl == pytest.approx(0.0)
    assert len(pf.positions) == 2


def test_merge_negrisk_no_zahlt_n_minus_1():
    pf = Portfolio(cash=100.0)
    for t in ("n1", "n2", "n3"):
        buy(pf, t, 0.60, 10)  # Set-Kosten 1.80 < n-1 = 2.00
    assert pf.merge_negrisk_no(["n1", "n2", "n3"], n=3) == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 18.0 + 20.0)
    assert pf.realized_pnl == pytest.approx(10 * (2.0 - 1.80))
    assert pf.positions == {}


# ---- Bot-Loop: automatischer Merge nach der Ausführung -----------------------

def snapshot_komplement_arb() -> tuple[MarketSnapshot, Market]:
    m = Market(condition_id="c1", question="Frage?", slug="frage",
               yes_token="yes1", no_token="no1",
               liquidity=50_000.0, volume_24h=20_000.0, neg_risk=False)
    books = {
        "yes1": OrderBook(token_id="yes1", asks=[Level(0.55, 20)]),
        "no1": OrderBook(token_id="no1", asks=[Level(0.40, 20)]),
    }
    snap = MarketSnapshot(markets=[m], books=books,
                          fee_rates={"yes1": 0.04, "no1": 0.04})
    return snap, m


def test_tick_merged_komplement_arb_zu_usdc():
    # Komplement-Arb-Fill YES@0.55 + NO@0.40 über 20 Shares: nach dem Merge
    # ist der Gewinn realisiert (0.05/Paar minus Taker-Gebühren beider Beine)
    # und die Position geschlossen — wie beim on-chain CTF-Merge.
    cfg = BotConfig()  # mode="paper"
    snap, _ = snapshot_komplement_arb()
    pf = Portfolio(cash=1000.0)
    fills = main.tick(cfg, snap, [REGISTRY["complement_arb"](cfg)],
                      RiskManager(cfg), PaperBroker(), pf)
    assert fills == 2
    # Taker-Gebühr rate * p * (1-p) je Share und Bein:
    fees = 20 * 0.04 * (0.55 * 0.45 + 0.40 * 0.60)
    assert pf.realized_pnl == pytest.approx(0.05 * 20 - fees)
    assert pf.positions == {}  # Paare gemerged, nichts bleibt offen
    assert pf.cash == pytest.approx(1000.0 - 20 * 0.95 - fees + 20.0)
    assert pf.fees_paid == pytest.approx(fees)


def test_tick_merged_nicht_im_live_modus():
    # Live wäre der Merge eine on-chain Transaktion — der Paper-Merge darf
    # dort die Buchhaltung nicht anfassen.
    cfg = BotConfig()
    cfg.mode = "live"
    snap, _ = snapshot_komplement_arb()
    pf = Portfolio(cash=1000.0)
    main.tick(cfg, snap, [REGISTRY["complement_arb"](cfg)],
              RiskManager(cfg), PaperBroker(), pf)
    assert pf.positions["yes1"].shares == pytest.approx(20)
    assert pf.positions["no1"].shares == pytest.approx(20)


def test_merge_positions_deckt_negrisk_sets_ab():
    # Sets kommen aus snap.negrisk_events: vollständige YES-Sets zahlen 1,
    # vollständige NO-Sets (n-1) USDC.
    markets = [
        Market(condition_id=f"c{i}", question=f"F{i}?", slug=f"f{i}",
               yes_token=f"y{i}", no_token=f"n{i}",
               liquidity=1_000.0, volume_24h=1_000.0, neg_risk=True)
        for i in range(3)
    ]
    snap = MarketSnapshot(negrisk_events={"ev": markets})
    pf = Portfolio(cash=100.0)
    for i in range(3):
        buy(pf, f"y{i}", 0.30, 10)
        buy(pf, f"n{i}", 0.60, 10)
    assert main.merge_positions(snap, pf) == pytest.approx(20)  # 10 YES- + 10 NO-Sets
    assert pf.positions == {}
    # YES-Sets: 10 * (1.00 - 0.90); NO-Sets: 10 * (2.00 - 1.80)
    assert pf.realized_pnl == pytest.approx(1.0 + 2.0)
    assert pf.cash == pytest.approx(100.0 - 27.0 + 10.0 + 20.0)
