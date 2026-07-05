"""Tests für die Go-Live-Startstrecke (polybot/preflight.py).

web3 wird KOMPLETT gemockt — kein Netz, keine Chain: geprüft werden der
Plan-Modus (nichts senden, Bedarf korrekt ableiten), die Balance-Auswertung,
die Swap-Bedarfsberechnung, das Approval-Skip bei ausreichender Allowance,
die eth_call-Ermittlung der Onramp-Signatur und der harte Abbruch im
execute-Modus, sobald ein Receipt scheitert (nie halbfertig weitermachen).
"""

from unittest.mock import MagicMock

import pytest
from eth_account import Account

import polybot.main as main
from polybot import preflight
from polybot.preflight import (MAX_UINT256, MIN_FUNDING_UNITS, Preflight,
                               build_funding_plan)

# Beliebiger gültiger secp256k1-Key — es wird nichts gesendet.
TEST_KEY = "0x" + "11" * 32
TEST_ADDR = Account.from_key(TEST_KEY).address
ZERO_ADDR = "0x" + "00" * 20


def make_w3(gas_price=30_000_000_000, pol=10**18, estimate=120_000, status=1):
    """MagicMock-web3: konfigurierbare Antworten, jede contract()-Instanz frisch."""
    w3 = MagicMock()
    w3.eth.contract.side_effect = lambda address, abi: MagicMock(address=address)
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.gas_price = gas_price
    w3.eth.estimate_gas.return_value = estimate
    w3.eth.get_balance.return_value = pol
    w3.eth.send_raw_transaction.return_value = b"\x12" * 32
    w3.eth.wait_for_transaction_receipt.return_value = {"status": status}
    return w3


def wire(contract_fn) -> None:
    """build_transaction des Funktions-Mocks liefert ein signierbares Tx-Dict."""
    contract_fn.return_value.build_transaction.side_effect = (
        lambda params: {**params, "to": ZERO_ADDR, "value": 0, "data": "0x"})


def make_pf(execute=False, usdc=0, usdce=0, pusd=0, allowance=0,
            approved_all=True, w3=None, status=1, clob=None,
            signature_type="0") -> tuple[Preflight, MagicMock]:
    """Voll verdrahteter Preflight gegen eine gemockte web3-Instanz."""
    w3 = w3 if w3 is not None else make_w3(status=status)
    pf = Preflight(TEST_KEY, signature_type=signature_type, execute=execute,
                   w3=w3, clob_factory=(lambda: clob) if clob is not None else None)
    pf.usdc.functions.balanceOf.return_value.call.return_value = usdc
    pf.usdce.functions.balanceOf.return_value.call.return_value = usdce
    pf.pusd.functions.balanceOf.return_value.call.return_value = pusd
    for token in (pf.usdc, pf.usdce, pf.pusd):
        token.functions.allowance.return_value.call.return_value = allowance
        wire(token.functions.approve)
    pf.ctf.functions.isApprovedForAll.return_value.call.return_value = approved_all
    wire(pf.ctf.functions.setApprovalForAll)
    wire(pf.router.functions.exactInputSingle)
    wire(pf.onramp.functions.wrap)
    wire(pf.onramp.functions.deposit)
    # QuoterV2: Default-Quote praktisch 1:1 — Fee-Tier 100 verifizierbar.
    pf.quoter.functions.quoteExactInputSingle.return_value.call.return_value = (
        10**18, 0, 0, 0)
    return pf, w3


def make_clob() -> MagicMock:
    clob = MagicMock()
    clob.get_address.return_value = TEST_ADDR
    return clob


def by_name(steps) -> dict:
    return {s.name: s for s in steps}


# ---- Plan-Modus ohne Key -------------------------------------------------------

def test_plan_ohne_key_meldet_fehlt_und_ruehrt_die_chain_nicht_an():
    w3 = make_w3()
    pf = Preflight(None, execute=False, w3=w3)
    steps = pf.run()
    assert [s.name for s in steps] == ["Wallet-Key"]
    assert steps[0].status == "FEHLT"
    assert "POLY_PRIVATE_KEY" in steps[0].detail
    w3.eth.get_balance.assert_not_called()
    w3.eth.send_raw_transaction.assert_not_called()


def test_unbrauchbarer_key_meldet_fehlt():
    pf = Preflight("0xkaputt", execute=False, w3=make_w3())
    steps = pf.run()
    assert steps[0].status == "FEHLT"
    assert "unbrauchbar" in steps[0].detail


