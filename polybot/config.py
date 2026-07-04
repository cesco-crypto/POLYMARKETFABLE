"""Konfiguration: YAML-Datei + Umgebungsvariablen (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"
POLYGON_CHAIN_ID = 137


@dataclass
class RiskConfig:
    """Harte Limits — der Bot handelt nie darüber hinaus."""

    max_order_usdc: float = 50.0          # max. Größe einer einzelnen Order
    max_position_usdc: float = 200.0      # max. Exposure pro Markt
    max_total_exposure_usdc: float = 500.0  # max. Gesamt-Exposure
    daily_loss_limit_usdc: float = 100.0  # Kill-Switch: Tagesverlustgrenze
    min_edge: float = 0.01                # min. Edge (1 Cent) nach Gebühren
    # Taker-Gebühr (seit März 2026): fee = rate * p * (1-p) pro Share.
    # Kategorieabhängig 0.00-0.07. Die Strategien nutzen die tokenspezifische
    # Rate vom CLOB (/fee-rate); dieser Wert ist nur der Fallback, wenn sie
    # nicht abrufbar ist — deshalb das echte Maximum 0.07 (Krypto), damit
    # eine unbekannte Rate die Edge nie überschätzt.
    taker_fee_rate: float = 0.07


@dataclass
class StrategyConfig:
    enabled: list[str] = field(default_factory=lambda: ["complement_arb", "negrisk_arb"])
    # Market Making
    mm_spread: float = 0.02               # Quotierung ±2 Cent um den Mittelkurs
    mm_size_usdc: float = 25.0
    mm_max_inventory_usdc: float = 100.0
    # Marktauswahl
    min_liquidity_usdc: float = 10_000.0
    min_volume_24h_usdc: float = 5_000.0
    max_markets: int = 50


@dataclass
class BotConfig:
    mode: str = "paper"                   # "paper" | "live"
    poll_interval_s: float = 10.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    # Nur für Live-Trading, ausschließlich aus der Umgebung gelesen:
    private_key: str | None = None
    funder_address: str | None = None     # Polymarket-Proxy-Wallet (USDC-Halter)
    signature_type: int = 2               # 2 = Browser-Wallet-Proxy (Standard bei polymarket.com)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "BotConfig":
        raw: dict = {}
        if path and Path(path).exists():
            raw = yaml.safe_load(Path(path).read_text()) or {}
        cfg = cls(
            mode=raw.get("mode", "paper"),
            poll_interval_s=float(raw.get("poll_interval_s", 10.0)),
            risk=RiskConfig(**raw.get("risk", {})),
            strategy=StrategyConfig(**raw.get("strategy", {})),
        )
        cfg.private_key = os.environ.get("POLY_PRIVATE_KEY")
        cfg.funder_address = os.environ.get("POLY_FUNDER_ADDRESS")
        cfg.signature_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
        if cfg.mode == "live" and not cfg.private_key:
            raise SystemExit(
                "Live-Modus verlangt POLY_PRIVATE_KEY in der Umgebung (.env). "
                "Zum Testen ohne Risiko: mode: paper"
            )
        return cfg
