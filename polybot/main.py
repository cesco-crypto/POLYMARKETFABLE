"""CLI-Einstiegspunkt.

  python -m polybot.main scan            # einmalig nach Arbitrage suchen
  python -m polybot.main run             # Bot-Loop (Modus laut config.yaml)
  python -m polybot.main run --config config.yaml
  python -m polybot.main status          # Paper-Portfolio anzeigen
"""

from __future__ import annotations

import argparse
import logging
import time

from rich.console import Console
from rich.table import Table

from polybot.config import BotConfig
from polybot.data.fees import FeeRateCache
from polybot.data.gamma import GammaClient, Market
from polybot.data.orderbook import BookClient, Level, OrderBook
from polybot.execution import make_broker
from polybot.portfolio import Portfolio
from polybot.risk import KillSwitch, RiskManager
from polybot.strategies import REGISTRY
from polybot.strategies.base import MarketSnapshot

console = Console()
log = logging.getLogger("polybot")

# Zweistufen-Scan: volle Bücher nur für Märkte laden, deren Top-of-Book-Summe
# höchstens so weit über der Arb-Schwelle liegt. Der Puffer (2 Ticks à 0.01)
# fängt Preisbewegungen zwischen Stufe 1 (Batch-Preise) und Stufe 2 ab.
PREFILTER_MARGIN = 0.02


def _candidate_tokens(markets: list[Market],
                      negrisk: dict[str, list[Market]],
                      asks: dict[str, float]) -> set[str]:
    """Tokens mit Arb-Verdacht laut Top-of-Book — nur deren Bücher lohnen sich.

    Binärmarkt: YES-Ask + NO-Ask < 1 + Puffer. NegRisk-Event: Summe der
    YES-Asks < 1 + Puffer oder Summe der NO-Asks < (n-1) + Puffer; fehlt ein
    Ask, ist das Event unvollständig und negrisk_arb verwirft es ohnehin.
    """
    out: set[str] = set()
    for m in markets:
        ya, na = asks.get(m.yes_token), asks.get(m.no_token)
        if ya is not None and na is not None and ya + na < 1.0 + PREFILTER_MARGIN:
            out.update((m.yes_token, m.no_token))
    for ev_markets in negrisk.values():
        yes = [asks.get(m.yes_token) for m in ev_markets]
        no = [asks.get(m.no_token) for m in ev_markets]
        if None in yes or None in no:
            continue
        if (sum(yes) < 1.0 + PREFILTER_MARGIN
                or sum(no) < (len(ev_markets) - 1) + PREFILTER_MARGIN):
            out.update(t for m in ev_markets for t in (m.yes_token, m.no_token))
    return out


def _load_books(cfg: BotConfig, books: BookClient, token_ids: set[str],
                markets: list[Market],
                negrisk: dict[str, list[Market]]) -> dict[str, OrderBook]:
    """Orderbücher laden — zweistufig, wenn möglich.

    Stufe 1: Batch-Preise (POST /prices, 200 Tokens/Request) für alle Tokens;
    Stufe 2: volle Bücher (POST /books, 50 Tokens/Request) nur für Kandidaten.
    Nicht-Kandidaten bekommen ein synthetisches Top-of-Book (Größe 0), damit
    Marks/Kill-Switch weiter Midpoints sehen; Strategien verwerfen sie über
    die Mindestgröße. Fallbacks auf den vollen Pfad: Market Making braucht
    echte Tiefe in ALLEN Märkten; Book-Clients ohne get_top_prices (Test-
    Fakes) und ein Komplettausfall der Batch-Preise ebenso.
    """
    if "market_making" in cfg.strategy.enabled or not hasattr(books, "get_top_prices"):
        return books.get_books(list(token_ids))
    top = books.get_top_prices(list(token_ids))
    if not top:
        return books.get_books(list(token_ids))
    asks = {t: a for t, (_, a) in top.items() if a is not None}
    book_map = books.get_books(list(_candidate_tokens(markets, negrisk, asks)))
    for t in token_ids - set(book_map):
        bid, ask = top.get(t, (None, None))
        if bid is None and ask is None:
            continue
        book_map[t] = OrderBook(
            token_id=t,
            bids=[Level(bid, 0.0)] if bid is not None else [],
            asks=[Level(ask, 0.0)] if ask is not None else [],
        )
    return book_map