def test_falscher_signatur_typ_wird_gemeldet():
    pf, _ = make_pf(signature_type="2")
    named = by_name(pf.run())
    assert named["Signatur-Typ"].status == "FEHLT"
    assert "POLY_SIGNATURE_TYPE=0" in named["Signatur-Typ"].detail


# ---- Balance-Auswertung ---------------------------------------------------------

def test_plan_liest_und_meldet_alle_guthaben():
    pf, w3 = make_pf(usdc=25_000_000, usdce=5_000_000, pusd=1_000_000)
    named = by_name(pf.run())
    detail = named["Guthaben"].detail
    assert named["Guthaben"].status == "OK"
    assert "USDC 25.00" in detail
    assert "USDC.e 5.00" in detail
    assert "pUSD 1.00" in detail
    assert "POL 1.0000" in detail
    # Adresse wurde aus dem Key abgeleitet und angezeigt.
    assert TEST_ADDR in named["Wallet-Key"].detail
    # Plan-Modus: es geht NIE eine Transaktion raus.
    w3.eth.send_raw_transaction.assert_not_called()


def test_null_pol_warnung_im_guthaben():
    pf, _ = make_pf(w3=make_w3(pol=0))
    named = by_name(pf.run())
    assert "0 POL" in named["Guthaben"].detail


def test_plan_zeigt_swap_wrap_und_approvals_als_plan():
    # 25 USDC + 5 USDC.e, keine Allowances: alles muss als PLAN erscheinen.
    pf, w3 = make_pf(usdc=25_000_000, usdce=5_000_000, allowance=0,
                     approved_all=False)
    named = by_name(pf.run())
    assert named["Swap USDC->USDC.e"].status == "PLAN"
    assert "Fee-Tier 100" in named["Swap USDC->USDC.e"].detail
    # Wrap plant den Gesamtbetrag (vorhandenes USDC.e + Swap-Output nominal 1:1).
    assert named["Wrap USDC.e->pUSD"].status == "PLAN"
    assert "30.00" in named["Wrap USDC.e->pUSD"].detail
    for name in ("Approval USDC->SwapRouter02",
                 "Approval USDC.e->CollateralOnramp",
                 "Approval pUSD->CTF Exchange V2",
                 "Approval pUSD->NegRisk Exchange V2",
                 "Freigabe CTF->CTF Exchange V2",
                 "Freigabe CTF->NegRisk Exchange V2",
                 "Freigabe CTF->NegRisk Adapter"):
        assert named[name].status == "PLAN", name
    assert named["CLOB-Anbindung"].status == "PLAN"
    w3.eth.send_raw_transaction.assert_not_called()


# ---- Swap-Bedarfsberechnung (reine Funktion) --------------------------------------

def test_funding_plan_swappt_ganzes_natives_usdc():
    plan = build_funding_plan({"usdc": 25_000_000, "usdce": 0, "pusd": 0})
    assert plan.swap_in == 25_000_000
    assert plan.needs_swap is True
    assert plan.wrap_expected == 25_000_000
    assert plan.needs_wrap is True


def test_funding_plan_ignoriert_staub():
    plan = build_funding_plan({"usdc": MIN_FUNDING_UNITS - 1, "usdce": 0})
    assert plan.swap_in == 0
    assert plan.needs_swap is False
    assert plan.needs_wrap is False


def test_funding_plan_wrap_ohne_swap():
    plan = build_funding_plan({"usdc": 0, "usdce": 7_000_000})
    assert plan.needs_swap is False
    assert plan.wrap_expected == 7_000_000
    assert plan.needs_wrap is True


def test_funding_plan_alles_schon_pusd():
    plan = build_funding_plan({"usdc": 0, "usdce": 0, "pusd": 500_000_000})
    assert plan.needs_swap is False
    assert plan.needs_wrap is False


# ---- Approvals -------------------------------------------------------------------

def test_approvals_werden_bei_ausreichender_allowance_uebersprungen():
    pf, w3 = make_pf(execute=True, allowance=2**200, approved_all=True,
                     clob=make_clob())
    named = by_name(pf.run())
    for name in ("Approval pUSD->CTF Exchange V2",
                 "Approval pUSD->NegRisk Exchange V2"):
        assert named[name].status == "OK"
        assert "bereits ausreichend" in named[name].detail
    for name in ("Freigabe CTF->CTF Exchange V2",
                 "Freigabe CTF->NegRisk Exchange V2",
                 "Freigabe CTF->NegRisk Adapter"):
        assert named[name].status == "OK"
    pf.pusd.functions.approve.assert_not_called()
    pf.ctf.functions.setApprovalForAll.assert_not_called()
    w3.eth.send_raw_transaction.assert_not_called()


