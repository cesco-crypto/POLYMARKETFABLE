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
- `updown.py` — UpDownRecorder (RISIKOFREI, handelt nicht): vermisst den
  Spät-Fenster-Latenz-Edge der 5-Min BTC/ETH/SOL-Up/Down-Märkte. Diese
  Märkte lösen nach dem **Chainlink**-Data-Stream auf (nicht Binance/
  Coinbase-Spot — steht wörtlich in der Marktbeschreibung). Der Recorder
  loggt Coinbase-Spot (handelbarer Proxy) gegen das Polymarket-Orderbuch
  nach `data/updown.jsonl` und trägt nach Fensterschluss das echte
  Ergebnis aus Gamma `outcomePrices` nach. Slug-Muster
  `{btc,eth,sol}-updown-5m-<fensterstart_unix>`, Ergebnis = Up, wenn der
  Chainlink-Preis am Ende ≥ am Start. Start: `updown-record`,
  Auswertung: `updown-report [--fee-rate 0.07]`. Der Report bucketet nach
  Sekunden-vor-Schluss und zeigt je Bucket: Proxy-Trefferquote, Ø Ask,
  Ø Tiefe, Anteil «Buch führt schon den Sieger» und die realisierte
  Ø Rendite/Share (>0 ⇒ dort war es +EV, die vorhergesagte Seite zu
  kaufen — vor Slippage/Order-Latenz). **Proxy_acc < 100% = Coinbase-vs-
  Chainlink-Divergenz = unser Risiko**, explizit ausgewiesen. Kontext:
  Forensik der Wallet followsmartwallet (06.07.2026), siehe REPORT.md.

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
7. **Proxy statt Wahrheit** (Up/Down-Recorder): Coinbase-Spot ist NICHT
   die Auflösungsquelle (Chainlink). Ein +EV im updown-report gilt nur,
   solange proxy_acc die Divergenz mitträgt; zusätzlich ist der Ask ohne
   Tiefe (Ø Tiefe) und ohne Order-Latenz eine Obergrenze, kein Realwert.

## Ergebnis-Pfad

Erkenntnisse gehören nach `REPORT.md` (committen + pushen — Container
sind flüchtig). Messdaten (`data/`, `*_state.json`) sind git-ignoriert.
Stufenplan und aktueller Stand: /CLAUDE.md («Mission»).
