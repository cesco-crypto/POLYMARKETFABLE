# Polybot — Arbeitsprinzipien

## Mission
Ziel ist ein ECHTER Ertrag von ~1000 USDC/Tag (Korridor 500–1500 ok) im
Live-Betrieb, iterativ erarbeitet. Paper-Trading ist das Messinstrument auf
dem Weg dorthin, nicht das Ziel. Der Weg: messen → Engpass finden → bauen →
wieder messen → Live-Capture-Quote bestimmen → skalieren, bis die ECHTE
Tagesrate im Korridor liegt.

Stufenplan (Stand 05.07.2026):
1. ✅ Paper-Rate über Ziel (>1000/Tag gemessen, bereinigt, validiert)
2. ⏳ Livegang Phase 1 mit Mikro-Limits (wartet nur auf POLY_PRIVATE_KEY
   als Umgebungs-Secret; Wallet 0x5cbE…d159 ist mit USDC+POL ausgestattet)
3. ⏳ Capture-Quote messen (capture-report, 24-48h) — die eine Zahl, die
   Paper von Real trennt
4. ⏳ Skalieren (Limits/Kapital) bis echte Tagesrate 500-1500, mit
   Re-Messung nach jeder Stufe

## Prinzip Nr. 1: Speed
Nicht die Idee gewinnt, sondern wer schneller lernt, baut, verbessert und
iteriert. Konkret heißt das hier:
- Keine Iteration wartet auf den nächsten Tag. Der Messbot läuft in
  90-Minuten-Zyklen; Code-Verbesserungen landen automatisch im nächsten
  Zyklus (jeder Zyklus startet den Prozess frisch).
- Neue Strategie-Ideen werden ZUERST im Recorder gemessen (beobachten,
  nicht handeln), dann erst als Trading-Strategie aktiviert — Messung ist
  billiger als ein falscher Trade.
- Jeder abgeschlossene Zyklus wird ausgewertet: Rate, Capture, Engpass.

## Prinzip Nr. 2: Ehrlichkeit vor Schönfärberei
- Paper-Gewinne sind nur so gut wie ihre Validierung (Lehre vom 04.07.2026:
  Phantom-Arbitragen auf abgelaufenen Märkten, gefunden per
  Trade-Print-Validierung gegen die Data-API).
- Kein Ergebnis wird berichtet, ohne die Gegenfrage: «Wie könnte diese
  Zahl lügen?»

## Technische Leitplanken
- Standardmodus paper; live nur mit expliziter Freigabe UND geklärter
  Rechtslage (Schweiz/GESPA: siehe docs/POLYMARKET_GUIDELINES.md).
- Keine ToS-Verstöße: kein Spoofing, kein Wash-Trading, Rate-Limits achten.
- Tests müssen grün sein, bevor Code in den Messzyklus geht:
  `.venv/bin/python -m pytest tests/ -q`
- Messdaten (paper_state.json, data/) sind git-ignoriert; Erkenntnisse
  gehören in REPORT.md (committen + pushen — Container sind flüchtig).

## OpenWiki usage rule

This repository uses OpenWiki for agent-readable documentation.

Before making non-trivial changes, especially in trading, execution,
websocket, orderbook, risk, wallet, settlement, or backtesting code:

1. Read the relevant files inside `/openwiki`.
2. Then inspect the actual source files.
3. If `/openwiki` and source code disagree, source code is the source of truth.
4. Do not add secrets, API keys, private keys, wallet seeds, tokens, or
   credentials to `/openwiki`.
5. After architectural changes, run `openwiki --update` — or update the
   betroffenen Wiki-Seiten direkt, wenn kein Modell-API-Key verfügbar ist
   (z.B. in dieser Claude-Code-Umgebung).
6. Never modify live trading, execution, wallet, or risk-limit code without
   explaining the impact and proposing tests first.

Einstieg: `openwiki/quickstart.md` (Landkarte aller Module und Seiten).
Die tägliche Auto-Aktualisierung läuft über
`.github/workflows/openwiki-update.yml` (braucht ANTHROPIC_API_KEY oder
OPENROUTER_API_KEY als Repo-Secret).
