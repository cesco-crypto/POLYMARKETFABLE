from polybot.data.fees import FeeRateCache
from polybot.data.gamma import Market


def market(i: int, fee_rate: float | None) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=1_000.0, volume_24h=1_000.0, neg_risk=False,
                  fee_rate=fee_rate)


def test_cache_uebernimmt_rate_fuer_beide_tokens():
    fees = FeeRateCache()
    fees.update_from_markets([market(1, 0.04)])
    assert fees.rates_for(["yes1", "no1"]) == {"yes1": 0.04, "no1": 0.04}


def test_cache_ueberspringt_maerkte_ohne_fee_info():
    # None = unbekannt -> Token fehlt im Ergebnis, Fallback greift downstream;
    # 0.0 dagegen ist eine echte Rate (gebührenfreie Kategorie).
    fees = FeeRateCache()
    fees.update_from_markets([market(1, None), market(2, 0.0)])
    assert fees.rates_for(["yes1", "no1", "yes2", "no2"]) == {"yes2": 0.0, "no2": 0.0}


def test_cache_behaelt_rate_wenn_fee_info_spaeter_fehlt():
    # Über Ticks gecacht: ein Tick ohne Fee-Info darf die bekannte Rate
    # nicht verwerfen.
    fees = FeeRateCache()
    fees.update_from_markets([market(1, 0.07)])
    fees.update_from_markets([market(1, None)])
    assert fees.rates_for(["yes1"]) == {"yes1": 0.07}


def test_cache_aktualisiert_geaenderte_rate():
    fees = FeeRateCache()
    fees.update_from_markets([market(1, 0.05)])
    fees.update_from_markets([market(1, 0.03)])
    assert fees.rates_for(["yes1", "no1"]) == {"yes1": 0.03, "no1": 0.03}


def test_rates_for_liefert_nur_angefragte_tokens():
    fees = FeeRateCache()
    fees.update_from_markets([market(1, 0.04), market(2, 0.05)])
    assert fees.rates_for(["yes1", "unbekannt"]) == {"yes1": 0.04}
