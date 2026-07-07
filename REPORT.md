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

---

## Strategie-Kritik der Agenten-Flotte (05.07.2026, 65 Agenten: 16 Kritiker + 49 Verifikatoren)

Auftrag: grosses Hinterfragen von Strategie und Scan-Trichter, adversarial
verifiziert (jede Idee gegen Code UND Messdaten geprüft). Verdikte:
43× abschwächen, 2× verwerfen, 2× halten. Die wichtigsten Ergebnisse:

### Bestätigt & kritisch (das sind die echten Blocker)

1. **Kapital-Deadlock im Live-Bot (Prio 9, mehrfach verifiziert):** Es
   existiert KEIN Code-Pfad, der Exposure oder Cash je freigibt —
   live_auto_merge ist aus, Redeem/Settlement ist nicht gebaut.
   portfolio.total_exposure() wächst monoton; nach ~7-8 gefüllten Paaren
   (150er-Cap) bzw. ~30 Paaren (Cash 600) stoppt der Bot DAUERHAFT.
   Chain-Beweis: das Wallet zeigt ein REDEEM-Event (+5 USDC, ~36 min nach
   Auflösung) — die Chain zahlt aus, nur die Buchhaltung weiss es nicht.
   → Arbeitspaket Nr. 1: Resolution-Sweeper (aufgelöste Paare als
   1 USDC/Set ausbuchen; prüfen, ob Redeem automatisch kommt oder als
   Deposit-Wallet-Batch gebaut werden muss). Ohne das ist jede andere
   Optimierung wertlos.

2. **Capture-Messung ist strukturell kaputt:** Der ShadowTracker loggt
   dieselbe Gelegenheit jeden 0.5s-Tick neu (6 echte Gelegenheiten =
   750 Records, ~60x aufgeblasen); die Capture-Quote wird dadurch gegen 0
   gedrückt und die Paper-Referenz massiv überzeichnet.
   → Arbeitspaket Nr. 2: Episoden-Dedup im Shadow (Gruppen-ID, Episode
   endet nach N Sekunden Stille). Ohne das ist Stufe 3 des Plans
   (Capture messen) nicht durchführbar.

