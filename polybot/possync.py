"""Positions-Abgleich gegen die Chain-Realität (Data-API) beim Live-Start.

Restlücke aus dem Selbst-Review 05.07.2026: Die Live-Buchhaltung
(live_state.json) kennt nur, was der eigene Prozess gebucht hat. Zwei
reale Driftquellen:

1. Orders, die ZWISCHEN Crash/Stop und Neustart matchen (Delayed-Orders,
   GTC-Reste): die Chain hat die Position, die Buchhaltung nicht.
2. Manuelle Eingriffe des Betreibers (Verkauf/Claim im Browser — am
   05.07. dreimal passiert): die Buchhaltung führt Geisterpositionen.

Beides verzerrt Exposure-Limits, Waisen-Erkennung und Settlement. Der
Syncer holt beim Start die echten Positionen des Handels-Wallets von der
öffentlichen Data-API und setzt die Portfolio-Positionen auf die
Chain-Wahrheit (force_set_position: KEINE PnL-/Cash-Buchung — externe
Aktionen sind kein Bot-PnL, jede Korrektur wird laut geloggt).

Cash wird bewusst NICHT synchronisiert (bräuchte on-chain
Balance-Abfrage); der Börsen-Balance-Check fängt Überzeichnungen ab.
Wie alle Hilfspfade: wirft nie in den Aufrufer.
"""

from __future__ import annotations

import logging

import requests

from polybot.portfolio import Portfolio

log = logging.getLogger(__name__)

DATA_API_HOST = "https://data-api.polymarket.com"
# Unter dieser Share-Differenz wird nicht korrigiert (Rundungs-/Staubrauschen).
MIN_DELTA_SHARES = 0.01


class PositionSyncer:
    """Gleicht Portfolio-Positionen mit der Data-API ab (Live-Start)."""

    PAGE = 500
    MAX_PAGES = 10  # Backstop — mehr als 5000 Positionen hat das Wallet nie

    def __init__(self, address: str, session: requests.Session | None = None):
        self.address = address
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "polybot/0.1"

    def fetch_chain_positions(self) -> dict[str, dict] | None:
        """Token-ID -> {size, avgPrice, title} laut Chain; None bei Fehler.

        None ist von {} zu unterscheiden: Bei einem API-Fehler darf der
        Sync NICHT laufen (er würde sonst alle Positionen austragen).
        """
        out: dict[str, dict] = {}
        try:
            for page in range(self.MAX_PAGES):
                r = self.http.get(f"{DATA_API_HOST}/positions",
                                  params={"user": self.address,
                                          "limit": self.PAGE,
                                          "offset": page * self.PAGE},
                                  timeout=20)
                r.raise_for_status()
                rows = r.json()
                if not isinstance(rows, list):
                    log.warning("Positions-Sync: unerwartete Antwort der "
                                "Data-API — Sync übersprungen")
                    return None
                for p in rows:
                    token = str(p.get("asset") or "")
                    size = float(p.get("size") or 0.0)
                    if token and size > 0:
                        out[token] = {"size": size,
                                      "avg_price": float(p.get("avgPrice") or 0.0),
                                      "title": str(p.get("title") or "")}
                if len(rows) < self.PAGE:
                    break
        except (requests.RequestException, ValueError, TypeError) as e:
            log.warning("Positions-Sync: Data-API nicht erreichbar/lesbar: %s "
                        "— Sync übersprungen", e)
            return None
        return out

    def sync(self, portfolio: Portfolio) -> int:
        """Portfolio auf Chain-Stand bringen; Rückgabe: Anzahl Korrekturen.

        Nur beim Prozessstart aufrufen: Während des Betriebs hinkt die
        Data-API-Indizierung frischen Fills um Sekunden hinterher und
        würde korrekte Buchungen fälschlich »korrigieren«.
        """
        chain = self.fetch_chain_positions()
        if chain is None:
            return 0
        corrections = 0
        for token, info in chain.items():
            pos = portfolio.positions.get(token)
            booked = pos.shares if pos else 0.0
            if abs(info["size"] - booked) > MIN_DELTA_SHARES:
                portfolio.force_set_position(token, info["size"],
                                             info["avg_price"], info["title"])
                corrections += 1
        for token in list(portfolio.positions):
            if token not in chain:
                portfolio.force_set_position(token, 0.0, 0.0, "")
                corrections += 1
        if corrections:
            log.warning("Positions-Sync: %d Korrektur(en) gegen die Chain — "
                        "Ursachen: Fills während Downtime oder manuelle "
                        "Eingriffe (Details oben)", corrections)
        else:
            log.info("Positions-Sync: Buchhaltung deckt sich mit der Chain "
                     "(%d Positionen)", len(chain))
        return corrections
