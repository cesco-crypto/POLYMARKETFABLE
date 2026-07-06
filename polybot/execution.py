"""Ausführung: Paper-Broker (Simulation) und Live-Broker (py-clob-client).

Der Paper-Broker simuliert Fills gegen das echte Orderbuch — konservativ:
gefillt wird nur, was bis zum Limitpreis tatsächlich im Buch liegt, und zwar
Level für Level zum jeweiligen Level-Preis (inkl. Taker-Gebühr).

Der Live-Broker bucht nur tatsächlich gematchte Mengen ins Portfolio:
ruhende GTC-Orders werden getrackt und ihre (Teil-)Fills asynchron über
den Order-Status reconciled statt sofort als Fill verbucht.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from polybot.config import CLOB_HOST, POLYGON_CHAIN_ID, BotConfig
from polybot.data.orderbook import OrderBook
from polybot.portfolio import Fill, Portfolio, RestingOrder
from polybot.strategies.base import Signal

log = logging.getLogger(__name__)

# Polymarket zahlt Makern 20-25% der Taker-Fees des Marktes als Rebate —
# konservativ das untere Ende für die Paper-Simulation: effektive
# Maker-Rebate-Rate pro Token = 0.2 * taker_fee_rate(token).
MAKER_REBATE_SHARE = 0.2


def _quantize_price(price: float, tick: float, side: str) -> float:
    """Preis aufs Tick-Raster quantisieren: BUY ab-, SELL aufrunden.

    Konservativ bzgl. Edge — der Client würde sonst stillschweigend
    kaufmännisch runden (round_normal) und z.B. eine BUY-Order von 0.155
    auf 0.16 anheben; bei Tick 0.0025/0.0001 würde ein nicht quantisierter
    Preis vom Server als invalid abgelehnt.
    """
    steps = (
        math.floor(price / tick + 1e-9) if side == "BUY"
        else math.ceil(price / tick - 1e-9)
    )
    q = steps * tick
    # Gültiger Bereich laut CLOB: tick <= p <= 1 - tick
    return round(min(max(q, tick), 1.0 - tick), 6)


def _marketable_size(size: float, price: float, side: str) -> float:
    """Größte Size <= size (Raster 0.01), deren Beträge die CLOB-Präzision einhalten.

    FOK/FAK prüft der Server als Market-Order: bei BUY darf der USDC-Betrag
    (Size*Preis) max. 2 Nachkommastellen haben, bei SELL max. 4; die
    Share-Menge selbst max. 2. Der Preis ist hier bereits aufs Tick-Raster
    quantisiert (max. 6 Nachkommastellen), daher exakt als Ganzzahl fassbar.
    """
    p = int(round(price * 1_000_000))
    if p <= 0 or size <= 0:
        return 0.0
    mod = 1_000_000 if side == "BUY" else 10_000
    k = int(math.floor(size * 100 + 1e-9))
    while k > 0 and (k * p) % mod:
        k -= 1
    return k / 100


def _to_float(v) -> float:
    """Response-Feld (String/None/Zahl) defensiv in float wandeln."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _looks_like_tick_error(text: str) -> bool:
    """Ablehnung, die auf eine zwischenzeitlich gewechselte Tick-Größe deutet."""
    t = text.lower()
    return "tick" in t or "invalid price" in t


class Broker(ABC):
    @abstractmethod
    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        """Signale ausführen; Anzahl der Fills zurückgeben."""


