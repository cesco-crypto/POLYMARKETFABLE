"""Live/Paper-Schattenvergleich: Messinstrument für die echte Capture-Quote.

Läuft der Bot LIVE, simuliert der ShadowTracker parallel dieselben (vom
RiskManager freigegebenen) Signale mit einem eigenen PaperBroker gegen
dieselben Orderbücher — mit eigenem In-Memory-Portfolio, das den Live-Lauf
nie berührt. Pro Signal entsteht ein Vergleichs-Datensatz in
data/shadow.jsonl: wie viel hätte der Paper-Lauf gefüllt (theoretische
Gelegenheit), wie viel kam live tatsächlich an, und das Verhältnis
(capture_ratio). Auswertung: `python -m polybot.main capture-report`.

Ehrliche Grenzen der Messung:
- Paper- und Live-Fills werden über (token_id, side, reason) den Signalen
  zugeordnet. Maker-Fills ruhender Orders aus FRÜHEREN Ticks tragen dieselben
  Schlüssel und können einem aktuellen Signal zugerechnet werden — auf beiden
  Seiten (Paper wie Live), der Fehler ist also weitgehend symmetrisch.
- capture_ratio kann > 1.0 werden (Live füllte mehr, als der Paper-Schatten
  simulierte, z.B. weil dem Schatten das Cash ausging) — das wird bewusst
  nicht gekappt, die Aggregation gewichtet ohnehin über Notional-Summen.
- Ohne Paper-Fill (paper_fill == 0) ist die Quote undefiniert (null im JSONL)
  und fließt nicht in die Aggregation ein.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from polybot.config import BotConfig
from polybot.execution import PaperBroker
from polybot.portfolio import Fill, Portfolio
from polybot.strategies.base import MarketSnapshot, Signal

log = logging.getLogger(__name__)

DEFAULT_SHADOW_PATH = Path("data") / "shadow.jsonl"

SECONDS_PER_DAY = 86_400.0

# In-Memory-Fill-Historie des Schatten-Portfolios kappen — der Schatten wird
# nie gespeichert, die Historie dient nur der Tick-Zuordnung.
MAX_SHADOW_FILLS = 200


def strategy_from_reason(reason: str) -> str:
    """Strategiename aus dem Signal-Grund ableiten (Signale tragen keinen).

    Die Strategien prägen stabile Präfixe: 'Komplement-Arb Edge=…',
    'NegRisk-…-Arb Edge=…', 'MM Bid'/'MM Ask'. Unbekannte Gründe fallen auf
    ihr erstes Wort zurück, damit die Aggregation nie crasht.
    """
    r = reason.lower()
    if r.startswith("komplement"):
        return "complement_arb"
    if r.startswith("negrisk"):
        return "negrisk_arb"
    if r.startswith("mm"):
        return "market_making"
    return reason.split(" ", 1)[0] or "unknown"


@dataclass
class ShadowRecord:
    """Vergleichs-Datensatz für EIN Signal-Bein über EINE Episode.

    Episoden-Dedup (Befund Agenten-Flotte 05.07.2026): Dieselbe
    Gelegenheit signalisiert bei 0.5s-Ticks hunderte Male neu — pro Tick
    geloggt war paper_notional ~60x aufgeblasen und die Capture-Quote
    strukturell gegen 0 gedrückt. Jetzt gilt: EINE Episode = eine
    zusammenhängend signalisierende Gelegenheit (Gruppen-Identität, endet
    nach EPISODE_GAP_S Stille). paper_fill ist die grösste Einzel-Tick-
    Füllung des Schattens (das einmalig nehmbare Angebot), live_fill die
    Summe aller Live-Fills der Episode.
    """

    ts: float               # Episodenbeginn
    token_id: str
    side: str
    price: float
    size: float
    reason: str
    strategy: str
    group: str | None
    expected_edge: float    # erwarteter USDC-Gewinn (Arb: nur am ersten Bein)
    paper_fill: float
    live_fill: float
    capture_ratio: float | None
    episode_ticks: int = 1  # wie oft die Gelegenheit signalisiert hat
    episode_s: float = 0.0  # Episodendauer (letzter - erster Tick)


def _sum_by_key(fills: list[Fill]) -> dict[tuple[str, str, str], float]:
    """Fill-Größen je (token_id, side, reason) aufsummieren."""
    out: dict[tuple[str, str, str], float] = {}
    for f in fills:
        k = (f.token_id, f.side, f.reason)
        out[k] = out.get(k, 0.0) + f.size
    return out


@dataclass
class _Leg:
    """Ein Signal-Bein innerhalb einer Episode."""

    token_id: str
    side: str
    price: float
    size: float
    reason: str
    group: str | None
    expected_edge: float
    paper_best: float = 0.0   # grösste Einzel-Tick-Füllung des Schattens
    live_total: float = 0.0   # Summe aller Live-Fills der Episode


@dataclass
class _Episode:
    """Eine zusammenhängend signalisierende Gelegenheit."""

    first_ts: float
    last_ts: float
    ticks: int = 0
    legs: dict = None  # legkey -> _Leg

    def __post_init__(self):
        if self.legs is None:
            self.legs = {}


class ShadowTracker:
    """Führt pro Tick einen Paper-Vergleichslauf und protokolliert die Quote.

    Eigener PaperBroker + eigenes In-Memory-Portfolio (Start-Cash =
    risk.paper_start_cash) — vollständig getrennt vom Live-Portfolio, wird
    nie auf Disk persistiert. Komplette Paare/Sets werden wie im Paper-Modus
    sofort gemergt (Kapital-Recycling), damit dem Schatten nicht nach wenigen
    Arbs das Cash ausgeht und die Quote künstlich fällt.
    """

    # Episode endet nach so vielen Sekunden ohne erneutes Signal. Länger als
    # das Delayed-Order-Poll-Fenster (15s), damit verspätet reconcilte
    # Live-Fills noch ihrer Episode zugerechnet werden.
    EPISODE_GAP_S = 30.0
    # Dauerbrenner-Episoden spätestens nach so vielen Sekunden schliessen
    # (bounded memory; eine stundenlang stehende "Gelegenheit" ist ohnehin
    # ein Stale-Book-Verdachtsfall und soll periodisch im Log auftauchen).
    EPISODE_MAX_S = 600.0

    def __init__(self, cfg: BotConfig, path: str | Path = DEFAULT_SHADOW_PATH,
                 start_cash: float | None = None):
        self.cfg = cfg
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._episodes: dict[str, _Episode] = {}
        self.broker = PaperBroker(cfg)
        # Kein Latenz-Verzug im Schatten: er misst die theoretische
        # Gelegenheit ZUM Signalzeitpunkt gegen die realen Live-Fills
        # DESSELBEN Ticks — der Latenz-Nachteil ist genau das, was die
        # Capture-Quote zeigen soll. Ein verzögerter Schatten würde ihn
        # verstecken und die Signal-Zuordnung in observe() brechen.
        self.broker.fill_delay_ticks = 0
        cash = cfg.risk.paper_start_cash if start_cash is None else start_cash
        self.portfolio = Portfolio(cash=cash, day_start_value=cash)

    def observe(self, signals: list[Signal], books: dict,
                live_fills: list[Fill],
                fee_rates: dict[str, float] | None = None,
                snap: MarketSnapshot | None = None,
                ts: float | None = None) -> list[ShadowRecord]:
        """Einen Tick vergleichen: Paper-Schattenlauf gegen die Live-Fills.

        signals: die vom RiskManager freigegebenen Signale dieses Ticks;
        books: dieselben Bücher, die der Live-Broker gesehen hat;
        live_fills: die in DIESEM Tick tatsächlich gebuchten Live-Fills.
        Darf den Tick nie crashen — Fehler werden nur geloggt.
        """
        ts = time.time() if ts is None else ts
        records: list[ShadowRecord] = []
        try:
            n0 = len(self.portfolio.fills)
            self.broker.execute(list(signals), books, self.portfolio, fee_rates)
            paper_fills = self.portfolio.fills[n0:]
            if snap is not None:
                self._merge(snap)
            paper_avail = _sum_by_key(paper_fills)
            live_avail = _sum_by_key(list(live_fills))
            # ZUERST abgelaufene Episoden schliessen — sonst würde ein
            # Signal nach langer Stille die alte Episode endlos verlängern,
            # statt eine neue Gelegenheit zu beginnen.
            records = self._close_due(ts)
            for s in signals:
                k = (s.token_id, s.side, s.reason)
                paper = min(s.size, paper_avail.get(k, 0.0))
                paper_avail[k] = paper_avail.get(k, 0.0) - paper
                live = min(s.size, live_avail.get(k, 0.0))
                live_avail[k] = live_avail.get(k, 0.0) - live
                self._feed_episode(s, paper, live, ts)
            # Live-Fills OHNE Signal in diesem Tick (z.B. verspätet
            # reconcilte Delayed-Orders): der noch offenen Episode desselben
            # Beins zurechnen, sonst fehlt genau der Fill, den wir messen.
            for (token, side, reason), size in live_avail.items():
                if size > 1e-9:
                    self._feed_stray_live(token, side, reason, size)
            if records:
                self._write(records)
            # Historie kappen — der Schatten läuft potenziell tagelang.
            if len(self.portfolio.fills) > MAX_SHADOW_FILLS:
                del self.portfolio.fills[:-MAX_SHADOW_FILLS]
        except Exception as e:  # noqa: BLE001 — Messpfad darf den Tick nie crashen
            log.warning("Schattenvergleich fehlgeschlagen: %s", e)
        return records

    # ---- Episoden-Verwaltung -------------------------------------------------

    @staticmethod
    def _episode_key(s: Signal) -> str:
        return s.group or f"solo|{s.token_id}|{s.side}|{s.reason}"

    def _feed_episode(self, s: Signal, paper: float, live: float,
                      ts: float) -> None:
        ep = self._episodes.get(self._episode_key(s))
        if ep is None:
            # last_ts=-1: der erste Feed unten zählt als Tick 1.
            ep = _Episode(first_ts=ts, last_ts=-1.0)
            self._episodes[self._episode_key(s)] = ep
        if ep.last_ts < ts:  # ersten Feed pro Tick zählen (Beine teilen den ts)
            ep.ticks += 1
        ep.last_ts = ts
        # Bein-Schlüssel OHNE reason: der Grund trägt den Edge-Wert
        # («Komplement-Arb Edge=0.050»), der sich pro Tick ändert — mit
        # reason im Schlüssel zerfiele die Episode in ein Bein pro
        # Edge-Wert und die Dedup wäre teilweise wieder aufgehoben.
        legkey = f"{s.token_id}|{s.side}"
        leg = ep.legs.get(legkey)
        if leg is None:
            leg = _Leg(token_id=s.token_id, side=s.side, price=s.price,
                       size=s.size, reason=s.reason, group=s.group,
                       expected_edge=s.expected_edge)
            ep.legs[legkey] = leg
        # Angebot = grösste Einzel-Tick-Füllung (das einmalig Nehmbare) —
        # NICHT die Summe über Ticks (das war die 60x-Inflation).
        leg.paper_best = max(leg.paper_best, paper)
        leg.live_total += live

    def _feed_stray_live(self, token: str, side: str, reason: str,
                         size: float) -> None:
        """Live-Fill ohne Signal in diesem Tick der offenen Episode zuordnen.

        Der reason des Fills stammt vom Ursprungs-Tick (mit dessen
        Edge-Wert) — zugeordnet wird über (token, side); reason dient nur
        noch der Diagnose.
        """
        for ep in self._episodes.values():
            leg = ep.legs.get(f"{token}|{side}")
            if leg is not None:
                leg.live_total += size
                return

    def _close_due(self, now: float, force: bool = False) -> list[ShadowRecord]:
        """Abgelaufene Episoden schliessen und als je 1 Record/Bein ausgeben."""
        out: list[ShadowRecord] = []
        for key in list(self._episodes):
            ep = self._episodes[key]
            if not force and (now - ep.last_ts) < self.EPISODE_GAP_S \
                    and (now - ep.first_ts) < self.EPISODE_MAX_S:
                continue
            for leg in ep.legs.values():
                ratio = (leg.live_total / leg.paper_best
                         if leg.paper_best > 1e-9 else None)
                out.append(ShadowRecord(
                    ts=ep.first_ts, token_id=leg.token_id, side=leg.side,
                    price=leg.price, size=leg.size, reason=leg.reason,
                    strategy=strategy_from_reason(leg.reason), group=leg.group,
                    expected_edge=leg.expected_edge,
                    paper_fill=leg.paper_best, live_fill=leg.live_total,
                    capture_ratio=ratio, episode_ticks=ep.ticks,
                    episode_s=ep.last_ts - ep.first_ts))
            del self._episodes[key]
        return out

    def flush(self) -> list[ShadowRecord]:
        """Alle offenen Episoden schliessen (Prozessende) und schreiben."""
        records = self._close_due(time.time(), force=True)
        if records:
            self._write(records)
        return records

    def _merge(self, snap: MarketSnapshot) -> None:
        """Paper-Pendant zum Kapital-Recycling (Spiegel von main.merge_positions).

        Bewusst hier dupliziert statt aus main importiert (Zirkularimport);
        ohne Ledger, weil der Schatten nur die Fill-Quote misst.
        """
        for m in snap.markets:
            self.portfolio.merge_pairs(m.yes_token, m.no_token)
        for ev_markets in snap.negrisk_events.values():
            self.portfolio.merge_negrisk_yes([m.yes_token for m in ev_markets])
            self.portfolio.merge_negrisk_no([m.no_token for m in ev_markets],
                                            len(ev_markets))

    def _write(self, records: list[ShadowRecord]) -> None:
        try:
            with self.path.open("a") as fh:
                for r in records:
                    fh.write(json.dumps(asdict(r)) + "\n")
        except OSError as e:
            log.warning("Shadow-Log %s nicht schreibbar: %s", self.path, e)


# ---- Report-Aggregation (python -m polybot.main capture-report) -------------


def load_shadow(path: str | Path = DEFAULT_SHADOW_PATH) -> list[dict]:
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


def _bucket() -> dict:
    return {"n": 0, "paper_notional": 0.0, "live_notional": 0.0, "capture": None}


def _finalize(b: dict) -> None:
    if b["paper_notional"] > 1e-9:
        b["capture"] = b["live_notional"] / b["paper_notional"]


def aggregate_capture(rows: list[dict]) -> dict:
    """shadow.jsonl aggregieren: Capture-Quote gesamt / pro Strategie / pro Stunde.

    Die Quote ist notional-gewichtet (Summe live-Notional / Summe paper-
    Notional) statt ein Mittel der Einzelquoten — kleine Signale würden das
    Bild sonst genauso stark prägen wie große. Datensätze ohne Paper-Fill
    tragen zu keiner Quote bei (0/0 ist keine Messung).

    Hochrechnung: paper_edge_total = Summe expected_edge * (paper_fill/size)
    ist der USDC-Gewinn, den der Paper-Schatten im Zeitraum theoretisch
    eingefahren hätte; auf 24h skaliert und mit der Gesamt-Capture
    multipliziert ergibt das die ehrliche Live-Erwartung.
    """
    overall = _bucket()
    by_strategy: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    paper_edge_total = 0.0
    for r in rows:
        price = r.get("price", 0.0)
        paper, live = r.get("paper_fill", 0.0), r.get("live_fill", 0.0)
        strat = r.get("strategy", "unknown")
        hour = time.strftime("%Y-%m-%d %H:00", time.gmtime(r["ts"]))
        for b in (overall, by_strategy.setdefault(strat, _bucket()),
                  by_hour.setdefault(hour, _bucket())):
            b["n"] += 1
            b["paper_notional"] += paper * price
            b["live_notional"] += live * price
        size = r.get("size", 0.0)
        if size > 0 and paper > 0:
            # min(…, 1.0): ein Paper-Fill über Signalgröße kommt nicht vor,
            # aber kaputte Log-Zeilen sollen die Hochrechnung nicht aufblasen.
            paper_edge_total += r.get("expected_edge", 0.0) * min(paper / size, 1.0)
    _finalize(overall)
    for b in by_strategy.values():
        _finalize(b)
    for b in by_hour.values():
        _finalize(b)

    first = min((r["ts"] for r in rows), default=0.0)
    last = max((r["ts"] for r in rows), default=0.0)
    duration = last - first
    paper_edge_per_day = (paper_edge_total * SECONDS_PER_DAY / duration
                          if duration > 0 else None)
    live_edge_per_day = (paper_edge_per_day * overall["capture"]
                         if paper_edge_per_day is not None
                         and overall["capture"] is not None else None)
    return {
        "n_records": len(rows),
        "first_ts": first,
        "last_ts": last,
        "duration_s": duration,
        "overall": overall,
        "by_strategy": by_strategy,
        "by_hour": dict(sorted(by_hour.items())),
        "paper_edge_total": paper_edge_total,
        "paper_edge_per_day": paper_edge_per_day,
        "live_edge_per_day": live_edge_per_day,
    }
