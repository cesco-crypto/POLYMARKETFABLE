import time

import pytest

from polybot.config import BotConfig
from polybot.portfolio import Fill, Portfolio
from polybot.risk import KillSwitch, RiskManager
from polybot.strategies.base import Signal


def buy(token: str, price: float, size: float, group: str | None = None) -> Signal:
    return Signal(token_id=token, side="BUY", price=price, size=size,
                  reason="test", group=group)


def test_risk_lehnt_zu_grosse_order_ab():
    cfg = BotConfig()
    cfg.risk.max_order_usdc = 10.0
    rm = RiskManager(cfg)
    assert rm.filter([buy("t1", 0.5, 100)], Portfolio()) == []


def test_risk_laesst_gueltige_order_durch():
    rm = RiskManager(BotConfig())
    signals = rm.filter([buy("t1", 0.5, 50)], Portfolio())
    assert len(signals) == 1


def test_risk_arb_gruppe_ganz_oder_gar_nicht():
    cfg = BotConfig()
    cfg.risk.max_order_usdc = 30.0
    rm = RiskManager(cfg)
    group = [
        buy("t1", 0.5, 50, group="g1"),   # 25 USDC ok
        buy("t2", 0.9, 50, group="g1"),   # 45 USDC > Limit -> ganze Gruppe weg
    ]
    assert rm.filter(group, Portfolio()) == []


def test_kill_switch_bei_tagesverlust():
    cfg = BotConfig()
    cfg.risk.daily_loss_limit_usdc = 50.0
    rm = RiskManager(cfg)
    pf = Portfolio(cash=940.0, day_start_value=1000.0)  # -60 USDC
    with pytest.raises(KillSwitch):
        rm.check_daily_loss(pf)


def test_portfolio_kauf_und_verkauf_pnl():
    pf = Portfolio(cash=100.0, day_start_value=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY", price=0.40, size=10, reason=""))
    assert pf.cash == pytest.approx(96.0)
    assert pf.exposure("t1") == pytest.approx(4.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="SELL", price=0.60, size=10, reason=""))
    assert pf.cash == pytest.approx(102.0)
    assert pf.realized_pnl == pytest.approx(2.0)
    assert pf.positions == {}


def test_portfolio_persistenz(tmp_path):
    pf = Portfolio(cash=500.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY", price=0.5, size=20, reason="x"))
    path = tmp_path / "state.json"
    pf.save(path)
    loaded = Portfolio.load(path)
    assert loaded.cash == pytest.approx(pf.cash)
    assert loaded.positions["t1"].shares == pytest.approx(20)
