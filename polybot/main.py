"""CLI-Einstiegspunkt.

  python -m polybot.main scan            # einmalig nach Arbitrage suchen
  python -m polybot.main run             # Bot-Loop (Modus laut config.yaml)
  python -m polybot.main run --config config.yaml
  python -m polybot.main status          # Paper-Portfolio anzeigen
  python -m polybot.main report          # Opportunity-Log auswerten (--target)
  python -m polybot.main cycle-report    # kompakte Zyklus-Selbstauswertung
  python -m polybot.main capture-report  # Live/Paper-Schattenvergleich auswerten
  python -m polybot.main preflight       # Go-Live-Startstrecke (EOA) prüfen
  python -m polybot.main preflight --execute  # ... und wirklich ausführen
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from polybot import cycle_report
from polybot.config import BotConfig
from polybot.cycle_report import CycleLedger
from polybot.data.fees import FeeRateCache
from polybot.data.gamma import GammaClient, Market
from polybot.data.orderbook import BookClient, Level, OrderBook
from polybot.data.stream import BookStreamer
from polybot.execution import make_broker
from polybot.orphan import OrphanFlattener
from polybot.portfolio import Portfolio
from polybot.possync import PositionSyncer
from polybot.settlement import SettlementSweeper
from polybot.preflight import cmd_preflight
from polybot.recorder import (OpportunityRecorder, aggregate,
                              load_opportunities, required_capital)
from polybot.shadow import (DEFAULT_SHADOW_PATH, ShadowTracker,
                            aggregate_capture, load_shadow)
from polybot.risk import KillSwitch, RiskManager
from polybot.strategies import REGISTRY
from polybot.strategies.base import MarketSnapshot
from polybot.strategies.implication_detector import (find_implication_pairs,
                                                     pair_tokens)
from polybot.strategies.market_making import candidate_markets as mm_candidate_markets

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
    YES-Asks < 1 + Puffer oder Summe der NO-Asks < (n-1) + Puffer.
    Unvollständige Events (mindestens ein Ask fehlt) verwirft negrisk_arb —
    der Recorder misst dort aber die Teilmengen-NO-Struktur (kind=
    'negrisk_partial_no'): k verfügbare NO-Beine zahlen garantiert >= k-1;
    Kandidat, wenn die Summe ihrer Asks < (k-1) + Puffer liegt. Nur diese
    NO-Beine bekommen dann volle Bücher (echte Tiefen für die Messung).
    """
    out: set[str] = set()
    for m in markets:
        ya, na = asks.get(m.yes_token), asks.get(m.no_token)
        if ya is not None and na is not None and ya + na < 1.0 + PREFILTER_MARGIN:
            out.update((m.yes_token, m.no_token))
    for ev_markets in negrisk.values():
        yes = [asks.get(m.yes_token) for m in ev_markets]
        no = [asks.get(m.no_token) for m in ev_markets]
        if None not in yes and None not in no:
            if (sum(yes) < 1.0 + PREFILTER_MARGIN
                    or sum(no) < (len(ev_markets) - 1) + PREFILTER_MARGIN):
                out.update(t for m in ev_markets for t in (m.yes_token, m.no_token))
            continue
        avail_no = [(m.no_token, a) for m, a in zip(ev_markets, no) if a is not None]
        if (len(avail_no) >= 2
                and sum(a for _, a in avail_no)
                < len(avail_no) - 1 + PREFILTER_MARGIN):
            out.update(t for t, _ in avail_no)
    return out


def _load_books(cfg: BotConfig, books: BookClient, token_ids: set[str],
                markets: list[Market],
                negrisk: dict[str, list[Market]],
                extra_full: set[str] | None = None) -> dict[str, OrderBook]:
    """Orderbücher laden — zweistufig, wenn möglich.

    Stufe 1: Batch-Preise (POST /prices, 200 Tokens/Request) für alle Tokens;
    Stufe 2: volle Bücher (POST /books, 50 Tokens/Request) nur für Kandidaten.
    Kandidaten sind die Arb-Verdachtsfälle plus — bei aktivem Market Making —
    die YES-Tokens der Top-N-MM-Kandidaten (strategies.market_making.
    candidate_markets); volle Bücher für ALLE Märkte würden den Tick massiv
    verlangsamen. Nicht-Kandidaten bekommen ein synthetisches Top-of-Book
    (Größe 0), damit Marks/Kill-Switch weiter Midpoints sehen; Strategien
    verwerfen sie über die Mindestgröße. Fallbacks auf den vollen Pfad:
    Book-Clients ohne get_top_prices (Test-Fakes) und ein Komplettausfall
    der Batch-Preise.
    """
    if not hasattr(books, "get_top_prices"):
        return books.get_books(list(token_ids))
    top = books.get_top_prices(list(token_ids))
    if not top:
        return books.get_books(list(token_ids))
    asks = {t: a for t, (_, a) in top.items() if a is not None}
    wanted = _candidate_tokens(markets, negrisk, asks)
    if "market_making" in cfg.strategy.enabled:
        # Market Making quotet nur YES-Tokens seiner Top-N-Kandidaten —
        # genau dafür braucht es echte Buchtiefe, für mehr nicht.
        wanted.update(m.yes_token for m in mm_candidate_markets(cfg, markets))
    if extra_full:
        # z.B. Implikations-Bein-Tokens: der Detektor braucht echte
        # Ask-Tiefen, synthetische Größe-0-Bücher wären wertlose Messung.
        wanted.update(extra_full)
    book_map = books.get_books(list(wanted))
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


# Deckel für volle Bücher der kurzlebigen Märkte (extra_full in _load_books):
# 300 Tokens ≙ 150 Märkte — deckt alle Up-or-Down/Esports-Fenster eines Ticks,
# ohne den Batch-Books-Pfad zu sprengen (50 Tokens/Request -> +6 Requests).
EXPIRING_FULL_BOOKS_CAP = 300


def _expiring_tokens(markets: list[Market], window_s: float,
                     now: float | None = None,
                     cap: int = EXPIRING_FULL_BOOKS_CAP) -> set[str]:
    """Tokens der kurzlebigen Märkte (endDate im Ereignisfenster), gedeckelt.

    Dieselben Märkte, die das WSS-Abo priorisiert (stream_tokens), bekommen
    im REST-Snapshot volle Bücher statt synthetischem Top-of-Book — sonst
    bleibt die Lebensdauer-Messung des Recorders auf genau diesen Märkten
    zensiert, weil Größe-0-Bücher von Strategien/Detektoren verworfen
    werden. Läuft der Deckel voll, gewinnen die baldigst endenden Märkte
    (end_ts aufsteigend) — dieselbe Rangfolge wie im Abo.
    """
    if window_s <= 0:
        return set()
    now = time.time() if now is None else now
    expiring = sorted((m for m in markets
                       if m.end_ts is not None and m.end_ts - now <= window_s),
                      key=lambda m: m.end_ts)
    out: set[str] = set()
    for m in expiring:
        if len(out) + 2 > cap:
            break
        out.update((m.yes_token, m.no_token))
    return out


