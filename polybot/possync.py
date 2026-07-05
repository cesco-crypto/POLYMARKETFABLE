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
# Tokens mit Fill jünger als das hier werden NIE reduziert/entfernt: die
# Data-API-Indizierung hinkt frischen Fills hinterher — sonst löscht der
# Sync korrekt gebuchte Positionen (Verifikations-Befunde 16/26).
RECENT_FILL_GRACE_S = 900.0


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
                                          "offset": page * self.PAGE,
                                          # Default-Filter der API ist 1.0 —
                                          # Sub-1-Share-Positionen würden
                                          # sonst bei jedem Start ausgebucht
                                          # (Befund 17).
                                          "sizeThreshold": 0.01},
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

    # Periodischer Re-Sync (löst auch das Start-Race: ein Delayed-Match
    # SEKUNDEN nach dem Start wird vom nächsten Lauf eingefangen).
    SYNC_INTERVAL_S = 900.0

    def __init_throttle(self):
        if not hasattr(self, "_last_sync"):
            self._last_sync = 0.0

    def periodic(self, portfolio: Portfolio, now: float | None = None) -> int:
        """Gedrosselter Sync für den Tick-Pfad (crasht nie)."""
        import time as _time
        now = _time.time() if now is None else now
        self.__init_throttle()
        if now - self._last_sync < self.SYNC_INTERVAL_S:
            return 0
        self._last_sync = now
        try:
            return self.sync(portfolio, now=now)
        except Exception as e:  # noqa: BLE001 — Sync nie tick-kritisch
            log.warning("Periodischer Positions-Sync fehlgeschlagen: %s", e)
            return 0

    def sync(self, portfolio: Portfolio, now: float | None = None) -> int:
        """Portfolio auf Chain-Stand bringen; Rückgabe: Anzahl Korrekturen.

        Schutzregeln (Verifikations-Flotte 05.07.2026):
        - Bereits GESETTELTE Tokens (portfolio.settled) werden nie wieder
          eingetragen — sonst zahlte der Sweeper nach jedem Neustart
          erneut aus (Befunde 2/20); der Registry-Eintrag verfällt, sobald
          die Chain den Token nicht mehr führt (Redeem durch).
        - Tokens mit Fill jünger als RECENT_FILL_GRACE_S werden nie
          reduziert/entfernt (Data-API-Lag, Befunde 16/26).
        - Liefert die Chain GAR KEINE Position, während die Buchhaltung
          welche führt, wird NICHTS entfernt (Befund 19: falsche
          Wallet-Adresse — z.B. fehlendes POLY_FUNDER_ADDRESS — würde
          sonst das ganze Portfolio löschen).
        - day_start_value wird um den Korrektur-Effekt verschoben, damit
          externe Eingriffe den Kill-Switch weder auslösen noch seinen
          Spielraum aufblasen (Befunde 18/35).
        """
        import time as _time
        now = _time.time() if now is None else now
        chain = self.fetch_chain_positions()
        if chain is None:
            return 0
        # Redeem durch -> Chain führt den Token nicht mehr -> Registry frei.
        for token in [t for t in portfolio.settled if t not in chain]:
            portfolio.settled.pop(token, None)
        recent = {f.token_id for f in portfolio.fills
                  if now - f.ts < RECENT_FILL_GRACE_S}
        value_before = portfolio.value()
        corrections = 0
        for token, info in chain.items():
            if token in portfolio.settled:
                continue  # ausgezahlt gebucht, Redeem on-chain noch offen
            pos = portfolio.positions.get(token)
            booked = pos.shares if pos else 0.0
            if abs(info["size"] - booked) <= MIN_DELTA_SHARES:
                continue
            if info["size"] < booked and token in recent:
                log.info("Positions-Sync: %s frisch gefüllt — Reduktion "
                         "wegen API-Lag aufgeschoben", token[:16])
                continue
            portfolio.force_set_position(token, info["size"],
                                         info["avg_price"], info["title"])
            corrections += 1
        if not chain and portfolio.positions:
            log.error("Positions-Sync: Chain meldet NULL Positionen, "
                      "Buchhaltung führt %d — falsche Wallet-Adresse? "
                      "(POLY_FUNDER_ADDRESS prüfen). Es wird NICHTS entfernt.",
                      len(portfolio.positions))
        else:
            for token in list(portfolio.positions):
                if token in chain or token in recent:
                    continue
                portfolio.force_set_position(token, 0.0, 0.0, "")
                corrections += 1
        if corrections:
            # Kill-Switch-Basis neutral halten: externe Korrekturen sind
            # kein Tages-PnL des Bots.
            portfolio.day_start_value += portfolio.value() - value_before
            log.warning("Positions-Sync: %d Korrektur(en) gegen die Chain — "
                        "Ursachen: Fills während Downtime oder manuelle "
                        "Eingriffe (Details oben)", corrections)
        else:
            log.info("Positions-Sync: Buchhaltung deckt sich mit der Chain "
                     "(%d Positionen)", len(chain))
        return corrections
