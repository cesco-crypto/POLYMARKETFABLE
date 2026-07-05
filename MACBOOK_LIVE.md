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
- nur die validierte Komplement-Arbitrage; Merges on-chain automatisch
- Budget-Obergrenze insgesamt: die 602.94 pUSD auf dem Burner-Wallet

## Sicherheit

- `.env` bleibt lokal (git-ignoriert, chmod 600). Niemals committen/teilen.
- Nach der Testphase Wallet rotieren (neues Konto, Guthaben transferieren),
  da der Key durch den Chat gereicht wurde.
