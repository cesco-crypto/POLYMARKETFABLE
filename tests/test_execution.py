"""Regressionstests für den LiveBroker: Order-Lifecycle (Cancel vor
Neu-Quoten) und Gruppen-Abbruch, wenn ein Arb-Bein scheitert.

Der echte ClobClient wird durch einen Fake ersetzt — es geht um die
Broker-Logik, nicht um Netzwerk oder Signaturen.
"""

import time

import pytest

from polybot.config import BotConfig
from polybot.execution import LiveBroker, PaperBroker
from polybot.data.orderbook import Level, OrderBook
from polybot.portfolio import Fill, Portfolio, Position
from polybot.strategies.base import Signal


class FakeClobClient:
    def __init__(self, fail_tokens: set[str] | None = None):
        self.fail_tokens = fail_tokens or set()
        self.posted: list[str] = []      # token_ids in Sendereihenfolge
        self.cancelled: list[str] = []   # gecancelte Order-IDs
        self._n = 0

    def get_tick_size(self, token_id: str) -> float:
        return 0.01

    def create_order(self, args, options=None):
        return {"token_id": args.token_id}

    def post_order(self, order, otype):
        self.posted.append(order["token_id"])
        if order["token_id"] in self.fail_tokens:
            return {"success": False, "errorMsg": "not enough balance"}
        self._n += 1
        return {"success": True, "orderID": f"oid{self._n}"}

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)


def make_live_broker(client: FakeClobClient) -> LiveBroker:
    # __init__ umgehen (verlangt Key + Netzwerk); nur die Felder setzen,
    # die execute() braucht.
    broker = LiveBroker.__new__(LiveBroker)
    broker.client = client
    broker._open_orders = {}
    return broker


def mm_quote(side: str, price: float) -> Signal:
    return Signal(token_id="tok", side=side, price=price, size=10,
                  reason="MM", replace=True)


def test_livebroker_cancelt_alte_quotes_vor_neuquote():
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio()
    pf.positions["tok"] = Position(token_id="tok", shares=100, cost_basis=50)

    # Tick 1: Bid+Ask platziert, nichts zu canceln
    broker.execute([mm_quote("BUY", 0.48), mm_quote("SELL", 0.52)], {}, pf)
    assert client.cancelled == []
    assert broker._open_orders["tok"] == ["oid1", "oid2"]

    # Tick 2: Alt-Quotes werden zuerst gecancelt (genau einmal pro Token)
    broker.execute([mm_quote("BUY", 0.47), mm_quote("SELL", 0.51)], {}, pf)
    assert client.cancelled == ["oid1", "oid2"]
    assert broker._open_orders["tok"] == ["oid3", "oid4"]


def test_livebroker_bricht_gruppe_nach_gescheitertem_bein_ab():
    # FOK sichert nur die Einzelorder: scheitert Bein 2, darf Bein 3
    # gar nicht mehr gesendet werden (halber Arb = offene Wette).
    client = FakeClobClient(fail_tokens={"t2"})
    broker = make_live_broker(client)
    group = [
        Signal(token_id="t1", side="BUY", price=0.30, size=10, reason="arb", group="g1"),
        Signal(token_id="t2", side="BUY", price=0.30, size=10, reason="arb", group="g1"),
        Signal(token_id="t3", side="BUY", price=0.30, size=10, reason="arb", group="g1"),
    ]
    fills = broker.execute(group, {}, Portfolio())
    assert client.posted == ["t1", "t2"]  # t3 wurde nicht mehr gesendet
    assert fills == 1


def test_livebroker_gruppenabbruch_stoppt_nicht_andere_gruppen():
    client = FakeClobClient(fail_tokens={"a1"})
    broker = make_live_broker(client)
    signals = [
        Signal(token_id="a1", side="BUY", price=0.30, size=10, reason="arb", group="gA"),
        Signal(token_id="a2", side="BUY", price=0.30, size=10, reason="arb", group="gA"),
        Signal(token_id="b1", side="BUY", price=0.30, size=10, reason="arb", group="gB"),
    ]
    broker.execute(signals, {}, Portfolio())
    assert client.posted == ["a1", "b1"]


def test_paperbroker_fill_nur_gegen_buchliquiditaet():
    # bestehendes Verhalten abgesichert: BUY füllt nur, was im Ask liegt
    broker = PaperBroker()
    pf = Portfolio()
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 7)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(7)


def test_paperbroker_kappt_kauf_am_cash():
    # Regressionstest: Cash konnte unbegrenzt negativ werden (Paper-Margin).
    broker = PaperBroker()
    pf = Portfolio(cash=5.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=100, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)  # 5 USDC / 0.50
    assert pf.cash == pytest.approx(0.0)


def test_paperbroker_kappt_sell_am_bestand():
    # Regressionstest: Oversell erzeugte früher Phantom-Gewinn im Portfolio.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="tok", side="BUY",
                       price=0.50, size=5, reason=""))
    book = OrderBook(token_id="tok", bids=[Level(0.60, 100)], asks=[])
    sig = Signal(token_id="tok", side="SELL", price=0.60, size=50, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions == {}  # nur die 5 gehaltenen Shares verkauft
    assert pf.cash == pytest.approx(100.0 - 2.5 + 3.0)


def test_paperbroker_verbucht_taker_gebuehr():
    # Regressionstest: Paper-Fills sind Taker-Fills, zahlten aber keine Gebühr.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    broker.execute([sig], {"tok": book}, pf, fee_rates={"tok": 0.04})
    # fee = 10 * 0.04 * 0.5 * (1-0.5) = 0.10 USDC
    assert pf.cash == pytest.approx(100.0 - 5.0 - 0.10)
    assert pf.realized_pnl == pytest.approx(-0.10)


def test_paperbroker_fallback_fee_rate_aus_config():
    # Ohne abrufbare Fee-Rate gilt konservativ das konfigurierte Maximum.
    broker = PaperBroker(BotConfig())  # taker_fee_rate=0.07
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    broker.execute([sig], {"tok": book}, pf)
    assert pf.cash == pytest.approx(100.0 - 5.0 - 10 * 0.07 * 0.25)
