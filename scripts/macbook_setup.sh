#!/usr/bin/env bash
# Einmaliges Setup für den Live-Bot auf macOS (Weg A).
# Aufruf:  bash scripts/macbook_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Polybot MacBook-Setup =="

# 1) Python >= 3.10 finden (Homebrew-Pythons zuerst, dann System)
PY=""
for cand in python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "FEHLER: Kein Python >= 3.10 gefunden. Installieren mit:  brew install python@3.12"
  exit 1
fi
echo "Python: $($PY --version)"

# 2) Virtualenv + Abhängigkeiten
if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
./.venv/bin/pip -q install --upgrade pip
./.venv/bin/pip -q install -r requirements.txt
echo "Abhängigkeiten installiert."

# 3) .env anlegen (Key wird verdeckt eingelesen, niemals geloggt)
if [ ! -f .env ]; then
  echo
  echo "Private Key des POLYBOT-Wallets (Eingabe bleibt unsichtbar):"
  read -rs PK
  umask 077
  printf 'POLY_PRIVATE_KEY=%s\nPOLY_SIGNATURE_TYPE=0\n' "$PK" > .env
  unset PK
  echo ".env angelegt (chmod 600, git-ignoriert)."
else
  echo ".env existiert bereits — unverändert gelassen."
fi

# 4) Schlüssel verifizieren (muss auf 0x5cbE…d159 ableiten)
./.venv/bin/python - <<'EOF'
from dotenv import load_dotenv; load_dotenv('.env')
import os, sys
from eth_account import Account
a = Account.from_key(os.environ["POLY_PRIVATE_KEY"]).address
ok = a.lower() == "0x5cbed94234eae9cbc0cea21c7c9c933c8a5ad159"
print(f"Abgeleitete Adresse: {a}  ->  {'OK' if ok else 'FALSCHER KEY!'}")
sys.exit(0 if ok else 1)
EOF

# 5) Erreichbarkeit der Polymarket-API von DIESEM Netz aus
#    (GESPA-DNS-Sperre des Schweizer ISPs würde hier sichtbar)
echo
echo "== Erreichbarkeits-Check =="
for host in clob.polymarket.com gamma-api.polymarket.com; do
  if curl -sS -m 10 -o /dev/null -w "%{http_code}" "https://$host/" >/dev/null 2>&1; then
    echo "  $host: erreichbar"
  else
    echo "  $host: NICHT ERREICHBAR."
    echo "    Falls die Namensauflösung scheitert, blockt vermutlich der ISP-DNS."
    echo "    Öffentlicher Resolver hilft: Systemeinstellungen -> Netzwerk -> DNS -> 1.1.1.1"
    exit 1
  fi
done

# 6) Testsuite (30s) + Preflight im Plan-Modus (sendet nichts)
./.venv/bin/python -m pytest tests/ -q
./.venv/bin/python -m polybot.main preflight

echo
echo "== Setup fertig. Live starten mit:  bash scripts/run_live_macbook.sh =="
