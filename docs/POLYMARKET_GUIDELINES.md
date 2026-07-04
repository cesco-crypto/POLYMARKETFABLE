# Polymarket-Guidelines — Forensische Recherche (Stand: 4. Juli 2026)

Zusammenfassung der offiziellen Polymarket-Regeln aus Primärquellen:
Terms of Use (gültig ab 1. Juni 2026, Betreiber: Adventure One QSS, Inc.),
docs.polymarket.com, help.polymarket.com und offiziellen Pressemitteilungen.
Jede Aussage ist am Ende belegt.

---

## 1. Sind Trading-Bots erlaubt? — JA

- Die ToS enthalten **kein Verbot von automatisiertem Handel**. Verboten ist
  nur Daten-*Scraping* der Website („data mining tools, robots, crawlers …
  to scrape") und Überlastung der Dienste (DoS-artiges Verhalten).
- Polymarket unterstützt Bots aktiv: öffentliche CLOB-API mit dokumentierten
  Rate-Limits, ein offizielles **Builder-Programm** und offizielle
  Agent-Repos (`Polymarket/agents`, `Polymarket/agent-skills`).
- **Aber:** „Capital Market Clients" (professionelle Firmen: Market Maker,
  Prop-Trading-Firmen, Hedgefonds, Broker …) dürfen die Daten laut ToS nur
  mit schriftlicher Vereinbarung nutzen. Privatpersonen fallen nicht unter
  diese Definition.

## 2. Verbotene Handelspraktiken (ToS §2, „Market Integrity Rules" v. 20.03.2026)

Ausdrücklich verboten — der Bot in diesem Repo tut nichts davon:

| Verbot | Bedeutung |
|---|---|
| Spoofing | Orders ohne Ausführungsabsicht platzieren |
| Wash Trading | Mit sich selbst handeln / Scheinumsatz erzeugen |
| Front-Running | Vor fremden Orders laufen |
| Fiktive Transaktionen, Cornering | Scheingeschäfte, Marktecken |
| Insiderhandel | Handel auf vertrauliche Infos / bei eigener Einflussmacht auf das Ereignis |
| Kollusion | Absprachen zur Preisbeeinflussung |
| Manipulatives Trading | Generalklausel, Ermessen von Polymarket |
| **VPN-Nutzung** | „privacy or anonymization tools" zur Umgehung von Restriktionen |

Durchsetzung: Kontosperrung, Ausschluss von Rewards, ggf. Zusammenarbeit
mit Strafverfolgung. Meldungen: integrity@polymarket.com.

## 3. Jurisdiktionen — wichtig für die Schweiz 🇨🇭

- **Polymarket selbst sperrt die Schweiz NICHT**: Sie steht weder in der
  ToS-Liste der „Restricted Jurisdictions" noch in der Geoblock-API.
- **ABER:** Die Schweizer Aufsichtsbehörde **GESPA** hat polymarket.com als
  unlizenziertes Geldspiel auf ihre Zugangssperrliste gesetzt (seit
  Nov. 2024) — Schweizer ISPs blockieren die Domain per DNS. Die Nutzung
  eines VPN zur Umgehung verstößt gegen die Polymarket-ToS und ändert
  nichts an der schweizerischen Rechtslage (Geldspielgesetz).
  **→ Vor Live-Betrieb aus der Schweiz die eigene Rechtslage klären.**
- Von Polymarket gesperrt (kein Eröffnen neuer Positionen, u. a.):
  USA, UK, Deutschland, Frankreich, Italien, Belgien, Niederlande (Frontend),
  Polen, Singapur, Australien, Taiwan, Thailand, Russland u. a.
  Vollsperre: Iran, Syrien, Kuba, Nordkorea, Krim/Donezk/Luhansk.
- USA: nur über die separate, CFTC-regulierte „Polymarket US" (QCX LLC,
  volles KYC), nicht über die internationale Plattform.
- Die Geoblock-Prüfung greift auch auf API-Ebene: „Orders submitted from
  blocked regions will be rejected."

## 4. KYC

- Internationale Plattform: **kein KYC** (Wallet + Geo-IP-Prüfung).
  Polymarket kann aber jederzeit Nachweise verlangen, dass man keine
  „Restricted Person" ist.
- Polymarket US: volles CFTC-KYC (Ausweis, SSN, Adressnachweis, Selfie).

