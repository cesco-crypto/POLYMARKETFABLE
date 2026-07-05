"""On-Chain-Merges für den Live-Modus (Polymarket V2, Polygon).

Kapital-Recycling wie im Paper-Modus: vollständige YES/NO-Paare eines
Binärmarkts werden über ConditionalTokens.mergePositions direkt zu pUSD
verschmolzen; NegRisk-Positionen laufen über den NegRisk Adapter —
mergePositions(conditionId, amount) für das YES/NO-Paar eines Teilmarkts,
convertPositions(marketId, indexSet, amount) für einen vollständigen
NO-Satz (zahlt n-1 pUSD pro Satz). Ein vollständiger YES-Satz hat KEIN
on-chain Pendant — er zahlt erst bei der Auflösung.

Der py-clob-client-v2 bringt dafür keine Helfer mit (er signiert Orders
nur über eth_account, ohne web3) — deshalb direkte Contract-Calls über
web3.py mit minimalen Inline-ABIs.

Robustheit: Der Konstruktor wirft ohne Private Key (Live-only-Komponente);
alle Merge-Methoden werfen dagegen NIE — jeder Fehler (RPC down, Revert,
zu wenig POL für Gas, Receipt-Timeout) wird geloggt und als False gemeldet,
damit der Bot-Loop ungestört weiterläuft.
"""

from __future__ import annotations

import logging
import math
import os

from polybot.config import POLYGON_CHAIN_ID

log = logging.getLogger(__name__)

# Polymarket-V2-Kontrakte auf Polygon (seit dem Exchange-Upgrade 28.04.2026).
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
DEFAULT_RPC_URL = "https://polygon-rpc.com"

# pUSD und Conditional Tokens rechnen in 6 Dezimalstellen.
TOKEN_DECIMALS = 6
# Binärmarkt: Outcome-Partition [YES, NO] als Index-Sets, Root-Collection.
BINARY_PARTITION = [1, 2]
PARENT_COLLECTION_ID = b"\x00" * 32
# Gas-Puffer über der Schätzung (Merges sind zustandsabhängig) und
# Wartezeit auf den Receipt.
GAS_LIMIT_BUFFER = 1.25
RECEIPT_TIMEOUT_S = 120

# Minimale ABI-Snippets — nur die tatsächlich genutzten Funktionen.
CTF_ABI = [
    {
        "name": "mergePositions", "type": "function",
        "stateMutability": "nonpayable", "outputs": [],
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
    },
    {
        "name": "isApprovedForAll", "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "setApprovalForAll", "type": "function",
        "stateMutability": "nonpayable", "outputs": [],
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
    },
]

NEG_RISK_ADAPTER_ABI = [
    {
        "name": "mergePositions", "type": "function",
        "stateMutability": "nonpayable", "outputs": [],
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amount", "type": "uint256"},
        ],
    },
    {
        "name": "convertPositions", "type": "function",
        "stateMutability": "nonpayable", "outputs": [],
        "inputs": [
            {"name": "_marketId", "type": "bytes32"},
            {"name": "_indexSet", "type": "uint256"},
            {"name": "_amount", "type": "uint256"},
        ],
    },
]