def _market_ok(m: Market, s) -> bool:
    """Gemeinsamer Handelbarkeits-Filter für den Snapshot.

    (a) endDate-Filter (Phantom-Arbitragen, Befund 04.07.2026) und
    (b) In-play-Matching-Delay-Filter (ungehedgte Beine, Befund 05.07.2026).
    """
    if not m.tradeable(s.min_time_to_end_s):
        return False
    return not (s.skip_delayed_inplay and m.inplay_delayed())


def _implication_candidates(
    cfg: BotConfig, gamma: GammaClient,
    negrisk: dict[str, list[Market]],
) -> tuple[dict[str, list[Market]], set[str]]:
    """Events + Bein-Tokens für den Implikations-Detektor (reine Messung).

    Lädt ALLE Gamma-Events (auch nicht-negRisk) und behält nur die, in denen
    der Detektor per Titel-Parsing eine Implikationsstruktur erkennt — sonst
    würden unnötig viele Orderbücher geladen. Die Paare selbst werden über
    Events UND negrisk erkannt (bei Slug-Kollision gewinnt negrisk, wie im
    Detektor). Fehler sind nie kritisch: der Detektor beobachtet nur, ein
    Ausfall kostet ausschließlich Messdaten.
    """
    s = cfg.strategy
    if not hasattr(gamma, "all_events"):  # Test-Fakes ohne Event-Endpunkt
        return {}, set()
    try:
        raw = gamma.all_events(min_liquidity=s.min_liquidity_usdc)
    except Exception as e:  # noqa: BLE001 — Messpfad darf den Tick nie crashen
        log.warning("Implikations-Events nicht ladbar: %s", e)
        return {}, set()
    events: dict[str, list[Market]] = {}
    for slug, ms in raw.items():
        ms = [m for m in ms if _market_ok(m, s)]
        if len(ms) >= 2:
            events[slug] = ms
    merged = dict(negrisk)
    for slug, ms in events.items():
        merged.setdefault(slug, ms)
    pairs = find_implication_pairs(merged)
    keep = {p.event_slug for p in pairs}
    return {slug: ms for slug, ms in events.items() if slug in keep}, pair_tokens(pairs)


def build_snapshot(cfg: BotConfig, gamma: GammaClient, books: BookClient,
                   fees: FeeRateCache | None = None) -> MarketSnapshot:
    fees = fees if fees is not None else FeeRateCache()
    s = cfg.strategy
    if s.scan_all_markets:
        # Vollmarkt-Scan: ALLE aktiven Märkte über der Mindestliquidität,
        # ohne max_markets-Deckel. min_volume dient dem Gamma-Client als
        # serverseitige Obermenge (Gesamtvolumen >= 24h-Volumen); der echte
        # 24h-Filter läuft danach clientseitig.
        markets = gamma.all_active_markets(min_liquidity=s.min_liquidity_usdc,
                                           min_volume=s.min_volume_24h_usdc)
        markets = [m for m in markets if m.volume_24h >= s.min_volume_24h_usdc]
    else:
        markets = gamma.active_markets(min_liquidity=s.min_liquidity_usdc,
                                       limit=s.max_markets * 2)
        markets = [m for m in markets if m.volume_24h >= s.min_volume_24h_usdc][: s.max_markets]
    # Endzeit-Filter (Trade-Print-Validierung 04.07.2026): Märkte behalten
    # nach endDate closed=False und ein stales, real nicht handelbares Buch
    # (abgelaufene 15-Min-Krypto-Fenster, beendete Spiele) — ohne diesen
    # Filter entstehen Phantom-Arbitragen im Paper-PnL und Live-Rejects.
    markets = [m for m in markets if _market_ok(m, s)]
    negrisk = gamma.negrisk_events(min_liquidity=s.min_liquidity_usdc)
    # Ein Event ist nur handelbar, wenn ALLE Teilmärkte noch laufen —
    # sonst wäre das Bündel unvollständig.
    negrisk = {slug: ms for slug, ms in negrisk.items()
               if all(_market_ok(m, s) for m in ms)}
    # Ohne Deckel würden die Bücher ALLER negRisk-Teilmärkte geladen (live
    # ~5000 Tokens -> ein Tick dauert länger als poll_interval_s): Events mit
    # zu vielen Teilmärkten überspringen (dort fehlt fast immer ein Buch und
    # negrisk_arb verwirft sie komplett), dann Top-N nach Summen-Liquidität.
    negrisk = {slug: ms for slug, ms in negrisk.items()
               if len(ms) <= s.max_negrisk_submarkets}
    negrisk = dict(sorted(negrisk.items(),
                          key=lambda kv: sum(m.liquidity for m in kv[1]),
                          reverse=True)[: s.max_negrisk_events])

    # Implikations-Detektor (reine Beobachtung): Events mit erkennbarer
    # Implikationsstruktur plus die Bein-Tokens, deren Bücher er braucht.
    events: dict[str, list[Market]] = {}
    impl_tokens: set[str] = set()
    if s.detect_implications:
        events, impl_tokens = _implication_candidates(cfg, gamma, negrisk)

    token_ids: set[str] = set()
    for m in markets:
        token_ids.update((m.yes_token, m.no_token))
    for ev_markets in negrisk.values():
        for m in ev_markets:
            token_ids.update((m.yes_token, m.no_token))
    token_ids.update(impl_tokens)

    if s.scan_all_markets:
        # Beim Vollmarkt-Scan sind volle Bücher für alle Tokens unbezahlbar
        # (~7k Tokens): zweistufig laden — Batch-Top-of-Book für alle,
        # volle Bücher nur für Arb-Kandidaten (siehe _load_books) plus die
        # kurzlebigen Profitmärkte des Stream-Abos (Recorder-Sichtbarkeit).
        extra_full = impl_tokens | _expiring_tokens(markets,
                                                    s.stream_event_window_s)
        book_map = _load_books(cfg, books, token_ids, markets, negrisk,
                               extra_full=extra_full)
    else:
        book_map = books.get_books(list(token_ids))
    # Tokenspezifische Taker-Fee-Raten (kategorieabhängig) aus den Gamma-
    # Marktobjekten, über Ticks gecacht; Tokens ohne bekannte Rate fehlen im
    # Dict und fallen auf cfg.risk.taker_fee_rate zurück.
    fees.update_from_markets(markets)
    for ev_markets in negrisk.values():
        fees.update_from_markets(ev_markets)
    for ev_markets in events.values():
        fees.update_from_markets(ev_markets)
    return MarketSnapshot(markets=markets, books=book_map, negrisk_events=negrisk,
                          events=events, fee_rates=fees.rates_for(token_ids))


