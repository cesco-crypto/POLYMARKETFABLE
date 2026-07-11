"""Full-Universe-Audit der Low-Vol-Reward-Edge — der Survivorship-/Look-ahead-Kill.

Motiv (Ehrlichkeit, Prinzip Nr. 2): Unsere EINE überlebende Edge — „ruhige
(Low-Vol) Reward-Märkte tragen den Maker-Farming-Netto-Edge" — wurde bisher nur
über die 12 handverlesenen Watchlist-Märkte (reward_watchlist.json) über EIN
kurzes Fenster mit OOS p≈0.001 gemessen. Ein sauberer Backtest (die Wall in
`volatility_edge`/`split_history` sitzt korrekt: Selektion auf IS-Vol, Messung
auf OOS) schützt NICHT gegen zwei Meta-Look-aheads:

  Risiko A — Threshold auf der Zukunft gewählt: Wurde die Vol-Schwelle (0.002)
             gepickt, WEIL sie die beste OOS-Trennung gab? -> Threshold-Sweep.
  Risiko B — Basket survivor-gepickt: Wurden die 12 Märkte NACHTRÄGLICH gewählt,
             weil sie gut backtesteten? -> Lauf über das GESAMTE, unvor-
             eingenommene Reward-Universum (fetch_reward_markets, API-Reihenfolge).

Kern-Idee: Wenn die Low-Vol-Regel über das ganze Universum (nicht nur die 12)
OOS immer noch positiv trennt UND der Anteil positiver OOS-Netti der selektierten
Märkte signifikant über 50 % liegt (exakter Binomial-Sign-Test), dann war der
Edge nicht cherry-gepickt. Verschwindet die Trennung -> der Basket war eine
schöne Lüge, und wir begraben die Edge ehrlich (die wertvollste Erkenntnis).

Alle Statistik-Funktionen sind rein/deterministisch (offline testbar); nur
`run_universe_audit` und die CLI berühren das Netz (öffentliche CLOB-Endpunkte,
kein Key nötig).
"""

from __future__ import annotations

import logging
import time
from fractions import Fraction
from math import comb, erfc, sqrt

import requests

from polybot.reward_maker import (
    fetch_price_history,
    volatility_edge,
)
from polybot.rewards import RewardMarket, fetch_reward_markets

log = logging.getLogger(__name__)

# Vol-Schwellen für den Sweep (killt Risiko A: der Edge muss über eine Bandbreite
# halten, nicht nur auf der Messerschneide 0.002).
DEFAULT_THRESHOLDS = (0.0010, 0.0015, 0.0020, 0.0025, 0.0030, 0.0040)


def binom_sign_p(k: int, n: int) -> float:
    """Einseitiges P(X >= k) für X ~ Binomial(n, 0.5).

    Der Sign-Test gegen den Münzwurf: Ist der Anteil positiver OOS-Netti der
    Low-Vol-Selektion signifikant über der Hälfte? Exakt (Fraction, kein
    Float-Underflow) bis n<=1000, darüber normal-approximiert mit
    Stetigkeitskorrektur.
    """
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    if n <= 1000:
        total = sum(comb(n, j) for j in range(k, n + 1))
        return float(Fraction(total, 1 << n))
    mu = n / 2.0
    sd = sqrt(n) / 2.0
    z = (k - 0.5 - mu) / sd            # Stetigkeitskorrektur
    return 0.5 * erfc(z / sqrt(2.0))