class PaperBroker(Broker):
    """Simuliert Marketable-Limit-Orders gegen den aktuellen Book-Snapshot.

    Marketable Orders sind Taker-Fills — die kategorieabhängige Taker-Gebühr
    (fee = shares * rate * p * (1-p)) wird mitverbucht, sonst wäre der
    Paper-PnL systematisch zu hoch. Gefüllt wird Level für Level zum
    jeweiligen Level-Preis (volumengewichtet), nicht pauschal zum Limitpreis;
    mehrere Signale auf dasselbe Token im selben Tick teilen sich die
    Buchliquidität, statt denselben Snapshot doppelt zu verplanen.

    Signalgruppen (s.group, Arb-Beine) füllen FOK-atomar: nur wenn JEDES Bein
    in voller Signalgröße füllbar ist, wird die Gruppe gebucht — sonst füllt
    kein Bein (ein halber Arb wäre eine offene, ungehedgte Wette).

    Ruhende Orders (GTC-Simulation): der nicht-marketable Rest ungruppierter
    Signale ruht als RestingOrder im Portfolio-State (überlebt Neustarts).
    Zu Beginn jedes execute() werden ruhende Orders gegen das AKTUELLE Buch
    geprüft: eine ruhende BUY füllt konservativ erst, wenn der beste Ask auf
    oder unter den Orderpreis gefallen ist (analog SELL gegen den Bid) —
    Fill zum Orderpreis als Maker (Gebühr 0, optional Rebate-Gutschrift).
    Das Cash ruhender BUYs ist reserviert und steht Sofort-Orders nicht zur
    Verfügung; ein replace-Signal ersetzt die alten Quotes seines Tokens
    (gleiche Semantik wie das Cancel-vor-Neuquote des LiveBrokers).

    Latenz-Verzug (fill_delay_ticks, ehrliche Fill-Simulation): live vergehen
    zwischen Signal und Order-Ankunft ~250ms (Erkennung + Order-RTT) — ein
    Fill gegen denselben Snapshot, aus dem das Signal entstand, wäre also
    systematisch optimistisch. Bei fill_delay_ticks > 0 werden neue Signale
    deshalb NICHT sofort gefüllt, sondern in eine Pending-Queue gelegt und
    erst nach so vielen weiteren execute()-Aufrufen gegen das DANN aktuelle
    Buch geprüft (FOK-Gruppen unverändert atomar: alle Beine gegen das neue
    Buch, ganz oder gar nicht). 0 = Sofort-Fill (altes Verhalten für
    Vergleichsmessungen).
    """

    def __init__(self, cfg: BotConfig | None = None):
        # Fallback-Rate für Tokens ohne abrufbare Fee-Rate (konservativ das
        # konfigurierte Maximum); ohne Config (Tests) gebührenfrei.
        self.fallback_fee_rate = cfg.risk.taker_fee_rate if cfg else 0.0
        # Rebate-Simulation für Maker-Fills: rebate = rate * p * (1-p) pro
        # Share (analog zur Taker-Formel). Die Rate ist tokenspezifisch
        # MAKER_REBATE_SHARE * taker_fee_rate(token); dieser Wert hier ist
        # nur der Fallback ohne bekannte Taker-Rate. Default 0.0 = aus.
        self.maker_rebate_rate = cfg.strategy.maker_rebate_rate if cfg else 0.0
        # Latenz-Verzug in execute()-Aufrufen (siehe Klassen-Docstring);
        # ohne Config (Tests) 0 = Sofort-Fill wie bisher.
        self.fill_delay_ticks = cfg.strategy.paper_fill_delay_ticks if cfg else 0
        # Pending-Queue wartender Signale: [verbleibende Ticks, Signale].
        # Bewusst NICHT persistiert — bei einem Neustart verfallen wartende
        # Signale ersatzlos (live wären diese Orders auch nie rausgegangen,
        # und ihr Ursprungs-Snapshot ist nach dem Neustart ohnehin stale).
        self._pending_signals: list[list] = []
        # Persistenter Liquiditätsverbrauch ÜBER Ticks (Befund Agenten-Flotte
        # 05.07.2026): Paper-Fills dezimieren das reale Buch nicht — dasselbe
        # ruhende Ask-Level wurde im Sekundentakt erneut »gekauft« (Beleg:
        # 74 identische Merges à +54.72 USDC aus einem einzigen 364-Share-Ask;
        # Up/Down-PnL ~10x inflationiert). Deshalb merken wir uns je
        # (Token, Seite) pro Preislevel, wie viel WIR simuliert konsumiert
        # haben: verfügbar ist nur noch max(0, Levelgröße - verbraucht).
        # Ein Eintrag verfällt, wenn das Level aus dem Buch verschwindet
        # (der Markt hat sich real bewegt; taucht der Preis später wieder
        # auf, ist das neue Liquidität).
        self._consumed_levels: dict[tuple[str, str], dict[float, float]] = {}

    def _rebate_rate(self, token_id: str, fee_rates: dict[str, float]) -> float:
        """Maker-Rebate-Rate eines Tokens: 20% seiner Taker-Fee-Rate.

        Ohne bekannte tokenspezifische Taker-Rate greift der konfigurierte
        Pauschalwert strategy.maker_rebate_rate (Default 0.0 — konservativ
        kein simulierter Verdienst, den es live vielleicht nicht gäbe).
        """
        rate = fee_rates.get(token_id)
        if rate is None:
            return self.maker_rebate_rate
        return MAKER_REBATE_SHARE * rate

    def _walk_levels(self, s: Signal, levels, max_size: float,
                     cash_left: float, rate: float,
                     ) -> tuple[float, float, float, list[tuple[float, float]]]:
        """Marketable Levels abfüllen: (filled, cost, fee, takes) zum Level-Preis.

        cash_left deckelt BUYs (inkl. Gebühr) — Polymarket kennt keine Margin.
        Verfügbar pro Level ist nur, was WIR nicht schon konsumiert haben
        (self._consumed_levels — persistent über Ticks UND innerhalb eines
        Aufrufs, weil jeder Fill sofort committet); takes listet
        (Levelpreis, Shares) für Commit/Rollback des Verbrauchs.
        """
        used_levels = self._consumed_levels.get((s.token_id, s.side), {})
        filled = cost = fee = 0.0
        takes: list[tuple[float, float]] = []
        remaining = max_size
        for lv in levels:
            if remaining <= 1e-9:
                break
            # Levels sind sortiert -> erstes unmarketables Level beendet.
            if (s.side == "BUY" and lv.price > s.price) or \
               (s.side == "SELL" and lv.price < s.price):
                break
            avail = max(0.0, lv.size - used_levels.get(lv.price, 0.0))
            take = min(remaining, avail)
            fee_ps = rate * lv.price * (1.0 - lv.price)
            if s.side == "BUY":
                # Kein Kauf über das Cash hinaus.
                cost_ps = lv.price + fee_ps
                take = min(take, cash_left / cost_ps if cost_ps > 0 else 0.0)
            if take <= 1e-12:
                continue
            if s.side == "BUY":
                cash_left -= take * (lv.price + fee_ps)
            filled += take
            cost += take * lv.price
            fee += take * fee_ps
            takes.append((lv.price, take))
            remaining -= take
        return filled, cost, fee, takes

    # ---- Persistenter Liquiditätsverbrauch (über Ticks) ---------------------

    # Obergrenze für getrackte (Token, Seite)-Einträge — ältestes fliegt
    # zuerst (Befund 12: Tokens ohne je wieder ein Buch wüchsen unbegrenzt).
    MAX_CONSUMED_ENTRIES = 5000

    def _commit_consumption(self, token_id: str, side: str,
                            takes: list[tuple[float, float]]) -> None:
        if not takes:
            return
        d = self._consumed_levels.setdefault((token_id, side), {})
        for price, shares in takes:
            d[price] = d.get(price, 0.0) + shares
        while len(self._consumed_levels) > self.MAX_CONSUMED_ENTRIES:
            self._consumed_levels.pop(next(iter(self._consumed_levels)))

    def _revert_consumption(self, token_id: str, side: str,
                            takes: list[tuple[float, float]]) -> None:
        d = self._consumed_levels.get((token_id, side))
        if not d:
            return
        for price, shares in takes:
            d[price] = max(0.0, d.get(price, 0.0) - shares)

    def _gc_consumption(self, books: dict[str, OrderBook]) -> None:
        """Verbrauch von Levels vergessen, die im aktuellen Buch fehlen.

        Level weg = der Markt hat sich real bewegt; taucht derselbe Preis
        später wieder auf, ist das neue Liquidität. Tokens ohne Buch in
        diesem Aufruf bleiben unangetastet (Buch nur gerade nicht geladen).
        """
        for (token, side), levels in list(self._consumed_levels.items()):
            book = books.get(token)
            if book is None:
                continue
            side_levels = book.asks if side == "BUY" else book.bids
            if not side_levels or all(lv.size <= 0 for lv in side_levels):
                # Synthetisches Top-of-Book (Grösse 0) bzw. Flicker-Leere:
                # kein echtes Markt-Update — Verbrauch NICHT vergessen,
                # sonst kehrt die Fill-Inflation zurück (Befund 9).
                continue
            current = {lv.price for lv in side_levels}
            for price in [p for p in levels if p not in current]:
                del levels[price]
            if not levels:
                del self._consumed_levels[(token, side)]

    # ---- Ruhende Orders (Maker-Simulation) ---------------------------------

    def _match_resting(self, books: dict[str, OrderBook], portfolio: Portfolio,
                       fee_rates: dict[str, float]) -> int:
        """Ruhende Orders gegen den aktuellen Book-Snapshot prüfen und füllen.

        Konservativ: eine ruhende BUY füllt erst, wenn der beste Ask auf oder
        unter den Orderpreis gefallen ist (der Markt hat unsere Quote
        durchschritten) — analog SELL gegen den besten Bid. Gefüllt wird zum
        ORDERpreis als Maker (Gebühr 0), höchstens die bis zum Orderpreis
        vorhandene Gegenliquidität; der Rest bleibt ruhen. BUY-Fills sind
        durch die Cash-Reservierung gedeckt, SELLs werden am Bestand gekappt.
        """
        fills = 0
        still_resting: list[RestingOrder] = []
        for o in portfolio.resting_orders:
            book = books.get(o.token_id)
            if not book:
                still_resting.append(o)
                continue
            # Gegenliquidität bis zum Orderpreis, abzüglich dessen, was WIR
            # auf diesen Levels schon in früheren Ticks konsumiert haben —
            # sonst füllt dieselbe stehende Gegenseite die Quote jeden Tick
            # aufs Neue (dieselbe Inflation wie bei Taker-Fills).
            used_levels = self._consumed_levels.get((o.token_id, o.side), {})
            if o.side == "BUY":
                crossing = [lv for lv in book.asks if lv.price <= o.price + 1e-9]
            else:
                crossing = [lv for lv in book.bids if lv.price >= o.price - 1e-9]
            take_cap = o.size
            if o.side == "SELL":
                pos = portfolio.positions.get(o.token_id)
                take_cap = min(take_cap, pos.shares if pos else 0.0)
            take = 0.0
            takes: list[tuple[float, float]] = []
            for lv in crossing:
                if take >= take_cap - 1e-9:
                    break
                avail = max(0.0, lv.size - used_levels.get(lv.price, 0.0))
                lv_take = min(take_cap - take, avail)
                if lv_take > 1e-12:
                    take += lv_take
                    takes.append((lv.price, lv_take))
            if take <= 1e-9:
                still_resting.append(o)
                continue
            self._commit_consumption(o.token_id, o.side, takes)
            fill = Fill(ts=time.time(), token_id=o.token_id, side=o.side,
                        price=o.price, size=take, reason=o.reason, fee=0.0)
            portfolio.apply_fill(fill)
            # Offizieller Maker-Verdienstkanal: Rebate-Anteil der Taker-Fees
            # des Marktes — tokenspezifisch, siehe _rebate_rate.
            portfolio.credit_rebate(take * self._rebate_rate(o.token_id, fee_rates)
                                    * o.price * (1.0 - o.price))
            fills += 1
            log.info("Paper-Maker-Fill: %s %.0f Shares @%.3f (Gebühr 0) — %s (%s)",
                     o.side, take, o.price, o.market_question[:50], o.reason)
            o.size -= take
            if o.size > 1e-9:
                still_resting.append(o)
        portfolio.resting_orders = still_resting
        return fills

    @staticmethod
    def _cancel_resting(portfolio: Portfolio, token_id: str) -> float:
        """Alle ruhenden Orders eines Tokens entfernen (replace-Semantik).

        Rückgabe: freigewordene Cash-Reservierung der entfernten BUYs.
        """
        freed = sum(o.price * o.size for o in portfolio.resting_orders
                    if o.token_id == token_id and o.side == "BUY")
        portfolio.resting_orders = [o for o in portfolio.resting_orders
                                    if o.token_id != token_id]
        return freed

    def _rest_remainder(self, s: Signal, filled: float, portfolio: Portfolio,
                        cash_left: float) -> float:
        """Ungefüllten Rest eines ungruppierten Signals als GTC ruhen lassen.

        Rückgabe: dafür reserviertes Cash (BUY: Orderpreis * Restgröße).
        Deckung wie bei Sofort-Fills: BUYs am verfügbaren Cash gekappt,
        SELLs am noch nicht durch andere ruhende SELLs gebundenen Bestand.
        """
        rest = s.size - filled
        if rest <= 1e-9 or not 0.0 < s.price < 1.0:
            return 0.0
        if s.side == "BUY":
            rest = min(rest, max(cash_left, 0.0) / s.price)
        else:
            pos = portfolio.positions.get(s.token_id)
            bound = sum(o.size for o in portfolio.resting_orders
                        if o.token_id == s.token_id and o.side == "SELL")
            rest = min(rest, (pos.shares if pos else 0.0) - bound)
        if rest <= 1e-9:
            return 0.0
        portfolio.resting_orders.append(RestingOrder(
            ts=time.time(), token_id=s.token_id, side=s.side, price=s.price,
            size=rest, reason=s.reason, market_question=s.market_question))
        log.info("Paper: Order ruht im Buch: %s %.0f @%.3f — %s (%s)",
                 s.side, rest, s.price, s.market_question[:50], s.reason)
        return rest * s.price if s.side == "BUY" else 0.0

    # ---- Ausführung --------------------------------------------------------

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        # Latenz-Verzug: zuerst fällige Signale aus der Pending-Queue holen,
        # neue Signale ggf. einreihen. Gefüllt wird immer gegen das Buch
        # DIESES Aufrufs — zwischenzeitliche Buchbewegungen treffen die
        # Simulation damit genauso wie live die verspätet ankommende Order.
        due: list[Signal] = []
        waiting: list[list] = []
        for entry in self._pending_signals:
            entry[0] -= 1
            if entry[0] <= 0:
                due.extend(entry[1])  # FIFO: ältere Signale zuerst
            else:
                waiting.append(entry)
        self._pending_signals = waiting
        if self.fill_delay_ticks > 0:
            if signals:
                self._pending_signals.append([self.fill_delay_ticks, list(signals)])
        else:
            due.extend(signals)
        return self._fill_against_books(due, books, portfolio, fee_rates)

    def _fill_against_books(self, signals: list[Signal], books: dict[str, OrderBook],
                            portfolio: Portfolio,
                            fee_rates: dict[str, float] | None = None) -> int:
        """Signale gegen die übergebenen Bücher füllen (Kernlogik ohne Verzug)."""
        fee_rates = fee_rates or {}
        # Verfallenen Level-Verbrauch aufräumen (Buch hat sich real bewegt).
        self._gc_consumption(books)
        # Zuerst ruhende Orders gegen das aktuelle Buch prüfen (Maker-Fills);
        # die dabei konsumierte Liquidität sehen neue Signale nicht mehr
        # (jeder Fill committet sofort in self._consumed_levels).
        fills = self._match_resting(books, portfolio, fee_rates)
        # Laufendes Cash über alle geplanten Fills dieses Aufrufs. Konservativ:
        # Erlöse noch nicht gebuchter Gruppen-SELLs zählen nicht als verfügbar;
        # das von ruhenden BUYs reservierte Cash ist nicht verfügbar.
        cash_left = portfolio.cash - portfolio.reserved_cash
        # Von noch nicht gebuchten Gruppen-SELLs reservierte Shares je Token.
        reserved: dict[str, float] = {}
        # Geplante Beine je Gruppe (gebucht erst, wenn alle Beine voll füllbar
        # waren) plus Rollback-Infos:
        # (token, side, filled, cash_used, reserviert, level_takes).
        group_plans: dict[str, list[Fill]] = {}
        group_state: dict[str, list[tuple[str, str, float, float, float, list]]] = {}
        failed_groups: set[str] = set()

        def fail_group(group: str) -> float:
            """Gruppe verwerfen: tentativ belegte Liquidität/Cash/Shares freigeben."""
            failed_groups.add(group)
            freed_cash = 0.0
            for tok, side, f_filled, cash_used, res, takes in group_state.pop(group, []):
                self._revert_consumption(tok, side, takes)
                freed_cash += cash_used
                if res > 0:
                    reserved[tok] -= res
            group_plans.pop(group, None)
            log.warning("Paper: Gruppe %s verworfen — ein Bein war nicht voll "
                        "füllbar, kein Bein wird gefüllt (FOK)", group)
            return freed_cash

        # Tokens, deren Alt-Quotes in diesem Aufruf schon ersetzt wurden.
        refreshed: set[str] = set()

        for s in signals:
            if s.replace and s.token_id not in refreshed:
                # Replace-Semantik wie im LiveBroker (Cancel vor Neuquote):
                # die neue Quote ersetzt die alten ruhenden Orders des Tokens,
                # deren Cash-Reservierung wird wieder frei.
                cash_left += self._cancel_resting(portfolio, s.token_id)
                refreshed.add(s.token_id)
            if s.group and s.group in failed_groups:
                continue
            book = books.get(s.token_id)
            if not book:
                if s.group:
                    cash_left += fail_group(s.group)
                continue
            rate = fee_rates.get(s.token_id, self.fallback_fee_rate)
            if s.side == "BUY":
                levels = book.asks
                max_size = s.size
            else:
                levels = book.bids
                # Kein Verkauf über den Bestand hinaus (kein Shorting);
                # von geplanten Gruppen-SELLs reservierte Shares sind belegt.
                pos = portfolio.positions.get(s.token_id)
                held = (pos.shares if pos else 0.0) - reserved.get(s.token_id, 0.0)
                max_size = min(s.size, max(held, 0.0))
            filled, cost, fee, takes = self._walk_levels(s, levels, max_size,
                                                         cash_left, rate)
            if s.group and filled < s.size - 1e-9:
                # FOK: Bein nicht in voller Größe füllbar -> ganze Gruppe weg.
                cash_left += fail_group(s.group)
                continue
            if filled > 1e-9:
                # Verbrauch sofort festhalten (sichtbar für spätere Signale
                # DIESES Aufrufs und alle künftigen Ticks); bei FOK-Rollback
                # gibt fail_group ihn wieder frei.
                self._commit_consumption(s.token_id, s.side, takes)
                cash_used = cost + fee if s.side == "BUY" else 0.0
                cash_left -= cash_used
                if s.side == "SELL" and not s.group:
                    # Erlöse sofort gebuchter SELLs stehen Folgekäufen zur Verfügung.
                    cash_left += cost - fee
                fill = Fill(ts=time.time(), token_id=s.token_id, side=s.side,
                            price=cost / filled, size=filled, reason=s.reason, fee=fee)
                if s.group:
                    res = filled if s.side == "SELL" else 0.0
                    if res > 0:
                        reserved[s.token_id] = reserved.get(s.token_id, 0.0) + res
                    group_plans.setdefault(s.group, []).append(fill)
                    group_state.setdefault(s.group, []).append(
                        (s.token_id, s.side, filled, cash_used, res, takes))
                else:
                    portfolio.apply_fill(fill)
                    fills += 1
                    log.info("Paper-Fill: %s %.0f Shares @%.3f (VWAP) — %s (%s)",
                             s.side, filled, fill.price, s.market_question[:50], s.reason)
            else:
                log.debug("Paper: kein Sofort-Fill für %s %s @%.3f",
                          s.side, s.token_id[:12], s.price)
            if not s.group:
                # GTC-Semantik: der nicht-marketable Rest ruht als Quote im
                # Buch und füllt später als Maker (siehe _match_resting).
                cash_left -= self._rest_remainder(s, filled, portfolio, cash_left)
        # Vollständig füllbare Gruppen jetzt atomar buchen (FOK erfüllt).
        for group, plan in group_plans.items():
            for fill in plan:
                portfolio.apply_fill(fill)
                fills += 1
                log.info("Paper-Fill (Gruppe %s): %s %.0f Shares @%.3f (VWAP) — %s",
                         group, fill.side, fill.size, fill.price, fill.reason)
        return fills


