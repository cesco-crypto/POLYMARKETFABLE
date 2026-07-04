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
from polybot.data.gamma import GammaClient
from polybot.data.orderbook import BookClient
from polybot.execution import make_broker
from polybot.portfolio import Portfolio
from polybot.risk import KillSwitch, RiskManager
from polybot.strategies import REGISTRY
from polybot.strategies.base import MarketSnapshot

console = Console()
log = logging.getLogger("polybot")


def build_snapshot(cfg: BotConfig, gamma: GammaClient, books: BookClient) -> MarketSnapshot:
    s = cfg.strategy
    markets = gamma.active_markets(min_liquidity=s.min_liquidity_usdc, limit=s.max_markets * 2)
    markets = [m for m in markets if m.volume_24h >= s.min_volume_24h_usdc][: s.max_markets]
    negrisk = gamma.negrisk_events(min_liquidity=s.min_liquidity_usdc)

    token_ids: set[str] = set()
    for m in markets:
        token_ids.update((m.yes_token, m.no_token))
    for ev_markets in negrisk.values():
        for m in ev_markets:
            token_ids.update((m.yes_token, m.no_token))

    book_map = books.get_books(list(token_ids))
    return MarketSnapshot(markets=markets, books=book_map, negrisk_events=negrisk)


def tick(cfg: BotConfig, snap: MarketSnapshot, strategies, risk: RiskManager,
         broker, portfolio: Portfolio) -> int:
    signals = []
    for strat in strategies:
        signals.extend(strat.generate(snap))
    approved = risk.filter(signals, portfolio)
    if signals and not approved:
        log.debug("%d Signale erzeugt, alle vom Risk-Manager abgelehnt", len(signals))
    fills = broker.execute(approved, snap.books, portfolio)
    risk.check_daily_loss(portfolio)
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
    portfolio = Portfolio.load()
    broker = make_broker(cfg)
    risk = RiskManager(cfg)
    strategies = [REGISTRY[n](cfg) for n in cfg.strategy.enabled if n in REGISTRY]
    console.print(f"Strategien: {[s.name for s in strategies]}")

    try:
        while True:
            started = time.time()
            try:
                snap = build_snapshot(cfg, gamma, books)
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
            time.sleep(max(0.0, cfg.poll_interval_s - (time.time() - started)))
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
    parser.add_argument("--config", default="config.yaml")
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
