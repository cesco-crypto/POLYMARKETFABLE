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
from polybot import deposit_wallet, preflight
from polybot.preflight import (MAX_UINT256, MIN_FUNDING_UNITS, Preflight,
                               build_funding_plan)

# Beliebiger gültiger secp256k1-Key — es wird nichts gesendet.
TEST_KEY = "0x" + "11" * 32
TEST_ADDR = Account.from_key(TEST_KEY).address
ZERO_ADDR = "0x" + "00" * 20
# Deterministische Deposit-Wallet-Adresse des Test-EOA (BeaconProxy-Form).
TEST_DW = deposit_wallet.derive_beacon_deposit_wallet(TEST_ADDR)


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
    # Kein Code an keiner Adresse (Deposit Wallet gilt als nicht deployt);
    # eth_call liefert Rohbytes, aus denen predictWalletAddress nichts liest
    # (die On-Chain-Gegenprobe gilt in Tests als "nicht verfügbar").
    w3.eth.get_code.return_value = b""
    return w3


def wire(contract_fn) -> None:
    """build_transaction des Funktions-Mocks liefert ein signierbares Tx-Dict."""
    contract_fn.return_value.build_transaction.side_effect = (
        lambda params: {**params, "to": ZERO_ADDR, "value": 0, "data": "0x"})


def make_pf(execute=False, usdc=0, usdce=0, pusd=0, allowance=0,
            approved_all=True, w3=None, status=1, clob=None,
            signature_type="3", funder="", relayer_key="",
            relayer_factory=None, dw_deployed=False) -> tuple[Preflight, MagicMock]:
    """Voll verdrahteter Preflight gegen eine gemockte web3-Instanz.

    funder=""/relayer_key="" bedeutet: nicht konfiguriert (unabhängig von
    Umgebungsvariablen der Test-Maschine); dw_deployed steuert, ob am
    Deposit Wallet Code liegt.
    """
    w3 = w3 if w3 is not None else make_w3(status=status)
    if dw_deployed:
        w3.eth.get_code.side_effect = (
            lambda addr: b"\xfe" if addr == TEST_DW else b"")
    pf = Preflight(TEST_KEY, signature_type=signature_type, execute=execute,
                   w3=w3, clob_factory=(lambda: clob) if clob is not None else None,
                   funder=funder, relayer_api_key=relayer_key,
                   relayer_factory=relayer_factory)
    pf.usdc.functions.balanceOf.return_value.call.return_value = usdc
    pf.usdce.functions.balanceOf.return_value.call.return_value = usdce
    pf.pusd.functions.balanceOf.return_value.call.return_value = pusd
    for token in (pf.usdc, pf.usdce, pf.pusd):
        token.functions.allowance.return_value.call.return_value = allowance
        wire(token.functions.approve)
    wire(pf.pusd.functions.transfer)
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
    assert "POLY_SIGNATURE_TYPE=3" in named["Signatur-Typ"].detail


def test_eoa_signatur_typ_gilt_nicht_mehr():
    # Seit dem Exchange-Upgrade lehnt der CLOB EOA-Maker ab — 0 ist FEHLT.
    pf, _ = make_pf(signature_type="0")
    named = by_name(pf.run())
    assert named["Signatur-Typ"].status == "FEHLT"


def test_signatur_typ_3_ist_ok():
    pf, _ = make_pf(signature_type="3")
    named = by_name(pf.run())
    assert named["Signatur-Typ"].status == "OK"
    assert "POLY_1271" in named["Signatur-Typ"].detail


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


# ---- Deposit Wallet (POLY_1271) --------------------------------------------------------

# Regressionsvektor der CREATE2-Ableitung: Owner des Live-Wallets -> die am
# 05.07.2026 on-chain (factory.predictWalletAddress) bestätigte Adresse.
LIVE_OWNER = "0x5cbED94234EaE9cbC0cea21c7c9c933C8a5ad159"
LIVE_DW_BEACON = "0xd651247C926E627fC87A859b97c1CC885ca219ec"
LIVE_DW_UUPS = "0xdf537A75bd568CFa9f72F1671d231fd616640444"


def make_relayer(state=None) -> MagicMock:
    """Relayer-Mock: submit-Aufrufe setzen state['granted']/state['deployed']."""
    state = state if state is not None else {}
    relayer = MagicMock()
    relayer.get_nonce.return_value = 0

    def _create():
        state["deployed"] = True
        return "txid-create"

    def _batch(*args, **kwargs):
        state["granted"] = True
        return "txid-batch"

    relayer.submit_wallet_create.side_effect = _create
    relayer.submit_wallet_batch.side_effect = _batch
    relayer.wait.return_value = {"transactionHash": "0xrelayed",
                                 "state": "STATE_CONFIRMED"}
    return relayer


