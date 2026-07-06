"""Tests der reinen Up/Down-Recorder-Logik (ohne Netz/I/O)."""

from __future__ import annotations

import json

from polybot.updown import (WINDOW_S, UpDownRecorder, active_window_starts,
                            aggregate_updown, build_snapshot, median_price,
                            outcome_from_prices, predict_direction, slugs_for,
                            window_start_from_slug)


def test_window_start_from_slug():
    assert window_start_from_slug("btc-updown-5m-1783370400") == 1783370400
    assert window_start_from_slug("eth-updown-5m-1783370700") == 1783370700
    assert window_start_from_slug("some-other-market") is None
    assert window_start_from_slug("btc-updown-5m-notanint") is None


def test_active_window_starts_alignment():
    # 20:42:04 UTC = 1783370524 -> laufendes Fenster startet 20:40 (1783370400)
    starts = active_window_starts(1783370524)
    assert starts == [1783370400, 1783370700]
    assert all(s % WINDOW_S == 0 for s in starts)


def test_slugs_for():
    slugs = slugs_for(["btc", "eth"], [1783370400])
    assert slugs == ["btc-updown-5m-1783370400", "eth-updown-5m-1783370400"]


def test_predict_direction():
    assert predict_direction(101.0, 100.0) == "up"
    assert predict_direction(99.0, 100.0) == "down"
    assert predict_direction(100.0, 100.0) == "flat"
    assert predict_direction(None, 100.0) == "flat"
    assert predict_direction(101.0, None) == "flat"


def test_outcome_from_prices():
    assert outcome_from_prices(["1", "0"]) == "up"
    assert outcome_from_prices(["0", "1"]) == "down"
    assert outcome_from_prices(["0.495", "0.505"]) is None   # noch handelnd
    assert outcome_from_prices(None) is None
    assert outcome_from_prices(["1"]) is None                # Formfehler


def _snap(**kw):
    base = dict(ts=1000.0, asset="btc", slug="btc-updown-5m-800",
                window_start=800, up_token="U", down_token="D",
                up_bid=0.40, up_ask=0.42, down_bid=0.58, down_ask=0.60,
                ref_now=100.5, ref_ts=999.8, ref_start=100.0)
    base.update(kw)
    return build_snapshot(**base)


def test_build_snapshot_fields():
    r = _snap()
    assert r["kind"] == "snapshot"
    assert r["window_end"] == 800 + WINDOW_S
    assert r["seconds_to_close"] == 800 + WINDOW_S - 1000.0
    assert r["up_mid"] == 0.41 and r["down_mid"] == 0.59
    assert r["pred"] == "up"          # ref_now 100.5 > ref_start 100.0
    assert r["ref_stale"] is False    # age 0.2s < 3s
    assert r["ref_age_s"] == 0.2


def test_build_snapshot_stale_ref():
    r = _snap(ref_ts=990.0)           # 10s alt
    assert r["ref_stale"] is True


def test_build_snapshot_missing_ref():
    r = _snap(ref_now=None, ref_ts=None, ref_start=None)
    assert r["ref_stale"] is True     # kein Referenz-Tick = nicht handelbar
    assert r["pred"] == "flat"
    assert r["up_mid"] == 0.41        # Buch bleibt trotzdem messbar


def test_aggregate_edge_positive_when_proxy_right_and_book_lags():
    # Fenster löst UP auf; Proxy sagt korrekt UP; Up-Ask günstig (0.60) 5s
    # vor Schluss -> Kauf zahlt 1 aus, Rendite +0.40.
    rows = [
        {"kind": "resolution", "slug": "s1", "outcome": "up"},
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "up", "up_ask": 0.60, "down_ask": 0.42,
         "up_ask_size": 250.0, "up_mid": 0.61, "down_mid": 0.39},
    ]
    agg = aggregate_updown(rows)
    assert agg["windows_resolved"] == 1
    b = agg["by_offset"][5]
    assert b["n"] == 1
    assert b["proxy_acc"] == 1.0
    assert abs(b["ev"] - 0.40) < 1e-9
    assert b["book_leads_winner"] == 1.0    # up_mid 0.61 > 0.5
    assert b["avg_depth"] == 250.0          # Tiefe der bespielten Seite


def test_aggregate_edge_negative_when_proxy_wrong():
    # Proxy sagt UP, Wahrheit DOWN -> Kauf der Up-Seite (0.55) verfällt, -0.55.
    rows = [
        {"kind": "resolution", "slug": "s1", "outcome": "down"},
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "up", "up_ask": 0.55, "down_ask": 0.47,
         "up_mid": 0.45, "down_mid": 0.55},
    ]
    b = aggregate_updown(rows)["by_offset"][5]
    assert b["proxy_acc"] == 0.0
    assert abs(b["ev"] + 0.55) < 1e-9
    assert b["book_leads_winner"] == 1.0    # down_mid 0.55 > 0.5


