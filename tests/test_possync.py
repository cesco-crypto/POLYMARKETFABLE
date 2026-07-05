"""Positions-Sync: Buchhaltung an die Chain-Wahrheit angleichen (Live-Start)."""

import pytest

from polybot.portfolio import Fill, Portfolio
from polybot.possync import PositionSyncer

T0 = 1_783_000_000.0


def pf_with(*positions: tuple[str, float, float]) -> Portfolio:
    pf = Portfolio(cash=1_000.0)
    for token, shares, price in positions:
        pf.apply_fill(Fill(ts=T0, token_id=token, side="BUY",
                           price=price, size=shares, reason="test"))
    return pf


def syncer_with(chain: dict | None) -> PositionSyncer:
    s = PositionSyncer("0xWallet")
    s.fetch_chain_positions = lambda: chain
    return s


def test_downtime_fill_wird_nachgetragen():
    """Order matchte zwischen Crash und Neustart: Chain hat die Position,
    die Buchhaltung nicht — der Sync trägt sie nach (ohne PnL-Buchung)."""
    pf = pf_with()
    chain = {"tok1": {"size": 20.0, "avg_price": 0.48, "title": "BTC Up"}}
    assert syncer_with(chain).sync(pf) == 1
    pos = pf.positions["tok1"]
    assert pos.shares == pytest.approx(20.0)
    assert pos.cost_basis == pytest.approx(9.6)
    assert pf.realized_pnl == 0.0 and pf.cash == pytest.approx(1_000.0)


def test_manuell_verkaufte_position_wird_ausgetragen():
    """Betreiber hat im Browser verkauft (05.07. dreimal passiert):
    die Geisterposition verzerrt sonst Exposure und Waisen-Erkennung.
    (Chain nicht komplett leer — sonst greift der Adress-Guard.)"""
    pf = pf_with(("tok1", 21.87, 0.32), ("tok2", 5.0, 0.5))
    chain = {"tok2": {"size": 5.0, "avg_price": 0.5, "title": "Y"}}
    assert syncer_with(chain).sync(pf, now=T0 + 2_000) == 1
    assert "tok1" not in pf.positions and "tok2" in pf.positions
    assert pf.realized_pnl == 0.0  # externer Verkauf ist kein Bot-PnL


def test_teilverkauf_wird_angepasst():
    pf = pf_with(("tok1", 20.0, 0.5))
    chain = {"tok1": {"size": 8.0, "avg_price": 0.5, "title": "X"}}
    assert syncer_with(chain).sync(pf, now=T0 + 2_000) == 1
    assert pf.positions["tok1"].shares == pytest.approx(8.0)


def test_uebereinstimmung_keine_korrektur():
    pf = pf_with(("tok1", 20.0, 0.5))
    chain = {"tok1": {"size": 20.0, "avg_price": 0.5, "title": "X"}}
    assert syncer_with(chain).sync(pf) == 0
    assert pf.positions["tok1"].cost_basis == pytest.approx(10.0)  # unangetastet


def test_api_fehler_traegt_nichts_aus():
    """None (API down) ist von {} (wirklich leer) zu unterscheiden — sonst
    würde ein API-Ausfall alle Positionen löschen."""
    pf = pf_with(("tok1", 20.0, 0.5))
    assert syncer_with(None).sync(pf) == 0
    assert "tok1" in pf.positions


def test_fetch_ueberlebt_kaputte_antwort():
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "kein Array"}

    class FakeHttp:
        headers = {}

        def get(self, url, params=None, timeout=None):
            return FakeResp()

    s = PositionSyncer("0xWallet", session=FakeHttp())
    assert s.fetch_chain_positions() is None


# ---- Schutzregeln der Verifikations-Flotte (Befunde 2/16/18/19/20) -----------


def test_leere_chain_antwort_loescht_nichts():
    """Befund 19: falsche Wallet-Adresse liefert gültiges [] — ohne Guard
    wäre das ganze Portfolio weg."""
    pf = pf_with(("tok1", 20.0, 0.5))
    assert syncer_with({}).sync(pf, now=T0 + 2_000) == 0
    assert "tok1" in pf.positions


def test_frischer_fill_wird_nicht_reduziert():
    """Befunde 16/26: Data-API-Lag — frisch gebuchte Fills fehlen dort noch."""
    pf = pf_with(("tok1", 20.0, 0.5), ("tok2", 5.0, 0.5))
    chain = {"tok2": {"size": 5.0, "avg_price": 0.5, "title": "Y"}}
    # now nur 60s nach dem Fill: tok1 ist in der Schonfrist.
    assert syncer_with(chain).sync(pf, now=T0 + 60) == 0
    assert "tok1" in pf.positions


def test_gesettelte_position_wird_nicht_wieder_eingetragen():
    """Befunde 2/20: Sweeper hat ausgezahlt, Redeem on-chain noch offen —
    die Chain zeigt den Token weiter. Ohne Registry käme die Position
    zurück und der Sweeper zahlte nach jedem Neustart erneut."""
    pf = pf_with()
    pf.settled["tok1"] = T0
    chain = {"tok1": {"size": 20.0, "avg_price": 0.5, "title": "X"}}
    assert syncer_with(chain).sync(pf, now=T0 + 2_000) == 0
    assert pf.positions == {}
    # Redeem durch (Chain führt Token nicht mehr) -> Registry-Eintrag frei.
    syncer_with({"andere": {"size": 1.0, "avg_price": 0.5, "title": "Z"}}
                ).sync(pf, now=T0 + 3_000)
    assert "tok1" not in pf.settled


def test_korrekturen_verschieben_day_start_kill_switch_neutral():
    """Befunde 18/35: externe Eingriffe dürfen den Tages-PnL (Kill-Switch-
    Basis) weder auslösen noch aufblasen."""
    pf = pf_with(("tok1", 20.0, 0.5), ("tok2", 5.0, 0.5))
    pnl_before = pf.daily_pnl()
    chain = {"tok2": {"size": 5.0, "avg_price": 0.5, "title": "Y"}}
    syncer_with(chain).sync(pf, now=T0 + 2_000)   # tok1 extern verkauft
    assert pf.daily_pnl() == pytest.approx(pnl_before)


def test_periodic_ist_gedrosselt():
    calls = []
    pf = pf_with()
    s = syncer_with({})
    orig = s.sync
    s.sync = lambda p, now=None: calls.append(now) or 0
    s.periodic(pf, now=T0)
    s.periodic(pf, now=T0 + 10)
    assert len(calls) == 1
    s.periodic(pf, now=T0 + s.SYNC_INTERVAL_S + 1)
    assert len(calls) == 2
