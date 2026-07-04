"""Tests für main.build_snapshot: Volumen-Filter, max_markets, Token-Sammlung."""

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market


def market(i: int, volume_24h: float = 10_000.0, liquidity: float = 50_000.0,
           neg_risk: bool = False) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=liquidity, volume_24h=volume_24h, neg_risk=neg_risk)


class FakeGamma:
    def __init__(self, markets: list[Market], negrisk: dict | None = None):
        self.markets = markets
        self.negrisk = negrisk or {}
        self.market_calls: list[tuple[float, int]] = []

    def active_markets(self, min_liquidity=0.0, limit=500):
        self.market_calls.append((min_liquidity, limit))
        return self.markets

    def negrisk_events(self, min_liquidity=0.0, limit=200):
        return self.negrisk


class FakeBooks:
    def __init__(self):
        self.requested: list[str] = []

    def get_books(self, token_ids):
        self.requested = list(token_ids)
        return {}


def test_build_snapshot_filtert_nach_24h_volumen():
    # Märkte unter min_volume_24h_usdc fliegen raus — Liquidität allein
    # reicht nicht (tote Märkte haben oft hohe Rest-Liquidität).
    cfg = BotConfig()
    cfg.strategy.min_volume_24h_usdc = 5_000.0
    gamma = FakeGamma([market(1, volume_24h=10_000.0),
                       market(2, volume_24h=100.0),
                       market(3, volume_24h=5_000.0)])
    snap = main.build_snapshot(cfg, gamma, FakeBooks())
    assert [m.condition_id for m in snap.markets] == ["c1", "c3"]


def test_build_snapshot_kappt_auf_max_markets():
    # Der Volumen-Filter läuft VOR dem Kappen — deshalb fragt build_snapshot
    # das Doppelte an (limit = 2 * max_markets) und schneidet erst danach.
    cfg = BotConfig()
    cfg.strategy.max_markets = 2
    cfg.strategy.min_volume_24h_usdc = 5_000.0
    gamma = FakeGamma([market(1, volume_24h=100.0),  # fällt dem Filter zum Opfer
                       market(2), market(3), market(4)])
    snap = main.build_snapshot(cfg, gamma, FakeBooks())
    assert [m.condition_id for m in snap.markets] == ["c2", "c3"]
    assert gamma.market_calls == [(cfg.strategy.min_liquidity_usdc, 4)]


def test_build_snapshot_sammelt_tokens_aus_maerkten_und_events():
    # Die Bücher werden für YES- UND NO-Token aller Binärmärkte plus aller
    # NegRisk-Teilmärkte in EINEM Batch angefragt.
    cfg = BotConfig()
    gamma = FakeGamma([market(1)],
                      negrisk={"ev": [market(10, neg_risk=True),
                                      market(11, neg_risk=True)]})
    books = FakeBooks()
    main.build_snapshot(cfg, gamma, books)
    assert set(books.requested) == {"yes1", "no1", "yes10", "no10", "yes11", "no11"}
    # Jedes Token genau einmal (keine doppelten Book-Requests)
    assert len(books.requested) == 6
