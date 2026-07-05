# Risk & Execution

**Härteste Regel des Repos:** Keine Änderung an Execution, Wallet,
Order-Routing, Risk-Limits oder Live-Trading ohne vorherige Erklärung,
Tests und Bestätigung des Betreibers (/CLAUDE.md).

## `risk.py` — RiskManager

- `filter(signals, portfolio)`: Alles-oder-nichts pro Arb-Gruppe.
  Gruppenlimits über die SUMME der Beine; Limits: `max_order_usdc`,
  `max_position_usdc`, `max_total_exposure_usdc`, Cash-Deckung,
  kein Shorting (SELL nur gegen Bestand), Preis in [0.001, 0.999].
- `check_daily_loss(portfolio, marks)`: Kill-Switch. Mit Marks zählen
  auch UNREALISIERTE Verluste; wirft `KillSwitch` ⇒ Bot stoppt sofort.
  Läuft in `tick()` VOR und NACH der Ausführung.

## `execution.py` — PaperBroker

Simuliert Marketable-Limit-Orders gegen den Book-Snapshot:

- Fills Level für Level (volumengewichtet), Taker-Fee mitverbucht;
  Signale desselben Ticks teilen sich die Buchliquidität (`consumed`).
- FOK-Gruppen atomar inkl. Rollback (`fail_group`).
- Ruhende GTC-Orders (`RestingOrder`, persistiert): füllen konservativ
  erst, wenn der Markt den Orderpreis DURCHSCHREITET — als Maker
  (Gebühr 0, optional Rebate). Cash ruhender BUYs ist reserviert.
- `paper_fill_delay_ticks`: ehrliche Latenz — Signale füllen erst n
  execute()-Aufrufe später gegen das DANN aktuelle Buch.

## `execution.py` — LiveBroker (py-clob-client-v2)

Exchange-Upgrade 28.04.2026: V2, pUSD statt USDC.e — v1-Client ist
inkompatibel. Deposit-Wallet-Flow: `POLY_SIGNATURE_TYPE=3` (POLY_1271),
maker/funder = Smart-Contract-Wallet, signiert mit dem EOA-Key.

- Arb-Beine als FOK; scheitert ein Bein, werden restliche Beine der
  Gruppe übersprungen und gefüllte Beine per FAK-Gegenorder unwound.
- Gebucht wird NUR laut CLOB Gematchtes (`size_matched`), nie die
  Signalgröße auf Verdacht.
- Delayed Orders (In-play-Matching-Delay): 15s-Poll-Fenster
  (`DELAY_POLL_*`) — ein Cancel verliert sonst das Race gegen das
  Matching (Lehre vom ersten Live-Trade 05.07.).
- `_quantize_fok_groups`: CLOB-Market-Order-Präzision (BUY 2, SELL 4
  Nachkommastellen auf Size×Preis) bei GLEICHER Stückzahl aller Beine.
- Reject-Cooldown je Token (`order_reject_cooldown_s`); fatale
  Konfig-Rejects («maker address not allowed») sperren bis Prozessende.
- GTC-Reste werden über `_reconcile_pending` zu Tick-Beginn nachgebucht.
- `merger` (onchain.MergeExecutor) nur bei `live_auto_merge: true`.

## `orphan.py` — Waisen-Detektor (Auto-Glattstellung)

Regel: Der Bot hält KEINE ungehedgte Position (Richtungsrisiko ist
nicht unser Geschäft). Aktivierung: `risk.flatten_orphan_grace_s > 0`.

- Lernt YES/NO-Paare kumulativ über ALLE je gesehenen Snapshots — die
  Märkte, auf denen Waisen entstehen (in-play, abgelaufen), fliegen aus
  dem nächsten Snapshot; Nur-Snapshot-Sicht wäre blind.
- Altbestände ohne Paar-Wissen: Gamma-Token-Lookup (`markets_by_tokens`),
  gedrosselt (300s Retry).
- Überhang > Schonfrist (60s live, > 15s-Poll-Fenster) ⇒ SELL zum besten
  Bid, `replace=True`; Bid off-Snapshot per Batch-Preisabfrage.
  Ohne Bid: beobachten, nicht raten. Staub < 1 USDC bleibt liegen.
- Ausgenommen: vollständige Paare, NegRisk-Tokens (Phase 2), Tokens mit
  eigenen ruhenden Orders (MM-Inventar).

## Tests

`tests/` (~400 Tests, pytest, keine Netzwerk-Calls — Fakes für Gamma/
Books/Clob). Vor JEDEM Deploy: `.venv/bin/python -m pytest tests/ -q`.
Konvention: Live-Befunde werden als Regressionstest mit Datum im
Docstring festgehalten (siehe `test_orphan.py`, `test_execution.py`).