def merge_positions(snap: MarketSnapshot, portfolio: Portfolio,
                    ledger: CycleLedger | None = None) -> float:
    """Komplement-Paare und vollständige NegRisk-Sets zu USDC mergen.

    Paper-Pendant zum on-chain CTF-Merge: Arbitragegewinne werden sofort
    realisiert statt bis zur Auflösung im Portfolio zu liegen. Rückgabe:
    Anzahl gemergter Paare/Sets (für Logging/Tests). Mit `ledger` wird jeder
    Merge samt Markt-Label und realisiertem PnL (Delta von realized_pnl über
    den Merge) protokolliert — Basis für den Cycle-Report.
    """
    merged = 0.0

    def do_merge(market: str, kind: str, fn) -> None:
        nonlocal merged
        before = portfolio.realized_pnl
        sets = fn()
        if sets > 0 and ledger is not None:
            ledger.record_merge(market, kind, sets,
                                portfolio.realized_pnl - before)
        merged += sets

    for m in snap.markets:
        do_merge(m.question, "complement",
                 lambda m=m: portfolio.merge_pairs(m.yes_token, m.no_token))
    for slug, ev_markets in snap.negrisk_events.items():
        do_merge(slug, "negrisk_yes",
                 lambda ev=ev_markets: portfolio.merge_negrisk_yes(
                     [m.yes_token for m in ev]))
        do_merge(slug, "negrisk_no",
                 lambda ev=ev_markets: portfolio.merge_negrisk_no(
                     [m.no_token for m in ev], len(ev)))
    return merged


def live_merge_positions(snap: MarketSnapshot, portfolio: Portfolio, merger,
                         ledger: CycleLedger | None = None) -> float:
    """Live-Pendant zu merge_positions: erst on-chain mergen, dann buchen.

    Gebucht wird NUR, was der MergeExecutor on-chain bestätigt hat (True
    erst nach erfolgreichem Receipt) — die Buchhaltung bleibt so an der
    Chain-Realität. Unterschiede zum Paper-Merge:

    - Binärmarkt-Paare: ConditionalTokens.mergePositions (1 pUSD/Paar).
    - NegRisk-NO-Sätze: NegRiskAdapter.convertPositions ((n-1) pUSD/Satz);
      braucht die questionIds aller Teilmärkte — fehlt eine, wird der Satz
      übersprungen. Läuft VOR den Paar-Merges, damit die kein NO-Bein aus
      einem vollständigen Satz konsumieren.
    - NegRisk-YES-Sätze: on-chain NICHT mergebar (kein Primitive) — sie
      zahlen erst bei der Auflösung und bleiben liegen.
    - YES/NO-Paare einzelner NegRisk-Teilmärkte (z.B. Market-Making-
      Inventar): NegRiskAdapter.mergePositions (1 pUSD/Paar).

    Der MergeExecutor loggt Fehler selbst und wirft nie; zur Sicherheit ist
    auch diese Funktion gegen den Bot-Loop abgeschirmt.
    """
    merged = 0.0

    def held(token: str) -> float:
        pos = portfolio.positions.get(token)
        return pos.shares if pos else 0.0

    def book(market: str, kind: str, fn) -> None:
        """On-chain bestätigten Merge in die Buchhaltung + Ledger übernehmen."""
        nonlocal merged
        before = portfolio.realized_pnl
        sets = fn()
        if sets > 0 and ledger is not None:
            ledger.record_merge(market, kind, sets,
                                portfolio.realized_pnl - before)
        merged += sets

    try:
        for m in snap.markets:
            sets = min(held(m.yes_token), held(m.no_token))
            if sets <= 1e-9 or not m.condition_id:
                continue
            if merger.merge_pairs(m.condition_id, sets):
                book(m.question, "complement",
                     lambda m=m: portfolio.merge_pairs(m.yes_token, m.no_token))
        for slug, ev_markets in snap.negrisk_events.items():
            no_sets = min(held(m.no_token) for m in ev_markets)
            question_ids = [m.question_id for m in ev_markets]
            if no_sets > 1e-9 and all(question_ids) \
                    and merger.merge_negrisk_no(question_ids, no_sets):
                book(slug, "negrisk_no",
                     lambda ev=ev_markets: portfolio.merge_negrisk_no(
                         [m.no_token for m in ev], len(ev)))
            for m in ev_markets:
                sets = min(held(m.yes_token), held(m.no_token))
                if sets <= 1e-9 or not m.condition_id:
                    continue
                if merger.merge_negrisk(m.condition_id, sets):
                    book(m.question, "negrisk_pair",
                         lambda m=m: portfolio.merge_pairs(m.yes_token,
                                                           m.no_token))
            if min(held(m.yes_token) for m in ev_markets) > 1e-9:
                log.debug("NegRisk-YES-Satz %s on-chain nicht mergebar — "
                          "zahlt erst bei Auflösung", slug)
    except Exception as e:  # noqa: BLE001 — Merge darf den Bot-Loop nie crashen
        log.error("Live-Merge fehlgeschlagen: %s", e)
    return merged


def tick(cfg: BotConfig, snap: MarketSnapshot, strategies, risk: RiskManager,
         broker, portfolio: Portfolio,
         recorder: OpportunityRecorder | None = None,
         ledger: CycleLedger | None = None,
         shadow: ShadowTracker | None = None,
         flattener: OrphanFlattener | None = None,
         sweeper: SettlementSweeper | None = None,
         syncer: PositionSyncer | None = None) -> int:
    snap.portfolio = portfolio  # Inventar-Sicht für Strategien (Market Making)
    # Marks (Midpoints) für den Kill-Switch: ohne sie wären unrealisierte
    # Verluste unsichtbar. Prüfung VOR der Ausführung, damit im Breach-Tick
    # keine neuen Orders mehr rausgehen — und danach noch einmal.
    marks = {t: b.midpoint for t, b in snap.books.items() if b.midpoint}
    risk.check_daily_loss(portfolio, marks)
    if recorder is not None:
        # Beweisdaten VOR der Ausführung sammeln: der Recorder rechnet selbst
        # auf dem Snapshot (Signal-Logik unverändert) und crasht nie den Tick.
        recorder.observe(snap)
    signals = []
    for strat in strategies:
        signals.extend(strat.generate(snap))
    approved = risk.filter(signals, portfolio)
    if signals and not approved:
        log.debug("%d Signale erzeugt, alle vom Risk-Manager abgelehnt", len(signals))
    # Für den Schattenvergleich: Live-Fills dieses Ticks sind genau die
    # Portfolio-Fills, die broker.execute gleich anhängt.
    fills_before = len(portfolio.fills)
    fills = broker.execute(approved, snap.books, portfolio, snap.fee_rates)
    if shadow is not None and cfg.mode == "live":
        # Live/Paper-Schattenvergleich: dieselben freigegebenen Signale gegen
        # dieselben Bücher im Paper-Lauf simulieren und pro Signal die
        # Capture-Quote protokollieren. Nur im Live-Modus sinnvoll (im
        # Paper-Modus wäre der Schatten identisch zum Lauf selbst); crasht
        # nie den Tick (siehe ShadowTracker.observe).
        shadow.observe(approved, snap.books, portfolio.fills[fills_before:],
                       snap.fee_rates, snap=snap)
    if cfg.mode != "live":
        # Paper-Modus: frisch gefüllte Arb-Paare/Sets sofort zu USDC mergen —
        # live läuft dasselbe als on-chain Merge (siehe unten).
        merge_positions(snap, portfolio, ledger)
    elif cfg.risk.live_auto_merge:
        # Live-Modus: vollständige Paare/Sätze on-chain zu pUSD mergen;
        # gebucht wird nur, was on-chain bestätigt wurde. Ohne MergeExecutor
        # (Init-Fehler, Test-Broker) bleibt alles liegen.
        merger = getattr(broker, "merger", None)
        if merger is not None:
            live_merge_positions(snap, portfolio, merger, ledger)
    if syncer is not None:
        # Periodischer Chain-Abgleich (Downtime-Fills, manuelle Eingriffe);
        # intern gedrosselt und mit Lag-Schonfrist, crasht nie.
        syncer.periodic(portfolio)
    if sweeper is not None:
        # Resolution-Sweeper (Kapital-Deadlock-Fix): Positionen final
        # aufgelöster Märkte zur Auszahlung ausbuchen — sonst wächst das
        # Exposure monoton und der Risk-Manager blockt dauerhaft.
        # NACH den Merges (was mergebar ist, ist billiger recycelt) und
        # VOR dem Waisen-Detektor (aufgelöste Beine sind kein Waisen-Fall).
        try:
            sweeper.sweep(portfolio, ledger)
        except Exception as e:  # noqa: BLE001 — Settlement nie tick-kritisch
            log.error("Settlement-Sweep fehlgeschlagen: %s", e)
    if flattener is not None:
        # Waisen-Detektor: NACH den Merges (vollständige Paare sind dann weg)
        # ungehedgte Einzelbeine zum Bid glattstellen. Die SELLs reduzieren
        # ausschließlich Bestand und Risiko — sie umgehen deshalb bewusst
        # risk.filter (das nur Käufe limitiert); crasht nie den Tick.
        try:
            extra, extra_books = flattener.signals(snap, portfolio)
            if extra:
                fills += broker.execute(extra, {**snap.books, **extra_books},
                                        portfolio, snap.fee_rates)
        except Exception as e:  # noqa: BLE001 — Glattstellung nie tick-kritisch
            log.error("Waisen-Check fehlgeschlagen: %s", e)
    if ledger is not None:
        # PnL-Ledger für den Cycle-Report (dedupliziert, crasht nie den
        # Tick) — VOR dem Loss-Check, damit auch der Breach-Tick im Ledger
        # steht (Befund 42).
        ledger.record_tick(portfolio)
    risk.check_daily_loss(portfolio, marks)
    return fills


