# Portfolio, Merges & Settlement

## `portfolio.py` — Buchhaltung (Paper UND Live)

- `Portfolio`: Cash, Positionen (Shares + Einstandskosten), Fills,
  `realized_pnl`, Fees/Rebates, ruhende Orders, Tages-PnL-Basis (UTC).
- `apply_fill`: verweigert Verkauf über Bestand (Phantom-Gewinn) und
  Kauf ohne Cash-Deckung (Polymarket kennt keine Margin) — beides
  ValueError, damit die Buchhaltung nie von der Realität abweicht.
- `value(marks)`: fehlender Mark ⇒ Einstandskosten + Warnung (gedrosselt
  auf 300s je Token — value() läuft im Stream-Betrieb sekündlich).
- Persistenz: atomar (`.tmp` + `os.replace`); Fill-Audit-Trail
  append-only nach `*_state.fills.jsonl`, im State nur die letzten 500.
- Merges (Paper-Pendant zum on-chain Merge): `merge_pairs` (YES+NO ⇒ 1),
  `merge_negrisk_yes` (Satz ⇒ 1), `merge_negrisk_no` (Satz ⇒ n−1) —
  realisieren Arbitragegewinn sofort statt bis zur Auflösung zu warten.

## `onchain.py` — MergeExecutor (Live-only)

Direkte web3-Contract-Calls (der py-clob-client-v2 hat keine Helfer):

- Binärpaar: `ConditionalTokens.mergePositions` (pUSD, Partition [1,2]).
- NegRisk-Paar: `NegRiskAdapter.mergePositions` (Adapter entpackt zu
  pUSD; braucht CTF-Operator-Freigabe, wird einmalig gesetzt/gecacht).
- NegRisk-NO-Satz: `NegRiskAdapter.convertPositions` (marketId =
  questionId mit genulltem letzten Byte, indexSet = Bitmaske).
  NegRisk-YES-Sätze sind on-chain NICHT mergebar — zahlen bei Auflösung.
- Konstruktor wirft ohne `POLY_PRIVATE_KEY`; alle Merge-Methoden werfen
  NIE (loggen + False). Shares werden ABGERUNDET (`_to_units`) — lieber
  ein Millionstel liegen lassen als ein Revert.
- Gebucht (in `main.live_merge_positions`) wird NUR, was on-chain per
  Receipt bestätigt ist.
- V2-Adressen (Polygon, seit 28.04.2026) stehen als Konstanten im Modul.

## `settlement.py` — Resolution-Sweeper

Siehe [risk-and-execution](risk-and-execution.md): bucht final
aufgelöste Positionen aus (`Portfolio.settle_position`); Ledger-Eintrag
`kind=settlement`; aktiv in paper UND live.

## Deposit-Wallet-Flow & Preflight

- `deposit_wallet.py`: Polymarket-V2-Konten halten pUSD/Tokens im
  Deposit-Wallet (Smart Contract), nicht im EOA. Orders laufen als
  POLY_1271 (`POLY_SIGNATURE_TYPE=3`, `POLY_FUNDER_ADDRESS`).
- WICHTIG: Solange Token im Deposit-Wallet liegen, schlagen EOA-Merges
  fehl ⇒ `live_auto_merge: false` in `config.live.yaml`. Vollständige
  Paare realisieren sich stattdessen bei Marktauflösung (Redeem/Claim
  auf polymarket.com) — Gewinn bleibt real, nur später liquide.
- `preflight.py` (`python -m polybot.main preflight [--execute]`):
  prüft vor dem Livegang Guthaben (pUSD, POL für Gas), Approvals und
  Wallet-Konfiguration; ohne `--execute` nur Plan-Ausgabe.

## State-Dateien (git-ignoriert)

| Datei | Inhalt |
|---|---|
| `paper_state.json` / `live_state.json` | Portfolio-State (strikt getrennt!) |
| `*_state.fills.jsonl` | Fill-Audit-Trail, append-only |
| `data/opportunities.jsonl` | Recorder-Beweisdaten |
| `data/pnl_ledger.jsonl` | prozessübergreifender PnL-Ledger |
| `data/shadow.jsonl` | Live/Paper-Schattenvergleich (Capture) |
