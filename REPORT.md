# Ergebnisbericht: Paper-Trading-Lauf & der Weg zu 1000 Fr./Tag

Stand: 4. Juli 2026, 22:40 UTC — automatisch aktualisiert nach 2.45 Stunden
Paper-Betrieb mit allen Upgrades (Vollmarkt-Scan, WebSocket-Streaming,
Opportunity-Recorder). Alle Zahlen stammen aus `paper_state.json` und
`data/opportunities.jsonl` (4.5 Mio. protokollierte Beobachtungen).

## 1. Das Ergebnis nach 2.45 Stunden (echter Markt, simulierte Fills)

| Kennzahl | Wert |
|---|---|
| Realisierter Gewinn | **+4'211.35 USDC in 2.45 h** (nach Gebühren) |
| Gezahlte Taker-Gebühren | 1'591.01 USDC |
| Portfolio-Wert | 104'211.35 USDC (Start: 100'000) |
| Offene Restpositionen | **0** (alle Paare sofort zu USDC gemergt) |
| Gelegenheiten über Schwelle (Recorder) | 1'527 in 2.45 h |
| Theoretischer Profit im Fenster | 6'856 USDC — der Bot hat davon **61 %** realisiert (konservative Limits) |

Meilensteine: erste 55 Minuten +189 USDC (500 Fills); danach beschleunigte
sich die Rate deutlich, u. a. ein einzelner Merge von 529 Komplement-Sets
(+31.75 USDC in einem Schritt). Der Bot hielt zu keinem Zeitpunkt eine
offene Wette.

## 2. Hochrechnung — und warum sie mit Vorsicht zu geniessen ist

Linear hochgerechnet: 4'211 USDC/2.45 h ≈ **~41'000 USDC pro 24 h**; die
theoretische Obergrenze laut Recorder (volle Tiefe, unbegrenztes Kapital)
läge bei ~67'000/Tag. **Das Tagesziel von 1'000 wäre damit um ein
Vielfaches übertroffen — aber diese Zahlen sind eine Obergrenze aus einem
aussergewöhnlichen Fenster, kein Erwartungswert:**

1. **Das Zeitfenster war aussergewöhnlich gut:** WM-2026-Abend mit vielen
   live laufenden Spielen. Live-Sport erzeugt laufend Preisverwerfungen
   zwischen YES und NO — nachts und an ereignisarmen Tagen gibt es davon
   deutlich weniger. Die 24h-Hochrechnung aus einem Top-Fenster ist eine
   Obergrenze, kein Erwartungswert.
2. **Paper-Fills sind optimistisch:** Die Simulation nimmt an, dass wir die
   im Buch liegende Liquidität bekommen. Live konkurrieren schnellere Bots
   (Colocation, dedizierte Infrastruktur) um exakt dieselben Cents — ein
   Grossteil der 1'527 Gelegenheiten wäre live von anderen zuerst gegriffen
   worden. Der realistische Live-Capture ist ein Bruchteil der Paper-Quote.
3. **2.45 Stunden sind eine Stichprobe.** Erst mehrere Tage Paper-Betrieb
   (verschiedene Tageszeiten, mit/ohne Sport) ergeben eine belastbare
   Tagesrate. Der Bot läuft unbeaufsichtigt weiter — diese Daten sammeln
   sich von selbst.

**Vorsichtige Lesart:** Selbst wenn live nur 5–10 % der Paper-Rate übrig
blieben, läge der Tageswert an einem sporthaltigen Tag noch im Bereich des
1'000er-Ziels. Genau diese Capture-Quote ist die entscheidende Unbekannte —
sie lässt sich erst mit kleinem echtem Kapital seriös messen, was
rechtliche Klärung voraussetzt (Abschnitt 4).

## 3. Antwort auf die Kernfrage: Wie kommt man auf 1000 Fr./Tag?

Die Analyse der echten Top-Trader (`docs/TRADER_ANALYSE.md`) plus unsere
Messdaten ergeben drei Wege:

| Weg | Mechanik | Voraussetzung | Unser Status |
|---|---|---|---|
| **A: Arbitrage-Bot** (dieser Bot) | Hunderte Mikro-Gewinne, risikofrei-strukturiert, Kapital wird durch Merges sofort recycelt | Geschwindigkeit + Marktabdeckung; wenig Kapital (vierstellig reicht laut Messung) | ✅ läuft; erste Stunde: +189 USDC |
| **B: In-Play-HFT** («swisstony», +536 k$/Tag) | Live-Sport schneller neu bepreisen als der Markt | Schnellere Sportdaten als die Konkurrenz, Top-Infrastruktur | Streaming-Basis gebaut; Sportdaten-Feed fehlt |
| **C: Kapital × Modell** («coldsway», +3.7 M$/Tag) | Grosse direktionale Wetten mit Prognose-Edge | 5–7-stelliges Kapital UND ein Modell, das den Markt schlägt — volles Verlustrisiko | ❌ kein seriöses Bot-Versprechen |

