"""Polymarket Deposit-Wallet-Flow (Exchange-Upgrade 28.04.2026, POLY_1271).

Seit dem V2-Upgrade lehnt der CLOB Orders mit einem EOA als Maker ab
("maker address not allowed, please use the deposit wallet flow"). Orders
müssen von einem DEPOSIT WALLET kommen: einem ERC-1967-Proxy, den die
DepositWalletFactory deterministisch (CREATE2) pro Owner-EOA deployt.
Das EOA bleibt der Signer — Orders werden mit signature_type 3 (POLY_1271)
als ERC-7739-gewrappte Signatur signiert, maker = signer = Deposit Wallet.

Quellen (verifiziert am 05.07.2026):
- https://docs.polymarket.com/trading/deposit-wallets.md (Flow, Relayer-API)
- https://github.com/Polymarket/py-builder-relayer-client (Ableitung,
  EIP-712-Batch-Typen; die CREATE2-Konstanten hier sind daraus portiert)
- On-chain-Gegenprobe: factory.predictWalletAddress(bytes32(owner)) liefert
  dieselbe Adresse wie die lokale Beacon-Ableitung (Probe 05.07.2026).

Arbeitsteilung on-chain/Relayer:
- Adresse ableiten:   rein lokal (CREATE2) + eth_call-Gegenprobe — kein Send.
- Wallet deployen:    NUR über den Polymarket-Relayer (factory.deploy ist
                      Operator-gated; Probe: Revert von fremden Adressen).
                      Gasless, braucht aber einen Relayer-API-Key
                      (polymarket.com/settings?tab=api-keys). CLOB-API-Creds
                      werden NICHT akzeptiert (Probe: 401).
- Approvals setzen:   müssen AUS dem Deposit Wallet kommen — als EIP-712-
                      signierter "Batch" über den Relayer (Typ WALLET).
- pUSD einzahlen:     gewöhnlicher ERC20-Transfer an die Wallet-Adresse
                      (eigene Transaktion des EOA, kostet POL-Gas).
"""

from __future__ import annotations

import logging
import time

import requests
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_bytes, to_checksum_address

log = logging.getLogger(__name__)

# ---- Contract-Adressen (Polygon Mainnet, Chain-ID 137) --------------------------
# DepositWalletFactory — deployt die Wallets per CREATE2:
DEPOSIT_WALLET_FACTORY_ADDRESS = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
# Beacon der neuen BeaconProxy-Wallets (factory.BEACON(), on-chain bestätigt):
DEPOSIT_WALLET_BEACON_ADDRESS = "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a"
# Implementation der älteren UUPS-Wallets (vor dem Factory-Upgrade):
DEPOSIT_WALLET_UUPS_IMPL_ADDRESS = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"

# Polymarket-Relayer (gasless Deploy + Wallet-Batches):
RELAYER_URL = "https://relayer-v2.polymarket.com"

# Funktions-Selektor factory.predictWalletAddress(bytes32) — On-Chain-Gegenprobe
# der lokalen Ableitung (openchain.xyz-Lookup + eth_call-Probe 05.07.2026):
PREDICT_WALLET_SELECTOR = "0x04f1d3c7"

# Relayer-Transaktionszustände (py-builder-relayer-client models.py):
RELAYER_SUCCESS_STATES = ("STATE_MINED", "STATE_CONFIRMED")
RELAYER_FAIL_STATES = ("STATE_FAILED", "STATE_INVALID")

# EIP-712-Typen des DepositWallet-Batches (Doku + py-builder-relayer-client):
DEPOSIT_WALLET_BATCH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Call": [
        {"name": "target", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
    ],
    "Batch": [
        {"name": "wallet", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "calls", "type": "Call[]"},
    ],
}