def test_aggregate_skips_stale_and_unresolved_and_flat():
    rows = [
        {"kind": "resolution", "slug": "s1", "outcome": "up"},
        # stale -> raus
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": True, "pred": "up", "up_ask": 0.6},
        # flat -> raus
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "flat", "up_ask": 0.6},
        # Fenster ohne Ergebnis -> raus
        {"kind": "snapshot", "slug": "s2", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "up", "up_ask": 0.6},
    ]
    agg = aggregate_updown(rows)
    assert agg["snapshots_used"] == 0
    assert agg["by_offset"] == {}


def test_aggregate_fee_reduces_ev():
    rows = [
        {"kind": "resolution", "slug": "s1", "outcome": "up"},
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "up", "up_ask": 0.50, "down_ask": 0.50,
         "up_mid": 0.51, "down_mid": 0.49},
    ]
    no_fee = aggregate_updown(rows)["by_offset"][5]["ev"]
    with_fee = aggregate_updown(rows, fee_rate=0.07)["by_offset"][5]["ev"]
    assert with_fee < no_fee


class _FakeGamma:
    def __init__(self, markets, resolutions=None):
        self._markets = markets            # slug -> row dict
        self._res = resolutions or {}      # slug -> outcomePrices list

    def _get(self, path, **params):
        slug = params.get("slug")
        base = dict(self._markets.get(slug, {}))
        if not base:
            return []
        # Faithful zu Gamma: das aufgelöste Ergebnis (outcomePrices 1/0)
        # erscheint NUR unter closed=true. Die Default-(open-)Abfrage liefert
        # den offenen Markt OHNE finales Ergebnis. So schlägt die alte
        # Sweep-Variante (ohne closed=true) den Resolution-Test fehl.
        if str(params.get("closed")) == "true":
            if slug not in self._res:
                return []                 # noch nicht geschlossen
            base["outcomePrices"] = json.dumps(self._res[slug])
            return [base]
        return [base]                     # offener Markt, kein finales Ergebnis


class _FakeBooks:
    def __init__(self, books):
        self._books = books

    def get_books(self, tokens):
        return {t: self._books[t] for t in tokens if t in self._books}


class _FakeTicker:
    """Fake-MultiExchangeTicker: liefert immer denselben Median, 1 Quelle."""
    def __init__(self, price):
        self._price = price

    def snapshot(self, asset, now=None):
        return self._price, 1000.0, 1, {"coinbase": self._price}


class _FakeChainlink:
    def __init__(self, price=None):
        self._price = price

    def get_price(self, asset):
        return (self._price, 1000.0) if self._price is not None else (None, None)

    def start(self):
        pass

    def stop(self):
        pass


class _Book:
    class _L:
        def __init__(self, p, s=100.0):
            self.price = p
            self.size = s

    def __init__(self, bid, ask, ask_size=100.0):
        self.best_bid = self._L(bid) if bid is not None else None
        self.best_ask = self._L(ask, ask_size) if ask is not None else None


def test_recorder_tick_writes_snapshot(tmp_path):
    ws = 900
    slug = f"btc-updown-5m-{ws}"
    gamma = _FakeGamma({slug: {"clobTokenIds": json.dumps(["U", "D"])}})
    books = _FakeBooks({"U": _Book(0.40, 0.42), "D": _Book(0.58, 0.60)})
    rec = UpDownRecorder(assets=["btc"], path=tmp_path / "updown.jsonl",
                         gamma=gamma, books=books,
                         ticker=_FakeTicker(101.0),
                         chainlink=_FakeChainlink(100.5))
    # now mitten im Fenster (Ende ws+300); Startpreise werden erfasst.
    n = rec.tick(now=ws + 250)
    assert n == 1
    lines = (tmp_path / "updown.jsonl").read_text().strip().splitlines()
    snap = json.loads(lines[0])
    assert snap["kind"] == "snapshot"
    assert snap["asset"] == "btc" and snap["slug"] == slug
    assert snap["up_ask"] == 0.42
    assert snap["up_ask_size"] == 100.0   # Tiefe am besten Ask mitprotokolliert
    assert snap["ref_start"] == 101.0     # Median am Fensterstart erfasst
    assert snap["ref_sources"] == 1
    assert snap["chain_price"] == 100.5   # Chainlink-on-chain mitprotokolliert
    assert snap["chain_start"] == 100.5   # am Fensterstart erfasst
    assert snap["pred"] == "flat"         # Median 101.0 == ref_start 101.0


