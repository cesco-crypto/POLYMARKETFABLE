"""Regressionstests: Endzeit-Filter gegen Phantom-Arbitragen.

Trade-Print-Validierung vom 04.07.2026: Der Bot handelte im Paper-Modus
Märkte, deren endDate bereits überschritten war (abgelaufene 15-Min-Krypto-
Fenster, beendetes Paraguay-Spiel) — die Bücher waren stale, real war dort
nichts handelbar. Diese Tests sichern den Filter ab.
"""

import time

from polybot.data.gamma import Market, _parse_end_ts, _parse_market


def mk(end_ts=None, **kw):
    defaults = dict(condition_id="c", question="?", slug="s",
                    yes_token="y", no_token="n",
                    liquidity=10_000, volume_24h=10_000, neg_risk=False)
    defaults.update(kw)
    return Market(end_ts=end_ts, **defaults)


def test_parse_end_ts_iso_mit_z():
    assert _parse_end_ts({"endDate": "2026-07-04T21:00:00Z"}) == 1783198800.0


def test_parse_end_ts_fehlend_oder_kaputt():
    assert _parse_end_ts({}) is None
    assert _parse_end_ts({"endDate": "quatsch"}) is None


def test_parse_market_uebernimmt_end_ts():
    m = _parse_market({
        "conditionId": "c", "question": "?", "slug": "s",
        "clobTokenIds": '["y", "n"]',
        "liquidityNum": 1, "endDate": "2026-07-04T21:00:00Z",
    })
    assert m is not None and m.end_ts == 1783198800.0


def test_abgelaufener_markt_ist_nicht_handelbar():
    # Paraguay-Szenario: endDate 94 Minuten in der Vergangenheit,
    # closed noch False, Buch stale.
    now = time.time()
    assert mk(end_ts=now - 94 * 60).tradeable(120.0) is False


def test_markt_kurz_vor_ende_ist_nicht_handelbar():
    now = time.time()
    assert mk(end_ts=now + 60).tradeable(120.0, now=now) is False


def test_laufender_markt_ist_handelbar():
    now = time.time()
    assert mk(end_ts=now + 3600).tradeable(120.0, now=now) is True


def test_ohne_end_date_bleibt_handelbar():
    assert mk(end_ts=None).tradeable(120.0) is True
