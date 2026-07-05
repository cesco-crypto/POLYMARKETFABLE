# Polybot — Polymarket Trading Bot

Ein Trading-Bot für [Polymarket](https://polymarket.com) (CLOB auf Polygon)
mit Arbitrage-Strategien, hartem Risk-Management und Paper-Trading-Modus.

> ## ⚠️ Ehrliche Einordnung zuerst
>
> **Kein Bot kann 1000 Fr. Gewinn pro Tag garantieren.** Wer das verspricht,
> lügt. Prediction Markets sind kompetitiv: Arbitrage-Fenster werden von
> vielen Bots in Sekunden geschlossen, und jede Strategie mit Marktrisiko
> kann verlieren. Realistisch gilt:
>
> - **Arbitrage** (dieser Bot): mathematisch risikofrei *wenn* beide Beine
>   gefüllt werden — aber selten, klein und umkämpft. Erträge skalieren mit
>   Kapital und Geschwindigkeit, nicht mit Wünschen. Für 1000 Fr./Tag bräuchte
>   es sechsstelliges Kapital plus Infrastruktur (Co-Location, WebSockets)
>   — und selbst dann gibt es keine Garantie.
> - **Market Making**: verdient Spread + offizielle Maker-Rebates, trägt
>   aber Inventarrisiko. Kann an schlechten Tagen verlieren.
> - Der Bot startet deshalb im **Paper-Modus** (Simulation gegen echte
>   Orderbücher). Erst nach Wochen positiver Paper-Ergebnisse überhaupt
>   über Live-Betrieb nachdenken — mit Geld, dessen Totalverlust verkraftbar ist.
>
> **🇨🇭 Schweiz:** Polymarket sperrt die Schweiz nicht, aber die GESPA hat
> polymarket.com als unlizenziertes Geldspiel auf die Schweizer
> ISP-Sperrliste gesetzt. VPN-Umgehung verstößt gegen die Polymarket-ToS.
> **Kläre deine Rechtslage, bevor du live handelst.**
> Details: [docs/POLYMARKET_GUIDELINES.md](docs/POLYMARKET_GUIDELINES.md)

## Was der Bot tut

| Strategie | Prinzip | Risiko |
|---|---|---|
| `complement_arb` | Bester YES-Ask + bester NO-Ask < 1 USDC (nach Taker-Gebühren) → beide kaufen; Auszahlung bei Auflösung immer 1 USDC | quasi-risikofrei bei vollständiger Ausführung |
| `negrisk_arb` | Multi-Outcome-Event: Summe aller YES-Asks < 1 (oder aller NO-Asks < n−1) → alle kaufen | quasi-risikofrei bei vollständiger Ausführung |
| `market_making` (opt-in) | Beidseitige Quotes um den Mittelkurs, verdient Spread + Maker-Rebates | Inventarrisiko — standardmäßig aus |

Eingebaute Schutzmechanismen (`polybot/risk.py`):

- Limits für Ordergröße, Position pro Markt und Gesamt-Exposure
- **Kill-Switch** bei Erreichen der Tagesverlustgrenze
- Arbitrage-Gruppen werden **ganz oder gar nicht** ausgeführt; live als
  FOK-Orders, damit kein Bein offen bleibt
- Taker-Gebühren (Stand März 2026: `feeRate × p × (1−p)`, 0–7 % je
  Kategorie) sind in jede Edge-Berechnung eingerechnet

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # Limits anpassen
```

## Benutzung

```bash
# Einmalig nach Arbitrage scannen (nur lesen, kein Handel):
python -m polybot.main scan

# Bot laufen lassen — Standard ist Paper-Trading (Simulation):
python -m polybot.main run

# Paper-Portfolio ansehen:
python -m polybot.main status
```

### Live-Trading (erst nach ausgiebigem Paper-Testing!)

1. `cp .env.example .env` und `POLY_PRIVATE_KEY` eintragen (bei
   Polymarket-Konto via Website zusätzlich `POLY_FUNDER_ADDRESS` =
   Proxy-Wallet-Adresse, `POLY_SIGNATURE_TYPE=2`).
2. In `config.yaml`: `mode: live`.
3. Klein anfangen: `max_order_usdc` und `daily_loss_limit_usdc` bewusst
   niedrig setzen.

Seit dem Exchange-Upgrade vom 28.04.2026 nutzt Polymarket V2-Contracts mit
pUSD als Collateral — dieser Bot verwendet dafür den offiziellen
`py-clob-client-v2`.

## Go-Live-Checkliste

Der Reihe nach, kein Schritt überspringbar. Paper-Ergebnisse sind eine
Obergrenze, keine Prognose — live konkurriert der Bot um dieselbe
Buchliquidität, die er im Paper-Modus einfach zugeteilt bekommt.

1. **Rechtslage klären.** Polymarket steht in der Schweiz auf der
   GESPA-Sperrliste; VPN-Umgehung verstößt gegen die ToS. Wer das nicht
   sauber geklärt hat, hört hier auf
   (siehe [docs/POLYMARKET_GUIDELINES.md](docs/POLYMARKET_GUIDELINES.md)).
2. **Wallet & Approvals.** Eigenes Wallet nur für den Bot, pUSD
   (`0xC011…2DFB`) und POL für Gas auf Polygon. Approvals für CTF Exchange V2,
   NegRisk Exchange V2 und NegRisk Adapter setzen. Nur Geld einzahlen, dessen
   Totalverlust verkraftbar ist.
3. **`.env` einrichten.** `POLY_PRIVATE_KEY` (bei Website-Konto zusätzlich
   `POLY_FUNDER_ADDRESS`, `POLY_SIGNATURE_TYPE=2`). Den Key nirgendwo
   committen; `mode: live` erst in einer separaten Config setzen.
4. **Mit Mikro-Limits starten.** `max_order_usdc` einstellig,
   `daily_loss_limit_usdc` niedrig, `max_total_exposure_usdc` klein. Die
   ersten Stunden zuschauen, nicht laufen lassen und weggehen.
5. **Nach 24h `capture-report` bewerten.** Im Live-Modus läuft automatisch
   ein Paper-Schatten mit (`data/shadow.jsonl`): dieselben Signale gegen
   dieselben Bücher, Vergleich Live-Fill vs. Paper-Fill pro Signal.
   `python -m polybot.main capture-report` zeigt die echte Capture-Quote
   (gesamt / pro Strategie / pro Stunde) und rechnet ehrlich hoch:
   Paper-Rate × Capture = Live-Erwartung. Liegt die Capture-Quote nahe 0,
   ist die gemessene Paper-Rate live wertlos — dann nicht skalieren, sondern
   Ursache suchen (Latenz, Konkurrenz, Orderrouting).
6. **Erst dann skalieren.** Limits schrittweise erhöhen, nach jeder Stufe
   erneut 24h messen. Fällt die Capture-Quote beim Skalieren, ist die eigene
   Ordergröße der Markt-Impact — das ist die reale Kapazitätsgrenze, kein
   Konfigurationsfehler.

## Tests

```bash
pip install pytest
python -m pytest tests/
```

## Projektstruktur

```
polybot/
├── config.py            # YAML + .env, Risk-Limits
├── data/gamma.py        # Marktdaten (Gamma-API, öffentlich)
├── data/orderbook.py    # Orderbücher (CLOB-API, öffentlich)
├── strategies/          # complement_arb, negrisk_arb, market_making
├── risk.py              # Limits + Kill-Switch
├── execution.py         # PaperBroker (Simulation) / LiveBroker (v2-Client)
├── portfolio.py         # Positionen, PnL, Persistenz
├── shadow.py            # Live/Paper-Schattenvergleich (Capture-Quote)
└── main.py              # CLI: scan | run | status | report | cycle-report | capture-report
docs/
└── POLYMARKET_GUIDELINES.md  # Forensische Recherche der Polymarket-Regeln
```

## Regelkonformität

Der Bot hält sich an die Polymarket-ToS (Stand 01.06.2026): kein Spoofing,
kein Wash Trading, keine Manipulation — nur reguläre Limit-Orders über die
offizielle, dokumentierte API innerhalb der Rate-Limits. Automatisierter
Handel ist von Polymarket ausdrücklich vorgesehen (Builder-Programm,
offizielle Agent-Repos). Siehe [docs/POLYMARKET_GUIDELINES.md](docs/POLYMARKET_GUIDELINES.md).

**Kein Financial Advice. Nutzung auf eigenes Risiko.**
