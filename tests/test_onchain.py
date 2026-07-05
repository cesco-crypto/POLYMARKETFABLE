"""Tests für den On-Chain-Merge im Live-Modus (polybot/onchain.py).

web3 wird KOMPLETT gemockt — kein Netz, keine Chain: geprüft werden die
Call-Parameter (Contract, Argumente, Einheiten), das Gas-Handling und die
Fehlerpfade (MergeExecutor wirft nie in den Bot-Loop). Die Integration
(main.live_merge_positions / tick) läuft gegen einen FakeMerger: gebucht
wird nur, was "on-chain" bestätigt wurde, und nie unvollständige Paare.
"""

from unittest.mock import MagicMock

import pytest
from web3 import Web3

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.data.orderbook import Level, OrderBook
from polybot.execution import PaperBroker
from polybot.onchain import (BINARY_PARTITION, GAS_LIMIT_BUFFER,
                             PARENT_COLLECTION_ID, PUSD_ADDRESS, MergeExecutor)
from polybot.portfolio import Fill, Portfolio
from polybot.risk import RiskManager
from polybot.strategies import REGISTRY
from polybot.strategies.base import MarketSnapshot

# Beliebiger gültiger secp256k1-Key — es wird nichts gesendet.
TEST_KEY = "0x" + "11" * 32
ZERO_ADDR = "0x" + "00" * 20
CID = "0x" + "ab" * 32  # gültige conditionId (32 Bytes hex)
CID_BYTES = bytes.fromhex("ab" * 32)


def make_w3(gas_price=30_000_000_000, balance=10**18, estimate=120_000,
            status=1):
    """MagicMock-web3: konfigurierbare Antworten, jede contract()-Instanz frisch."""
    w3 = MagicMock()
    w3.eth.contract.side_effect = lambda address, abi: MagicMock(address=address)
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.gas_price = gas_price
    w3.eth.estimate_gas.return_value = estimate
    w3.eth.get_balance.return_value = balance
    w3.eth.send_raw_transaction.return_value = b"\x12" * 32
    w3.eth.wait_for_transaction_receipt.return_value = {"status": status}
    return w3


def wire(contract_fn) -> None:
    """build_transaction des Funktions-Mocks liefert ein signierbares Tx-Dict."""
    contract_fn.return_value.build_transaction.side_effect = (
        lambda params: {**params, "to": ZERO_ADDR, "value": 0, "data": "0x"})


def make_executor(w3=None, approved=True) -> MergeExecutor:
    ex = MergeExecutor(TEST_KEY, w3=w3 if w3 is not None else make_w3())
    ex.ctf.functions.isApprovedForAll.return_value.call.return_value = approved
    for fn in (ex.ctf.functions.mergePositions,
               ex.ctf.functions.setApprovalForAll,
               ex.adapter.functions.mergePositions,
               ex.adapter.functions.convertPositions):
        wire(fn)
    return ex


# ---- Konstruktor -------------------------------------------------------------

def test_konstruktor_wirft_ohne_private_key():
    # Live-only-Komponente: ohne Key ist die Instanz wertlos -> sofort scheitern.
    with pytest.raises(ValueError):
        MergeExecutor(None, w3=make_w3())
    with pytest.raises(ValueError):
        MergeExecutor("", w3=make_w3())


# ---- merge_pairs (Binärmarkt via ConditionalTokens) ---------------------------

def test_merge_pairs_baut_korrekten_ctf_call():
    w3 = make_w3()
    ex = make_executor(w3)
    assert ex.merge_pairs(CID, 12.5) is True
    # pUSD-Collateral, Root-Collection, Partition [1,2], 6 Dezimalstellen.
    ex.ctf.functions.mergePositions.assert_called_once_with(
        Web3.to_checksum_address(PUSD_ADDRESS), PARENT_COLLECTION_ID,
        CID_BYTES, BINARY_PARTITION, 12_500_000)
    w3.eth.send_raw_transaction.assert_called_once()
    # Kein Approval nötig: der CTF verbrennt eigene Tokens des Senders.
    ex.ctf.functions.isApprovedForAll.assert_not_called()


