# Live-Betrieb auf dem MacBook — Schnellstart (Weg A)

Warum MacBook: Die Cloud-Umgebung geht mit US-IP raus — die USA sind bei
Polymarket API-geblockt (403 bei Orders). Die Schweiz steht NICHT auf
Polymarkets Geoblock-Liste; Orders von deiner Schweizer IP sind zulässig.

## Einmalig (5 Minuten)

```bash
# 1. Terminal öffnen, Repo holen:
git clone https://github.com/cesco-crypto/POLYMARKETFABLE.git
cd POLYMARKETFABLE
git checkout claude/polymarket-bot-profit-h6ecwx

# 2. Setup (fragt den Private Key verdeckt ab, prüft alles):
bash scripts/macbook_setup.sh
```

Das Setup prüft der Reihe nach: Python ≥ 3.10 → Abhängigkeiten → Key
(muss auf `0x5cbE…d159` ableiten) → **Erreichbarkeit der Polymarket-API
aus deinem Netz** → Testsuite → Preflight-Plan.

**Falls «NICHT ERREICHBAR» erscheint:** Dein ISP-DNS blockt die Domain
(GESPA-Liste). Öffentlichen DNS-Resolver eintragen: Systemeinstellungen →
Netzwerk → WLAN → Details → DNS → `1.1.1.1` hinzufügen. Danach Setup
erneut laufen lassen.

## Deposit Wallet (einmalig, Pflicht seit Exchange-Upgrade 28.04.2026)

Der V2-CLOB lehnt Orders direkt vom EOA ab («maker address not allowed»).
Gehandelt wird über ein **Deposit Wallet** — einen deterministischen
Smart-Contract-Proxy deines EOA (für `0x5cbE…d159` ist das
`0xd651247C926E627fC87A859b97c1CC885ca219ec`). Signiert wird weiter mit
deinem Key (signature_type 3, POLY_1271); das pUSD muss IM Deposit Wallet
liegen.

1. **Relayer-API-Key holen:** auf polymarket.com mit dem Bot-Wallet
   einloggen → Settings → API Keys → Key erzeugen
   (`polymarket.com/settings?tab=api-keys`). CLOB-API-Creds funktionieren
   beim Relayer NICHT.
2. **`.env` ergänzen** (Adresse zeigt dir der Preflight-Plan an):

   ```env
   POLY_PRIVATE_KEY=0x…           # wie bisher
   POLY_SIGNATURE_TYPE=3          # POLY_1271 (Deposit-Wallet-Flow)
   POLY_FUNDER_ADDRESS=0xd651247C926E627fC87A859b97c1CC885ca219ec
   POLY_RELAYER_API_KEY=…         # aus Schritt 1
   ```

3. **Preflight ausführen:**

   ```bash
   ./.venv/bin/python -m polybot.main preflight            # Plan prüfen
   ./.venv/bin/python -m polybot.main preflight --execute  # ausführen
   ```

   Der Preflight erledigt dann automatisch: Deposit-Wallet-Adresse ableiten
   und on-chain verifizieren → Wallet über den Polymarket-Relayer deployen
   (gasless) → Freigaben aus dem Wallet setzen (signierter Relayer-Batch,
   gasless) → pUSD vom EOA ins Deposit Wallet transferieren (kostet etwas
   POL) → CLOB-Anbindung + Balance-Sync testen. Jeder Send wird vorher per
   eth_call simuliert; jeder Fehler bricht sauber ab.

## Starten

```bash
bash scripts/run_live_macbook.sh
```

- `caffeinate` verhindert, dass macOS den Bot schlafen legt (MacBook darf
  zugeklappt NICHT sein — Deckel offen lassen oder externen Monitor).
- Auto-Neustart bei Netzabriss/Crash; **kein** Neustart nach Kill-Switch.
- Stoppen: `Ctrl-C`.

## Beobachten

```bash
tail -f logs/live.log                                  # laufendes Log
./.venv/bin/python -m polybot.main status              # Portfolio (live_state.json)
./.venv/bin/python -m polybot.main capture-report      # nach ~24h: die Capture-Quote
```

Erster Erfolgsindikator: `Live-Order platziert:`-Zeilen im Log statt
403-Fehlern. Danach sammelt `data/shadow.jsonl` automatisch den
Live/Paper-Vergleich — nach 24 h beantwortet `capture-report` die Frage,
wie viel der Paper-Rate real ist.

## Schutzgeländer (config.live.yaml, Phase 1)

- max. 20 USDC pro Order, 150 USDC Gesamt-Exposure
- **Kill-Switch bei −30 USDC Tagesverlust** (stoppt hart, kein Neustart)
- nur die validierte Komplement-Arbitrage; Auto-Merge vorerst AUS
  (Paare realisieren sich bei Marktauflösung zu 1 USDC — Merge als
  Deposit-Wallet-Batch ist das nächste Arbeitspaket)
- Budget-Obergrenze insgesamt: die 602.94 pUSD auf dem Burner-Wallet

## Sicherheit

- `.env` bleibt lokal (git-ignoriert, chmod 600). Niemals committen/teilen.
- Nach der Testphase Wallet rotieren (neues Konto, Guthaben transferieren),
  da der Key durch den Chat gereicht wurde.
