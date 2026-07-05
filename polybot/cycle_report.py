"""Zyklus-Selbstauswertung: PnL-Ledger + kompakter Cycle-Report.

Der Messbot startet den Bot in 90-Minuten-Zyklen frisch. Damit
`python -m polybot.main cycle-report` trotzdem Fenster-Metriken liefern kann
(letzte 1h/3h, seit UTC-Tagesbeginn, seit Prozessstart), schreibt der
Bot-Loop einen schlanken, prozessübergreifenden PnL-Ledger
(data/pnl_ledger.jsonl, append-only JSONL):

- event="start": Zählerstand beim Prozessstart (Baseline "seit Prozessstart")
- event="tick":  kumulierte Zähler (realized_pnl/fees/rebates/cash) — nur bei
  Änderung oder als Heartbeat alle HEARTBEAT_S, damit die Datei klein bleibt
- event="merge": ein realisierter Arb-Merge mit Markt-Label, Sets und PnL —
  Basis für "Top-Märkte nach realisiertem Profit"

Fenster-Deltas entstehen aus den kumulierten Zählern: Baseline ist die letzte
Zähler-Zeile VOR dem Fensterstart (Zähler ändern sich nur, wenn eine Zeile
geschrieben wurde — zwischen zwei Zeilen ist der Stand konstant).

Der Report kombiniert Ledger, Opportunity-Log (recorder) und Paper-State zu
einer Bildschirmseite plus JSON (data/cycle_report.json).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = Path("data") / "pnl_ledger.jsonl"
DEFAULT_REPORT_PATH = Path("data") / "cycle_report.json"

# Unveränderte Zähler höchstens einmal pro Heartbeat schreiben — sonst wächst
# der Ledger bei aktivem Stream (Inner-Loop alle stream_tick_s) sinnlos.
HEARTBEAT_S = 60.0

# Fenster kürzer als ~36s liefern keine sinnvolle Stundenrate (Division
# durch fast 0 explodiert) — dann lieber 0 ausweisen.
MIN_RATE_HOURS = 0.01

# Bekannte Opportunity-Arten des Recorders (feste Zeilenreihenfolge im Report).
OPP_KINDS = ("complement", "negrisk_yes", "negrisk_no", "negrisk_partial_no",
             "implication")

# Welche Strategie eine Opportunity-Art handeln würde — Arten ohne Strategie
# (negrisk_partial_no, implication: reine Beobachtung) sind per Definition
# ungenutzter theo_profit.
KIND_STRATEGY = {
    "complement": "complement_arb",
    "negrisk_yes": "negrisk_arb",
    "negrisk_no": "negrisk_arb",
    "negrisk_partial_no": None,
    "implication": None,
}

COUNTER_KEYS = ("realized_pnl", "fees_paid", "rebates_earned")


def _counters(portfolio) -> dict:
    """Kumulierte Portfolio-Zähler defensiv lesen (Test-Stubs sind unvollständig)."""
    return {
        "realized_pnl": float(getattr(portfolio, "realized_pnl", 0.0)),
        "fees_paid": float(getattr(portfolio, "fees_paid", 0.0)),
        "rebates_earned": float(getattr(portfolio, "rebates_earned", 0.0)),
        "cash": float(getattr(portfolio, "cash", 0.0)),
    }


class CycleLedger:
    """Hängt sich an den Bot-Loop und schreibt den PnL-Ledger append-only.

    Darf den Tick nie crashen: Schreibfehler werden nur geloggt (dasselbe
    Prinzip wie beim OpportunityRecorder).
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else Path(DEFAULT_LEDGER_PATH)
        self._last: dict | None = None  # zuletzt geschriebene Zähler
        self._last_ts: float = 0.0

    def _append(self, row: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as e:
            log.warning("PnL-Ledger %s nicht schreibbar: %s", self.path, e)

    def record_start(self, portfolio, ts: float | None = None) -> None:
        """Prozessstart markieren — Baseline für 'seit Prozessstart'."""
        ts = time.time() if ts is None else ts
        cur = _counters(portfolio)
        self._append({"ts": ts, "event": "start", **cur})
        self._last, self._last_ts = cur, ts

    def record_tick(self, portfolio, ts: float | None = None) -> None:
        """Zählerstand nach einem Tick protokollieren (dedupliziert).

        Geschrieben wird nur bei geänderten Zählern oder als Heartbeat alle
        HEARTBEAT_S — so haben Fenster-Baselines nie mehr als HEARTBEAT_S
        Unschärfe, ohne dass der Ledger pro Stream-Tick wächst.
        """
        ts = time.time() if ts is None else ts
        cur = _counters(portfolio)
        if (self._last is not None and cur == self._last
                and ts - self._last_ts < HEARTBEAT_S):
            return
        self._append({"ts": ts, "event": "tick", **cur})
        self._last, self._last_ts = cur, ts

    def record_merge(self, market: str, kind: str, sets: float, pnl: float,
                     ts: float | None = None) -> None:
        """Einen realisierten Merge (Arb-Gewinnmitnahme) mit Markt-Label loggen."""
        ts = time.time() if ts is None else ts
        self._append({"ts": ts, "event": "merge", "market": market,
                      "kind": kind, "sets": float(sets), "pnl": float(pnl)})


def load_ledger(path: str | Path = DEFAULT_LEDGER_PATH) -> list[dict]:
    """Ledger-JSONL einlesen; kaputte Zeilen überspringen (append-only Log)."""
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
            if isinstance(row, dict) and "ts" in row and "event" in row:
                out.append(row)
    return out


# ---- Fenster-Arithmetik -----------------------------------------------------


def utc_day_start(now: float) -> float:
    """Timestamp des UTC-Tagesbeginns (00:00) des Tages von `now`."""
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def process_start_ts(rows: list[dict]) -> float | None:
    """Zeitpunkt des letzten Prozessstarts laut Ledger (None ohne start-Event)."""
    starts = [r["ts"] for r in rows if r.get("event") == "start"]
    return max(starts) if starts else None


def delta_since(rows: list[dict], start_ts: float) -> dict:
    """Delta der kumulierten Zähler seit start_ts.

    Baseline ist die letzte start/tick-Zeile mit ts <= start_ts (der Stand
    zum Fensterstart — Zähler ändern sich nur per geschriebener Zeile).
    Beginnt der Ledger erst IM Fenster, dient die erste Zeile als Baseline
    (das Delta ist dann eine Untergrenze) und `covered_from` markiert, ab
    wann das Fenster tatsächlich abgedeckt ist. Ohne Zähler-Zeilen: alles 0.
    """
    counters = sorted((r for r in rows if r.get("event") in ("start", "tick")),
                      key=lambda r: r["ts"])
    if not counters:
        return {k: 0.0 for k in COUNTER_KEYS} | {"covered_from": None}
    baseline = None
    for r in counters:
        if r["ts"] <= start_ts:
            baseline = r
        else:
            break
    covered_from = start_ts if baseline is not None else counters[0]["ts"]
    if baseline is None:
        baseline = counters[0]
    latest = counters[-1]
    out = {k: latest.get(k, 0.0) - baseline.get(k, 0.0) for k in COUNTER_KEYS}
    out["covered_from"] = covered_from
    return out


def top_markets(rows: list[dict], start_ts: float, n: int = 5) -> list[dict]:
    """Top-N Märkte nach realisiertem Merge-PnL seit start_ts."""
    per: dict[str, dict] = {}
    for r in rows:
        if r.get("event") != "merge" or r.get("ts", 0.0) < start_ts:
            continue
        agg = per.setdefault(r.get("market", "?"), {
            "market": r.get("market", "?"), "pnl": 0.0, "sets": 0.0, "merges": 0,
        })
        agg["pnl"] += r.get("pnl", 0.0)
        agg["sets"] += r.get("sets", 0.0)
        agg["merges"] += 1
    return sorted(per.values(), key=lambda m: m["pnl"], reverse=True)[:n]


def opps_by_kind(opps: list[dict], start_ts: float) -> dict[str, dict]:
    """Recorder-Gelegenheiten seit start_ts nach kind aggregieren.

    n_pos zählt nur echte Gelegenheiten (theo_profit > 0); n_logged auch die
    Fast-Gelegenheiten, die der Recorder ab EDGE_FLOOR protokolliert.
    """
    out = {k: {"n_pos": 0, "n_logged": 0, "theo_profit": 0.0} for k in OPP_KINDS}
    for o in opps:
        if o.get("ts", 0.0) < start_ts:
            continue
        agg = out.setdefault(o.get("kind", "?"),
                             {"n_pos": 0, "n_logged": 0, "theo_profit": 0.0})
        agg["n_logged"] += 1
        theo = o.get("theo_profit", 0.0)
        if theo > 0:
            agg["n_pos"] += 1
        agg["theo_profit"] += theo
    return out


# ---- Engpass-Analyse --------------------------------------------------------


def bottleneck(opps: list[dict], realized: float, cash: float, min_edge: float,
               min_shares: float, max_order_usdc: float,
               enabled: list[str]) -> dict:
    """Engpass-Hinweis: wohin ist der positive theo_profit verschwunden?

    Zerlegt den positiven theoretischen Profit rein aus den Recorder-Feldern:
    - lost_no_strategy: Arten ohne handelnde Strategie (z.B. implication)
    - lost_min_size:    Edge über min_edge, aber Tiefe < Mindestordergröße
    - lost_min_edge:    positive Edge unterhalb der Handels-Schwelle
    - lost_order_cap:   handelbar, aber Tiefe über dem Order-Limit —
      Näherung mit Sets <= max_order_usdc / Kosten_pro_Set (konservativ:
      real gilt das Limit pro Bein, das erlaubt eher mehr Sets)
    - residual:         handelbar und im Limit, aber nicht realisiert
      (Timing/Wettbewerb/Cash) = theo_tradeable - lost_order_cap - realized
    Dazu die Cash-Frage: braucht die größte handelbare Einzelgelegenheit
    mehr Kapital, als das Portfolio an Cash hat?
    """
    theo_total = theo_tradeable = 0.0
    lost_no_strategy = lost_min_size = lost_min_edge = lost_order_cap = 0.0
    max_capital_need = 0.0
    for o in opps:
        theo = o.get("theo_profit", 0.0)
        edge = o.get("net_edge", 0.0)
        depth = o.get("depth", 0.0)
        if theo <= 0 or edge <= 0 or depth <= 0:
            continue
        theo_total += theo
        strategy = KIND_STRATEGY.get(o.get("kind"))
        cost_per_set = o.get("gross", 0.0) + o.get("fees", 0.0)
        if strategy is None or strategy not in enabled:
            lost_no_strategy += theo
        elif o.get("above_threshold"):
            theo_tradeable += theo
            if cost_per_set > 0:
                cap_sets = max_order_usdc / cost_per_set
                lost_order_cap += edge * max(0.0, depth - cap_sets)
                max_capital_need = max(max_capital_need,
                                       min(depth, cap_sets) * cost_per_set)
        elif edge >= min_edge:
            lost_min_size += theo
        else:
            lost_min_edge += theo

    unused = max(0.0, theo_total - realized)
    unused_pct = 100.0 * unused / theo_total if theo_total > 0 else 0.0
    capture_pct = 100.0 * realized / theo_tradeable if theo_tradeable > 0 else 0.0
    residual = max(0.0, theo_tradeable - lost_order_cap - realized)
    cash_binding = max_capital_need > cash

    if theo_total <= 0:
        text = ("Keine Gelegenheiten mit positivem theo_profit im Tagesfenster "
                "beobachtet — Engpass-Analyse leer.")
    else:
        parts = []
        if lost_no_strategy > 0:
            parts.append(f"{lost_no_strategy:.2f} USDC ohne handelnde Strategie "
                         f"(z.B. implication)")
        if lost_min_size > 0:
            parts.append(f"{lost_min_size:.2f} USDC unter Mindestgröße "
                         f"(Tiefe < {min_shares:.0f} Shares)")
        if lost_min_edge > 0:
            parts.append(f"{lost_min_edge:.2f} USDC unter min_edge ({min_edge:.3f})")
        if lost_order_cap > 0:
            parts.append(f"{lost_order_cap:.2f} USDC über max_order_usdc "
                         f"({max_order_usdc:.0f}) gedeckelt")
        if residual > 0.005:
            parts.append(f"{residual:.2f} USDC auf handelbaren Gelegenheiten "
                         f"nicht realisiert (Timing/Wettbewerb)")
        cash_txt = (f"Cash bindet: größte Einzelgelegenheit braucht "
                    f"~{max_capital_need:.0f} USDC > Cash {cash:.0f}"
                    if cash_binding else "Cash nicht bindend")
        text = (f"{unused_pct:.0f}% des positiven theo_profit heute ungenutzt "
                f"({unused:.2f} von {theo_total:.2f} USDC)"
                + (" — " + "; ".join(parts) if parts else "")
                + f". {cash_txt}."
                + (f" Capture auf handelbaren Gelegenheiten: {capture_pct:.0f}%."
                   if theo_tradeable > 0 else ""))

    return {
        "theo_total": theo_total,
        "theo_tradeable": theo_tradeable,
        "realized": realized,
        "unused": unused,
        "unused_pct": unused_pct,
        "capture_pct": capture_pct,
        "lost_no_strategy": lost_no_strategy,
        "lost_min_size": lost_min_size,
        "lost_min_edge": lost_min_edge,
        "lost_order_cap": lost_order_cap,
        "residual": residual,
        "max_capital_need": max_capital_need,
        "cash_binding": cash_binding,
        "text": text,
    }


# ---- Report-Aufbau ----------------------------------------------------------


def build_report(cfg, now: float, ledger_rows: list[dict], opps: list[dict],
                 portfolio) -> dict:
    """Alle Report-Bausteine zu einem JSON-tauglichen Dict kombinieren."""
    day0 = utc_day_start(now)
    proc0 = process_start_ts(ledger_rows)
    window_starts: dict[str, float] = {
        "1h": now - 3_600.0,
        "3h": now - 10_800.0,
        "today": day0,
    }
    if proc0 is not None:
        window_starts["process"] = proc0

    windows: dict[str, dict] = {}
    for name, start in window_starts.items():
        d = delta_since(ledger_rows, start)
        covered_from = d.pop("covered_from")
        hours = (max(0.0, (now - covered_from) / 3600.0)
                 if covered_from is not None else 0.0)
        windows[name] = {
            "start_ts": start,
            "hours_covered": hours,
            "pnl": d["realized_pnl"],
            "fees": d["fees_paid"],
            "rebates": d["rebates_earned"],
            "rate_per_h": d["realized_pnl"] / hours if hours >= MIN_RATE_HOURS else 0.0,
        }

    opps_today = [o for o in opps if o.get("ts", 0.0) >= day0]
    bn = bottleneck(
        opps_today,
        realized=windows["today"]["pnl"],
        cash=float(getattr(portfolio, "cash", 0.0)),
        min_edge=cfg.risk.min_edge,
        min_shares=cfg.strategy.min_order_shares,
        max_order_usdc=cfg.risk.max_order_usdc,
        enabled=list(cfg.strategy.enabled),
    )

    return {
        "generated_at": now,
        "generated_at_iso": datetime.fromtimestamp(now, tz=timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S UTC"),
        "day_start_ts": day0,
        "process_start_ts": proc0,
        "windows": windows,
        "opportunities": {name: opps_by_kind(opps, start)
                          for name, start in window_starts.items()},
        "top_markets_today": top_markets(ledger_rows, day0),
        "bottleneck": bn,
        "totals": {
            "cash": float(getattr(portfolio, "cash", 0.0)),
            "realized_pnl": float(getattr(portfolio, "realized_pnl", 0.0)),
            "fees_paid": float(getattr(portfolio, "fees_paid", 0.0)),
            "rebates_earned": float(getattr(portfolio, "rebates_earned", 0.0)),
            "positions": len(getattr(portfolio, "positions", {}) or {}),
            "fills": len(getattr(portfolio, "fills", []) or []),
        },
    }
