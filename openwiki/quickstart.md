# Polybot — Quickstart

Polybot ist ein Polymarket-Arbitrage-Bot (Python 3.11, `.venv`). Ziel:
~1000 USDC/Tag ECHTER Ertrag, iterativ erarbeitet über die Schleife
messen → Engpass finden → bauen → wieder messen (siehe `/CLAUDE.md`).
Standardmodus ist **paper** (Simulation gegen echte Orderbücher); live
nur mit expliziter Freigabe und geklärter Rechtslage.

## Befehle

```bash
source .venv/bin/activate
python -m polybot.main run --config config.paper.yaml    # Paper-Messbot
python -m polybot.main run --config config.live.yaml     # LIVE (echtes Geld!)
python -m polybot.main scan                               # Einmal-Scan, handelt nichts
python -m polybot.main status                             # Portfolio ansehen
python -m polybot.main report --target 1000               # Hochrechnung & Kapitalfrage
python -m polybot.main cycle-report                       # Fenster-Metriken (PnL-Ledger)
python -m polybot.main capture-report                     # Live vs. Paper (Capture-Quote)
python -m polybot.main preflight [--execute]              # Live-Vorflugkontrolle
python -m pytest tests/ -q                                # MUSS grün sein vor jedem Deploy
```

Live-Deployment auf dem MacBook des Betreibers: `MACBOOK_LIVE.md` und
`scripts/run_live_macbook.sh` (Update-Zyklus: Ctrl-C → `git pull` →
Skript neu starten; jeder Neustart lädt den aktuellen Code).

## Landkarte

| Pfad | Inhalt | Wiki-Seite |
|---|---|---|
| `polybot/main.py` | CLI, Snapshot-Aufbau, Tick-Pipeline, Stream-Loop, Merges | [architecture](architecture.md) |
| `polybot/data/` | Gamma-API, Orderbücher (REST+WSS), Fee-Raten | [data-layer](data-layer.md) |
| `polybot/strategies/` | Signal-Erzeuger: complement_arb, negrisk_arb, market_making | [strategies](strategies.md) |
| `polybot/risk.py` | Harte Limits, Kill-Switch, Alles-oder-nichts-Gruppen | [risk-and-execution](risk-and-execution.md) |
| `polybot/execution.py` | PaperBroker (Fill-Simulation), LiveBroker (CLOB-Orders) | [risk-and-execution](risk-and-execution.md) |
| `polybot/orphan.py` | Waisen-Detektor: ungehedgte Einzelbeine glattstellen | [risk-and-execution](risk-and-execution.md) |
| `polybot/portfolio.py` | Buchhaltung, Fills, Merges (Paper), Persistenz | [portfolio-and-settlement](portfolio-and-settlement.md) |
| `polybot/onchain.py` | On-chain-Merges via web3 (Live-Kapitalrecycling) | [portfolio-and-settlement](portfolio-and-settlement.md) |
| `polybot/deposit_wallet.py`, `preflight.py` | V2-Deposit-Wallet-Flow, Live-Checks | [portfolio-and-settlement](portfolio-and-settlement.md) |
| `polybot/recorder.py`, `cycle_report.py`, `shadow.py` | Messinstrumente (Beweisdaten, Ledger, Capture) | [measurement-and-reporting](measurement-and-reporting.md) |
| `polybot/config.py`, `config.*.yaml` | Konfiguration, Validierung, Secrets aus Env | [config-and-ops](config-and-ops.md) |
| `tests/` | pytest-Suite (~400 Tests), Fakes statt Netz | [risk-and-execution](risk-and-execution.md#tests) |

## Eiserne Regeln (Kurzfassung, Details in /CLAUDE.md)

1. Tests grün vor jedem Deploy in den Messzyklus.
2. Keine Änderung an Execution/Wallet/Risk-Limits/Live-Trading ohne
   Erklärung, Tests und Bestätigung des Betreibers.
3. Jede berichtete Zahl braucht die Gegenfrage «Wie könnte sie lügen?».
4. Erkenntnisse nach `REPORT.md` (committen), Messdaten sind git-ignoriert.
5. Keine Secrets in Code, Config-Dateien, Wiki oder Chat — nur Env-Variablen.
