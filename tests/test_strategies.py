from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.strategies import ComplementArb, NegRiskArb
from polybot.strategies.base import MarketSnapshot


def mk_market(i: int, neg_risk: bool = False) -> Market:
    return Market(
        condition_id=f"cond{i}", question=f"Frage {i}?", slug=f"frage-{i}",
        yes_token=f"yes{i}", no_token=f"no{i}",
        liquidity=50_000, volume_24h=20_000, neg_risk=neg_risk,
    )


def mk_book(token: str, ask: float, ask_size: float = 100, bid: float | None = None) -> OrderBook:
    return OrderBook(
        token_id=token,
        bids=[Level(bid, 100)] if bid else [],
        asks=[Level(ask, ask_size)],
    )


def test_complement_arb_findet_unterbewertetes_paar():
    m = mk_market(1)
    snap = MarketSnapshot(
        markets=[m],
        books={"yes1": mk_book("yes1", 0.55), "no1": mk_book("no1", 0.40)},
    )
    signals = ComplementArb(BotConfig()).generate(snap)
    assert len(signals) == 2
    assert {s.token_id for s in signals} == {"yes1", "no1"}
    assert all(s.side == "BUY" for s in signals)
    assert signals[0].group == signals[1].group


def test_complement_arb_beruecksichtigt_taker_gebuehren():
    # Ohne Gebühren wäre Edge = 1 - 0.99 = 0.01 (genau an der Schwelle),
    # mit Taker-Gebühr (rate * p * (1-p) je Bein) fällt sie darunter.
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.05
    m = mk_market(1)
    snap = MarketSnapshot(
        markets=[m],
        books={"yes1": mk_book("yes1", 0.59), "no1": mk_book("no1", 0.40)},
    )
    assert ComplementArb(cfg).generate(snap) == []
    cfg.risk.taker_fee_rate = 0.0
    assert len(ComplementArb(cfg).generate(snap)) == 2


def test_complement_arb_ignoriert_faires_paar():
    m = mk_market(1)
    snap = MarketSnapshot(
        markets=[m],
        books={"yes1": mk_book("yes1", 0.60), "no1": mk_book("no1", 0.41)},
    )
    assert ComplementArb(BotConfig()).generate(snap) == []


def test_complement_arb_respektiert_ordergroesse():
    cfg = BotConfig()
    cfg.risk.max_order_usdc = 10.0
    m = mk_market(1)
    snap = MarketSnapshot(
        markets=[m],
        books={"yes1": mk_book("yes1", 0.50, ask_size=1000),
               "no1": mk_book("no1", 0.40, ask_size=1000)},
    )
    signals = ComplementArb(cfg).generate(snap)
    assert signals
    # Größe ist durch max_order_usdc / Paarpreis begrenzt
    assert signals[0].size <= 10.0 / 0.90 + 1e-6


def test_negrisk_arb_alle_yes_unter_eins():
    markets = [mk_market(i, neg_risk=True) for i in range(3)]
    books = {}
    for i in range(3):
        books[f"yes{i}"] = mk_book(f"yes{i}", 0.30)   # Summe 0.90 < 1.00
        books[f"no{i}"] = mk_book(f"no{i}", 0.75)     # Summe 2.25 > 2 -> kein NO-Arb
    snap = MarketSnapshot(negrisk_events={"event-a": markets}, books=books)
    signals = NegRiskArb(BotConfig()).generate(snap)
    yes_signals = [s for s in signals if s.token_id.startswith("yes")]
    assert len(yes_signals) == 3
    assert all(s.group == yes_signals[0].group for s in yes_signals)


def test_negrisk_arb_unvollstaendiges_event_wird_ausgelassen():
    markets = [mk_market(i, neg_risk=True) for i in range(3)]
    books = {f"yes{i}": mk_book(f"yes{i}", 0.30) for i in range(3)}
    books["no0"] = mk_book("no0", 0.75)  # no1/no2 fehlen -> Event unvollständig
    snap = MarketSnapshot(negrisk_events={"event-a": markets}, books=books)
    assert NegRiskArb(BotConfig()).generate(snap) == []


def test_negrisk_arb_faires_event_kein_signal():
    markets = [mk_market(i, neg_risk=True) for i in range(2)]
    books = {}
    for i in range(2):
        books[f"yes{i}"] = mk_book(f"yes{i}", 0.50)   # Summe exakt 1.00
        books[f"no{i}"] = mk_book(f"no{i}", 0.50)     # Summe exakt 1.00 = n-1
    snap = MarketSnapshot(negrisk_events={"event-a": markets}, books=books)
    assert NegRiskArb(BotConfig()).generate(snap) == []