def test_merge_pairs_signiert_mit_gas_puffer_und_nonce():
    w3 = make_w3(estimate=120_000)
    ex = make_executor(w3)
    assert ex.merge_pairs(CID, 1.0) is True
    tx = w3.eth.estimate_gas.call_args[0][0]
    assert tx["nonce"] == 7
    assert tx["chainId"] == 137
    assert tx["gasPrice"] == 30_000_000_000
    assert tx["from"] == ex.account.address
    w3.eth.wait_for_transaction_receipt.assert_called_once()


def test_merge_pairs_ungueltige_condition_id_ohne_call():
    ex = make_executor()
    assert ex.merge_pairs("c1", 10.0) is False        # kein Hex / zu kurz
    assert ex.merge_pairs("0x1234", 10.0) is False    # keine 32 Bytes
    ex.ctf.functions.mergePositions.assert_not_called()


def test_merge_pairs_amount_null_oder_negativ_ohne_call():
    ex = make_executor()
    assert ex.merge_pairs(CID, 0.0) is False
    assert ex.merge_pairs(CID, -5.0) is False
    ex.ctf.functions.mergePositions.assert_not_called()


def test_units_werden_abgerundet():
    # Nie mehr mergen, als on-chain sicher vorhanden ist (Float-Rauschen).
    assert MergeExecutor._to_units(19.9999999) == 19_999_999
    assert MergeExecutor._to_units(20.0) == 20_000_000
    assert MergeExecutor._to_units(0.0000009) == 0
    assert MergeExecutor._to_units(float("nan")) == 0


# ---- Gas-Handling & Fehlerpfade (niemals raisen) ------------------------------

def test_merge_ohne_pol_guthaben_sendet_nicht():
    # 120k Gas * 1.25 Puffer * 30 gwei = 4.5e15 wei — Balance knapp darunter.
    w3 = make_w3(balance=int(120_000 * GAS_LIMIT_BUFFER * 30_000_000_000) - 1)
    ex = make_executor(w3)
    assert ex.merge_pairs(CID, 10.0) is False
    w3.eth.send_raw_transaction.assert_not_called()


def test_merge_revert_bei_gas_schaetzung_liefert_false():
    w3 = make_w3()
    w3.eth.estimate_gas.side_effect = RuntimeError("execution reverted")
    ex = make_executor(w3)
    assert ex.merge_pairs(CID, 10.0) is False
    w3.eth.send_raw_transaction.assert_not_called()


def test_merge_receipt_status_0_liefert_false():
    ex = make_executor(make_w3(status=0))
    assert ex.merge_pairs(CID, 10.0) is False


def test_merge_rpc_fehler_raist_nie():
    w3 = make_w3()
    w3.eth.get_transaction_count.side_effect = ConnectionError("RPC down")
    ex = make_executor(w3)
    assert ex.merge_pairs(CID, 10.0) is False
    assert ex.merge_negrisk(CID, 10.0) is False


def test_merge_receipt_timeout_liefert_false():
    w3 = make_w3()
    w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("120s")
    ex = make_executor(w3)
    assert ex.merge_pairs(CID, 10.0) is False


# ---- merge_negrisk (Teilmarkt-Paar via NegRisk Adapter) -----------------------

def test_merge_negrisk_nutzt_adapter_mit_bestehender_freigabe():
    ex = make_executor(approved=True)
    assert ex.merge_negrisk(CID, 7.25) is True
    ex.adapter.functions.mergePositions.assert_called_once_with(
        CID_BYTES, 7_250_000)
    ex.ctf.functions.setApprovalForAll.assert_not_called()
    # Binär-Pfad (CTF direkt) bleibt unberührt.
    ex.ctf.functions.mergePositions.assert_not_called()


