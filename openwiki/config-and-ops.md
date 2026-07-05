# Konfiguration & Betrieb

## `config.py`

`BotConfig.load(path)`:

- `path=None` ⇒ `config.yaml` probieren, Fehlen tolerieren (Defaults).
  Ein EXPLIZITER `--config`-Pfad MUSS existieren (Tippfehler dürfen den
  Bot nicht mit Default-Limits starten).
- Validierung wirft `SystemExit` mit klarer Meldung (Limits ≥ 0,
  Fee-Raten-Bereiche, verbotene Kombination Waisen-Detektor +
  market_making, Live-Modus verlangt `POLY_PRIVATE_KEY`).
- Sektionen: `RiskConfig` (Limits, Kill-Switch, Fees, Cooldowns,
  `flatten_orphan_grace_s`, `live_auto_merge`) und `StrategyConfig`
  (enabled, Scan-/Stream-Parameter, MM-Parameter).

## Config-Dateien

- `config.paper.yaml` — Messbot: 100k Paper-Cash, weite Limits, alle
  Strategien, `min_edge` 0.002.
- `config.live.yaml` — Phase 1 Mikro-Kapital: NUR `complement_arb`,
  `max_order_usdc: 20`, Tagesverlust-Kill-Switch 30 USDC,
  `min_edge: 0.005`, `paper_start_cash` knapp UNTER dem echten
  pUSD-Guthaben (Cash-Deckungscheck gibt nie mehr frei als real da ist),
  `live_auto_merge: false` (Deposit-Wallet!), `flatten_orphan_grace_s: 60`.

## Secrets — NUR als Umgebungsvariablen

| Variable | Zweck |
|---|---|
| `POLY_PRIVATE_KEY` | EOA-Signer-Key (Pflicht für live) |
| `POLY_FUNDER_ADDRESS` | Deposit-/Proxy-Wallet-Adresse |
| `POLY_SIGNATURE_TYPE` | 0=EOA, 1=PROXY, 2=GNOSIS_SAFE, 3=POLY_1271 |
| `POLY_RPC_URL` | Polygon-RPC (Default polygon-rpc.com) |

NIE in Dateien im Repo, NIE im Chat, NIE in diese Wiki. Auf dem
MacBook liegen sie in einer lokalen `.env` (git-ignoriert), geladen vom
Launcher-Skript.

## Live-Betrieb (MacBook des Betreibers)

- Setup: `scripts/macbook_setup.sh`; Start: `scripts/run_live_macbook.sh`
  (mit caffeinate-Schlafschutz). Anleitung: `MACBOOK_LIVE.md`.
- Update-Zyklus: Ctrl-C → `git pull` → Skript neu starten. Es gibt kein
  Auto-Deploy auf das MacBook — Codeänderungen wirken dort erst nach
  diesem manuellen Pull.
- Log: `logs/live.log`. Normaler Startverlauf: 400 auf
  `POST /auth/api-key` («Could not create api key») ist HARMLOS —
  direkt danach folgt `derive-api-key 200` (Key existiert schon).
- Rechtslage (Schweiz/GESPA): `docs/POLYMARKET_GUIDELINES.md` — live nur
  mit expliziter Freigabe des Betreibers.

## Remote-Messbetrieb (Claude-Code-Container)

Paper-Messbot läuft im Container (`logs/paper.log`); Container sind
FLÜCHTIG — alles Behaltenswerte committen/pushen (REPORT.md, Code).
Der Messbot-Prozess startet je Zyklus frisch; Code-Verbesserungen landen
automatisch im nächsten Zyklus.