def universe_edge(entries: list[dict],
                  thresholds=DEFAULT_THRESHOLDS) -> list[dict]:
    """Low-Vol-Edge über EINE Entry-Menge für mehrere Vol-Schwellen auswerten.

    Rein/deterministisch (baut nur auf der schon-sauberen `volatility_edge`).
    Je Schwelle: wie viele Märkte selektiert die IS-Vol-Regel, wie oft ist ihr
    OOS-Netto positiv (Sign-Test-p), und trennt sie im Mittel von den REST-
    Märkten (Separation = selected_mean − rest_mean). Positive Separation +
    kleines sign_p ÜBER DAS GESAMTUNIVERSUM = echter Edge, kein Survivorship.
    """
    rows = []
    for th in thresholds:
        ve = volatility_edge(entries, threshold=th)
        n, pos = ve["selected_n"], ve["selected_positive"]
        rest_n = ve["rest_n"]
        sel_mean = ve["selected_oos_sum"] / n if n else 0.0
        rest_mean = ve["rest_oos_sum"] / rest_n if rest_n else 0.0
        rows.append({
            "threshold": th,
            "selected_n": n,
            "selected_positive": pos,
            "selected_positive_rate": pos / n if n else 0.0,
            "selected_mean_oos": sel_mean,
            "rest_n": rest_n,
            "rest_mean_oos": rest_mean,
            "separation": sel_mean - rest_mean,
            "sign_p": binom_sign_p(pos, n),
        })
    return rows


def verdict(rows: list[dict], alpha: float = 0.05,
            min_selected: int = 20) -> dict:
    """Ehrliches Urteil über den Sweep: hält der Edge das Gesamtuniversum aus?

    Kriterium (bewusst streng): über die Schwellen mit ausreichend Selektion
    (>= min_selected) muss die Mehrheit sowohl positiv TRENNEN (separation>0)
    als auch den Sign-Test bestehen (sign_p < alpha). Sonst: nicht bestätigt.
    """
    usable = [r for r in rows if r["selected_n"] >= min_selected]
    if not usable:
        return {"survives": False, "reason": "zu wenige selektierte Märkte "
                "je Schwelle — Universum/Historie zu dünn für ein Urteil",
                "usable_thresholds": 0}
    sep_ok = sum(1 for r in usable if r["separation"] > 0)
    sig_ok = sum(1 for r in usable if r["sign_p"] < alpha)
    survives = sep_ok > len(usable) / 2 and sig_ok > len(usable) / 2
    return {
        "survives": survives,
        "usable_thresholds": len(usable),
        "separation_positive": sep_ok,
        "sign_significant": sig_ok,
        "reason": ("Low-Vol-Regel trennt OOS auch über das unvoreingenommene "
                   "Gesamtuniversum — Edge NICHT survivor-gepickt"
                   if survives else
                   "Über das Gesamtuniversum verschwindet die Trennung — der "
                   "12-Markt-Basket war wahrscheinlich cherry-gepickt"),
    }


def build_universe_entries(markets: list[RewardMarket],
                           session: requests.Session | None = None,
                           days: int = 14, fidelity: int = 5,
                           comp_floor: float = 500.0,
                           size: float | None = None,
                           min_history: int = 8,
                           max_markets: int | None = None,
                           sleep_s: float = 0.05,
                           end_ts: int | None = None) -> list[dict]:
    """Reward-Märkte in Backtest-Entries überführen (History je Up-Token ziehen).

    WICHTIG gegen Survivorship: die Märkte werden in API-Reihenfolge genommen
    (unvoreingenommen bzgl. unserer Edge), nicht nach Performance gefiltert.

    Vereinfachung ggü. backtest_watchlist: die Konkurrenz-Tiefe wird NICHT je
    Markt aus dem Live-Buch geholt (tausende Requests), sondern einheitlich auf
    `comp_floor` gesetzt. Das ist konservativ und — entscheidend — für Low-Vol-
    UND REST-Märkte GLEICH, verzerrt die zu testende Trennung also nicht.
    """
    http = session or requests.Session()
    entries: list[dict] = []
    for m in markets:
        hist = fetch_price_history(m.up_token, http, days=days,
                                   fidelity=fidelity, end_ts=end_ts)
        if sleep_s:
            time.sleep(sleep_s)
        if len(hist) < min_history:
            continue
        entries.append({
            "daily_rate": m.daily_rate,
            "band": (m.max_spread or 0.0) / 100.0,
            "comp": comp_floor,
            "size": size if size is not None else (m.min_size or 100.0),
            "tick": m.min_tick or 0.01,
            "history": hist,
            "label": m.slug or m.condition_id[:12],
        })
        if max_markets and len(entries) >= max_markets:
            break
    return entries


