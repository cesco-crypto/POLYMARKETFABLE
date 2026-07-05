"""Opportunity-Recorder: Beweisdaten sammeln statt tagelang warten.

Protokolliert bei jedem Tick ALLE beobachteten Fast-Arbitragen als JSONL
(data/opportunities.jsonl) — auch Gelegenheiten UNTER der Handels-Schwelle
(bis Netto-Edge >= EDGE_FLOOR), damit die Verteilung sichtbar wird. Der
Recorder rechnet selbst auf dem MarketSnapshot (dieselben Formeln wie die
Strategien), verändert die Signal-Logik also nicht.

Dazu die Report-Aggregation für `python -m polybot.main report`:
beobachteter Zeitraum, Gelegenheiten über/unter Schwelle, Summe des
theoretischen Profits, 24h-Hochrechnung und die Kapitalfrage ("welches
Kapital wäre für X USDC/Tag nötig?").
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from polybot.config import BotConfig
from polybot.strategies.base import MarketSnapshot
from polybot.strategies.implication_detector import find_violations

log = logging.getLogger(__name__)

# Auch knapp verpasste Gelegenheiten loggen: alles ab Netto-Edge -1 Cent.
# Weiter unten wird nicht protokolliert — das wäre nur Rauschen (fast jeder
# fair bepreiste Markt hätte sonst einen Eintrag pro Tick).
EDGE_FLOOR = -0.01

DEFAULT_PATH = Path("data") / "opportunities.jsonl"

SECONDS_PER_DAY = 86_400.0


@dataclass
class Opportunity:
    """Eine beobachtete (Fast-)Arbitrage zu einem Zeitpunkt.

    Alle Geldgrößen in USDC, Preise pro Share-Set. `gross` ist die Summe der
    besten Asks aller Beine, `net_edge` die Auszahlung minus gross minus
    Gebühren, `depth` die verfügbare Tiefe (min. Ask-Größe über alle Beine)
    und `theo_profit = max(0, net_edge) * depth` der theoretische Profit,
    wenn man die volle Tiefe genommen hätte (ohne Order-/Kapital-Limits).
    """

    ts: float
    kind: str            # "complement" | "negrisk_yes" | "negrisk_no" | "implication"
    market: str          # Marktfrage bzw. Event-Slug
    gross: float         # Brutto-Summe der besten Asks (Kosten pro Set)
    fees: float          # Taker-Gebühren pro Set (rate * p * (1-p) je Bein)
    net_edge: float      # Auszahlung - gross - fees (pro Set)
    depth: float         # verfügbare Tiefe in Sets (min. Bein-Größe)
    theo_profit: float   # max(0, net_edge) * depth
    above_threshold: bool  # hätte der Bot gehandelt? (Edge- UND Größen-Schwelle)


def _fee(rate: float, price: float) -> float:
    """Taker-Gebühr pro Share: rate * p * (1-p)."""
    return rate * price * (1 - price)


def find_opportunities(cfg: BotConfig, snap: MarketSnapshot,
                       ts: float | None = None) -> list[Opportunity]:
    """Alle (Fast-)Arbitragen im Snapshot berechnen — Spiegel der Strategien.

    Dieselben Formeln wie complement_arb/negrisk_arb, aber ohne deren
    Handels-Filter: protokolliert wird alles ab EDGE_FLOOR, unabhängig von
    min_edge und Ordergrößen-Limits. `above_threshold` markiert, ob der Bot
    bei dieser Gelegenheit tatsächlich Signale erzeugt hätte.
    """
    ts = time.time() if ts is None else ts
    out: list[Opportunity] = []
    min_edge = cfg.risk.min_edge
    min_shares = cfg.strategy.min_order_shares

    def rate(token_id: str) -> float:
        return snap.fee_rates.get(token_id, cfg.risk.taker_fee_rate)

    def add(kind: str, market: str, payout: float,
            legs: list[tuple[str, float, float]]) -> None:
        """legs: (token_id, ask_price, ask_size) je Bein."""
        gross = sum(p for _, p, _ in legs)
        fees = sum(_fee(rate(t), p) for t, p, _ in legs)
        net_edge = payout - gross - fees
        if net_edge < EDGE_FLOOR:
            return
        depth = min(s for _, _, s in legs)
        out.append(Opportunity(
            ts=ts, kind=kind, market=market, gross=gross, fees=fees,
            net_edge=net_edge, depth=depth,
            theo_profit=max(0.0, net_edge) * depth,
            above_threshold=net_edge >= min_edge and depth >= min_shares,
        ))

    # Wie in complement_arb: negRisk-Teilmärkte auslassen — für die ist
    # negrisk_arb (und unten die Event-Betrachtung) zuständig.
    negrisk_tokens = {
        t
        for ev_markets in snap.negrisk_events.values()
        for nm in ev_markets
        for t in (nm.yes_token, nm.no_token)
    }
    for m in snap.markets:
        if m.yes_token in negrisk_tokens or m.no_token in negrisk_tokens:
            continue
        yb, nb = snap.books.get(m.yes_token), snap.books.get(m.no_token)
        if not yb or not nb or not yb.best_ask or not nb.best_ask:
            continue
        ya, na = yb.best_ask, nb.best_ask
        add("complement", m.question, 1.0,
            [(m.yes_token, ya.price, ya.size), (m.no_token, na.price, na.size)])

    for slug, markets in snap.negrisk_events.items():
        yes_legs, no_legs = [], []
        for m in markets:
            yb, nb = snap.books.get(m.yes_token), snap.books.get(m.no_token)
            if not yb or not yb.best_ask or not nb or not nb.best_ask:
                yes_legs = no_legs = []  # unvollständiges Event -> auslassen
                break
            yes_legs.append((m.yes_token, yb.best_ask.price, yb.best_ask.size))
            no_legs.append((m.no_token, nb.best_ask.price, nb.best_ask.size))
        if not yes_legs:
            continue
        # YES-Struktur nur, wenn die Outcome-Menge garantiert vollständig
        # bleibt (kein negRiskAugmented) — sonst keine echte Arbitrage.
        if not any(m.neg_risk_augmented for m in markets):
            add("negrisk_yes", slug, 1.0, yes_legs)
        add("negrisk_no", slug, float(len(markets) - 1), no_legs)

    # Cross-Market-Implikationen (kind='implication'): reine Beobachtung —
    # der Detektor erzeugt nie Signale, above_threshold ist deshalb immer
    # False. Geloggt wird nur bei klarer Verletzung (net_edge > MARGIN),
    # nicht ab EDGE_FLOOR — konsistente Paare wären nur Rauschen.
    if cfg.strategy.detect_implications:
        try:
            for v in find_violations(cfg, snap):
                out.append(Opportunity(
                    ts=ts, kind="implication", market=v.label,
                    gross=v.gross, fees=v.fees, net_edge=v.net_edge,
                    depth=v.depth,
                    theo_profit=max(0.0, v.net_edge) * v.depth,
                    above_threshold=False,
                ))
        except Exception as e:  # noqa: BLE001 — Messpfad darf den Tick nie crashen
            log.warning("Implikations-Detektor fehlgeschlagen: %s", e)
    return out


class OpportunityRecorder:
    """Hängt sich an tick() und schreibt Gelegenheiten append-only als JSONL."""

    def __init__(self, cfg: BotConfig, path: str | Path = DEFAULT_PATH):
        self.cfg = cfg
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def observe(self, snap: MarketSnapshot, ts: float | None = None) -> list[Opportunity]:
        """Snapshot auswerten und Gelegenheiten protokollieren.

        Darf den Tick nie crashen: Schreibfehler werden nur geloggt.
        """
        opps = find_opportunities(self.cfg, snap, ts=ts)
        if opps:
            try:
                with self.path.open("a") as fh:
                    for o in opps:
                        fh.write(json.dumps(asdict(o)) + "\n")
            except OSError as e:
                log.warning("Opportunity-Log %s nicht schreibbar: %s", self.path, e)
        return opps


# ---- Report-Aggregation (python -m polybot.main report) --------------------


@dataclass
class ReportStats:
    """Aggregat über eine Opportunity-JSONL-Datei."""

    first_ts: float
    last_ts: float
    duration_s: float          # beobachteter Zeitraum (0 bei < 2 Zeitpunkten)
    n_above: int               # Gelegenheiten über der Handels-Schwelle
    n_below: int               # geloggt, aber unter der Schwelle
    theo_profit_total: float   # Summe theoretischer Profit im Zeitraum
    theo_profit_per_day: float  # Hochrechnung auf 24h (0 bei duration_s == 0)


def load_opportunities(path: str | Path = DEFAULT_PATH) -> list[dict]:
    """JSONL einlesen; kaputte Zeilen überspringen (append-only Log)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                log.warning("Kaputte Zeile in %s übersprungen", p)
                continue
            if isinstance(row, dict) and "ts" in row:
                out.append(row)
    return out