def test_deposit_wallet_ableitung_regressionsvektor():
    assert deposit_wallet.derive_beacon_deposit_wallet(LIVE_OWNER) == LIVE_DW_BEACON
    assert deposit_wallet.derive_uups_deposit_wallet(LIVE_OWNER) == LIVE_DW_UUPS


def test_plan_meldet_deposit_wallet_adresse_und_funder_fehlt():
    pf, w3 = make_pf()
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Adresse"].status == "OK"
    assert TEST_DW in named["Deposit-Wallet-Adresse"].detail
    # Ohne POLY_FUNDER_ADDRESS: FEHLT mit der abgeleiteten Adresse zum Kopieren.
    assert named["Funder-Konfiguration"].status == "FEHLT"
    assert TEST_DW in named["Funder-Konfiguration"].detail
    w3.eth.send_raw_transaction.assert_not_called()


def test_funder_abgleich_ok_bei_passender_adresse():
    pf, _ = make_pf(funder=TEST_DW.lower())  # Groß-/Kleinschreibung egal
    named = by_name(pf.run())
    assert named["Funder-Konfiguration"].status == "OK"


def test_funder_abgleich_meldet_falsche_adresse():
    pf, _ = make_pf(funder="0x" + "aa" * 20)
    named = by_name(pf.run())
    assert named["Funder-Konfiguration"].status == "FEHLT"
    assert TEST_DW in named["Funder-Konfiguration"].detail


def test_deployment_ohne_relayer_key_meldet_fehlt():
    pf, _ = make_pf()
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Deployment"].status == "FEHLT"
    assert "polymarket.com/settings" in named["Deposit-Wallet-Deployment"].detail


def test_deployment_plan_mit_relayer_key():
    pf, _ = make_pf(relayer_key="relayer-key")
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Deployment"].status == "PLAN"
    assert "WALLET-CREATE" in named["Deposit-Wallet-Deployment"].detail


def test_deployment_ok_wenn_code_vorhanden():
    pf, _ = make_pf(dw_deployed=True)
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Deployment"].status == "OK"


def test_execute_deployt_deposit_wallet_via_relayer():
    state = {"deployed": False}
    relayer = make_relayer(state)
    pf, w3 = make_pf(execute=True, allowance=2**200, clob=make_clob(),
                     relayer_key="relayer-key", relayer_factory=lambda: relayer)
    # Nach dem WALLET-CREATE liegt Code am Deposit Wallet (Receipt-Ersatz).
    w3.eth.get_code.side_effect = (
        lambda addr: b"\xfe" if state["deployed"] and addr == TEST_DW else b"")
    named = by_name(pf.run())
    relayer.submit_wallet_create.assert_called_once()
    relayer.wait.assert_called_once_with("txid-create")
    assert named["Deposit-Wallet-Deployment"].status == "AUSGEFÜHRT"
    assert named["Deposit-Wallet-Deployment"].tx == "0xrelayed"


def test_execute_bricht_ab_wenn_relayer_erfolg_ohne_code_meldet():
    # Ehrlichkeit: der Relayer sagt "confirmed", aber on-chain liegt kein
    # Code -> harter Abbruch statt Weitermachen auf Verdacht.
    relayer = make_relayer()
    pf, _ = make_pf(execute=True, allowance=2**200, clob=make_clob(),
                    relayer_key="relayer-key", relayer_factory=lambda: relayer)
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    assert "kein Code" in named["Abbruch"].detail
    assert "CLOB-Anbindung" not in named


def test_execute_relayer_fehlschlag_bricht_ab():
    relayer = make_relayer()
    relayer.wait.side_effect = deposit_wallet.DepositWalletError(
        "Relayer-Transaktion txid-create endete als STATE_FAILED")
    pf, _ = make_pf(execute=True, pusd=500_000_000, allowance=2**200,
                    clob=make_clob(), relayer_key="relayer-key",
                    relayer_factory=lambda: relayer)
    named = by_name(pf.run())
    assert named["Abbruch"].status == "FEHLER"
    assert "STATE_FAILED" in named["Abbruch"].detail
    # Nach dem Abbruch fließt kein Kapital mehr und keine Stufe läuft weiter.
    pf.pusd.functions.transfer.assert_not_called()
    assert "CLOB-Anbindung" not in named


def test_deposit_approvals_ok_wenn_gesetzt():
    pf, _ = make_pf(allowance=2**200, approved_all=True)
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Approvals"].status == "OK"


def test_deposit_approvals_plan_listet_fehlende():
    pf, _ = make_pf(allowance=0, approved_all=False)
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Approvals"].status == "PLAN"
    assert "pUSD->CTF Exchange V2" in named["Deposit-Wallet-Approvals"].detail
    assert "CTF->NegRisk Adapter" in named["Deposit-Wallet-Approvals"].detail


