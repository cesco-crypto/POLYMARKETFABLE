# Messung & Reporting

Paper-Trading ist das MESSINSTRUMENT auf dem Weg zu echtem Ertrag,
nicht das Ziel. Prinzip: Ehrlichkeit vor Schönfärberei — jede Zahl
braucht die Gegenfrage «Wie könnte diese Zahl lügen?» (/CLAUDE.md).

## Instrumente

- `recorder.py` — OpportunityRecorder: protokolliert JEDE beobachtete
  (Fast-)Arbitrage nach `data/opportunities.jsonl`, mit kind
  (`complement`, `negrisk_*`, `implication`…), gross/fees/depth/ts.
  Neue Strategie-Ideen werden ZUERST hier gemessen, dann gebaut.
- `cycle_report.py` — CycleLedger: prozessübergreifender PnL-Ledger
  (`data/pnl_ledger.jsonl`); der Messbot startet alle 90min frisch,
  Fenster-Metriken brauchen deshalb Disk-State.
  Auswertung: `python -m polybot.main cycle-report`.
- `shadow.py` — ShadowTracker (nur Live-Modus): misst die
  **Capture-Quote** = Live-Fill / Paper-Fill pro GELEGENHEITS-EPISODE
  (nicht pro Tick — das war ~60x aufgeblasen): Episode = Gruppen-ID,
  endet nach 30s Stille bzw. 600s hart; paper_fill = grösste
  Einzel-Tick-Füllung, live_fill = Episodensumme; flush() bei
  Prozessende. Auswertung: `capture-report`.
- `report` (`cmd_report`): Hochrechnung auf USDC/Tag + Kapitalfrage.

## Bekannte Lügen der Zahlen (Checkliste vor jedem Bericht)

1. **Phantom-Arbitragen** (04.07.2026): abgelaufene Märkte behalten
   stale Bücher ⇒ endDate-Filter; Validierung gegen echte Trade-Prints
   der Data-API.
2. **In-play-Fata-Morgana** (05.07.2026): ~70% des theoretischen
   Komplement-Profits lag auf Märkten mit Matching-Delay — für Taker
   nicht einnehmbar ⇒ In-play-Filter (`Market.inplay_delayed`).
3. **Paper-Fills sind optimistisch**: Konkurrenz greift dieselben Cents
   zuerst; Latenz (`paper_fill_delay_ticks`) mildert, ersetzt aber keine
   gemessene Capture-Quote.
4. **MM-Maker-Fills**: Paper kennt keine Queue-Position — füllt immer,
   wenn der Markt die Quote durchschreitet. MM-PnL nie unbereinigt
   berichten.
5. **Zeitfenster-Bias**: Sport-Abende sind Ausreisser nach oben; erst
   mehrere Tage über verschiedene Tageszeiten ergeben eine Tagesrate.
6. **Ein Markt dominiert**: Top-1/Top-3-Profitanteil prüfen
   (cycle-report) — konzentrierter Gewinn ist fragil.

## Ergebnis-Pfad

Erkenntnisse gehören nach `REPORT.md` (committen + pushen — Container
sind flüchtig). Messdaten (`data/`, `*_state.json`) sind git-ignoriert.
Stufenplan und aktueller Stand: /CLAUDE.md («Mission»).
