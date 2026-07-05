"""Positions- und PnL-Verfolgung, persistiert als JSON (Paper-Modus)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Fill-Historie: vollständiger Audit-Trail landet append-only in einer
# JSONL-Datei; im RAM und im State-JSON werden nur die letzten N gehalten.
MAX_FILLS_IN_STATE = 500
# "Kein Mark"-Warnung höchstens alle so viele Sekunden je Token wiederholen.
MARK_WARN_INTERVAL_S = 300.0


def _utc_today() -> str:
    """Aktuelles UTC-Kalenderdatum als ISO-String (Tagesgrenze für PnL)."""
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Position:
    token_id: str
    question: str = ""
    shares: float = 0.0
    cost_basis: float = 0.0  # gezahlte USDC


@dataclass
class Fill:
    ts: float
    token_id: str
    side: str
    price: float
    size: float
    reason: str
    fee: float = 0.0  # Taker-Gebühr in USDC (Maker zahlen 0)


@dataclass
class RestingOrder:
    """Ruhende (nicht-marketable) Paper-Order — Simulation einer GTC-Quote.

    Wird vom PaperBroker gegen jeden neuen Book-Snapshot geprüft und füllt
    konservativ erst, wenn der Markt den Orderpreis durchschreitet
    (Fill als Maker zum Orderpreis, Gebühr 0).
    """

    ts: float
    token_id: str
    side: str                 # "BUY" | "SELL"
    price: float
    size: float               # noch offene Shares
    reason: str
    market_question: str = ""


@dataclass
class Portfolio:
    cash: float = 1000.0  # Start-Cash im Paper-Modus (USDC)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0  # kumulierte Taker-Gebühren in USDC (bereits im Cash abgezogen)
    rebates_earned: float = 0.0  # kumulierte Maker-Rebates in USDC (bereits im Cash enthalten)
    # Ruhende Paper-Orders (GTC-Quotes) — persistiert, damit sie einen
    # Neustart überleben; das Cash ruhender BUYs gilt als reserviert.
    resting_orders: list[RestingOrder] = field(default_factory=list)
    # Bereits GESETTELTE Tokens (Token -> Settlement-Zeitpunkt), persistiert:
    # Schutz gegen Doppel-Settlement nach Neustart (Verifikations-Befund 2/20:
    # der Positions-Sync sähe die noch nicht redeemten Chain-Tokens sonst als
    # "fehlend", trüge sie neu ein, und der Sweeper zahlte ERNEUT aus —
    # Phantom-Cash bei jedem 90-Min-Neustart). Einträge werden vom Sync
    # entfernt, sobald die Chain den Token nicht mehr führt (Redeem durch).
    settled: dict[str, float] = field(default_factory=dict)
    day_start_date: str = field(default_factory=_utc_today)  # UTC-Kalendertag
    # None = "beim Start setzen": wird in __post_init__ an den tatsächlichen
    # Startwert gekoppelt, damit Portfolio(cash=X) nicht sofort PnL zeigt.
    day_start_value: float | None = None
    # Wie viele Einträge aus self.fills schon in die JSONL-Datei geschrieben
    # wurden (nicht persistiert, nur für save()).
    _fills_flushed: int = field(default=0, repr=False)
    # Drossel für die "Kein Mark"-Warnung je Token (nicht persistiert):
    # value() läuft im Stream-Betrieb mehrmals pro Sekunde — ohne Drossel
    # flutet ein einziger markloser Altbestand das Log (Befund 05.07.2026).
    _mark_warned: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.day_start_value is None:
            self.day_start_value = self.value()

    def exposure(self, token_id: str) -> float:
        pos = self.positions.get(token_id)
        return pos.cost_basis if pos else 0.0

    def total_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    @property
    def reserved_cash(self) -> float:
        """Durch ruhende BUY-Orders gebundenes Cash (Orderpreis * Restgröße).

        Maker zahlen keine Gebühr — reserviert wird genau der Kaufpreis.
        Das Cash bleibt in self.cash (Fills buchen normal dagegen), steht
        aber neuen Sofort-Orders nicht mehr zur Verfügung.
        """
        return sum(o.price * o.size for o in self.resting_orders if o.side == "BUY")

    def credit_rebate(self, amount: float) -> None:
        """Maker-Rebate gutschreiben: Cash, realisierter PnL und Ausweis."""
        if amount <= 0:
            return
        self.cash += amount
        self.realized_pnl += amount
        self.rebates_earned += amount

    def apply_fill(self, fill: Fill, force: bool = False) -> None:
        """Fill buchen. force=True nur für BÖRSEN-BESTÄTIGTE Live-Fills:
        Chain-Realität schlägt Buchhaltungs-Guards — SELL wird am Bestand
        gekappt (Überhang wurde extern erworben/verkauft, das klärt der
        Positions-Sync), BUY darf das Cash ins Minus ziehen (lauter
        Log-Hinweis; die Börse hätte ohne echte Deckung nie gematcht)."""
        pos = self.positions.setdefault(fill.token_id, Position(token_id=fill.token_id))
        if force and fill.side != "BUY" and fill.size > pos.shares + 1e-9:
            log.error("Forcierter SELL %.2f > Bestand %.2f (%s) — am Bestand "
                      "gekappt, Positions-Sync gleicht ab", fill.size,
                      pos.shares, fill.token_id[:16])
            fill = Fill(ts=fill.ts, token_id=fill.token_id, side=fill.side,
                        price=fill.price, size=pos.shares, reason=fill.reason,
                        fee=fill.fee)
            if fill.size <= 1e-9:
                self.positions.pop(fill.token_id, None)
                return
        if fill.side != "BUY" and fill.size > pos.shares + 1e-9:
            # Verkauf über den Bestand hinaus würde Phantom-Gewinn buchen
            # (avg_cost=0) und die negative Position still verwerfen.
            if pos.shares <= 1e-9:
                self.positions.pop(fill.token_id, None)
            raise ValueError(
                f"Verkauf über Bestand: {fill.size:.2f} > {pos.shares:.2f} Shares "
                f"({fill.token_id[:16]})"
            )
        if fill.side == "BUY":
            cost = fill.price * fill.size + fill.fee
            if force and cost > self.cash + 1e-6:
                log.error("Forcierter BUY zieht Cash ins Minus (%.2f -> %.2f) "
                          "— Chain-Fill gebucht, Buchhaltung driftet, "
                          "Positions-Sync/Balance-Check beachten",
                          self.cash, self.cash - cost)
            elif cost > self.cash + 1e-6:
                # Polymarket kennt keine Margin — ein BUY ohne Deckung würde
                # die Paper-Buchhaltung von der Realität entkoppeln.
                if pos.shares <= 1e-9:
                    self.positions.pop(fill.token_id, None)
                raise ValueError(
                    f"Unzureichendes Cash: Kauf kostet {cost:.2f} USDC, "
                    f"verfügbar {self.cash:.2f} ({fill.token_id[:16]})"
                )
        self.fills.append(fill)
        if fill.side == "BUY":
            pos.shares += fill.size
            pos.cost_basis += fill.price * fill.size
            self.cash -= fill.price * fill.size + fill.fee
        else:
            avg_cost = pos.cost_basis / pos.shares if pos.shares > 0 else 0.0
            sold_cost = avg_cost * fill.size
            self.realized_pnl += fill.price * fill.size - sold_cost
            pos.shares -= fill.size
            pos.cost_basis -= sold_cost
            self.cash += fill.price * fill.size - fill.fee
        # Gebühren sind in jedem Fall realisierte Kosten
        self.realized_pnl -= fill.fee
        self.fees_paid += fill.fee
        if pos.shares <= 1e-9:
            self.positions.pop(fill.token_id, None)

    def settle_position(self, token_id: str, payout_per_share: float,
                        question: str = "") -> float:
        """Position eines AUFGELÖSTEN Markts zur finalen Auszahlung ausbuchen.

        Kapital-Deadlock-Fix (Befund Agenten-Flotte 05.07.2026): Ohne
        Settlement wächst total_exposure() monoton und der Risk-Manager
        blockt nach ~7-8 Paaren dauerhaft. Gewinner zahlen 1 USDC/Share,
        Verlierer 0 — beides wird ehrlich realisiert (ein wertloses Bein
        ist ein realisierter Verlust, kein ewiger Bestand).

        Rückgabe: ausgezahlte USDC (0.0 wenn Position nicht existiert).
        """
        pos = self.positions.get(token_id)
        self.settled[token_id] = datetime.now(timezone.utc).timestamp()
        # Ruhende Orders auf dem aufgelösten Markt sind tot — entfernen,
        # sonst bleibt ihre Cash-Reservierung für immer gebunden
        # (Verifikations-Befund 3/22: schleichender Rest-Deadlock).
        before_resting = len(self.resting_orders)
        self.resting_orders = [o for o in self.resting_orders
                               if o.token_id != token_id]
        if len(self.resting_orders) != before_resting:
            log.info("Settlement: %d ruhende Order(s) auf %s entfernt",
                     before_resting - len(self.resting_orders), token_id[:16])
        if pos is None or pos.shares <= 1e-9:
            return 0.0
        payout = pos.shares * payout_per_share
        pnl = payout - pos.cost_basis
        self.cash += payout
        self.realized_pnl += pnl
        log.info("Settlement: %.2f Shares %s (%s) zu %.0f%% -> %+.2f USDC "
                 "(PnL %+.2f)", pos.shares, token_id[:16], question[:50],
                 payout_per_share * 100, payout, pnl)
        self.positions.pop(token_id, None)
        return payout

    def force_set_position(self, token_id: str, shares: float,
                           avg_price: float, question: str = "") -> None:
        """Position hart auf die Chain-Wahrheit setzen (Positions-Sync).

        Bewusst OHNE PnL-/Cash-Buchung: externe Veränderungen (Fills
        während Downtime, manuelle Verkäufe/Claims des Betreibers) sind
        kein Bot-PnL — aber Exposure/Waisen/Settlement müssen mit der
        echten Position rechnen. Jede Korrektur wird laut geloggt.
        """
        old = self.positions.get(token_id)
        old_shares = old.shares if old else 0.0
        if shares <= 1e-9:
            self.positions.pop(token_id, None)
        else:
            self.positions[token_id] = Position(
                token_id=token_id, question=question, shares=shares,
                cost_basis=shares * avg_price)
        log.warning("Positions-Sync: %s (%s) %.2f -> %.2f Shares "
                    "(extern verändert — kein Bot-PnL gebucht)",
                    token_id[:16], question[:40], old_shares, shares)

    # ---- Merge zu USDC (Paper-Pendant zum on-chain CTF-Merge) --------------

    def _consume_shares(self, token_id: str, shares: float) -> float:
        """Shares aus einer Position entnehmen; Rückgabe: anteilige Einstandskosten."""
        pos = self.positions[token_id]
        avg_cost = pos.cost_basis / pos.shares if pos.shares > 0 else 0.0
        cost = avg_cost * shares
        pos.shares -= shares
        pos.cost_basis -= cost
        if pos.shares <= 1e-9:
            self.positions.pop(token_id, None)
        return cost

    def _merge_sets(self, token_ids: list[str], payout_per_set: float, kind: str) -> float:
        """Vollständige Sets (1 Share je Token) gegen payout_per_set USDC vernichten.

        Realisiert den Arbitragegewinn sofort: payout minus anteilige
        Einstandskosten aller Beine wandert in realized_pnl, der Payout in
        Cash. Rückgabe: Anzahl gemergter Sets (0.0, wenn ein Bein fehlt).
        """
        if not token_ids or len(set(token_ids)) != len(token_ids):
            return 0.0
        sets = min(
            (self.positions[t].shares if t in self.positions else 0.0)
            for t in token_ids
        )
        if sets <= 1e-9:
            return 0.0
        cost = sum(self._consume_shares(t, sets) for t in token_ids)
        payout = sets * payout_per_set
        self.cash += payout
        self.realized_pnl += payout - cost
        log.info("Merge (%s): %.2f Sets -> %.2f USDC (Einstand %.2f, PnL %+.2f)",
                 kind, sets, payout, cost, payout - cost)
        return sets

    def merge_pairs(self, yes_token: str, no_token: str) -> float:
        """YES/NO-Paare eines binären Markts zu je 1 USDC mergen (CTF-Merge).

        Vernichtet min(shares_yes, shares_no) Paare; Rückgabe: Anzahl Paare.
        """
        return self._merge_sets([yes_token, no_token], 1.0, "Komplement")

    def merge_negrisk_yes(self, yes_tokens: list[str]) -> float:
        """Vollständige YES-Sets eines NegRisk-Events zu je 1 USDC mergen.

        Genau ein Outcome gewinnt -> ein Set aus allen YES zahlt sicher 1 USDC.
        """
        return self._merge_sets(list(yes_tokens), 1.0, "NegRisk-YES")

    def merge_negrisk_no(self, no_tokens: list[str], n: int) -> float:
        """Vollständige NO-Sets eines NegRisk-Events mit n Outcomes mergen.

        Alle NO außer dem des Gewinners zahlen aus -> (n-1) USDC pro Set.
        """
        return self._merge_sets(list(no_tokens), float(n - 1), "NegRisk-NO")

    def value(self, marks: dict[str, float] | None = None) -> float:
        """Cash + Positionen (zu Marktpreisen, sonst zu Einstandskosten)."""
        v = self.cash
        for p in self.positions.values():
            if marks is None:
                v += p.cost_basis
            elif p.token_id in marks:
                v += p.shares * marks[p.token_id]
            else:
                # Fehlender Mark (Book-Fetch gescheitert, Markt delistet):
                # Fallback auf Einstandskosten schönt den Wert — warnen,
                # aber gedrosselt (max. alle MARK_WARN_INTERVAL_S je Token).
                now = datetime.now(timezone.utc).timestamp()
                if now - self._mark_warned.get(p.token_id, 0.0) \
                        >= MARK_WARN_INTERVAL_S:
                    self._mark_warned[p.token_id] = now
                    log.warning("Kein Mark für %s — bewerte zu Einstandskosten "
                                "(%.2f USDC)", p.token_id[:16], p.cost_basis)
                v += p.cost_basis
        return v

    def daily_pnl(self, marks: dict[str, float] | None = None) -> float:
        # neuer Kalendertag (UTC) -> Basis zurücksetzen
        v = self.value(marks)
        today = _utc_today()
        if today != self.day_start_date:
            self.day_start_date = today
            self.day_start_value = v
        return v - self.day_start_value

    # ---- Persistenz -------------------------------------------------------

    def save(self, path: str | Path = "paper_state.json") -> None:
        path = Path(path)
        # Audit-Trail: neue Fills append-only in eine JSONL-Datei schreiben,
        # bevor die In-Memory-Historie gekappt wird.
        new_fills = self.fills[self._fills_flushed:]
        if new_fills:
            with path.with_suffix(".fills.jsonl").open("a") as fh:
                for f in new_fills:
                    fh.write(json.dumps(asdict(f)) + "\n")
        if len(self.fills) > MAX_FILLS_IN_STATE:
            self.fills = self.fills[-MAX_FILLS_IN_STATE:]
        self._fills_flushed = len(self.fills)

        data = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "rebates_earned": self.rebates_earned,
            "day_start_date": self.day_start_date,
            "day_start_value": self.day_start_value,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "settled": dict(self.settled),
            "fills": [asdict(f) for f in self.fills],
            "resting_orders": [asdict(o) for o in self.resting_orders],
        }
        # Atomar schreiben: erst in Temp-Datei, dann os.replace — ein Crash
        # mitten im Schreiben darf den State nicht zerstören.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str | Path = "paper_state.json", start_cash: float = 1000.0) -> "Portfolio":
        p = Path(path)
        if not p.exists():
            return cls(cash=start_cash, day_start_value=start_cash)
        data = json.loads(p.read_text())
        day_start_date = data.get("day_start_date")
        if not day_start_date:
            # Altes State-Format: Timestamp -> UTC-Datum umrechnen
            ts = data.get("day_start_ts")
            day_start_date = (
                datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                if ts else _utc_today()
            )
        pf = cls(
            cash=data["cash"],
            realized_pnl=data.get("realized_pnl", 0.0),
            fees_paid=data.get("fees_paid", 0.0),  # alte States: 0.0
            rebates_earned=data.get("rebates_earned", 0.0),  # alte States: 0.0
            day_start_date=day_start_date,
            day_start_value=data.get("day_start_value", start_cash),
        )
        pf.positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        pf.settled = dict(data.get("settled", {}))
        pf.fills = [Fill(**f) for f in data.get("fills", [])]
        pf.resting_orders = [RestingOrder(**o) for o in data.get("resting_orders", [])]
        pf._fills_flushed = len(pf.fills)  # geladene Fills stehen schon auf Disk
        return pf
