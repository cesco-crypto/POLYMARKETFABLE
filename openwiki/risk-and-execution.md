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
- `_quantize_fok_groups` / `_marketable_size`: CLOB-Betrags-Präzision auf
  Size×Preis bei GLEICHER Stückzahl aller Beine. Die erlaubten Dezimalen
  hängen am Tick (`_amount_precision`, gespiegelt aus
  `py_clob_client_v2.ROUNDING_CONFIG`: Tick 0.01 → 4, 0.1 → 3, 0.001 → 5,
  usw.), NICHT hart 2/4 (Befund 07.07.: die alte 2-Dezimal-Annahme verwarf
  gültige preis-schiefe Arbs wie NO@0.897/Größe 6). Auf dem 0.01-Raster ist
  Size×Preis stets ≤ 4 Dezimalen → dort wird nie getrimmt; der Trimm bleibt
  nur defensiver Fallback (streng 2 Dez.) für unbekannte Ticks.
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

## `settlement.py` — Resolution-Sweeper (Kapital-Deadlock-Fix)

Bucht Positionen FINAL aufgelöster Märkte aus (closed + UMA resolved +
outcomePrices exakt 0/1 -> `Market.resolved_payouts`): Gewinner 1
USDC/Share, Verlierer 0. Gedrosselt (60s), Gamma-Token-Lookup übers
ganze Inventar, crasht nie den Tick. Ohne ihn wächst total_exposure()
monoton und der Risk-Manager blockt dauerhaft. Live-Grenze: Cash-
Gutschrift setzt Polymarket-Auto-Redeem voraus (Batch-Redeem = Ausbau).

## Paper-Fill-Ehrlichkeit: persistenter Liquiditätsverbrauch

`PaperBroker._consumed_levels` merkt je (Token, Seite, Preislevel), was
die Simulation konsumiert hat — dasselbe stehende Level füllt NICHT in
jedem Tick erneut (das war die 2-10x-PnL-Inflation). Verbrauch verfällt
erst, wenn das Level aus dem Buch verschwindet; Aufstocken gibt nur den
Zuwachs frei; FOK-Rollback gibt tentativen Verbrauch zurück; gilt auch
für Maker-Fills ruhender Quoten.

## Not-Aus: cancel_all

`LiveBroker.cancel_all_orders` läuft beim Prozessstart (Alt-Orders eines
abgestürzten Vorgängers) und in `cmd_run finally` (Kill-Switch/Ctrl-C/
Crash). Kein Zustand «Bot tot, Orders leben». Wirft nie; Fehlschlag wird
laut geloggt (dann von Hand auf polymarket.com prüfen).

## Tests

`tests/` (~400 Tests, pytest, keine Netzwerk-Calls — Fakes für Gamma/
Books/Clob). Vor JEDEM Deploy: `.venv/bin/python -m pytest tests/ -q`.
Konvention: Live-Befunde werden als Regressionstest mit Datum im
Docstring festgehalten (siehe `test_orphan.py`, `test_execution.py`).
