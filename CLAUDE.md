# Polybot — Arbeitsprinzipien

## Mission
Ziel ist ein Paper-Trading-Ertrag von ~1000 USDC/Tag (Korridor 500–1500 ok),
iterativ erarbeitet. Der Weg: messen → Engpass finden → bauen → wieder messen.

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