def run_universe_audit(days: int = 14, fidelity: int = 5,
                       size: float | None = None, comp_floor: float = 500.0,
                       thresholds=DEFAULT_THRESHOLDS,
                       max_markets: int | None = 400,
                       min_history: int = 8,
                       offset_days: float = 0.0,
                       session: requests.Session | None = None) -> dict:
    """Den vollständigen Audit fahren: Universum ziehen -> Entries -> Sweep -> Urteil.

    `offset_days` > 0 verschiebt das Fensterende in die Vergangenheit
    (Out-of-Time-Test gegen Regime-Glück): getestet wird dann das Fenster
    [jetzt−offset−days, jetzt−offset]. Ehrliche Einschränkung: das Universum
    stammt aus /sampling-markets von HEUTE — Märkte, die damals liefen und
    inzwischen zu sind, fehlen (leichte Überlebens-Verzerrung des Universums,
    nicht der Selektion; die Vol-Regel selbst bleibt Wall-sauber).
    """
    http = session or requests.Session()
    end_ts = int(time.time() - offset_days * 86400) if offset_days else None
    markets = fetch_reward_markets(http)
    log.info("Universum: %d reward-tragende Märkte gezogen", len(markets))
    entries = build_universe_entries(
        markets, http, days=days, fidelity=fidelity, comp_floor=comp_floor,
        size=size, min_history=min_history, max_markets=max_markets,
        end_ts=end_ts)
    log.info("%d Märkte mit ausreichender Historie -> Backtest", len(entries))
    rows = universe_edge(entries, thresholds)
    return {
        "universe_n": len(markets),
        "tested_n": len(entries),
        "offset_days": offset_days,
        "rows": rows,
        "verdict": verdict(rows),
    }


def _format_report(res: dict) -> str:
    lines = [
        "=" * 72,
        "FULL-UNIVERSE-AUDIT — Low-Vol-Reward-Edge (Survivorship-/Look-ahead-Kill)",
        "=" * 72,
        f"Universum (reward-tragend):   {res['universe_n']}",
        f"Getestet (genug Historie):    {res['tested_n']}",
        f"Fenster-Offset:               {res.get('offset_days', 0):.0f} Tage "
        f"{'(OUT-OF-TIME)' if res.get('offset_days') else '(aktuell)'}",
        "",
        f"{'Vol-Schwelle':>12} {'#sel':>5} {'pos%':>6} {'sel_OOS':>10} "
        f"{'rest_OOS':>10} {'Trennung':>10} {'sign_p':>9}",
        "-" * 72,
    ]
    for r in res["rows"]:
        lines.append(
            f"{r['threshold']:>12.4f} {r['selected_n']:>5d} "
            f"{r['selected_positive_rate']*100:>5.0f}% "
            f"{r['selected_mean_oos']:>10.4f} {r['rest_mean_oos']:>10.4f} "
            f"{r['separation']:>+10.4f} {r['sign_p']:>9.4f}")
    v = res["verdict"]
    lines += [
        "-" * 72,
        f"URTEIL: {'✅ EDGE HÄLT' if v['survives'] else '❌ EDGE FÄLLT'} "
        f"(nutzbare Schwellen: {v.get('usable_thresholds', 0)}, "
        f"Trennung>0: {v.get('separation_positive', 0)}, "
        f"sign_p<0.05: {v.get('sign_significant', 0)})",
        v["reason"],
        "=" * 72,
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--fidelity", type=int, default=5)
    ap.add_argument("--max-markets", type=int, default=400,
                    help="Obergrenze getesteter Märkte (unvoreingenommene "
                         "API-Reihenfolge; 0 = kein Limit)")
    ap.add_argument("--size", type=float, default=None,
                    help="Feste Quote-Größe (Default: min_size je Markt)")
    ap.add_argument("--offset-days", type=float, default=0.0,
                    help="Fensterende N Tage in die Vergangenheit schieben "
                         "(Out-of-Time-Test)")
    args = ap.parse_args(argv)
    res = run_universe_audit(days=args.days, fidelity=args.fidelity,
                             size=args.size,
                             max_markets=args.max_markets or None,
                             offset_days=args.offset_days)
    print(_format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