def test_fehlende_approvals_werden_im_execute_gesetzt():
    pf, w3 = make_pf(execute=True, allowance=0, approved_all=False,
                     clob=make_clob())
    named = by_name(pf.run())
    # pUSD: unbegrenzte Freigabe an beide Exchanges.
    spenders = [c.args[0] for c in pf.pusd.functions.approve.call_args_list]
    amounts = {c.args[1] for c in pf.pusd.functions.approve.call_args_list}
    assert spenders == [preflight.CTF_EXCHANGE_V2_ADDRESS,
                        preflight.NEG_RISK_CTF_EXCHANGE_V2_ADDRESS]
    assert amounts == {MAX_UINT256}
    # CTF: Operator-Freigabe an beide Exchanges UND den NegRisk Adapter.
    operators = [c.args[0] for c in
                 pf.ctf.functions.setApprovalForAll.call_args_list]
    assert operators == [preflight.CTF_EXCHANGE_V2_ADDRESS,
                         preflight.NEG_RISK_CTF_EXCHANGE_V2_ADDRESS,
                         preflight.NEG_RISK_ADAPTER_ADDRESS]
    assert all(c.args[1] is True for c in
               pf.ctf.functions.setApprovalForAll.call_args_list)
    # 2 approve- + 3 setApprovalForAll-Transaktionen, alle mit Receipt.
    assert w3.eth.send_raw_transaction.call_count == 5
    assert named["Approval pUSD->CTF Exchange V2"].status == "AUSGEFÜHRT"
    assert named["Freigabe CTF->NegRisk Adapter"].status == "AUSGEFÜHRT"
    assert named["Approval pUSD->CTF Exchange V2"].tx  # Tx-Hash im Bericht


# ---- Swap + Wrap im execute-Modus ---------------------------------------------------

def test_execute_swap_und_wrap_happy_path():
    clob = make_clob()
    pf, w3 = make_pf(execute=True, usdc=50_000_000, usdce=50_000_000,
                     allowance=2**200, clob=clob)
    named = by_name(pf.run())
    # Swap: exactInputSingle mit Fee-Tier 100 (Quoter ok) und minOut = -0.3%.
    params = pf.router.functions.exactInputSingle.call_args.args[0]
    token_in, token_out, fee, recipient, amount_in, min_out, sqrt_limit = params
    assert token_in == preflight.USDC_NATIVE_ADDRESS
    assert token_out == preflight.USDC_E_ADDRESS
    assert fee == 100
    assert recipient == TEST_ADDR
    assert amount_in == 50_000_000
    assert min_out == 49_850_000
    assert sqrt_limit == 0
    assert named["Swap USDC->USDC.e"].status == "AUSGEFÜHRT"
    assert named["Swap USDC->USDC.e"].tx
    # Wrap: Variante per eth_call ermittelt — erster Kandidat ist die on-chain
    # verifizierte Signatur wrap(from, to, amount), voller USDC.e-Bestand.
    pf.onramp.functions.wrap.assert_called_once_with(
        TEST_ADDR, TEST_ADDR, 50_000_000)
    pf.onramp.functions.deposit.assert_not_called()
    assert named["Wrap USDC.e->pUSD"].status == "AUSGEFÜHRT"
    # CLOB getestet, Adresse abgeglichen.
    clob.create_or_derive_api_key.assert_called_once()
    assert named["CLOB-Anbindung"].status == "OK"
    # Genau 2 Transaktionen (Approvals reichten aus): Swap + Wrap.
    assert w3.eth.send_raw_transaction.call_count == 2


def test_wrap_faellt_auf_naechste_variante_zurueck_wenn_simulation_revertet():
    w3 = make_w3()
    # eth_call: beide wrap(address,address,uint256)-Deutungen und wrap(uint256)
    # reverten, erst deposit(uint256) simuliert erfolgreich.
    w3.eth.call.side_effect = [RuntimeError("execution reverted")] * 3 + [b""]
    pf, _ = make_pf(execute=True, usdce=10_000_000, allowance=2**200,
                    w3=w3, clob=make_clob())
    named = by_name(pf.run())
    pf.onramp.functions.deposit.assert_called_once_with(10_000_000)
    pf.onramp.functions.wrap.assert_not_called()
    assert "deposit(amount)" in named["Wrap USDC.e->pUSD"].detail


