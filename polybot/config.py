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
    # Live-Modus: vollständige YES/NO-Paare und NegRisk-NO-Sätze nach Fills
    # on-chain zu pUSD mergen (Kapital-Recycling wie der Paper-Merge; siehe
    # polybot/onchain.py). Erfordert POL für Gas auf dem Signer-Wallet;
    # Fehler werden nur geloggt und stoppen nie den Bot-Loop.
    live_auto_merge: bool = True
    # Live-Modus: nach einer harten Order-Ablehnung (HTTP 400/403 bzw.
    # success=false) KEINE erneuten Versuche auf demselben Token für so viele
    # Sekunden — der Ablehnungsgrund (Balance, Tick, Markt zu) ändert sich
    # nicht im Sekundentakt, und Wiederholungs-Spam kostet Rate-Limit-Budget.
    # Konfigurationsfehler wie "maker address not allowed" sperren unabhängig
    # davon dauerhaft bis zum Prozessende (siehe execution.LiveBroker).
    order_reject_cooldown_s: float = 60.0
    # Waisen-Detektor (polybot/orphan.py): ungehedgte Einzelbeine nach so
    # vielen Sekunden Schonfrist zum Bid glattstellen; 0 = aus. Die Frist
    # muss das Delayed-Order-Poll-Fenster (15s) plus einen Reconcile-Tick
    # überdauern — sonst würde ein noch schwebendes Gegenbein fälschlich
    # verkauft. Nicht mit aktivem Market Making kombinieren (Inventar!).
    flatten_orphan_grace_s: float = 0.0


