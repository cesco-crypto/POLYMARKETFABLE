"""Tests der reinen Reward-Maker-Shadow-Logik (ohne Netz)."""

from __future__ import annotations

import json

from polybot.data.orderbook import Level, OrderBook
from polybot.reward_maker import (MakerState, RewardMakerShadow, detect_fills,
                                  quote_prices, replay_market, reward_accrual,
                                  round_tick)


def test_quote_prices_within_band_and_tick():
    # mid 0.50, band = 4.5c/100 = 0.045, half_frac 0.8 -> h=0.036, tick 0.01
    bid, ask = quote_prices(0.50, 0.045, 0.01, half_frac=0.8, skew=0.0)
    assert bid == 0.46 and ask == 0.54          # auf Tick gerundet, im Band
    assert bid < 0.50 < ask


def test_quote_prices_skew_shifts_center():
    bid, ask = quote_prices(0.50, 0.045, 0.01, half_frac=0.8, skew=0.02)
    # Zentrum bei 0.52 -> bid 0.48, ask 0.56
    assert bid == 0.48 and ask == 0.56


def test_quote_prices_clamps_extremes():
    bid, ask = quote_prices(0.99, 0.045, 0.01)
    assert bid <= 0.99 and ask <= 0.99 and ask > bid


def test_detect_fills_both_sides():
    # bid 0.46, ask 0.54. best_ask 0.45 <= 0.46 -> Kauf; best_bid 0.55 >= 0.54 -> Verkauf
    fills = detect_fills(0.46, 0.54, 100, best_bid=0.55, best_ask=0.45)
    assert ("buy", 0.46, 100) in fills and ("sell", 0.54, 100) in fills


def test_detect_fills_none_when_market_inside():
    # best_bid 0.50, best_ask 0.51 — kreuzt unsere 0.46/0.54 nicht
    assert detect_fills(0.46, 0.54, 100, best_bid=0.50, best_ask=0.51) == []


def test_reward_accrual_pro_rata():
    # rate 100/Tag, wir 100 notional, Konkurrenz 900 -> Anteil 10%; 1 Tag
    r = reward_accrual(100.0, our_notional=100.0, competing_notional=900.0,
                       dt_s=86400.0)
    assert abs(r - 10.0) < 1e-9
    assert reward_accrual(100, 0, 900, 86400) == 0.0     # keine Order -> 0
    assert reward_accrual(0, 100, 900, 86400) == 0.0     # keine Rate -> 0


def test_maker_state_adverse_selection_pnl():
    st = MakerState()
    st.apply_fill("buy", 0.60, 100)      # kaufe 100 zu 0.60 -> cash -60, inv 100
    # Markt faellt auf 0.40 -> Inventar-Mark 40, PnL = -60 + 40 = -20
    assert abs(st.trading_pnl(0.40) + 20.0) < 1e-9
    st.rewards = 25.0
    assert abs(st.net(0.40) - 5.0) < 1e-9   # Reward 25 ueberkompensiert -20
    assert st.max_abs_inv == 100


def test_maker_state_roundtrip_spread_capture():
    st = MakerState()
    st.apply_fill("buy", 0.48, 100)      # cash -48
    st.apply_fill("sell", 0.52, 100)     # cash +52, inv 0
    assert st.inv == 0
    assert abs(st.trading_pnl(0.50) - 4.0) < 1e-9   # 4 Cent Spread * 100


def test_replay_flat_market_only_rewards():
    # Flacher Markt (0.50 konstant) -> keine Fills, nur Rewards.
    hist = [{"t": i * 86400, "p": 0.50} for i in range(3)]
    r = replay_market(hist, daily_rate=100, band=0.045, competing_notional=900,
                      quote_size=100, half_frac=0.8)
    assert r["fills_up"] == 0 and r["fills_down"] == 0
    assert r["trading_pnl"] == 0.0
    assert r["rewards"] > 0 and r["net"] == r["rewards"]


def test_replay_trend_creates_adverse_selection():
    # Aufwärtstrend 0.30->0.80: up steigt -> DOWN-Gebot füllt wiederholt teuer,
    # DOWN endet wertlos -> stark negativer Trading-PnL (Adverse Selection).
    hist = [{"t": i * 3600, "p": p} for i, p in
            enumerate([0.30, 0.40, 0.50, 0.60, 0.70, 0.80])]
    r = replay_market(hist, daily_rate=0, band=0.045, competing_notional=1000,
                      quote_size=100, half_frac=0.8)
    assert r["fills_down"] > 0          # DOWN wiederholt gekauft
    assert r["trading_pnl"] < 0         # und es lief gegen uns
    assert r["net"] < 0                 # ohne Rewards klar negativ


def test_replay_rewards_can_offset_adverse_selection():
    hist = [{"t": i * 3600, "p": p} for i, p in
            enumerate([0.30, 0.40, 0.50, 0.60, 0.70, 0.80])]
    lo = replay_market(hist, 0, 0.045, 1000, 100, 0.8)["net"]
    hi = replay_market(hist, 100000, 0.045, 1000, 100, 0.8)["net"]
    assert hi > lo                      # hohe Reward-Rate hebt Netto


