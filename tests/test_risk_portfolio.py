import logging
import time

import pytest

from polybot.config import BotConfig
from polybot.portfolio import MAX_FILLS_IN_STATE, Fill, Portfolio, _utc_today
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


def test_risk_lehnt_sell_ohne_bestand_ab():
    # Polymarket erlaubt kein Shorting: SELL nur gegen gehaltene Shares
    rm = RiskManager(BotConfig())
    sell = Signal(token_id="t1", side="SELL", price=0.5, size=10, reason="test")
    assert rm.filter([sell], Portfolio()) == []


def test_risk_laesst_gedeckten_sell_durch():
    rm = RiskManager(BotConfig())
    pf = Portfolio()
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY", price=0.5, size=10, reason=""))
    sell = Signal(token_id="t1", side="SELL", price=0.6, size=10, reason="test")
    assert len(rm.filter([sell], pf)) == 1


def test_portfolio_verweigert_verkauf_ueber_bestand():
    # Regressionstest: ungedeckter SELL buchte früher Phantom-Gewinn
    # (avg_cost=0) und verwarf die negative Position stillschweigend.
    pf = Portfolio(cash=100.0)
    with pytest.raises(ValueError):
        pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="SELL",
                           price=0.6, size=10, reason=""))
    assert pf.realized_pnl == pytest.approx(0.0)
    assert pf.cash == pytest.approx(100.0)
    assert pf.fills == []
    assert pf.positions == {}


def test_risk_gesamt_exposure_summiert_gruppenbeine():
    # Regressionstest: das Gesamtlimit wurde früher pro Bein statt für die
    # Summe der Gruppe geprüft — eine n-Bein-NegRisk-Gruppe, deren Beine
    # einzeln passten, konnte das Limit n-fach überschreiten.
    cfg = BotConfig()
    cfg.risk.max_order_usdc = 500.0
    cfg.risk.max_position_usdc = 500.0
    cfg.risk.max_total_exposure_usdc = 500.0
    rm = RiskManager(cfg)
    group = [buy(f"t{i}", 0.5, 400, group="g1") for i in range(3)]  # 3 x 200 USDC
    assert rm.filter(group, Portfolio(cash=10_000.0)) == []


def test_risk_positionslimit_zaehlt_geplante_kaeufe_desselben_tokens():
    # Regressionstest: geplante Käufe desselben Tokens aus anderen Signalen
    # im selben Tick wurden nicht angerechnet — k Signale erlaubten das
    # k-fache des Positionslimits.
    cfg = BotConfig()
    cfg.risk.max_order_usdc = 200.0
    cfg.risk.max_position_usdc = 200.0
    cfg.risk.max_total_exposure_usdc = 10_000.0
    rm = RiskManager(cfg)
    pf = Portfolio(cash=10_000.0)
    # zwei unabhängige Signale à 150 USDC: nur das erste passt ins Limit
    assert len(rm.filter([buy("t1", 0.5, 300), buy("t1", 0.5, 300)], pf)) == 1
    # beide Beine in derselben Gruppe -> ganze Gruppe abgelehnt
    group = [buy("t1", 0.5, 300, group="g"), buy("t1", 0.5, 300, group="g")]
    assert rm.filter(group, pf) == []


def test_risk_prueft_ungruppierte_signale_einzeln():
    # Regressionstest: alle Signale mit group=None wurden als EINE Gruppe
    # gepoolt — ein einziges ungültiges Signal verwarf sämtliche
    # unabhängigen Signale (z.B. alle MM-Quotes) des Ticks.
    rm = RiskManager(BotConfig())
    signals = [buy("t1", 0.5, 20), buy("t2", 2.0, 20), buy("t3", 0.5, 20)]
    approved = rm.filter(signals, Portfolio())
    assert [s.token_id for s in approved] == ["t1", "t3"]


def test_risk_lehnt_nichtpositive_groesse_ab():
    # Regressionstest: negatives Notional passierte alle Limits und senkte
    # planned_exposure — nachfolgende Gruppen bekamen zusätzlichen Spielraum.
    rm = RiskManager(BotConfig())
    assert rm.filter([buy("t1", 0.5, -10)], Portfolio()) == []
    assert rm.filter([buy("t1", 0.5, 0)], Portfolio()) == []


def test_risk_lehnt_kauf_ueber_cash_ab():
    # Polymarket kennt keine Margin: BUYs müssen durch Cash gedeckt sein.
    cfg = BotConfig()
    cfg.risk.max_total_exposure_usdc = 10_000.0
    rm = RiskManager(cfg)
    assert rm.filter([buy("t1", 0.5, 60)], Portfolio(cash=20.0)) == []  # 30 > 20


