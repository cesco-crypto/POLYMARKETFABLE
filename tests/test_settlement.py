"""Resolution-Sweeper: Kapital aufgelöster Märkte ausbuchen (Deadlock-Fix)."""

import pytest

from polybot.data.gamma import Market, _parse_market
from polybot.portfolio import Fill, Portfolio
from polybot.settlement import SettlementSweeper

T0 = 1_783_000_000.0


def market(i: int = 1, closed: bool = True, uma: str = "resolved",
           prices: tuple | None = (1.0, 0.0)) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"f{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=1_000.0, volume_24h=1_000.0, neg_risk=False,
                  closed=closed, uma_resolution_status=uma,
                  outcome_prices=prices)


class FakeGamma:
    def __init__(self, markets):
        self.markets = markets
        self.calls = 0

    def markets_by_tokens(self, token_ids):
        self.calls += 1
        return self.markets


def pf_with(*fills: Fill) -> Portfolio:
    pf = Portfolio(cash=1_000.0)
    for f in fills:
        pf.apply_fill(f)
    return pf


def buy(token: str, shares: float, price: float = 0.5) -> Fill:
    return Fill(ts=T0, token_id=token, side="BUY", price=price, size=shares,
                reason="test")


# ---- Market.resolved_payouts ------------------------------------------------

def test_resolved_payouts_final():
    assert market(prices=(1.0, 0.0)).resolved_payouts() == (1.0, 0.0)
    assert market(prices=(0.0, 1.0)).resolved_payouts() == (0.0, 1.0)


def test_resolved_payouts_nicht_final():
    assert market(closed=False).resolved_payouts() is None          # läuft noch
    assert market(uma="").resolved_payouts() is None                # UMA offen
    assert market(prices=(0.6, 0.4)).resolved_payouts() is None     # Handelspreise
    assert market(prices=None).resolved_payouts() is None
    assert market(prices=(1.0, 1.0)).resolved_payouts() is None     # Summe != 1


def test_parse_market_liest_aufloesungsfelder():
    m = _parse_market({
        "conditionId": "0xabc", "question": "Zu?", "slug": "zu",
        "clobTokenIds": '["t1", "t2"]', "closed": True,
        "umaResolutionStatus": "resolved", "outcomePrices": '["0", "1"]',
    })
    assert m.uma_resolution_status == "resolved"
    assert m.outcome_prices == (0.0, 1.0)
    assert m.resolved_payouts() == (0.0, 1.0)


# ---- Portfolio.settle_position ----------------------------------------------

def test_settlement_gewinner_realisiert_gewinn():
    pf = pf_with(buy("yes1", 20, price=0.4))       # Einstand 8.00
    paid = pf.settle_position("yes1", 1.0, "Frage")
    assert paid == pytest.approx(20.0)
    assert pf.cash == pytest.approx(1_000.0 - 8.0 + 20.0)
    assert pf.realized_pnl == pytest.approx(12.0)
    assert "yes1" not in pf.positions
    assert pf.total_exposure() == pytest.approx(0.0)


def test_settlement_verlierer_realisiert_verlust():
    pf = pf_with(buy("no1", 20, price=0.49))       # Einstand 9.80 (der MacBook-Fall)
    paid = pf.settle_position("no1", 0.0)
    assert paid == 0.0
    assert pf.realized_pnl == pytest.approx(-9.8)
    assert "no1" not in pf.positions               # kein ewiger Bestand


# ---- SettlementSweeper.sweep ------------------------------------------------

def test_sweep_bucht_beide_seiten_und_gibt_exposure_frei():
    pf = pf_with(buy("yes1", 10, price=0.5), buy("no1", 4, price=0.5))
    sw = SettlementSweeper(FakeGamma([market(prices=(1.0, 0.0))]))
    paid = sw.sweep(pf, now=T0)
    assert paid == pytest.approx(10.0)             # YES zahlt, NO ist 0
    assert pf.positions == {}
    assert pf.total_exposure() == 0.0


def test_sweep_laesst_laufende_maerkte_liegen():
    pf = pf_with(buy("yes1", 10))
    sw = SettlementSweeper(FakeGamma([market(closed=False, uma="",
                                             prices=(0.6, 0.4))]))
    assert sw.sweep(pf, now=T0) == 0.0
    assert "yes1" in pf.positions


def test_sweep_ist_gedrosselt():
    gamma = FakeGamma([])
    pf = pf_with(buy("yes1", 10))
    sw = SettlementSweeper(gamma)
    sw.sweep(pf, now=T0)
    sw.sweep(pf, now=T0 + 10)                      # innerhalb des Intervalls
    assert gamma.calls == 1
    sw.sweep(pf, now=T0 + sw.SWEEP_INTERVAL_S + 1)
    assert gamma.calls == 2


def test_sweep_ohne_positionen_macht_keinen_request():
    gamma = FakeGamma([])
    SettlementSweeper(gamma).sweep(Portfolio(cash=100.0), now=T0)
    assert gamma.calls == 0


def test_sweep_ueberlebt_gamma_fehler():
    class Boom:
        def markets_by_tokens(self, token_ids):
            raise RuntimeError("Gamma down")

    pf = pf_with(buy("yes1", 10))
    assert SettlementSweeper(Boom()).sweep(pf, now=T0) == 0.0
    assert "yes1" in pf.positions                  # nichts angefasst


def test_sweep_schreibt_ledger():
    class FakeLedger:
        def __init__(self):
            self.entries = []

        def record_merge(self, market, kind, sets, pnl):
            self.entries.append((market, kind, round(pnl, 2)))

    pf = pf_with(buy("yes1", 10, price=0.4))
    ledger = FakeLedger()
    SettlementSweeper(FakeGamma([market(prices=(1.0, 0.0))])).sweep(
        pf, ledger=ledger, now=T0)
    assert ledger.entries == [("Frage 1?", "settlement", 6.0)]


def test_tick_integration_settlement_loest_deadlock():
    """Nach dem Sweep akzeptiert der Risk-Manager wieder neue Gruppen."""
    from polybot.config import BotConfig
    from polybot.execution import PaperBroker
    from polybot.main import tick
    from polybot.risk import RiskManager
    from polybot.strategies.base import MarketSnapshot

    cfg = BotConfig()
    cfg.risk.max_total_exposure_usdc = 12.0
    pf = pf_with(buy("yes1", 20, price=0.5))       # Exposure 10 — fast am Cap
    snap = MarketSnapshot()
    tick(cfg, snap, strategies=[], risk=RiskManager(cfg),
         broker=PaperBroker(cfg), portfolio=pf,
         sweeper=SettlementSweeper(FakeGamma([market(prices=(1.0, 0.0))])))
    assert pf.total_exposure() == 0.0
    assert pf.cash == pytest.approx(1_000.0 - 10.0 + 20.0)
