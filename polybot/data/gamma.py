"""Gamma-API-Client: öffentliche Markt- und Event-Daten (kein API-Key nötig).

Docs: https://docs.polymarket.com — Gamma Markets API.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import requests

from polybot.config import GAMMA_HOST

log = logging.getLogger(__name__)


@dataclass
class Market:
    """Ein binärer Markt (ein Token-Paar YES/NO)."""

    condition_id: str
    question: str
    slug: str
    yes_token: str
    no_token: str
    liquidity: float
    volume_24h: float
    neg_risk: bool
    event_slug: str | None = None
    closed: bool = False
    # Event erlaubt nachträgliches Hinzufügen von Outcomes (negRiskAugmented):
    # dann zahlt "alle gelisteten YES kaufen" nicht garantiert 1 USDC aus.
    neg_risk_augmented: bool = False


def _parse_market(m: dict) -> Market | None:
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
        if len(token_ids) != 2:
            return None
        return Market(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            yes_token=token_ids[0],
            no_token=token_ids[1],
            liquidity=float(m.get("liquidityNum") or m.get("liquidity") or 0),
            volume_24h=float(m.get("volume24hr") or 0),
            neg_risk=bool(m.get("negRisk", False)),
            closed=bool(m.get("closed", False)),
            neg_risk_augmented=bool(m.get("negRiskAugmented", False)),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class GammaClient:
    def __init__(self, session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "polybot/0.1"

    def _get(self, path: str, **params) -> list | dict:
        """GET mit einfachem Retry/Backoff bei transienten Fehlern (429/5xx/Timeout).

        Die Gamma-API ist rate-limitiert — ein einzelnes 429 mitten in einer
        Pagination-Schleife soll nicht den ganzen Scan-Durchlauf abbrechen.
        Nicht-transiente Fehler (z.B. 404) werden sofort weitergereicht.
        """
        attempts = 3
        for attempt in range(attempts):
            try:
                r = self.http.get(f"{GAMMA_HOST}{path}", params=params, timeout=30)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                transient = status is None or status == 429 or status >= 500
                if not transient or attempt == attempts - 1:
                    raise
                wait = 2.0**attempt
                log.warning("Gamma GET %s fehlgeschlagen (%s) — Retry in %.0fs", path, e, wait)
                time.sleep(wait)
        raise AssertionError("unerreichbar")  # Schleife returned oder raist immer

    def active_markets(self, min_liquidity: float = 0.0, limit: int = 500) -> list[Market]:
        """Aktive, offene Märkte, sortiert nach Liquidität."""
        # Offset-Pagination läuft über einen live nach liquidityNum sortierten
        # Feed: verschiebt sich die Sortierung zwischen zwei Seitenabrufen,
        # kann derselbe Markt auf zwei Seiten auftauchen -> nach condition_id
        # deduplizieren (erste Fundstelle gewinnt).
        out: dict[str, Market] = {}
        offset = 0
        page = 100
        while offset < limit:
            rows = self._get(
                "/markets",
                active="true",
                closed="false",
                order="liquidityNum",
                ascending="false",
                limit=min(page, limit - offset),
                offset=offset,
            )
            if not rows:
                break
            for row in rows:
                m = _parse_market(row)
                if m and not m.closed and m.liquidity >= min_liquidity:
                    out.setdefault(m.condition_id, m)
            if len(rows) < page:
                break
            offset += page
        return list(out.values())

    def negrisk_events(self, min_liquidity: float = 0.0, limit: int = 200) -> dict[str, list[Market]]:
        """Multi-Outcome-Events (negRisk): Event-Slug -> Liste der Teilmärkte.

        In einem negRisk-Event schließen sich die Outcomes gegenseitig aus,
        d.h. die Summe aller YES-Preise sollte 1.00 betragen.
        """
        result: dict[str, list[Market]] = {}
        offset = 0
        # Server-Cap: /events liefert pro Request höchstens 100 Zeilen — ein
        # größeres limit wird stillschweigend abgeschnitten, daher paginieren.
        page = 100
        while offset < limit:
            events = self._get(
                "/events",
                active="true",
                closed="false",
                order="liquidity",
                ascending="false",
                limit=min(page, limit - offset),
                offset=offset,
            )
            if not events:
                break
            for ev in events:
                if not ev.get("negRisk"):
                    continue
                # Nicht deployte Platzhalter-Outcomes (active=false) sind nicht
                # handelbar, tragen aber veraltete outcomePrices — sie würden
                # die Summe-1-Annahme zerstören und Phantom-Mispricings
                # erzeugen -> vorab herausfiltern.
                rows = [x for x in ev.get("markets", []) if x.get("active") is True]
                parsed = [_parse_market(x) for x in rows]
                markets = [m for m in parsed if m]
                # Vollständigkeit ist Pflicht: ist irgendein Teilmarkt bereits
                # geschlossen (z.B. der bekannte Gewinner) oder nicht parsebar,
                # wäre "alle YES kaufen" nicht mehr risikofrei -> das ganze Event
                # überspringen statt Teilmärkte stillschweigend herauszufiltern.
                if len(markets) != len(parsed) or any(m.closed for m in markets):
                    continue
                if len(markets) >= 2 and sum(m.liquidity for m in markets) >= min_liquidity:
                    slug = ev.get("slug", ev.get("id", "?"))
                    augmented = bool(ev.get("negRiskAugmented", False))
                    for m in markets:
                        m.event_slug = slug
                        m.neg_risk_augmented = m.neg_risk_augmented or augmented
                    result[slug] = markets
            if len(events) < page:
                break
            offset += page
        return result
