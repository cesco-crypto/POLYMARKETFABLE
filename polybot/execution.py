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
    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio) -> int:
        """Signale ausführen; Anzahl der Fills zurückgeben."""


class PaperBroker(Broker):
    """Simuliert Marketable-Limit-Orders gegen den aktuellen Book-Snapshot."""

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio) -> int:
        fills = 0
        for s in signals:
            book = books.get(s.token_id)
            if not book:
                continue
            if s.side == "BUY":
                available = sum(lv.size for lv in book.asks if lv.price <= s.price)
            else:
                available = sum(lv.size for lv in book.bids if lv.price >= s.price)
            fill_size = min(s.size, available)
            if fill_size <= 0:
                log.debug("Paper: kein Fill für %s %s @%.3f", s.side, s.token_id[:12], s.price)
                continue
            portfolio.apply_fill(Fill(
                ts=time.time(), token_id=s.token_id, side=s.side,
                price=s.price, size=fill_size, reason=s.reason,
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
        log.info("Live-Broker verbunden (Adresse %s)", self.client.get_address())

    def execute(self, signals: list[Signal], books: dict[str, OrderBook], portfolio: Portfolio) -> int:
        from py_clob_client_v2.clob_types import CreateOrderOptions, OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        fills = 0
        for s in signals:
            try:
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
                    portfolio.apply_fill(Fill(
                        ts=time.time(), token_id=s.token_id, side=s.side,
                        price=s.price, size=s.size, reason=s.reason,
                    ))
                    log.info("Live-Order platziert: %s %.0f @%.3f — %s",
                             s.side, s.size, s.price, s.market_question[:50])
                else:
                    log.warning("Order abgelehnt: %s", resp)
            except Exception as e:  # noqa: BLE001 — Bot darf durch eine Order nicht sterben
                log.error("Live-Order fehlgeschlagen (%s): %s", s.market_question[:40], e)
        return fills


def make_broker(cfg: BotConfig) -> Broker:
    if cfg.mode == "live":
        return LiveBroker(cfg)
    return PaperBroker()
