"""Tests für gamma._parse_market und die active_markets-Filter."""

import json

import pytest

from polybot.data.gamma import GammaClient, _parse_market


def row(i: int = 0, **overrides) -> dict:
    base = {
        "conditionId": f"cond{i}",
        "question": f"Frage {i}?",
        "slug": f"frage-{i}",
        "clobTokenIds": json.dumps([f"yes{i}", f"no{i}"]),
        "liquidityNum": 50_000,
        "volume24hr": 20_000,
        "negRisk": True,
        "closed": False,
    }
    base.update(overrides)
    return base


def test_parse_market_liest_kernfelder():
    m = _parse_market(row(7, negRiskAugmented=True))
    assert m.condition_id == "cond7"
    assert m.question == "Frage 7?"
    assert m.slug == "frage-7"
    assert (m.yes_token, m.no_token) == ("yes7", "no7")
    assert m.liquidity == pytest.approx(50_000)
    assert m.volume_24h == pytest.approx(20_000)
    assert m.neg_risk is True
    assert m.neg_risk_augmented is True
    assert m.closed is False


def test_parse_market_clob_token_ids_als_native_liste_wird_verworfen():
    # Dokumentierter IST-Zustand (potentieller Produktbug): liefert Gamma
    # clobTokenIds als native JSON-Liste statt als String, wirft json.loads
    # TypeError und der Markt wird STILL zu None — er verschwindet
    # kommentarlos aus dem Universum, obwohl beide Token-IDs vorliegen.
    m = _parse_market(row(clobTokenIds=["yes0", "no0"]))
    assert m is None


@pytest.mark.parametrize("token_ids", [
    "[]",                                # leer
    json.dumps(["nur-eins"]),            # 1 Token
    json.dumps(["a", "b", "c"]),         # 3 Tokens
    None,                                # Feld fehlt / null
    "kein json",                         # unparsebar
])
def test_parse_market_verwirft_ungueltige_token_listen(token_ids):
    assert _parse_market(row(clobTokenIds=token_ids)) is None


def test_parse_market_liquidity_fallback_auf_stringfeld():
    r = row()
    del r["liquidityNum"]
    r["liquidity"] = "123.5"
    assert _parse_market(r).liquidity == pytest.approx(123.5)


def test_parse_market_fehlende_zahlen_werden_null():
    r = row()
    del r["liquidityNum"]
    del r["volume24hr"]
    m = _parse_market(r)
    assert m.liquidity == 0.0
    assert m.volume_24h == 0.0


# ---- active_markets: Liquiditäts- und closed-Filter --------------------------

def test_active_markets_filtert_liquiditaet_und_geschlossene():
    # Der Client filtert defensiv nach: min_liquidity und closed — auch wenn
    # die API eigentlich schon closed=false liefern sollte.
    rows = [
        row(1, liquidityNum=50_000),
        row(2, liquidityNum=10),          # unter min_liquidity
        row(3, closed=True),              # geschlossen
        row(4, clobTokenIds="[]"),        # unparsebar
    ]
    gc = GammaClient()
    gc._get = lambda path, **params: rows  # noqa: SLF001 — Test-Stub
    out = gc.active_markets(min_liquidity=1_000.0)
    assert [m.condition_id for m in out] == ["cond1"]


def test_active_markets_reicht_filterparameter_an_die_api():
    captured: dict = {}

    def fake_get(path, **params):
        captured.update(params, path=path)
        return []

    gc = GammaClient()
    gc._get = fake_get  # noqa: SLF001 — Test-Stub
    gc.active_markets(limit=50)
    assert captured["path"] == "/markets"
    assert captured["active"] == "true"
    assert captured["closed"] == "false"
    assert captured["order"] == "liquidityNum"
    assert captured["limit"] == 50
