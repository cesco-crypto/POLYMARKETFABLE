# Daten-Schicht (`polybot/data/`)

Alle Clients sind key-los (öffentliche Endpunkte) und dürfen NIE eine
Exception in den Bot-Loop werfen — Fehler werden geloggt und als
None/leer gemeldet.

## `gamma.py` — Marktmetadaten (Gamma-API)

- `Market` (dataclass): das zentrale Marktobjekt. Wichtige Felder:
  `yes_token`/`no_token` (CLOB-Token-IDs), `condition_id` (on-chain),
  `question_id` (NegRisk-Convert), `neg_risk`, `fee_rate`, `end_ts`,
  `seconds_delay` + `game_start_ts` (In-play-Matching-Delay).
- `Market.tradeable(min_time_to_end_s)`: endDate-Filter — Märkte behalten
  nach Ablauf `closed=False` und ein STALES Buch (Phantom-Arbitragen!).
- `Market.inplay_delayed()`: Spiel läuft UND Matching-Delay aktiv —
  für Taker-Strategien Fata Morgana (Cancel-Races).
- `all_active_markets()`: Vollmarkt-Scan. Achtung API-Eigenheiten:
  max. 100 Zeilen/Request, Offsets > ~2000 ⇒ HTTP 422, deshalb
  Fenster-Pagination über Liquidität; Dedup nach `condition_id`.
- `negrisk_events()`: Events, in denen genau ein Outcome gewinnt.
- `markets_by_tokens(token_ids)`: Rückwärts-Lookup Token → Markt, auch
  für GESCHLOSSENE Märkte (zwei Abfragen: ohne und mit `closed=true` —
  ohne den Parameter liefert Gamma nur offene). Nutzer: Waisen-Detektor.
- `_get`: Retry/Backoff für 429/5xx/Timeout (Gamma ist rate-limitiert).

## `orderbook.py` — CLOB-Bücher (REST)

- `OrderBook` mit sortierten `Level`-Listen, `best_bid/best_ask/midpoint`.
- `BookClient.get_top_prices` (POST /prices, 200 Tokens/Batch) und
  `get_books` (POST /books, 50 Tokens/Batch). `main._load_books`
  kombiniert beides zweistufig; Nicht-Kandidaten bekommen synthetische
  Top-of-Book-Bücher mit **Größe 0** (Strategien verwerfen sie über die
  Mindestgröße, Marks/Kill-Switch sehen trotzdem Midpoints).

## `stream.py` — WSS-Streaming

- `BookStreamer`: WebSocket-Abo auf max. `stream_max_tokens` Tokens,
  liefert `get_books()` als Overlay über den REST-Snapshot. Kandidaten
  priorisiert `main.stream_tokens` (bald endende Märkte zuerst).
- Streaming ist OPTIONAL: scheitert der Start, läuft der Bot im reinen
  REST-Betrieb weiter.

## `fees.py` — Taker-Fee-Raten

- Seit März 2026: `fee = rate * p * (1-p)` pro Share, kategorieabhängig
  0.00-0.07. Quelle ist das Gamma-Marktobjekt (CLOB `/fee-rate` liefert
  keine Kategorierate). `FeeRateCache` hält Raten über Ticks; unbekannte
  Tokens fallen auf `cfg.risk.taker_fee_rate` (Maximum 0.07) zurück,
  damit eine unbekannte Rate die Edge nie überschätzt.
