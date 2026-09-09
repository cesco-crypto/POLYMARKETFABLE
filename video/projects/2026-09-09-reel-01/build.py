#!/usr/bin/env python3
"""Baut index.html für das Reel aus transcript.cut.json + Overlay-Plan.
Alle Zahlen in Overlays stammen wörtlich aus dem Transkript (numbers.md)."""
import json, re, html

W, H = 1080, 1920
words = json.load(open("transcript.cut.json"))
DUR = 63.95

def t_of(text, nth=0):
    hits = [w for w in words if w["text"].strip(".,!?").lower() == text.lower()]
    return hits[nth]["start"]

# ---------- Untertitel-Gruppen ----------
FIX = {"Axa-Wechselservice": "AXA Wechselservice", "Axa": "AXA", "Axa,": "AXA,",
       "600-700": "600–700", "erspart": "ersparst", "Bschicken": "B schicken",
       "7,4%": "7,4 %"}
EMPH = re.compile(r"^(7,4 %|BAG|224|600–700|Franken|Franken\.|günstigste|automatisiert|AXA|AXA,|Link|WhatsApp\.|Krankenkassenprämien|Mehrkosten|Grundversicherung|akzeptieren|Familie|Familie,)$")
groups, cur = [], []
def flush():
    global cur
    if cur: groups.append(cur); cur = []
prev_end = None
for w in words:
    txt = FIX.get(w["text"], w["text"])
    ww = {**w, "text": txt}
    gap = (w["start"] - prev_end) if prev_end is not None else 0
    chars = sum(len(x["text"]) + 1 for x in cur)
    if cur and (gap > 0.6 or len(cur) >= 4 or chars + len(txt) > 22):
        flush()
    cur.append(ww); prev_end = w["end"]
    if re.search(r"[.!?,]$", txt) and len(cur) >= 2: flush()
flush()

cap_html, cap_tl = [], []
for i, g in enumerate(groups):
    start = round(g[0]["start"] - 0.08, 3); end = round(g[-1]["end"] + 0.12, 3)
    if i + 1 < len(groups): end = min(end, groups[i+1][0]["start"] - 0.08 - 0.02)
    dur = max(0.5, round(end - start, 3))
    spans = " ".join(f'<span class="w{" em" if EMPH.match(x["text"]) else ""}" id="w{i}-{j}">{html.escape(x["text"])}</span>' for j, x in enumerate(g))
    cap_html.append(f'<div class="clip cap" id="cap{i}" data-start="{start}" data-duration="{dur}" data-track-index="5"><div class="capbox">{spans}</div></div>')
    cap_tl.append(f'tl.fromTo("#cap{i} .capbox", {{opacity: 0, y: 14}}, {{opacity: 1, y: 0, duration: 0.18, ease: "power2.out"}}, {start});')
    for j, x in enumerate(g):
        if EMPH.match(x["text"]):
            cap_tl.append(f'tl.fromTo("#w{i}-{j}", {{scale: 1}}, {{scale: 1.08, duration: 0.12, yoyo: true, repeat: 1, ease: "power1.inOut"}}, {round(x["start"],3)});')

# ---------- Overlay-Zeiten aus dem Transkript ----------
t_hook = t_of("7,4%") - 0.4          # 7,4 %
t_bag  = t_of("BAG") - 0.1
t_224  = t_of("224") - 0.4
t_fam  = t_of("600-700") - 0.5
t_axa  = t_of("Axa-Wechselservice") - 0.2
t_cut1 = 29.04; t_cut1_end = 32.63   # Punch-in zwischen den Schnitten
t_auto = t_of("jedes") - 0.2
t_rech = t_of("Rechnungen") - 0.6
t_cta  = t_of("Klicke") - 0.2
t_end  = DUR