# ---- CREATE2-Ableitung (portiert aus py-builder-relayer-client, derive.py) ------
# Die Konstanten sind die Solady-ERC1967-Proxy-Initcode-Fragmente; das
# Regressions-Testpaar (Owner unseres Live-Wallets -> on-chain bestätigte
# Deposit-Adresse) sichert die Portierung ab.
_ERC1967_CONST1 = "0xcc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3"
_ERC1967_CONST2 = "0x5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076"
_ERC1967_PREFIX = 0x61003D3D8160233D3973
_ERC1967_BEACON_CONST1 = "0xb3582b35133d50545afa5036515af43d6000803e604d573d6000fd5b3d6000f3"
_ERC1967_BEACON_CONST2 = "0x1b60e01b36527fa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6c"
_ERC1967_BEACON_CONST3 = "0x60195155f3363d3d373d3d363d602036600436635c60da"
_ERC1967_BEACON_PREFIX = 0x6100523D8160233D3973


class DepositWalletError(Exception):
    """Fehler im Deposit-Wallet-/Relayer-Flow (Auth, Ablehnung, Timeout)."""


def _create2_address(factory: str, salt: bytes, init_code_hash: bytes) -> str:
    digest = keccak(b"\xff" + to_bytes(hexstr=factory) + salt + init_code_hash)
    return to_checksum_address("0x" + digest[-20:].hex())


def _deposit_wallet_args(owner: str, factory: str) -> bytes:
    """Konstruktor-Argumente des Wallet-Proxys: (factory, walletId).

    walletId ist per Konvention die Owner-Adresse als bytes32 (links mit
    Nullen aufgefüllt) — dadurch ist die Wallet-Adresse pro Owner eindeutig.
    """
    wallet_id = to_bytes(hexstr=to_checksum_address(owner)).rjust(32, b"\x00")
    return abi_encode(["address", "bytes32"], [to_checksum_address(factory), wallet_id])


def derive_uups_deposit_wallet(
        owner: str,
        factory: str = DEPOSIT_WALLET_FACTORY_ADDRESS,
        implementation: str = DEPOSIT_WALLET_UUPS_IMPL_ADDRESS) -> str:
    """Adresse der (älteren) UUPS-Variante des Deposit Wallets."""
    args = _deposit_wallet_args(owner, factory)
    n = len(args)
    init_code = (
        (_ERC1967_PREFIX + (n << 56)).to_bytes(10, "big")
        + to_bytes(hexstr=to_checksum_address(implementation))
        + to_bytes(hexstr="0x6009")
        + to_bytes(hexstr=_ERC1967_CONST2)
        + to_bytes(hexstr=_ERC1967_CONST1)
        + args
    )
    return _create2_address(factory, keccak(args), keccak(init_code))


def derive_beacon_deposit_wallet(
        owner: str,
        factory: str = DEPOSIT_WALLET_FACTORY_ADDRESS,
        beacon: str = DEPOSIT_WALLET_BEACON_ADDRESS) -> str:
    """Adresse der (aktuellen) BeaconProxy-Variante des Deposit Wallets."""
    args = _deposit_wallet_args(owner, factory)
    n = len(args)
    init_code = (
        (_ERC1967_BEACON_PREFIX + (n << 56)).to_bytes(10, "big")
        + to_bytes(hexstr=to_checksum_address(beacon))
        + to_bytes(hexstr=_ERC1967_BEACON_CONST3)
        + to_bytes(hexstr=_ERC1967_BEACON_CONST2)
        + to_bytes(hexstr=_ERC1967_BEACON_CONST1)
        + args
    )
    return _create2_address(factory, keccak(args), keccak(init_code))