def stream_tokens(snap: MarketSnapshot, cap: int,
                  event_window_s: float = 0.0,
                  now: float | None = None,
                  min_time_to_end_s: float = 0.0) -> list[str]:
    """Kandidaten-Tokens für das WSS-Abo aus dem letzten REST-Snapshot.

    Priorität (Messbefund 05.07.2026: der Profit konzentriert sich auf
    kurzlebige Crypto-'Up or Down'-5/15-Min-Märkte und Esports, die das
    Abo-Budget vorher kaum abdeckte — Beobachtungsintervall 12-229s):

    1. Binärmärkte im EREIGNISFENSTER (endDate innerhalb event_window_s),
       AUFSTEIGEND nach end_ts — die baldigst endenden zuerst, denn dort ist
       das Zeitfenster am knappsten. Märkte mit Restlaufzeit unterhalb
       min_time_to_end_s fliegen ganz raus: sie sind nicht mehr handelbar
       (totes Abo-Budget), und der Snapshot altert zwischen den Rotationen.
    2. NegRisk-Events, nur KOMPLETT (negrisk_arb braucht alle Beine eines
       Events — halbe Events wären totes Abo-Budget).
    3. Übrige Binärmärkte absteigend nach 24h-Volumen.

    event_window_s=0 schaltet die Fenster-Priorisierung ab (negRisk zuerst,
    dann reine Volumen-Sortierung).
    """
    out: list[str] = []
    seen: set[str] = set()
    now = time.time() if now is None else now

    def add(*tokens: str) -> None:
        for t in tokens:
            if t and t not in seen:
                seen.add(t)
                out.append(t)

    expiring: list[Market] = []
    rest: list[Market] = []
    for m in snap.markets:
        left = None if m.end_ts is None else m.end_ts - now
        if left is not None and left <= min_time_to_end_s:
            continue  # praktisch abgelaufen — Abo wäre totes Budget
        if event_window_s > 0 and left is not None and left <= event_window_s:
            expiring.append(m)
        else:
            rest.append(m)
    for m in sorted(expiring, key=lambda m: m.end_ts):
        if len(out) + 2 > cap:
            break
        add(m.yes_token, m.no_token)
    for ev_markets in snap.negrisk_events.values():
        if len(out) + 2 * len(ev_markets) > cap:
            continue
        for m in ev_markets:
            add(m.yes_token, m.no_token)
    for m in sorted(rest, key=lambda m: -m.volume_24h):
        if len(out) + 2 > cap:
            break
        add(m.yes_token, m.no_token)
    return out