class MergeExecutor:
    """Führt Positions-Merges on-chain aus (Live-only-Komponente).

    Erwartet den Private Key des Handels-Wallets (POLY_PRIVATE_KEY) — die
    Gas-Kosten zahlt dieses Wallet in POL. Der RPC-Endpunkt kommt aus
    POLY_RPC_URL (Default: https://polygon-rpc.com). Für Tests kann eine
    fertige web3-Instanz injiziert werden (w3=...).
    """

    def __init__(self, private_key: str | None, rpc_url: str | None = None,
                 chain_id: int = POLYGON_CHAIN_ID, w3=None):
        if not private_key:
            raise ValueError(
                "MergeExecutor ist eine Live-only-Komponente: ohne Private Key "
                "(POLY_PRIVATE_KEY) sind keine on-chain Merges möglich")
        from eth_account import Account
        from web3 import HTTPProvider, Web3

        self.account = Account.from_key(private_key)
        self.chain_id = chain_id
        rpc_url = rpc_url or os.environ.get("POLY_RPC_URL") or DEFAULT_RPC_URL
        self.w3 = w3 if w3 is not None else Web3(HTTPProvider(rpc_url))
        self.collateral_address = Web3.to_checksum_address(PUSD_ADDRESS)
        self.adapter_address = Web3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS)
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        self.adapter = self.w3.eth.contract(
            address=self.adapter_address, abi=NEG_RISK_ADAPTER_ABI)
        # Operator-Freigabe des NegRisk Adapters auf dem CTF — einmal pro
        # Prozess geprüft/gesetzt, danach gecacht.
        self._adapter_approved = False

    # ---- Öffentliche Merge-Operationen (werfen nie) -------------------------

    def merge_pairs(self, condition_id: str, amount: float) -> bool:
        """YES/NO-Paare eines Binärmarkts zu pUSD mergen (1 pUSD pro Paar).

        ConditionalTokens.mergePositions mit pUSD als Collateral, leerer
        Parent-Collection und der Binär-Partition [1, 2]. amount ist die
        Anzahl Paare in Shares (wird auf 6 Dezimalstellen abgerundet).
        """
        cid = self._to_bytes32(condition_id, "conditionId")
        units = self._to_units(amount)
        if cid is None or units <= 0:
            return False
        fn = self.ctf.functions.mergePositions(
            self.collateral_address, PARENT_COLLECTION_ID, cid,
            BINARY_PARTITION, units)
        return self._send(fn, f"CTF-Merge {condition_id[:10]}…")

    def merge_negrisk(self, condition_id: str, amount: float) -> bool:
        """YES/NO-Paar eines NegRisk-Teilmarkts zu pUSD mergen (1 pUSD/Paar).

        NegRisk-Tokens liegen auf dem Wrapped-Collateral des Adapters —
        der Merge MUSS deshalb über NegRiskAdapter.mergePositions laufen
        (der Adapter entpackt zurück zu pUSD). Der Adapter zieht die Tokens
        per transferFrom ein und braucht dafür die Operator-Freigabe.
        """
        cid = self._to_bytes32(condition_id, "conditionId")
        units = self._to_units(amount)
        if cid is None or units <= 0:
            return False
        if not self._ensure_adapter_approval():
            return False
        fn = self.adapter.functions.mergePositions(cid, units)
        return self._send(fn, f"NegRisk-Merge {condition_id[:10]}…")

    def merge_negrisk_no(self, question_ids: list[str], amount: float) -> bool:
        """Vollständigen NO-Satz eines NegRisk-Events zu (n-1) pUSD konvertieren.

        NegRiskAdapter.convertPositions(marketId, indexSet, amount):
        die marketId ist die questionId mit genulltem letzten Byte, das
        letzte Byte der questionId ist der Frage-Index, indexSet die
        Bitmaske aller Indizes. Deckt der Satz ALLE Fragen des Markts ab,
        gibt es (n-1) pUSD pro Satz und keine Rest-Tokens; fehlende Fragen
        kämen als (hier untracked) YES-Tokens zurück — der Aufrufer bucht
        deshalb konservativ nur die (n-1) pUSD.
        """
        units = self._to_units(amount)
        qids = [self._to_bytes32(q, "questionId") for q in question_ids]
        if units <= 0 or len(qids) < 2 or any(q is None for q in qids):
            return False
        prefix = qids[0][:31]
        index_set = 0
        for q in qids:
            if q[:31] != prefix:
                log.error("NegRisk-Convert: questionIds aus verschiedenen "
                          "Märkten — abgebrochen")
                return False
            index_set |= 1 << q[31]
        if index_set.bit_count() != len(qids):
            log.error("NegRisk-Convert: doppelte Frage-Indizes in %d "
                      "questionIds — abgebrochen", len(qids))
            return False
        if not self._ensure_adapter_approval():
            return False
        fn = self.adapter.functions.convertPositions(
            prefix + b"\x00", index_set, units)
        return self._send(
            fn, f"NegRisk-Convert {question_ids[0][:10]}… ({len(qids)} NO)")

    # ---- Interna -------------------------------------------------------------

    def _ensure_adapter_approval(self) -> bool:
        """CTF-Operator-Freigabe für den NegRisk Adapter sicherstellen.

        Wallets, die schon über polymarket.com mit NegRisk gehandelt haben,
        haben die Freigabe in der Regel bereits — dann kostet das genau
        einen View-Call pro Prozess. Fehlt sie, wird sie einmalig gesetzt.
        """
        if self._adapter_approved:
            return True
        try:
            approved = self.ctf.functions.isApprovedForAll(
                self.account.address, self.adapter_address).call()
        except Exception as e:  # noqa: BLE001 — nie in den Bot-Loop werfen
            log.error("NegRisk-Adapter-Freigabe nicht prüfbar: %s", e)
            return False
        if not approved:
            log.info("Setze CTF-Operator-Freigabe für den NegRisk Adapter")
            approved = self._send(
                self.ctf.functions.setApprovalForAll(self.adapter_address, True),
                "CTF-Approval NegRisk-Adapter")
        self._adapter_approved = bool(approved)
        return self._adapter_approved

    @staticmethod
    def _to_units(amount: float) -> int:
        """Shares in On-Chain-Einheiten (6 Dezimalstellen), abgerundet.

        Abrunden (mit Mini-Epsilon gegen Float-Rauschen): lieber ein
        Millionstel Share liegen lassen als einen Revert riskieren, weil
        on-chain minimal weniger liegt als die Buchhaltung glaubt.
        """
        if not amount or amount <= 0 or not math.isfinite(amount):
            return 0
        return int(math.floor(amount * 10**TOKEN_DECIMALS + 1e-3))

    @staticmethod
    def _to_bytes32(hex_id: str, label: str) -> bytes | None:
        """Hex-ID ('0x…', 64 Zeichen) defensiv nach bytes32 wandeln."""
        try:
            raw = bytes.fromhex(str(hex_id).removeprefix("0x"))
        except ValueError:
            raw = b""
        if len(raw) != 32:
            log.error("Ungültige %s: %r", label, hex_id)
            return None
        return raw

    def _send(self, fn, label: str) -> bool:
        """Contract-Call signieren, senden und auf den Receipt warten.

        Wirft NIE. Gas läuft in POL: Preis vom Node (eth_gasPrice), Limit
        aus der Schätzung plus Puffer; reicht das POL-Guthaben nicht, wird
        gar nicht erst gesendet. Ein Revert scheitert bereits bei der
        Gas-Schätzung — billiger als eine fehlgeschlagene Transaktion.
        """
        try:
            addr = self.account.address
            tx = fn.build_transaction({
                "from": addr,
                "nonce": self.w3.eth.get_transaction_count(addr),
                "chainId": self.chain_id,
                "gasPrice": self.w3.eth.gas_price,
            })
            if "gas" not in tx:
                tx["gas"] = self.w3.eth.estimate_gas(tx)
            tx["gas"] = int(tx["gas"] * GAS_LIMIT_BUFFER)
            gas_cost = tx["gas"] * tx["gasPrice"]
            balance = self.w3.eth.get_balance(addr)
            if balance < gas_cost:
                log.error("%s: zu wenig POL für Gas (%.6f < %.6f POL) — "
                          "übersprungen", label, balance / 1e18, gas_cost / 1e18)
                return False
            # eth_account signiert ohne 'from' (die Adresse steckt im Key).
            signed = self.account.sign_transaction(
                {k: v for k, v in tx.items() if k != "from"})
            raw = getattr(signed, "raw_transaction", None)
            if raw is None:  # web3/eth-account < 7 nannten das Feld anders
                raw = signed.rawTransaction
            tx_hash = self.w3.eth.send_raw_transaction(raw)
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=RECEIPT_TIMEOUT_S)
            tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
            if not receipt or int(receipt.get("status") or 0) != 1:
                log.error("%s: Transaktion %s fehlgeschlagen (status=%s)",
                          label, tx_hex,
                          receipt.get("status") if receipt else None)
                return False
            log.info("%s: on-chain bestätigt (tx %s)", label, tx_hex)
            return True
        except Exception as e:  # noqa: BLE001 — nie in den Bot-Loop werfen
            log.error("%s: on-chain fehlgeschlagen: %s", label, e)
            return False
