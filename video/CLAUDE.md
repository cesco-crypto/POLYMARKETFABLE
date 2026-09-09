# Video-Produktion — Instagram-Reels mit HyperFrames

Dieser Ordner (`video/`) ist die Produktionsumgebung. Alles hier ist vom
Trading-Bot im Repo-Root unabhängig.

## Auslöser
Sagt der Nutzer «neues Video» (oder legt ein Rohvideo in `input/` und
bittet um ein Reel), läuft der Ablauf unten in genau dieser Reihenfolge.
Zwischenstopps, an denen auf den Nutzer gewartet wird, sind mit ⏸ markiert.
Alles andere läuft ohne Rückfrage durch.

## Ordner
| Ordner | Inhalt |
| --- | --- |
| `input/` | Rohvideos des Nutzers (git-ignoriert) |
| `output/` | fertige MP4s (`<projektname>.mp4`) |
| `projects/<name>/` | ein HyperFrames-Projekt pro Video |
| `brand/brand.md` | Farben, Schriften, Name, Handle, Logo — IMMER zuerst lesen |
| `vendor/` | lokale Kopien von GSAP und Inter (Rendern ohne CDN) |
| `scripts/new-project.sh` | legt ein Projekt mit Standard-Bausteinen an |
| `.agents/skills/` | HyperFrames-Skills (`/hyperframes`, `/talking-head-recut`, `/embedded-captions`, `/media-use`, `/motion-graphics`, …) |

## Feste Regeln
1. Vor jedem Video `brand/brand.md` lesen und Farben/Schriften anwenden.
   Leere Platzhalter → Standardwert aus der Tabelle; fehlen Name/Titel für
   das Lower-Third, nachfragen.
2. Keine Stock-Musik, kein TTS, keine SFX ohne ausdrückliches OK. Standard
   ist der Originalton des Rohvideos.
3. Nie Zahlen erfinden. Jede Zahl in einem Overlay muss wörtlich im
   Transkript stehen (mit Zeitstempel belegbar). Whisper-Halluzinationen
   (z. B. «Vielen Dank» in Pausen) streichen.
4. Format: 9:16, 1080×1920, 30 fps, H.264 MP4, maximal 90 s.
5. Sicherheitszonen: oben 250 px, unten 350 px, rechts 120 px frei.
   Overlays, Zahlen und Untertitel nur innerhalb dieser Zone.
6. Rendern erst nach dem OK des Nutzers (⏸ in Schritt 7).
7. Vor jedem Render `npx hyperframes check` mit 0 Findings.
8. Fertige MP4s per SendUserFile an den Nutzer schicken UND committen +
   pushen (Container ist flüchtig).

## Umgebungs-Besonderheiten (Cloud-Container)
- Immer `export HYPERFRAMES_SKIP_SKILLS=1` setzen (verhindert Skill-Netzabfragen bei `init`).
- Der Render-Browser erreicht CDNs (jsdelivr, Google Fonts) NICHT. Darum:
  GSAP und Schriften ausschliesslich lokal aus `assets/vendor/` laden.
  `scripts/new-project.sh` erledigt das inkl. Umschreiben der CDN-Links
  in installierten Katalog-Bausteinen. Neu installierte Bausteine
  (`npx hyperframes add …`) danach ebenfalls umschreiben.
- Transkription: whisper.cpp ist gebaut, Modell `small` (multilingual)
  liegt im Cache. Nie ein `.en`-Modell verwenden (übersetzt Deutsch ins
  Englische).
- Parakeet (macOS-only) und Docker sind hier nicht verfügbar; lokal rendern.

## Ablauf für jedes neue Video

### 1. Rohvideo erkennen, Projekt anlegen, transkribieren
```bash
cd video && export HYPERFRAMES_SKIP_SKILLS=1
RAW=$(ls -t input/*.mp4 input/*.mov input/*.MOV 2>/dev/null | head -1)   # neuestes Video
NAME=$(date +%Y-%m-%d)-<kurzes-thema>                                     # kebab-case
bash scripts/new-project.sh "$NAME" "$RAW"
cd "projects/$NAME"
ffprobe -v error -show_entries stream=width,height,r_frame_rate,duration -of default=nw=1 assets/source.mp4
ffmpeg -y -i assets/source.mp4 -vn -ac 1 -ar 16000 audio.wav -loglevel error
npx hyperframes transcribe audio.wav -d . --json --model small --language de
npx hyperframes transcribe transcript.json --to srt -o transcript.srt
```
Skill dazu: `/media-use` (Transkription, Referenz `audio/references/transcribe.md`).
Ergebnis: `transcript.json` (Wort-Array mit Zeitstempeln) + `transcript.srt`.
Transkript lesen und offensichtliche ASR-Fehler korrigieren (Schweizer
Kontext: Franken/CHF, Rappen, Kantone, AHV, BVG, Säule 3a, Pensionskasse,
Hypothek, Steuern, Orts- und Firmennamen). Ist das Transkript unbrauchbar:
einmal mit `--model medium` wiederholen, sonst dem Nutzer melden.