class SnapshotWorker(threading.Thread):
    """Hintergrund-Thread: REST-Refresh (build_snapshot) im Doppelpuffer.

    build_snapshot blockiert live 39-52s (Gamma-Pagination + Batch-Preise) —
    im alten Ein-Thread-Design war die Engine damit 56% der Wanduhrzeit blind
    (Messbefund 05.07.2026). Deshalb baut dieser Worker die Snapshots
    parallel und tauscht sie atomar ein (Lock + Referenz-Swap); der
    Inner-Stream-Loop liest über snapshot() immer den letzten FERTIGEN
    Snapshot und läuft ununterbrochen weiter.

    Robustheit: Fehler beim Aufbau werden geloggt und mit exponentiellem
    Backoff erneut versucht — der alte Snapshot bleibt bis dahin gültig.
    Nach jedem frischen Snapshot rotiert der Worker das WSS-Abo
    (stream_tokens). Der Thread ist daemon: ein KeyboardInterrupt im
    Hauptthread beendet den Prozess sauber, ohne auf einen laufenden
    REST-Refresh warten zu müssen.
    """

    def __init__(self, cfg: BotConfig, gamma: GammaClient, books: BookClient,
                 fees: FeeRateCache, streamer=None,
                 initial_backoff_s: float = 2.0, max_backoff_s: float = 60.0,
                 min_sleep_s: float = 1.0):
        super().__init__(name="SnapshotWorker", daemon=True)
        self.cfg = cfg
        self.gamma = gamma
        self.books = books
        self.fees = fees
        self.streamer = streamer
        self.initial_backoff_s = initial_backoff_s
        self.max_backoff_s = max_backoff_s
        # Mindestpause zwischen zwei Refreshes: auch wenn der Aufbau länger
        # als poll_interval_s dauert, keine lückenlose Anfragekette gegen die
        # API (Rate-Limit-Schutz — wie der 1s-Mindest-Sleep im alten Loop).
        self.min_sleep_s = min_sleep_s
        self._lock = threading.Lock()
        self._snap: MarketSnapshot | None = None
        self._version = 0
        self._built_ts = 0.0
        self._stop = threading.Event()

    def snapshot_age(self, now: float | None = None) -> float:
        """Alter des letzten fertigen Snapshots in Sekunden (inf = keiner)."""
        with self._lock:
            if self._built_ts <= 0:
                return float("inf")
            return (time.time() if now is None else now) - self._built_ts

    def snapshot(self) -> tuple[MarketSnapshot | None, int]:
        """Letzter fertiger Snapshot + Versionszähler (atomar gelesen).

        None, solange noch kein erster Aufbau gelungen ist. Die Version
        erlaubt dem Leser zu erkennen, ob ein Snapshot FRISCH ist (voller
        REST-Tick fällig) oder nur der bekannte alte.
        """
        with self._lock:
            return self._snap, self._version

    def stop(self) -> None:
        """Worker beenden (weckt auch eine laufende Wartepause)."""
        self._stop.set()

    def refresh_once(self) -> bool:
        """Einen Snapshot bauen und atomar eintauschen; True bei Erfolg.

        Ein Fehlschlag lässt den alten Snapshot unangetastet — der
        Inner-Loop arbeitet dann einfach mit dem letzten guten Stand weiter.
        """
        try:
            snap = build_snapshot(self.cfg, self.gamma, self.books, self.fees)
        except Exception as e:  # noqa: BLE001 — Worker darf nie sterben
            log.error("Snapshot-Aufbau fehlgeschlagen: %s — alter Snapshot "
                      "bleibt gültig", e)
            return False
        with self._lock:
            self._snap = snap
            self._version += 1
            self._built_ts = time.time()
        if self.streamer is not None:
            # Abo auf die Kandidaten des frischen Snapshots rotieren.
            # subscribe ist eine reine Zustandsänderung, zur Sicherheit
            # trotzdem abgeschirmt (der Swap oben ist da schon passiert).
            try:
                self.streamer.subscribe(stream_tokens(
                    snap, self.cfg.strategy.stream_max_tokens,
                    event_window_s=self.cfg.strategy.stream_event_window_s,
                    min_time_to_end_s=self.cfg.strategy.min_time_to_end_s))
            except Exception as e:  # noqa: BLE001
                log.warning("WSS-Abo-Rotation fehlgeschlagen: %s", e)
        return True

    def _cycle(self, backoff: float) -> tuple[float, float]:
        """Ein Refresh-Durchlauf; liefert (Wartezeit, nächster Backoff)."""
        started = time.time()
        if self.refresh_once():
            elapsed = time.time() - started
            if elapsed > self.cfg.poll_interval_s:
                log.warning("Snapshot-Aufbau dauerte %.1fs > Intervall %.1fs — "
                            "der Stream-Loop läuft zwar weiter, aber die "
                            "REST-Sicht altert; max_markets/max_negrisk_events "
                            "senken oder Intervall erhöhen",
                            elapsed, self.cfg.poll_interval_s)
            return (max(self.min_sleep_s, self.cfg.poll_interval_s - elapsed),
                    self.initial_backoff_s)
        return backoff, min(backoff * 2, self.max_backoff_s)

    def run(self) -> None:
        backoff = self.initial_backoff_s
        while not self._stop.is_set():
            wait, backoff = self._cycle(backoff)
            self._stop.wait(wait)


def stream_loop(cfg: BotConfig, worker: SnapshotWorker, strategies,
                risk: RiskManager, broker, portfolio: Portfolio,
                streamer,
                recorder: OpportunityRecorder | None = None,
                ledger: CycleLedger | None = None,
                shadow: ShadowTracker | None = None,
                state_path: str = "paper_state.json",
                flattener: OrphanFlattener | None = None,
                sweeper: SettlementSweeper | None = None,
                syncer: PositionSyncer | None = None) -> None:
    """Endloser Inner-Loop gegen den Doppelpuffer des SnapshotWorkers.

    Läuft UNUNTERBROCHEN — der REST-Refresh passiert parallel im Worker,
    hier wird nur dessen letzter fertiger Snapshot gelesen (kein
    Blindfenster mehr, kein Deadline-Konstrukt). Pro Iteration:

    - Frischer Snapshot (Version gewechselt): voller Tick + Statuszeile.
    - Sonst alle stream_tick_s NUR die live gestreamten Bücher (über die
      REST-Bücher gelegt) gegen die Strategien — kein einziger REST-Call.
      Liefert der Stream nichts (leer/tot/kein Streamer), wird bis zum
      nächsten frischen Snapshot nur geschlafen (reiner REST-Betrieb).

    KillSwitch propagiert (stoppt den ganzen Bot), alle anderen Tick- und
    Streamer-Fehler werden geloggt und überlebt.
    """
    interval = cfg.strategy.stream_tick_s
    last_version = 0
    while True:
        snap, version = worker.snapshot()
        if snap is None:
            # Noch kein erster Snapshot fertig (oder Worker im Fehler-Backoff).
            time.sleep(interval)
            continue
        fresh = version != last_version
        last_version = version
        # Altersdeckel (Befund 36): Fällt der REST-Refresh dauerhaft aus,
        # altern die Snapshot-Märkte unbegrenzt — Stream-Ticks würden dann
        # abgelaufene Märkte handeln (die Phantom-Arb-Klasse vom 04.07.).
        max_age = max(cfg.poll_interval_s * 3.0, 300.0)
        age = worker.snapshot_age() if hasattr(worker, "snapshot_age") else 0.0
        if age > max_age:
            if time.time() - getattr(stream_loop, "_age_warned", 0.0) > 60:
                stream_loop._age_warned = time.time()
                log.error("Snapshot ist %.0fs alt (Deckel %.0fs) — Handel "
                          "pausiert, bis der REST-Refresh wieder liefert",
                          age, max_age)
            time.sleep(interval)
            continue
        streamed: dict[str, OrderBook] = {}
        if streamer is not None:
            try:
                streamed = streamer.get_books(list(snap.books))
            except Exception as e:  # noqa: BLE001 — Streamer-Fehler nie durchreichen
                log.warning("Stream-Bücher nicht lesbar: %s — nur REST-Snapshot", e)
                streamed = {}
        if fresh or streamed:
            fast = MarketSnapshot(markets=snap.markets,
                                  books={**snap.books, **streamed},
                                  negrisk_events=snap.negrisk_events,
                                  events=snap.events,
                                  fee_rates=snap.fee_rates)
            got = 0
            try:
                got = tick(cfg, fast, strategies, risk, broker, portfolio,
                           recorder, ledger, shadow, flattener, sweeper,
                           syncer)
            except KillSwitch:
                raise
            except Exception as e:  # noqa: BLE001 — Netzwerk/Broker-Fehler überleben
                log.error("Tick fehlgeschlagen: %s", e)
            if fresh:
                marks = {t: b.midpoint for t, b in fast.books.items() if b.midpoint}
                console.print(
                    f"[dim]{time.strftime('%H:%M:%S')}[/dim] "
                    f"Fills: {got} | Wert: {portfolio.value(marks):.2f} USDC | "
                    f"Tages-PnL: {portfolio.daily_pnl(marks):+.2f} | "
                    f"Exposure: {portfolio.total_exposure():.2f}"
                )
                portfolio.save(state_path)
            elif got:
                console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] "
                              f"[cyan]Stream-Tick[/cyan] Fills: {got}")
                portfolio.save(state_path)
        time.sleep(interval)


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