@dataclass
class _PendingOrder:
    """Ruhende Live-Order (GTC), deren Fills asynchron reconciled werden."""

    order_id: str
    token_id: str
    side: str
    price: float
    reason: str
    taker: bool = False       # ruhende GTC-Orders füllen als Maker -> Gebühr 0
    booked_size: float = 0.0  # bereits ins Portfolio gebuchte Menge
    misses: int = 0           # aufeinanderfolgende gescheiterte get_order-Abfragen


class LiveBroker(Broker):
    """Echte Orders über den offiziellen py-clob-client-v2.

    Seit dem Exchange-Upgrade vom 28.04.2026 (CTF Exchange V2, pUSD statt
    USDC.e) ist der v1-Client inkompatibel — dieser Broker nutzt v2.
    Erfordert POLY_PRIVATE_KEY (und für Proxy-Wallets POLY_FUNDER_ADDRESS)
    in der Umgebung.

    Ausführungsregeln:
    - Gebucht wird nur, was laut CLOB tatsächlich gematcht ist (Status
      "matched" bzw. size_matched), zu den tatsächlichen Mengen/Preisen aus
      der Response — nie die volle Signalgröße auf Verdacht.
    - Arb-Beine gehen als FOK raus; scheitert ein Bein einer Gruppe, werden
      die restlichen Beine übersprungen und bereits gefüllte Beine per
      FAK-Gegenorder glattgestellt.
    - Taker-Fills (FOK/FAK, sofortige Matches) verbuchen die Taker-Gebühr
      rate * p * (1-p) pro Share; ruhende GTC-Maker zahlen 0.
    """

    # Polling für Orders mit Matching-Delay (z.B. Sport in-play).
    # In-play-Märkte (Sport/Esports) haben serverseitig einen Matching-Delay;
    # 15s Poll-Fenster, sonst verliert das Cancel das Race gegen das Matching
    # (Lehre vom ersten Live-Trade 05.07.: Order matchte nach >5s trotz Cancel).
    DELAY_POLL_ATTEMPTS = 30
    DELAY_POLL_INTERVAL_S = 0.5
    # Keep-Alive (Flotten-Befund 06.07.2026): Die CLOB-Verbindung stirbt nach
    # ~5s Leerlauf; der erste Request einer neuen Gelegenheit zahlt dann den
    # kalten TLS-Handshake (gemessen: 475-698ms kalt vs. 131-153ms warm; 4s
    # hält 146ms, 6s reicht nicht). Ein Hintergrund-Ping alle HEARTBEAT_S
    # hält die Verbindung warm — Gelegenheiten liegen Minuten auseinander,
    # ohne Heartbeat wäre JEDER Schuss kalt.
    HEARTBEAT_S = 4.0
    # Nach so vielen erfolglosen get_order-Abfragen wird das Tracking beendet.
    MAX_RECONCILE_MISSES = 10
    # Ablehnungstexte, die einen Konfigurationsfehler bedeuten: Wiederholen
    # ist zwecklos, bis der Betreiber eingreift -> Sperre bis Prozessende.
    FATAL_REJECT_MARKERS = ("maker address not allowed",)

    def __init__(self, cfg: BotConfig):
        from py_clob_client_v2.client import ClobClient

        kwargs = {
            "key": cfg.private_key,
            "chain_id": POLYGON_CHAIN_ID,
        }
        if cfg.funder_address:
            # Deposit-Wallet-Flow (POLY_SIGNATURE_TYPE=3/POLY_1271) bzw.
            # Proxy-Wallets: maker/funder ist das Smart-Contract-Wallet,
            # signiert wird weiterhin mit dem EOA-Key.
            kwargs["signature_type"] = cfg.signature_type
            kwargs["funder"] = cfg.funder_address
            if cfg.signature_type == 3:
                log.info("Deposit-Wallet-Modus: Orders als POLY_1271, "
                         "Funder %s", cfg.funder_address)
        self.client = ClobClient(CLOB_HOST, **kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.fallback_fee_rate = cfg.risk.taker_fee_rate
        # Reject-Cooldown: Token nach harter Ablehnung (400/403) so viele
        # Sekunden nicht erneut versuchen; None-Eintrag gibt es nicht.
        self.reject_cooldown_s = cfg.risk.order_reject_cooldown_s
        self._reject_until: dict[str, float] = {}
        # Konfigurationsfehler (z.B. "maker address not allowed"): dauerhaft
        # bis Prozessende gesperrt, EIN klarer Log-Hinweis statt Spam.
        self._fatal_reject: str | None = None
        # Ruhende eigene Orders (GTC-Quotes) je Token — werden vor dem
        # Neu-Quoten gecancelt, damit sich keine veralteten Quotes stapeln.
        self._open_orders: dict[str, list[str]] = {}
        # Alle ruhenden Orders je orderID — ihre (Teil-)Fills werden zu Beginn
        # jedes Ticks gegen den Order-Status reconciled.
        self._pending: dict[str, _PendingOrder] = {}
        # On-Chain-Merges (Kapital-Recycling, siehe main.live_merge_positions):
        # ein Init-Fehler (RPC/Abhängigkeit) deaktiviert nur das Auto-Merge,
        # der Handel selbst läuft unverändert weiter.
        self.merger = None
        if cfg.risk.live_auto_merge:
            try:
                from polybot.onchain import MergeExecutor

                self.merger = MergeExecutor(cfg.private_key)
            except Exception as e:  # noqa: BLE001 — Merge ist nie handelskritisch
                log.error("MergeExecutor nicht initialisierbar: %s — "
                          "Live-Auto-Merge deaktiviert", e)
        log.info("Live-Broker verbunden (Adresse %s)", self.client.get_address())
        # Keep-Alive-Heartbeat: hält die CLOB-Verbindung warm (siehe
        # HEARTBEAT_S). Daemon-Thread — stirbt mit dem Prozess; stop_heartbeat()
        # beendet ihn sauber beim geordneten Shutdown.
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="clob-keepalive", daemon=True)
        self._hb_thread.start()
        # Tokens, deren Tick-Größe schon im Client-Cache liegt (Prewarm).
        self._prewarmed: set[str] = set()
        # Start-Hygiene (Befund Agenten-Flotte 05.07.2026): Orders eines
        # abgestürzten/gestoppten Vorgänger-Prozesses leben auf der Börse
        # weiter und füllen unbeaufsichtigt — beim Start alles canceln.
        # Der neue Prozess kennt die Alt-Orders nicht (kein persistiertes
        # Order-Tracking), Cancel ist die einzige sichere Option; gefüllte
        # Mengen holt der Settlement-/Waisen-Pfad über die Positionen ein.
        # Scheitert das Cancel (CLOB-Glitch), startet der Bot trotzdem —
        # aber der Versuch wird pro Tick wiederholt, bis er gelingt
        # (Befund 15: sonst quotet er auf lebende Geister-Orders drauf).
        self._start_cancel_pending = not self.cancel_all_orders("Prozessstart")

    # ---- Not-Aus: alle offenen Börsen-Orders canceln -------------------------

    def cancel_all_orders(self, why: str, portfolio: Portfolio | None = None,
                          fee_rates: dict[str, float] | None = None) -> bool:
        """ALLE offenen Orders dieses Kontos auf dem CLOB canceln.

        Der gefährlichste Zustand ist »Bot tot, Orders leben«: ruhende
        GTC-Orders (MM-Quotes, Waisen-SELLs) füllen nach Kill-Switch/Crash
        unbeaufsichtigt weiter, ohne dass irgendjemand sie bucht. Wird beim
        Prozessstart und beim Prozessende (cmd_run finally) gerufen.

        Mit portfolio läuft VOR dem Cancel ein letzter Reconcile-Pass und
        NACH dem Cancel noch einer (Verifikations-Befund 13/25/31: Fills
        der letzten Sekunden bzw. aus dem Cancel-Race gingen sonst
        endgültig verloren — _pending wurde kommentarlos geleert).
        Wirft nie — ein fehlgeschlagenes Cancel wird laut geloggt, damit
        der Betreiber von Hand eingreifen kann.
        """
        def reconcile_quiet():
            if portfolio is None or not self._pending:
                return
            try:
                self._reconcile_pending(portfolio, fee_rates or {})
            except Exception as e:  # noqa: BLE001
                log.warning("Reconcile vor/nach cancel_all fehlgeschlagen: %s", e)

        reconcile_quiet()
        try:
            self.client.cancel_all()
            # Race-Fenster schliessen: was zwischen letztem Reconcile und
            # Cancel-Wirkung noch matchte, jetzt nachbuchen.
            reconcile_quiet()
            self._pending.clear()
            self._open_orders.clear()
            log.info("Alle offenen Börsen-Orders gecancelt (%s)", why)
            return True
        except Exception as e:  # noqa: BLE001 — Not-Aus darf nie selbst crashen
            log.error("cancel_all fehlgeschlagen (%s): %s — offene Orders "
                      "ggf. VON HAND auf polymarket.com prüfen!", why, e)
            return False

    # ---- Keep-Alive ----------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Hält die CLOB-Verbindung warm; wirft nie (Daemon-Thread)."""
        while not self._hb_stop.wait(self.HEARTBEAT_S):
            try:
                self.client.get_ok()
            except Exception as e:  # noqa: BLE001 — Heartbeat ist nie kritisch
                log.debug("Keep-Alive-Ping fehlgeschlagen: %s", e)

    def stop_heartbeat(self) -> None:
        """Heartbeat-Thread sauber beenden (geordneter Shutdown)."""
        hb = getattr(self, "_hb_stop", None)
        if hb is not None:
            hb.set()

    # ---- Tick-Size-Prewarm (Hot-Path-Latenz) ---------------------------------

    # Höchstens so viele NEUE Tick-Größen pro Prewarm-Aufruf holen — läuft im
    # Hintergrund-Worker (nicht im Hot Path), aber der Rate-Limit-Schutz gilt.
    PREWARM_MAX_PER_CALL = 40

    def prewarm_ticks(self, token_ids) -> int:
        """Tick-Größen der Arb-Kandidaten vorab in den Client-Cache holen.

        Flotten-Befund 06.07.2026: get_tick_size ist ein ~145ms-HTTP-GET,
        der Client cacht aber prozessweit. Im Hot Path (Order-Bau,
        _quantize_fok_groups) kostet die ERSTE Abfrage je Token genau diese
        145ms — mal zwei Beine sequenziell ~290ms pro Gruppe. Wird die
        Abfrage vom SnapshotWorker vorgezogen (alle ~90s, off Hot Path),
        ist der Tick beim Signal bereits im Cache. Rückgabe: Anzahl neu
        geholter Ticks. Wirft nie (Hilfspfad).
        """
        done = 0
        for t in token_ids:
            if done >= self.PREWARM_MAX_PER_CALL:
                break
            if t in self._prewarmed:
                continue
            self._prewarmed.add(t)
            try:
                self.client.get_tick_size(t)
                done += 1
            except Exception as e:  # noqa: BLE001 — Prewarm nie kritisch
                # Cache-Miss bleibt: beim nächsten Zyklus erneut versuchen.
                self._prewarmed.discard(t)
                log.debug("Tick-Prewarm für %s fehlgeschlagen: %s", t[:12], e)
        if done:
            log.debug("Tick-Prewarm: %d neue Tick-Größen gecacht", done)
        return done

    # ---- Ausführung --------------------------------------------------------

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        from py_clob_client_v2.clob_types import OrderType

        fee_rates = fee_rates or {}
        if getattr(self, "_start_cancel_pending", False):
            # Start-Hygiene nachholen (Befund 15): Geister-Orders des
            # Vorgängers so lange erneut canceln, bis es gelingt.
            self._start_cancel_pending = not self.cancel_all_orders(
                "Prozessstart-Retry", portfolio, fee_rates)
        # Zuerst reale (Teil-)Fills ruhender Orders nachbuchen — Portfolio und
        # Risiko-Limits dürfen weder Phantom- noch fehlende Positionen sehen.
        fills = self._reconcile_pending(portfolio, fee_rates)
        if self._fatal_reject is not None:
            # Konfigurationsfehler: der Hinweis stand EINMAL im Log (siehe
            # _register_reject) — hier nur noch leise verwerfen, kein Spam.
            if signals:
                log.debug("%d Signal(e) verworfen — Orders gesperrt seit: %s",
                          len(signals), self._fatal_reject)
            return fills
        # Arb-Gruppen vorab auf eine gemeinsame, börsenkonforme Size bringen
        # (CLOB-Präzisionsregeln für Market-Orders; Beine müssen gleich groß
        # bleiben, sonst bliebe ein ungehedgter Rest).
        signals = self._quantize_fok_groups(signals)
        # FOK sichert nur die Einzelorder, nicht die Arb-Gruppe: scheitert ein
        # Bein, dürfen die restlichen Beine der Gruppe nicht mehr raus.
        failed_groups: set[str] = set()
        quiet_groups: set[str] = set()  # per Cooldown übersprungen: kein Log-Spam
        group_fills: dict[str, list[Fill]] = {}  # gebuchte Beine je Gruppe (für Unwind)
        refreshed: set[str] = set()  # Tokens, deren Alt-Quotes dieser Tick schon gecancelt sind
        for s in signals:
            if s.group and s.group in failed_groups:
                if s.group in quiet_groups:
                    log.debug("Gruppe %s: Bein %s übersprungen (Cooldown-Gruppe)",
                              s.group, s.token_id[:12])
                else:
                    log.warning("Gruppe %s: Bein %s übersprungen, da ein voriges "
                                "Bein scheiterte", s.group, s.token_id[:12])
                continue
            if self._fatal_reject is not None or self._token_blocked(s.token_id):
                # Reject-Cooldown bzw. mid-Tick erkannter Konfigurationsfehler:
                # dieselbe Order würde nur wieder abgelehnt. Kein ERROR-Log —
                # der Grund stand beim Verhängen des Cooldowns bereits im Log,
                # und dieser Zweig feuert sonst jeden 0.5s-Tick erneut.
                log.debug("Token %s im Reject-Cooldown — Signal übersprungen",
                          s.token_id[:12])
                if s.group:
                    failed_groups.add(s.group)
                    quiet_groups.add(s.group)
                    fills += self._unwind_group(s.group, group_fills, books,
                                                portfolio, fee_rates)
                continue
            try:
                if s.replace and s.token_id not in refreshed:
                    self._cancel_open_orders(s.token_id)
                    refreshed.add(s.token_id)
                # Arb-Beine als FOK (ganz oder gar nicht), Rest als GTC
                otype = OrderType.FOK if s.group else OrderType.GTC
                outcome, fill = self._submit_signal(s, otype, portfolio, fee_rates)
            except Exception as e:  # noqa: BLE001 — Bot darf durch eine Order nicht sterben
                log.error("Live-Order fehlgeschlagen (%s): %s", s.market_question[:40], e)
                outcome, fill = "failed", None
            if outcome == "matched":
                fills += 1
                if s.group and fill:
                    group_fills.setdefault(s.group, []).append(fill)
            elif outcome == "failed":
                self._abort_group(s, failed_groups)
                if s.group:
                    fills += self._unwind_group(s.group, group_fills, books,
                                                portfolio, fee_rates)
            # outcome "pending": Order ruht im Buch — Fills kommen später
            # über _reconcile_pending, nicht als sofortige Buchung.
        return fills

    def _quantize_fok_groups(self, signals: list[Signal]) -> list[Signal]:
        """Arb-Gruppen auf eine gemeinsame, börsenkonforme Size quantisieren.

        Jedes FOK-Bein muss die Market-Order-Präzision einhalten (BUY:
        Size*Preis max. 2 Nachkommastellen, SELL: max. 4) UND alle Beine
        einer Gruppe müssen dieselbe Stückzahl behalten. Gruppen ohne
        gültige gemeinsame Size werden verworfen (einmal geloggt) statt
        vom Server abgelehnt zu werden.
        """
        import dataclasses

        groups: dict[str, list[Signal]] = {}
        for s in signals:
            if s.group:
                groups.setdefault(s.group, []).append(s)
        if not groups:
            return signals
        drop: set[str] = set()
        common: dict[str, float] = {}
        for g, legs in groups.items():
            try:
                prices = [
                    int(round(_quantize_price(
                        s.price, float(self.client.get_tick_size(s.token_id)),
                        s.side) * 1_000_000))
                    for s in legs
                ]
            except Exception as e:  # noqa: BLE001 — dann prüft es der Server
                log.warning("Gruppe %s: Tick-Abfrage für Size-Quantisierung "
                            "fehlgeschlagen: %s", g, e)
                continue
            mods = [1_000_000 if s.side == "BUY" else 10_000 for s in legs]
            k = min(int(math.floor(s.size * 100 + 1e-9)) for s in legs)
            while k > 0 and any((k * p) % m for p, m in zip(prices, mods)):
                k -= 1
            if k <= 0:
                drop.add(g)
                log.info("Gruppe %s: keine börsenkonforme gemeinsame Size — "
                         "Gelegenheit übersprungen", g)
                continue
            common[g] = k / 100
        out: list[Signal] = []
        for s in signals:
            if s.group in drop:
                continue
            if s.group in common and abs(s.size - common[s.group]) > 1e-9:
                s = dataclasses.replace(s, size=common[s.group])
            out.append(s)
        return out

    def _submit_signal(self, s: Signal, otype, portfolio: Portfolio,
                       fee_rates: dict[str, float],
                       tick_size: str | None = None) -> tuple[str, Fill | None]:
        """Eine Order quantisieren, bauen, posten und das Ergebnis verbuchen.

        Rückgabe: ("matched", Fill) bei bestätigtem Match,
                  ("pending", None) für ruhende GTC-Orders,
                  ("failed", None) bei Ablehnung/Fehler.
        """
        from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        try:
            tick = float(tick_size) if tick_size else float(self.client.get_tick_size(s.token_id))
            price = _quantize_price(s.price, tick, s.side)
            size = round(s.size, 2)
            if otype != OrderType.GTC:
                # FOK/FAK prüft der Server als Market-Order: Beträge müssen
                # exakt aufs Präzisionsraster passen, sonst 400 "invalid amounts".
                size = _marketable_size(size, price, s.side)
                if size <= 0:
                    log.warning("Order verworfen (%s): keine börsenkonforme "
                                "Size für %s @%.6f", s.market_question[:40],
                                s.side, price)
                    return "failed", None
            order = self.client.create_order(
                OrderArgs(
                    token_id=s.token_id,
                    price=price,
                    size=size,
                    side=BUY if s.side == "BUY" else SELL,
                ),
                # Kein explizites tick_size (create_order löst den Tick selbst
                # auf) — außer beim Retry nach Tick-Wechsel, wo der frische
                # Wert den veralteten Client-Cache übersteuern muss.
                # NegRisk-Märkte laufen über den NegRisk-Exchange (andere
                # EIP-712-Domain) — muss deklariert werden.
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=s.neg_risk),
            )
        except Exception as e:  # noqa: BLE001
            # Vor dem POST gescheitert -> nichts unterwegs. "invalid price"
            # kann ein zwischenzeitlich gewechselter Tick sein: einmal mit
            # frischem Tick (am Client-Cache vorbei) neu versuchen.
            if tick_size is None and _looks_like_tick_error(str(e)):
                return self._retry_with_fresh_tick(s, otype, portfolio, fee_rates)
            log.error("Order-Aufbau fehlgeschlagen (%s): %s", s.market_question[:40], e)
            return "failed", None

        posted_at = time.time()
        try:
            resp = self.client.post_order(order, otype)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) in (400, 403):
                # Definitive Server-Ablehnung (nichts unterwegs): erst der
                # Tick-Retry-Sonderfall, sonst Cooldown statt Wiederholungs-Spam.
                if tick_size is None and _looks_like_tick_error(str(e)):
                    return self._retry_with_fresh_tick(s, otype, portfolio, fee_rates)
                log.warning("Order abgelehnt (HTTP %s, %s): %s",
                            getattr(e, "status_code", "?"),
                            s.market_question[:40], e)
                self._register_reject(s.token_id, str(e))
                return "failed", None
            # Timeout & Co.: Der Server kann die Order trotzdem angenommen
            # haben -> Zustand verifizieren statt still weitermachen.
            log.error("POST /order unklar gescheitert (%s): %s — verifiziere Orderzustand",
                      s.market_question[:40], e)
            self._recover_unknown_state(s.token_id, posted_at)
            return "failed", None
        if not isinstance(resp, dict):
            # 200-Antwort ohne JSON: Zustand genauso unklar wie ein Timeout.
            log.error("POST /order lieferte keine auswertbare Antwort (%s): %r — verifiziere",
                      s.market_question[:40], resp)
            self._recover_unknown_state(s.token_id, posted_at)
            return "failed", None
        if not resp.get("success"):
            if tick_size is None and _looks_like_tick_error(str(resp)):
                return self._retry_with_fresh_tick(s, otype, portfolio, fee_rates)
            log.warning("Order abgelehnt: %s", resp)
            self._register_reject(s.token_id, str(resp))
            return "failed", None
        return self._book_response(s, resp, price, otype, portfolio, fee_rates)

    def _book_response(self, s: Signal, resp: dict, price: float, otype,
                       portfolio: Portfolio, fee_rates: dict[str, float]) -> tuple[str, Fill | None]:
        """Erfolgs-Response auswerten: nur bestätigte Matches buchen."""
        from py_clob_client_v2.clob_types import OrderType

        order_id = resp.get("orderID") or resp.get("order_id") or ""
        status = resp.get("status", "")
        if s.replace and order_id:
            self._open_orders.setdefault(s.token_id, []).append(order_id)
        if status == "matched":
            fill = self._fill_from_response(s, resp, price, fee_rates)
            self._apply_fill_safe(portfolio, fill)
            log.info("Live-Fill: %s %.2f Shares @%.4f (Fee %.4f) — %s",
                     s.side, fill.size, fill.price, fill.fee, s.market_question[:50])
            if otype == OrderType.GTC and order_id and fill.size < s.size - 1e-9:
                # Teil-Match: der Rest ruht im Buch und füllt als Maker.
                self._pending[order_id] = _PendingOrder(
                    order_id=order_id, token_id=s.token_id, side=s.side,
                    price=price, reason=s.reason, booked_size=fill.size,
                )
            return "matched", fill
        if status == "delayed":
            # Match ist noch nicht final (Matching-Delay, z.B. Sport in-play).
            return self._resolve_delayed(s, order_id, price, portfolio, fee_rates)
        if order_id:
            # "live": Order ruht ungefüllt im Buch — kein Fill, nur tracken.
            self._pending[order_id] = _PendingOrder(
                order_id=order_id, token_id=s.token_id, side=s.side,
                price=price, reason=s.reason,
            )
            log.info("Live-Order ruht im Buch: %s %.2f @%.4f — %s",
                     s.side, s.size, price, s.market_question[:50])
            return "pending", None
        log.warning("Order-Antwort ohne orderID/Status — kein Fill gebucht: %s", resp)
        return "failed", None

    def _resolve_delayed(self, s: Signal, order_id: str, price: float,
                         portfolio: Portfolio, fee_rates: dict[str, float]) -> tuple[str, Fill | None]:
        """Order mit Matching-Delay: auf finale Bestätigung warten.

        Bleibt sie aus, wird die Order gecancelt — eine später doch noch
        matchende Order wäre z.B. ein ungehedgtes Arb-Bein.
        """
        from py_clob_client_v2.clob_types import OrderPayload

        if not order_id:
            log.error("Delayed-Order ohne orderID (%s) — als Fehlschlag behandelt",
                      s.token_id[:12])
            return "failed", None
        for _ in range(self.DELAY_POLL_ATTEMPTS):
            time.sleep(self.DELAY_POLL_INTERVAL_S)
            o = self._get_order_safe(order_id)
            status = (o.get("status") or "") if o else ""
            if status == "matched":
                return "matched", self._book_order_state(s, o, price, portfolio, fee_rates)
            if status in ("canceled", "cancelled", "unmatched"):
                return self._book_delayed_leftover(s, o, order_id, price,
                                                   portfolio, fee_rates)
        cancel_ok = True
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception as e:  # noqa: BLE001
            cancel_ok = False
            log.warning("Cancel der delayed Order %s fehlgeschlagen: %s", order_id, e)
        o = self._get_order_safe(order_id)
        if o and o.get("status") == "matched":
            return "matched", self._book_order_state(s, o, price, portfolio, fee_rates)
        if not cancel_ok and (o is None or o.get("status") in ("live", "delayed")):
            # Befund 28: Cancel fehlgeschlagen und Order lebt (womöglich)
            # weiter — Tracking behalten, damit _reconcile_pending spätere
            # Fills nachbucht (der Waisen-Detektor stellt sie dann glatt).
            # Für die Gruppen-Logik zählt das Bein als Fehlschlag (Unwind).
            matched_so_far = _to_float(((o or {}).get("size_matched")
                                        or (o or {}).get("sizeMatched")))
            self._pending[order_id] = _PendingOrder(
                order_id=order_id, token_id=s.token_id, side=s.side,
                price=price, reason=s.reason, taker=True,
                booked_size=matched_so_far)
            log.error("Delayed-Order %s lebt nach Cancel-Fehlschlag ggf. "
                      "weiter — bleibt im Reconcile-Tracking", order_id)
            return "failed", None
        return self._book_delayed_leftover(s, o, order_id, price, portfolio, fee_rates)

    def _book_delayed_leftover(self, s: Signal, o: dict | None, order_id: str,
                               price: float, portfolio: Portfolio,
                               fee_rates: dict[str, float]) -> tuple[str, Fill | None]:
        """Endzustand einer delayed Order ehrlich verbuchen.

        Lehre vom ersten Live-Trade (05.07.): Das Cancel kann das Race gegen
        das Matching VERLIEREN — die Order ist dann trotz Status "canceled"
        (teil-)gefüllt. size_matched ist die Wahrheit, nicht der Status; ohne
        diese Buchung driftet das Ledger von der Realität weg und das
        Arb-Gegenbein fehlt.
        """
        matched = _to_float((o or {}).get("size_matched") or (o or {}).get("sizeMatched"))
        if matched > 1e-9:
            fill = self._book_order_state(s, o, price, portfolio, fee_rates)
            log.warning("Delayed-Order %s trotz Cancel (teil-)gefüllt: %.2f Shares "
                        "@%.4f — als Fill gebucht", order_id, fill.size, fill.price)
            return "matched", fill
        # Wirklich ungefüllt: sofortiges Neu-Feuern im nächsten 0.5s-Tick
        # würde nur das nächste Race provozieren und riskiert Doppel-Fills.
        if self.reject_cooldown_s > 0:
            self._reject_until[s.token_id] = time.time() + self.reject_cooldown_s
        log.error("Delayed-Order %s nicht bestätigt — gecancelt, als Fehlschlag "
                  "gewertet, Token %s für %.0fs im Cooldown", order_id,
                  s.token_id[:12], self.reject_cooldown_s)
        return "failed", None

    # ---- Buchung -----------------------------------------------------------

    def _fill_from_response(self, s: Signal, resp: dict, price: float,
                            fee_rates: dict[str, float]) -> Fill:
        """Fill aus einer matched-Response: tatsächliche Mengen/Preise buchen.

        makingAmount/takingAmount sind die real getauschten Beträge — der
        Client rundet Preise intern, und marketable Orders füllen zu besseren
        Preisen als dem Limit.
        """
        making = _to_float(resp.get("makingAmount"))
        taking = _to_float(resp.get("takingAmount"))
        if s.side == "BUY":
            size, quote = taking, making  # Shares erhalten, USDC gegeben
        else:
            size, quote = making, taking  # Shares gegeben, USDC erhalten
        if size <= 0:
            size = _to_float(resp.get("sizeMatched")) or s.size
            quote = size * price
        avg_price = quote / size if size > 0 else price
        rate = fee_rates.get(s.token_id, self.fallback_fee_rate)
        # Sofortiger Match = Taker-Fill -> Gebühr rate * p * (1-p) pro Share.
        fee = size * rate * avg_price * (1.0 - avg_price)
        return Fill(ts=time.time(), token_id=s.token_id, side=s.side,
                    price=avg_price, size=size, reason=s.reason, fee=fee)

    def _book_order_state(self, s: Signal, o: dict, fallback_price: float,
                          portfolio: Portfolio, fee_rates: dict[str, float]) -> Fill:
        """Fill aus einem get_order-Zustand buchen (delayed-Bestätigung)."""
        size = _to_float(o.get("size_matched") or o.get("sizeMatched")) or s.size
        price = _to_float(o.get("price")) or fallback_price
        rate = fee_rates.get(s.token_id, self.fallback_fee_rate)
        fill = Fill(ts=time.time(), token_id=s.token_id, side=s.side, price=price,
                    size=size, reason=s.reason, fee=size * rate * price * (1.0 - price))
        self._apply_fill_safe(portfolio, fill)
        log.info("Live-Fill (delayed bestätigt): %s %.2f Shares @%.4f — %s",
                 s.side, size, price, s.market_question[:50])
        return fill

    @staticmethod
    def _apply_fill_safe(portfolio: Portfolio, fill: Fill) -> None:
        """Fill buchen; Buchhaltungsfehler dürfen die reale Ausführung nicht kippen.

        Ein von der BÖRSE bestätigter Fill ist Chain-Realität — schlägt die
        normale Buchung fehl (Cash-/Bestands-Guard, weil die Buchhaltung
        z.B. nach einem Sync von der Chain abwich), wird FORCIERT gebucht
        statt verworfen (Befunde 21/29: der Fill ging sonst endgültig
        verloren, das Bein war für Flattener/Settlement unsichtbar).
        """
        try:
            portfolio.apply_fill(fill)
        except ValueError as e:
            log.error("Fill kollidiert mit der Buchhaltung (%s) — Chain-Fill "
                      "wird FORCIERT gebucht, Positions-Sync gleicht später "
                      "ab", e)
            portfolio.apply_fill(fill, force=True)

    # ---- Reconciliation ruhender Orders -------------------------------------

    def _reconcile_pending(self, portfolio: Portfolio, fee_rates: dict[str, float]) -> int:
        """(Teil-)Fills ruhender Orders seit dem letzten Tick nachbuchen."""
        fills = 0
        for oid, po in list(self._pending.items()):
            o = self._get_order_safe(oid)
            if o is None:
                po.misses += 1
                if po.misses >= self.MAX_RECONCILE_MISSES:
                    self._pending.pop(oid, None)
                    log.error("Order %s ist %d Ticks nicht abfragbar — Tracking beendet, "
                              "Fills manuell abgleichen!", oid, po.misses)
                continue
            po.misses = 0
            matched = _to_float(o.get("size_matched") or o.get("sizeMatched"))
            delta = matched - po.booked_size
            if delta > 1e-9:
                price = _to_float(o.get("price")) or po.price
                rate = fee_rates.get(po.token_id, self.fallback_fee_rate) if po.taker else 0.0
                fill = Fill(ts=time.time(), token_id=po.token_id, side=po.side,
                            price=price, size=delta, reason=po.reason,
                            fee=delta * rate * price * (1.0 - price))
                self._apply_fill_safe(portfolio, fill)
                po.booked_size = matched
                fills += 1
                log.info("Reconcile: %s %.2f Shares @%.4f nachgebucht (Order %s)",
                         po.side, delta, price, oid)
            if o.get("status") not in ("live", "delayed"):
                self._pending.pop(oid, None)
        return fills

    def _get_order_safe(self, order_id: str) -> dict | None:
        try:
            o = self.client.get_order(order_id)
            return o if isinstance(o, dict) else None
        except Exception as e:  # noqa: BLE001
            log.warning("get_order(%s) fehlgeschlagen: %s", order_id, e)
            return None

    # ---- Fehlerbehandlung ----------------------------------------------------

    def _token_blocked(self, token_id: str) -> bool:
        """Steht das Token noch im Reject-Cooldown? (Abgelaufene Einträge weg.)"""
        until = self._reject_until.get(token_id)
        if until is None:
            return False
        if time.time() >= until:
            del self._reject_until[token_id]
            return False
        return True

    def _register_reject(self, token_id: str, message: str) -> None:
        """Harte Ablehnung verbuchen: Token-Cooldown bzw. dauerhafte Sperre.

        "maker address not allowed" & Co. sind Konfigurationsfehler des
        Kontos (Deposit-Wallet-Flow fehlt) — jede weitere Order würde
        identisch abgelehnt: dauerhaft sperren, EIN klarer Hinweis im Log.
        Alles andere (Balance, geschlossener Markt, ...) bekommt einen
        Token-Cooldown von reject_cooldown_s Sekunden.
        """
        text = message.lower()
        if any(marker in text for marker in self.FATAL_REJECT_MARKERS):
            if self._fatal_reject is None:
                self._fatal_reject = message
                log.error(
                    "Order-Ablehnung ist ein KONFIGURATIONSFEHLER: %s — der "
                    "V2-CLOB verlangt den Deposit-Wallet-Flow. Abhilfe: "
                    "python -m polybot.main preflight --execute ausführen und "
                    "POLY_FUNDER_ADDRESS=<Deposit-Wallet> sowie "
                    "POLY_SIGNATURE_TYPE=3 in .env setzen. Bis zum Neustart "
                    "werden KEINE weiteren Orders versucht.", message)
            return
        if self.reject_cooldown_s > 0:
            self._reject_until[token_id] = time.time() + self.reject_cooldown_s
            log.warning("Token %s für %.0fs im Order-Cooldown nach harter "
                        "Ablehnung: %s", token_id[:12], self.reject_cooldown_s,
                        message[:160])

    def _cancel_open_orders(self, token_id: str) -> None:
        """Zuvor platzierte ruhende Orders eines Tokens canceln.

        Die finalen (Teil-)Fills gecancelter Orders bucht _reconcile_pending
        im nächsten Tick nach (der Order-Status bleibt abfragbar).
        """
        from py_clob_client_v2.clob_types import OrderPayload

        survivors: list[str] = []
        for oid in self._open_orders.pop(token_id, []):
            try:
                self.client.cancel_order(OrderPayload(orderID=oid))
            except Exception as e:  # noqa: BLE001
                survivors.append(oid)  # Befund 33: ID behalten -> Retry
                log.warning("Cancel für Order %s fehlgeschlagen: %s — wird "
                            "beim nächsten Requote erneut versucht", oid, e)
        if survivors:
            self._open_orders[token_id] = survivors

    def _fresh_tick(self, token_id: str) -> str | None:
        """Tick-Größe am Client-Cache vorbei frisch vom CLOB holen.

        Der Client cacht get_tick_size für die Prozesslebensdauer, Polymarket
        wechselt den Tick aber dynamisch, wenn der Preis Richtung 0/1 läuft.
        """
        import requests

        try:
            r = requests.get(f"{CLOB_HOST}/tick-size",
                             params={"token_id": token_id}, timeout=15)
            r.raise_for_status()
            return str(r.json()["minimum_tick_size"])
        except Exception as e:  # noqa: BLE001
            log.warning("Frische Tick-Größe für %s nicht abrufbar: %s", token_id[:12], e)
            return None

    def _retry_with_fresh_tick(self, s: Signal, otype, portfolio: Portfolio,
                               fee_rates: dict[str, float]) -> tuple[str, Fill | None]:
        """Nach invalid-tick/price-Ablehnung genau einmal mit frischem Tick retrien."""
        fresh = self._fresh_tick(s.token_id)
        if fresh is None:
            return "failed", None
        log.warning("Tick-Größe für %s evtl. gewechselt — neuer Versuch mit Tick %s",
                    s.token_id[:12], fresh)
        return self._submit_signal(s, otype, portfolio, fee_rates, tick_size=fresh)

    def _recover_unknown_state(self, token_id: str, since: float) -> None:
        """Nach Timeout/unbrauchbarer Antwort auf POST /order den Zustand verifizieren.

        Der Server kann die Order angenommen (und sogar gefüllt) haben, ohne
        dass eine auswertbare Antwort ankam. Unbekannte offene Orders auf dem
        Token werden gecancelt (verhindert hängende Orders und Doppel-Orders
        im nächsten Tick); frische Trades werden als Alarm geloggt, da sie
        ungebuchte Fills bedeuten können.
        """
        from py_clob_client_v2.clob_types import OpenOrderParams, OrderPayload, TradeParams

        known = set(self._pending) | {o for ids in self._open_orders.values() for o in ids}
        try:
            open_orders = self.client.get_open_orders(
                OpenOrderParams(asset_id=token_id), only_first_page=True) or []
            for o in open_orders:
                if not isinstance(o, dict):
                    continue
                oid = o.get("id") or o.get("orderID") or ""
                if not oid or oid in known:
                    continue
                log.error("Hängende Order %s auf %s nach unklarem POST entdeckt — cancel",
                          oid, token_id[:12])
                try:
                    self.client.cancel_order(OrderPayload(orderID=oid))
                except Exception as e:  # noqa: BLE001
                    log.error("Cancel der hängenden Order %s fehlgeschlagen: %s — "
                              "manuell prüfen!", oid, e)
        except Exception as e:  # noqa: BLE001
            log.error("Verifikation offener Orders für %s fehlgeschlagen: %s — "
                      "manuell prüfen!", token_id[:12], e)
        try:
            trades = self.client.get_trades(
                TradeParams(asset_id=token_id, after=int(since) - 1), only_first_page=True)
            if trades:
                log.error("%d Trade(s) auf %s seit dem unklaren POST — mögliche ungebuchte "
                          "Fills, Portfolio manuell abgleichen!", len(trades), token_id[:12])
        except Exception as e:  # noqa: BLE001
            log.warning("Trade-Verifikation für %s fehlgeschlagen: %s", token_id[:12], e)

    def _unwind_group(self, group: str, group_fills: dict[str, list[Fill]],
                      books: dict[str, OrderBook], portfolio: Portfolio,
                      fee_rates: dict[str, float]) -> int:
        """Bereits gefüllte Beine einer gescheiterten Arb-Gruppe glattstellen.

        Gegenorder als FAK zum besten Gegenkurs — lieber ein kleiner, sofort
        realisierter Verlust als eine offene, ungehedgte Wette.
        """
        from py_clob_client_v2.clob_types import OrderType

        fills = 0
        for f in group_fills.pop(group, []):
            book = books.get(f.token_id)
            level = (book.best_bid if f.side == "BUY" else book.best_ask) if book else None
            if level is None:
                log.error("Gruppe %s: kein Gegenkurs für Unwind von %s — Position bleibt "
                          "offen und muss manuell glattgestellt werden!",
                          group, f.token_id[:12])
                continue
            counter = Signal(
                token_id=f.token_id, side="SELL" if f.side == "BUY" else "BUY",
                price=level.price, size=f.size,
                reason=f"Unwind {group}", market_question=f"Unwind {group}",
            )
            outcome, _fill = self._submit_signal(counter, OrderType.FAK, portfolio, fee_rates)
            unwound = _fill.size if _fill is not None else 0.0
            if outcome == "matched" and unwound >= f.size - 1e-9:
                fills += 1
                log.warning("Gruppe %s: Bein %s glattgestellt (%s %.2f @%.4f)",
                            group, f.token_id[:12], counter.side, f.size, level.price)
            elif outcome == "matched":
                # FAK füllt, was geht, und cancelt den Rest — eine
                # Teilfüllung liess das Rest-Bein bisher STILL ungehedgt
                # (Verifikations-Befund 30). Der Waisen-Detektor stellt den
                # Rest nach der Schonfrist glatt; hier laut machen.
                fills += 1
                log.error("Gruppe %s: Unwind für %s nur TEILWEISE gefüllt "
                          "(%.2f von %.2f) — Rest ungehedgt, Waisen-Detektor "
                          "übernimmt nach Schonfrist", group, f.token_id[:12],
                          unwound, f.size)
            else:
                log.error("Gruppe %s: Unwind für %s nicht gefüllt — Position "
                          "offen; Waisen-Detektor übernimmt nach Schonfrist, "
                          "sonst manuell glattstellen!", group, f.token_id[:12])
        return fills

    @staticmethod
    def _abort_group(s: Signal, failed_groups: set[str]) -> None:
        """Gruppe nach gescheitertem Bein sperren; Rest-Beine werden nicht gesendet."""
        if s.group and s.group not in failed_groups:
            failed_groups.add(s.group)
            log.error("Arb-Gruppe %s abgebrochen: Bein %s scheiterte — bereits "
                      "gefüllte Beine werden per Gegenorder glattgestellt",
                      s.group, s.token_id[:12])


def make_broker(cfg: BotConfig) -> Broker:
    if cfg.mode == "live":
        return LiveBroker(cfg)
    return PaperBroker(cfg)
