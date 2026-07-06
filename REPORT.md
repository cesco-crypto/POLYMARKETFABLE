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