def resolve_deposit_wallet(w3, owner: str) -> str:
    """Deposit-Wallet-Adresse eines Owners bestimmen (Auflösung wie das SDK).

    Alte UUPS-Wallets behalten ihre Adresse: ist an der UUPS-Adresse schon
    Code, gilt sie — sonst die BeaconProxy-Adresse (Neuanlage). Zusätzlich
    wird die lokale Ableitung per eth_call gegen
    factory.predictWalletAddress(bytes32(owner)) verifiziert; eine Abweichung
    ist ein harter Fehler (dorthin fließt Kapital!).
    """
    uups = derive_uups_deposit_wallet(owner)
    try:
        deployed_uups = len(w3.eth.get_code(uups) or b"") > 0
    except Exception as e:  # noqa: BLE001 — ohne Code-Check keine Alt-Wallet-Erkennung
        raise DepositWalletError(f"eth_getCode({uups}) fehlgeschlagen: {e}") from e
    local = uups if deployed_uups else derive_beacon_deposit_wallet(owner)
    predicted = predict_wallet_address_onchain(w3, owner)
    if predicted is not None and not deployed_uups \
            and predicted.lower() != local.lower():
        raise DepositWalletError(
            f"Ableitung uneinig: lokal {local}, Factory meldet {predicted} — "
            "NICHT einzahlen, Ableitung prüfen")
    return local


def predict_wallet_address_onchain(w3, owner: str) -> str | None:
    """factory.predictWalletAddress(bytes32(owner)) per eth_call (Gegenprobe).

    Rückgabe None, wenn der Call scheitert (RPC-Ausfall o.Ä.) — die lokale
    Ableitung bleibt dann unbestätigt, was der Aufrufer berichten kann.
    """
    wallet_id = to_bytes(hexstr=to_checksum_address(owner)).rjust(32, b"\x00")
    data = PREDICT_WALLET_SELECTOR + wallet_id.hex()
    try:
        raw = w3.eth.call({
            "to": to_checksum_address(DEPOSIT_WALLET_FACTORY_ADDRESS),
            "data": data,
        })
        raw_hex = raw.hex() if hasattr(raw, "hex") else str(raw)
        return to_checksum_address("0x" + raw_hex.replace("0x", "")[-40:])
    except Exception as e:  # noqa: BLE001 — Gegenprobe ist optional
        log.warning("predictWalletAddress-Gegenprobe fehlgeschlagen: %s", e)
        return None


def sign_wallet_batch(account, chain_id: int, wallet: str, nonce: int,
                      deadline: int, calls: list[dict]) -> str:
    """EIP-712-Signatur des DepositWallet-Batches (Domain = das Wallet selbst).

    calls: [{"target": addr, "value": int, "data": bytes|hex}, ...] — genau
    die Liste, die auch an den Relayer geht. Signiert das Owner-EOA.
    """
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    def _data_bytes(d):
        return d if isinstance(d, bytes) else to_bytes(hexstr=d)

    full_message = {
        "primaryType": "Batch",
        "types": DEPOSIT_WALLET_BATCH_TYPES,
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": wallet,
        },
        "message": {
            "wallet": wallet,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": [{"target": c["target"], "value": int(c["value"]),
                       "data": _data_bytes(c["data"])} for c in calls],
        },
    }
    signed = Account.sign_message(encode_typed_data(full_message=full_message),
                                  private_key=account.key)
    return "0x" + signed.signature.hex()