def _sigterm_to_interrupt(signum, frame):
    """SIGTERM (systemd/kill/Container-Stop) wie Ctrl-C behandeln.

    Verifikations-Befund 24: SIGTERM umging das finally in cmd_run —
    kein cancel_all, kein Save, kein Shadow-Flush.
    """
    raise KeyboardInterrupt("SIGTERM")


def cmd_run(cfg: BotConfig) -> None:
    import signal

    try:
        signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    except ValueError:
        pass  # nicht im Main-Thread (Tests) — dann kein Handler
    if cfg.mode == "live":
        console.print("[bold red]LIVE-MODUS: Es wird mit echtem Geld gehandelt![/bold red]")
    else:
        console.print("[bold green]Paper-Modus: Simulation gegen echte Orderbücher.[/bold green]")

    gamma, books = GammaClient(), BookClient()
    # Fee-Cache lebt über den ganzen Lauf: Raten bleiben auch dann bekannt,
    # wenn Gamma die Fee-Info eines Markts in einem Tick nicht mitliefert.
    fees = FeeRateCache()
    # start_cash greift nur beim ERSTEN Anlegen (kein State auf Disk) —
    # ein bestehender State behält sein Cash. Live und Paper führen strikt
    # GETRENNTE State-Dateien: ein parallel laufender Paper-Messbot darf
    # niemals dieselbe Datei beschreiben wie die Live-Buchhaltung.
    state_path = "live_state.json" if cfg.mode == "live" else "paper_state.json"
    # Doppelstart-Schutz (Befund 23): zwei Prozesse auf demselben State
    # würden sich gegenseitig die Buchhaltung zerschreiben und die Orders
    # canceln. Exklusives flock auf einer Lock-Datei, gehalten bis
    # Prozessende (fd bleibt referenziert).
    import fcntl

    lock_file = open(f"{state_path}.lock", "w")  # noqa: SIM115 — lebt bis Prozessende
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"Ein anderer Bot-Prozess hält {state_path}.lock — Doppelstart "
            "auf demselben State ist nicht erlaubt (zuerst den laufenden "
            "Prozess stoppen).") from None
    portfolio = Portfolio.load(state_path, start_cash=cfg.risk.paper_start_cash)
    broker = make_broker(cfg)
    syncer = None
    if cfg.mode == "live":
        # Buchhaltung an die Chain-Wahrheit angleichen (Fills während
        # Downtime, manuelle Eingriffe) — NACH make_broker, damit dessen
        # Start-cancel_all keine In-flight-Orders mehr offen lässt.
        # Der Syncer bleibt danach aktiv (periodisch im Tick, 15-Min-Takt
        # mit Lag-Schonfrist) und fängt so auch das Start-Race ein, wenn
        # ein Delayed-Match erst Sekunden nach dem ersten Sync indiziert wird.
        addr = cfg.funder_address or getattr(
            getattr(broker, "client", None), "get_address", lambda: None)()
        if addr:
            syncer = PositionSyncer(addr)
            try:
                syncer.sync(portfolio)
                portfolio.save(state_path)
            except Exception as e:  # noqa: BLE001 — Sync nie startkritisch
                log.error("Positions-Sync fehlgeschlagen: %s — Buchhaltung "
                          "kann von der Chain abweichen", e)
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

    # Beweisdaten-Sammler: protokolliert JEDE beobachtete (Fast-)Arbitrage
    # nach data/opportunities.jsonl — Auswertung: python -m polybot.main report
    recorder = OpportunityRecorder(cfg)
    # PnL-Ledger (prozessübergreifend, data/pnl_ledger.jsonl): Basis für
    # `python -m polybot.main cycle-report` — der Messbot startet den Prozess
    # alle 90 Minuten frisch, die Fenster-Metriken brauchen deshalb Disk-State.
    # Paper und Live führen GETRENNTE Ledger (Befund 37: gemischte
    # Fenster-Deltas zweier Portfolios machen den cycle-report wertlos).
    ledger = CycleLedger(path="data/pnl_ledger.live.jsonl"
                         if cfg.mode == "live" else None)
    ledger.record_start(portfolio)
    # Live/Paper-Schattenvergleich (nur Live-Modus): pro Signal wird der
    # Live-Fill gegen einen Paper-Schattenlauf gemessen (data/shadow.jsonl);
    # Auswertung: python -m polybot.main capture-report
    shadow = ShadowTracker(cfg) if cfg.mode == "live" else None
    # Waisen-Detektor (Live-Befund 05.07.2026): ungehedgte Einzelbeine nach
    # Schonfrist zum Bid glattstellen — der Bot hält keine Richtungswetten.
    flattener = None
    if cfg.risk.flatten_orphan_grace_s > 0:
        flattener = OrphanFlattener(cfg, books=books, gamma=gamma)
        console.print(f"[green]Waisen-Detektor aktiv — Schonfrist "
                      f"{cfg.risk.flatten_orphan_grace_s:.0f}s.[/green]")
    # Resolution-Sweeper (immer aktiv, paper wie live): bucht Positionen
    # final aufgelöster Märkte zur Auszahlung aus — der Kapital-Deadlock-Fix
    # der Agenten-Flotte vom 05.07.2026.
    sweeper = SettlementSweeper(gamma)

    # WebSocket-Streaming (optional): scheitert der Start (z.B. fehlende
    # Bibliothek), läuft der Bot unverändert im reinen REST-Betrieb weiter.
    streamer = None
    if cfg.strategy.use_stream:
        try:
            streamer = BookStreamer(max_tokens=cfg.strategy.stream_max_tokens)
            streamer.start()
            console.print(f"[green]WebSocket-Stream aktiv — Inner-Loop alle "
                          f"{cfg.strategy.stream_tick_s}s.[/green]")
        except Exception as e:  # noqa: BLE001 — Stream ist nie kritisch
            log.warning("BookStreamer nicht startbar: %s — reines REST-Polling", e)
            streamer = None

    # Doppelpuffer-Architektur: der REST-Refresh (build_snapshot, live
    # 39-52s) läuft im SnapshotWorker-Thread; der Inner-Loop hier im
    # Hauptthread tickt ununterbrochen gegen den letzten fertigen Snapshot
    # (plus Stream-Overlay) — die alte 56%-Blindzeit entfällt.
    worker = SnapshotWorker(cfg, gamma, books, fees, streamer=streamer)
    worker.start()
    try:
        stream_loop(cfg, worker, strategies, risk, broker, portfolio,
                    streamer, recorder, ledger, shadow, state_path=state_path,
                    flattener=flattener, sweeper=sweeper, syncer=syncer)
    except KillSwitch as e:
        console.print(f"[bold red]{e}[/bold red]")
    except KeyboardInterrupt:
        console.print("Gestoppt. Portfolio gespeichert.")
    finally:
        # Kein Zustand »Bot tot, Orders leben«: bei Kill-Switch, Ctrl-C
        # oder Crash ALLE offenen Börsen-Orders canceln (Live-Broker).
        cancel_all = getattr(broker, "cancel_all_orders", None)
        if cancel_all is not None:
            # Mit Portfolio: letzter Reconcile-Pass vor UND nach dem Cancel,
            # damit Fills der letzten Sekunden nicht verloren gehen.
            cancel_all("Prozessende", portfolio)
        portfolio.save(state_path)
        if shadow is not None:
            # Offene Mess-Episoden schliessen — sonst fehlt der letzte
            # Datenpunkt der Capture-Messung bei jedem 90-Min-Neustart.
            shadow.flush()
        worker.stop()  # daemon-Thread: kein Join auf laufenden REST-Refresh nötig
        if streamer is not None:
            streamer.stop()