def test_replay_fill_prob_scales_filled_size():
    hist = [{"t": i * 3600, "p": p} for i, p in
            enumerate([0.30, 0.40, 0.50, 0.60, 0.70, 0.80])]
    full = replay_market(hist, 0, 0.045, 1000, 100, fill_prob=1.0)
    half = replay_market(hist, 0, 0.045, 1000, 100, fill_prob=0.5)
    # halbe Fill-Wahrscheinlichkeit -> halbe gefüllte Grösse (gleiche Fill-Zahl)
    assert half["fills_down"] == full["fills_down"]
    assert abs(half["n_down"] - full["n_down"] / 2) < 1e-9


def test_replay_adverse_ticks_worsen_pnl():
    hist = [{"t": i * 3600, "p": p} for i, p in
            enumerate([0.30, 0.40, 0.50, 0.60, 0.70, 0.80])]
    no_pen = replay_market(hist, 0, 0.045, 1000, 100, tick=0.01, adverse_ticks=0)
    pen = replay_market(hist, 0, 0.045, 1000, 100, tick=0.01, adverse_ticks=1.5)
    assert pen["trading_pnl"] < no_pen["trading_pnl"]   # Aufschlag verteuert Fills


def test_replay_trend_filter_cuts_adverse_fills():
    # Klarer Aufwärtstrend: der Trend-Filter zieht das DOWN-Gebot zurück ->
    # weniger toxische Down-Fills, besserer (weniger negativer) Trading-PnL.
    hist = [{"t": i * 3600, "p": p} for i, p in
            enumerate([0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78])]
    off = replay_market(hist, 0, 0.045, 1000, 100, trend_window=0)
    on = replay_market(hist, 0, 0.045, 1000, 100, trend_window=2, trend_thresh=0.03)
    assert on["fills_down"] < off["fills_down"]
    assert on["trading_pnl"] > off["trading_pnl"]


def test_replay_exit_quotes_execute_sells():
    # Exit-Asks feuern (verkaufen Inventar bei Gegenbewegung) und veraendern
    # das End-Inventar gegenueber kaufen+halten. (Ob das PnL VERBESSERT, ist
    # marktabhaengig und NICHT garantiert — Befund 07.07.: Exits verkaufen oft
    # mit Verlust und brechen profitable Paare auf.)
    hist = [{"t": i * 3600, "p": p} for i, p in enumerate([0.50, 0.44, 0.50])]
    hold = replay_market(hist, 0, 0.045, 1000, 100, exit_quotes=False,
                         fill_prob=1.0, adverse_ticks=0)
    mm = replay_market(hist, 0, 0.045, 1000, 100, exit_quotes=True,
                       fill_prob=1.0, adverse_ticks=0)
    assert hold["sells_up"] == 0 and hold["sells_down"] == 0   # hold verkauft nie
    assert mm["sells_up"] >= 1                                  # Exit verkauft UP
    assert (mm["n_up"], mm["n_down"]) != (hold["n_up"], hold["n_down"])


def test_replay_depth_multiplier_cuts_rewards():
    hist = [{"t": i * 86400, "p": 0.50} for i in range(3)]   # flach, nur Rewards
    lo_mult = replay_market(hist, 100, 0.045, 1000, 100, depth_multiplier=1.0)
    hi_mult = replay_market(hist, 100, 0.045, 1000, 100, depth_multiplier=3.0)
    assert hi_mult["rewards"] < lo_mult["rewards"]      # mehr Konkurrenz -> weniger Reward


def _wl(tmp_path):
    wl = {"markets": [{"label": "T", "slug": "m1", "up_token": "U",
                       "down_token": "D", "daily_rate": 100, "min_size": 100,
                       "max_spread": 4.5, "min_tick": 0.01}]}
    p = tmp_path / "wl.json"
    p.write_text(json.dumps(wl))
    return p


class _Books:
    def __init__(self, book):
        self._book = book

    def get_books(self, tokens):
        return {"U": self._book}


def _book(bid, ask, bsz=1000, asz=1000):
    return OrderBook("U", bids=[Level(bid, bsz)], asks=[Level(ask, asz)])


def test_shadow_tick_fills_and_persists(tmp_path):
    wl = _wl(tmp_path)
    # Tick 1: Markt bei 0.50 -> wir quoten 0.46/0.54, keine Fills.
    sh = RewardMakerShadow(watchlist_path=wl,
                           state_path=tmp_path / "state.json",
                           log_path=tmp_path / "log.jsonl",
                           books=_Books(_book(0.50, 0.51)))
    sh.tick(now=1000.0)
    assert sh.state["m1"].buys == 0
    # Tick 2: best_ask faellt auf 0.45 <= unser bid 0.46 -> Kauf-Fill.
    sh.books = _Books(_book(0.44, 0.45))
    sh.tick(now=1000.0 + 86400.0)        # 1 Tag spaeter -> voller Reward-Anteil
    st = sh.state["m1"]
    assert st.buys == 1
    assert st.rewards > 0                 # pro-rata Reward gutgeschrieben
    # Persistenz: neuer Shadow laedt den Zustand
    sh2 = RewardMakerShadow(watchlist_path=wl, state_path=tmp_path / "state.json",
                            log_path=tmp_path / "log.jsonl",
                            books=_Books(_book(0.50, 0.51)))
    assert sh2.state["m1"].buys == 1
