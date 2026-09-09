# Brand-Vorgaben für Instagram-Reels

Diese Datei wird vor JEDEM Video gelesen. Bitte die Platzhalter in den
eckigen Klammern ausfüllen. Was leer bleibt, wird mit dem Standardwert
(rechts in der Spalte) gefüllt.

## Person / Absender
| Feld | Wert | Standard, falls leer |
| --- | --- | --- |
| Name (Lower-Third) | Francesco Miotti (vorläufig, aus Reel 1) | – (wird nachgefragt) |
| Titel / Rolle (Lower-Third) | AXA Wechselservice (vorläufig, aus Reel 1) | – (wird nachgefragt) |
| Instagram-Handle | [@handle] | – (Follow-Overlay wird weggelassen) |
| Profilbild für Follow-Overlay | [brand/avatar.jpg] | Platzhalter-Avatar |

## Farben (Hex)
| Rolle | Wert | Standard, falls leer |
| --- | --- | --- |
| Primär (Titel, Balken) | #00008F (AXA-Blau, vorläufig) | #22C55E |
| Akzent (Hervorhebung Schlüsselwörter) | #FFD24D (Untertitel) · #FF1721 (AXA-Rot, Zahlen/Kicker) | #FACC15 |
| Hintergrund Overlays / Karten | rgba(8,12,40,0.78) Glas-Karte | #0B0F1A |
| Text auf dunklem Grund | [#RRGGBB] | #FFFFFF |
| Text auf hellem Grund | [#RRGGBB] | #0B0F1A |
| Negativ / Verlust (rote Balken) | [#RRGGBB] | #EF4444 |

## Schriften
| Rolle | Wert | Standard, falls leer |
| --- | --- | --- |
| Titel / Zahlen | [Schriftname, Datei in brand/fonts/] | Inter (liegt in vendor/fonts/) |
| Untertitel | [Schriftname, Datei in brand/fonts/] | Inter |
| Mindestgrösse Untertitel | [px] | 64 px |

Schriftdateien (.woff2 oder .ttf) bitte in `brand/fonts/` ablegen. Ohne
Datei wird Inter verwendet (lokal vorhanden, kein Netz nötig).

## Logo
| Feld | Wert |
| --- | --- |
| Logo-Datei | [brand/logo.svg oder logo.png] |
| Position | [oben links / oben rechts / unten rechts] |
| Abstand vom Rand | [px, Standard 60] |

## Stil-Regeln (Freitext)
- Tonalität: [z. B. sachlich, direkt, Schweizer Hochdeutsch]
- Zahlenformat: [Standard: Schweizer Schreibweise, z. B. 4,5 % · CHF 1'250.–]
- Was NIE vorkommen darf: [z. B. keine Emojis, kein «!!!»]
- Musik: Keine Stock-Musik ohne ausdrückliches OK (Standard: Originalton only).

## Instagram-Sicherheitszonen (fest)
- Oben ca. 250 px frei (Statusleiste, Reel-Titel)
- Unten ca. 350 px frei (Caption, Buttons rechts)
- Rechts ca. 120 px frei (Like/Kommentar/Teilen-Leiste)
- Alle Overlays, Zahlen und Untertitel bleiben innerhalb dieser Zone.
