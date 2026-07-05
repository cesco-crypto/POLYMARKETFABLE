"""Go-Live-Preflight (POLY_PRIVATE_KEY + Deposit-Wallet-Flow, POLY_SIGNATURE_TYPE=3).

Die komplette Startstrecke vor dem ersten Live-Trade, als CLI-Kommando:

  python -m polybot.main preflight             # nur prüfen, PLAN drucken
  python -m polybot.main preflight --execute   # Transaktionen wirklich senden

Stufen (jede endet als OK / FEHLT / PLAN / AUSGEFÜHRT / FEHLER im Bericht):
  1. Private Key aus der Umgebung laden, Adresse ableiten.
  2. Guthaben lesen: POL (Gas), natives USDC, USDC.e, pUSD.
  3. Natives USDC -> USDC.e swappen (Uniswap V3 SwapRouter02, Fee-Tier per
     QuoterV2 on-chain verifiziert, Slippage max. 0.3%).
  4. USDC.e -> pUSD wrappen (Polymarket CollateralOnramp; die Signatur
     wrap(address,address,uint256) ist per Bytecode-Analyse verifiziert,
     die Argument-Deutung nicht — vor dem Senden werden alle Kandidaten
     per eth_call mit den echten Parametern simuliert).
  5. Trading-Approvals des EOA (nur falls fehlend; für Onramp/Merges weiter
     nötig): pUSD an beide V2-Exchanges, CTF-Operator-Freigaben.
  6. Deposit Wallet (seit Exchange-Upgrade 28.04.2026 Pflicht — der CLOB
     lehnt EOA-Maker mit "maker address not allowed" ab):
     a. Adresse deterministisch ableiten + on-chain gegen
        factory.predictWalletAddress verifizieren; POLY_FUNDER_ADDRESS-Abgleich.
     b. Deployment prüfen; fehlt es: WALLET-CREATE über den Polymarket-
        Relayer (gasless, braucht POLY_RELAYER_API_KEY aus der UI).
     c. Approvals AUS dem Deposit Wallet (pUSD an beide V2-Exchanges,
        CTF-Operator-Freigaben) — als EIP-712-signierter Relayer-Batch,
        jede Call-Data vorher per eth_call aus Wallet-Sicht simuliert.
     d. pUSD vom EOA in das Deposit Wallet transferieren (ERC20-Transfer,
        vorher per eth_call simuliert) — EOA-pUSD zählt NICHT als
        CLOB-Buying-Power des Deposit Wallets.
  7. CLOB-Anbindung testen (create_or_derive_api_key, Adress-Abgleich,
     Balance-Sync mit signature_type=3).

Fehlerphilosophie: Im execute-Modus bricht JEDER fehlgeschlagene Schritt
(Revert, Receipt status=0, zu wenig POL, RPC down, Relayer-Fehlschlag) die
Strecke sauber ab — es wird nie halbfertig weitergemacht. Im Plan-Modus
wird nichts gesendet, nur gelesen und der Plan gedruckt.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from polybot.config import CLOB_HOST, POLYGON_CHAIN_ID, BotConfig
from polybot.deposit_wallet import (DepositWalletError, DepositWalletRelayer,
                                    predict_wallet_address_onchain,
                                    resolve_deposit_wallet, sign_wallet_batch)
from polybot.onchain import (CTF_ABI, CTF_ADDRESS, GAS_LIMIT_BUFFER,
                             NEG_RISK_ADAPTER_ADDRESS, PUSD_ADDRESS,
                             RECEIPT_TIMEOUT_S)

log = logging.getLogger(__name__)
console = Console()

# ---- Contract-Adressen (Polygon Mainnet, Chain-ID 137) -------------------------
# Natives USDC (Circle, "USDC" auf Polygon PoS seit Okt. 2023):
# https://developers.circle.com/stablecoins/usdc-contract-addresses
USDC_NATIVE_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
# Bridged USDC.e (Ethereum-USDC über die PoS-Bridge) — Collateral der
# Polymarket-V1-Contracts und Input des CollateralOnramp:
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Uniswap V3 SwapRouter02 (identische Adresse auf Ethereum/Polygon/…):
# https://docs.uniswap.org/contracts/v3/reference/deployments
SWAP_ROUTER_02_ADDRESS = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
# Uniswap V3 QuoterV2 (gleiche Quelle) — quoteExactInputSingle via eth_call:
QUOTER_V2_ADDRESS = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
# Polymarket CollateralOnramp (USDC.e -> pUSD, Exchange-Upgrade 28.04.2026):
# https://docs.polymarket.com/resources/contracts (siehe docs/POLYMARKET_GUIDELINES.md)
COLLATERAL_ONRAMP_ADDRESS = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
# Polymarket CTF Exchange V2 + NegRisk CTF Exchange V2 (gleiche Quelle):
CTF_EXCHANGE_V2_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_V2_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
# pUSD, ConditionalTokens und NegRisk Adapter: siehe polybot/onchain.py.

# Öffentliche Polygon-RPCs: primär publicnode, Fallback polygon-rpc.com;
# POLY_RPC_URL aus der Umgebung wird (falls gesetzt) zuerst probiert.
RPC_URLS = ("https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com")

# USDC/USDC.e/pUSD rechnen alle in 6 Dezimalstellen.
USDC_DECIMALS = 6
# Beträge unter 0.01 USDC lohnen keine Transaktion (Gas > Nutzen).
MIN_FUNDING_UNITS = 10_000
# Uniswap-Fee-Tiers für USDC/USDC.e: 0.01% (100) ist der liquide Stable-Pool,
# 0.05% (500) der Fallback, falls der 100er on-chain nicht quotierbar ist.
SWAP_FEE_TIERS = (100, 500)
# Slippage-Deckel 0.3% (30 Basispunkte): amountOutMinimum der Swap-Tx —
# selbst wenn der Quoter lügt/ausfällt, kann nie schlechter gefüllt werden.
SLIPPAGE_BPS = 30
MAX_UINT256 = 2**256 - 1
# Ab dieser Allowance gilt eine Trading-Freigabe als "unbegrenzt gesetzt"
# (die Polymarket-UI approved MAX_UINT256; 2**128 lässt Spielraum für
# Wallets, die einen anderen, aber praktisch unerschöpflichen Wert gesetzt haben).
UNLIMITED_ALLOWANCE_THRESHOLD = 2**128

# ---- Minimale ABIs (nur die tatsächlich genutzten Funktionen) ------------------
ERC20_ABI = [
    {
        "name": "balanceOf", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"},
                   {"name": "spender", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "spender", "type": "address"},
                   {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "transfer", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"},
                   {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

# SwapRouter02: exactInputSingle OHNE deadline-Feld (anders als der V1-Router).
SWAP_ROUTER_ABI = [
    {
        "name": "exactInputSingle", "type": "function", "stateMutability": "payable",
        "inputs": [{
            "name": "params", "type": "tuple", "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
        }],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]

# QuoterV2: nonpayable, wird aber nur via eth_call (.call()) simuliert.
QUOTER_V2_ABI = [
    {
        "name": "quoteExactInputSingle", "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{
            "name": "params", "type": "tuple", "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "fee", "type": "uint24"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
        }],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
]

# CollateralOnramp: das verifizierte ABI ist ohne Polygonscan-API-Key nicht
# abrufbar. Bytecode-Analyse des Dispatchers (eth_getCode, 05.07.2026):
# der Contract exponiert wrap(address,address,uint256) (Selector 0x62355638);
# wrap(uint256)/deposit(uint256) existieren dort NICHT. Die Semantik der
# beiden Adress-Argumente ((from, to) oder (token, to)) ist ohne verifizierte
# Quelle nicht beweisbar — deshalb werden vor dem Senden BEIDE Deutungen
# (plus die 1-Arg-Legacy-Kandidaten) per eth_call mit den echten Parametern
# simuliert und nur die tatsächlich funktionierende Variante gesendet.
ONRAMP_ABI = [
    {
        "name": "wrap", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "from_", "type": "address"},
                   {"name": "to", "type": "address"},
                   {"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "wrap", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "amount", "type": "uint256"}], "outputs": [],
    },
    {
        "name": "deposit", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "amount", "type": "uint256"}], "outputs": [],
    },
]


class PreflightError(Exception):
    """Harter Abbruch der Startstrecke — nie halbfertig weitermachen."""


@dataclass
class StepResult:
    """Ergebnis einer Preflight-Stufe für den Abschlussbericht."""

    name: str
    status: str          # OK | FEHLT | PLAN | AUSGEFÜHRT | FEHLER
    detail: str = ""
    tx: str = ""         # Tx-Hash, falls eine Transaktion gesendet wurde


@dataclass
class FundingPlan:
    """Kapitalfluss-Plan: was muss geswappt, was gewrappt werden.

    pUSD-Bedarf besteht, sobald Kapital als natives USDC oder USDC.e liegt —
    Ziel der Strecke ist, ALLES verfügbare USDC-Kapital als pUSD (dem
    V2-Collateral) bereitzustellen.
    """

    swap_in: int = 0     # natives USDC -> USDC.e (Einheiten, 6 Dezimalstellen)
    usdce_now: int = 0   # bereits vorhandenes USDC.e

    @property
    def wrap_expected(self) -> int:
        """Erwartete Wrap-Menge (nominal 1:1; real minus Swap-Slippage)."""
        return self.usdce_now + self.swap_in

    @property
    def needs_swap(self) -> bool:
        return self.swap_in > 0

    @property
    def needs_wrap(self) -> bool:
        return self.wrap_expected >= MIN_FUNDING_UNITS


def build_funding_plan(balances: dict[str, int]) -> FundingPlan:
    """Swap-/Wrap-Bedarf aus den Guthaben ableiten (rein, gut testbar).

    Natives USDC unterhalb von MIN_FUNDING_UNITS (0.01 USDC) gilt als Staub
    und wird nicht geswappt — die Gas-Kosten überstiegen den Betrag.
    """
    usdc = int(balances.get("usdc", 0))
    return FundingPlan(
        swap_in=usdc if usdc >= MIN_FUNDING_UNITS else 0,
        usdce_now=int(balances.get("usdce", 0)),
    )


def _fmt(units: int | None) -> str:
    """6-Dezimal-Einheiten menschenlesbar (z.B. 25_000_000 -> '25.00 USDC-Einheiten')."""
    if units is None:
        return "?"
    return f"{units / 10**USDC_DECIMALS:,.2f}"


class Preflight:
    """Führt die Go-Live-Startstrecke aus (Plan- oder Execute-Modus).

    Für Tests kann eine fertige web3-Instanz injiziert werden (w3=...) —
    die Contract-Objekte hängen dann als Attribute (usdc, usdce, pusd,
    router, quoter, onramp, ctf) an der Instanz und sind mockbar. Ebenso
    ist der CLOB-Client über clob_factory injizierbar.
    """

    def __init__(self, private_key: str | None, signature_type: str = "0",
                 execute: bool = False, w3=None, clob_factory=None,
                 funder: str | None = None, relayer_api_key: str | None = None,
                 relayer_factory=None):
        self.private_key = private_key
        self.signature_type = signature_type
        self.execute = execute
        self.w3 = w3
        self._clob_factory = clob_factory
        # Deposit-Wallet-Flow: Ziel-Funder aus der Umgebung (Abgleich mit der
        # abgeleiteten Adresse), Relayer-API-Key für Deploy/Approval-Batches.
        self.funder = funder if funder is not None \
            else os.environ.get("POLY_FUNDER_ADDRESS")
        self.relayer_api_key = relayer_api_key if relayer_api_key is not None \
            else os.environ.get("POLY_RELAYER_API_KEY")
        self._relayer_factory = relayer_factory
        self.deposit_wallet: str | None = None
        self.dw_deployed = False
        self.account = None
        self.steps: list[StepResult] = []
        if self.w3 is not None:
            self._setup_contracts()

    # ---- Ablauf ----------------------------------------------------------------

    def run(self) -> list[StepResult]:
        """Alle Stufen durchlaufen; bei PreflightError sauber abbrechen.

        Rückgabe: die StepResults für den Abschlussbericht. Wirft selbst nie —
        der Aufrufer entscheidet anhand der Status über den Exit-Code.
        """
        try:
            if not self._step_key():
                return self.steps
            self._step_rpc()
            balances = self._step_balances()
            plan = build_funding_plan(balances)
            self._step_swap(plan)
            self._step_wrap(plan)
            self._step_trading_approvals()
            self._step_deposit_wallet()
            self._step_deposit_deploy()
            self._step_deposit_approvals()
            self._step_deposit_funding()
            self._step_clob()
        except PreflightError as e:
            log.error("Preflight abgebrochen: %s", e)
            self.steps.append(StepResult("Abbruch", "FEHLER", str(e)))
        return self.steps

    # ---- Stufe 1: Key + Signatur-Typ --------------------------------------------

    def _step_key(self) -> bool:
        if not self.private_key:
            self._add("Wallet-Key", "FEHLT",
                      "POLY_PRIVATE_KEY ist nicht gesetzt — EOA-Private-Key "
                      "(0x + 64 Hex-Zeichen) in .env/Umgebung hinterlegen")
            return False
        try:
            from eth_account import Account

            self.account = Account.from_key(self.private_key)
        except Exception as e:  # noqa: BLE001 — kaputter Key = klare Meldung
            self._add("Wallet-Key", "FEHLT", f"POLY_PRIVATE_KEY unbrauchbar: {e}")
            return False
        self._add("Wallet-Key", "OK", f"Adresse {self.account.address}")
        if str(self.signature_type) == "3":
            self._add("Signatur-Typ", "OK",
                      "POLY_SIGNATURE_TYPE=3 (POLY_1271, Deposit-Wallet-Flow)")
        else:
            # Kein Abbruch: die On-Chain-Stufen hängen nicht am Signatur-Typ.
            # Aber der V2-CLOB lehnt EOA-Maker seit dem Exchange-Upgrade ab
            # ("maker address not allowed, please use the deposit wallet flow")
            # — Orders brauchen zwingend signature_type 3 + Deposit Wallet.
            self._add("Signatur-Typ", "FEHLT",
                      "POLY_SIGNATURE_TYPE=3 (Deposit-Wallet-Flow) in der "
                      f"Umgebung setzen — aktuell {self.signature_type!r}; "
                      "EOA-Orders (0) lehnt der V2-CLOB ab")
        return True

    # ---- Stufe 2: RPC + Guthaben -------------------------------------------------

    def _step_rpc(self) -> None:
        if self.w3 is not None:  # injizierte Instanz (Tests)
            self._add("RPC", "OK", "injizierte web3-Instanz")
            return
        from web3 import HTTPProvider, Web3

        urls = list(RPC_URLS)
        env_url = os.environ.get("POLY_RPC_URL")
        if env_url:
            urls.insert(0, env_url)
        errors: list[str] = []
        for url in urls:
            try:
                w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 15}))
                chain_id = w3.eth.chain_id
                if chain_id != POLYGON_CHAIN_ID:
                    errors.append(f"{url}: chain_id {chain_id} != {POLYGON_CHAIN_ID}")
                    continue
                self.w3 = w3
                self._setup_contracts()
                self._add("RPC", "OK", url)
                return
            except Exception as e:  # noqa: BLE001 — nächsten RPC probieren
                errors.append(f"{url}: {e}")
        raise PreflightError("Kein Polygon-RPC erreichbar: " + " | ".join(errors))

    def _setup_contracts(self) -> None:
        from web3 import Web3

        contract = self.w3.eth.contract
        cs = Web3.to_checksum_address
        self.usdc = contract(address=cs(USDC_NATIVE_ADDRESS), abi=ERC20_ABI)
        self.usdce = contract(address=cs(USDC_E_ADDRESS), abi=ERC20_ABI)
        self.pusd = contract(address=cs(PUSD_ADDRESS), abi=ERC20_ABI)
        self.router = contract(address=cs(SWAP_ROUTER_02_ADDRESS), abi=SWAP_ROUTER_ABI)
        self.quoter = contract(address=cs(QUOTER_V2_ADDRESS), abi=QUOTER_V2_ABI)
        self.onramp = contract(address=cs(COLLATERAL_ONRAMP_ADDRESS), abi=ONRAMP_ABI)
        self.ctf = contract(address=cs(CTF_ADDRESS), abi=CTF_ABI)

    def _step_balances(self) -> dict[str, int]:
        addr = self.account.address
        try:
            balances = {
                "pol": int(self.w3.eth.get_balance(addr)),
                "usdc": int(self.usdc.functions.balanceOf(addr).call()),
                "usdce": int(self.usdce.functions.balanceOf(addr).call()),
                "pusd": int(self.pusd.functions.balanceOf(addr).call()),
            }
        except Exception as e:  # noqa: BLE001 — ohne Guthaben keine Strecke
            raise PreflightError(f"Guthaben nicht lesbar: {e}") from e
        detail = (f"POL {balances['pol'] / 1e18:.4f} | "
                  f"USDC {_fmt(balances['usdc'])} | "
                  f"USDC.e {_fmt(balances['usdce'])} | "
                  f"pUSD {_fmt(balances['pusd'])}")
        if balances["pol"] == 0:
            detail += " — ACHTUNG: 0 POL, keine Transaktion bezahlbar"
        self._add("Guthaben", "OK", detail)
        return balances

    # ---- Stufe 3: natives USDC -> USDC.e (Uniswap V3) -----------------------------

    def _choose_fee_tier(self, amount: int, min_out: int) -> tuple[int, int | None]:
        """Fee-Tier on-chain via QuoterV2 verifizieren (0.01% zuerst).

        Rückgabe: (fee, quotierter Output) — Output None, wenn KEIN Tier
        quotierbar war; dann wird das letzte Tier (500) verwendet und die
        Sicherheit hängt allein an amountOutMinimum (die Swap-Tx revertet,
        statt schlechter als der Slippage-Deckel zu füllen).
        """
        for fee in SWAP_FEE_TIERS:
            try:
                quote = self.quoter.functions.quoteExactInputSingle(
                    (self.usdc.address, self.usdce.address, amount, fee, 0)
                ).call()
                amount_out = int(quote[0] if isinstance(quote, (list, tuple))
                                 else quote)
            except Exception as e:  # noqa: BLE001 — Pool fehlt/Quoter down
                log.info("QuoterV2: Fee-Tier %d nicht quotierbar: %s", fee, e)
                continue
            if amount_out >= min_out:
                return fee, amount_out
            log.warning("QuoterV2: Fee-Tier %d quotiert nur %s von %s — "
                        "über dem Slippage-Deckel, nächstes Tier",
                        fee, _fmt(amount_out), _fmt(amount))
        log.warning("QuoterV2: kein Fee-Tier verifizierbar — Fallback %d, "
                    "amountOutMinimum schützt", SWAP_FEE_TIERS[-1])
        return SWAP_FEE_TIERS[-1], None

    def _step_swap(self, plan: FundingPlan) -> None:
        name = "Swap USDC->USDC.e"
        if not plan.needs_swap:
            self._add(name, "OK", "kein natives USDC — Swap entfällt")
            return
        amount = plan.swap_in
        min_out = amount * (10_000 - SLIPPAGE_BPS) // 10_000
        fee, quoted = self._choose_fee_tier(amount, min_out)
        quote_txt = (f"Quote {_fmt(quoted)}" if quoted is not None
                     else "Pool nicht quotierbar — minOut schützt")
        self._ensure_allowance(self.usdc, "Approval USDC->SwapRouter02",
                               self.router.address, amount)
        if not self.execute:
            self._add(name, "PLAN",
                      f"{_fmt(amount)} USDC via exactInputSingle, Fee-Tier {fee} "
                      f"({quote_txt}), minOut {_fmt(min_out)}")
            return
        params = (self.usdc.address, self.usdce.address, fee,
                  self.account.address, amount, min_out, 0)
        tx = self._send(self.router.functions.exactInputSingle(params), name)
        self._add(name, "AUSGEFÜHRT",
                  f"{_fmt(amount)} USDC, Fee-Tier {fee}, minOut {_fmt(min_out)}",
                  tx=tx)

    # ---- Stufe 4: USDC.e -> pUSD (CollateralOnramp) --------------------------------

    def _onramp_candidates(self, amount: int) -> tuple[tuple[str, str, tuple], ...]:
        """Wrap-Kandidaten in Probier-Reihenfolge: (Label, Funktionsname, Args).

        Die On-chain-verifizierte Signatur wrap(address,address,uint256)
        zuerst — in beiden plausiblen Deutungen der Adress-Argumente —
        danach die 1-Arg-Legacy-Kandidaten (siehe Kommentar am ONRAMP_ABI).
        """
        owner = self.account.address
        return (
            ("wrap(from,to,amount)", "wrap", (owner, owner, amount)),
            ("wrap(token,to,amount)", "wrap", (self.usdce.address, owner, amount)),
            ("wrap(amount)", "wrap", (amount,)),
            ("deposit(amount)", "deposit", (amount,)),
        )

    def _detect_onramp_fn(self, amount: int) -> tuple[str, str, tuple]:
        """Wrap-Variante per eth_call-Simulation ermitteln, BEVOR gesendet wird.

        Ein eth_call mit den ECHTEN Parametern (nach gesetzter Allowance)
        beweist, welche Signatur/Argument-Deutung der Contract akzeptiert —
        eine falsche Variante revertet und wird verworfen. Rückgabe:
        der erste funktionierende Kandidat (Label, Funktionsname, Args).
        """
        errors: list[str] = []
        for label, fn_name, args in self._onramp_candidates(amount):
            try:
                data = self.onramp.encode_abi(fn_name, args=list(args))
                self.w3.eth.call({"to": self.onramp.address,
                                  "from": self.account.address, "data": data})
                log.info("CollateralOnramp: %s per eth_call bestätigt", label)
                return label, fn_name, args
            except Exception as e:  # noqa: BLE001 — nächsten Kandidaten probieren
                errors.append(f"{label}: {e}")
        raise PreflightError(
            "CollateralOnramp: keine Wrap-Variante per eth_call simulierbar "
            "(Allowance gesetzt?) — " + " | ".join(errors))

    def _step_wrap(self, plan: FundingPlan) -> None:
        name = "Wrap USDC.e->pUSD"
        if self.execute:
            # Frisch lesen: nach dem Swap liegt der reale Output (inkl.
            # Slippage) plus etwaiges Alt-USDC.e auf dem Wallet.
            amount = int(self.usdce.functions.balanceOf(
                self.account.address).call())
        else:
            amount = plan.wrap_expected
        if amount < MIN_FUNDING_UNITS:
            self._add(name, "OK", "kein USDC.e — Wrap entfällt")
            return
        self._ensure_allowance(self.usdce, "Approval USDC.e->CollateralOnramp",
                               self.onramp.address, amount)
        if not self.execute:
            self._add(name, "PLAN",
                      f"{_fmt(amount)} USDC.e wrappen — Variante wird vor dem "
                      "Senden per eth_call ermittelt (on-chain verifiziert: "
                      "wrap(address,address,uint256); Fallbacks: wrap(uint256), "
                      "deposit(uint256))")
            return
        label, fn_name, args = self._detect_onramp_fn(amount)
        tx = self._send(getattr(self.onramp.functions, fn_name)(*args),
                        f"{name} via {label}")
        pusd_after = int(self.pusd.functions.balanceOf(
            self.account.address).call())
        self._add(name, "AUSGEFÜHRT",
                  f"{_fmt(amount)} via {label} — "
                  f"pUSD jetzt {_fmt(pusd_after)}", tx=tx)

    # ---- Stufe 5: Trading-Approvals -----------------------------------------------

    def _step_trading_approvals(self) -> None:
        from web3 import Web3

        exchanges = (
            ("CTF Exchange V2", Web3.to_checksum_address(CTF_EXCHANGE_V2_ADDRESS)),
            ("NegRisk Exchange V2",
             Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE_V2_ADDRESS)),
        )
        # pUSD-Spending-Approval an beide Exchanges (Order-Settlement zieht
        # das Collateral per transferFrom ein).
        for label, spender in exchanges:
            self._ensure_allowance(self.pusd, f"Approval pUSD->{label}",
                                   spender, MAX_UINT256, unlimited=True)
        # Operator-Freigabe der ConditionalTokens (ERC1155) an beide Exchanges
        # und den NegRisk Adapter (SELLs/Merges ziehen Outcome-Tokens ein).
        operators = exchanges + (
            ("NegRisk Adapter", Web3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS)),
        )
        for label, operator in operators:
            self._ensure_operator_approval(f"Freigabe CTF->{label}", operator)

    def _ensure_allowance(self, token, name: str, spender: str, amount: int,
                          unlimited: bool = False) -> None:
        """ERC20-Approval nur setzen, wenn die Allowance nicht schon reicht.

        unlimited=True: Trading-Approval — Ziel MAX_UINT256, "reicht" ab
        UNLIMITED_ALLOWANCE_THRESHOLD. Sonst exakter Betrag (Router/Onramp
        bekommen nie mehr Freigabe als für diese eine Strecke nötig).
        """
        owner = self.account.address
        try:
            current = int(token.functions.allowance(owner, spender).call())
        except Exception as e:  # noqa: BLE001
            raise PreflightError(f"{name}: Allowance nicht lesbar: {e}") from e
        threshold = UNLIMITED_ALLOWANCE_THRESHOLD if unlimited else amount
        if current >= threshold:
            self._add(name, "OK", "Allowance bereits ausreichend")
            return
        target = MAX_UINT256 if unlimited else amount
        target_txt = "MAX_UINT256" if unlimited else _fmt(target)
        if not self.execute:
            self._add(name, "PLAN", f"approve({target_txt}) nötig "
                                    f"(aktuell {_fmt(current)})")
            return
        tx = self._send(token.functions.approve(spender, target), name)
        self._add(name, "AUSGEFÜHRT", f"approve({target_txt})", tx=tx)

    def _ensure_operator_approval(self, name: str, operator: str) -> None:
        """ERC1155-setApprovalForAll nur setzen, wenn sie fehlt."""
        try:
            approved = bool(self.ctf.functions.isApprovedForAll(
                self.account.address, operator).call())
        except Exception as e:  # noqa: BLE001
            raise PreflightError(f"{name}: Freigabe nicht prüfbar: {e}") from e
        if approved:
            self._add(name, "OK", "Operator-Freigabe bereits gesetzt")
            return
        if not self.execute:
            self._add(name, "PLAN", "setApprovalForAll(operator, true) nötig")
            return
        tx = self._send(self.ctf.functions.setApprovalForAll(operator, True), name)
        self._add(name, "AUSGEFÜHRT", "setApprovalForAll(operator, true)", tx=tx)

    # ---- Stufe 6: Deposit Wallet (POLY_1271) ------------------------------------------

    # Deadline-Fenster für signierte Wallet-Batches (Relayer-Vorgabe: zukünftig,
    # nicht zu weit weg — die SDK-Beispiele nutzen +600s).
    BATCH_DEADLINE_S = 600

    def _relayer(self) -> DepositWalletRelayer:
        if self._relayer_factory is not None:
            return self._relayer_factory()
        return DepositWalletRelayer(self.relayer_api_key, self.account.address)

    def _step_deposit_wallet(self) -> None:
        """Deposit-Wallet-Adresse ableiten und POLY_FUNDER_ADDRESS abgleichen."""
        name = "Deposit-Wallet-Adresse"
        try:
            self.deposit_wallet = resolve_deposit_wallet(self.w3,
                                                         self.account.address)
        except DepositWalletError as e:
            raise PreflightError(f"{name}: {e}") from e
        confirmed = predict_wallet_address_onchain(self.w3, self.account.address)
        probe = ("on-chain bestätigt (factory.predictWalletAddress)"
                 if confirmed and confirmed.lower() == self.deposit_wallet.lower()
                 else "Gegenprobe nicht verfügbar — lokale CREATE2-Ableitung")
        self._add(name, "OK", f"{self.deposit_wallet} ({probe})")
        if not self.funder:
            self._add("Funder-Konfiguration", "FEHLT",
                      f"POLY_FUNDER_ADDRESS={self.deposit_wallet} in .env "
                      "setzen (der LiveBroker adressiert Orders darüber)")
        elif self.funder.lower() != self.deposit_wallet.lower():
            self._add("Funder-Konfiguration", "FEHLT",
                      f"POLY_FUNDER_ADDRESS ist {self.funder}, abgeleitet ist "
                      f"aber {self.deposit_wallet} — Wert korrigieren")
        else:
            self._add("Funder-Konfiguration", "OK",
                      "POLY_FUNDER_ADDRESS == abgeleitete Deposit-Wallet-Adresse")

    def _dw_code_present(self) -> bool:
        try:
            return len(self.w3.eth.get_code(self.deposit_wallet) or b"") > 0
        except Exception as e:  # noqa: BLE001 — ohne Code-Check kein Deploy-Status
            raise PreflightError(
                f"Deposit-Wallet-Code nicht lesbar ({self.deposit_wallet}): {e}"
            ) from e

    def _step_deposit_deploy(self) -> None:
        """Deployment prüfen; fehlt es: gasless WALLET-CREATE über den Relayer."""
        name = "Deposit-Wallet-Deployment"
        self.dw_deployed = self._dw_code_present()
        if self.dw_deployed:
            self._add(name, "OK", "Wallet-Code liegt on-chain")
            return
        if not self.relayer_api_key:
            self._add(name, "FEHLT",
                      "Wallet nicht deployt und POLY_RELAYER_API_KEY fehlt — "
                      "Key mit diesem Wallet unter "
                      "polymarket.com/settings?tab=api-keys erzeugen "
                      "(CLOB-API-Creds gelten beim Relayer nicht)")
            return
        if not self.execute:
            self._add(name, "PLAN",
                      "WALLET-CREATE über den Polymarket-Relayer (gasless, "
                      "deterministische CREATE2-Adresse, keine User-Signatur)")
            return
        try:
            relayer = self._relayer()
            txid = relayer.submit_wallet_create()
            info = relayer.wait(txid)
        except DepositWalletError as e:
            raise PreflightError(f"{name}: {e}") from e
        if not self._dw_code_present():
            raise PreflightError(
                f"{name}: Relayer meldet Erfolg, aber an {self.deposit_wallet} "
                "liegt kein Code — nicht weitermachen")
        self.dw_deployed = True
        self._add(name, "AUSGEFÜHRT", "WALLET-CREATE über Relayer bestätigt",
                  tx=str(info.get("transactionHash") or ""))

    def _dw_approval_targets(self) -> list[tuple[str, object, str, tuple]]:
        """Nötige Freigaben AUS dem Deposit Wallet: (Label, Contract, Fn, Args)."""
        from web3 import Web3

        cs = Web3.to_checksum_address
        exchanges = (("CTF Exchange V2", cs(CTF_EXCHANGE_V2_ADDRESS)),
                     ("NegRisk Exchange V2", cs(NEG_RISK_CTF_EXCHANGE_V2_ADDRESS)))
        targets: list[tuple[str, object, str, tuple]] = []
        for label, spender in exchanges:
            targets.append((f"pUSD->{label}", self.pusd, "approve",
                            (spender, MAX_UINT256)))
        for label, operator in exchanges + (
                ("NegRisk Adapter", cs(NEG_RISK_ADAPTER_ADDRESS)),):
            targets.append((f"CTF->{label}", self.ctf, "setApprovalForAll",
                            (operator, True)))
        return targets

    def _dw_missing_approvals(self) -> list[tuple[str, object, str, tuple]]:
        """Fehlende Deposit-Wallet-Freigaben on-chain ermitteln (nur lesen)."""
        missing = []
        for label, contract, fn, args in self._dw_approval_targets():
            try:
                if fn == "approve":
                    current = int(contract.functions.allowance(
                        self.deposit_wallet, args[0]).call())
                    ok = current >= UNLIMITED_ALLOWANCE_THRESHOLD
                else:
                    ok = bool(contract.functions.isApprovedForAll(
                        self.deposit_wallet, args[0]).call())
            except Exception as e:  # noqa: BLE001
                raise PreflightError(
                    f"Deposit-Wallet-Freigabe {label} nicht prüfbar: {e}") from e
            if not ok:
                missing.append((label, contract, fn, args))
        return missing

    def _step_deposit_approvals(self) -> None:
        """Freigaben aus dem Deposit Wallet — als signierter Relayer-Batch."""
        name = "Deposit-Wallet-Approvals"
        missing = self._dw_missing_approvals()
        if not missing:
            self._add(name, "OK", "alle Freigaben bereits gesetzt")
            return
        labels = ", ".join(label for label, *_ in missing)
        if not self.execute:
            self._add(name, "PLAN",
                      f"{len(missing)} Freigaben als EIP-712-Batch über den "
                      f"Relayer setzen: {labels}")
            return
        if not self.dw_deployed or not self.relayer_api_key:
            # Ohne Wallet/Key kann der Batch nicht raus; der Deploy-Schritt
            # hat die konkrete Anweisung bereits als FEHLT gemeldet.
            self._add(name, "FEHLT",
                      f"{len(missing)} Freigaben offen ({labels}) — erst "
                      "Deployment/POLY_RELAYER_API_KEY klären")
            return
        calls = []
        for label, contract, fn, args in missing:
            data = contract.encode_abi(fn, args=list(args))
            # Simulation aus Wallet-Sicht: eth_call mit from=DepositWallet
            # beweist, dass die Call-Data am Ziel-Contract nicht revertet.
            try:
                self.w3.eth.call({"to": contract.address,
                                  "from": self.deposit_wallet, "data": data})
            except Exception as e:  # noqa: BLE001
                raise PreflightError(
                    f"{name}: Simulation {label} revertet: {e}") from e
            calls.append({"target": contract.address, "value": 0, "data": data})
        try:
            relayer = self._relayer()
            nonce = relayer.get_nonce("WALLET")
            deadline = int(time.time()) + self.BATCH_DEADLINE_S
            signature = sign_wallet_batch(self.account, POLYGON_CHAIN_ID,
                                          self.deposit_wallet, nonce, deadline,
                                          calls)
            txid = relayer.submit_wallet_batch(self.deposit_wallet, nonce,
                                               deadline, calls, signature)
            info = relayer.wait(txid)
        except DepositWalletError as e:
            raise PreflightError(f"{name}: {e}") from e
        still = self._dw_missing_approvals()
        if still:
            raise PreflightError(
                f"{name}: Batch bestätigt, aber Freigaben fehlen weiterhin: "
                + ", ".join(label for label, *_ in still))
        self._add(name, "AUSGEFÜHRT", f"Batch gesetzt: {labels}",
                  tx=str(info.get("transactionHash") or ""))

    def _step_deposit_funding(self) -> None:
        """pUSD vom EOA in das Deposit Wallet transferieren (Buying-Power)."""
        name = "pUSD->Deposit-Wallet"
        try:
            amount = int(self.pusd.functions.balanceOf(
                self.account.address).call())
        except Exception as e:  # noqa: BLE001
            raise PreflightError(f"{name}: EOA-pUSD nicht lesbar: {e}") from e
        if amount < MIN_FUNDING_UNITS:
            self._add(name, "OK", "kein pUSD auf dem EOA — Transfer entfällt")
            return
        if not self.execute:
            self._add(name, "PLAN",
                      f"{_fmt(amount)} pUSD per ERC20-Transfer an "
                      f"{self.deposit_wallet} (EOA-pUSD zählt nicht als "
                      "Buying-Power des Deposit Wallets)")
            return
        if not self.dw_deployed:
            self._add(name, "FEHLT",
                      f"{_fmt(amount)} pUSD warten auf das Deployment — erst "
                      "Deposit Wallet anlegen, dann transferieren")
            return
        # Simulation vor dem Senden: der Transfer darf nicht reverten.
        try:
            data = self.pusd.encode_abi("transfer",
                                        args=[self.deposit_wallet, amount])
            self.w3.eth.call({"to": self.pusd.address,
                              "from": self.account.address, "data": data})
        except Exception as e:  # noqa: BLE001
            raise PreflightError(f"{name}: Transfer-Simulation revertet: {e}") from e
        tx = self._send(self.pusd.functions.transfer(self.deposit_wallet, amount),
                        name)
        try:
            dw_balance = int(self.pusd.functions.balanceOf(
                self.deposit_wallet).call())
        except Exception:  # noqa: BLE001 — reine Anzeige, Receipt war schon ok
            dw_balance = None
        self._add(name, "AUSGEFÜHRT",
                  f"{_fmt(amount)} pUSD transferiert — Deposit Wallet hält "
                  f"jetzt {_fmt(dw_balance)}", tx=tx)

    # ---- Stufe 7: CLOB-Anbindung ----------------------------------------------------

    def _step_clob(self) -> None:
        name = "CLOB-Anbindung"
        if not self.execute:
            self._add(name, "PLAN",
                      "create_or_derive_api_key + Adress-Abgleich + Balance-Sync "
                      "(signature_type=3) laufen bei --execute")
            return
        try:
            if self._clob_factory is not None:
                client = self._clob_factory()
            else:
                from py_clob_client_v2.client import ClobClient

                # Deposit-Wallet-Flow: maker/funder ist das Deposit Wallet,
                # signiert wird mit dem EOA-Key (POLY_1271, signature_type 3).
                client = ClobClient(CLOB_HOST, key=self.private_key,
                                    chain_id=POLYGON_CHAIN_ID,
                                    signature_type=3,
                                    funder=self.deposit_wallet)
            client.set_api_creds(client.create_or_derive_api_key())
            clob_addr = client.get_address()
        except Exception as e:  # noqa: BLE001
            raise PreflightError(f"{name} fehlgeschlagen: {e}") from e
        if clob_addr and str(clob_addr).lower() != self.account.address.lower():
            raise PreflightError(
                f"{name}: CLOB meldet Adresse {clob_addr}, erwartet "
                f"{self.account.address} — falscher Key/Signatur-Typ?")
        # Balance-Sync: nach Einzahlung/Freigaben muss der CLOB seinen
        # Collateral-Cache für das Deposit Wallet aktualisieren (docs:
        # /balance-allowance/update mit signature_type=3).
        sync = ""
        try:
            from py_clob_client_v2.clob_types import (AssetType,
                                                      BalanceAllowanceParams)

            client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            sync = ", Balance-Sync ok"
        except Exception as e:  # noqa: BLE001 — Sync ist nachholbar, kein Abbruch
            log.warning("Balance-Sync (signature_type=3) fehlgeschlagen: %s", e)
            sync = ", Balance-Sync fehlgeschlagen (beim ersten Trade nachholen)"
        self._add(name, "OK", f"API-Key abgeleitet, Adresse {clob_addr}{sync}")

    # ---- Interna ---------------------------------------------------------------------

    def _send(self, fn, label: str) -> str:
        """Transaktion signieren, senden, Receipt verifizieren — oder abbrechen.

        Gas: Schätzung +25% Puffer (GAS_LIMIT_BUFFER), POL-Deckung wird VOR
        dem Senden geprüft. Jeder Fehler (Revert bei der Schätzung, zu wenig
        POL, Receipt status != 1, Timeout) wirft PreflightError — die
        Startstrecke macht danach nichts mehr.
        """
        try:
            addr = self.account.address
            tx = fn.build_transaction({
                "from": addr,
                "nonce": self.w3.eth.get_transaction_count(addr),
                "chainId": POLYGON_CHAIN_ID,
                "gasPrice": self.w3.eth.gas_price,
            })
            if "gas" not in tx:
                tx["gas"] = self.w3.eth.estimate_gas(tx)
            tx["gas"] = int(tx["gas"] * GAS_LIMIT_BUFFER)
            gas_cost = tx["gas"] * tx["gasPrice"]
            balance = self.w3.eth.get_balance(addr)
            if balance < gas_cost:
                raise PreflightError(
                    f"{label}: zu wenig POL für Gas "
                    f"({balance / 1e18:.6f} < {gas_cost / 1e18:.6f} POL)")
            # eth_account signiert ohne 'from' (die Adresse steckt im Key).
            signed = self.account.sign_transaction(
                {k: v for k, v in tx.items() if k != "from"})
            raw = getattr(signed, "raw_transaction", None)
            if raw is None:  # ältere eth-account-Versionen
                raw = signed.rawTransaction
            tx_hash = self.w3.eth.send_raw_transaction(raw)
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=RECEIPT_TIMEOUT_S)
            tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
            if not receipt or int(receipt.get("status") or 0) != 1:
                raise PreflightError(
                    f"{label}: Transaktion {tx_hex} fehlgeschlagen "
                    f"(status={receipt.get('status') if receipt else None})")
            log.info("%s: on-chain bestätigt (tx %s)", label, tx_hex)
            return tx_hex
        except PreflightError:
            raise
        except Exception as e:  # noqa: BLE001 — jede Panne = sauberer Abbruch
            raise PreflightError(f"{label}: {e}") from e

    def _add(self, name: str, status: str, detail: str, tx: str = "") -> None:
        self.steps.append(StepResult(name, status, detail, tx))
        log.info("Preflight [%s] %s: %s", status, name, detail)


# ---- CLI-Anbindung ------------------------------------------------------------------

_STATUS_STYLE = {"OK": "green", "AUSGEFÜHRT": "green", "PLAN": "cyan",
                 "FEHLT": "yellow", "FEHLER": "red"}


def print_report(steps: list[StepResult]) -> None:
    """Abschlussbericht: jede Stufe mit Status, Details und Tx-Hash."""
    table = Table(title="Preflight-Bericht")
    for col in ("Stufe", "Status", "Details", "Tx"):
        table.add_column(col)
    for s in steps:
        style = _STATUS_STYLE.get(s.status, "")
        table.add_row(s.name, f"[{style}]{s.status}[/{style}]" if style else s.status,
                      s.detail, s.tx)
    console.print(table)


def cmd_preflight(cfg: BotConfig, execute: bool = False) -> list[StepResult]:
    """CLI-Einstieg: Strecke laufen lassen, Bericht drucken, Exit-Code setzen.

    Der Signatur-Typ wird bewusst roh aus der Umgebung gelesen (nicht über
    cfg.signature_type, dessen Default 2 ist): die Go-Live-Checkliste soll
    ein EXPLIZIT gesetztes POLY_SIGNATURE_TYPE=3 (Deposit-Wallet-Flow)
    einfordern.
    """
    console.print("[bold]Preflight (Go-Live, Deposit-Wallet-Flow)[/bold] — "
                  + ("[red]EXECUTE: Transaktionen werden gesendet[/red]"
                     if execute else
                     "[green]Plan-Modus: es wird nichts gesendet[/green]"))
    pf = Preflight(cfg.private_key,
                   signature_type=os.environ.get("POLY_SIGNATURE_TYPE", ""),
                   execute=execute)
    steps = pf.run()
    print_report(steps)
    bad = [s for s in steps if s.status in ("FEHLT", "FEHLER")]
    if bad:
        console.print(f"[bold red]Preflight unvollständig: "
                      f"{', '.join(s.name for s in bad)}[/bold red]")
        raise SystemExit(1)
    if not execute and any(s.status == "PLAN" for s in steps):
        console.print("[bold]Plan geprüft — Ausführung mit: "
                      "python -m polybot.main preflight --execute[/bold]")
    return steps