overlays = f'''
<div class="clip card lt" id="lt" data-start="0.5" data-duration="{round(t_hook-0.5-0.1,2)}" data-track-index="3">
  <div class="lt-bar"></div>
  <div class="lt-text"><div class="lt-name">Francesco Miotti</div><div class="lt-role">AXA Wechselservice</div></div>
</div>

<div class="clip card stat" id="hook" data-start="{t_hook:.2f}" data-duration="{t_224-0.3-t_hook:.2f}" data-track-index="3">
  <div class="kicker">Krankenkassenprämien</div>
  <div class="big red"><span id="hook-num">+0,0</span> %</div>
    <div class="src" id="hook-src">Quelle: BAG, Bundesamt für Gesundheit</div>
</div>

<div class="clip card stat" id="s224" data-start="{t_224:.2f}" data-duration="{t_fam-0.2-t_224:.2f}" data-track-index="3">
  <div class="kicker">Mehrkosten pro Person und Jahr</div>
  <div class="big">CHF <span id="s224-num">0</span></div>
  <div class="src">Durchschnitt pro Jahr</div>
</div>

<div class="clip card bars" id="bars" data-start="{t_fam:.2f}" data-duration="{t_axa-0.2-t_fam:.2f}" data-track-index="3">
  <div class="kicker">Mehrkosten pro Jahr</div>
  <div class="row"><div class="lab">1 Person</div><div class="track"><div class="bar b1"></div></div><div class="val">CHF 224</div></div>
  <div class="row"><div class="lab">Familie (4)</div><div class="track"><div class="bar b2 red-bg"></div></div><div class="val red">CHF 600–700</div></div>
</div>

<div class="clip title" id="t-axa" data-start="{t_axa:.2f}" data-duration="{t_cut1+0.6-t_axa:.2f}" data-track-index="3">
  <div class="title-in"><span class="pill">AXA Wechselservice</span></div>
</div>

<div class="clip title" id="t-auto" data-start="{t_auto:.2f}" data-duration="5.2" data-track-index="3">
  <div class="title-in"><span class="pill blue">Günstigste Grundversicherung</span><span class="pill-sub">jedes Jahr, automatisch</span></div>
</div>

<div class="clip title" id="t-rech" data-start="{t_rech:.2f}" data-duration="{t_cta-0.4-t_rech:.2f}" data-track-index="3">
  <div class="title-in"><span class="pill blue">Kein Rechnungs-Hin-und-Her</span></div>
</div>

<div class="clip card cta" id="cta" data-start="{t_cta:.2f}" data-duration="{t_end-t_cta:.2f}" data-track-index="3">
  <div class="cta-line"><span class="arrow">↓</span> Link hier unten</div>
  <div class="cta-line small">Mehr Infos? Schreib uns auf WhatsApp</div>
</div>
'''

tl_extra = f'''
// Lower-Third
tl.fromTo("#lt .lt-bar", {{scaleY: 0}}, {{scaleY: 1, duration: 0.35, ease: "power3.out"}}, 0.5);
tl.fromTo("#lt .lt-text", {{opacity: 0, x: -30}}, {{opacity: 1, x: 0, duration: 0.45, ease: "power3.out"}}, 0.65);
tl.to("#lt .lt-text", {{opacity: 0, x: -20, duration: 0.25, ease: "power2.in"}}, {t_hook-0.4:.2f});
tl.to("#lt .lt-bar", {{scaleY: 0, duration: 0.25, ease: "power2.in"}}, {t_hook-0.35:.2f});

// Hook 7,4 %
cardIn("#hook", {t_hook:.2f});
countUp("#hook-num", 0, 7.4, 1.4, {t_hook+0.25:.2f}, 1, "+");
tl.fromTo("#hook-src", {{opacity: 0}}, {{opacity: 1, duration: 0.3}}, {t_bag:.2f});
cardOut("#hook", {t_224-0.6:.2f});

// CHF 224
cardIn("#s224", {t_224:.2f});
countUp("#s224-num", 0, 224, 1.2, {t_224+0.25:.2f}, 0, "");
cardOut("#s224", {t_fam-0.5:.2f});

// Balken 224 vs 600-700 (Skala 0-700)
cardIn("#bars", {t_fam:.2f});
tl.fromTo("#bars .b1", {{scaleX: 0}}, {{scaleX: {224/700:.3f}, duration: 0.9, ease: "power2.out"}}, {t_fam+0.3:.2f});
tl.fromTo("#bars .b2", {{scaleX: 0}}, {{scaleX: {650/700:.3f}, duration: 1.1, ease: "power2.out"}}, {t_fam+0.7:.2f});
cardOut("#bars", {t_axa-0.5:.2f});

// Keyword-Titel
titleIn("#t-axa", {t_axa:.2f}); titleOut("#t-axa", {t_cut1+0.3:.2f});
titleIn("#t-auto", {t_auto:.2f}); titleOut("#t-auto", {t_auto+4.9:.2f});
titleIn("#t-rech", {t_rech:.2f}); titleOut("#t-rech", {t_cta-0.7:.2f});

// CTA
cardIn("#cta", {t_cta:.2f});
tl.fromTo("#cta .arrow", {{y: -10}}, {{y: 10, duration: 0.5, ease: "power1.inOut", yoyo: true, repeat: 7}}, {t_cta+0.4:.2f});
'''