### 2. Zahlen extrahieren ⏸
Aus dem Transkript alle Zahlen, Prozentwerte, Franken-Beträge und
Vergleiche («doppelt so viel», «von … auf …», «statt …») als Liste zeigen:

| # | Zeit | Wortlaut im Transkript | Wert(e) | Vorschlag Overlay |
| --- | --- | --- | --- | --- |

Vorschlag pro Zeile: Balken (Vergleich zweier Werte), Zähler (ein Wert),
Verlauf (von→auf). ⏸ Nutzer bestätigt, welche Zeilen animiert werden.
Liste auch als `numbers.md` im Projekt speichern.

### 3. Schnittplan
- Pausen > 0,7 s entfernen (Lücken zwischen `end` und nächstem `start` im Transkript).
- Versprecher, Wiederholungen («äh», doppelt gesagte Sätze) entfernen.
- Hook: In den ersten 3 s muss die stärkste Aussage/Zahl stehen. Wenn der
  Hook später im Video liegt, ihn nach vorn ziehen.
- Plan als `cutplan.json` speichern: `[{ "start": s, "end": s, "note": "…" }]`
  (nur die behaltenen Segmente, in Reihenfolge).
- Schnitt ausführen mit ffmpeg (Segmente schneiden + concat, Ton bleibt
  Originalton) nach `assets/cut.mp4`, danach Transkript-Zeitstempel auf das
  geschnittene Video ummappen (`transcript.cut.json`). Referenz:
  `/hyperframes-core` → `references/creator-editing-recipes.md`.
- Hochformat: Ist das Rohvideo 16:9, zentriert auf 9:16 croppen
  (`crop=ih*9/16:ih`), Gesicht muss im Bild bleiben (Kontaktbogen prüfen).

### 4. Overlays mit `/talking-head-recut`
Skill lesen: `.agents/skills/talking-head-recut/SKILL.md`. Canvas 9:16.
Pflicht-Overlays:
- Lower-Third mit Name/Titel aus `brand.md` in den ersten 2–4 s.
- An jeder bestätigten Stelle aus Schritt 2: animierter Balken/Zähler auf
  Basis des `data-chart`-Blocks (`compositions/data-chart.html`), Werte
  exakt aus dem Transkript, Schweizer Schreibweise (4,5 % · CHF 1'250.–).
- Keyword-Titel bei jedem Themenwechsel (2–4 Wörter, Primärfarbe).
- Optional am Ende: `instagram-follow`-Block mit Handle aus `brand.md`
  (nur wenn Handle ausgefüllt ist).
- Übergänge: `compositions/components/directional-wipe.html`, sparsam.
Alle Overlays innerhalb der Sicherheitszone. `npx hyperframes check --snapshots`
laufen lassen, Snapshots ansehen.

### 5. Untertitel mit `/embedded-captions`
Skill lesen: `.agents/skills/embedded-captions/SKILL.md`. Rail-Stil
(`anchor`) als Standard, keine Effekt-Captions ohne Wunsch.
- Schrift ≥ 64 px, weiss auf halbtransparentem dunklem Balken, mittig,
  unterhalb des Gesichts, oberhalb der unteren Sicherheitszone.
- Schlüsselwörter (Zahlen, Beträge, Kernbegriffe) in der Akzentfarbe.
- Wörtlich nach Transkript, Deutsch mit Schweizer Schreibweise (ss statt ß).

### 6. Format-Check
1080×1920, 30 fps, ≤ 90 s. Ist der Schnitt länger als 90 s, weitere
Kürzungen vorschlagen (nicht stumm kürzen).

### 7. Vorschau ⏸ und Render
```bash
npx hyperframes preview --background     # URL dem Nutzer geben
npx hyperframes preview --status
```
⏸ Erst nach OK des Nutzers:
```bash
npx hyperframes render --quality high --fps 30 --output ../../output/<name>.mp4
npx hyperframes preview --stop
```
Dann: MP4 per SendUserFile schicken, `output/<name>.mp4` + Projekt
committen und pushen.

## Testlauf (Referenz, funktioniert)
`projects/test/` → `output/test.mp4`: Titel «Test», Balken 0 → 4,5 %,
5 s, 1080×1920, 30 fps. Kette init → check → render ist verifiziert.
