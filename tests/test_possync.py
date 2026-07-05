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
    die Geisterposition verzerrt sonst Exposure und Waisen-Erkennung."""
    pf = pf_with(("tok1", 21.87, 0.32))
    assert syncer_with({}).sync(pf) == 1
    assert pf.positions == {}
    assert pf.realized_pnl == 0.0  # externer Verkauf ist kein Bot-PnL


def test_teilverkauf_wird_angepasst():
    pf = pf_with(("tok1", 20.0, 0.5))
    chain = {"tok1": {"size": 8.0, "avg_price": 0.5, "title": "X"}}
    assert syncer_with(chain).sync(pf) == 1
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