def build_snapshot(cfg: BotConfig, gamma: GammaClient, books: BookClient,
                   fees: FeeRateCache | None = None) -> MarketSnapshot:
    fees = fees if fees is not None else FeeRateCache()
    s = cfg.strategy
    markets = gamma.active_markets(min_liquidity=s.min_liquidity_usdc, limit=s.max_markets * 2)
    markets = [m for m in markets if m.volume_24h >= s.min_volume_24h_usdc][: s.max_markets]
    negrisk = gamma.negrisk_events(min_liquidity=s.min_liquidity_usdc)
    # Ohne Deckel würden die Bücher ALLER negRisk-Teilmärkte geladen (live
    # ~5000 Tokens -> ein Tick dauert länger als poll_interval_s): Events mit
    # zu vielen Teilmärkten überspringen (dort fehlt fast immer ein Buch und
    # negrisk_arb verwirft sie komplett), dann Top-N nach Summen-Liquidität.
    negrisk = {slug: ms for slug, ms in negrisk.items()
               if len(ms) <= s.max_negrisk_submarkets}
    negrisk = dict(sorted(negrisk.items(),
                          key=lambda kv: sum(m.liquidity for m in kv[1]),
                          reverse=True)[: s.max_negrisk_events])

    token_ids: set[str] = set()
    for m in markets:
        token_ids.update((m.yes_token, m.no_token))
    for ev_markets in negrisk.values():
        for m in ev_markets:
            token_ids.update((m.yes_token, m.no_token))

    book_map = books.get_books(list(token_ids))
    # Tokenspezifische Taker-Fee-Raten (kategorieabhängig) aus den Gamma-
    # Marktobjekten, über Ticks gecacht; Tokens ohne bekannte Rate fehlen im
    # Dict und fallen auf cfg.risk.taker_fee_rate zurück.
    fees.update_from_markets(markets)
    for ev_markets in negrisk.values():
        fees.update_from_markets(ev_markets)
    return MarketSnapshot(markets=markets, books=book_map, negrisk_events=negrisk,
                          fee_rates=fees.rates_for(token_ids))


def merge_positions(snap: MarketSnapshot, portfolio: Portfolio) -> float:
    """Komplement-Paare und vollständige NegRisk-Sets zu USDC mergen.

    Paper-Pendant zum on-chain CTF-Merge: Arbitragegewinne werden sofort
    realisiert statt bis zur Auflösung im Portfolio zu liegen. Rückgabe:
    Anzahl gemergter Paare/Sets (für Logging/Tests).
    """
    merged = 0.0
    for m in snap.markets:
        merged += portfolio.merge_pairs(m.yes_token, m.no_token)
    for ev_markets in snap.negrisk_events.values():
        merged += portfolio.merge_negrisk_yes([m.yes_token for m in ev_markets])
        merged += portfolio.merge_negrisk_no([m.no_token for m in ev_markets],
                                             len(ev_markets))
    return merged


def tick(cfg: BotConfig, snap: MarketSnapshot, strategies, risk: RiskManager,
         broker, portfolio: Portfolio) -> int:
    snap.portfolio = portfolio  # Inventar-Sicht für Strategien (Market Making)
    # Marks (Midpoints) für den Kill-Switch: ohne sie wären unrealisierte
    # Verluste unsichtbar. Prüfung VOR der Ausführung, damit im Breach-Tick
    # keine neuen Orders mehr rausgehen — und danach noch einmal.
    marks = {t: b.midpoint for t, b in snap.books.items() if b.midpoint}
    risk.check_daily_loss(portfolio, marks)
    signals = []
    for strat in strategies:
        signals.extend(strat.generate(snap))
    approved = risk.filter(signals, portfolio)
    if signals and not approved:
        log.debug("%d Signale erzeugt, alle vom Risk-Manager abgelehnt", len(signals))
    fills = broker.execute(approved, snap.books, portfolio, snap.fee_rates)
    if cfg.mode != "live":
        # Paper-Modus: frisch gefüllte Arb-Paare/Sets sofort zu USDC mergen —
        # live wäre das ein on-chain CTF-Merge und Sache des LiveBrokers.
        merge_positions(snap, portfolio)
    risk.check_daily_loss(portfolio, marks)
    return fills


def cmd_scan(cfg: BotConfig) -> None:
    """Einmaliger Scan: aktuelle Arbitrage-Gelegenheiten anzeigen, nichts handeln."""
    gamma, books = GammaClient(), BookClient()
    console.print("[bold]Scanne Polymarket nach Arbitrage-Gelegenheiten...[/bold]")
    snap = build_snapshot(cfg, gamma, books)
    console.print(f"Geladen: {len(snap.markets)} Binärmärkte, "
                  f"{len(snap.negrisk_events)} NegRisk-Events, {len(snap.books)} Orderbücher")

    strategies = [REGISTRY[n](cfg) for n in ("complement_arb", "negrisk_arb")]
    signals = [sig for st in strategies for sig in st.generate(snap)]

    if not signals:
        console.print("[yellow]Aktuell keine Arbitrage über der Edge-Schwelle "
                      f"({cfg.risk.min_edge:.3f}) gefunden — das ist der Normalfall; "
                      "solche Fenster sind kurzlebig.[/yellow]")
        return
    table = Table(title="Gefundene Signale")
    for col in ("Markt", "Seite", "Preis", "Größe", "Grund"):
        table.add_column(col)
    for s in signals:
        table.add_row(s.market_question[:50], s.side, f"{s.price:.3f}", f"{s.size:.0f}", s.reason)
    console.print(table)


