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

    def _walk_levels(self, s: Signal, levels, max_size: float, skip: float,
                     cash_left: float, rate: float) -> tuple[float, float, float]:
        """Marketable Levels abfüllen: (filled, cost, fee) zum Level-Preis.

        skip = im selben Tick bereits konsumierte Buchliquidität dieser Seite;
        cash_left deckelt BUYs (inkl. Gebühr) — Polymarket kennt keine Margin.
        """
        filled = cost = fee = 0.0
        remaining = max_size
        for lv in levels:
            if remaining <= 1e-9:
                break
            # Levels sind sortiert -> erstes unmarketables Level beendet.
            if (s.side == "BUY" and lv.price > s.price) or \
               (s.side == "SELL" and lv.price < s.price):
                break
            avail = lv.size
            if skip > 0:
                used = min(skip, avail)
                avail -= used
                skip -= used
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
            remaining -= take
        return filled, cost, fee

    # ---- Ruhende Orders (Maker-Simulation) ---------------------------------

    def _match_resting(self, books: dict[str, OrderBook], portfolio: Portfolio,
                       consumed: dict[tuple[str, str], float],
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
            if o.side == "BUY":
                avail = sum(lv.size for lv in book.asks if lv.price <= o.price + 1e-9)
            else:
                avail = sum(lv.size for lv in book.bids if lv.price >= o.price - 1e-9)
            take = min(o.size, avail)
            if o.side == "SELL":
                pos = portfolio.positions.get(o.token_id)
                take = min(take, pos.shares if pos else 0.0)
            if take <= 1e-9:
                still_resting.append(o)
                continue
            fill = Fill(ts=time.time(), token_id=o.token_id, side=o.side,
                        price=o.price, size=take, reason=o.reason, fee=0.0)
            portfolio.apply_fill(fill)
            # Offizieller Maker-Verdienstkanal: Rebate-Anteil der Taker-Fees
            # des Marktes — tokenspezifisch, siehe _rebate_rate.
            portfolio.credit_rebate(take * self._rebate_rate(o.token_id, fee_rates)
                                    * o.price * (1.0 - o.price))
            consumed[(o.token_id, o.side)] = consumed.get((o.token_id, o.side), 0.0) + take
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
        fee_rates = fee_rates or {}
        # Innerhalb dieses Aufrufs bereits konsumierte Buchliquidität je
        # (Token, Seite): spätere Signale sehen nur noch die Restliquidität.
        consumed: dict[tuple[str, str], float] = {}
        # Zuerst ruhende Orders gegen das aktuelle Buch prüfen (Maker-Fills);
        # die dabei konsumierte Liquidität sehen neue Signale nicht mehr.
        fills = self._match_resting(books, portfolio, consumed, fee_rates)
        # Laufendes Cash über alle geplanten Fills dieses Aufrufs. Konservativ:
        # Erlöse noch nicht gebuchter Gruppen-SELLs zählen nicht als verfügbar;
        # das von ruhenden BUYs reservierte Cash ist nicht verfügbar.
        cash_left = portfolio.cash - portfolio.reserved_cash
        # Von noch nicht gebuchten Gruppen-SELLs reservierte Shares je Token.
        reserved: dict[str, float] = {}
        # Geplante Beine je Gruppe (gebucht erst, wenn alle Beine voll füllbar
        # waren) plus Rollback-Infos: (token, side, filled, cash_used, reserviert).
        group_plans: dict[str, list[Fill]] = {}
        group_state: dict[str, list[tuple[str, str, float, float, float]]] = {}
        failed_groups: set[str] = set()

        def fail_group(group: str) -> float:
            """Gruppe verwerfen: tentativ belegte Liquidität/Cash/Shares freigeben."""
            failed_groups.add(group)
            freed_cash = 0.0
            for tok, side, f_filled, cash_used, res in group_state.pop(group, []):
                consumed[(tok, side)] -= f_filled
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
            skip = consumed.get((s.token_id, s.side), 0.0)
            filled, cost, fee = self._walk_levels(s, levels, max_size, skip,
                                                  cash_left, rate)
            if s.group and filled < s.size - 1e-9:
                # FOK: Bein nicht in voller Größe füllbar -> ganze Gruppe weg.
                cash_left += fail_group(s.group)
                continue
            if filled > 1e-9:
                consumed[(s.token_id, s.side)] = skip + filled
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
                        (s.token_id, s.side, filled, cash_used, res))
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
    DELAY_POLL_ATTEMPTS = 10
    DELAY_POLL_INTERVAL_S = 0.5
    # Nach so vielen erfolglosen get_order-Abfragen wird das Tracking beendet.
    MAX_RECONCILE_MISSES = 10

    def __init__(self, cfg: BotConfig):
        from py_clob_client_v2.client import ClobClient

        kwargs = {
            "key": cfg.private_key,
            "chain_id": POLYGON_CHAIN_ID,
        }
        if cfg.funder_address:
            kwargs["signature_type"] = cfg.signature_type
            kwargs["funder"] = cfg.funder_address
        self.client = ClobClient(CLOB_HOST, **kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.fallback_fee_rate = cfg.risk.taker_fee_rate
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

    # ---- Ausführung --------------------------------------------------------

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        from py_clob_client_v2.clob_types import OrderType

        fee_rates = fee_rates or {}
        # Zuerst reale (Teil-)Fills ruhender Orders nachbuchen — Portfolio und
        # Risiko-Limits dürfen weder Phantom- noch fehlende Positionen sehen.
        fills = self._reconcile_pending(portfolio, fee_rates)
        # FOK sichert nur die Einzelorder, nicht die Arb-Gruppe: scheitert ein
        # Bein, dürfen die restlichen Beine der Gruppe nicht mehr raus.
        failed_groups: set[str] = set()
        group_fills: dict[str, list[Fill]] = {}  # gebuchte Beine je Gruppe (für Unwind)
        refreshed: set[str] = set()  # Tokens, deren Alt-Quotes dieser Tick schon gecancelt sind
        for s in signals:
            if s.group and s.group in failed_groups:
                log.warning("Gruppe %s: Bein %s übersprungen, da ein voriges Bein scheiterte",
                            s.group, s.token_id[:12])
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

    def _submit_signal(self, s: Signal, otype, portfolio: Portfolio,
                       fee_rates: dict[str, float],
                       tick_size: str | None = None) -> tuple[str, Fill | None]:
        """Eine Order quantisieren, bauen, posten und das Ergebnis verbuchen.

        Rückgabe: ("matched", Fill) bei bestätigtem Match,
                  ("pending", None) für ruhende GTC-Orders,
                  ("failed", None) bei Ablehnung/Fehler.
        """
        from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        try:
            tick = float(tick_size) if tick_size else float(self.client.get_tick_size(s.token_id))
            price = _quantize_price(s.price, tick, s.side)
            order = self.client.create_order(
                OrderArgs(
                    token_id=s.token_id,
                    price=price,
                    size=round(s.size, 2),
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
                return "failed", None
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception as e:  # noqa: BLE001
            log.warning("Cancel der delayed Order %s fehlgeschlagen: %s", order_id, e)
        o = self._get_order_safe(order_id)
        if o and o.get("status") == "matched":
            return "matched", self._book_order_state(s, o, price, portfolio, fee_rates)
        log.error("Delayed-Order %s nicht bestätigt — gecancelt und als Fehlschlag gewertet",
                  order_id)
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
        """Fill buchen; Buchhaltungsfehler dürfen die reale Ausführung nicht kippen."""
        try:
            portfolio.apply_fill(fill)
        except ValueError as e:
            log.error("Fill nicht verbuchbar (Portfolio inkonsistent zur Börse?): %s — "
                      "Bestände manuell abgleichen!", e)

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

    def _cancel_open_orders(self, token_id: str) -> None:
        """Zuvor platzierte ruhende Orders eines Tokens canceln.

        Die finalen (Teil-)Fills gecancelter Orders bucht _reconcile_pending
        im nächsten Tick nach (der Order-Status bleibt abfragbar).
        """
        from py_clob_client_v2.clob_types import OrderPayload

        for oid in self._open_orders.pop(token_id, []):
            try:
                self.client.cancel_order(OrderPayload(orderID=oid))
            except Exception as e:  # noqa: BLE001
                log.warning("Cancel für Order %s fehlgeschlagen: %s", oid, e)

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
            if outcome == "matched":
                fills += 1
                log.warning("Gruppe %s: Bein %s glattgestellt (%s %.2f @%.4f)",
                            group, f.token_id[:12], counter.side, f.size, level.price)
            else:
                log.error("Gruppe %s: Unwind für %s nicht gefüllt — Position offen, "
                          "manuell glattstellen!", group, f.token_id[:12])
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
