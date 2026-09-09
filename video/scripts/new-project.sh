#!/usr/bin/env bash
# Legt ein neues HyperFrames-Reel-Projekt an: 9:16, 1080x1920, Standard-Bausteine,
# lokal vendorte GSAP + Inter (kein CDN-Zugriff beim Rendern nötig).
#
# Aufruf:  bash scripts/new-project.sh <projektname> [pfad/zum/rohvideo.mp4]
set -euo pipefail

NAME="${1:?Projektname fehlt}"
VIDEO="${2:-}"
VIDEO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$VIDEO_ROOT/projects/$NAME"
export HYPERFRAMES_SKIP_SKILLS=1

if [ -e "$PROJECT_DIR/hyperframes.json" ]; then
  echo "Projekt existiert bereits: $PROJECT_DIR"
  exit 1
fi

# 1) Scaffold (Hochformat, leer). Rohvideo wird als source.mp4 kopiert,
#    Transkription läuft separat mit deutschem Modell (siehe CLAUDE.md Schritt 1).
(cd "$VIDEO_ROOT" && npx hyperframes init "projects/$NAME" --non-interactive \
  --example=blank --resolution portrait --skill=talking-head-recut >/dev/null)

# 2) Standard-Bausteine aus dem Katalog
for item in data-chart instagram-follow directional-wipe; do
  (cd "$PROJECT_DIR" && npx hyperframes add "$item" --no-clipboard --json >/dev/null)
done

# 3) Lokale Kopien von GSAP und Inter, CDN-Referenzen umbiegen
mkdir -p "$PROJECT_DIR/assets/vendor/fonts"
cp "$VIDEO_ROOT/vendor/gsap.min.js" "$PROJECT_DIR/assets/vendor/"
cp "$VIDEO_ROOT/vendor/fonts/"*.woff2 "$PROJECT_DIR/assets/vendor/fonts/"
if [ -d "$VIDEO_ROOT/brand/fonts" ]; then
  find "$VIDEO_ROOT/brand/fonts" -maxdepth 1 -type f \( -name '*.woff2' -o -name '*.ttf' -o -name '*.otf' \) \
    -exec cp {} "$PROJECT_DIR/assets/vendor/fonts/" \;
fi
find "$PROJECT_DIR" -name '*.html' -print0 | xargs -0 sed -i \
  -e 's#https://cdn.jsdelivr.net/npm/gsap@[0-9.]*/dist/gsap.min.js#assets/vendor/gsap.min.js#g'

# 4) Rohvideo übernehmen
if [ -n "$VIDEO" ]; then
  mkdir -p "$PROJECT_DIR/assets"
  cp "$VIDEO" "$PROJECT_DIR/assets/source.mp4"
  echo "Rohvideo kopiert nach $PROJECT_DIR/assets/source.mp4"
fi

echo "Projekt angelegt: $PROJECT_DIR"
echo "Bausteine: data-chart, instagram-follow, directional-wipe (compositions/)"
echo "Nächster Schritt: siehe video/CLAUDE.md, Schritt 1 (Transkription)."
