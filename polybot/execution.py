"""Ausführung: Paper-Broker (Simulation) und Live-Broker (py-clob-client).

Der Paper-Broker simuliert Fills gegen das echte Orderbuch — konservativ:
gefillt wird nur, was zum Signalpreis tatsächlich im Buch liegt.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from polybot.config import CLOB_HOST, POLYGON_CHAIN_ID, BotConfig
from polybot.data.orderbook import OrderBook
from polybot.portfolio import Fill, Portfolio
from polybot.strategies.base import Signal

log = logging.getLogger(__name__)


class Broker(ABC):
    @abstractmethod
    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        """Signale ausführen; Anzahl der Fills zurückgeben."""


class PaperBroker(Broker):
    """Simuliert Marketable-Limit-Orders gegen den aktuellen Book-Snapshot.

    Marketable Orders sind Taker-Fills — die kategorieabhängige Taker-Gebühr
    (fee = shares * rate * p * (1-p)) wird mitverbucht, sonst wäre der
    Paper-PnL systematisch zu hoch.
    """

    def __init__(self, cfg: BotConfig | None = None):
        # Fallback-Rate für Tokens ohne abrufbare Fee-Rate (konservativ das
        # konfigurierte Maximum); ohne Config (Tests) gebührenfrei.
        self.fallback_fee_rate = cfg.risk.taker_fee_rate if cfg else 0.0

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        fee_rates = fee_rates or {}
        fills = 0
        for s in signals:
            book = books.get(s.token_id)
            if not book:
                continue
            rate = fee_rates.get(s.token_id, self.fallback_fee_rate)
            fee_per_share = rate * s.price * (1.0 - s.price)
            if s.side == "BUY":
                available = sum(lv.size for lv in book.asks if lv.price <= s.price)
                # Kein Kauf über das Cash hinaus — Polymarket kennt keine Margin.
                cost_per_share = s.price + fee_per_share
                affordable = portfolio.cash / cost_per_share if cost_per_share > 0 else 0.0
                fill_size = min(s.size, available, affordable)
            else:
                available = sum(lv.size for lv in book.bids if lv.price >= s.price)
                # Kein Verkauf über den Bestand hinaus (kein Shorting).
                pos = portfolio.positions.get(s.token_id)
                held = pos.shares if pos else 0.0
                fill_size = min(s.size, available, held)
            if fill_size <= 0:
                log.debug("Paper: kein Fill für %s %s @%.3f", s.side, s.token_id[:12], s.price)
                continue
            portfolio.apply_fill(Fill(
                ts=time.time(), token_id=s.token_id, side=s.side,
                price=s.price, size=fill_size, reason=s.reason,
                fee=fill_size * fee_per_share,
            ))
            fills += 1
            log.info("Paper-Fill: %s %.0f Shares @%.3f — %s (%s)",
                     s.side, fill_size, s.price, s.market_question[:50], s.reason)
        return fills


class LiveBroker(Broker):
    """Echte Orders über den offiziellen py-clob-client-v2.

    Seit dem Exchange-Upgrade vom 28.04.2026 (CTF Exchange V2, pUSD statt
    USDC.e) ist der v1-Client inkompatibel — dieser Broker nutzt v2.
    Erfordert POLY_PRIVATE_KEY (und für Proxy-Wallets POLY_FUNDER_ADDRESS)
    in der Umgebung. Verwendet FOK-Orders für Arb-Beine, damit kein Bein
    hängen bleibt.
    """

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
        # Ruhende eigene Orders (GTC-Quotes) je Token — werden vor dem
        # Neu-Quoten gecancelt, damit sich keine veralteten Quotes stapeln.
        self._open_orders: dict[str, list[str]] = {}
        log.info("Live-Broker verbunden (Adresse %s)", self.client.get_address())

    def _cancel_open_orders(self, token_id: str) -> None:
        """Zuvor platzierte ruhende Orders eines Tokens canceln."""
        from py_clob_client_v2.clob_types import OrderPayload

        for oid in self._open_orders.pop(token_id, []):
            try:
                self.client.cancel_order(OrderPayload(orderID=oid))
            except Exception as e:  # noqa: BLE001
                log.warning("Cancel für Order %s fehlgeschlagen: %s", oid, e)

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio,
                fee_rates: dict[str, float] | None = None) -> int:
        from py_clob_client_v2.clob_types import CreateOrderOptions, OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        fills = 0
        # FOK sichert nur die Einzelorder, nicht die Arb-Gruppe: scheitert ein
        # Bein, dürfen die restlichen Beine der Gruppe nicht mehr raus.
        failed_groups: set[str] = set()
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
                tick = self.client.get_tick_size(s.token_id)
                order = self.client.create_order(
                    OrderArgs(
                        token_id=s.token_id,
                        price=round(s.price, 3),
                        size=round(s.size, 2),
                        side=BUY if s.side == "BUY" else SELL,
                    ),
                    # NegRisk-Märkte laufen über den NegRisk-Exchange
                    # (andere EIP-712-Domain) — muss deklariert werden.
                    options=CreateOrderOptions(tick_size=str(tick), neg_risk=s.neg_risk),
                )
                # Arb-Beine als FOK (ganz oder gar nicht), Rest als GTC
                otype = OrderType.FOK if s.group else OrderType.GTC
                resp = self.client.post_order(order, otype)
                if resp.get("success"):
                    fills += 1
                    order_id = resp.get("orderID") or resp.get("order_id")
                    if s.replace and order_id:
                        self._open_orders.setdefault(s.token_id, []).append(order_id)
                    portfolio.apply_fill(Fill(
                        ts=time.time(), token_id=s.token_id, side=s.side,
                        price=s.price, size=s.size, reason=s.reason,
                    ))
                    log.info("Live-Order platziert: %s %.0f @%.3f — %s",
                             s.side, s.size, s.price, s.market_question[:50])
                else:
                    self._abort_group(s, failed_groups)
                    log.warning("Order abgelehnt: %s", resp)
            except Exception as e:  # noqa: BLE001 — Bot darf durch eine Order nicht sterben
                self._abort_group(s, failed_groups)
                log.error("Live-Order fehlgeschlagen (%s): %s", s.market_question[:40], e)
        return fills

    @staticmethod
    def _abort_group(s: Signal, failed_groups: set[str]) -> None:
        """Gruppe nach gescheitertem Bein sperren; Rest-Beine werden nicht gesendet."""
        if s.group and s.group not in failed_groups:
            failed_groups.add(s.group)
            log.warning("Arb-Gruppe %s abgebrochen: Bein %s scheiterte — bereits "
                        "gefüllte Beine sind ungehedged und sollten geprüft werden",
                        s.group, s.token_id[:12])


def make_broker(cfg: BotConfig) -> Broker:
    if cfg.mode == "live":
        return LiveBroker(cfg)
    return PaperBroker(cfg)