def test_merge_negrisk_setzt_fehlende_freigabe_genau_einmal():
    w3 = make_w3()
    ex = make_executor(w3, approved=False)
    assert ex.merge_negrisk(CID, 1.0) is True
    ex.ctf.functions.setApprovalForAll.assert_called_once_with(
        ex.adapter_address, True)
    # Approval-Tx + Merge-Tx
    assert w3.eth.send_raw_transaction.call_count == 2
    # Zweiter Merge: Freigabe ist gecacht, kein weiterer Approval-Call.
    assert ex.merge_negrisk(CID, 1.0) is True
    ex.ctf.functions.setApprovalForAll.assert_called_once()


def test_merge_negrisk_ohne_pruefbare_freigabe_liefert_false():
    ex = make_executor()
    ex.ctf.functions.isApprovedForAll.return_value.call.side_effect = (
        ConnectionError("RPC down"))
    assert ex.merge_negrisk(CID, 1.0) is False
    ex.adapter.functions.mergePositions.assert_not_called()


# ---- merge_negrisk_no (NO-Satz via convertPositions) --------------------------

def qid(index: int, prefix: str = "ab" * 31) -> str:
    """questionId: 31 Bytes marketId-Präfix + 1 Byte Frage-Index."""
    return "0x" + prefix + f"{index:02x}"


def test_merge_negrisk_no_baut_market_id_und_bitmaske():
    ex = make_executor()
    assert ex.merge_negrisk_no([qid(0), qid(1), qid(2)], 10.0) is True
    ex.adapter.functions.convertPositions.assert_called_once_with(
        bytes.fromhex("ab" * 31) + b"\x00", 0b111, 10_000_000)


def test_merge_negrisk_no_nicht_zusammengehoerige_fragen_ohne_call():
    ex = make_executor()
    assert ex.merge_negrisk_no([qid(0), qid(1, prefix="cd" * 31)], 10.0) is False
    ex.adapter.functions.convertPositions.assert_not_called()


def test_merge_negrisk_no_doppelte_indizes_ohne_call():
    ex = make_executor()
    assert ex.merge_negrisk_no([qid(1), qid(1)], 10.0) is False
    ex.adapter.functions.convertPositions.assert_not_called()


def test_merge_negrisk_no_braucht_mindestens_zwei_fragen():
    ex = make_executor()
    assert ex.merge_negrisk_no([qid(0)], 10.0) is False
    assert ex.merge_negrisk_no([], 10.0) is False
    ex.adapter.functions.convertPositions.assert_not_called()


def test_merge_negrisk_no_ungueltige_question_id_ohne_call():
    ex = make_executor()
    assert ex.merge_negrisk_no([qid(0), "kaputt"], 10.0) is False
    ex.adapter.functions.convertPositions.assert_not_called()


# ---- Integration: main.live_merge_positions -----------------------------------

