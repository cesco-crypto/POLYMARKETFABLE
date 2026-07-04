# Wie die Top-Trader auf Polymarket wirklich Geld verdienen

Forensische Analyse vom 4. Juli 2026, auf Basis der offiziellen
Polymarket-Leaderboard-API (`lb-api.polymarket.com` — dieselbe Datenquelle,
die polymarketanalytics.com/traders anzeigt) und der öffentlichen
Trade-Historien der Top-Wallets (`data-api.polymarket.com/trades`).

## Die Fakten: Ja, es gibt Trader mit weit über 1000 $/Tag

Tages-Leaderboard (PnL, 04.07.2026):

| Rang | Trader | Tages-PnL | Tagesvolumen |
|---|---|---|---|
| 1 | coldsway | +3'698'563 $ | 19.3 Mio. $ |
| 2 | muchobliged | +677'343 $ | 1.5 Mio. $ |
| 3 | swisstony | +536'228 $ | 19.9 Mio. $ |
| 8 | weflyhigh | +202'905 $ | 0.56 Mio. $ |
| 15 | … | sechsstellig | … |

## Wie sie es machen — drei Archetypen (aus den echten Trade-Historien)

### Typ 1: Der Kapital-Wal mit Modell-Edge (coldsway, muchobliged)
- **136 Trades in 7 Tagen, Median-Ticket 62'789 $, Maximum 926'025 $.**
- Handelt fast ausschließlich WM-2026-Fußballmärkte (die WM läuft gerade):
  Siegmärkte, Draw-Märkte, Team-Advance.
- Kauft bei mittleren Quoten (Median-Preis 0.45) — kein Lotterie-Ticket-
  Kauf, sondern systematische Wetten, wo sein Modell die Wahrscheinlichkeit
  höher sieht als der Markt.
- **Mechanik: 19 Mio. $ Volumen × ~19 % Marge = 3.7 Mio. $ Tag.** Der Edge
  kommt aus einem Wahrscheinlichkeitsmodell (oder besserer Information),
  die Größe aus dem Kapital. Ohne beides funktioniert dieser Typ nicht.

### Typ 2: Der In-Play-HFT-Bot (swisstony)
- **2769 Trades pro STUNDE, Median-Ticket 73 $** — eindeutig ein Bot.
- Handelt live laufende Spiele (Over/Under, Spread, Exact Score des
  laufenden Marokko-Spiels) im Sekundentakt.
- **Mechanik: Geschwindigkeit.** Bei jedem Tor/Ereignis preist er die
  Märkte schneller neu als die restlichen Teilnehmer — Tausende kleine
  Gewinne. 19.9 Mio. $ Volumen × ~2.7 % Marge = 536 k$/Tag.
  Das ist die maschinelle Variante — braucht schnelle Datenfeeds
  (Live-Sportdaten schneller als der Markt) und beste Infrastruktur.

### Typ 3: Der selektive Event-Trader (weflyhigh)
- Wenige, mittelgroße Positionen (0.56 Mio. $ Volumen → 203 k$ PnL,
  36 % Marge) in ausgewählten Sport-Events.
- Mechanik: Research-Edge in wenigen Märkten statt Breite.

## Die ehrliche Einordnung

1. **Ein Leaderboard zeigt die Gewinner eines Nullsummenspiels** (nach
   Gebühren sogar negativ-summig). Für jeden Dollar auf dieser Liste hat
   jemand anderes einen Dollar verloren. Die Verlierer haben keine
   Analytics-Seite.
2. **Alle drei Archetypen brauchen mindestens eines von beiden:**
   - **Kapital** (Typ 1 und 3: fünf- bis siebenstellige Einsätze), oder
   - **überlegene Geschwindigkeit/Daten** (Typ 2: Live-Sportfeeds,
     Colocation, mikrosekundenschnelle Ausführung).
3. **1000 $/Tag ist damit real erreichbar — als Größenordnung:**
   - Typ-1-Weg: ~2 % Tagesmarge auf ~50'000 $ eingesetztes Kapital,
     mit einem Modell, das den Markt schlägt. Kapitalrisiko: echt.
   - Typ-2-Weg: tausende Mikro-Trades — braucht die schnellste
     Infrastruktur im Raum; genau dorthin zielen unsere
     WebSocket-/Vollabdeckungs-Upgrades.
   - Risikofreier Arb (unser Startpunkt) ist der Einstieg mit dem
     geringsten Risiko, aber auch der am härtesten umkämpfte — der
     Opportunity-Recorder misst gerade, wie viel er real hergibt.

## Konsequenz für unseren Bot

- Kurzfristig: WebSocket-Speed + Vollmarkt-Scan + Maker-Rebates
  (in Arbeit) — das ist die Infrastruktur, die Typ 2 auszeichnet.
- Der Opportunity-Recorder liefert die Datenbasis für die Frage:
  «Welches Kapital braucht die gemessene Gelegenheitsdichte für
  1000 $/Tag?» — statt Behauptung eine Messung.
- Typ-1-Erträge (Modell-Edge auf Sport/Events) sind KEIN
  Infrastruktur-Problem, sondern ein Prognose-Problem: Dafür bräuchte es
  ein Wahrscheinlichkeitsmodell, das besser ist als der Marktkonsens —
  das verspricht kein seriöser Bot per Knopfdruck.

## Quellen
- https://polymarketanalytics.com/traders (Frontend; blockt Bots per 403)
- https://lb-api.polymarket.com/leaderboard?window=1d&rankType=pnl (Rohdaten)
- https://data-api.polymarket.com/trades?user=0x... (Trade-Historien der Top-Wallets)