page = f'''<!doctype html>
<html lang="de" data-resolution="portrait">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={W}, height={H}" />
<script src="assets/vendor/gsap.min.js"></script>
<style>
@font-face {{ font-family: "Inter"; src: url("assets/vendor/fonts/Inter-Variable.woff2") format("woff2"); font-weight: 100 900; font-style: normal; font-display: block; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {W}px; height: {H}px; overflow: hidden; background: #000; }}
body {{ font-family: "Inter", "Helvetica Neue", Arial, sans-serif; color: #fff; }}
#root {{ position: relative; width: {W}px; height: {H}px; background: #000; overflow: hidden; }}
#video-wrap {{ position: absolute; inset: 0; width: {W}px; height: {H}px; transform-origin: 50% 40%; }}
#a-roll {{ width: {W}px; height: {H}px; object-fit: cover; display: block; }}

/* Sicherheitszone: oben 250, unten 350, rechts 120 */
.card {{ position: absolute; left: 80px; width: 880px; }}
.lt {{ top: 1070px; display: flex; align-items: stretch; gap: 22px; height: 150px; }}
.lt-bar {{ width: 12px; height: 150px; background: #FF1721; border-radius: 6px; transform-origin: top; }}
.lt-text {{ display: flex; flex-direction: column; justify-content: center; }}
.lt-name {{ font-size: 62px; font-weight: 800; letter-spacing: -0.02em; line-height: 1.05; text-shadow: 0 2px 12px rgba(0,0,0,.6); }}
.lt-role {{ font-size: 34px; font-weight: 500; color: #dbe4ff; margin-top: 8px; text-shadow: 0 2px 10px rgba(0,0,0,.6); }}

.stat, .bars, .cta {{ top: 1040px; background: rgba(8, 12, 40, 0.78); border: 2px solid rgba(255,255,255,0.12); border-radius: 36px; padding: 30px 44px; backdrop-filter: blur(14px); }}
.kicker {{ font-size: 32px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: #b9c4ff; }}
.big {{ font-size: 132px; font-weight: 900; letter-spacing: -0.03em; line-height: 1.05; margin-top: 8px; font-variant-numeric: tabular-nums; }}
.red {{ color: #FF3B47; }}
.sub {{ font-size: 38px; font-weight: 500; color: #e6eaff; margin-top: 6px; }}
.src {{ font-size: 26px; color: #9aa6d9; margin-top: 14px; }}

.bars .row {{ display: grid; grid-template-columns: 230px 1fr 290px; align-items: center; gap: 20px; margin-top: 30px; }}
.bars .lab {{ font-size: 32px; font-weight: 600; }}
.bars .track {{ position: relative; height: 44px; background: rgba(255,255,255,0.12); border-radius: 22px; overflow: hidden; }}
.bars .bar {{ position: absolute; left: 0; top: 0; height: 44px; width: 100%; border-radius: 22px; background: #6f86ff; transform-origin: left center; }}
.bars .red-bg {{ background: #FF3B47; }}
.bars .val {{ font-size: 36px; font-weight: 800; text-align: right; font-variant-numeric: tabular-nums; }}

.title {{ position: absolute; left: 80px; top: 1060px; width: 880px; }}
.title-in {{ display: flex; flex-direction: column; align-items: flex-start; gap: 14px; }}
.pill {{ display: inline-block; padding: 18px 34px; border-radius: 20px; background: #FF1721; color: #fff; font-size: 52px; font-weight: 800; letter-spacing: -0.01em; line-height: 1.15; }}
.pill.blue {{ background: #00008F; }}
.pill-sub {{ display: inline-block; padding: 10px 26px; border-radius: 16px; background: rgba(8,12,40,0.8); font-size: 36px; font-weight: 600; }}

.cta {{ top: 1060px; text-align: center; }}
.cta-line {{ font-size: 60px; font-weight: 800; line-height: 1.2; }}
.cta-line.small {{ font-size: 36px; font-weight: 500; color: #dbe4ff; margin-top: 14px; }}
.arrow {{ display: inline-block; color: #FF3B47; }}

/* Untertitel-Schiene */
.cap {{ position: absolute; left: 80px; top: 1350px; width: 880px; height: 210px; display: flex; align-items: center; justify-content: center; }}
.capbox {{ display: inline-block; max-width: 880px; padding: 16px 32px; border-radius: 22px; background: rgba(0,0,0,0.55); font-size: 64px; font-weight: 700; line-height: 1.2; text-align: center; letter-spacing: -0.01em; text-shadow: 0 2px 8px rgba(0,0,0,.5); }}
.w {{ display: inline-block; }}
.w.em {{ color: #FFD24D; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{DUR}" data-width="{W}" data-height="{H}">
  <div id="video-wrap">
    <video id="a-roll" class="clip" src="assets/cut.mp4" data-start="0" data-duration="{DUR}" data-track-index="0" muted playsinline></video>
  </div>
  <audio id="a-roll-audio" src="assets/cut.mp4" data-start="0" data-duration="{DUR}" data-track-index="10" data-volume="1"></audio>

  <div id="overlays-host" data-composition-id="overlays" data-composition-src="compositions/overlays.html" data-start="0" data-duration="{DUR}" data-track-index="3" data-width="{W}" data-height="{H}"></div>
  <div id="captions-host" data-composition-id="captions" data-composition-src="compositions/captions.html" data-start="0" data-duration="{DUR}" data-track-index="5" data-width="{W}" data-height="{H}"></div>
</div>

<script>
const tl = gsap.timeline({{ paused: true }});
function cardIn(sel, t) {{ tl.fromTo(sel, {{opacity: 0, y: 40, scale: 0.96}}, {{opacity: 1, y: 0, scale: 1, duration: 0.45, ease: "power3.out"}}, t); }}
function cardOut(sel, t) {{ tl.to(sel, {{opacity: 0, y: -30, duration: 0.3, ease: "power2.in"}}, t); }}
function titleIn(sel, t) {{ tl.fromTo(sel + " .title-in", {{opacity: 0, x: -60}}, {{opacity: 1, x: 0, duration: 0.4, ease: "power3.out"}}, t); }}
function titleOut(sel, t) {{ tl.to(sel + " .title-in", {{opacity: 0, x: 40, duration: 0.3, ease: "power2.in"}}, t); }}
function countUp(sel, from, to, dur, t, decimals, prefix) {{
  const o = {{ v: from }}; const el = document.querySelector(sel);
  tl.to(o, {{ v: to, duration: dur, ease: "power2.out", onUpdate: () => {{ el.textContent = prefix + o.v.toFixed(decimals).replace(".", ","); }} }}, t);
}}
// Punch-in verdeckt die zwei Schnitte
tl.set("#video-wrap", {{scale: 1.07}}, {t_cut1:.2f});
tl.set("#video-wrap", {{scale: 1}}, {t_cut1_end:.2f});
window.__timelines["main"] = tl;
</script>
</body>
</html>
'''
open("index.html", "w").write(page)
import re as _re
css = _re.search(r"<style>(.*?)</style>", page, _re.S).group(1)
fontface = _re.search(r"(@font-face \{.*?\})", css, _re.S).group(1)
helpers = """function cardIn(sel, t) { tl.fromTo(sel, {opacity: 0, y: 40, scale: 0.96}, {opacity: 1, y: 0, scale: 1, duration: 0.45, ease: "power3.out"}, t); }
function cardOut(sel, t) { tl.to(sel, {opacity: 0, y: -30, duration: 0.3, ease: "power2.in"}, t); }
function titleIn(sel, t) { tl.fromTo(sel + " .title-in", {opacity: 0, x: -60}, {opacity: 1, x: 0, duration: 0.4, ease: "power3.out"}, t); }
function titleOut(sel, t) { tl.to(sel + " .title-in", {opacity: 0, x: 40, duration: 0.3, ease: "power2.in"}, t); }
function countUp(sel, from, to, dur, t, decimals, prefix) {
  const o = { v: from }; const el = document.querySelector(sel);
  tl.to(o, { v: to, duration: dur, ease: "power2.out", onUpdate: () => { el.textContent = prefix + o.v.toFixed(decimals).replace(".", ","); } }, t);
}"""
def subcomp(cid, body, script, tidx):
    return f'''<!doctype html>
<html lang="de"><head><meta charset="UTF-8" /></head><body>
<template>
<div id="{cid}" data-composition-id="{cid}" data-start="0" data-duration="{DUR}" data-width="{W}" data-height="{H}">
<style>
{fontface}
#{cid} {{ position: relative; width: {W}px; height: {H}px; font-family: "Inter", "Helvetica Neue", Arial, sans-serif; color: #fff; }}
{css.split("/* Sicherheitszone: oben 250, unten 350, rechts 120 */")[1].split("/* Untertitel-Schiene */")[0] if cid == "overlays" else css.split("/* Untertitel-Schiene */")[1]}
</style>
{body}
<script>
(function () {{
const tl = gsap.timeline({{ paused: true }});
{script}
window.__timelines["{cid}"] = tl;
}})();
</script>
</div>
</template>
</body></html>
'''
open("compositions/overlays.html", "w").write(subcomp("overlays", overlays, helpers + tl_extra, 3))
open("compositions/captions.html", "w").write(subcomp("captions", chr(10).join(cap_html), chr(10).join(cap_tl), 5))
print(f"groups={len(groups)} hook={t_hook:.2f} bag={t_bag:.2f} 224={t_224:.2f} fam={t_fam:.2f} axa={t_axa:.2f} auto={t_auto:.2f} rech={t_rech:.2f} cta={t_cta:.2f}")