class FakeMerger:
    """MergeExecutor-Ersatz: protokolliert Calls, Bestätigung konfigurierbar."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple] = []

    def merge_pairs(self, condition_id, amount):
        self.calls.append(("pairs", condition_id, amount))
        return self.ok

    def merge_negrisk(self, condition_id, amount):
        self.calls.append(("negrisk", condition_id, amount))
        return self.ok

    def merge_negrisk_no(self, question_ids, amount):
        self.calls.append(("negrisk_no", tuple(question_ids), amount))
        return self.ok


class FakeLedger:
    def __init__(self):
        self.merges: list[tuple] = []

    def record_merge(self, market, kind, sets, pnl):
        self.merges.append((market, kind, sets, pnl))


def buy(pf: Portfolio, token: str, price: float, size: float) -> None:
    pf.apply_fill(Fill(ts=0.0, token_id=token, side="BUY",
                       price=price, size=size, reason="test"))


def mk_market(i: int = 0, negrisk: bool = False, question_id: str = "") -> Market:
    return Market(condition_id=f"c{i}", question=f"F{i}?", slug=f"f{i}",
                  yes_token=f"y{i}", no_token=f"n{i}", liquidity=1_000.0,
                  volume_24h=1_000.0, neg_risk=negrisk, question_id=question_id)


def test_live_merge_bucht_nur_onchain_bestaetigtes():
    snap = MarketSnapshot(markets=[mk_market(1)])
    pf = Portfolio(cash=100.0)
    buy(pf, "y1", 0.55, 20)
    buy(pf, "n1", 0.40, 20)
    merger, ledger = FakeMerger(ok=True), FakeLedger()
    assert main.live_merge_positions(snap, pf, merger, ledger) == pytest.approx(20)
    assert merger.calls == [("pairs", "c1", pytest.approx(20.0))]
    assert pf.positions == {}
    assert pf.cash == pytest.approx(100.0 - 19.0 + 20.0)
    assert pf.realized_pnl == pytest.approx(1.0)
    assert ledger.merges == [("F1?", "complement", pytest.approx(20.0),
                              pytest.approx(1.0))]


def test_live_merge_bucht_nichts_ohne_onchain_bestaetigung():
    # merge_pairs liefert False (Revert/RPC/Gas) -> Buchhaltung unverändert.
    snap = MarketSnapshot(markets=[mk_market(1)])
    pf = Portfolio(cash=100.0)
    buy(pf, "y1", 0.55, 20)
    buy(pf, "n1", 0.40, 20)
    ledger = FakeLedger()
    assert main.live_merge_positions(snap, pf, FakeMerger(ok=False), ledger) == 0.0
    assert pf.positions["y1"].shares == pytest.approx(20)
    assert pf.positions["n1"].shares == pytest.approx(20)
    assert pf.realized_pnl == pytest.approx(0.0)
    assert ledger.merges == []


def test_live_merge_ueberspringt_unvollstaendige_paare():
    # Nur ein Bein im Bestand -> es geht gar keine Transaktion raus.
    snap = MarketSnapshot(markets=[mk_market(1)])
    pf = Portfolio(cash=100.0)
    buy(pf, "y1", 0.55, 20)
    merger = FakeMerger()
    assert main.live_merge_positions(snap, pf, merger) == 0.0
    assert merger.calls == []
    assert pf.positions["y1"].shares == pytest.approx(20)


def test_live_merge_negrisk_no_satz_vor_paaren_und_yes_bleibt_liegen():
    # Vollständige YES- UND NO-Sätze: der NO-Satz läuft über convertPositions
    # ((n-1) pro Satz), die YES-Tokens sind on-chain nicht mergebar und
    # bleiben liegen — insbesondere zerlegen Paar-Merges nie den NO-Satz.
    markets = [mk_market(i, negrisk=True, question_id=qid(i)) for i in range(3)]
    snap = MarketSnapshot(negrisk_events={"ev": markets})
    pf = Portfolio(cash=100.0)
    for i in range(3):
        buy(pf, f"y{i}", 0.30, 10)
        buy(pf, f"n{i}", 0.60, 10)
    merger = FakeMerger(ok=True)
    assert main.live_merge_positions(snap, pf, merger) == pytest.approx(10)
    assert merger.calls == [
        ("negrisk_no", (qid(0), qid(1), qid(2)), pytest.approx(10.0))]
    # NO-Beine verbraucht (Einstand 18, Payout 20), YES-Set unangetastet.
    assert sorted(pf.positions) == ["y0", "y1", "y2"]
    assert pf.realized_pnl == pytest.approx(20.0 - 18.0)
    assert pf.cash == pytest.approx(100.0 - 27.0 + 20.0)


def test_live_merge_negrisk_teilmarkt_paar_via_adapter():
    # Kein vollständiger Satz, aber YES+NO desselben Teilmarkts (z.B. aus
    # Market-Making-Inventar) -> Merge über den NegRisk Adapter.
    markets = [mk_market(i, negrisk=True, question_id=qid(i)) for i in range(3)]
    snap = MarketSnapshot(negrisk_events={"ev": markets})
    pf = Portfolio(cash=100.0)
    buy(pf, "y0", 0.55, 10)
    buy(pf, "n0", 0.40, 10)
    ledger = FakeLedger()
    merger = FakeMerger(ok=True)
    assert main.live_merge_positions(snap, pf, merger, ledger) == pytest.approx(10)
    assert merger.calls == [("negrisk", "c0", pytest.approx(10.0))]
    assert pf.positions == {}
    assert pf.realized_pnl == pytest.approx(10.0 - 9.5)
    assert ledger.merges[0][1] == "negrisk_pair"


def test_live_merge_negrisk_no_ohne_question_ids_ohne_call():
    # Ohne questionIds ist die convertPositions-Bitmaske nicht ableitbar —
    # der Satz bleibt liegen statt eine falsche Transaktion zu riskieren.
    markets = [mk_market(i, negrisk=True, question_id="") for i in range(3)]
    snap = MarketSnapshot(negrisk_events={"ev": markets})
    pf = Portfolio(cash=100.0)
    for i in range(3):
        buy(pf, f"n{i}", 0.60, 10)
    merger = FakeMerger(ok=True)
    assert main.live_merge_positions(snap, pf, merger) == 0.0
    assert merger.calls == []
    assert len(pf.positions) == 3


def test_live_merge_raist_nie_in_den_bot_loop():
    class BoomMerger:
        def merge_pairs(self, *a):
            raise RuntimeError("boom")

    snap = MarketSnapshot(markets=[mk_market(1)])
    pf = Portfolio(cash=100.0)
    buy(pf, "y1", 0.55, 20)
    buy(pf, "n1", 0.40, 20)
    assert main.live_merge_positions(snap, pf, BoomMerger()) == 0.0
    assert pf.positions["y1"].shares == pytest.approx(20)


# ---- Integration: tick() im Live-Modus ----------------------------------------

def snapshot_komplement_arb() -> MarketSnapshot:
    m = mk_market(1)
    books = {
        "y1": OrderBook(token_id="y1", asks=[Level(0.55, 20)]),
        "n1": OrderBook(token_id="n1", asks=[Level(0.40, 20)]),
    }
    return MarketSnapshot(markets=[m], books=books)


def live_broker_mit_merger(merger) -> PaperBroker:
    # PaperBroker als Ausführungs-Fake; der Merge-Pfad in tick() hängt nur
    # am Attribut broker.merger (wie beim echten LiveBroker).
    broker = PaperBroker()
    broker.merger = merger
    return broker


def test_tick_merged_live_via_merge_executor():
    cfg = BotConfig()
    cfg.mode = "live"
    assert cfg.risk.live_auto_merge is True  # Default an
    merger = FakeMerger(ok=True)
    pf = Portfolio(cash=1000.0)
    fills = main.tick(cfg, snapshot_komplement_arb(),
                      [REGISTRY["complement_arb"](cfg)], RiskManager(cfg),
                      live_broker_mit_merger(merger), pf)
    assert fills == 2
    assert merger.calls == [("pairs", "c1", pytest.approx(20.0))]
    assert pf.positions == {}  # on-chain bestätigt -> gebucht wie im Paper-Merge
    assert pf.cash == pytest.approx(1000.0 - 20 * 0.95 + 20.0)


def test_tick_live_ohne_auto_merge_flag_laesst_positionen_liegen():
    cfg = BotConfig()
    cfg.mode = "live"
    cfg.risk.live_auto_merge = False
    merger = FakeMerger(ok=True)
    pf = Portfolio(cash=1000.0)
    main.tick(cfg, snapshot_komplement_arb(),
              [REGISTRY["complement_arb"](cfg)], RiskManager(cfg),
              live_broker_mit_merger(merger), pf)
    assert merger.calls == []
    assert pf.positions["y1"].shares == pytest.approx(20)
    assert pf.positions["n1"].shares == pytest.approx(20)