def cmd_status(cfg: BotConfig) -> None:
    state_path = "live_state.json" if cfg.mode == "live" else "paper_state.json"
    pf = Portfolio.load(state_path, start_cash=cfg.risk.paper_start_cash)
    console.print(f"[dim]Modus {cfg.mode} — State {state_path}[/dim]")
    console.print(f"Cash: {pf.cash:.2f} USDC | realisierter PnL: {pf.realized_pnl:+.2f} | "
                  f"Gebühren: {pf.fees_paid:.2f} | Rebates: {pf.rebates_earned:.2f} | "
                  f"Positionen: {len(pf.positions)} | Fills: {len(pf.fills)} | "
                  f"ruhende Orders: {len(pf.resting_orders)}")
    if pf.positions:
        table = Table(title="Offene Positionen")
        for col in ("Token", "Shares", "Einstand (USDC)"):
            table.add_column(col)
        for p in pf.positions.values():
            table.add_row(p.token_id[:16] + "…", f"{p.shares:.1f}", f"{p.cost_basis:.2f}")
        console.print(table)


def cmd_report(cfg: BotConfig, target: float = 1000.0,
               opps_path: str = "data/opportunities.jsonl",
               state_path: str = "paper_state.json") -> None:
    """Opportunity-Log auswerten: Dichte, Hochrechnung, Kapitalfrage.

    Liest data/opportunities.jsonl (vom OpportunityRecorder) und
    paper_state.json (tatsächliche Paper-Ergebnisse als Realitäts-Check).
    """
    opps = load_opportunities(opps_path)
    if not opps:
        console.print(f"[yellow]Keine Beobachtungen in {opps_path} — erst "
                      "'python -m polybot.main run' eine Weile laufen lassen.[/yellow]")
        return
    stats = aggregate(opps)

    def fmt_ts(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))

    hours = stats.duration_s / 3600
    console.print(f"[bold]Beobachteter Zeitraum:[/bold] {fmt_ts(stats.first_ts)} — "
                  f"{fmt_ts(stats.last_ts)} UTC ({hours:.2f} h)")
    console.print(f"Gelegenheiten über Handels-Schwelle: {stats.n_above} | "
                  f"darunter (geloggt ab Edge >= -0.01): {stats.n_below}")
    console.print(f"Summe theoretischer Profit im Zeitraum: "
                  f"{stats.theo_profit_total:.2f} USDC")
    if stats.duration_s > 0:
        console.print(f"[bold]Hochrechnung auf 24h:[/bold] "
                      f"{stats.theo_profit_per_day:.2f} USDC/Tag "
                      f"(bei voller Tiefe, ohne Kapital-Limits)")
    else:
        console.print("[yellow]Zeitraum zu kurz (< 2 Zeitpunkte) — keine "
                      "24h-Hochrechnung möglich.[/yellow]")

    # Kapitalfrage: Modell siehe recorder._daily_profit_at_capital — Kapital
    # wird pro Gelegenheit recycelt (Merge macht es sofort wieder frei).
    capital, max_daily = required_capital(opps, stats.duration_s, target)
    console.print(f"[bold]Kapitalfrage (Ziel {target:.0f} USDC/Tag):[/bold]")
    if stats.duration_s <= 0:
        console.print("[yellow]  nicht beantwortbar ohne Zeitraum.[/yellow]")
    elif capital is None:
        console.print(f"[yellow]  Mit unbegrenztem Kapital wären maximal "
                      f"{max_daily:.2f} USDC/Tag drin — die gemessene "
                      f"Gelegenheitsdichte/Tiefe reicht für das Ziel nicht.[/yellow]")
    else:
        console.print(f"  Benötigtes Arbeitskapital: ~{capital:.2f} USDC "
                      f"(Maximum bei unbegrenztem Kapital: {max_daily:.2f} USDC/Tag)")

    # Realitäts-Check: was hat das Paper-Portfolio tatsächlich erwirtschaftet?
    pf = Portfolio.load(state_path, start_cash=cfg.risk.paper_start_cash)
    console.print(f"[bold]Paper-Portfolio ({state_path}):[/bold] "
                  f"Cash {pf.cash:.2f} USDC | realisierter PnL {pf.realized_pnl:+.2f} | "
                  f"Gebühren {pf.fees_paid:.2f} | Fills {len(pf.fills)}")


