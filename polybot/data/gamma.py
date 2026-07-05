"""Gamma-API-Client: öffentliche Markt- und Event-Daten (kein API-Key nötig).

Docs: https://docs.polymarket.com — Gamma Markets API.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

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
    # Taker-Fee-Rate der Marktkategorie (0.00-0.07, gilt für YES wie NO);
    # None = Gamma lieferte keine Fee-Info -> Aufrufer nutzen den
    # konfigurierten Fallback (cfg.risk.taker_fee_rate).
    fee_rate: float | None = None
    # NegRisk-questionId (0x…, 32 Bytes): letztes Byte = Frage-Index im
    # Event, Rest = marketId — gebraucht für den on-chain NO-Satz-Convert
    # über den NegRisk Adapter (polybot/onchain.py). Leer = unbekannt.
    question_id: str = ""
    # Marktende (endDate) als Unix-Timestamp; None = unbekannt.
    # Trade-Print-Validierung vom 04.07.2026: Märkte behalten nach endDate
    # (Spielende, abgelaufene 15-Min-Krypto-Fenster) noch closed=False und
    # ein STALES Orderbuch — dort ist real nichts mehr handelbar. Ohne
    # Endzeit-Filter entstehen Phantom-Arbitragen (12%+ des Fill-Volumens
    # im ersten Messlauf).
    end_ts: float | None = None
    # In-play-Matching-Delay (Sport/Esports): Sekunden, die eine Order nach
    # Spielbeginn serverseitig verzögert gematcht wird; Spielbeginn als
    # Unix-Timestamp (None = kein Spiel-Markt).
    seconds_delay: float = 0.0
    game_start_ts: float | None = None

    def tradeable(self, min_time_to_end_s: float, now: float | None = None) -> bool:
        """Bleibt bis zum Marktende genug Zeit, um real zu handeln?"""
        if self.end_ts is None:
            return True  # kein endDate geliefert -> nicht aussortieren
        return self.end_ts - (now if now is not None else time.time()) > min_time_to_end_s

    def inplay_delayed(self, now: float | None = None) -> bool:
        """Läuft das Spiel bereits UND matcht der Markt nur mit Verzögerung?

        Live-Befund 05.07.2026: In diesem Zustand hängen Taker-Orders
        sekundenlang im Matching-Delay — für Arbitrage ist der Edge dann
        eine Fata Morgana (Cancel-Races, ungehedgte Beine).
        """
        if self.seconds_delay <= 0 or self.game_start_ts is None:
            return False
        return (now if now is not None else time.time()) >= self.game_start_ts


def _parse_fee_rate(m: dict) -> float | None:
    """Taker-Fee-Rate aus dem Gamma-Marktobjekt lesen.

    Live-Befund (Juli 2026): Gamma-Märkte tragen `feesEnabled` und
    `feeSchedule.rate` — die Rate deckt sich exakt mit der offiziellen
    Kategorien-Tabelle (z.B. Sport 0.03, Politik 0.04, Economics 0.05);
    gebührenfreie Märkte (Geopolitik) haben feesEnabled=false. Der CLOB-
    Endpunkt GET /fee-rate liefert dagegen nur ein nicht interpretierbares
    `{"base_fee": 1000}` und taugt nicht als Quelle.
    """
    if m.get("feesEnabled") is False:
        return 0.0
    schedule = m.get("feeSchedule")
    raw = schedule.get("rate") if isinstance(schedule, dict) else None
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return None
    # Unplausible Werte verwerfen -> konservativer Fallback statt Mini-Fee
    return rate if 0.0 <= rate <= 1.0 else None


def _parse_ts(raw) -> float | None:
    """Zeitstempel-String als Unix-Timestamp.

    Gamma liefert zwei Formate: ISO-8601 ('2026-07-04T21:00:00Z') und bei
    gameStartTime '2026-07-05 20:00:00+00' (Leerzeichen, Kurz-Offset).
    """
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    # Kurz-Offset '+00'/'-05' auf '+00:00' normalisieren (fromisoformat
    # älterer Python-Versionen scheitert daran).
    if len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit():
        text += ":00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _parse_end_ts(m: dict) -> float | None:
    """endDate (ISO-8601, z.B. '2026-07-04T21:00:00Z') als Unix-Timestamp."""
    return _parse_ts(m.get("endDate") or m.get("endDateIso"))


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
            question_id=str(m.get("questionID") or ""),
            closed=bool(m.get("closed", False)),
            neg_risk_augmented=bool(m.get("negRiskAugmented", False)),
            fee_rate=_parse_fee_rate(m),
            end_ts=_parse_end_ts(m),
            seconds_delay=float(m.get("secondsDelay") or 0),
            game_start_ts=_parse_ts(m.get("gameStartTime")),
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

    def markets_by_tokens(self, token_ids: list[str]) -> list[Market]:
        """Märkte zu CLOB-Token-IDs auflösen — auch bereits geschlossene.

        Anwendungsfall Waisen-Detektor: Altbestände im Portfolio, deren
        Märkte längst aus dem Scan gefallen sind (beendet, in-play
        gefiltert). Live-Befund: /markets?clob_token_ids=… liefert ohne
        closed-Parameter NUR offene Märkte — geschlossene brauchen einen
        zweiten Abruf mit closed=true. Fehler werden geloggt und liefern
        das bis dahin Gefundene (der Aufrufer versucht es später erneut).
        """
        ids = ",".join(str(t) for t in token_ids if t)
        if not ids:
            return []
        out: dict[str, Market] = {}
        for extra in ({}, {"closed": "true"}):
            try:
                rows = self._get("/markets", clob_token_ids=ids, **extra)
            except requests.RequestException as e:
                log.warning("Gamma-Token-Lookup fehlgeschlagen (%s): %s",
                            extra or "offen", e)
                continue
            for row in rows or []:
                m = _parse_market(row)
                if m:
                    out.setdefault(m.condition_id, m)
        return list(out.values())

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

    # Live-Befund (Juli 2026): /markets liefert höchstens 100 Zeilen pro
    # Request (limit=500 wird stillschweigend auf 100 gekappt) und lehnt
    # Offsets über ~2000 mit HTTP 422 ab ("use /markets/keyset"). Der
    # /markets/keyset-Endpunkt ignoriert seinen eigenen next_cursor aber in
    # jeder getesteten Parameter-Schreibweise (Seiten wiederholen sich) und
    # ist damit unbrauchbar. Deshalb Fenster-Pagination über die Liquidität.
    _OFFSET_CAP = 2000

    def all_active_markets(self, min_liquidity: float = 0.0,
                           min_volume: float = 0.0) -> list[Market]:
        """ALLE aktiven Märkte mit Mindestliquidität — Fenster-Pagination.

        Statt Offset-Pagination (Server-Deckel bei ~2000, s.o.) wird nach
        Liquidität absteigend sortiert und das Fenster über liquidity_num_max
        weitergeschoben: Ist der Offset-Deckel eines Fensters erreicht, geht
        es mit liquidity_num_max = kleinste gesehene Liquidität weiter.
        Grenz-Duplikate (gleiche Liquidität in zwei Fenstern) fängt die
        Deduplizierung nach condition_id ab; kommt ein Fenster nicht voran
        (>2000 Märkte mit identischer Liquidität), wird mit Warnung
        abgebrochen statt endlos zu schleifen.

        min_volume filtert serverseitig über volume_num_min (GESAMT-Volumen).
        Weil Gesamtvolumen >= 24h-Volumen ist das eine sichere Obermenge des
        24h-Filters des Aufrufers — es fällt nie ein Markt weg, der den
        24h-Filter bestanden hätte, aber der Scan schrumpft deutlich
        (live gemessen: ~9.5k statt ~16k Märkte bei 2k Mindestliquidität).
        Rate-Limit-Budget: ~100 sequenzielle /markets-Calls über ~30s —
        deutlich unter den ~300 Calls/10s der Gamma-API.
        """
        out: dict[str, Market] = {}
        page = 100  # Server-Maximum pro Request (s.o.)
        liq_max: float | None = None
        while True:
            offset = 0
            window_min: float | None = None
            exhausted = False
            while offset <= self._OFFSET_CAP:
                params = dict(
                    active="true", closed="false", order="liquidityNum",
                    ascending="false", limit=page, offset=offset,
                )
                if min_liquidity > 0:
                    params["liquidity_num_min"] = min_liquidity
                if min_volume > 0:
                    params["volume_num_min"] = min_volume
                if liq_max is not None:
                    params["liquidity_num_max"] = liq_max
                rows = self._get("/markets", **params)
                for row in rows:
                    m = _parse_market(row)
                    if m and not m.closed and m.liquidity >= min_liquidity:
                        out.setdefault(m.condition_id, m)
                    if m is not None:
                        window_min = (m.liquidity if window_min is None
                                      else min(window_min, m.liquidity))
                if len(rows) < page:
                    exhausted = True
                    break
                offset += page
            if exhausted or window_min is None:
                break
            if liq_max is not None and window_min >= liq_max:
                log.warning(
                    "Vollmarkt-Scan: Liquiditätsfenster kommt bei %.2f nicht "
                    "voran — Abbruch mit %d Märkten als Teilergebnis",
                    liq_max, len(out),
                )
                break
            liq_max = window_min
        return list(out.values())

    def all_events(self, min_liquidity: float = 0.0, limit: int = 200) -> dict[str, list[Market]]:
        """ALLE aktiven Events (negRisk und normale) mit ihren Teilmärkten.

        Für den Implikations-Detektor (reine Beobachtung): der braucht die
        Event-Gruppierung auch für Events ohne negRisk-Struktur (z.B.
        Over/Under-Ketten am selben Spiel). Anders als negrisk_events wird
        ein Event NICHT verworfen, wenn ein Teilmarkt fehlt/geschlossen ist —
        Implikationspaare sind paarweise gültig, Vollständigkeit ist keine
        Voraussetzung. Nicht parsebare/geschlossene Teilmärkte werden nur
        einzeln herausgefiltert.
        """
        result: dict[str, list[Market]] = {}
        offset = 0
        page = 100  # Server-Cap pro /events-Request (siehe negrisk_events)
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
                rows = [x for x in ev.get("markets", []) if x.get("active") is True]
                markets = [m for m in (_parse_market(x) for x in rows)
                           if m and not m.closed]
                if len(markets) >= 2 and sum(m.liquidity for m in markets) >= min_liquidity:
                    slug = ev.get("slug", ev.get("id", "?"))
                    for m in markets:
                        m.event_slug = slug
                    result[slug] = markets
            if len(events) < page:
                break
            offset += page
        return result

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