def test_wrap_token_deutung_wenn_from_deutung_revertet():
    w3 = make_w3()
    # Erste Deutung (from, to, amount) revertet, zweite (token, to, amount)
    # simuliert erfolgreich -> gesendet wird die zweite.
    w3.eth.call.side_effect = [RuntimeError("execution reverted"), b""]
    pf, _ = make_pf(execute=True, usdce=10_000_000, allowance=2**200,
                    w3=w3, clob=make_clob())
    named = by_name(pf.run())
    pf.onramp.functions.wrap.assert_called_once_with(
        preflight.USDC_E_ADDRESS, TEST_ADDR, 10_000_000)
    assert "wrap(token,to,amount)" in named["Wrap USDC.e->pUSD"].detail


def test_wrap_bricht_ab_wenn_keine_signatur_simulierbar():
    w3 = make_w3()
    w3.eth.call.side_effect = RuntimeError("execution reverted")
    pf, _ = make_pf(execute=True, usdce=10_000_000, allowance=2**200,
                    w3=w3, clob=make_clob())
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    assert "CollateralOnramp" in named["Abbruch"].detail
    # Es wurde NICHTS gesendet — und nach dem Abbruch kommt keine Stufe mehr.
    w3.eth.send_raw_transaction.assert_not_called()
    assert "CLOB-Anbindung" not in named


def test_quoter_fallback_auf_fee_tier_500():
    pf, _ = make_pf(usdc=20_000_000)
    # Fee-Tier 100 nicht quotierbar (Pool fehlt), 500 liefert ~1:1.
    pf.quoter.functions.quoteExactInputSingle.return_value.call.side_effect = [
        RuntimeError("no pool"), (19_990_000, 0, 0, 0)]
    named = by_name(pf.run())
    assert named["Swap USDC->USDC.e"].status == "PLAN"
    assert "Fee-Tier 500" in named["Swap USDC->USDC.e"].detail


def test_quoter_quote_unter_slippage_deckel_probiert_naechstes_tier():
    pf, _ = make_pf(usdc=20_000_000)
    # Tier 100 quotiert 1% unter Parität (> 0.3% Slippage) -> Tier 500 gewinnt.
    pf.quoter.functions.quoteExactInputSingle.return_value.call.side_effect = [
        (19_800_000, 0, 0, 0), (19_990_000, 0, 0, 0)]
    named = by_name(pf.run())
    assert "Fee-Tier 500" in named["Swap USDC->USDC.e"].detail


# ---- Harter Abbruch im execute-Modus -------------------------------------------------

def test_execute_bricht_bei_fehlgeschlagenem_receipt_ab():
    # Receipt status=0 auf der ersten Transaktion (Swap): Abbruch als FEHLER,
    # keine weitere Stufe läuft, keine weitere Transaktion geht raus.
    pf, w3 = make_pf(execute=True, usdc=50_000_000, allowance=2**200,
                     status=0, clob=make_clob())
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    assert "status=0" in named["Abbruch"].detail
    assert w3.eth.send_raw_transaction.call_count == 1
    pf.onramp.functions.wrap.assert_not_called()
    pf.pusd.functions.approve.assert_not_called()
    pf.ctf.functions.setApprovalForAll.assert_not_called()
    assert "CLOB-Anbindung" not in named


def test_execute_bricht_bei_zu_wenig_pol_ab():
    # 120k Gas * 1.25 Puffer * 30 gwei = 4.5e15 wei — Balance knapp darunter.
    w3 = make_w3(pol=int(120_000 * 1.25 * 30_000_000_000) - 1)
    pf, _ = make_pf(execute=True, usdc=50_000_000, allowance=2**200,
                    w3=w3, clob=make_clob())
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    assert "POL" in named["Abbruch"].detail
    w3.eth.send_raw_transaction.assert_not_called()


def test_execute_bricht_bei_gas_revert_ab():
    w3 = make_w3()
    w3.eth.estimate_gas.side_effect = RuntimeError("execution reverted")
    pf, _ = make_pf(execute=True, usdc=50_000_000, allowance=2**200,
                    w3=w3, clob=make_clob())
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    w3.eth.send_raw_transaction.assert_not_called()


def test_clob_adressabgleich_schlaegt_alarm_bei_fremder_adresse():
    clob = make_clob()
    clob.get_address.return_value = "0x" + "99" * 20
    pf, _ = make_pf(execute=True, clob=clob)
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    assert "erwartet" in named["Abbruch"].detail


# ---- CLI-Integration ------------------------------------------------------------------

def test_main_preflight_ohne_key_exit_code_1(monkeypatch):
    monkeypatch.setattr("sys.argv", ["polybot", "preflight"])
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLY_SIGNATURE_TYPE", raising=False)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
