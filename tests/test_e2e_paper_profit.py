"""End-to-End-Test der kompletten Gewinn-Pipeline im Paper-Modus — ohne Netz.

Beweist gegen einen synthetischen MarketSnapshot, dass der volle Pfad
Strategien -> RiskManager -> PaperBroker -> Merge aus einer vorhandenen
Arbitrage tatsächlich realisierten Gewinn macht (und dass bei fehlender
Buchtiefe die FOK-Semantik greift: kein Fill, Cash unverändert).
"""

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.execution import PaperBroker
from polybot.portfolio import Fill, Portfolio
from polybot.risk import KillSwitch, RiskManager
from polybot.strategies import REGISTRY
from polybot.strategies.base import MarketSnapshot, Signal, Strategy

FEE_RATE = 0.04  # Politik/Finance-Kategorie — für alle Tokens im Szenario


def _binary_market() -> Market:
    return Market(condition_id="bin1", question="Binärmarkt?", slug="bin",
                  yes_token="b-yes", no_token="b-no",
                  liquidity=50_000.0, volume_24h=20_000.0, neg_risk=False)


def _negrisk_markets() -> list[Market]:
    return [
        Market(condition_id=f"nr{i}", question=f"Outcome {i}?", slug=f"nr-{i}",
               yes_token=f"nr-y{i}", no_token=f"nr-n{i}",
               liquidity=30_000.0, volume_24h=15_000.0, neg_risk=True,
               event_slug="ev")
        for i in range(3)
    ]


def build_arb_snapshot(no_ask_size: float = 200.0) -> MarketSnapshot:
    """Snapshot mit zwei Arbitragen:

    - Binärmarkt: YES-Ask 0.55 + NO-Ask 0.40 = 0.95 < 1.00
    - NegRisk-Event (3 Outcomes): YES-Asks je 0.30, Summe 0.90 < 1.00;
      NO-Asks je 0.75 (Summe 2.25 > n-1=2 -> KEIN NO-Arb, aber genug
      NO-Liquidität, damit das Event als vollständig gilt).
    """
    books = {
        "b-yes": OrderBook("b-yes", bids=[Level(0.53, 100)], asks=[Level(0.55, 200)]),
        "b-no": OrderBook("b-no", bids=[Level(0.38, 100)], asks=[Level(0.40, no_ask_size)]),
    }
    for i in range(3):
        books[f"nr-y{i}"] = OrderBook(f"nr-y{i}", bids=[Level(0.28, 100)],
                                      asks=[Level(0.30, 100)])
        books[f"nr-n{i}"] = OrderBook(f"nr-n{i}", bids=[Level(0.73, 100)],
                                      asks=[Level(0.75, 100)])
    return MarketSnapshot(
        markets=[_binary_market()],
        books=books,
        negrisk_events={"ev": _negrisk_markets()},
        fee_rates={t: FEE_RATE for t in books},
    )


def test_e2e_arbitrage_pipeline_realisiert_gewinn():
    # Volle Pipeline wie main.tick: Strategien -> RiskManager -> PaperBroker
    # -> Merge, frisches Portfolio mit 1000 USDC Start-Cash.
    cfg = BotConfig()  # mode="paper", max_order_usdc=50, min_edge=0.01
    pf = Portfolio(cash=1000.0)
    strategies = [REGISTRY[n](cfg) for n in ("complement_arb", "negrisk_arb")]
    fills = main.tick(cfg, build_arb_snapshot(), strategies,
                      RiskManager(cfg), PaperBroker(cfg), pf)

    # 2 Beine Komplement-Arb + 3 Beine NegRisk-YES-Arb
    assert fills == 5
    assert len(pf.fills) == 5

    # Erwartete Größen/Edges wie in den Strategien: Gebühren im Nenner,
    # damit der reale Cash-Abfluss <= max_order_usdc bleibt.
    comp_fees = FEE_RATE * (0.55 * 0.45 + 0.40 * 0.60)   # Gebühr je Paar
    comp_size = 50.0 / (0.95 + comp_fees)
    comp_edge = 1.0 - 0.95 - comp_fees
    nr_fees = 3 * FEE_RATE * 0.30 * 0.70                  # Gebühr je YES-Set
    nr_size = 50.0 / (0.90 + nr_fees)
    nr_edge = 1.0 - 0.90 - nr_fees

    # Nach dem Merge (Paar -> 1 USDC, YES-Set -> 1 USDC) ist der Gewinn
    # realisiert — nicht nur Buchwert.
    assert pf.realized_pnl > 0
    assert pf.realized_pnl == pytest.approx(comp_size * comp_edge + nr_size * nr_edge)
    assert pf.fees_paid == pytest.approx(comp_size * comp_fees + nr_size * nr_fees)

    # Keine offene Arb-Position übrig: alles gemerged.
    assert pf.positions == {}
    # Portfolio-Gesamtwert über dem Start-Cash (Positionen leer -> Wert = Cash).
    assert pf.value() == pytest.approx(1000.0 + pf.realized_pnl)
    assert pf.value() > 1000.0
    assert pf.cash > 1000.0

    # Keine Order über max_order_usdc (1%-Toleranz wie im RiskManager).
    for f in pf.fills:
        assert f.side == "BUY"
        assert f.price * f.size <= cfg.risk.max_order_usdc * 1.01