def cmd_run(cfg: BotConfig) -> None:
    if cfg.mode == "live":
        console.print("[bold red]LIVE-MODUS: Es wird mit echtem Geld gehandelt![/bold red]")
    else:
        console.print("[bold green]Paper-Modus: Simulation gegen echte Orderbücher.[/bold green]")

    gamma, books = GammaClient(), BookClient()
    # Fee-Cache lebt über den ganzen Lauf: Raten bleiben auch dann bekannt,
    # wenn Gamma die Fee-Info eines Markts in einem Tick nicht mitliefert.
    fees = FeeRateCache()
    portfolio = Portfolio.load()
    broker = make_broker(cfg)
    risk = RiskManager(cfg)
    unknown = [n for n in cfg.strategy.enabled if n not in REGISTRY]
    if unknown:
        raise SystemExit(f"Unbekannte Strategien in strategy.enabled: {unknown} — "
                         f"verfügbar: {sorted(REGISTRY)}")
    strategies = [REGISTRY[n](cfg) for n in cfg.strategy.enabled]
    if not strategies:
        raise SystemExit("strategy.enabled ist leer — mindestens eine Strategie "
                         f"aktivieren (verfügbar: {sorted(REGISTRY)})")
    console.print(f"Strategien: {[s.name for s in strategies]}")

    try:
        while True:
            started = time.time()
            try:
                snap = build_snapshot(cfg, gamma, books, fees)
                fills = tick(cfg, snap, strategies, risk, broker, portfolio)
                marks = {t: b.midpoint for t, b in snap.books.items() if b.midpoint}
                console.print(
                    f"[dim]{time.strftime('%H:%M:%S')}[/dim] "
                    f"Fills: {fills} | Wert: {portfolio.value(marks):.2f} USDC | "
                    f"Tages-PnL: {portfolio.daily_pnl(marks):+.2f} | "
                    f"Exposure: {portfolio.total_exposure():.2f}"
                )
            except KillSwitch as e:
                console.print(f"[bold red]{e}[/bold red]")
                break
            except Exception as e:  # noqa: BLE001 — Netzwerkfehler etc. überleben
                log.error("Tick fehlgeschlagen: %s", e)
            finally:
                portfolio.save()
            elapsed = time.time() - started
            if elapsed > cfg.poll_interval_s:
                log.warning("Tick dauerte %.1fs > Intervall %.1fs — API-Last zu hoch, "
                            "max_markets/max_negrisk_events senken oder Intervall erhöhen",
                            elapsed, cfg.poll_interval_s)
            # Mindestens 1s schlafen: auch bei Überlauf keine lückenlose
            # Anfragekette gegen die API (Rate-Limit-Schutz).
            time.sleep(max(1.0, cfg.poll_interval_s - elapsed))
    except KeyboardInterrupt:
        console.print("Gestoppt. Portfolio gespeichert.")
        portfolio.save()


def cmd_status(cfg: BotConfig) -> None:
    pf = Portfolio.load()
    console.print(f"Cash: {pf.cash:.2f} USDC | realisierter PnL: {pf.realized_pnl:+.2f} | "
                  f"Positionen: {len(pf.positions)} | Fills: {len(pf.fills)}")
    if pf.positions:
        table = Table(title="Offene Positionen")
        for col in ("Token", "Shares", "Einstand (USDC)"):
            table.add_column(col)
        for p in pf.positions.values():
            table.add_row(p.token_id[:16] + "…", f"{p.shares:.1f}", f"{p.cost_basis:.2f}")
        console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(prog="polybot")
    parser.add_argument("command", choices=["scan", "run", "status"])
    # default=None: BotConfig.load unterscheidet so zwischen explizit gesetztem
    # --config (Datei MUSS existieren) und implizitem config.yaml-Fallback.
    parser.add_argument("--config", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = BotConfig.load(args.config)
    {"scan": cmd_scan, "run": cmd_run, "status": cmd_status}[args.command](cfg)


if __name__ == "__main__":
    main()
