"""Waisen-Detektor + Auto-Glattstellung (Live-Befund 05.07.2026).

Ungehedgte Einzelbeine («Waisen») entstehen live, wenn von einem Arb-Paar
nur ein Bein füllt: Matching-Delay-Races auf In-play-Märkten, gescheiterte
FOK-Gegenbeine, fehlgeschlagene Unwinds. Ein Waisenbein ist eine offene
Richtungswette — nicht unser Geschäft. Regel deshalb: Der Bot hält KEINE
ungehedgte Position. Waisen werden nach einer Schonfrist zum besten Bid
verkauft — immer, nicht nur im Gewinn (dreimal Lotterie-Glück am 05.07.
ist Rückenwind, kein Systembeweis; dieselbe Mechanik kann genauso ins
Minus laufen).

Bewusst NICHT glattgestellt werden:
- vollständige YES/NO-Paare (kein Überhang -> realisieren sich per
  Merge bzw. bei der Auflösung des Markts),
- NegRisk-Tokens (NO-Sätze sind strukturell «einbeinig» pro Teilmarkt;
  die Paar-Logik greift dort erst mit Phase 2),
- Tokens mit eigenen ruhenden Orders (Market-Making-Inventar wird von
  der MM-Strategie selbst abgebaut),
- Staub unterhalb MIN_NOTIONAL_USDC (unter dem Börsen-Mindestvolumen
  nicht verkäuflich; bleibt liegen wie eine 0.01-Share-Leiche).

Die Paar-Zuordnung (YES <-> NO je Markt) wird über ALLE je gesehenen
Snapshots gelernt und behalten: genau die Märkte, auf denen Waisen
entstehen (in-play, abgelaufen), fliegen aus dem nächsten Snapshot raus —
mit einer Nur-Snapshot-Sicht wäre der Detektor auf dem wichtigsten Fall
blind. Fehlt das Buch im Snapshot, holt er den Bid per Batch-Preisabfrage
selbst (gedrosselt, nur für tatsächliche Waisen).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from polybot.config import BotConfig
from polybot.data.orderbook import Level, OrderBook
from polybot.strategies.base import MarketSnapshot, Signal

log = logging.getLogger(__name__)


@dataclass
class _PairInfo:
    partner: str          # Token der Gegenseite (YES <-> NO)
    question: str
    neg_risk: bool


class OrphanFlattener:
    """Erkennt einbeinige Positionen und erzeugt SELL-Signale zum Bid.

    Aktivierung über cfg.risk.flatten_orphan_grace_s > 0 (Sekunden
    Schonfrist, bevor ein Überhang als Waise gilt — sie muss das
    Delayed-Order-Poll-Fenster und einen Reconcile-Tick überdauern,
    sonst würde ein noch schwebendes Gegenbein fälschlich glattgestellt).
    """

    # Frühestens alle so viele Sekunden erneut für denselben Token senden —
    # die replace-Semantik cancelt zwar Alt-Orders, aber ein 0.5s-Stream-Tick
    # darf daraus kein Cancel/Replace-Gewitter machen.
    REPEAT_S = 30.0
    # Unter diesem Orderwert lehnt die Börse ab (Mindestvolumen ~1 USDC) —
    # solcher Staub bleibt liegen statt Reject-Spam zu erzeugen.
    MIN_NOTIONAL_USDC = 1.0
    # Selbst geholte Top-of-Book-Bids so lange wiederverwenden.
    BID_TTL_S = 20.0

    def __init__(self, cfg: BotConfig, books=None):
        self.grace_s = cfg.risk.flatten_orphan_grace_s
        self.books = books  # BookClient für Bids off-Snapshot (optional)
        self.pairs: dict[str, _PairInfo] = {}
        self.first_seen: dict[str, float] = {}   # Token -> Beginn des Überhangs
        self.last_emit: dict[str, float] = {}    # Token -> letztes SELL-Signal
        self._bid_cache: dict[str, tuple[float, float | None]] = {}
        self._warned: set[str] = set()           # einmalige Hinweise je Token

    # ---- Paar-Karte pflegen -------------------------------------------------

    def observe(self, snap: MarketSnapshot) -> None:
        """Paar-Zuordnungen aus dem Snapshot lernen (kumulativ, nie vergessen)."""
        for m in snap.markets:
            self._learn(m)
        for ev_markets in snap.negrisk_events.values():
            for m in ev_markets:
                self._learn(m)

    def _learn(self, m) -> None:
        if not m.yes_token or not m.no_token:
            return
        self.pairs[m.yes_token] = _PairInfo(m.no_token, m.question, m.neg_risk)
        self.pairs[m.no_token] = _PairInfo(m.yes_token, m.question, m.neg_risk)

    # ---- Waisen finden -------------------------------------------------------

    def signals(self, snap: MarketSnapshot, portfolio,
                now: float | None = None
                ) -> tuple[list[Signal], dict[str, OrderBook]]:
        """SELL-Signale für reife Waisen + synthetische Bücher off-Snapshot.

        Rückgabe: (signals, extra_books). extra_books enthält für Tokens ohne
        Snapshot-Buch ein Top-of-Book mit Größe 0 (ehrlich: die echte Tiefe
        ist unbekannt) — der LiveBroker braucht nur den Preis, der
        PaperBroker lässt die Order konservativ ruhen.
        """
        now = time.time() if now is None else now
        self.observe(snap)
        candidates: list[tuple[str, _PairInfo, float]] = []
        for token, pos in list(portfolio.positions.items()):
            info = self.pairs.get(token)
            if info is None:
                # Nie in einem Snapshot gesehen (z.B. Altbestand aus einem
                # früheren Lauf): ohne Paar-Wissen nicht beurteilbar.
                self._warn_once(token, "Waisen-Check: Token %s ohne bekanntes "
                                       "Paar — bleibt unangetastet", token[:16])
                continue
            if info.neg_risk:
                continue
            partner = portfolio.positions.get(info.partner)
            excess = pos.shares - (partner.shares if partner else 0.0)
            if excess <= 1e-9:
                self.first_seen.pop(token, None)
                continue
            if any(o.token_id == token for o in portfolio.resting_orders):
                # Eigene ruhende Quote auf dem Token: das ist gemanagtes
                # (MM-)Inventar, kein verwaistes Arb-Bein.
                self.first_seen.pop(token, None)
                continue
            first = self.first_seen.setdefault(token, now)
            if now - first < self.grace_s:
                continue
            if now - self.last_emit.get(token, 0.0) < self.REPEAT_S:
                continue
            # Börsenkonform abrunden (Share-Hundertstel) statt aufblasen.
            size = math.floor(excess * 100 + 1e-9) / 100
            if size > 0:
                candidates.append((token, info, size))

        if not candidates:
            return [], {}
        self._fetch_missing_bids([t for t, _, _ in candidates
                                  if self._snap_bid(snap, t) is None], now)
        out: list[Signal] = []
        extra_books: dict[str, OrderBook] = {}
        for token, info, size in candidates:
            bid = self._snap_bid(snap, token)
            if bid is None:
                bid = self._cached_bid(token, now)
            if bid is None or bid < 0.001:
                # Kein Bid (Buch leer/Markt tot): zum Verkaufen braucht es
                # einen Käufer — beobachten, nicht raten.
                self._warn_once(token, "Waise %s (%s): kein Bid auffindbar — "
                                       "Glattstellung wartet auf Käufer",
                                token[:16], info.question[:40])
                continue
            price = min(bid, 0.999)
            if size * price < self.MIN_NOTIONAL_USDC:
                self._warn_once(token, "Waise %s (%s): %.2f Shares @%.3f unter "
                                       "Mindestvolumen — bleibt als Staub liegen",
                                token[:16], info.question[:40], size, price)
                continue
            log.warning("Waise erkannt: %.2f Shares %s (%s) ohne Gegenbein — "
                        "verkaufe @%.3f", size, token[:16], info.question[:60],
                        price)
            out.append(Signal(
                token_id=token, side="SELL", price=price, size=size,
                reason="Waise glattstellen (ungehedgtes Arb-Bein)",
                market_question=info.question, neg_risk=info.neg_risk,
                replace=True,
            ))
            if token not in snap.books:
                extra_books[token] = OrderBook(token_id=token,
                                               bids=[Level(price, 0.0)])
            self.last_emit[token] = now
        return out, extra_books

    # ---- Interna -------------------------------------------------------------

    def _snap_bid(self, snap: MarketSnapshot, token: str) -> float | None:
        book = snap.books.get(token)
        return book.best_bid.price if book and book.best_bid else None

    def _cached_bid(self, token: str, now: float) -> float | None:
        ts, bid = self._bid_cache.get(token, (0.0, None))
        return bid if now - ts <= self.BID_TTL_S else None

    def _fetch_missing_bids(self, tokens: list[str], now: float) -> None:
        """Bids off-Snapshot per Batch holen (gedrosselt über BID_TTL_S)."""
        stale = [t for t in tokens if now - self._bid_cache.get(t, (0.0, None))[0]
                 > self.BID_TTL_S]
        if not stale or self.books is None \
                or not hasattr(self.books, "get_top_prices"):
            return
        try:
            top = self.books.get_top_prices(stale)
        except Exception as e:  # noqa: BLE001 — Preisabfrage nie tick-kritisch
            log.warning("Waisen-Check: Batch-Preise nicht abrufbar: %s", e)
            return
        for t in stale:
            bid, _ask = top.get(t, (None, None))
            self._bid_cache[t] = (now, bid)

    def _warn_once(self, token: str, msg: str, *args) -> None:
        if token not in self._warned:
            log.info(msg, *args)
            self._warned.add(token)