class StaleSizedArb(Strategy):
    """Stub: Arb-Signale, deren Größe auf einem veralteten Buch basiert.

    Modelliert das Live-Rennen zwischen Signalerzeugung und Ausführung:
    das NO-Buch ist zwischenzeitlich auf 10 Shares ausgedünnt, das Signal
    verlangt aber noch 40 — die FOK-Semantik des PaperBrokers muss dann
    BEIDE Beine verwerfen (ein halber Arb wäre eine offene Wette).
    """

    name = "stale_arb"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        return [
            Signal(token_id="b-yes", side="BUY", price=0.55, size=40,
                   reason="stale", market_question="Binärmarkt?", group="comp:stale"),
            Signal(token_id="b-no", side="BUY", price=0.40, size=40,
                   reason="stale", market_question="Binärmarkt?", group="comp:stale"),
        ]


def test_e2e_fok_ein_bein_ohne_tiefe_kein_fill():
    # Arbitrage vorhanden (0.55 + 0.40 < 1), aber das NO-Bein hat nur noch
    # 10 Shares Tiefe -> FOK schlägt fehl, KEIN Bein füllt, Cash unverändert.
    cfg = BotConfig()
    pf = Portfolio(cash=1000.0)
    fills = main.tick(cfg, build_arb_snapshot(no_ask_size=10.0), [StaleSizedArb(cfg)],
                      RiskManager(cfg), PaperBroker(cfg), pf)
    assert fills == 0
    assert pf.fills == []
    assert pf.positions == {}
    assert pf.cash == pytest.approx(1000.0)
    assert pf.realized_pnl == pytest.approx(0.0)


# ---- main.tick: Signal-Pipeline und KillSwitch-Propagation -------------------

class SpyBroker:
    def __init__(self):
        self.calls = 0

    def execute(self, signals, books, portfolio, fee_rates=None):
        self.calls += 1
        return 0


def test_tick_killswitch_vor_ausfuehrung_blockt_orders():
    # Mark-to-Market-Verlust über dem Tageslimit: der Kill-Switch feuert VOR
    # der Ausführung — im Breach-Tick darf keine Order mehr rausgehen.
    cfg = BotConfig()
    cfg.risk.daily_loss_limit_usdc = 100.0
    pf = Portfolio(cash=1000.0)  # day_start_value = 1000
    pf.apply_fill(Fill(ts=0.0, token_id="t1", side="BUY", price=0.90, size=500,
                       reason="alt"))
    # Midpoint 0.10 -> Position 450 -> 50 USDC, Tages-PnL -400
    snap = MarketSnapshot(books={
        "t1": OrderBook("t1", bids=[Level(0.09, 10)], asks=[Level(0.11, 10)]),
    })
    broker = SpyBroker()
    with pytest.raises(KillSwitch):
        main.tick(cfg, snap, [], RiskManager(cfg), broker, pf)
    assert broker.calls == 0


class OverpayStrategy(Strategy):
    """Stub: kauft weit über dem Midpoint -> sofortiger Mark-to-Market-Verlust."""

    name = "overpay"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        return [Signal(token_id="t1", side="BUY", price=0.90, size=55,
                       reason="overpay", market_question="t1?")]


def test_tick_killswitch_nach_ausfuehrung():
    # Die zweite Prüfung NACH der Ausführung sieht den frischen Verlust:
    # Kauf 55 @ 0.90, Mark (Midpoint) 0.50 -> -22 USDC > Limit 10.
    cfg = BotConfig()
    cfg.risk.daily_loss_limit_usdc = 10.0
    pf = Portfolio(cash=1000.0)
    snap = MarketSnapshot(books={
        "t1": OrderBook("t1", bids=[Level(0.10, 100)], asks=[Level(0.90, 100)]),
    })
    with pytest.raises(KillSwitch):
        main.tick(cfg, snap, [OverpayStrategy(cfg)], RiskManager(cfg),
                  PaperBroker(), pf)
    # Der Fill selbst ist durchgegangen — erst die Nachprüfung stoppt den Bot.
    assert len(pf.fills) == 1


class OversizedStrategy(Strategy):
    """Stub: Signal über max_order_usdc — muss am RiskManager scheitern."""

    name = "oversized"

    def generate(self, snap: MarketSnapshot) -> list[Signal]:
        return [Signal(token_id="t1", side="BUY", price=0.50, size=1000,
                       reason="zu gross", market_question="t1?")]


def test_tick_risk_manager_stoppt_abgelehnte_signale():
    # Pipeline-Glue: erzeugte, aber abgelehnte Signale erreichen den Broker
    # als leere Liste -> keine Fills, Portfolio unangetastet.
    cfg = BotConfig()  # max_order_usdc = 50 < 500 Notional
    pf = Portfolio(cash=10_000.0)
    snap = MarketSnapshot(books={
        "t1": OrderBook("t1", bids=[Level(0.49, 2000)], asks=[Level(0.50, 2000)]),
    })
    fills = main.tick(cfg, snap, [OversizedStrategy(cfg)], RiskManager(cfg),
                      PaperBroker(cfg), pf)
    assert fills == 0
    assert pf.fills == []
    assert pf.cash == pytest.approx(10_000.0)
