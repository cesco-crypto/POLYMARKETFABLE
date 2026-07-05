# Strategien (`polybot/strategies/`)

## Vertrag

`Strategy.generate(snap: MarketSnapshot) -> list[Signal]` — pure
Funktion über dem Snapshot, KEINE Seiteneffekte, KEINE Netzwerk-Calls.
`Signal`-Felder, die die Execution steuern:

- `group`: Signale derselben Arb-Gruppe sind atomar (alle Beine oder
  keines — Risk-Manager UND Broker erzwingen das).
- `neg_risk`: Markt läuft über den NegRisk-Exchange (andere
  Signatur-Domain beim LiveBroker).
- `replace`: eigene ruhende Orders des Tokens vorher canceln (MM-Quotes).
- `expected_edge`: erwarteter Gewinn pro Share-Bündel (für Messung).

Registry: `strategies/__init__.py::REGISTRY` — Aktivierung über
`strategy.enabled` in der Config. Live Phase 1: NUR `complement_arb`.

## `complement_arb.py` — die validierte Kernstrategie

YES-Ask + NO-Ask < 1 − Fees − `min_edge` ⇒ beide Beine als FOK-Gruppe
kaufen; der Merge (1 USDC pro Paar) realisiert den Gewinn sofort.
Tiefenbewusst: Edge wird über die tatsächlich kaufbare Buchtiefe
gerechnet, nicht über Top-of-Book allein.

## `negrisk_arb.py` — Event-Arbitrage (live erst Phase 2)

Zwei Formen je NegRisk-Event: Summe aller YES-Asks < 1, oder Summe
aller NO-Asks < (n−1). Verwirft Events mit fehlendem Buch oder
`neg_risk_augmented` (nachträgliche Outcomes ⇒ YES-Satz zahlt nicht
garantiert 1).

## `market_making.py` — Quotes statt Taker (live erst Phase 3)

Quotet YES-Tokens der Top-N-Kandidaten (`mm_candidate_markets`) beidseitig
innerhalb des Spreads; Inventar-Steuerung über das Portfolio im Snapshot.
ACHTUNG: bewusst EINBEINIGES Inventar — deshalb ist die Kombination mit
dem Waisen-Detektor per Config-Validierung verboten
(`flatten_orphan_grace_s` + `market_making` ⇒ SystemExit).
Paper-Maker-Fills sind systematisch optimistisch (keine Queue-Position) —
MM-Paper-PnL nie unbereinigt berichten.

## `implication_detector.py` — reine Beobachtung

Erkennt Implikationsstrukturen zwischen Märkten per Titel-Parsing
(z.B. «X gewinnt Turnier» ⇒ «X erreicht Finale») und misst Verletzungen.
Erzeugt KEINE Signale — Messung vor Aktivierung (Prinzip: erst Recorder,
dann Trading).

## Neue Strategie hinzufügen — Pflichtweg

1. Zuerst als Recorder-Messung (`recorder.py`, kind-Feld) beobachten.
2. Messdaten auswerten (Rate, Lebensdauer, Tiefe — `report`).
3. Erst dann als Strategy-Klasse + `REGISTRY`-Eintrag + Tests bauen.
4. In Paper-Config aktivieren, NIE direkt live.
