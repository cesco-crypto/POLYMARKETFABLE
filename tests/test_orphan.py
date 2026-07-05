"""Waisen-Detektor: ungehedgte Einzelbeine erkennen und glattstellen."""

import pytest

from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.orphan import OrphanFlattener
from polybot.portfolio import Fill, Portfolio, RestingOrder
from polybot.strategies.base import MarketSnapshot

T0 = 1_783_000_000.0


def market(i: int, neg_risk: bool = False) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=10_000.0, volume_24h=10_000.0, neg_risk=neg_risk)


def book(token: str, bid: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token_id=token, bids=[Level(bid, size)],
                     asks=[Level(min(bid + 0.02, 0.99), size)])


def cfg(grace: float = 60.0) -> BotConfig:
    c = BotConfig()
    c.risk.flatten_orphan_grace_s = grace
    return c


def portfolio_with(*fills: Fill) -> Portfolio:
    pf = Portfolio(cash=1_000.0)
    for f in fills:
        pf.apply_fill(f)
    return pf


def buy(token: str, shares: float, price: float = 0.5) -> Fill:
    return Fill(ts=T0, token_id=token, side="BUY", price=price, size=shares,
                reason="test")


def snap_of(*markets: Market, books: dict[str, OrderBook] | None = None,
            negrisk: dict[str, list[Market]] | None = None) -> MarketSnapshot:
    return MarketSnapshot(markets=list(markets), books=books or {},
                          negrisk_events=negrisk or {})


def test_vollstaendiges_paar_ist_keine_waise():
    m = market(1)
    fl = OrphanFlattener(cfg())
    pf = portfolio_with(buy("yes1", 10), buy("no1", 10))
    snap = snap_of(m, books={"yes1": book("yes1", 0.5), "no1": book("no1", 0.48)})
    fl.signals(snap, pf, now=T0)
    sigs, _ = fl.signals(snap, pf, now=T0 + 120)
    assert sigs == []


def test_einzelbein_wird_nach_schonfrist_verkauft():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=60))
    pf = portfolio_with(buy("yes1", 10))
    snap = snap_of(m, books={"yes1": book("yes1", 0.53)})
    # Innerhalb der Schonfrist: nichts (Gegenbein könnte noch schweben).
    sigs, _ = fl.signals(snap, pf, now=T0)
    assert sigs == []
    sigs, _ = fl.signals(snap, pf, now=T0 + 61)
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.side, s.token_id, s.price) == ("SELL", "yes1", 0.53)
    assert s.size == pytest.approx(10)
    assert s.replace is True and s.group is None


def test_ueberhang_nur_die_differenz_verkaufen():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=0))
    pf = portfolio_with(buy("yes1", 10), buy("no1", 4))
    snap = snap_of(m, books={"yes1": book("yes1", 0.5)})
    sigs, _ = fl.signals(snap, pf, now=T0 + 1)
    assert len(sigs) == 1
    assert sigs[0].size == pytest.approx(6)


def test_geschlossener_ueberhang_setzt_schonfrist_zurueck():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=60))
    pf = portfolio_with(buy("yes1", 10))
    snap = snap_of(m, books={"yes1": book("yes1", 0.5)})
    fl.signals(snap, pf, now=T0)  # Überhang beginnt zu reifen
    pf.apply_fill(buy("no1", 10))  # Gegenbein füllt doch noch
    sigs, _ = fl.signals(snap, pf, now=T0 + 120)
    assert sigs == [] and "yes1" not in fl.first_seen


def test_negrisk_tokens_werden_nicht_angefasst():
    m = market(1, neg_risk=True)
    fl = OrphanFlattener(cfg(grace=0))
    pf = portfolio_with(buy("no1", 10))
    snap = snap_of(books={"no1": book("no1", 0.5)}, negrisk={"ev": [m]})
    sigs, _ = fl.signals(snap, pf, now=T0 + 999)
    assert sigs == []


def test_token_mit_eigener_ruhender_order_ist_mm_inventar():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=0))
    pf = portfolio_with(buy("yes1", 10))
    pf.resting_orders.append(RestingOrder(ts=T0, token_id="yes1", side="SELL",
                                          price=0.55, size=10, reason="MM Ask"))
    snap = snap_of(m, books={"yes1": book("yes1", 0.5)})
    sigs, _ = fl.signals(snap, pf, now=T0 + 999)
    assert sigs == []


