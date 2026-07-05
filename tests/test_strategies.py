import pytest

from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.portfolio import Fill, Portfolio
from polybot.strategies import ComplementArb, MarketMaking, NegRiskArb
from polybot.strategies.base import MarketSnapshot


def mk_market(i: int, neg_risk: bool = False, augmented: bool = False,
              volume_24h: float = 20_000) -> Market:
    return Market(
        condition_id=f"cond{i}", question=f"Frage {i}?", slug=f"frage-{i}",
        yes_token=f"yes{i}", no_token=f"no{i}",
        liquidity=50_000, volume_24h=volume_24h, neg_risk=neg_risk,
        neg_risk_augmented=augmented,
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


# ---- Regressionstests: kategorieabhängige Taker-Gebühren ------------------

def test_complement_arb_nutzt_tokenspezifische_fee_rate():
    # Krypto-Rate 0.07 macht die scheinbare Edge negativ; ohne Gebühren
    # (Geopolitik, Rate 0.00) ist derselbe Preis ein echter Arb.
    m = mk_market(1)
    books = {"yes1": mk_book("yes1", 0.55), "no1": mk_book("no1", 0.44)}
    snap = MarketSnapshot(markets=[m], books=books,
                          fee_rates={"yes1": 0.07, "no1": 0.07})
    assert ComplementArb(BotConfig()).generate(snap) == []
    snap.fee_rates = {"yes1": 0.0, "no1": 0.0}
    assert len(ComplementArb(BotConfig()).generate(snap)) == 2


def test_complement_arb_fallback_fee_rate_ist_maximum():
    # Ohne tokenspezifische Rate muss der konservative Fallback (0.07,
    # Kategorien-Maximum) greifen — nicht ein zu niedriger Pauschalsatz.
    assert BotConfig().risk.taker_fee_rate == pytest.approx(0.07)


def test_negrisk_arb_krypto_fee_verhindert_scheinbaren_arb():
    # Szenario aus dem Fee-Finding: n=5, alle YES-Asks 0.1898 (Kosten 0.949).
    # Mit Krypto-Rate 0.07 ist die echte Edge negativ -> kein Trade;
    # mit Rate 0.05 wäre die (falsche) Edge 0.0125 gewesen.
    markets = [mk_market(i, neg_risk=True) for i in range(5)]
    books = {}
    for i in range(5):
        books[f"yes{i}"] = mk_book(f"yes{i}", 0.1898)
        books[f"no{i}"] = mk_book(f"no{i}", 0.90)   # kein NO-Arb
    fee_rates = {t: 0.07 for t in books}
    snap = MarketSnapshot(negrisk_events={"ev": markets}, books=books, fee_rates=fee_rates)
    assert NegRiskArb(BotConfig()).generate(snap) == []
    snap.fee_rates = {t: 0.05 for t in books}
    assert len(NegRiskArb(BotConfig()).generate(snap)) == 5


# ---- Regressionstest: Größe inkl. Gebühren <= max_order_usdc --------------

def test_complement_arb_groesse_inklusive_gebuehren():
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
    cost = 0.90
    fees = 0.07 * (0.50 * 0.50 + 0.40 * 0.60)
    # realer Cash-Abfluss (Paarkosten + Gebühren) bleibt unter dem Limit
    assert signals[0].size * (cost + fees) <= 10.0 + 1e-6
    assert signals[0].size == pytest.approx(10.0 / (cost + fees))


def test_negrisk_arb_groesse_inklusive_gebuehren():
    cfg = BotConfig()
    cfg.risk.max_order_usdc = 20.0
    markets = [mk_market(i, neg_risk=True) for i in range(3)]
    books = {}
    for i in range(3):
        books[f"yes{i}"] = mk_book(f"yes{i}", 0.30, ask_size=1000)
        books[f"no{i}"] = mk_book(f"no{i}", 0.90)
    snap = MarketSnapshot(negrisk_events={"ev": markets}, books=books)
    yes_signals = [s for s in NegRiskArb(cfg).generate(snap) if s.token_id.startswith("yes")]
    assert yes_signals
    yes_cost = 3 * 0.30
    yes_fees = 0.07 * 3 * 0.30 * 0.70
    assert yes_signals[0].size * (yes_cost + yes_fees) <= 20.0 + 1e-6


# ---- Regressionstest: negRiskAugmented -> YES-Struktur nicht risikofrei ----

def test_negrisk_arb_augmented_event_keine_yes_struktur():
    markets = [mk_market(i, neg_risk=True, augmented=True) for i in range(3)]
    books = {}
    for i in range(3):
        books[f"yes{i}"] = mk_book(f"yes{i}", 0.30)  # Summe 0.90 -> YES-Arb-Optik
        books[f"no{i}"] = mk_book(f"no{i}", 0.55)    # Summe 1.65 < 2 -> echter NO-Arb
    snap = MarketSnapshot(negrisk_events={"ev": markets}, books=books)
    signals = NegRiskArb(BotConfig()).generate(snap)
    # YES-Struktur unterdrückt (Outcomes können nachträglich hinzukommen) ...
    assert [s for s in signals if s.token_id.startswith("yes")] == []
    # ... die NO-Struktur bleibt handelbar (Auszahlung >= n-1 garantiert)
    assert len([s for s in signals if s.token_id.startswith("no")]) == 3


# ---- Regressionstest: keine doppelt verplante Liquidität -------------------

def test_complement_arb_ueberspringt_negrisk_teilmaerkte():
    # Markt ist zugleich NegRisk-Teilmarkt im Snapshot -> complement_arb
    # muss ihn auslassen, sonst verplanen beide Strategien denselben Ask.
    m = mk_market(1, neg_risk=True)
    books = {"yes1": mk_book("yes1", 0.55), "no1": mk_book("no1", 0.40)}
    snap = MarketSnapshot(markets=[m], books=books,
                          negrisk_events={"ev": [m, mk_market(2, neg_risk=True)]})
    assert ComplementArb(BotConfig()).generate(snap) == []
    # ohne NegRisk-Zugehörigkeit wird derselbe Markt gehandelt
    snap.negrisk_events = {}
    assert len(ComplementArb(BotConfig()).generate(snap)) == 2


# ---- Regressionstests: Market Making (Inventar & kein Naked Short) --------

def mm_snapshot(portfolio: Portfolio | None) -> MarketSnapshot:
    # Enger Spread (0.02 = mm_spread-Default): Markt qualifiziert als Kandidat.
    m = mk_market(1)
    book = OrderBook(token_id="yes1",
                     bids=[Level(0.49, 500)], asks=[Level(0.51, 500)])
    return MarketSnapshot(markets=[m], books={"yes1": book}, portfolio=portfolio)


def test_mm_kein_sell_ohne_bestand():
    snap = mm_snapshot(Portfolio())
    signals = MarketMaking(BotConfig()).generate(snap)
    assert [s.side for s in signals] == ["BUY"]  # kein Naked Short


def test_mm_sell_nur_bis_bestand():
    pf = Portfolio()
    pf.apply_fill(Fill(ts=0, token_id="yes1", side="BUY", price=0.50, size=10, reason=""))
    signals = MarketMaking(BotConfig()).generate(mm_snapshot(pf))
    sells = [s for s in signals if s.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].size <= 10 + 1e-9


def test_mm_inventar_limit_stoppt_buy():
    cfg = BotConfig()
    pf = Portfolio()
    # Exposure = 100 USDC = mm_max_inventory_usdc -> kein weiterer BUY
    pf.apply_fill(Fill(ts=0, token_id="yes1", side="BUY", price=0.50,
                       size=cfg.strategy.mm_max_inventory_usdc / 0.50, reason=""))
    signals = MarketMaking(cfg).generate(mm_snapshot(pf))
    assert [s for s in signals if s.side == "BUY"] == []
    # verkaufen darf sie weiterhin (Inventar abbauen)
    assert [s for s in signals if s.side == "SELL"]


def test_mm_quotes_tragen_replace_flag():
    # replace=True signalisiert dem LiveBroker, Alt-Quotes vorher zu canceln
    pf = Portfolio()
    pf.apply_fill(Fill(ts=0, token_id="yes1", side="BUY", price=0.50, size=10, reason=""))
    signals = MarketMaking(BotConfig()).generate(mm_snapshot(pf))
    assert signals and all(s.replace for s in signals)


# ---- Geschärftes Market Making: Marktauswahl & passive Preissetzung --------

def test_mm_quotet_am_touch_ohne_zu_kreuzen():
    # "Join the Touch" (Iteration 05.07.2026: 1 Tick dahinter ergab 3.5h lang
    # null Fills): Bid AUF dem besten Bid, Ask AUF dem besten Ask — vorne in
    # der Schlange, aber weiterhin reine Maker-Orders (bid < ask, kreuzt nie).
    pf = Portfolio()
    pf.apply_fill(Fill(ts=0, token_id="yes1", side="BUY", price=0.50, size=10, reason=""))
    signals = MarketMaking(BotConfig()).generate(mm_snapshot(pf))
    by_side = {s.side: s for s in signals}
    assert by_side["BUY"].price == pytest.approx(0.49)   # = best_bid
    assert by_side["SELL"].price == pytest.approx(0.51)  # = best_ask
    assert by_side["BUY"].price < by_side["SELL"].price  # kreuzt nie


def test_mm_ueberspringt_weiten_spread():
    # Spread 0.04 > mm_spread (0.02): illiquide/unsicher bepreist -> keine Quotes.
    m = mk_market(1)
    book = OrderBook(token_id="yes1",
                     bids=[Level(0.48, 500)], asks=[Level(0.52, 500)])
    snap = MarketSnapshot(markets=[m], books={"yes1": book}, portfolio=Portfolio())
    assert MarketMaking(BotConfig()).generate(snap) == []


def test_mm_quotet_nur_top_n_maerkte_nach_volumen():
    # mm_max_markets deckelt die Kandidaten: nur die volumenstärksten Märkte
    # bekommen Quotes, auch wenn andere Märkte enge Spreads hätten.
    cfg = BotConfig()
    cfg.strategy.mm_max_markets = 1
    markets = [mk_market(1, volume_24h=1_000), mk_market(2, volume_24h=99_000)]
    books = {f"yes{i}": OrderBook(token_id=f"yes{i}",
                                  bids=[Level(0.49, 500)], asks=[Level(0.51, 500)])
             for i in (1, 2)}
    snap = MarketSnapshot(markets=markets, books=books, portfolio=Portfolio())
    signals = MarketMaking(cfg).generate(snap)
    assert signals and {s.token_id for s in signals} == {"yes2"}


def test_mm_max_markets_default_und_yaml_ladbar(tmp_path):
    assert BotConfig().strategy.mm_max_markets == 10
    p = tmp_path / "config.yaml"
    p.write_text("strategy:\n  mm_max_markets: 3\n")
    assert BotConfig.load(p).strategy.mm_max_markets == 3
    p.write_text("strategy:\n  mm_max_markets: 0\n")
    with pytest.raises(SystemExit, match="mm_max_markets"):
        BotConfig.load(p)