def aggregate(opps: list[dict]) -> ReportStats:
    if not opps:
        return ReportStats(0.0, 0.0, 0.0, 0, 0, 0.0, 0.0)
    first = min(o["ts"] for o in opps)
    last = max(o["ts"] for o in opps)
    duration = last - first
    n_above = sum(1 for o in opps if o.get("above_threshold"))
    total = sum(o.get("theo_profit", 0.0) for o in opps)
    per_day = total * SECONDS_PER_DAY / duration if duration > 0 else 0.0
    return ReportStats(
        first_ts=first, last_ts=last, duration_s=duration,
        n_above=n_above, n_below=len(opps) - n_above,
        theo_profit_total=total, theo_profit_per_day=per_day,
    )


def _daily_profit_at_capital(opps: list[dict], duration_s: float,
                             capital: float) -> float:
    """Hochgerechneter Tagesprofit bei gegebenem Arbeitskapital.

    Modell: Das Kapital wird pro Gelegenheit neu eingesetzt (Arb-Sets werden
    sofort zu USDC gemergt, das Kapital ist also nach jedem Trade wieder
    frei). Pro Gelegenheit ist der Einsatz durch Kapital UND verfügbare
    Tiefe begrenzt: profit = net_edge * min(depth, capital / Kosten_pro_Set).
    """
    if duration_s <= 0:
        return 0.0
    total = 0.0
    for o in opps:
        edge = o.get("net_edge", 0.0)
        depth = o.get("depth", 0.0)
        if edge <= 0 or depth <= 0:
            continue
        cost_per_set = o.get("gross", 0.0) + o.get("fees", 0.0)
        sets = depth if cost_per_set <= 0 else min(depth, capital / cost_per_set)
        total += edge * sets
    return total * SECONDS_PER_DAY / duration_s


