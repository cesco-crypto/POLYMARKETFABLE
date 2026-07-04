import json

import pytest
import requests

from polybot.data.gamma import GammaClient, _parse_market


def gamma_market_row(i: int, closed: bool = False, active: bool = True) -> dict:
    return {
        "conditionId": f"cond{i}",
        "question": f"Frage {i}?",
        "slug": f"frage-{i}",
        "clobTokenIds": json.dumps([f"yes{i}", f"no{i}"]),
        "liquidityNum": 50_000,
        "volume24hr": 20_000,
        "negRisk": True,
        "closed": closed,
        "active": active,
    }


def make_client(events: list[dict]) -> GammaClient:
    gc = GammaClient()
    gc._get = lambda path, **params: events  # noqa: SLF001 — Test-Stub
    return gc


def test_negrisk_events_liefert_offenes_event():
    gc = make_client([{
        "negRisk": True, "slug": "ev-a",
        "markets": [gamma_market_row(0), gamma_market_row(1)],
    }])
    result = gc.negrisk_events()
    assert set(result) == {"ev-a"}
    assert len(result["ev-a"]) == 2
    assert all(not m.neg_risk_augmented for m in result["ev-a"])


def test_negrisk_events_ueberspringt_event_mit_geschlossenem_teilmarkt():
    # Regressionstest: Ist ein Teilmarkt geschlossen (z.B. der bekannte
    # Gewinner), ist die Outcome-Menge unvollständig — das Event darf NICHT
    # gefiltert weitergereicht werden, sonst kauft der Bot wertlose YES.
    gc = make_client([{
        "negRisk": True, "slug": "ev-a",
        "markets": [gamma_market_row(0, closed=True),
                    gamma_market_row(1), gamma_market_row(2)],
    }])
    assert gc.negrisk_events() == {}


def test_negrisk_events_ueberspringt_event_mit_unparsebarem_teilmarkt():
    kaputt = gamma_market_row(0)
    kaputt["clobTokenIds"] = "[]"  # kein Token-Paar -> Outcome fehlt
    gc = make_client([{
        "negRisk": True, "slug": "ev-a",
        "markets": [kaputt, gamma_market_row(1), gamma_market_row(2)],
    }])
    assert gc.negrisk_events() == {}


def test_negrisk_events_propagiert_augmented_flag():
    gc = make_client([{
        "negRisk": True, "negRiskAugmented": True, "slug": "ev-a",
        "markets": [gamma_market_row(0), gamma_market_row(1)],
    }])
    result = gc.negrisk_events()
    assert all(m.neg_risk_augmented for m in result["ev-a"])


def test_negrisk_events_filtert_inaktive_platzhalter_maerkte():
    # Regressionstest: nicht deployte Platzhalter-Outcomes (active=false)
    # haben zwar Token-IDs, aber veraltete Preise — sie dürfen nicht in die
    # Summe-1-Betrachtung eingehen und dürfen das Event auch nicht kippen.
    gc = make_client([{
        "negRisk": True, "slug": "ev-a",
        "markets": [gamma_market_row(0), gamma_market_row(1),
                    gamma_market_row(2, active=False)],
    }])
    result = gc.negrisk_events()
    assert {m.condition_id for m in result["ev-a"]} == {"cond0", "cond1"}


def test_negrisk_events_paginiert_ueber_das_server_cap_von_100():
    # Regressionstest: /events cappt limit serverseitig auf 100 — ohne
    # Offset-Pagination fehlen alle Events jenseits der Top-100 kommentarlos.
    def event(i: int) -> dict:
        return {"negRisk": True, "slug": f"ev-{i}",
                "markets": [gamma_market_row(2 * i), gamma_market_row(2 * i + 1)]}

    calls: list[tuple[int, int]] = []

    def fake_get(path, **params):
        calls.append((params["limit"], params["offset"]))
        start = params["offset"]
        n = min(params["limit"], 100)  # Server-Cap
        return [event(i) for i in range(start, min(start + n, 120))]

    gc = GammaClient()
    gc._get = fake_get  # noqa: SLF001 — Test-Stub
    result = gc.negrisk_events(limit=200)
    assert calls == [(100, 0), (100, 100)]
    assert len(result) == 120
    assert "ev-119" in result


def test_active_markets_dedupliziert_bei_seitenueberlappung():
    # Regressionstest: der Feed ist live nach Liquidität sortiert — rutscht
    # ein Markt zwischen zwei Seitenabrufen, taucht er doppelt auf und würde
    # ohne Deduplizierung zweimal verarbeitet (doppelte Orders).
    def market_row(i: int) -> dict:
        row = gamma_market_row(i)
        row["negRisk"] = False
        return row

    def fake_get(path, **params):
        if params["offset"] == 0:
            return [market_row(i) for i in range(100)]
        return [market_row(99), market_row(100)]  # cond99 nochmal auf Seite 2

    gc = GammaClient()
    gc._get = fake_get  # noqa: SLF001 — Test-Stub
    out = gc.active_markets(limit=200)
    ids = [m.condition_id for m in out]
    assert len(ids) == len(set(ids)) == 101


def _liq_row(i: int, liquidity: float) -> dict:
    row = gamma_market_row(i)
    row["negRisk"] = False
    row["liquidityNum"] = liquidity
    return row