def test_execute_setzt_deposit_approvals_als_relayer_batch():
    state = {"granted": False}
    relayer = make_relayer(state)
    pf, w3 = make_pf(execute=True, clob=make_clob(), relayer_key="relayer-key",
                     relayer_factory=lambda: relayer, dw_deployed=True)

    # EOA hat alle Freigaben; das Deposit Wallet erst nach dem Batch.
    def allowance_mock(owner, spender):
        m = MagicMock()
        m.call.return_value = (MAX_UINT256 if owner == TEST_ADDR
                               or state["granted"] else 0)
        return m

    def afa_mock(owner, operator):
        m = MagicMock()
        m.call.return_value = owner == TEST_ADDR or state["granted"]
        return m

    pf.pusd.functions.allowance.side_effect = allowance_mock
    pf.ctf.functions.isApprovedForAll.side_effect = afa_mock
    pf.pusd.encode_abi = MagicMock(return_value="0x01")
    pf.ctf.encode_abi = MagicMock(return_value="0x02")
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Approvals"].status == "AUSGEFÜHRT"
    assert named["Deposit-Wallet-Approvals"].tx == "0xrelayed"
    wallet, nonce, deadline, calls, signature = (
        relayer.submit_wallet_batch.call_args.args)
    assert wallet == TEST_DW
    assert nonce == 0
    # 2x pUSD-approve + 3x setApprovalForAll, alle value=0, an das Wallet gerichtet.
    assert len(calls) == 5
    assert all(c["value"] == 0 for c in calls)
    # Echte 65-Byte-EIP-712-Signatur des Owner-EOA über den Batch.
    assert signature.startswith("0x") and len(signature) == 132
    # Jede Call-Data wurde vor dem Batch aus Wallet-Sicht simuliert.
    froms = [c.kwargs.get("from") or c.args[0].get("from")
             for c in w3.eth.call.call_args_list
             if (c.args and isinstance(c.args[0], dict))]
    assert froms.count(TEST_DW) >= 5


def test_execute_deposit_approvals_ohne_wallet_meldet_fehlt():
    # Kein Relayer-Key, Wallet nicht deployt: execute kann den Batch nicht
    # senden — FEHLT (kein Abbruch, die Anweisung steht im Deploy-Schritt).
    pf, _ = make_pf(execute=True, allowance=0, approved_all=False,
                    clob=make_clob())
    named = by_name(pf.run())
    assert named["Deposit-Wallet-Approvals"].status == "FEHLT"


def test_funding_plan_zeigt_pusd_transfer():
    pf, _ = make_pf(pusd=500_000_000)
    named = by_name(pf.run())
    assert named["pUSD->Deposit-Wallet"].status == "PLAN"
    assert "500.00" in named["pUSD->Deposit-Wallet"].detail
    assert TEST_DW in named["pUSD->Deposit-Wallet"].detail


def test_funding_entfaellt_ohne_pusd():
    pf, _ = make_pf(pusd=0)
    named = by_name(pf.run())
    assert named["pUSD->Deposit-Wallet"].status == "OK"


def test_execute_transferiert_pusd_ins_deposit_wallet():
    pf, w3 = make_pf(execute=True, pusd=500_000_000, allowance=2**200,
                     clob=make_clob(), dw_deployed=True)
    named = by_name(pf.run())
    assert named["pUSD->Deposit-Wallet"].status == "AUSGEFÜHRT"
    assert named["pUSD->Deposit-Wallet"].tx
    pf.pusd.functions.transfer.assert_called_once_with(TEST_DW, 500_000_000)
    assert w3.eth.send_raw_transaction.call_count == 1


def test_execute_funding_wartet_auf_deployment():
    # Wallet (noch) nicht deployt: Kapital bleibt auf dem EOA, kein Transfer.
    pf, w3 = make_pf(execute=True, pusd=500_000_000, allowance=2**200,
                     clob=make_clob())
    named = by_name(pf.run())
    assert named["pUSD->Deposit-Wallet"].status == "FEHLT"
    pf.pusd.functions.transfer.assert_not_called()
    w3.eth.send_raw_transaction.assert_not_called()


# ---- CLI-Integration ------------------------------------------------------------------

def test_main_preflight_ohne_key_exit_code_1(monkeypatch):
    monkeypatch.setattr("sys.argv", ["polybot", "preflight"])
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLY_SIGNATURE_TYPE", raising=False)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1


def test_erc20_abi_kann_transfer_encodieren():
    """Regressionstest 05.07.: preflight --execute brach mit "ABI Not Found:
    transfer" ab — das Minimal-ABI enthielt kein transfer(address,uint256)."""
    from web3 import Web3
    from polybot.preflight import ERC20_ABI
    c = Web3().eth.contract(
        address="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", abi=ERC20_ABI)
    data = c.encode_abi("transfer",
                        ["0xd651247C926E627fC87A859b97c1CC885ca219ec", 123])
    assert data.startswith("0xa9059cbb")  # Selector von transfer(address,uint256)
    fn = c.functions.transfer("0xd651247C926E627fC87A859b97c1CC885ca219ec", 123)
    assert fn is not None