def required_capital(opps: list[dict], duration_s: float,
                     target_per_day: float) -> tuple[float | None, float]:
    """Kapitalfrage: 'Welches Kapital wäre für X USDC/Tag nötig?'

    Rückgabe: (benötigtes Kapital oder None, maximal erreichbarer Tages-
    profit). None bedeutet: selbst mit unbegrenztem Kapital reicht die
    gemessene Gelegenheitsdichte nicht für das Ziel — die Tiefe ist der
    Engpass. Der Tagesprofit ist monoton in Kapital -> Binärsuche.
    """
    cap_max = max(
        (o.get("depth", 0.0) * (o.get("gross", 0.0) + o.get("fees", 0.0))
         for o in opps if o.get("net_edge", 0.0) > 0),
        default=0.0,
    )
    max_daily = _daily_profit_at_capital(opps, duration_s, cap_max)
    if target_per_day <= 0:
        return 0.0, max_daily
    if max_daily < target_per_day:
        return None, max_daily
    lo, hi = 0.0, max(cap_max, 1e-9)
    for _ in range(80):  # Bisektion: cap_max / 2^80 << 1 Cent
        mid = (lo + hi) / 2
        if _daily_profit_at_capital(opps, duration_s, mid) >= target_per_day:
            hi = mid
        else:
            lo = mid
    return hi, max_daily
