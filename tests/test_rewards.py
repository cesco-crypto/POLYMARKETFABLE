"""Tests der reinen Reward-Scanner-Logik (ohne Netz)."""

from __future__ import annotations

from polybot.data.orderbook import Level, OrderBook
from polybot.rewards import (USDC, RewardMarket, daily_rate, parse_reward_market,
                            qualifying_depth, rank_markets)


def _sm(**kw):
    base = {
        "condition_id": "0xabc", "market_slug": "m", "question": "Q?",
        "closed": False, "enable_order_book": True, "accepting_orders": True,
        "minimum_tick_size": 0.01, "end_date_iso": "2027-01-01T00:00:00Z",
        "tokens": [{"token_id": "U", "outcome": "Yes", "price": 0.4},
                   {"token_id": "D", "outcome": "No", "price": 0.6}],
        "rewards": {"rates": [{"asset_address": USDC, "rewards_daily_rate": 10}],
                    "min_size": 30, "max_spread": 3.0},
    }
    base.update(kw)
    return base


def test_daily_rate_sums_usdc_only():
    m = _sm(rewards={"rates": [{"asset_address": USDC, "rewards_daily_rate": 10},
                               {"asset_address": "0xother", "rewards_daily_rate": 99}],
                     "min_size": 30, "max_spread": 3})
    assert daily_rate(m) == 10       # Fremd-Asset zählt nicht


def test_parse_reward_market_ok():
    rm = parse_reward_market(_sm())
    assert rm is not None
    assert rm.up_token == "U" and rm.down_token == "D"
    assert rm.daily_rate == 10 and rm.min_size == 30 and rm.max_spread == 3.0


def test_parse_rejects_closed_and_zero_rate():
    assert parse_reward_market(_sm(closed=True)) is None
    assert parse_reward_market(_sm(rewards={"rates": [], "min_size": 30,
                                            "max_spread": 3})) is None


def test_tradeable_filters():
    now = 1_000_000.0
    rm = parse_reward_market(_sm())
    rm.end_ts = now + 7200
    assert rm.tradeable(now, 3600) is True
    # zu nah am Ende
    rm.end_ts = now + 600
    assert rm.tradeable(now, 3600) is False
    # nicht accepting
    rm.end_ts = now + 7200
    rm.accepting = False
    assert rm.tradeable(now, 3600) is False


def test_tradeable_inplay_excluded():
    now = 1_000_000.0
    rm = parse_reward_market(_sm())
    rm.end_ts = now + 7200
    rm.seconds_delay = 5
    rm.game_start_ts = now - 10        # Spiel läuft
    assert rm.inplay(now) is True
    assert rm.tradeable(now, 3600) is False


def test_qualifying_depth_within_band():
    # mid 0.40, band 3c -> [0.37, 0.43]. Nur Levels im Band zählen.
    book = OrderBook("U",
                     bids=[Level(0.39, 100), Level(0.36, 100)],   # 0.36 raus
                     asks=[Level(0.41, 100), Level(0.44, 100)])   # 0.44 raus
    d = qualifying_depth(book, 0.40, 3.0)
    assert abs(d - (0.39 * 100 + 0.41 * 100)) < 1e-6


def test_rank_empty_band_not_a_mirage():
    # Leeres Band (keine Bücher) -> Yield wird durch min_capital gedeckelt,
    # täuscht keinen absurden Wert vor.
    rm = parse_reward_market(_sm(rewards={"rates": [{"asset_address": USDC,
                                 "rewards_daily_rate": 1000}], "min_size": 30,
                                 "max_spread": 3}))
    ranked = rank_markets([rm], books={}, min_capital=100.0)
    assert len(ranked) == 1
    # 1000/Tag auf leerem Band -> 100*1000/100 = 1000%/Tag Boden, NICHT unendlich
    assert ranked[0].yield_pct_day == 1000.0
    assert ranked[0].depth_notional == 0.0


def test_rank_orders_by_yield():
    a = parse_reward_market(_sm(condition_id="A", rewards={"rates": [
        {"asset_address": USDC, "rewards_daily_rate": 10}], "min_size": 30,
        "max_spread": 3}))
    b = parse_reward_market(_sm(condition_id="B", rewards={"rates": [
        {"asset_address": USDC, "rewards_daily_rate": 50}], "min_size": 30,
        "max_spread": 3}))
    ranked = rank_markets([a, b], books={}, min_capital=100.0)
    assert ranked[0].rm.condition_id == "B"    # höhere Rate -> höherer Yield