## 5. Gebühren (seit März 2026 — relevant für jede Strategie!)

- **Maker: 0 %** — immer. **Taker:** `fee = Shares × feeRate × p × (1 − p)`
  (Maximum bei p = 0.50, gegen 0 an den Rändern):

| Kategorie | feeRate | Max. pro 100 Shares |
|---|---|---|
| Krypto | 0.07 | $1.75 |
| Economics/Culture/Weather/Other | 0.05 | $1.25 |
| Finance/Politik/Mentions/Tech | 0.04 | $1.00 |
| Sport | 0.03 | $0.75 |
| **Geopolitik** | **0.00** | gebührenfrei |

- 20–25 % der Taker-Gebühren fließen als **Maker-Rebates** (täglich in pUSD)
  an Liquiditätsgeber — ein echter, offizieller Verdienstkanal für
  Market-Making-Bots.
- Ein-/Auszahlungen: keine Polymarket-Gebühren; Trading via UI/Relayer ist
  gasfrei (subventioniert).

## 6. Technik-Basics (Details in der API-Doku)

- Handel läuft auf **Polygon (Chain-ID 137)**, hybrid: Off-Chain-Matching,
  On-Chain-Settlement, Orders als EIP-712-Signaturen. Non-custodial.
- **Exchange-Upgrade 28.04.2026:** neue V2-Contracts, Collateral ist jetzt
  **pUSD** (1:1-USDC-Wrapper) statt USDC.e. Alte v1-Clients sind
  inkompatibel → dieses Repo nutzt `py-clob-client-v2`.
- Ordertypen: GTC, GTD, FOK, FAK. Tick-Größen je Markt (meist 0.01),
  Mindestordergröße je Markt (meist 5 Shares).
- Rate-Limits (Auszug, pro 10 s): CLOB gesamt 9 000, `/book` 1 500,
  Order-POST 5 000 (Burst). Überschreitung wird gedrosselt, nicht gebannt.
- **NegRisk-Events** (Multi-Outcome): genau ein Outcome gewinnt; 1 NO-Share
  ist via NegRiskAdapter in je 1 YES-Share aller anderen Outcomes
  konvertierbar. NegRisk-Orders brauchen `neg_risk=True` (eigene
  Signatur-Domain).

## 7. Marktauflösung

- Auflösung durch das **UMA Optimistic Oracle**: Vorschlag mit Bond
  (typisch $750), 2-Stunden-Widerspruchsfenster, bei Streit Eskalation zur
  UMA-Token-Holder-Abstimmung (~48 h). Selten „50/50"-Auflösung möglich
  (jeder Token zahlt $0.50) — Restrisiko auch für „risikofreie" Arbitrage.
- Polymarket selbst ist an Auflösungsstreitigkeiten nicht beteiligt;
  Ergebnisse sind final.

---

## Quellen

- Terms of Use (eff. 01.06.2026): https://polymarket.com/tos
- Geoblock-API: https://docs.polymarket.com/api-reference/geoblock
- Geo-Restriktionen (Help Center): https://help.polymarket.com/en/articles/13364163-geographic-restrictions
- Gebühren: https://docs.polymarket.com/polymarket-learn/trading/fees und https://help.polymarket.com/en/articles/13364478-trading-fees
- Maker-Rebates: https://docs.polymarket.com/market-makers/maker-rebates
- Rate-Limits: https://docs.polymarket.com/api-reference/rate-limits
- UMA-Auflösung: https://docs.polymarket.com/developers/resolution/UMA
- NegRisk: https://docs.polymarket.com/advanced/neg-risk
- Builder-Programm: https://docs.polymarket.com/builders/overview
- Exchange-Upgrade V2 (28.04.2026): https://help.polymarket.com/en/articles/14762452-polymarket-exchange-upgrade-april-28-2026
- pUSD: https://docs.polymarket.com/concepts/pusd
- Contracts: https://docs.polymarket.com/resources/contracts
- Market-Integrity-Regeln (20.03.2026): https://www.businesswire.com/news/home/20260320997513/en/
- Offizielle Agent-Repos: https://github.com/Polymarket/agents , https://github.com/Polymarket/agent-skills
- GESPA-Zugangssperren (Schweiz): https://www.gespa.ch/en/fighting-illegal-gambling/access-blocking