@dataclass
class StrategyConfig:
    enabled: list[str] = field(default_factory=lambda: ["complement_arb", "negrisk_arb"])
    # Market Making: gequotet wird NUR in Märkten mit engem Spread (höchstens
    # mm_spread) und hohem Volumen (Top-mm_max_markets nach 24h-Volumen),
    # und immer mindestens 1 Tick HINTER dem Best-Bid/Ask — nie aggressiv,
    # damit jeder Fill ein Maker-Fill ist (Gebühr 0 + Rebate).
    mm_spread: float = 0.02               # max. tolerierter Spread der Kandidaten-Märkte
    mm_size_usdc: float = 25.0
    mm_max_inventory_usdc: float = 100.0
    mm_max_markets: int = 10              # Quotes nur in den Top-N-Märkten nach volume_24h
    # Maker-Rebate-Simulation (Paper): effektive Rebate-Rate analog zur
    # Taker-Formel — rebate = rate * p * (1-p) pro Share auf Maker-Fills.
    # Polymarket zahlt Makern 20-25% der Taker-Fees des Marktes; der Paper-
    # Broker rechnet deshalb tokenspezifisch 0.2 * taker_fee_rate(token)
    # (siehe execution.MAKER_REBATE_SHARE). Dieser Wert hier ist nur der
    # FALLBACK für Tokens ohne bekannte Taker-Rate. Default 0.0 = aus
    # (konservativ: kein simulierter Verdienst, den es live vielleicht nicht gäbe).
    maker_rebate_rate: float = 0.0
    # Ehrliche Fill-Simulation (Paper): live vergehen zwischen Signal und
    # Order-Ankunft ~250ms (Erkennung + Order-RTT, Messung 05.07.2026) —
    # sofortige Paper-Fills gegen denselben Snapshot wären optimistisch.
    # Marketable Signale warten deshalb so viele execute()-Aufrufe in einer
    # Pending-Queue und füllen erst gegen das DANN aktuelle Buch (bei ~0.6s
    # Tick-Kadenz ist 1 Tick ≈ 600ms > 250ms — konservativ).
    # 0 = Sofort-Fill (altes Verhalten, nur für Vergleichsmessungen).
    paper_fill_delay_ticks: int = 1
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
    # In-play-Märkte mit Matching-Delay (Sport/Esports nach Spielbeginn,
    # secondsDelay > 0) für Taker-Strategien meiden. Live-Befund 05.07.2026:
    # das erste Arb-Bein hängt dort sekundenlang im Delay, bis es füllt, ist
    # der Preis des Gegenbeins im Live-Spiel weg -> ungehedgtes Bein bzw.
    # Unwind-Verluste statt Arbitrage. Gilt für Paper UND Live, damit die
    # Paper-Rate nichts zählt, was real nicht einfangbar ist.
    skip_delayed_inplay: bool = True
    # negRisk-Auswahl: ohne Deckel würden die Orderbücher ALLER Events geladen
    # (live gemessen ~70 Events / ~5000 Tokens -> ein Tick dauert länger als
    # poll_interval_s). Nur die Top-N Events nach Summen-Liquidität behalten;
    # Events mit sehr vielen Teilmärkten überspringen — dort fehlt fast immer
    # mindestens ein Buch und negrisk_arb verwirft sie dann ohnehin komplett.
    max_negrisk_events: int = 20
    max_negrisk_submarkets: int = 20
    # Cross-Market-Implikations-Detektor (REINE BEOBACHTUNG, keine Signale):
    # findet in Gamma-Events Marktpaare mit logischer Implikation (Over/
    # Under-Ketten 'O/U X.5', win/reach-final-Paare) und loggt Preis-
    # Verletzungen als kind='implication' ins Opportunity-Log — Messung
    # vor Trade. Kostet pro Tick einen zusätzlichen /events-Abruf.
    detect_implications: bool = True
    # WebSocket-Streaming (modeunabhängig): zwischen den REST-Snapshots läuft
    # ein schneller Inner-Loop, der die live gestreamten Orderbücher gegen die
    # Strategien prüft — Reaktionszeit in Millisekunden statt poll_interval_s.
    # Die REST-Rotation (Markt-Discovery pro Tick) bleibt erhalten; fällt der
    # Stream aus, arbeitet der Bot unverändert über REST weiter.
    use_stream: bool = False
    stream_tick_s: float = 0.5            # Intervall des Inner-Loops
    stream_max_tokens: int = 500          # Abo-Deckel pro WSS-Verbindung
    # Ereignisfenster fürs Stream-Abo: Binärmärkte, deren endDate innerhalb
    # dieses Fensters liegt (laufende/bald endende Live-Ereignisse: Sport-
    # spiele, Esports, Kurzfrist-Krypto), bekommen im WSS-Abo HÖCHSTE
    # Priorität (vor negRisk und Volumen), aufsteigend nach endDate.
    # Messbefunde 04./05.07.2026: Preisverwerfungen ballen sich in
    # Live-Fenstern, und der Profit konzentriert sich auf kurzlebige
    # Crypto-'Up or Down'-5/15-Min- und Esports-Märkte mit endDate < 2h —
    # das Abo-Budget (stream_max_tokens) ist dort der Engpass.
    # 0 = aus (negRisk zuerst, dann nur Volumen).
    stream_event_window_s: float = 2 * 3600.0


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
        if cfg.strategy.stream_event_window_s < 0:
            raise SystemExit("strategy.stream_event_window_s darf nicht negativ "
                             "sein (0 = Ereignisfenster-Priorisierung aus)")
        if cfg.risk.paper_start_cash <= 0:
            raise SystemExit("risk.paper_start_cash muss > 0 sein — ohne Start-Cash "
                             "kann der Paper-Bot nichts kaufen")
        if not 0.0 <= cfg.risk.taker_fee_rate <= 0.1:
            raise SystemExit("risk.taker_fee_rate muss zwischen 0 und 0.1 liegen "
                             "(Polymarket-Maximum ist 0.07)")
        if cfg.strategy.mm_max_markets <= 0:
            raise SystemExit("strategy.mm_max_markets muss > 0 sein")
        if cfg.strategy.paper_fill_delay_ticks < 0:
            raise SystemExit("strategy.paper_fill_delay_ticks darf nicht negativ "
                             "sein (0 = Sofort-Fill ohne Latenz-Verzug)")
        if not 0.0 <= cfg.strategy.maker_rebate_rate <= 0.1:
            raise SystemExit("strategy.maker_rebate_rate muss zwischen 0 und 0.1 liegen "
                             "(realistisch sind 20-25% der Taker-Rate, also <= 0.0175)")
        if cfg.risk.order_reject_cooldown_s < 0:
            raise SystemExit("risk.order_reject_cooldown_s darf nicht negativ "
                             "sein (0 = Cooldown aus)")
        if cfg.risk.flatten_orphan_grace_s < 0:
            raise SystemExit("risk.flatten_orphan_grace_s darf nicht negativ "
                             "sein (0 = Waisen-Detektor aus)")
        if cfg.risk.flatten_orphan_grace_s > 0 \
                and "market_making" in cfg.strategy.enabled:
            raise SystemExit(
                "risk.flatten_orphan_grace_s und market_making schließen sich "
                "aus: MM-Inventar ist absichtlich einbeinig, der Waisen-"
                "Detektor würde es glattstellen")
        cfg.private_key = os.environ.get("POLY_PRIVATE_KEY")
        cfg.funder_address = os.environ.get("POLY_FUNDER_ADDRESS")
        try:
            cfg.signature_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
        except ValueError:
            raise SystemExit("POLY_SIGNATURE_TYPE muss eine Zahl sein "
                             "(0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, "
                             "3=POLY_1271/Deposit-Wallet)") from None
        if cfg.signature_type not in (0, 1, 2, 3):
            raise SystemExit(
                f"POLY_SIGNATURE_TYPE={cfg.signature_type} ist ungültig — "
                "erlaubt: 0 (EOA), 1 (POLY_PROXY), 2 (POLY_GNOSIS_SAFE), "
                "3 (POLY_1271, Deposit-Wallet-Flow)")
        if cfg.mode == "live" and not cfg.private_key:
            raise SystemExit(
                "Live-Modus verlangt POLY_PRIVATE_KEY in der Umgebung (.env). "
                "Zum Testen ohne Risiko: mode: paper"
            )
        return cfg