def cmd_cycle_report(cfg: BotConfig, now: float | None = None,
                     opps_path: str = "data/opportunities.jsonl",
                     ledger_path: str | Path = cycle_report.DEFAULT_LEDGER_PATH,
                     state_path: str = "paper_state.json",
                     json_path: str | Path = cycle_report.DEFAULT_REPORT_PATH) -> dict:
    """Zyklus-Selbstauswertung: eine Bildschirmseite + data/cycle_report.json.

    Kombiniert PnL-Ledger (Fenster-Deltas/Raten), Opportunity-Log (Dichte
    nach kind) und Paper-State (Totale) — gedacht für die schnelle Iteration
    zwischen den 90-Minuten-Zyklen des Messbots. Rückgabe: das Report-Dict
    (identisch zum geschriebenen JSON).
    """
    now = time.time() if now is None else now
    rows = cycle_report.load_ledger(ledger_path)
    opps = load_opportunities(opps_path)
    pf = Portfolio.load(state_path, start_cash=cfg.risk.paper_start_cash)
    report = cycle_report.build_report(cfg, now, rows, opps, pf)

    # JSON-Ausgabe für spätere Auswertung — Schreibfehler nur warnen, die
    # Konsolen-Ausgabe soll trotzdem kommen.
    out = Path(json_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
    except OSError as e:
        log.warning("Cycle-Report-JSON %s nicht schreibbar: %s", out, e)

    # ---- Kompakte Konsolen-Ausgabe (eine Bildschirmseite) ------------------
    labels = {"1h": "1h", "3h": "3h", "today": "heute", "process": "Prozess"}
    w = report["windows"]

    def money_line(name: str) -> str:
        win = w[name]
        return (f"PnL {win['pnl']:+.2f} | Gebühren {win['fees']:.2f} | "
                f"Rebates {win['rebates']:.2f} USDC")

    console.print(f"[bold]Cycle-Report[/bold] {report['generated_at_iso']} "
                  f"(Ledger: {len(rows)} Zeilen | Opportunities: {len(opps)})")
    console.print(f"[bold]Heute (UTC):[/bold]        {money_line('today')}")
    if "process" in w:
        console.print(f"[bold]Seit Prozessstart:[/bold]  {money_line('process')} "
                      f"({w['process']['hours_covered']:.2f} h)")
    else:
        console.print("[yellow]Kein Prozessstart im Ledger — 'seit Prozessstart' "
                      "entfällt (erst 'python -m polybot.main run' laufen "
                      "lassen).[/yellow]")
    console.print("[bold]Rate:[/bold] " + " | ".join(
        f"{labels[n]} {w[n]['rate_per_h']:+.2f} USDC/h"
        for n in ("1h", "3h", "today")))

    window_names = [n for n in ("1h", "3h", "today", "process")
                    if n in report["opportunities"]]
    table = Table(title="Gelegenheiten (Anzahl theo_profit>0 / Summe theo_profit USDC)")
    table.add_column("kind")
    for n in window_names:
        table.add_column(labels[n], justify="right")
    kinds = list(cycle_report.OPP_KINDS) + sorted(
        k for k in report["opportunities"]["today"] if k not in cycle_report.OPP_KINDS)
    for k in kinds:
        cells = [k]
        for n in window_names:
            agg = report["opportunities"][n].get(
                k, {"n_pos": 0, "theo_profit": 0.0})
            cells.append(f"{agg['n_pos']} / {agg['theo_profit']:.2f}")
        table.add_row(*cells)
    console.print(table)

    top = report["top_markets_today"]
    if top:
        tt = Table(title="Top-Märkte heute (realisierter Merge-PnL)")
        for col in ("Markt", "PnL (USDC)", "Sets", "Merges"):
            tt.add_column(col, justify="right" if col != "Markt" else "left")
        for m in top:
            tt.add_row(m["market"][:60], f"{m['pnl']:+.2f}",
                       f"{m['sets']:.1f}", f"{m['merges']}")
        console.print(tt)
    else:
        console.print("Heute noch keine realisierten Merges.")

    console.print(f"[bold]Engpass:[/bold] {report['bottleneck']['text']}")
    tot = report["totals"]
    console.print(f"[bold]Gesamt:[/bold] Cash {tot['cash']:.2f} | realisierter "
                  f"PnL {tot['realized_pnl']:+.2f} | Gebühren {tot['fees_paid']:.2f} | "
                  f"Rebates {tot['rebates_earned']:.2f} | Positionen {tot['positions']} | "
                  f"Fills {tot['fills']}")
    console.print(f"[dim]JSON: {out}[/dim]")
    return report


def cmd_capture_report(cfg: BotConfig,
                       shadow_path: str | Path = DEFAULT_SHADOW_PATH) -> dict | None:
    """Live/Paper-Schattenvergleich auswerten: die echte Capture-Quote.

    Liest data/shadow.jsonl (vom ShadowTracker im Live-Modus geschrieben)
    und zeigt: Capture-Quote gesamt / pro Strategie / pro Stunde plus die
    ehrliche Hochrechnung 'Paper-Rate x Capture = Live-Erwartung'.
    Rückgabe: das Aggregat-Dict (None ohne Daten).
    """
    rows = load_shadow(shadow_path)
    if not rows:
        console.print(f"[yellow]Keine Schattenvergleichs-Daten in {shadow_path} — "
                      "die schreibt nur ein Lauf im Live-Modus "
                      "('python -m polybot.main run' mit mode: live).[/yellow]")
        return None
    agg = aggregate_capture(rows)

    def fmt_ts(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))

    def fmt_capture(b: dict) -> str:
        if b["capture"] is None:
            return "—"
        return f"{b['capture'] * 100:.1f}%"

    hours = agg["duration_s"] / 3600
    console.print(f"[bold]Capture-Report[/bold] {fmt_ts(agg['first_ts'])} — "
                  f"{fmt_ts(agg['last_ts'])} UTC ({hours:.2f} h, "
                  f"{agg['n_records']} Signal-Datensätze)")
    o = agg["overall"]
    console.print(f"[bold]Capture gesamt:[/bold] {fmt_capture(o)} "
                  f"(Paper {o['paper_notional']:.2f} USDC Notional gefüllt, "
                  f"live {o['live_notional']:.2f})")

    table = Table(title="Capture pro Strategie")
    for col in ("Strategie", "Signale", "Paper (USDC)", "Live (USDC)", "Capture"):
        table.add_column(col, justify="right" if col != "Strategie" else "left")
    for name, b in sorted(agg["by_strategy"].items()):
        table.add_row(name, str(b["n"]), f"{b['paper_notional']:.2f}",
                      f"{b['live_notional']:.2f}", fmt_capture(b))
    console.print(table)

    ht = Table(title="Capture pro Stunde (UTC)")
    for col in ("Stunde", "Signale", "Paper (USDC)", "Live (USDC)", "Capture"):
        ht.add_column(col, justify="right" if col != "Stunde" else "left")
    for hour, b in agg["by_hour"].items():
        ht.add_row(hour, str(b["n"]), f"{b['paper_notional']:.2f}",
                   f"{b['live_notional']:.2f}", fmt_capture(b))
    console.print(ht)

    # Die ehrliche Hochrechnung: gemessene Paper-Rate x gemessene Capture.
    if agg["live_edge_per_day"] is not None:
        console.print(
            f"[bold]Hochrechnung:[/bold] Bei gemessener Paper-Rate "
            f"{agg['paper_edge_per_day']:.2f} USDC/Tag und Capture "
            f"{o['capture'] * 100:.1f}% wären das "
            f"{agg['live_edge_per_day']:.2f} USDC/Tag live.")
    else:
        console.print("[yellow]Hochrechnung nicht möglich — Zeitraum zu kurz "
                      "(< 2 Zeitpunkte) oder noch kein Paper-Fill gemessen.[/yellow]")
    return agg


def main() -> None:
    parser = argparse.ArgumentParser(prog="polybot")
    parser.add_argument("command",
                        choices=["scan", "run", "status", "report",
                                 "cycle-report", "capture-report", "preflight"])
    # default=None: BotConfig.load unterscheidet so zwischen explizit gesetztem
    # --config (Datei MUSS existieren) und implizitem config.yaml-Fallback.
    parser.add_argument("--config", default=None)
    parser.add_argument("--target", type=float, default=1000.0,
                        help="Zielprofit in USDC/Tag für die Kapitalfrage (report)")
    parser.add_argument("--execute", action="store_true",
                        help="preflight: Transaktionen wirklich senden "
                             "(Default: nur prüfen und PLAN drucken)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = BotConfig.load(args.config)
    if args.command == "report":
        cmd_report(cfg, target=args.target)
        return
    if args.command == "preflight":
        cmd_preflight(cfg, execute=args.execute)
        return
    {"scan": cmd_scan, "run": cmd_run, "status": cmd_status,
     "cycle-report": cmd_cycle_report,
     "capture-report": cmd_capture_report}[args.command](cfg)


if __name__ == "__main__":
    main()