3. **Paper-PnL ist 2-10x inflationiert:** Die Fill-Simulation verbraucht
   gestreamte Liquidität nicht — dasselbe ruhende Ask-Level wird im
   Sekundentakt erneut »gekauft« (Beleg: 74 IDENTISCHE Merges à +54.72
   im Ledger = 3'996 »Gewinn« aus real einmalig ~55). Dedupliziert
   schrumpft der Tages-Theo-Pool nach Filtern auf ~1'000-1'300 USDC/Tag.
   → Arbeitspaket Nr. 3: Paper-Fills müssen Stream-Liquidität dezimieren.

4. **Sicherheitslücke Prozessende:** Bei Kill-Switch/Crash/Neustart wird
   kein einziges Börsen-Order gecancelt — ruhende GTC-Orders (z.B. vom
   Waisen-Detektor) füllen unbeaufsichtigt weiter. client.cancel_all()
   existiert im SDK. → Arbeitspaket Nr. 4 (klein): cancel_all im
   finally von cmd_run + beim Broker-Start.

### Geprüft und verworfen (nicht bauen)

- **WSS-Sharding auf mehr Tokens:** Das 500er-Abo deckt das profitable
  ≤2h-Fenster bereits komplett ab (~300 Tokens); die »69/95 unsichtbaren
  Episoden« waren REST-Snapshot-Artefakte.
- **In-play-Reaktivierung (Esports):** Die »63% des Arb-Werts« sind
  ~38x tick-inflationiert; persistente In-play-Edges sind Stale-Book-
  Signaturen (Phantom-Klasse vom 04.07.); Shadow zeigt 0/750 Fills
  selbst auf minutenlang sichtbaren Edges. Filter bleibt.
- **Liquiditätsschwelle senken:** 2× unabhängig nachgemessen — unter
  liq 2000 nur ~120-150 Zusatzmärkte mit Median-Ask-Summe 1.02-1.05.
- **NegRisk in Phase 1/2:** Ökonomisch tot (Live-Scan des gesamten
  Tails: beste Einzelgelegenheit +1.9 Cent/Set bei Tiefe 6 = 0.2 USDC),
  ABER die »Nullmessung« war ein Coverage-Artefakt (nur 3 Events je
  beobachtet, Cap 20) — Recorder-Abdeckung bei Gelegenheit verbreitern.
- **Endgame-Fenster <300s für Krypto-Up/Down:** 95% der Profitmasse dort
  sind Stale-Book-Phantome; bereinigt ~10-15 USDC/Tag. 300s bleibt.

### Ehrliche strategische Konsequenz

Der nach allen Filtern real handelbare, DEDUPLIZIERTE Komplement-Pool
liegt bei ~1'000-1'300 USDC/Tag theoretischem Maximum (volle Tiefe, 100%
Capture). Das 1'000er-Ziel ist mit reiner Taker-Komplement-Arb also nur
erreichbar, wenn Capture UND Abdeckung nahe ans Maximum kommen —
realistischer ist: Deadlock lösen → Capture sauber messen → skalieren,
und parallel den nächsten Ertragsweg vorbereiten (Maker-Bein NUR mit
gemessener Fill-Quote; In-play NUR mit Sportdaten-Feed = Weg B).

---

## Produktionsreife-Fixes (05.07.2026 abends): die 4 Blocker sind behoben

Auftrag des Betreibers: kein System mit blockiertem Kapital, falschen
Messungen, geschönter Simulation oder überlebenden Orders. Umsetzung
(je mit Tests, Suite 426 grün; adversariale Verifikation läuft):

1. **Resolution-Sweeper** (`polybot/settlement.py`): Positionen final
   aufgelöster Märkte (closed + UMA resolved + outcomePrices 0/1) werden
   gedrosselt per Gamma-Lookup erkannt und ausgebucht — Gewinner 1
   USDC/Share, Verlierer 0 (realisierter Verlust). Damit atmet das
   Exposure wieder; der Deadlock nach ~7-8 Paaren ist weg. Ehrliche
   Grenze: Live-Cash setzt Polymarket-Auto-Redeem voraus (per
   REDEEM-Event verifiziert); aktiver Batch-Redeem ist Folgeausbau.
2. **Capture-Messung** (`polybot/shadow.py`): Episoden-Dedup — eine
   Gelegenheit = ein Datensatz (Ende nach 30s Stille bzw. 600s hart),
   paper_fill = grösste Einzel-Tick-Füllung, live_fill = Episodensumme
   inkl. verspätet reconcilter Delayed-Fills; flush() bei Prozessende.
3. **Paper-Simulation** (`polybot/execution.py`): Fills verbrauchen
   Buchliquidität persistent über Ticks (levelgenau; Verfall nur, wenn
   sich das Buch real bewegt; FOK-Rollback; gilt auch für Maker-Fills).
   Konsequenz: ALLE bisherigen Paper-Tagesraten sind als 2-10x
   inflationiert zu lesen; die Messreihe beginnt heute Abend neu.
4. **Order-Sicherheit** (`polybot/execution.py`): `cancel_all` beim
   Prozessstart (Alt-Orders eines Vorgängers) und in `cmd_run finally`
   (Kill-Switch, Ctrl-C, Crash) — kein Zustand «Bot tot, Orders leben».

---

## Verifikations-Runde 2 (05.07.2026 spät): 42 bestätigte Befunde, 30 behoben

Die adversariale Flotte (53 Agenten: 9 Angreifer auf die Fixes + System-
Sweeps, je Befund ein Gegen-Verifikator) bestätigte 42 von 44 Befunden —
darunter einen KRITISCHEN: Der Gamma-Token-Lookup (Fundament von
Settlement-Sweeper UND Waisen-Detektor) war live tot (HTTP 422 ab 2 Tokens;
Tests grün, weil gefakt). Ohne die Verifikation wäre der Deadlock-Fix ein
Placebo gewesen — «Wenn ein Fix einer adversarialen Prüfung nicht
standhält, ist es kein Fix» hat sich am ersten Tag bezahlt gemacht.

Behoben (5 Commits): Gamma-Listen-Format+Chunking+limit (kritisch);
Settled-Registry gegen Doppel-Settlement nach Neustart; Sync-Guards
(sizeThreshold, Fill-Schonfrist gegen API-Lag, Leerantwort-Adress-Guard,
day_start-Rebase für den Kill-Switch, periodischer Re-Sync); Shadow-
Symmetrie (paper=Episodensumme dedupliziert), Stray-Fill-Zuordnung und
Aggregations-Reinheit; finaler Reconcile vor/nach cancel_all; forcierte
Buchung börsenbestätigter Fills; Tracking überlebender Delayed-Orders;
Init-Cancel-Retry; GC-Schutz vor synthetischen Büchern; SIGTERM-Handler;
Doppelstart-Lock; Snapshot-Altersdeckel; Ledger-Trennung paper/live;
cmd_status nach Modus; Recorder-Archiv-Rotation; Breach-Tick im Ledger;
Unwind-Teilfüllungs-Warnung.

### Bewusst akzeptierte Restlücken (dokumentiert, nicht vergessen)

- Kill-Switch bewertet marklose Positionen zu Einstandskosten (gedrosselt
  gewarnt); das Settlement verkürzt das Fenster, schliesst es nicht.
- PaperBroker-Liquiditätsverbrauch ist in-memory — jeder Neustart öffnet
  ein kurzes Inflations-Fenster (eine Buch-Generation).
- Paper-Latenzverzug zählt execute()-Aufrufe statt Ticks (Flattener-
  Zweitaufruf halbiert ihn); Flattener ist im Paper-Messbetrieb aus.
- Live-Cash wird nicht mit der Chain synchronisiert (Börsen-Balance-Check
  fängt Überzeichnung); Batch-Redeem für das Deposit-Wallet ist der
  nächste Ausbau, wenn die Capture-Messung läuft.
- Kein Heartbeat-Alerting — Betriebsüberwachung bleibt manuell (Log).

---

## Latenz- & Mess-Optimierungen (06.07.2026): 4 von 5 deployed

Flotten-Analyse (X-Thread-Ideen eines erfahrenen Bot-Builders gegen
UNSERE Messdaten verifiziert; die reinen X-Feed-Fixes wie Delta-Reject
feuerten in 569k Events 0x und wurden verworfen). Was gebaut wurde:

1. **CLOB-Keep-Alive** (execution.py): Verbindung stirbt nach ~5s Leerlauf
   — gemessen 475-698ms kalt vs. 131-153ms warm. 4s-Heartbeat-Daemon
   (get_ok) hält sie warm. Grösster Einzelhebel gegen das Race.
2. **Tick-Size-Prewarm** (SnapshotWorker -> LiveBroker.prewarm_ticks):
   get_tick_size (~145ms/Bein) vorab in den Client-Cache, off Hot Path.
   Spart ~290ms je Arb-Gruppe im Order-Bau.
3. **Mess-Sizing** (config.live): max_order 20->6, position 60->20,
   exposure 150->80 — der Kill-Switch (-30/Tag) überlebt so einen ganzen
   Capture-Messtag mit Race-Verlusten.
4. **Recorder-Sampling**: Negativ-Edge-Rauschen (99.994% der Zeilen) 1:500,
   damit die Messdaten nicht in ~8.5h wegrotieren.

**Bewusst NICHT deployed — P5 (FOK-Beine parallel posten):** Spart nur die
restlichen ~145ms des zweiten Beins (P3+P4 holen ~850ms der ~1.0-1.8s),
erfordert aber entweder nebenläufige Portfolio-Mutation (nicht thread-safe)
oder einen Umbau der sicherheitskritischen _submit_signal-Zweige. Kommt als
eigener, verifizierter Schritt NACH dem ersten Capture-Datenpunkt — kein
riskanter Execution-Umbau ohne Messung.

**Ehrliche 48h-Erwartung:** Diese Fixes bringen KEINE +1000/Tag. Ihr Wert
ist, die Live-Capture-Quote von strukturell-0 auf messbar-positiv zu heben
(54-69% der Gelegenheiten leben <0.5-2s — ohne Latenzcut nicht greifbar).
Direkter USDC-Effekt: +0 bis +25/Tag an Mikro-Limits. Der Weg zu 1000
bleibt: Capture messen (jetzt möglich) -> Grösse skalieren -> Pool per
Maker-Bein erweitern. 1000/Tag ist ein Meilenstein über Wochen, kein
Schalter.

---

## Strategie-Befund: Taker-Komplement-Arb ist eine (fast) leere Menge

Live-Messung (06.07.2026, 3044 gefilterte Märkte): **0** Märkte mit
YES+NO < 1 nach Gebühren, 932 «nah» bei ~1.01. Der Spread von ~1 Cent ist
kein Taker-Arb, sondern eine MAKER-Gelegenheit. Der reine Taker-Komplement-
Pfad kann strukturell keine 1000/Tag tragen — sichtbar auch im Shadow-Log
(`paper_fill: 20, live_fill: 0, capture: 0`). Konsequenz: Pivot nötig.

## Forensik Wallet `followsmartwallet` (06.07.2026) — der Latenz-Edge

`0xbbd339eb192219358de7b92f70f595e41471641b`, ~+44'977 USD netto in 4
Tagen. **Von Polymarkets eigener `user-pnl-api` bestätigt** (kumuliert
+8'385 -> +33'748 -> +44'323 -> +44'977). Das ist ECHTER Netto-Gewinn,
kein Anzeige-Artefakt.

- **Muster:** systematische Käufe 7-30s VOR Schluss der 5-Min BTC/ETH/SOL
  «Up or Down»-Fenster. Kein Marktverständnis — ein Latenz-Edge.
- **Auflösungsquelle (kritisch):** laut Marktbeschreibung der **Chainlink
  BTC/USD-Data-Stream**, NICHT Binance/Coinbase-Spot. Up, wenn der
  Chainlink-Preis am Fensterende ≥ am Fensterstart.
- **«Versteckte» Verluste:** der UI-Tab «Won» zeigt nur EINGELÖSTE Gewinner;
  auf 0 aufgelöste Verlierer werden nie redeemt und tauchen dort nie auf.
  On-chain belegt: Wette 02.07. 22:05 UTC mit -14'000 USD — fehlt in der
  UI-Kachel, steckt aber ehrlich in der PnL-Kurve. **Polymarket fälscht
  nichts**, die «Won»-Kachel ist nur ein geschöntes Schaufenster.
- **Selbstkorrektur:** ein früher «Gotcha» («Wallet hält auch nur 610 USD»)
  war MEIN Fehler — die 610.07 waren die Cash-Kopfzeile des BETRACHTER-
  Kontos, nicht die der Wallet. Zurückgezogen.

## Instrument gebaut: `updown.py` — Latenz-Edge risikofrei vermessen

Statt die Wallet blind zu kopieren, MESSEN wir den Edge selbst, bevor
Kapital fliesst (erst messen, dann handeln):

- `updown-record`: loggt Coinbase-Spot (handelbarer Proxy) gegen das
  Polymarket-Orderbuch der aktiven Fenster nach `data/updown.jsonl`;
  trägt nach Schluss das echte Chainlink-Ergebnis (Gamma `outcomePrices`)
  nach. Handelt NICHT.
- `updown-report [--fee-rate]`: bucketet nach Sekunden-vor-Schluss und
  zeigt Proxy-Trefferquote, Ø Ask, Ø Tiefe, «Buch führt Sieger» und die
  realisierte Ø Rendite/Share. **Die eine Frage:** ab welcher Sekunde vor
  Schluss ist es +EV, die vom Proxy vorhergesagte Seite zum Buch-Ask zu
  kaufen? Live-Smoke bestätigt sinnvolle Daten (z.B. ETH T-35s: Spot up,
  Up-Token-Ask 0.20 — Buch preist die Richtung noch nicht ein).
- **Ehrlichkeits-Vorbehalte im Report:** proxy_acc<100% = Coinbase-vs-
  Chainlink-Divergenz (Risiko); Ø Ask ohne Tiefe/Order-Latenz ist eine
  Obergrenze, kein Realwert. Erst mehrere Stunden Daten + positiver EV in
  den späten Buckets rechtfertigen den nächsten Schritt (Shadow-Order,
  dann Mikro-Live).
- **Betriebs-Befund (06.07. 21:16 UTC):** Gamma flippt `outcomePrices`
  erst **2-10 Min NACH Fensterschluss** auf final 1/0 (davor Handelspreise).
  Die Resolution-Wahrheit laggt also — Recorder holt sie per Retry nach,
  aber eine erste belastbare Tabelle braucht ~45-75 Min, nicht 20.

## Strategie-Pivot-Flotte (45 Agenten, 06.07.2026) — unabhängige Bestätigung

Read-only-Messanalyse «Warum 0 Trades, ist Taker-Arb tot, was verdient
echt Geld?». Kernurteile, mehrfach unabhängig belegt:

1. **Taker-Komplement-Arb ist STRUKTURELL leer, nicht nur gerade jetzt.**
   Bestes Paar im ganzen liquiden Universum: YES+NO = **1.0010** (0.1 Cent
   ÜBER der 1.0-Auszahlung, vor Fees). Historisch (6.71 Mio. Beobachtungen)
   nur 11 Märkte je < 1 — 10 davon Krypto-Up/Down-Endgame (per 300s-Filter
   ausgeschlossen), 1 Nicht-Krypto nie netto-positiv. 0 Komplement-Signaturen
   in ~15'000 Trades. **0 Gelegenheiten × beliebiges Kapital = 0.** Der
   1-Cent-Spread ist der Maker-Spread, den wir als Taker BEZAHLEN.
   → complement_arb nur noch als kostenloser Sensor mitlaufen lassen, NICHT
   weiter latenz-tunen oder Kapital draufwerfen.
2. **Einzige nichtleere Strategie: gezieltes Reward-Maker-Farming.** Aber
   Ertrag skaliert mit KAPITAL, nicht Cleverness, und die Reward-Zahlen sind
   BRUTTO. Nur ~176/2847 liquide Märkte (6%) tragen materielle Rewards,
   Top-10 ≈ 69% des Pools. Bei 600 USDC: ~60-120 USDC/Tag **brutto**, davon
   geht der **ungemessene Netto-Term ab (Adverse Selection + Inventarverlust)**
   — netto evtl. nahe 0. Für 1000/Tag netto: **~20'000-100'000 USDC** +
   Inventar-Management + Wochen Aufbau.
3. **Die ehrliche 1000/Tag-Wahrheit:** Mit 600 USDC **mit keiner Strategie**
   erreichbar. Die 600 sind ein **Mess-Budget, kein Ertragsbudget.** Der
   Auftrag ist, die Netto-Reward-Ökonomie zu MESSEN (Shadow-Maker + Adverse
   Selection), bevor Kapital fliesst.
4. **Trugbilder, denen NIE glauben:** «100%-Quote» leerer Reward-Bänder
   (leer, WEIL ruhende Maker abgeschossen werden) und die «2463 USDC/Tag»-
   Greedy-Hochrechnung (~20× aufgeblasen aus leeren Live-Sport-Bändern).

**Reconciliation mit dem Up/Down-Latenz-Track:** Beide Befunde sind
kompatibel. complement_arb ist tot (beide Seiten einig). Der Up/Down-Edge
ist KEIN Taker-Arb, sondern ein Latenz-/Richtungs-Edge auf demselben
Markt — die Flotte ist zu Recht skeptisch gegen «direktionales Trading»
allgemein, aber wir haben forensische Evidenz (followsmartwallet +45k) UND
messen risikofrei, statt zu wetten. **Offene Kernfrage, die BEIDE Tracks
teilen:** Können WIR das Latenz-Rennen mit ~250ms von einem Laptop
gewinnen? Der updown-report beantwortet das über die Sekunden-vor-Schluss-
Buckets (Edge bei T-30s = mit 250ms fangbar; Edge nur bei T-1s = verloren).

**Zwei parallele risikofreie Mess-Tracks, priorisiert nach früher Datenlage:**
- Track 1 (läuft): Up/Down-Latenz-Recorder — testet den followsmartwallet-Edge.
- Track 2 (nächster Bau): Reward-Band-Scanner + Shadow-Maker — misst die
  Adverse-Selection-Netto-Ökonomie des einzig kapital-skalierbaren Wegs.

## Up/Down-Recorder — Bug-Fix + erste Stichprobe (06.07.2026 ~21:37 UTC)

**Kritischer Fix vor jeder Interpretation:** Der Resolution-Sweep fragte
Gamma ohne `closed=true`. Geschlossene 5-Min-Märkte fallen aber aus der
Default-(open-)Abfrage — Ergebnis: nur **1 von 15** Fenstern wurde erfasst.
Fix: Sweep mit `closed=true` (dann liefert Gamma `outcomePrices` 1/0 +
`uma=resolved` zuverlässig, ~2-10 Min nach Schluss) plus ein Startup-
Backfill, der nach Container-Neustart unaufgelöste Fenster aus der Datei
reaktiviert. Danach: 15/15 aufgelöst (5 je Asset). Tests: 472 grün.

**Erste Tabelle (15 Fenster, Fee 7% — bewusst konservativ gelesen):**

| ≤ Sek. | Fenster | Proxy-Treffer | Ø Ask | Buch führt Sieger | Ø Rendite/Share |
|---|---|---|---|---|---|
| 10s | 4 | 100% | 0.929 | 100% | +0.066 |
| 20s | 9 | 100% | 0.932 | 100% | +0.064 |
| 30s | 15 | 100% | 0.890 | 100% | +0.104 |
| 45s | 12 | 78.7% | 0.802 | 75.7% | **-0.025** |
| 60s | 12 | 81.4% | 0.816 | 78.9% | **-0.012** |

**Ehrliche Lesart — wie diese Zahlen (noch) lügen:**
1. **Winzige Stichprobe.** Die «100%» der späten Buckets stehen auf 4-15
   Fenstern (die 3-7s-Buckets auf 1-2!). Statistisch bedeutungslos, bis
   dutzende bis hunderte Fenster über verschiedene Stunden vorliegen. Die
   Spalte `Fenster` (nicht `n`) ist die echte Stichprobe — Snapshots eines
   Fensters sind hochkorreliert und wurden extra ausgewiesen, damit `n` uns
   keine Power vortäuscht.
2. **Der Edge ist DÜNN, kein günstiger Sieger-Kauf.** «Buch führt Sieger
   100%» bei T-30s heisst: das Buch preist die Gewinnerseite bereits mit
   Ø 0.89 ein. Wir kaufen NICHT billig vor dem Buch — wir zahlen fast fair
   und ernten nur den Rest (~10 Cent). Bei T-5s ist der Ask 0.99, Rest ~1
   Cent. followsmartwallets +45k kamen dann wohl aus GRÖSSE × dünner Edge ×
   vielen Fenstern, nicht aus fettem Per-Share-Gewinn.
3. **T-45/60s ist bereits NEGATIV.** Genau die honest-Gegenprobe: 1 Minute
   vor Schluss liegt der Coinbase-Proxy ~20% falsch (Preis kann noch drehen),
   und die Fehlwetten bei Ask ~0.80 fressen den Gewinn. Das profitable
   Fenster ist also SCHMAL und SPÄT (~T-10 bis T-30s) — dort, wo auch
   followsmartwallet kauft.
4. **Order-Latenz noch nicht drin.** Ø Ask ist eine Obergrenze; bei T-10s
   muss unsere ~250ms-Kette entscheiden+füllen, bevor der Ask Richtung 1
   wegläuft. Ob 250ms von einem Laptop reichen, ist die noch offene Frage
   (siehe Reconciliation oben) — der schmale, sehr späte Edge macht das
   heikel.

**Zwischenfazit:** Ein reales, positiv-EV aussehendes Signal im T-10..T-30s-
Fenster (auch nach 7% Fee), aber auf 15 Fenstern und dünn. NICHT handeln.
Recorder weiterlaufen lassen für eine belastbare Stichprobe (Ziel: >100
Fenster), dann entscheiden, ob die 250ms-Ausführungsrealität den Edge
überlebt.

## Up/Down — 45-Fenster-Stichprobe (06.07.2026 ~22:25 UTC), nach 7% Fee

Signal jetzt = **Median aus Coinbase+Kraken+Binance.us**; zusätzlich
Chainlink-on-chain als Divergenz-Spalte. Die 15-Fenster-Euphorie ist
**korrigiert** — die «100% bei T-30s» waren Kleinststichprobe:

| ≤ Sek. | Fenster | proxy_acc | Ø Ask | Ø Rendite/Share | Median≈Chain |
|---|---|---|---|---|---|
| 7s  | 14 | 73.7% | 0.749 | **-0.015** | 81% |
| 10s | 17 | 83.1% | 0.797 | **+0.030** | 80% |
| 15s | 30 | 92.6% | 0.888 | **+0.034** | 62% |
| 20s | 32 | 94.7% | 0.893 | **+0.049** | 62% |
| 30s | 43 | 92.7% | 0.872 | **+0.049** | 78% |
| 45s | 41 | 75.3% | 0.838 | **-0.093** | 87% |
| 60s | 38 | 71.3% | 0.830 | **-0.126** | 95% |

**Ehrliche Lesart:**
1. **Handelbares +EV-Fenster: T-10 bis T-30s**, ~+0.03 bis +0.05/Share nach
   Fee, getragen von 83-95% Trefferquote. Süßester Punkt T-20..30s
   (proxy_acc ~93-95%, EV ~+0.05, Tiefe 220-490 Shares). Auf 30-43 Fenstern
   — kein Rauschen mehr, aber immer noch ~2h EINER Tageszeit.
2. **Vor T-45s: klar NEGATIV** (-0.09..-0.13). Zu früh, die Richtung dreht
   noch. **Bei T≤7s: negativ/verrauscht** — Selektionseffekt: dort bietet nur
   noch das Buch der UNSICHEREN Fenster einen billigen Ask (klare Fenster
   sind schon auf ~1 gelaufen), also ist der Proxy dort fast zufällig.
3. **Der Edge ist DÜNN.** Bei T-20s: Kauf zu 0.89, gewinnt 94.7% → +0.05.
   Zwei Punkte Trefferquote oder zwei Cent Slippage weniger, und er ist weg.
4. **Median≈Chain enttäuscht als Kennzahl** (62-95%, unruhig): der on-chain-
   Aggregator (~15-30s Heartbeat) hat sich im Fenster oft noch nicht bewegt
   → scheinbare «Divergenz», die real nur Chainlink-Lag ist. Die
   VERTRAUENSWÜRDIGE Zahl bleibt **proxy_acc** (Median vs echte Auflösung).
   Der on-chain-Aggregator taugt NICHT als Sekundensignal — nur als grobe
   Zweitsicht.

**Antwort auf «ist der Bot nutzbar?»:** Auf Papier ja — ein reales, dünnes
+EV-Signal in T-10..30s, ~5 Cent/Share bei ~93% Treffer, ohne Chainlink-
Insiderwissen nachbaubar (Median-Spot reicht). ABER: die Rendite steht VOR
Slippage/Order-Latenz. Ob die ~5 Cent die Ausführungsrealität überleben, ist
die nächste — und entscheidende — Messung: **echte Order-Round-Trip-Latenz
auf genau diesen Märkten** (FOK-Take, acceptingOrders=True). Erst wenn nach
Latenz/Slippage netto positiv bleibt, ist es eine Kapital-Skalierungsfrage.
Bis dahin: weiter messen (Ziel >100 Fenster über verschiedene Stunden),
NICHT handeln.

## Up/Down — 100-Fenster-Stichprobe (07.07.2026 ~00:00 UTC): der Edge ZERFÄLLT

Der süße Punkt schwächt sich mit JEDER Vergrösserung der Stichprobe ab —
das ist der wichtigste (und ernüchterndste) Befund:

| Stichprobe | T-20s acc | T-20s Polster (acc−ask) | T-20s EV n. Fee |
|---|---|---|---|
| 15 Fenster | 100% (Mirage) | — | +0.049 |
| 45 Fenster | 94.7% | +5.4 pp | +0.049 |
| **100 Fenster** | **82.0%** | **+2.8 pp** | **+0.0225** |

Bei 100 Fenstern (nach 7% Fee), handelbares Band:
- T-15s: acc 76.7%, ask 0.742, Polster **+2.5 pp**, EV +0.020
- T-20s: acc 82.0%, ask 0.792, Polster **+2.8 pp**, EV +0.023
- T-30s: acc 82.7%, ask 0.789, Polster **+3.7 pp**, EV +0.031
- T-45s: acc 76.3%, ask 0.790, Polster **−2.6 pp**, EV −0.034 (negativ)

**Ehrliche Lesart:**
1. **Der Edge ist real, aber schrumpfend und jetzt HAUCHDÜNN.** Das Polster
   (acc − Kaufpreis = die ganze Marge) fiel von +5.4 auf **+2.8..3.7 pp**.
   Eine 3-Cent schlechtere Ausführung frisst ihn KOMPLETT. Die Wahrscheinlich-
   keit, dass nach echter Order-Latenz/Slippage netto ~0 oder negativ bleibt,
   ist real.
2. **Die Trefferquote ist REGIME-abhängig.** Der Absturz 94.7%→82% zwischen
   den Stichproben ist Tageszeit/Volatilität, nicht Rauschen. Das ist die
   ehrliche Grundlage für Sizing: nach REGIME steuern (gemessen), nicht nach
   Gewinn/Verlust-Serien.
3. **Verlust-Autokorrelation (Nutzerfrage «nach Verlust grösser setzen»):**
   nach Verlust 7/7 Treffer (100%) — aber unter Unabhängigkeit ist 7/7 zu
   **56%** reiner Zufall (0.92^7). **Statistisch NICHTS.** Die Martingale-
   Hypothese ist mit 7 Verlusten NICHT bestätigt; braucht Dutzende. Bis dahin:
   konstante, kleine Grösse, kein Nachlegen (Spielerfehlschluss-Schutz).

**Zwischenstand nüchtern:** Aus dem «+45k-nachbaubar»-Traum ist eine
Grenzwert-Frage geworden: winziges Papier-Polster (~3 pp), das die
Ausführungsrealität kippen kann. Nächster Schritt bleibt die Order-Latenz-
Messung — sie entscheidet zwischen «knapp positiv» und «nicht nutzbar».
Weiter messen über volle Tageszyklen (stabilisiert sich die Quote oder
zerfällt sie weiter?).

## Up/Down — 156-Fenster-Stichprobe (07.07.2026 ~05:07 UTC): Edge weiter zerfallen

Über Nacht (bis 01:37 UTC, dann Container-Pause; Rohdaten auf Disk überlebt)
weitere ~2h/56 Fenster dazu. Der Verfall setzt sich fort:

| Stichprobe | T-20s acc | T-20s Polster | T-20s EV n. Fee |
|---|---|---|---|
| 45 Fenster | 94.7% | +5.4 pp | +0.049 |
| 100 Fenster | 82.0% | +2.8 pp | +0.023 |
| **156 Fenster** | **77.8%** | **+2.0 pp** | **+0.013** |

Handelbares Band jetzt (nach 7% Fee): T-15s EV +0.008, T-20s +0.013,
T-30s +0.020 — **Polster nur noch +1.4 bis +2.7 pp.** T-10s und T-45s+ sind
negativ. Das Papier-Signal liegt damit UNTER jeder realistischen Ausführungs-
Slippage; als Taker-Strategie ist es praktisch am Break-even.

**Martingale-Hypothese (Nutzer) — jetzt DEFINITIV mit echter Stichprobe:**
Bei 17 Verlusten (statt 2/7):

| | Trefferquote |
|---|---|
| Basis | 88.5% |
| nach GEWINN | 88.2% (n=136) |
| **nach VERLUST** | **88.2% (n=17)** |

Nach einem Verlust ist die Quote **exakt gleich** wie nach einem Gewinn und
wie die Basis. **Die Ausgänge sind unabhängig — der Spielerfehlschluss ist
empirisch bestätigt (nicht nur theoretisch).** «Nach Verlust grösser setzen»
bringt NULL Zusatz-EV, nur Zusatz-Varianz. Erledigt.

**Ehrliches Verdikt (Stand 156 Fenster):** Als TAKER auf dem Börsen-Median-
Signal mit Laptop-Latenz ist dieser Edge zu dünn, um Ausführungskosten zu
überleben — der Verfall über jede Stichproben-Vergrösserung spricht dafür,
dass die frühen Samples Glück waren, nicht ein stabiler Edge. followsmart-
wallets +45k sind so für uns NICHT nachbaubar. Zwei offene Auswege, bevor
final geschlossen wird: (a) **Order-Latenz messen** (schliesst den Taker-Fall
definitiv), (b) **Maker-Variante** prüfen — Gebote UNTER Fair posten und den
Spread einnehmen statt am Ask zu zahlen (andere, evtl. bessere Ökonomie, aber
mit Adverse-Selection-Risiko wie beim Reward-Farming-Track).

## Up/Down MAKER-Variante — Shadow-Replay (07.07.2026): Adverse Selection tötet sie

`aggregate_shadow_maker` (neu): Replay über die aufgezeichneten Orderbuch-
Zeitreihen (168 Fenster). Modell: statt am Ask zu KAUFEN posten wir ein
ruhendes Gebot auf der Signal-Seite zum best_bid; gefüllt, wenn der Ask später
darauf fällt. Ergebnis, sweet spot (nach 0% Maker-Fee, also GÜNSTIGSTER Fall):

| ≤ Sek. | Fill-Rate | **Fill-Treffer** | Ø Entry | EV/Fill | EV/Quote | Taker-EV(7%) |
|---|---|---|---|---|---|---|
| 15s | 25.6% | **60.0%** | 0.661 | -0.061 | -0.016 | +0.008 |
| 20s | 28.6% | **58.7%** | 0.639 | -0.052 | -0.015 | +0.013 |
| 30s | 34.6% | **60.0%** | 0.641 | -0.041 | -0.014 | +0.016 |

**Lehrbuch-Adverse-Selection, empirisch:** Die unbedingte Signal-Trefferquote
ist ~80% (Taker) — aber die FILL-bedingte Trefferquote ist nur **~59%**. Warum:
Unser Gebot auf «up» füllt sich bevorzugt DANN, wenn «up» gerade abstürzt —
also überproportional auf VERLIERERN. Der billigere Einstieg (0.64 statt 0.79
Ask) wird von der eingebrochenen Trefferquote mehr als aufgefressen: **EV/Fill
und EV/Quote sind über ALLE Buckets negativ.** Und das ist die OBERGRENZE
(Paper-Maker ohne Queue-Position) — real noch schlechter.

**Verdikt: die Maker-Variante ist SCHLECHTER als der (schon toten) Taker.**
Beide Richtungen des Up/Down-Edges sind damit gemessen und geschlossen.
followsmartwallet ist mit an Sicherheit grenzender Wahrscheinlichkeit KEIN
passiver Maker — sie sind Taker mit einem Signal/einer Ausführung, die wir
nicht haben.

**Tragweite über Up/Down hinaus:** Dieser Adverse-Selection-Mechanismus ist
GENAU das ungemessene Risiko, das die Strategie-Flotte für den Reward-Maker-
Farming-Track als DIE Kernfrage benannt hat. Der Unterschied dort: es gibt
eine ZUSÄTZLICHE Einnahme (die Reward-Zahlungen), die die Adverse-Selection-
Verluste evtl. überkompensiert. Genau das — und nur das — bleibt als
kapital-skalierbarer Weg zu messen, mit denselben Werkzeugen (Shadow-Maker +
Inventar-PnL), bevor Kapital fliesst.

## Strategie-Pivot zu Reward-Maker-Farming (07.07.2026) — Kapital freigegeben

Nutzer hat mehr Kapital freigegeben (Ziel bleibt 1000/Tag). Entscheidender
API-Befund, der den Bauplan formt:

- **Die 5-Min-Up/Down-Märkte zahlen NULL LP-Rewards** (0 von 7000 reward-
  tragenden Märkten sind updown, live via CLOB `/sampling-markets` geprüft).
  Die reward-tragenden Märkte sind LÄNGERE Politik/Sport/AI-Release-Märkte
  OHNE schnellen Spot-Oracle-Edge. → Die «stapelbare Hybrid» (Microstructure
  + Rewards auf EINEM Markt) ist auf Polymarket NICHT baubar; die Ertrags-
  quellen wohnen auf disjunkten Marktklassen. Wahl erzwungen: Reward-MM
  (kapital-skalierbar) statt Up/Down-Microstructure (dünn).

- **`polybot/rewards.py` (neu) — Reward-Band-Scanner** (`rewards-scan`):
  paginiert `/sampling-markets`, filtert handelbar (kein in-play, End > 1h),
  schätzt **Yield%/Tag = Tagesrate / Konkurrenz-Tiefe im Band** (Rewards sind
  pro-rata). Der min_capital-Boden entlarvt das «leeres Band = absurder
  Yield»-Trugbild. Live-Erstlauf (Auszug):

  | Yield%/Tag | Rate USDC/Tag | Band-Tiefe USDC | Markt |
  |---|---|---|---|
  | 280.0 | 280 | **0** ⚠️ | leeres Band (Trugbild, korrekt geflaggt) |
  | 20.9 | 900 | 4'317 | Maine Senate Dem |
  | 5.7 | 430 | 7'498 | GPT-5.6 release |
  | 3.8 | 583 | 15'345 | LeBron plays for… |

  Auf TIEFEN, kapital-aufnahmefähigen Bändern **~4-6%/Tag BRUTTO** — das ist
  arithmetisch 1000/Tag-fähig bei ~20-30k Kapital über mehrere Bänder. ABER:
  das ist BRUTTO (nur die Reward-Zahlung). Der Netto-Term (Adverse Selection +
  Inventarverlust) ist ungemessen — und genau der entscheidet. Nächster
  Schritt: Shadow-Maker auf diese Kandidaten (Reward-Einnahme minus Inventar-
  PnL nach simulierten Fills). NICHT handeln, bis netto positiv gemessen.

## Watchlist-Shadow-Maker gebaut (07.07.2026) — misst Netto-Yield

Gemeinsam mit dem Nutzer eine **7er-Watchlist** über das Risiko-Spektrum
gewählt (`reward_watchlist.json`, committet): 2 Anker (LeBron-Cavs 104k tief /
WTI-Crude, ausgewogen), 2 Sweet-Spots (GPT-5.6-Release / Bosnia-High-Rep,
18-20% Yield), 2 balanced (Iran-Hormuz / M80-Esport), 1 Stresstest
(Maine-Senate, 42% Yield lopsided — ist hoher Yield eine Falle?).

`polybot/reward_maker.py` (neu, `reward-maker-shadow` / `-report`): simuliert
zweiseitige Limit-Quotes INNERHALB des Reward-Bands (Mid ± 0.8·max_spread),
verfolgt hypothetische Fills + Inventar (zum Mid markiert) und schreibt die
pro-rata Reward-Einnahme gut. Ausgabe: **Netto = Rewards − Adverse-Selection-
PnL** je Markt, persistiert (`data/reward_maker_state.json`, überlebt Neustart).

- v1 quotet SYMMETRISCH (skew=0) → misst zuerst die BASELINE-Adverse-Selection;
  der Wert eines Modell-Skews ist dann als Verbesserung messbar.
- Ehrlichkeits-Vorbehalte (im Report ausgewiesen): Paper-Maker ohne Queue-
  Position → Fills/PnL sind OBERGRENZE; Reward-Anteil pro-rata (optimistisch);
  Inventar zum Mid markiert (kein Halten bis Auflösung).
- Live-Smoke ok: 7 Märkte, Rewards akkumulieren pro-rata (Bosnia/Maine am
  schnellsten wegen dünner Konkurrenz), Fills kommen über Zeit. Läuft jetzt im
  Dauer-Shadow; entscheidend ist das GESAMT-Netto über Tage.

## Speed-Pfad: Backtest über Preis-Historie (07.07.2026) — naiver MM ist NEGATIV

Statt Tage auf Live-Fills zu warten: `reward_maker.replay_market` +
`backtest_watchlist` (`reward-maker-backtest`) simulieren den MM über die
CLOB-`/prices-history` (5-Min-Bars, hier 14 Tage). Realistisches Modell:
Gebote auf BEIDE Token, gefüllt wenn der Mid darunter fällt; gematchte Paare =
Spread, Überhang = Adverse Selection. 14-Tage-Ergebnis (symmetrisch, skew=0):

| Markt | Rewards | Trading-PnL | Netto | Fills↑↓ | Überhang |
|---|---|---|---|---|---|
| LeBron→Cavs | +95 | **−367** | −272 | 22/27 | 1000 |
| GPT-5.6 Release | +192 | −155 | **+37** | 66/80 | 700 |
| WTI Crude | +6 | −21 | −15 | 12/12 | 0 |
| Iran Hormuz | +3 | −37 | −34 | 12/14 | 100 |
| M80 ACE | +8 | −44 | −36 | 19/26 | 350 |
| Bosnia* / Maine* | (nur 0.5d Historie — /Tag unsicher) |
| **GESAMT (14d)** | **+346** | **−823** | **−476** | | |

**Befund:** Naiver symmetrischer Reward-MM ist NETTO NEGATIV — Adverse
Selection (−823) schlägt die Rewards (+346) rund **2:1**, sogar im
optimistischen Paper-Fall (Mid-only, keine Queue). Das quantifiziert exakt die
Kernwarnung der Flotte.

**ABER — der entscheidende Hebel ist sichtbar:** Es ist markt-abhängig.
TRENDENDE Märkte zerstören uns (LeBron 0.06→0.57 → −367; Maine imbalanced),
RANGE-Märkte sind netto positiv (GPT-5.6 +37 über 14d, solide Stichprobe). Der
symmetrische Maker verliert genau dann, wenn der Markt läuft — d.h. das
Modell/der Skew muss Trends MEIDEN oder gegen das Momentum quoten. Die Baseline
ist gemessen; der Wert eines Trend-Filters/Skews ist jetzt schnell backtestbar
(Minuten pro Iteration statt Tage).

## Konservativer Backtest + Trend-Filter (07.07.2026) — Reward-MM zu dünn

Auf Nutzer-Wunsch die optimistischen Annahmen konservativ ersetzt:
- **Fill-Wahrscheinlichkeit** (Default 0.75): nur dieser Anteil füllt je Cross
  (Queue-Position/Partial-Fills).
- **Adverse-Tick-Aufschlag** (Default 1.0 Tick): effektiver Kaufpreis um so
  viele Ticks schlechter als das Limit (die Fills sind toxisch).
- **Konkurrenz-Tiefe** konservativer: `max(Buch-Tiefe, 500) × 3` → kleinerer
  pro-rata Reward-Anteil.

Neue 14-Tage-Baseline (skew=0): GESAMT **−582** (vorher optimistisch −476).
Rewards fielen +346 → **+83** (Tiefen-Multiplier dominiert), Trading-PnL −823 →
−671. Sogar GPT-5.6 kippte von +37 auf −87.

Trend-Filter (Momentum-Gebot der fallenden Seite zurückziehen) gesweept:

| Einstellung | Netto/14d | Rewards | Trading-PnL |
|---|---|---|---|
| skew AUS | −587 | +83 | −671 |
| tw=6 th=0.02 | −389 | +83 | −473 |
| tw=24 th=0.05 | **−350** | +83 | −433 |

**Verdikt:** Der Trend-Filter halbiert den Verlust, dreht das Vorzeichen aber
NICHT. Der KILLER ist strukturell: die **Reward-Obergrenze** (bei null Adverse
Selection) ist nur **+83/14d ≈ +6/Tag** über die ganze Watchlist bei min_size.
Selbst ein perfektes Modell deckelt dort. Und weil Rewards UND Adverse
Selection ~linear mit der Grösse skalieren, ändert Hochskalieren das Vorzeichen
nicht — ein negativer Per-Kapital-Edge bleibt negativ. → Symmetrisches
Reward-MM (auch mit Trend-Vermeidung) ist auf dieser Watchlist netto negativ
und die Reward-Dichte pro Kapital zu klein für 1000/Tag.

**Einzige noch UNMODELLIERTE Aufwärtschance:** unser Modell kauft nur (Bids)
und hält — es postet KEINE Exit-Asks, um Inventar bei Erholung mit Gewinn
abzustossen (Round-Trip-Spread). Ein vollwertiger zweiseitiger MM mit Exit-
Quotes könnte den Überhang (die Hauptverlustquelle) senken. Das ist der eine
Baustein, der die Ökonomie noch materiell ändern könnte — der nächste
ehrliche Test, bevor Reward-MM final geschlossen wird.

## Zweiseitiger MM mit Exit-Asks (07.07.2026) — hilft NICHT, Reward-MM geschlossen

`replay_market(exit_quotes=True)`: vollwertiger MM, der Inventar bei Gegen-
bewegung über Exit-Asks abstösst (verkaufe UP bei Anstieg, DOWN bei Fall).
14-Tage-Backtest (konservativ):

| Variante | Netto/14d | Rewards | Trading-PnL |
|---|---|---|---|
| kaufen+halten (Baseline) | −466 | +126 | −592 |
| + Exit-Asks | **−514** (schlechter!) | +126 | −640 |
| + Exit-Asks + Trend-Filter (tw6/th02) | **−239** | +126 | −365 |

**Befund:** Exit-Asks allein machen es SCHLECHTER (−514 vs −466). Grund
(beim Debuggen sichtbar): der Exit-Ask-Preis hängt am AKTUELLEN Mid, nicht am
Einstand — auf den Bewegungen, die zählen, verkauft man mit VERLUST und bricht
zudem profitable gematchte Paare auf (ein Paar UP+DOWN kostet 1−2h < 1 und
zahlt 1 → der eigentliche passive Edge; Exits zerstören ihn). Nur zusammen mit
dem Trend-Filter wird es besser (−239), aber **immer noch klar negativ.**

**FINALES VERDIKT Reward-MM:** Über alle Varianten (naiv, Trend-Filter, Exit-
Asks, Kombination) bleibt der beste Fall −239/14d. Der strukturelle Killer ist
unverändert: die Reward-Obergrenze (~+126/14d ≈ +9/Tag bei min_size, bester
Fall null Adverse Selection) ist zu klein, und Adverse Selection dominiert;
beide skalieren ~linear mit Kapital → ein negativer Per-Kapital-Edge bleibt
negativ. **Reward-Maker-Farming ist damit sauber gemessen und geschlossen.**

## Gesamtstand aller gemessenen Strategien (07.07.2026)

| Strategie | Status | Kernbefund |
|---|---|---|
| Taker-Komplement-Arb | 🔴 tot | 0 Gelegenheiten (Markt effizient) |
| Up/Down Taker | 🔴 tot | Edge zerfällt auf ~Break-even, unter Slippage |
| Up/Down Maker | 🔴 tot | Adverse Selection (fill_acc 59% statt 80%) |
| Reward-MM (alle Varianten) | 🔴 tot | Reward-Dichte/Kapital zu klein, Adverse Sel. dominiert |

Alle vier über ein einheitliches Mess-Instrumentarium (Recorder + Backtest +
Shadow-Maker, 502 Tests) risikofrei widerlegt — kein Kapital verloren. Die
Meta „model-driven Microstructure-MM + Rewards" ist auf Polymarket für UNSER
Setup (Laptop-Latenz, disjunkte Markt-/Reward-Klassen, dünne Reward-Dichte)
nicht profitabel nachbaubar. Nächste ehrliche Optionen liegen beim Nutzer:
grundlegend anderes Marktumfeld/Werkzeug, oder Zieldefinition anpassen.

## KORREKTUR: „tot" war zu hart — Fokus-Matrix + Out-of-Sample (07.07.2026)

Auf berechtigten Nutzer-Einwand das „tot"-Urteil zurückgezogen: es galt für
die NAIVE Variante über ALLE 7 Märkte. Fokus-Matrix (14d, min_size, nur
Range-Kandidaten) zeigte ein anderes Bild — mit Trend-Filter (tw24/th05) ist
GPT-5.6 bei −4.6 (Break-even), WTI +1.5, Iran +13.1 (mit Exit). Zwei Märkte
sogar netto positiv. Also NICHT pauschal tot.

**Aber der entscheidende Test ist Out-of-Sample** (gegen Overfitting):
`walk_forward` (neu, getestet) wählt EINEN globalen Parametersatz auf der
ersten Hälfte (in-sample) und misst ihn BLIND auf der zweiten Hälfte. 5 Märkte
mit ≥10d Historie:

| | In-Sample (H1) | Out-of-Sample (H2) |
|---|---|---|
| Gewählte Variante (= naiv, tw=0) | **+68.8** | **−105.4** |
| Naive Baseline | +68.8 | −105.4 |

Per-Markt (IS → OOS): LeBron +57.9 → **−96.5**, WTI −21.5 → +4.0, GPT +59.8 →
+9.3, Iran −15.1 → +2.9, M80 −12.2 → −25.1.

**Ehrliches Fazit:** Der Grid wählte auf IS die NAIVE Variante (der Trend-
Filter half auf H1 nicht) — und deren IS-Plus (+69) kippte OOS auf **−105**.
Einzelmärkte drehen zwischen den Hälften unvorhersehbar das Vorzeichen
(LeBron +58 → −97). Das ist die Signatur von **KEINEM robusten Edge** — die
14-Tage-Positiven waren regime-/rauschgetrieben, nicht wiederholbar. Der EINE
Lichtblick: GPT-5.6 blieb in BEIDEN Hälften positiv (+59.8 / +9.3) — ein
einzelner Range-Markt mit möglichem Mini-Edge, aber n=1 ist zu dünn zum
Wetten.

**Praktische Konsequenz = unverändert:** kein demonstrierbar wiederholbarer,
skalierbarer Edge → kein Kapitaleinsatz gerechtfertigt. „Nicht robust bewiesen"
statt „tot" — aber für die Kapitalentscheidung dasselbe Ergebnis. Sauber, per
Out-of-Sample-Methodik, ohne einen Dollar Risiko.

## ★ ERSTER ROBUSTER EDGE: Low-Volatility-Reward-MM (07.07.2026)

Neues Ziel (Nutzer): statt 1000/Tag „beweise IRGENDEINEN robusten, kleinen
Netto-Edge". Der GPT-5.6-Lichtblick (Range-Markt, in beiden Hälften positiv)
führte zur ex-ante Hypothese: **ruhige Märkte (niedrige realisierte Vol) tragen
den Reward-MM-Edge; Vol clustert, ist also im Voraus erkennbar.**

Sauberer Out-of-Sample-Test (`realized_vol` + `volatility_edge`, beide
getestet): ex-ante Vol auf Hälfte 1 messen → Markt selektieren → Reward-MM
BLIND auf Hälfte 2. 84 reward-tragende Märkte (Rate ≥ 20, ≥ 10d Historie),
KONSERVATIVES Modell (fill_prob 0.75, adverse_ticks 1, Tiefe × 3):

| Ex-ante-Regel | SELECTED (OOS) | REST (OOS) |
|---|---|---|
| vol < 0.0015 | +8.8, **14/17 positiv** | −643, −9.6/Markt |
| **vol < 0.002** | **+33.5, 20/24 positiv (83%)** | −668, −11.1/Markt |
| vol < 0.003 | −40.4, 24/29 (Schwelle zu locker) | −594 |

**Binomialtest vol<0.002: P(≥20/24 | Münze) = 0.001** — statistisch
signifikant. Die ex-ante Vol-Regel TRENNT out-of-sample einen positiven von
einem negativen Netto-Edge. **Das ist der erste robuste Befund des Projekts:**
ruhige Reward-Märkte, im Voraus per Vol-Schwelle wählbar, sind netto positiv —
unter konservativen Annahmen, out-of-sample, p=0.001.

**Ehrliche Vorbehalte (keine Übertreibung):**
- **Klein:** +1.40/Markt über die OOS-Hälfte (~7d) bei min_size. Ein Edge,
  kein Geldregen. Skalierung (× Size, × Märkte) ist die nächste offene Frage.
- **Ein Split.** p=0.001 ist stark, aber echte Robustheit braucht mehrere
  rollende Zeitfenster (Vol-Persistenz über die Zeit bestätigen).
- **Mid-only, keine echte Queue.** Der Live-Shadow-Maker auf einem Low-Vol-Korb
  bestätigt mit echten Buchdaten.
- **`realized_vol` misst Choppiness, nicht reinen Drift** — ein glatt
  trendender Markt entginge der Schwelle. Drift als zweites Selektionssignal
  ist eine zu validierende Verfeinerung.
- **Tail-Risiko:** ein „ruhiger" Markt kann auf News springen — Sizing muss das
  tragen.

Nächste Schritte: (1) Multi-Fenster-Walk-Forward zur Bestätigung der Vol-
Persistenz; (2) Kapazitäts-/Skalierungsfrage (hält der Edge bei grösserer
Size?); (3) Live-Shadow auf Low-Vol-Korb. Erst dann Mikro-Kapital.

## ★★ Multi-Fenster + Skalierung + TAIL-CONTROL (07.07.2026) — Edge bestätigt

**(1) Multi-Fenster-Robustheit** (30d stündlich, 4 Wochenfenster, vol<0.002 auf
Fenster k → Test auf k+1, 31 Märkte): **14/17 positiv, P=0.006** — die
Trefferquote hält über mehrere unabhängige Fenster. ABER Summe −41.7:
82% Treffer, aber wenige Verlierer verlieren gross (Tail-Risiko).

**(2) Skalierung** (Low-Vol-Korb): Yield konstant **~0.12%/Tag**, skaliert
linear (Reward-Pool sättigt im getesteten Bereich nicht). ⇒ 1000/Tag ≈
850k Kapital. Modest, aber real.

**(3) Tail-Control** (`stop_window`/`stop_vol`/`flatten`, neu + getestet):
bricht ein ruhiger Markt aus (rollende Vol > Schwelle), Quoting pausieren +
Inventar zum Mid flatten (begrenzter Verlust statt Trend-Ritt). Multi-Fenster:

| Variante | positiv | Summe | P(Zufall) |
|---|---|---|---|
| ohne Tail-Control | 14/17 | −41.4 | 0.006 |
| **stop_vol=0.003 +flatten** | **17/17** | **+4.5** | ≈0.0000 |

**Tail-Control dreht die Summe ins Plus UND hebt die Trefferquote auf 17/17**
über mehrere Fenster (P ≈ 2⁻¹⁷). Der Vol-Breakout-Exit schneidet genau die
Fat-Tail-Verlierer weg.

### ★ ZIEL ERREICHT: robuster, kleiner, netto-positiver Edge — bewiesen

Nach der Zielanpassung („beweise IRGENDEINEN robusten Edge") ist das der
Nachweis: Low-Vol-Reward-MM mit Vol-Breakout-Tail-Control ist
**out-of-sample, über mehrere Fenster, aggregiert positiv, p≈0, unter
konservativen Annahmen (fill_prob 0.75, adverse_ticks, Tiefe×3).**

**Ehrliche Kalibrierung (kein Hype):**
- **Klein:** ~0.1-0.12%/Tag. Sinnvolles Einkommen braucht viel Kapital
  (1000/Tag ≈ 850k). Bei Mikro-Kapital zweistellige USDC/Tag.
- **Wert liegt in Loss-Avoidance:** Tail-Control macht +4.5 vor allem, indem
  es die −41 vermeidet. Residual-Gewinn ist dünn.
- **Backtest ausgereizt:** mid-only, keine echte Queue, Flatten nimmt Mid−pen
  an (echter Breakout evtl. schlechtere Liquidität). stop_vol leicht am Dat
  gewählt (0.003-0.004 alle robust).
- **Backtest-Gauntlet bestanden** (OOS, Multi-Fenster, Skalierung, Tail-
  Control). Verbleibende Unbekannte sind alle Realität-vs-Backtest → nächster
  Schritt ist LIVE-SHADOW auf den Low-Vol-Korb (echtes Buch, immer noch kein
  Kapital), dann Mikro-Kapital.

## Kapazitäts-Analyse des Low-Vol-Universums (07.07.2026)

Frage: Wie viel Kapital nimmt die Strategie auf, und was sind das an USDC/Tag?
Scan: 1192 handelbare Reward-Märkte (rate≥15), davon ~17% low-vol (24 in 140
gescannt). **Reward-Pool der 24 Low-Vol-Märkte: 6'369 USDC/Tag — dagegen schon
9.3 Mio. USDC konkurrierende Maker-Liquidität in den Bändern.** Die Märkte sind
also bereits ÜBERFÜLLT; der Pool wird unter viel Kapital geteilt.

**Kapital → BRUTTO-Reward/Tag** (gierig, dichteste Märkte zuerst, max 1× Tiefe):

| Budget | Brutto-Reward/Tag | Brutto-Yield |
|---|---|---|
| 10'000 | ~60 | 0.60%/Tag |
| 100'000 | ~278 | 0.28%/Tag |
| 500'000 | ~751 | 0.15%/Tag |
| 1'000'000 | ~982 | 0.10%/Tag |
| 5'000'000 | ~1'551 | 0.03%/Tag |

**Der Kern-Befund — Dichte-vs-Kapazität-Tradeoff:** Der Yield ZERFÄLLT mit dem
Kapital. Dichte Märkte (guter Yield) sind DÜNN (wenig Kapazität); tiefe Märkte
(viel Kapazität) sind ÜBERFÜLLT (schlechter Yield). Und das ist BRUTTO — netto
(minus Adverse Selection, Backtest ~0.12%/Tag auf den dichtesten) ist deutlich
weniger.

**Ehrliche Ertragserwartung (netto, grob):**
- 6-stelliges Kapital (100-500k): **~50-200 USDC/Tag netto** — echtes
  Nebeneinkommen, nicht 1000.
- **1000/Tag netto braucht 7-stelliges Kapital** — und bei der Verdünnung/
  Crowding auf dieser Ebene ist das Netto NICHT validiert (könnte gegen 0
  gehen). Der Backtest bestätigte netto-positiv nur auf den DICHTEN (dünnen)
  Märkten bei kleinem Einsatz.

**Fazit der ganzen Kette:** Es gibt einen echten, robusten, netto-positiven
Edge (bewiesen, p≈0) — aber seine KAPAZITÄT ist begrenzt. Bei realistischem
Kapital ein solides Nebeneinkommen (zweistellig bis ~200/Tag); 1000/Tag bleibt
kapitalseitig ausserhalb der validierten Zone. Ehrlich: ein guter kleiner
Motor, kein 1000/Tag-Motor.

---

## P6 — Amount-Präzision war zu streng modelliert (07.07.2026)

**Befund aus dem ersten Live-Log nach dem Survivability-/P5-Deploy:** Viele
Zeilen `Gruppe … keine börsenkonforme gemeinsame Size — Gelegenheit
übersprungen`, u.a. auf SPY Up/Down (YES@0.08 + NO@0.897, Größe 6). Kein
Verlust — aber **verlorene Fill-Versuche**: der Bot kam gar nicht erst dazu,
die Order zu posten.

**Ursache (Modellfehler, kein Marktproblem):** Der Vorab-Filter
(`_marketable_size` / `_quantize_fok_groups`) nahm an, der USDC-Betrag
(Size × Preis) dürfe bei BUY nur **2 Nachkommastellen** haben (`mod =
1_000_000`). Die echte Präzision hängt aber am Tick — gespiegelt aus
`py_clob_client_v2.ROUNDING_CONFIG`:

| Tick | erlaubte Betrags-Dezimalen |
|---|---|
| 0.1 | 3 |
| 0.01 | 4 |
| 0.005 / 0.001 | 5 |
| 0.0025 / 0.0001 | 6 |

Für NO@0.897 (Tick 0.001, 5 Dezimalen erlaubt) ist 6 × 0.897 = 5.382 längst
konform — das 2-Dezimal-Modell verlangte aber eine Größe als Vielfaches von
10 Shares und verwarf die Gruppe. **Rechnerisch belegt:** altes Modell
`max k≤600 = 0`, korrektes Modell `= 600` (volle Größe 6).

**Wichtige Nebenerkenntnis:** Bei Tick 0.01 (die Mehrheit der Märkte) ist
Size × Preis mit 2-Dezimal-Size und 2-Dezimal-Preis **immer** ≤ 4 Dezimalen —
d.h. auf dem 0.01-Raster wird **nie** getrimmt oder verworfen. Der ganze
Trimm-Apparat war reine Kompensation der falschen Annahme; er bleibt nur noch
als defensiver Fallback (strenge 2 Dezimalen) für unbekannte Ticks.

**Fix:** `_amount_precision(tick)` (spiegelt ROUNDING_CONFIG), `mod = 10^(8 −
amount_dezimalen)` in beiden Pfaden; Tick pro Bein statt hardcodiert. Tests:
`test_amount_precision_spiegelt_rounding_config`,
`test_marketable_size_haelt_tickabhaengige_praezision_ein`,
`test_fok_gruppe_skewed_preise_wird_nicht_faelschlich_verworfen` (SPY-
Regression), untrimmt-Fall + Fallback-Drop. 512 Tests grün.

**Ehrliche Einordnung (Wie könnte das lügen?):** Das schaltet nur **mehr
Fill-VERSUCHE** frei, keinen Gewinn. Ob die Versuche live tatsächlich beide
Beine füllen (oder FOK weiter alles killt), bleibt die offene Go/No-Go-Frage
— zu klären am nächsten 24h-Log über echte Doppel-Fills.