**Empfehlung:** Weg A konsequent weiterfahren (läuft), die gemessene
Tagesrate über 3–7 Tage validieren, dann entscheiden, ob Weg B (Sport-Feed)
den Aufwand lohnt. Weg C ist Trading, kein Engineering — dort verliert man
genauso schnell 1000/Tag wie man sie gewinnt.

## 4. Rechtliches (unverändert wichtig)

Polymarket sperrt die Schweiz nicht, aber die GESPA lässt polymarket.com
von Schweizer ISPs blockieren (unlizenziertes Geldspiel); VPN-Umgehung
verletzt die Polymarket-ToS. **Vor jedem Live-Einsatz mit echtem Geld die
eigene Rechtslage klären.** Paper-Trading (nur öffentliche Marktdaten
lesen) ist davon nicht berührt. Details: `docs/POLYMARKET_GUIDELINES.md`.

## 5. Reproduzieren

```bash
source .venv/bin/activate
python -m polybot.main run --config config.paper.yaml    # Bot laufen lassen
python -m polybot.main status                            # Portfolio ansehen
python -m polybot.main report --target 1000              # Hochrechnung & Kapitalfrage
```

Hinweis zur `report`-Ausgabe: Die «benötigtes Arbeitskapital»-Zahl misst nur
das *gleichzeitig* gebundene Kapital pro Gelegenheit (durch Sofort-Merges
minimal); praktisch relevant sind Positions-/Exposure-Limits in
`config.paper.yaml` — aktuell 500/2'000/20'000 USDC.

---

## Nachtrag 05.07.2026: Live-Befunde Tag 1 und ihre Konsequenzen

### Befund 1: In-play-Märkte sind der grösste, aber vergiftete Teil des Pools

Messung (Tail von `data/opportunities.jsonl`, ~8h, 147 Märkte, 35'005 USDC
theoretischer Komplement-Arb-Profit), gekreuzt mit Gamma-Metadaten
(gameStartTime, secondsDelay):

| Marktklasse | Anteil am theo. Profit | Märkte |
|---|---|---|
| In-play Sport/Esports (Spiel läuft, Matching-Delay aktiv) | **49.9 %** | 33 |
| «Unbekannt» (Map-Winner/Over-Under-Esports — de facto auch in-play) | 21.8 % | 11 |
| Krypto Up/Down (5-Min/15-Min/1h-Fenster) | 21.4 % | 98 |
| Sport vor Spielbeginn | 6.4 % | 3 |
| Sonstige | 0.5 % | 2 |

**Wie könnte diese Zahl lügen?** Sie tut es — genau das ist der Punkt: Der
theoretische Profit auf In-play-Märkten ist für Taker praktisch nicht
einnehmbar. Der serverseitige Matching-Delay (1-5s) macht jede Taker-Order
zu einem Cancel-Race; beide echten Live-Fills des Tages entstanden genau so
und endeten als ungehedgte Einzelbeine. ~70 % des Paper-Pools sind also
Fata Morgana für unsere Taker-Strategie. Konsequenz: der In-play-Filter
(Commit 77d1a66) schneidet sie bewusst weg. Was bleibt und real handelbar
ist: Krypto-Up/Down-Fenster (~21 %) und Vor-Spiel-Sport (~6 %).

### Befund 2: Ungehedgte Einzelbeine («Waisen») — Detektor + Auto-Glattstellung

Dreimal hinterliess der Live-Tag einbeinige Positionen (FaZe-Handicap,
ITF-Tennis, Valorant/O-U) — alle drei endeten zufällig im Plus (+29 USDC),
was der Lehrsatz vom 04.07. verbietet zu feiern: dieselbe Mechanik liefert
genauso −29. Deshalb neu (`polybot/orphan.py`):

- **Waisen-Detektor:** vergleicht pro Tick den Bestand mit der über alle
  je gesehenen Snapshots gelernten YES/NO-Paar-Karte (wichtig: die Märkte,
  auf denen Waisen entstehen, fliegen aus dem nächsten Snapshot — eine
  Nur-Snapshot-Sicht wäre auf dem Hauptfall blind).
- **Auto-Glattstellung:** Überhänge werden nach 60s Schonfrist (länger als
  das Delayed-Order-Poll-Fenster von 15s) zum besten Bid verkauft — immer,
  nicht nur im Gewinn. Fehlt das Buch im Snapshot, holt der Detektor den
  Bid per Batch-Preisabfrage; ohne Bid wird beobachtet, nicht geraten.
- Nicht angefasst: vollständige Paare, NegRisk-Tokens (Phase 2),
  MM-Inventar (Kombination per Config-Check verboten), Staub < 1 USDC.
- Aktiv in `config.live.yaml` (`flatten_orphan_grace_s: 60`); im
  Paper-Messbetrieb aus, damit die Messreihe vergleichbar bleibt.