def test_markt_aus_snapshot_verschwunden_paar_bleibt_gelernt():
    """Der wichtigste Fall: In-play-/abgelaufene Märkte fliegen aus dem
    Snapshot — genau dort entstehen die Waisen. Bid kommt dann per
    Batch-Preisabfrage."""

    class FakeBooks:
        def __init__(self):
            self.asked: list[str] = []

        def get_top_prices(self, token_ids):
            self.asked = list(token_ids)
            return {t: (0.44, 0.46) for t in token_ids}

    m = market(1)
    books = FakeBooks()
    fl = OrphanFlattener(cfg(grace=0), books=books)
    fl.observe(snap_of(m))                       # Paar in einem frühen Tick gelernt
    pf = portfolio_with(buy("yes1", 20))
    empty = snap_of()                            # Markt weg, kein Buch mehr
    sigs, extra_books = fl.signals(empty, pf, now=T0 + 1)
    assert books.asked == ["yes1"]
    assert len(sigs) == 1 and sigs[0].price == pytest.approx(0.44)
    # Synthetisches Buch: Preis fürs Live-Signal, Größe 0 (ehrlich).
    assert extra_books["yes1"].best_bid.price == pytest.approx(0.44)
    assert extra_books["yes1"].best_bid.size == 0.0


def test_ohne_bid_wird_nicht_geraten():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=0))          # kein BookClient
    fl.observe(snap_of(m))
    pf = portfolio_with(buy("yes1", 20))
    sigs, extra = fl.signals(snap_of(), pf, now=T0 + 1)
    assert sigs == [] and extra == {}


def test_staub_unter_mindestvolumen_bleibt_liegen():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=0))
    pf = portfolio_with(buy("yes1", 0.01))
    snap = snap_of(m, books={"yes1": book("yes1", 0.5)})
    sigs, _ = fl.signals(snap, pf, now=T0 + 1)
    assert sigs == []


def test_wiederholung_gedrosselt():
    m = market(1)
    fl = OrphanFlattener(cfg(grace=0))
    pf = portfolio_with(buy("yes1", 10))
    snap = snap_of(m, books={"yes1": book("yes1", 0.5)})
    sigs1, _ = fl.signals(snap, pf, now=T0 + 1)
    sigs2, _ = fl.signals(snap, pf, now=T0 + 2)     # 0.5s-Stream-Tick
    sigs3, _ = fl.signals(snap, pf, now=T0 + 1 + fl.REPEAT_S + 1)
    assert len(sigs1) == 1 and sigs2 == [] and len(sigs3) == 1


def test_unbekannter_token_bleibt_unangetastet():
    fl = OrphanFlattener(cfg(grace=0))
    pf = portfolio_with(buy("fremd", 10))
    sigs, _ = fl.signals(snap_of(), pf, now=T0 + 999)
    assert sigs == []


def test_config_verbietet_kombination_mit_market_making(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  flatten_orphan_grace_s: 60\n"
                 "strategy:\n  enabled: [complement_arb, market_making]\n")
    with pytest.raises(SystemExit, match="market_making"):
        BotConfig.load(p)


def test_tick_stellt_waise_im_paper_broker_glatt():
    """Integration: tick() mit Flattener verkauft das Einzelbein real."""
    from polybot.execution import PaperBroker
    from polybot.main import tick
    from polybot.risk import RiskManager

    c = cfg(grace=0)
    c.strategy.paper_fill_delay_ticks = 0  # Sofort-Fill: testet die Verdrahtung
    m = market(1)
    pf = portfolio_with(buy("yes1", 10, price=0.5))
    snap = snap_of(m, books={"yes1": book("yes1", 0.52),
                             "no1": book("no1", 0.46)})
    fl = OrphanFlattener(c)
    fills = tick(c, snap, strategies=[], risk=RiskManager(c),
                 broker=PaperBroker(c), portfolio=pf, flattener=fl)
    assert fills >= 1
    assert "yes1" not in pf.positions  # Bein verkauft, kein Bestand mehr