def make_windowed_get(rows: list[dict], calls: list[dict]):
    """Simuliert das Live-Verhalten von /markets: max. 100 Zeilen pro Request,
    Offset-Deckel bei 2000, serverseitige liquidity_num_min/max-Filter."""

    def fake_get(path, **params):
        calls.append(dict(params))
        assert params["offset"] <= 2000, "Offset über Server-Deckel -> live HTTP 422"
        subset = [r for r in rows
                  if r["liquidityNum"] >= params.get("liquidity_num_min", 0.0)
                  and r["liquidityNum"] <= params.get("liquidity_num_max", float("inf"))]
        subset.sort(key=lambda r: -r["liquidityNum"])
        off = params["offset"]
        return subset[off: off + min(params["limit"], 100)]

    return fake_get


def test_all_active_markets_paginiert_ueber_den_offset_deckel():
    # Regressionstest: /markets lehnt Offsets über ~2000 mit HTTP 422 ab und
    # /markets/keyset ignoriert seinen Cursor — mehr als 2100 Märkte sind nur
    # über das Liquiditätsfenster (liquidity_num_max) erreichbar.
    rows = [_liq_row(i, 10_000.0 - i) for i in range(2_300)]
    calls: list[dict] = []
    gc = GammaClient()
    gc._get = make_windowed_get(rows, calls)  # noqa: SLF001 — Test-Stub
    out = gc.all_active_markets(min_liquidity=1.0, min_volume=500.0)
    ids = {m.condition_id for m in out}
    assert len(out) == len(ids) == 2_300  # vollständig UND dedupliziert
    assert all(c["offset"] <= 2000 for c in calls)
    # Das zweite Fenster beginnt bei der kleinsten Liquidität des ersten:
    assert any("liquidity_num_max" in c and c["offset"] == 0 for c in calls)
    # Serverseitige Filter (Volumen = sichere Obermenge des 24h-Filters):
    assert all(c["liquidity_num_min"] == 1.0 and c["volume_num_min"] == 500.0
               for c in calls)


def test_all_active_markets_bricht_bei_stagnierendem_fenster_ab():
    # Über 2100 Märkte mit IDENTISCHER Liquidität: das Fenster kann nicht
    # weiterrücken — Abbruch mit Teilergebnis statt Endlosschleife.
    rows = [_liq_row(i, 5_000.0) for i in range(2_200)]
    calls: list[dict] = []
    gc = GammaClient()
    gc._get = make_windowed_get(rows, calls)  # noqa: SLF001 — Test-Stub
    out = gc.all_active_markets()
    assert len(out) == 2_100  # ein voller Offset-Durchlauf (21 Seiten à 100)
    assert len(calls) <= 42  # zwei Fenster-Durchläufe, dann Stopp


# ---- Fee-Rate-Parsing (Quelle: feesEnabled + feeSchedule.rate) -------------

def test_parse_market_liest_fee_rate_aus_fee_schedule():
    row = gamma_market_row(0)
    row["feesEnabled"] = True
    row["feeSchedule"] = {"exponent": 1, "rate": 0.03, "takerOnly": True,
                          "rebateRate": 0.25}
    assert _parse_market(row).fee_rate == pytest.approx(0.03)


def test_parse_market_fees_disabled_bedeutet_rate_null():
    # Gebührenfreie Kategorien (Geopolitik) haben feesEnabled=false und
    # kein feeSchedule — das ist eine echte 0.0, kein "unbekannt".
    row = gamma_market_row(0)
    row["feesEnabled"] = False
    assert _parse_market(row).fee_rate == 0.0


def test_parse_market_ohne_fee_info_liefert_none():
    # None -> Aufrufer fallen auf cfg.risk.taker_fee_rate (Maximum) zurück.
    assert _parse_market(gamma_market_row(0)).fee_rate is None


def test_parse_market_verwirft_unplausible_fee_rate():
    row = gamma_market_row(0)
    row["feesEnabled"] = True
    for raw in ("abc", None, -0.01, 1000):  # 1000 = bps-Rohwert, keine Dezimalrate
        row["feeSchedule"] = {"rate": raw}
        assert _parse_market(row).fee_rate is None


class FakeResponse:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.headers: dict = {}
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def test_get_wiederholt_bei_transienten_fehlern(monkeypatch):
    # Regressionstest: 429/5xx sind transient — _get soll mit Backoff
    # wiederholen statt den ganzen Scan-Durchlauf abbrechen zu lassen.
    monkeypatch.setattr("polybot.data.gamma.time.sleep", lambda s: None)
    session = FakeSession([
        FakeResponse(429),
        FakeResponse(503),
        FakeResponse(200, [{"ok": True}]),
    ])
    gc = GammaClient(session=session)
    assert gc._get("/markets") == [{"ok": True}]  # noqa: SLF001
    assert session.calls == 3


def test_get_gibt_nicht_transiente_fehler_sofort_weiter(monkeypatch):
    monkeypatch.setattr("polybot.data.gamma.time.sleep", lambda s: None)
    session = FakeSession([FakeResponse(404)])
    gc = GammaClient(session=session)
    with pytest.raises(requests.HTTPError):
        gc._get("/markets")  # noqa: SLF001
    assert session.calls == 1