def test_kill_switch_sieht_unrealisierte_verluste():
    # Regressionstest: ohne marks wurden Positionen zum Einstandswert
    # bewertet — Mark-to-Market-Verluste waren für den Kill-Switch unsichtbar.
    cfg = BotConfig()
    cfg.risk.daily_loss_limit_usdc = 100.0
    rm = RiskManager(cfg)
    pf = Portfolio(cash=1000.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY",
                       price=0.90, size=500, reason=""))
    rm.check_daily_loss(pf)  # ohne marks: kein Breach sichtbar
    with pytest.raises(KillSwitch):
        rm.check_daily_loss(pf, marks={"t1": 0.10})  # -400 USDC unrealisiert


def test_daily_pnl_reset_am_utc_kalendertag():
    # Regressionstest: früher rollierendes 24h-Fenster ab letztem Reset
    # statt UTC-Kalendertag (widersprach dem eigenen Kommentar).
    pf = Portfolio(cash=1000.0)
    pf.cash = 900.0
    assert pf.daily_pnl() == pytest.approx(-100.0)
    pf.day_start_date = "2020-01-01"  # simulierter Tageswechsel
    assert pf.daily_pnl() == pytest.approx(0.0)  # Basis neu gesetzt
    assert pf.day_start_date == _utc_today()


def test_day_start_value_folgt_start_cash():
    # Regressionstest: hart kodierte 1000.0 machten Portfolio(cash=500)
    # sofort zu -500 Tages-PnL (Kill-Switch-Fehlauslösung).
    assert Portfolio(cash=500.0).daily_pnl() == pytest.approx(0.0)


def test_portfolio_verweigert_kauf_ohne_deckung():
    # Regressionstest: Cash konnte bei BUY unbegrenzt negativ werden.
    pf = Portfolio(cash=10.0)
    with pytest.raises(ValueError):
        pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY",
                           price=0.5, size=100, reason=""))
    assert pf.cash == pytest.approx(10.0)
    assert pf.positions == {}
    assert pf.fills == []


def test_fill_gebuehr_reduziert_cash_und_pnl():
    # Regressionstest: Taker-Gebühren fehlten komplett in der Buchhaltung.
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY",
                       price=0.5, size=10, reason="", fee=0.10))
    assert pf.cash == pytest.approx(94.90)
    assert pf.realized_pnl == pytest.approx(-0.10)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="SELL",
                       price=0.6, size=10, reason="", fee=0.12))
    assert pf.cash == pytest.approx(100.78)
    assert pf.realized_pnl == pytest.approx(1.0 - 0.10 - 0.12)


def test_value_warnt_bei_fehlendem_mark(caplog):
    # Fehlender Mark (Book-Fetch gescheitert): Fallback auf Einstand, aber laut.
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY",
                       price=0.5, size=10, reason=""))
    with caplog.at_level(logging.WARNING, logger="polybot.portfolio"):
        assert pf.value(marks={}) == pytest.approx(100.0)
    assert "Kein Mark" in caplog.text


def test_save_atomar_und_fill_audit_trail(tmp_path):
    path = tmp_path / "state.json"
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY", price=0.5, size=10, reason="a"))
    pf.save(path)
    pf.apply_fill(Fill(ts=time.time(), token_id="t2", side="BUY", price=0.5, size=10, reason="b"))
    pf.save(path)
    assert not (tmp_path / "state.tmp").exists()  # atomar: kein Temp-Rest
    # Audit-Trail: jeder Fill genau einmal, auch über mehrere saves hinweg
    jsonl = tmp_path / "state.fills.jsonl"
    assert len(jsonl.read_text().splitlines()) == 2
    loaded = Portfolio.load(path)
    assert len(loaded.fills) == 2
    loaded.save(path)  # erneutes Speichern dupliziert nichts
    assert len(jsonl.read_text().splitlines()) == 2


def test_fills_in_memory_gekappt(tmp_path):
    # Regressionstest: Disk kappte auf 500, RAM wuchs unbegrenzt — jetzt
    # liegt der volle Trail im JSONL und der RAM wird konsistent gekappt.
    pf = Portfolio(cash=10_000.0)
    for i in range(MAX_FILLS_IN_STATE + 10):
        pf.fills.append(Fill(ts=float(i), token_id="t", side="BUY", price=0.5, size=1, reason=""))
    path = tmp_path / "s.json"
    pf.save(path)
    assert len(pf.fills) == MAX_FILLS_IN_STATE
    lines = (tmp_path / "s.fills.jsonl").read_text().splitlines()
    assert len(lines) == MAX_FILLS_IN_STATE + 10


def test_portfolio_persistenz(tmp_path):
    pf = Portfolio(cash=500.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY", price=0.5, size=20, reason="x"))
    path = tmp_path / "state.json"
    pf.save(path)
    loaded = Portfolio.load(path)
    assert loaded.cash == pytest.approx(pf.cash)
    assert loaded.positions["t1"].shares == pytest.approx(20)
