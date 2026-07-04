"""Konfiguration: YAML-Datei + Umgebungsvariablen (.env)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

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
    # Start-Cash des Paper-Portfolios in USDC — greift nur beim ERSTEN Anlegen
    # (wenn noch kein paper_state.json existiert); ein bestehender State behält
    # sein Cash. Skalierung der übrigen Limits nicht vergessen (max_order etc.).
    paper_start_cash: float = 1000.0
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
    # Maker-Rebate-Simulation (Paper): effektive Rebate-Rate analog zur
    # Taker-Formel — rebate = rate * p * (1-p) pro Share auf Maker-Fills.
    # Polymarket zahlt Makern 20-25% der Taker-Fees (Taker-Rate 0.00-0.07),
    # realistisch also ~0.0-0.0175. Default 0.0 = aus (konservativ: kein
    # simulierter Verdienst, den es live vielleicht nicht gäbe).
    maker_rebate_rate: float = 0.0
    # Marktauswahl
    min_liquidity_usdc: float = 10_000.0
    min_volume_24h_usdc: float = 5_000.0
    max_markets: int = 50
    # Vollmarkt-Scan: ALLE aktiven Binärmärkte über min_liquidity_usdc laden
    # (Gamma-Fenster-Pagination) statt nur die Top-max_markets. Die Bücher
    # werden dann zweistufig geladen: Batch-Top-of-Book für alle Tokens,
    # volle Bücher nur für Arb-Kandidaten. Ein Tick dauert damit deutlich
    # länger (live gemessen 45-70s) — poll_interval_s entsprechend erhöhen.
    scan_all_markets: bool = False
    # Mindestgröße einer Order in Shares (Polymarket-Minimum ist meist 5);
    # Signale unterhalb dieser Größe werden von den Strategien verworfen.
    min_order_shares: float = 5.0
    # Mindest-Restlaufzeit eines Marktes in Sekunden. Nach endDate bleiben
    # Bücher stale und untradeable (Validierungs-Befund 04.07.2026) — und
    # kurz vor dem Ende ist das Auflösungs-/Reject-Risiko am höchsten.
    min_time_to_end_s: float = 120.0
    # negRisk-Auswahl: ohne Deckel würden die Orderbücher ALLER Events geladen
    # (live gemessen ~70 Events / ~5000 Tokens -> ein Tick dauert länger als
    # poll_interval_s). Nur die Top-N Events nach Summen-Liquidität behalten;
    # Events mit sehr vielen Teilmärkten überspringen — dort fehlt fast immer
    # mindestens ein Buch und negrisk_arb verwirft sie dann ohnehin komplett.
    max_negrisk_events: int = 20
    max_negrisk_submarkets: int = 20
    # WebSocket-Streaming (modeunabhängig): zwischen den REST-Snapshots läuft
    # ein schneller Inner-Loop, der die live gestreamten Orderbücher gegen die
    # Strategien prüft — Reaktionszeit in Millisekunden statt poll_interval_s.
    # Die REST-Rotation (Markt-Discovery pro Tick) bleibt erhalten; fällt der
    # Stream aus, arbeitet der Bot unverändert über REST weiter.
    use_stream: bool = False
    stream_tick_s: float = 0.5            # Intervall des Inner-Loops
    stream_max_tokens: int = 500          # Abo-Deckel pro WSS-Verbindung


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
        """Lädt die Config.

        path=None bedeutet: implizit config.yaml probieren, Fehlen tolerieren
        (Defaults). Ein EXPLIZIT übergebener Pfad muss existieren — ein
        Tippfehler in --config darf den Bot nicht stillschweigend mit
        Default-Risikolimits starten.
        """
        raw: dict = {}
        if path is not None:
            if not Path(path).exists():
                raise SystemExit(f"Config-Datei {path} nicht gefunden")
            raw = yaml.safe_load(Path(path).read_text()) or {}
        elif Path("config.yaml").exists():
            raw = yaml.safe_load(Path("config.yaml").read_text()) or {}
        else:
            log.info("Keine config.yaml gefunden — Built-in-Defaults werden verwendet")

        def section(key: str, section_cls):
            # 'risk:' ohne Wert liefert None (nicht {}) — 'or {}' fängt das ab.
            data = raw.get(key) or {}
            try:
                return section_cls(**data)
            except TypeError as e:
                raise SystemExit(
                    f"Ungültiger Config-Abschnitt '{key}': {e}"
                ) from e

        cfg = cls(
            mode=raw.get("mode", "paper"),
            poll_interval_s=float(raw.get("poll_interval_s", 10.0)),
            risk=section("risk", RiskConfig),
            strategy=section("strategy", StrategyConfig),
        )
        if cfg.poll_interval_s <= 0:
            raise SystemExit("poll_interval_s muss > 0 sein")
        for name in ("max_order_usdc", "max_position_usdc",
                     "max_total_exposure_usdc", "daily_loss_limit_usdc"):
            if getattr(cfg.risk, name) < 0:
                raise SystemExit(f"risk.{name} darf nicht negativ sein")
        if cfg.strategy.min_order_shares < 0:
            raise SystemExit("strategy.min_order_shares darf nicht negativ sein")
        if cfg.strategy.stream_tick_s <= 0:
            raise SystemExit("strategy.stream_tick_s muss > 0 sein")
        if cfg.strategy.stream_max_tokens <= 0:
            raise SystemExit("strategy.stream_max_tokens muss > 0 sein")
        if cfg.risk.paper_start_cash <= 0:
            raise SystemExit("risk.paper_start_cash muss > 0 sein — ohne Start-Cash "
                             "kann der Paper-Bot nichts kaufen")
        if not 0.0 <= cfg.risk.taker_fee_rate <= 0.1:
            raise SystemExit("risk.taker_fee_rate muss zwischen 0 und 0.1 liegen "
                             "(Polymarket-Maximum ist 0.07)")
        if not 0.0 <= cfg.strategy.maker_rebate_rate <= 0.1:
            raise SystemExit("strategy.maker_rebate_rate muss zwischen 0 und 0.1 liegen "
                             "(realistisch sind 20-25% der Taker-Rate, also <= 0.0175)")
        cfg.private_key = os.environ.get("POLY_PRIVATE_KEY")
        cfg.funder_address = os.environ.get("POLY_FUNDER_ADDRESS")
        cfg.signature_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
        if cfg.mode == "live" and not cfg.private_key:
            raise SystemExit(
                "Live-Modus verlangt POLY_PRIVATE_KEY in der Umgebung (.env). "
                "Zum Testen ohne Risiko: mode: paper"
            )
        return cfg
