"""Cross-Market-Implikations-Detektor — NUR BEOBACHTEN, NICHT HANDELN.

Zwischen Märkten desselben Events gelten logische Implikationen: das
spezifischere Outcome impliziert das allgemeinere, also muss
P(spezifisch) <= P(allgemein) gelten. Erkannt werden ausschließlich
robuste, aus den Titeln parsbare Muster (konservativ — lieber ein Paar
verpassen als ein falsches "Paar" loggen):

  - Over/Under-Ketten am selben Event: Titel-Muster 'O/U X.5' (auch
    'Over/Under X.5') mit identischer Titel-Schablone bis auf die Linie.
    Implikation: P(Over 3.5) <= P(Over 2.5). Annahme: das erste Outcome
    (yes_token) ist die Over-Seite — Polymarket listet O/U-Märkte so.
  - win/reach-final-Paare: 'Will X win the <tournament|title|cup|...>?'
    impliziert 'Will X reach/advance to/make the final?' — gleiches
    Subjekt X im selben Event vorausgesetzt.

Eine Preis-Verletzung P(spezifisch) > P(allgemein) + fees + MARGIN ist
eine potenzielle Arbitrage: NO(spezifisch) + YES(allgemein) kaufen zahlt
garantiert mindestens 1 USDC aus (tritt das spezifische Outcome ein, tritt
auch das allgemeine ein — beide Beine können nie gleichzeitig wertlos
werden). Verletzungen werden als Opportunity (kind='implication') in den
bestehenden Recorder geloggt; es werden NIE Signale erzeugt (Messung vor
Trade).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.strategies.base import MarketSnapshot

# Verletzungs-Schwelle: geloggt wird erst ab net_edge > MARGIN, d.h.
# P(spezifisch) > P(allgemein) + fees + 0.005 — halbe Tick-Größe Puffer
# gegen Rundungs-/Stale-Rauschen.
IMPLICATION_MARGIN = 0.005

# Over/Under-Linie im Titel: 'O/U 2.5' oder 'Over/Under 2.5'. Nur .5-Linien
# (keine Push-Möglichkeit) — ganzzahlige Linien wären keine saubere Kette.
_OU_RE = re.compile(r"\b(?:o/u|over/under)\s*(\d+\.5)\b", re.IGNORECASE)

# 'Will <Subjekt> win <Objekt>?' — das Objekt muss ein Turnier-Wort tragen,
# damit 'win the next match' o.ä. NICHT als Implikation durchgeht.
_WIN_RE = re.compile(r"^will\s+(.+?)\s+win\s+(?:the\s+)?(.+?)\s*\??$",
                     re.IGNORECASE)
_WIN_OBJECT_RE = re.compile(
    r"\b(tournament|title|cup|championship|trophy|finals?|league)\b",
    re.IGNORECASE)
# 'Will <Subjekt> reach/advance to/make (it to) the final(s)?'
_REACH_RE = re.compile(
    r"^will\s+(.+?)\s+(?:reach|advance\s+to|make(?:\s+it\s+to)?)\s+"
    r"(?:the\s+)?finals?\s*\??$",
    re.IGNORECASE)


@dataclass
class ImplicationPair:
    """Ein erkanntes Paar: specific impliziert general (P_spec <= P_gen)."""

    event_slug: str
    specific: Market   # das spezifischere Outcome (muss <= sein)
    general: Market    # das allgemeinere Outcome
    relation: str      # "over_under" | "win_reach"


@dataclass
class ImplicationViolation:
    """Eine beobachtete Preis-Verletzung eines Implikationspaars.

    Struktur: NO(specific) + YES(general) kaufen, garantierte Auszahlung 1
    (Auszahlung 2, falls general ohne specific eintritt — konservativ
    ignoriert). gross/fees/net_edge pro Set, depth in Sets.
    """

    pair: ImplicationPair
    gross: float       # NO-Ask(specific) + YES-Ask(general)
    fees: float        # Taker-Gebühren beider Beine (rate * p * (1-p))
    net_edge: float    # 1 - gross - fees
    depth: float       # min verfügbare Ask-Tiefe beider Beine

    @property
    def label(self) -> str:
        return (f"{self.pair.event_slug}: '{self.pair.specific.question}' => "
                f"'{self.pair.general.question}'")


def _ou_key(question: str) -> tuple[str, float] | None:
    """(Titel-Schablone, Linie) aus einem O/U-Titel — None, wenn kein Muster.

    Die Schablone ist der Titel mit der Linie durch '#' ersetzt: nur Märkte
    mit EXAKT gleicher Schablone bilden eine Kette (konservativ — 'O/U 2.5
    goals' und 'O/U 3.5 corners' werden nie gepaart).
    """
    m = _OU_RE.search(question)
    if not m:
        return None
    template = (question[: m.start(1)] + "#" + question[m.end(1):]).casefold().strip()
    return template, float(m.group(1))


def find_implication_pairs(events: dict[str, list[Market]]) -> list[ImplicationPair]:
    """Implikationspaare in Event-Gruppierungen erkennen (nur Titel-Parsing).

    Konservativ: nur eindeutige Muster; kaputte/unpassende Titel werden
    stillschweigend ignoriert. Braucht keine Orderbücher — kann deshalb vor
    dem Buch-Laden laufen, um die Bein-Tokens zu bestimmen.
    """
    pairs: list[ImplicationPair] = []
    for slug, markets in events.items():
        # Over/Under-Ketten: gleiche Schablone, aufsteigende Linien —
        # jede höhere Linie impliziert jede niedrigere (alle Paare, denn
        # eine Verletzung über der Schwelle muss nicht zwischen benachbarten
        # Linien liegen).
        chains: dict[str, list[tuple[float, Market]]] = {}
        for m in markets:
            key = _ou_key(m.question)
            if key is not None:
                chains.setdefault(key[0], []).append((key[1], m))
        for entries in chains.values():
            entries.sort(key=lambda e: e[0])
            for i, (lo_line, lo_m) in enumerate(entries):
                for hi_line, hi_m in entries[i + 1:]:
                    if hi_line > lo_line:  # gleiche Linie doppelt gelistet -> kein Paar
                        pairs.append(ImplicationPair(
                            event_slug=slug, specific=hi_m, general=lo_m,
                            relation="over_under"))

        # win/reach-final-Paare: gleiches Subjekt, win-Objekt mit Turnier-Wort.
        wins: list[tuple[str, Market]] = []
        reaches: list[tuple[str, Market]] = []
        for m in markets:
            q = m.question.strip()
            wm = _WIN_RE.match(q)
            if wm and _WIN_OBJECT_RE.search(wm.group(2)):
                wins.append((wm.group(1).casefold(), m))
            rm = _REACH_RE.match(q)
            if rm:
                reaches.append((rm.group(1).casefold(), m))
        for w_subj, w_mkt in wins:
            for r_subj, r_mkt in reaches:
                if w_subj == r_subj and w_mkt is not r_mkt:
                    pairs.append(ImplicationPair(
                        event_slug=slug, specific=w_mkt, general=r_mkt,
                        relation="win_reach"))
    return pairs


def pair_tokens(pairs: list[ImplicationPair]) -> set[str]:
    """Die Bein-Tokens aller Paare — dafür braucht der Detektor volle Bücher."""
    return {t for p in pairs for t in (p.specific.no_token, p.general.yes_token)}


def find_violations(cfg: BotConfig, snap: MarketSnapshot) -> list[ImplicationViolation]:
    """Preis-Verletzungen der Implikationspaare im Snapshot finden.

    Geprüft werden snap.events UND snap.negrisk_events (bei Slug-Kollision
    gewinnt die negRisk-Sicht — identische Märkte, strengere Filterung).
    Eine Verletzung liegt vor, wenn die implizite P(spezifisch) über der
    NO-Ask-Seite (1 - NO-Ask) den YES-Ask des allgemeineren Markts um mehr
    als fees + IMPLICATION_MARGIN übersteigt — äquivalent: die garantierte
    Auszahlung 1 der Struktur NO(spezifisch)+YES(allgemein) kostet weniger
    als 1 - fees - MARGIN.
    """
    events: dict[str, list[Market]] = dict(snap.negrisk_events)
    for slug, ms in snap.events.items():
        events.setdefault(slug, ms)

    def rate(token_id: str) -> float:
        return snap.fee_rates.get(token_id, cfg.risk.taker_fee_rate)

    out: list[ImplicationViolation] = []
    for pair in find_implication_pairs(events):
        no_book = snap.books.get(pair.specific.no_token)
        yes_book = snap.books.get(pair.general.yes_token)
        if not no_book or not yes_book:
            continue
        na, ya = no_book.best_ask, yes_book.best_ask
        if not na or not ya:
            continue
        gross = na.price + ya.price
        fees = (rate(pair.specific.no_token) * na.price * (1 - na.price)
                + rate(pair.general.yes_token) * ya.price * (1 - ya.price))
        net_edge = 1.0 - gross - fees
        if net_edge <= IMPLICATION_MARGIN:
            continue  # keine (klare) Verletzung -> still bleiben
        out.append(ImplicationViolation(
            pair=pair, gross=gross, fees=fees, net_edge=net_edge,
            depth=min(na.size, ya.size)))
    return out