def test_recorder_sweeps_resolution(tmp_path):
    ws = 900
    slug = f"btc-updown-5m-{ws}"
    gamma = _FakeGamma({slug: {"clobTokenIds": json.dumps(["U", "D"])}},
                       resolutions={slug: ["1", "0"]})
    books = _FakeBooks({"U": _Book(0.9, 0.95), "D": _Book(0.05, 0.1)})
    rec = UpDownRecorder(assets=["btc"], path=tmp_path / "updown.jsonl",
                         gamma=gamma, books=books, ticker=_FakeTicker(101.0),
                         chainlink=_FakeChainlink())
    rec.tick(now=ws + 100)                # Fenster aktiv, Markt gecacht
    rec.tick(now=ws + WINDOW_S + 10)      # nach Schluss -> Resolution
    rows = [json.loads(l) for l in
            (tmp_path / "updown.jsonl").read_text().strip().splitlines()]
    res = [r for r in rows if r["kind"] == "resolution"]
    assert len(res) == 1
    assert res[0]["outcome"] == "up" and res[0]["slug"] == slug


def test_backfill_resolves_missed_windows(tmp_path):
    # Simuliert einen Neustart: die Datei enthält Snapshots eines längst
    # beendeten Fensters OHNE Resolution-Zeile. backfill + Sweep müssen es
    # nachträglich auflösen (Container-Neustart-Fall).
    ws = 900
    slug = f"btc-updown-5m-{ws}"
    path = tmp_path / "updown.jsonl"
    with path.open("w") as f:
        f.write(json.dumps({"kind": "snapshot", "slug": slug, "asset": "btc",
                            "window_start": ws, "window_end": ws + WINDOW_S,
                            "ref_start": 100.0}) + "\n")
    gamma = _FakeGamma({slug: {"clobTokenIds": json.dumps(["U", "D"])}},
                       resolutions={slug: ["0", "1"]})   # DOWN
    rec = UpDownRecorder(assets=["btc"], path=path, gamma=gamma,
                         books=_FakeBooks({}), ticker=_FakeTicker(100.0),
                         chainlink=_FakeChainlink())
    reactivated = rec.backfill_refs_from_disk()
    assert reactivated == 1
    rec._sweep_resolutions(now=ws + WINDOW_S + 100)
    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    res = [r for r in rows if r["kind"] == "resolution"]
    assert len(res) == 1 and res[0]["outcome"] == "down"


def test_backfill_skips_already_resolved(tmp_path):
    ws = 900
    slug = f"btc-updown-5m-{ws}"
    path = tmp_path / "updown.jsonl"
    with path.open("w") as f:
        f.write(json.dumps({"kind": "snapshot", "slug": slug, "asset": "btc",
                            "window_start": ws}) + "\n")
        f.write(json.dumps({"kind": "resolution", "slug": slug,
                            "outcome": "up"}) + "\n")
    rec = UpDownRecorder(assets=["btc"], path=path,
                         gamma=_FakeGamma({}), books=_FakeBooks({}),
                         ticker=_FakeTicker(100.0), chainlink=_FakeChainlink())
    assert rec.backfill_refs_from_disk() == 0     # bereits aufgelöst -> übersprungen


def test_median_price():
    now = 1000.0
    # 3 frische Quellen -> echter Median
    src = {"coinbase": (100.0, now), "kraken": (102.0, now - 1),
           "binanceus": (101.0, now - 2)}
    med, ts, n = median_price(src, now)
    assert med == 101.0 and n == 3 and ts == now
    # eine Quelle veraltet (>3s) -> fällt raus, Median der übrigen zwei
    src2 = {"coinbase": (100.0, now), "kraken": (200.0, now - 10)}
    med2, _ts2, n2 = median_price(src2, now)
    assert med2 == 100.0 and n2 == 1     # Ausreißer-Feed vergiftet den Median NICHT
    # alle veraltet -> None
    med3, _t, n3 = median_price({"coinbase": (100.0, now - 99)}, now)
    assert med3 is None and n3 == 0


def test_aggregate_chain_agreement():
    # Median sagt UP; Chainlink stimmt in einem Snapshot zu, im anderen nicht.
    rows = [
        {"kind": "resolution", "slug": "s1", "outcome": "up"},
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "up", "chain_pred": "up",
         "up_ask": 0.6, "down_ask": 0.4, "up_mid": 0.55, "down_mid": 0.45},
        {"kind": "snapshot", "slug": "s1", "seconds_to_close": 5.0,
         "ref_stale": False, "pred": "up", "chain_pred": "down",
         "up_ask": 0.6, "down_ask": 0.4, "up_mid": 0.55, "down_mid": 0.45},
    ]
    b = aggregate_updown(rows)["by_offset"][5]
    assert b["chain_agree"] == 0.5        # 1 von 2 stimmt mit Chainlink überein
