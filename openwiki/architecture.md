# Architektur & Tick-Pipeline

## Gesamtbild

```
Gamma-API ──┐                                   ┌─> RiskManager.filter ──> Broker.execute
            ├─> build_snapshot ──> MarketSnapshot ┤        (risk.py)         (execution.py)
CLOB-REST ──┤   (main.py)          (strategies/    │
CLOB-WSS ───┘                       base.py)       └─> Recorder.observe (nur Messung)
                                        │
                                        └─> Strategien.generate ──> [Signal]
Nach der Ausführung: merge_positions / live_merge_positions (Kapitalrecycling),
OrphanFlattener (Waisen glattstellen), Ledger/Shadow (Messung), Kill-Switch-Check.
```

Alles läuft in **einem Prozess** (`polybot/main.py`), zwei Threads:

- **SnapshotWorker** (Thread, `main.py`): baut im Hintergrund alle
  `poll_interval_s` (live 90s) per REST einen vollen `MarketSnapshot`
  (Doppelpuffer; ein Refresh dauert live 40-50s).
- **stream_loop** (Hauptthread, `main.py`): tickt alle `stream_tick_s`
  (0.5s) gegen den letzten fertigen Snapshot, überlagert mit den live
  gestreamten WSS-Büchern (`data/stream.py`). Frischer Snapshot ⇒ voller
  Tick + Statuszeile + `portfolio.save()`.

## tick() — die eine Funktion, durch die alles läuft

`main.tick()` ist die Pipeline pro Iteration, Reihenfolge ist WICHTIG:

1. `risk.check_daily_loss` (VOR der Ausführung — im Breach-Tick gehen
   keine Orders mehr raus; wirft `KillSwitch`, stoppt den Bot).
2. `recorder.observe(snap)` — Beweisdaten sammeln, crasht nie den Tick.
3. Strategien erzeugen `Signal`s → `risk.filter` (Alles-oder-nichts pro
   Arb-Gruppe) → `broker.execute`.
4. `shadow.observe` (nur live): Paper-Schattenlauf derselben Signale für
   die Capture-Quote.
5. Merges: Paper `merge_positions`, live `live_merge_positions` (nur
   on-chain Bestätigtes wird gebucht; live aktuell per Config AUS).
6. `flattener.signals` + execute — Waisen-Glattstellung (umgeht
   `risk.filter` bewusst: SELLs reduzieren nur Risiko).
7. `risk.check_daily_loss` erneut, `ledger.record_tick`.

## Snapshot-Aufbau (`build_snapshot`)

- Vollmarkt-Scan (`scan_all_markets`): alle aktiven Binärmärkte über
  `min_liquidity_usdc`, danach clientseitig `min_volume_24h_usdc`.
- **`_market_ok`-Filter** (zwei Live-Lehren, nicht entfernen!):
  (a) endDate-Filter gegen Phantom-Arbitragen auf abgelaufenen Märkten
  (Befund 04.07.2026), (b) In-play-Matching-Delay-Filter gegen
  Cancel-Races/ungehedgte Beine (Befund 05.07.2026).
- Bücher zweistufig: Batch-Top-of-Book für ALLE Tokens, volle Bücher nur
  für Arb-Kandidaten + kurzlebige Märkte (`_load_books`,
  `_candidate_tokens`, `_expiring_tokens`) — sonst dauert ein Tick länger
  als das Poll-Intervall.
- NegRisk-Events gedeckelt (`max_negrisk_events/submarkets`).

## Invarianten (brechen = Geld verlieren)

- Ein halb ausgeführter Arb ist eine offene Wette: FOK-Gruppen sind
  atomar (Paper wie Live), gescheiterte Gruppen werden unwound
  (`LiveBroker._unwind_group`), Reste stellt der Waisen-Detektor glatt.
- Gebucht wird nur, was real gefüllt/bestätigt ist (Live: CLOB-Response
  bzw. on-chain Receipt) — die Buchhaltung folgt der Realität, nie der
  Absicht.
- Kein Tick darf durch Fehler eines Teilsystems sterben: Recorder,
  Shadow, Merges, Flattener, Streamer sind alle einzeln abgeschirmt.
- Paper und Live führen strikt getrennte State-Dateien
  (`paper_state.json` / `live_state.json`).