class DepositWalletRelayer:
    """Minimaler Client für den Polymarket-Relayer (nur Deposit-Wallet-Pfade).

    Auth: Relayer-API-Key aus der Polymarket-UI (Settings > API Keys) plus
    die Adresse des Key-Besitzers — Header RELAYER_API_KEY /
    RELAYER_API_KEY_ADDRESS (docs: trading/gasless). Die GET-Endpunkte
    (/nonce, /deployed, /transaction) sind ohne Auth abrufbar.
    """

    def __init__(self, api_key: str, owner_address: str,
                 base_url: str = RELAYER_URL, timeout: int = 30):
        self.api_key = api_key
        self.owner = owner_address
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---- ohne Auth ---------------------------------------------------------

    def get_nonce(self, tx_type: str = "WALLET") -> int:
        r = requests.get(f"{self.base_url}/nonce",
                         params={"address": self.owner, "type": tx_type},
                         timeout=self.timeout)
        self._check(r, "GET /nonce")
        return int(r.json()["nonce"])

    def get_transaction(self, transaction_id: str) -> dict:
        r = requests.get(f"{self.base_url}/transaction",
                         params={"id": transaction_id}, timeout=self.timeout)
        self._check(r, "GET /transaction")
        payload = r.json()
        if isinstance(payload, list):  # der Relayer liefert eine Liste
            return payload[0] if payload else {}
        return payload if isinstance(payload, dict) else {}

    # ---- mit Auth ------------------------------------------------------------

    def _headers(self) -> dict:
        return {"RELAYER_API_KEY": self.api_key,
                "RELAYER_API_KEY_ADDRESS": self.owner}

    def _submit(self, body: dict, label: str) -> str:
        r = requests.post(f"{self.base_url}/submit", json=body,
                          headers=self._headers(), timeout=self.timeout)
        if r.status_code == 401:
            raise DepositWalletError(
                f"{label}: Relayer-Auth abgelehnt (401) — POLY_RELAYER_API_KEY "
                "prüfen (Key unter polymarket.com/settings?tab=api-keys mit "
                "genau diesem Wallet erzeugen; CLOB-API-Creds gelten NICHT)")
        self._check(r, label)
        txid = (r.json() or {}).get("transactionID", "")
        if not txid:
            raise DepositWalletError(f"{label}: Relayer-Antwort ohne "
                                     f"transactionID: {r.text[:200]}")
        return txid

    def submit_wallet_create(self) -> str:
        """Deposit Wallet deployen lassen (gasless; keine User-Signatur nötig)."""
        return self._submit({
            "type": "WALLET-CREATE",
            "from": self.owner,
            "to": DEPOSIT_WALLET_FACTORY_ADDRESS,
        }, "WALLET-CREATE")

    def submit_wallet_batch(self, wallet: str, nonce: int, deadline: int,
                            calls: list[dict], signature: str) -> str:
        """Signierten Call-Batch aus dem Deposit Wallet ausführen lassen."""
        def _data_hex(d):
            return d if isinstance(d, str) else "0x" + d.hex()

        return self._submit({
            "type": "WALLET",
            "from": self.owner,
            "to": DEPOSIT_WALLET_FACTORY_ADDRESS,
            "nonce": str(nonce),
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": wallet,
                "deadline": str(deadline),
                "calls": [{"target": c["target"], "value": str(c["value"]),
                           "data": _data_hex(c["data"])} for c in calls],
            },
        }, "WALLET-Batch")

    def wait(self, transaction_id: str, timeout_s: float = 180.0,
             poll_s: float = 2.0) -> dict:
        """Auf den Endzustand einer Relayer-Transaktion warten.

        Rückgabe: der Transaktions-Datensatz (inkl. transactionHash) bei
        Erfolg; DepositWalletError bei STATE_FAILED/STATE_INVALID/Timeout.
        """
        deadline = time.monotonic() + timeout_s
        last: dict = {}
        while time.monotonic() < deadline:
            try:
                last = self.get_transaction(transaction_id)
            except DepositWalletError as e:
                log.warning("Relayer-Statusabfrage %s: %s", transaction_id, e)
                last = {}
            state = str(last.get("state") or "")
            if state in RELAYER_SUCCESS_STATES:
                return last
            if state in RELAYER_FAIL_STATES:
                raise DepositWalletError(
                    f"Relayer-Transaktion {transaction_id} endete als {state}")
            time.sleep(poll_s)
        raise DepositWalletError(
            f"Relayer-Transaktion {transaction_id} nach {timeout_s:.0f}s nicht "
            f"final (zuletzt: {last.get('state') or 'unbekannt'})")

    @staticmethod
    def _check(r, label: str) -> None:
        if r.status_code != 200:
            raise DepositWalletError(f"{label}: HTTP {r.status_code} — "
                                     f"{r.text[:200]}")
