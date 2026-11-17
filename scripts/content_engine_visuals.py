#!/usr/bin/env python3
"""Content engine visuals: a post's graphic or carousel, in your brand, as files ready for LinkedIn.

1080x1350 (4:5), light slides or dark slides with an accent glow, your fonts, your logo bottom left, page dots on
carousels. Colours, fonts and logos come from [visuals] in config.toml. No kicker or eyebrow label over a headline:
it is the stock template look.

A spec file lists the visuals for one batch:
  {"batch": "2026-10-27_Engine_Batch_1", "visuals": [
     {"post": 3, "theme": "light", "slides": [{"type": "cover", "headline": "...", "sub": "...", "art": "none"}]}, ...]}
Slide types: cover (headline, sub, cards, art), pain (title, lines, note), steps (title, steps[]), bars (title, bars[]),
donut (headline, title, bars[]), cta (headline, sub). Text comes from the post itself: never a claim the post doesn't make.
Art: 'portrait' (the author's photo, [photos] reference_photo), 'none', or a drawing of your own from [visuals] art_dir
(name.svg plus name.json, see templates/art/).

Out: PNG per slide (+ one PDF for a carousel) in <cache_dir>/visuals/<batch>/.

Usage:
  python3 scripts/content_engine_visuals.py data/batches/<batch>_visuals.json [--post 3] [--upload]
  python3 scripts/content_engine_visuals.py data/batches/<batch>.json --auto
"""
import argparse
import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

V = lambda k: C.get("visuals", k)  # noqa: E731
LOGO = {"light": V("logo_light"), "dark": V("logo_dark")}
OUT = C.CACHE / "visuals"
PORTRAIT = C.get("photos", "reference_photo")  # a cut-out photo of the author, the face on story cards
NAVY, LIME, CLOUD, INK2, LINE = V("navy"), V("accent"), V("cloud"), V("ink2"), V("line")
W, H = 1080, 1350
DRIVE_STATE = C.STATE / "drive.json"  # folder ids the engine made in Drive (Graphics, per-batch folders)


def _uri(p: str) -> str:
    return C.path(p).as_uri() if p and C.path(p).exists() else ""


def _rgba(hex_colour: str, a: float) -> str:
    h = hex_colour.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


CSS = """
@font-face { font-family: P; src: url('FONT_REGULAR'); font-weight: 400; }
@font-face { font-family: P; src: url('FONT_BOLD'); font-weight: 700; }
@font-face { font-family: P; src: url('FONT_LIGHT'); font-weight: 300; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { width: 1080px; height: 1350px; overflow: hidden; font-family: P, sans-serif; position: relative; }
body.light { background: CLOUD; color: NAVY; }
body.dark { background: NAVY; color: CLOUD; }
body.dark::before { content: ''; position: absolute; left: -260px; top: 520px; width: 900px; height: 900px; border-radius: 50%;
  background: radial-gradient(circle, GLOW, rgba(0,0,0,0) 65%); }
.pad { position: absolute; inset: 104px 96px 210px 96px; display: flex; flex-direction: column; }
h1 { font-weight: 700; font-size: 100px; line-height: 1.04; letter-spacing: -.02em; margin-top: 34px; }
h1.m { font-size: 82px; }
.bars { flex: 1; display: flex; align-items: flex-end; gap: 14px; margin-top: 40px; padding-bottom: 8px; border-bottom: 3px solid rgba(154,166,184,.45); }
.bar { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar i { display: block; width: 100%; background: LIME; border-radius: 8px 8px 0 0; }
.bar .v { font-size: 22px; font-weight: 700; margin-bottom: 10px; white-space: nowrap; }
.donut { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 28px; }
.donut svg { width: 560px; height: 560px; }
.donut .dc { font-family: P; font-weight: 700; font-size: 130px; fill: currentColor; }
.dl { display: flex; flex-direction: column; gap: 12px; font-size: 32px; align-self: stretch; }
.dl i { display: inline-block; width: 26px; height: 26px; border-radius: 6px; margin-right: 14px; vertical-align: -3px; }
.dl b { font-weight: 700; }
.blabels { display: flex; gap: 14px; margin-top: 14px; }
.blabels span { flex: 1; text-align: center; font-size: 22px; opacity: .8; }
h2 { font-weight: 700; font-size: 72px; line-height: 1.08; letter-spacing: -.015em; margin-top: 30px; }
.sub { font-size: 38px; line-height: 1.42; margin-top: 34px; }
body.light .sub { color: INK2; } body.dark .sub { color: #C9D2DF; }
.pill { align-self: flex-start; margin-top: 46px; font-weight: 700; font-size: 32px; padding: 22px 42px; border-radius: 999px; }
body.light .pill { background: NAVY; color: CLOUD; } body.dark .pill { background: LIME; color: NAVY; }
.art { flex: 1; display: flex; align-items: center; justify-content: center; min-height: 0; padding-top: 30px; }
.art svg { width: 100%; height: 100%; }
.art.portrait img { height: 100%; max-height: 560px; aspect-ratio: 1; object-fit: cover; border-radius: 50%; }
.foot { position: absolute; left: 70px; right: 96px; bottom: 64px; display: flex; justify-content: space-between; align-items: center; }
.foot img { width: 128px; height: 128px; object-fit: contain; }
.dots { display: flex; gap: 12px; } .dots i { display: block; width: 58px; height: 10px; border-radius: 5px; }
body.light .dots i { background: LINE; } body.dark .dots i { background: rgba(248,250,252,.22); }
.dots i.on { background: LIME !important; }
.steps { margin-top: 56px; display: flex; flex-direction: column; gap: 30px; }
.step { display: flex; gap: 30px; align-items: flex-start; }
.step b { flex: none; width: 76px; height: 76px; border-radius: 50%; background: LIME; color: NAVY; font-size: 38px; display: flex; align-items: center; justify-content: center; }
.step span { font-size: 38px; line-height: 1.38; padding-top: 6px; }
.cards { margin-top: 56px; display: grid; grid-template-columns: 1fr 1fr; gap: 26px; }
.card { border-radius: 28px; padding: 38px 34px; }
body.dark .card { background: rgba(248,250,252,.07); border: 2px solid rgba(248,250,252,.14); }
body.light .card { background: #FFFFFF; border: 2px solid LINE; }
.card .v { font-weight: 700; font-size: 58px; letter-spacing: -.01em; line-height: 1.1; }
.card .v small { font-size: 32px; font-weight: 400; }
.cards.two { margin-top: 90px; }
.cards.two .card { padding: 56px 40px; }
.cards.two .card .v { font-size: 150px; line-height: 1; }
.cards.two .card .l { font-size: 36px; }
.card .l { font-size: 30px; line-height: 1.35; margin-top: 14px; }
body.dark .card .l { color: #C9D2DF; } body.light .card .l { color: INK2; }
.pains { margin-top: 48px; display: flex; flex-direction: column; gap: 22px; align-items: flex-start; }
.pn { position: relative; font-size: 40px; line-height: 1.28; padding: 24px 38px 24px 40px; border-radius: 30px 30px 30px 8px; max-width: 100%; }
.pn.q { font-weight: 700; padding-left: 96px; }
.pn.q::before { content: '\\201C'; position: absolute; left: 30px; top: 10px; font-size: 104px; line-height: 1; font-weight: 700; color: LIME; }
.pn:nth-child(2) { margin-left: 44px; } .pn:nth-child(4) { margin-left: 44px; }
body.dark .pn { background: rgba(248,250,252,.08); border: 2px solid rgba(248,250,252,.16); }
body.light .pn { background: #FFFFFF; border: 2px solid LINE; }
.note + .note { margin-top: 18px; }
.note { margin-top: 40px; font-size: 34px; line-height: 1.4; display: flex; gap: 18px; align-items: flex-start; }
.note::before { content: ''; flex: none; width: 18px; height: 18px; border-radius: 50%; background: LIME; margin-top: 15px; }
"""
for k, v in {"FONT_REGULAR": _uri(V("font_regular")), "FONT_BOLD": _uri(V("font_bold")), "FONT_LIGHT": _uri(V("font_light")),
             "GLOW": _rgba(LIME, .30), "CLOUD": CLOUD, "NAVY": NAVY, "LIME": LIME, "INK2": INK2, "LINE": LINE}.items():
    CSS = CSS.replace(k, v)


def _load_art() -> dict[str, dict]:
    """Your own drawings: <art_dir>/<name>.svg, with <name>.json saying what it shows ("description") and when it fits
    ("fits": a list of word groups; the post must contain a word from every group). {NAVY}, {ACCENT}, {CLOUD}, {INK2}
    and {LINE} in the SVG take your brand colours."""
    folder = V("art_dir")
    out = {}
    if not folder or not C.path(folder).is_dir():
        return out
    for svg in sorted(C.path(folder).glob("*.svg")):
        meta_file = svg.with_suffix(".json")
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        body = svg.read_text()
        for k, v in {"{NAVY}": NAVY, "{ACCENT}": LIME, "{CLOUD}": CLOUD, "{INK2}": INK2, "{LINE}": LINE}.items():
            body = body.replace(k, v)
        out[svg.stem] = {"svg": body, "description": meta.get("description", ""), "fits": meta.get("fits", [])}
    return out


ART = _load_art()

SPEC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["visuals"], "properties": {"visuals": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["post", "theme", "slides"], "properties": {
        "post": {"type": "integer"}, "theme": {"type": "string", "enum": ["light", "dark"]},
        "slides": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["type", "headline", "sub", "art", "title", "steps", "lines", "note", "size", "bars", "cards"],
            "properties": {"type": {"type": "string", "enum": ["cover", "pain", "steps", "bars", "donut", "cta"]},
                           "cards": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["value", "label"],
                                     "properties": {"value": {"type": "string"}, "label": {"type": "string"}}}},
                           "bars": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["label", "value"],
                                    "properties": {"label": {"type": "string"}, "value": {"type": "string"}}}},
                           "lines": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"},
                           "headline": {"type": "string"}, "sub": {"type": "string"},
                           "art": {"type": "string", "enum": ["none", "portrait"] + sorted(ART)},
                           "title": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}},
                           "size": {"type": "string", "enum": ["", "m"]}}}}}}}}}
_ART_LINES = "".join(f"\n- Art {k} only for a post about this: {a['description']}" for k, a in ART.items() if a["description"])
SPEC_SYSTEM = f"""You design LinkedIn graphics for {C.get('author', 'name') or 'the author'}'s Danish posts, one visual per
post, in their brand (the layout is fixed; you only choose words, slide type and art). Rules:
- Every word on an image comes from the post itself: copy short lines from the post, word for word. Never a claim,
  number or phrase the post doesn't have. Danish, with æ, ø, å, no long dashes.
- One cover per post. A carousel (cover + 'steps' slides + a 'cta' slide with the post's own ask) only when the post has
  real steps or a list. Right after a carousel's cover comes a 'pain' slide when the post names the reader's problem:
  title = the post's line that names it, lines = 2 to 4 short lines from the post on what goes wrong today and what it
  costs the reader (quotes keep their quote marks), note = optional, one or two lines from the post (newline between).
  Headline: the post's strongest line, at most 70 characters (size 'm' when it is over 40). Sub: optional, one short
  line from the post.
- Art: 'portrait' (the author's own photo) for stories about the author, their clients or their work, or 'none' for a
  pure statement.{_ART_LINES}
- A post with a series of its own numbers over time (months, years) gets a 'bars' slide right after the cover: title =
  a short line from the post, bars = 4 to 8 points, label short ("Sep 25"), value exactly as the post writes it
  ("12.500 kr."). Never a list of numbers as 'steps'.
- A post whose key number is a share of a whole (40 %, 300 of 750) gets a 'donut' slide: headline = the share as the
  post writes it, at most 8 characters ("75 %", "9 af 12"), title = a short line from the post, bars = the parts
  (label, value exactly as in the post), the part the post is about first. A single big number on plain text is the
  boring version; avoid it.
- Never a bare cover. A cover with art 'none' gets 2 to 4 cards: the post's own key numbers, value exactly as the post
  writes it ("27", "64.000 kr."), label 2 to 5 words from the post. A cover with no numbers in the post gets 'portrait'.
- Mix light and dark across the batch. Fields you don't use are '' or []."""


def image_rules() -> str:
    """The author's own rules about images, from the rulebook the feedback flow keeps (data/kb/voice_rules.md,
    "## Images and video"), so feedback on graphics reaches the next batch without a code change."""
    f = C.KB / "voice_rules.md"
    m = re.search(r"## Images and video\n(.*?)(?=\n## |\Z)", f.read_text(), re.S) if f.exists() else None
    return ("\n\nThe author's own rules for images, learned from their feedback (they win over the rules above):\n" + m.group(1).strip()) if m else ""


def _art_fits(art: str, post: str) -> bool:
    """A drawing that carries its own words only goes on the post it was drawn for: every word group in its "fits"
    needs a word in the post."""
    if art not in ART:
        return True
    t = post.lower()
    return all(any(w.lower() in t for w in group) for group in ART[art]["fits"])


def _from_post(text: str, post: str) -> bool:
    """Every image text must be the post's own words: 85%+ of its words appear in the post."""
    words = [w for w in re.findall(r"[\wæøåÆØÅ]+", text.lower()) if len(w) > 2]
    have = set(re.findall(r"[\wæøåÆØÅ]+", post.lower()))
    return not words or sum(w in have for w in words) / len(words) >= 0.85


def auto_spec(batch_file: pathlib.Path) -> pathlib.Path:
    """A graphic for every post of a batch that has none yet, except a video post: the video is the post and its
    cover is a frame of it."""
    import llm_guard
    from anthropic import Anthropic
    posts = json.loads(batch_file.read_text())
    spec_file = batch_file.with_name(batch_file.stem + "_visuals.json")
    spec = json.loads(spec_file.read_text()) if spec_file.exists() else {"batch": batch_file.stem, "visuals": []}
    have = {x["post"] for x in spec["visuals"]}
    todo = [(i, p) for i, p in enumerate(posts, 1) if i not in have and p.get("status") != "dropped" and p.get("format") != "video"]
    if not todo:
        return spec_file
    ask = "\n\n".join(f"Post {i} ({p['format']}; visual note: {p.get('visual', '')}):\n{p['text']}" for i, p in todo)
    client = llm_guard.guarded(Anthropic())

    def ask_llm(text):
        msg = client.messages.create(model=C.get("llm", "model"), max_tokens=6000, system=SPEC_SYSTEM + image_rules(),
                                     messages=[{"role": "user", "content": text}],
                                     output_config={"format": {"type": "json_schema", "schema": SPEC_SCHEMA}})
        return json.loads(msg.content[0].text)["visuals"]

    def not_own(made):
        bad = []
        for v in made:
            post = posts[v["post"] - 1]["text"]
            for sl in v["slides"]:
                bad += [(v["post"], sl[k]) for k in ("headline", "sub", "title") if sl.get(k) and not _from_post(sl[k], post)]
                bad += [(v["post"], st) for st in (sl.get("steps") or []) + (sl.get("lines") or []) + [sl.get("note") or ""]
                        if st and not _from_post(st, post)]
        return bad

    made = ask_llm(ask)
    bad = not_own(made)
    if bad:  # one repair round, then anything still not the post's own words is dropped
        made = ask_llm(ask + "\n\nYour last answer used words that are not in the posts. Copy the post's own lines instead:\n" +
                       "\n".join(f"post {p}: '{t}'" for p, t in bad))
    for v in made:
        post = posts[v["post"] - 1]["text"]
        first = next(l.strip() for l in post.splitlines() if l.strip())
        for sl in v["slides"]:
            if sl.get("art") in ART and not _art_fits(sl["art"], post):
                sl["art"] = "none"
            if sl.get("art") == "portrait" and not _uri(PORTRAIT):
                sl["art"] = "none"
            for k in ("sub", "title"):
                if sl.get(k) and not _from_post(sl[k], post):
                    sl[k] = ""
            if sl.get("headline") and not _from_post(sl["headline"], post):
                sl["headline"] = first if len(first) <= 70 else first[:70].rsplit(" ", 1)[0]
            sl["steps"] = [st for st in sl.get("steps") or [] if _from_post(st, post)]
            sl["lines"] = [st for st in sl.get("lines") or [] if _from_post(st, post)]
            if sl.get("note") and not _from_post(sl["note"], post):
                sl["note"] = ""
        v["slides"] = [sl for sl in v["slides"] if (sl["type"] != "steps" or sl["steps"]) and (sl["type"] != "pain" or sl["lines"]) and (sl["type"] not in ("bars", "donut") or sl["bars"])]
        for sl in v["slides"]:
            for key in [k for k, val in sl.items() if val in ("", [])]:
                sl.pop(key)
    spec["visuals"] = sorted(spec["visuals"] + [v for v in made if v["post"] not in have], key=lambda x: x["post"])
    spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1))
    return spec_file


def esc(s: str) -> str:
    return html.escape(s or "")


def slide_html(slide: dict, theme: str, page: int, pages: int) -> str:
    t = slide["type"]
    body = ""
    if t in ("cover", "cta"):
        body += f'<h1 class="{slide.get("size", "")}">{esc(slide["headline"])}</h1>'
        if slide.get("sub"):
            body += f'<p class="sub">{esc(slide["sub"])}</p>'
        if slide.get("cards"):  # the post's own key numbers as tiles: a cover is never bare
            body += f'<div class="cards{" two" if len(slide["cards"]) <= 2 else ""}">' + "".join(f'<div class="card"><div class="v">{esc(c["value"])}</div><div class="l">{esc(c["label"])}</div></div>'
                                                    for c in slide["cards"][:4]) + "</div>"
        if slide.get("pill"):
            body += f'<div class="pill">{esc(slide["pill"])}</div>'
        if slide.get("art") == "portrait" and _uri(PORTRAIT):
            body += f'<div class="art portrait"><img src="{_uri(PORTRAIT)}" alt=""></div>'
        elif slide.get("art") in ART:
            body += f'<div class="art">{ART[slide["art"]]["svg"]}</div>'
    elif t == "steps":
        body += f'<h2>{esc(slide["title"])}</h2><div class="steps">' + "".join(
            f'<div class="step"><b>{i}</b><span>{esc(s)}</span></div>' for i, s in enumerate(slide["steps"], 1)) + "</div>"
    elif t == "donut":  # a share of a whole; the first part in the accent colour
        vals = [(b["label"], b["value"], float(re.sub(r"[^\d,]", "", b["value"]).replace(",", ".") or 0)) for b in slide["bars"]]
        total, r, c, at, arcs = sum(v for _, _, v in vals) or 1, 230, 2 * 3.14159265 * 230, 0.0, ""
        colors = [LIME, "#9AA6B8", "#5B6B85", "#3A4A64"]
        for k, (_, _, v) in enumerate(vals):
            ln = c * v / total
            arcs += (f'<circle cx="300" cy="300" r="{r}" fill="none" stroke="{colors[k % 4]}" stroke-width="96" '
                     f'stroke-dasharray="{ln:.1f} {c - ln:.1f}" stroke-dashoffset="{-at:.1f}" transform="rotate(-90 300 300)"/>')
            at += ln
        body += (f'<h2>{esc(slide["title"])}</h2><div class="donut"><svg viewBox="0 0 600 600">{arcs}'
                 f'<text x="300" y="300" text-anchor="middle" dominant-baseline="central" class="dc" '
                 f'style="font-size:{min(130, int(330 / (0.62 * max(1, len(slide.get("headline", ""))))))}px">{esc(slide.get("headline", ""))}</text></svg>'
                 '<div class="dl">' + "".join(f'<div><i style="background:{colors[k % 4]}"></i><b>{esc(shown)}</b> {esc(l)}</div>'
                                               for k, (l, shown, _) in enumerate(vals)) + "</div></div>")
        if slide.get("sub"):
            body += f'<p class="note">{esc(slide["sub"])}</p>'
    elif t == "bars":  # a real number series, one accent bar each
        vals = [(b["label"], b["value"], float(re.sub(r"[^\d,]", "", b["value"]).replace(",", ".") or 0)) for b in slide["bars"]]
        top = max(v for _, _, v in vals) or 1
        body += f'<h2>{esc(slide["title"])}</h2><div class="bars">' + "".join(
            f'<div class="bar"><span class="v">{esc(shown)}</span><i style="height:{max(3, 100 * v / top):.1f}%"></i></div>'
            for _, shown, v in vals) + '</div><div class="blabels">' + "".join(f"<span>{esc(l)}</span>" for l, _, _ in vals) + "</div>"
        if slide.get("note"):
            body += f'<p class="note">{esc(slide["note"])}</p>'
    elif t == "pain":  # what goes wrong for the reader today
        quote = lambda x: x.strip()[:1] in ('"', "“", "„", "«")
        body += f'<h2>{esc(slide["title"])}</h2><div class="pains">' + "".join(
            f'<div class="pn{" q" if quote(x) else ""}">{esc(x.strip().strip(chr(34) + "“”„«»") if quote(x) else x)}</div>'
            for x in slide["lines"]) + "</div>"
        body += "".join(f'<p class="note">{esc(n)}</p>' for n in (slide.get("note") or "").split("\n") if n.strip())
    elif t == "cards":
        body += f'<h2>{esc(slide["title"])}</h2><div class="cards">' + "".join(
            f'<div class="card"><div class="v">{esc(v).replace("/md.", "<small>/md.</small>")}</div><div class="l">{esc(l)}</div></div>'
            for v, l in slide["cards"]) + "</div>"
        if slide.get("note"):
            body += f'<p class="note">{esc(slide["note"])}</p>'
    dots = "" if pages < 2 else '<div class="dots">' + "".join(
        f'<i class="{"on" if i == page else ""}"></i>' for i in range(pages)) + "</div>"
    logo = f'<img src="{_uri(LOGO[theme])}" alt="{esc(V("brand_name"))}">' if _uri(LOGO[theme]) else "<span></span>"
    return (f'<!doctype html><html lang="da"><head><meta charset="utf-8"><style>{CSS}</style></head>'
            f'<body class="{theme}"><div class="pad">{body}</div>'
            f'<div class="foot">{logo}{dots}</div></body></html>')


def render(spec_file: pathlib.Path, only: int | None = None) -> list[pathlib.Path]:
    from playwright.sync_api import sync_playwright
    spec = json.loads(spec_file.read_text())
    out = OUT / spec["batch"]
    out.mkdir(parents=True, exist_ok=True)
    made = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        for v in spec["visuals"]:
            if only and v["post"] != only:
                continue
            pngs = []
            for i, s in enumerate(v["slides"]):
                f = out / f"post{v['post']}_{i + 1}.html"
                f.write_text(slide_html(s, v.get("theme", "light"), i, len(v["slides"])))
                pg.goto(f.as_uri())
                pg.evaluate("document.fonts.ready")
                png = f.with_suffix(".png")
                pg.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": W, "height": H})
                f.unlink()
                pngs.append(png)
            made += pngs
            if len(pngs) > 1:  # a carousel: LinkedIn takes it as one PDF document
                from PIL import Image
                imgs = [Image.open(x).convert("RGB") for x in pngs]
                pdf = out / f"post{v['post']}_carousel.pdf"
                imgs[0].save(pdf, save_all=True, append_images=imgs[1:], resolution=144)
                made.append(pdf)
        b.close()
    return made


def _google(api: str, version: str):
    import google_api
    return google_api.service(api, version)


def _folder(drive, name: str, parent: str) -> dict:
    q = f"name = '{name}' and '{parent}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    hit = drive.files().list(q=q, fields="files(id, webViewLink)").execute().get("files")
    return hit[0] if hit else drive.files().create(body={"name": name, "parents": [parent], "mimeType": "application/vnd.google-apps.folder"},
                                                   fields="id, webViewLink").execute()


def graphics_folder(drive, batch: str) -> dict:
    """<engine folder>/Graphics/<batch> in Drive, made on first use."""
    graphics = _folder(drive, "Graphics", C.need("google", "drive_root_id"))
    st = json.loads(DRIVE_STATE.read_text()) if DRIVE_STATE.exists() else {}
    if st.get("graphics") != graphics["id"]:
        st["graphics"] = graphics["id"]
        DRIVE_STATE.parent.mkdir(parents=True, exist_ok=True)
        DRIVE_STATE.write_text(json.dumps(st, indent=1))
    return _folder(drive, batch, graphics["id"])


def upload(spec_file: pathlib.Path, files: list[pathlib.Path]) -> dict:
    """Files into '<engine folder>/Graphics/<batch>' (same name = new version), links into the doc and batch file."""
    from googleapiclient.http import MediaFileUpload
    spec = json.loads(spec_file.read_text())
    drive = _google("drive", "v3")
    folder = graphics_folder(drive, spec["batch"])
    links = {}
    for f in files:
        q = f"name = '{f.name}' and '{folder['id']}' in parents and trashed = false"
        old = drive.files().list(q=q, fields="files(id)").execute().get("files")
        media = MediaFileUpload(str(f), mimetype="application/pdf" if f.suffix == ".pdf" else "image/png")
        res = (drive.files().update(fileId=old[0]["id"], media_body=media, fields="id, webViewLink") if old else
               drive.files().create(body={"name": f.name, "parents": [folder["id"]]}, media_body=media, fields="id, webViewLink")).execute(num_retries=6)
        post = int(f.name.split("_")[0].removeprefix("post"))
        links.setdefault(post, []).append({"file": f.name, "link": res["webViewLink"]})
    batch_file = C.BATCHES / f"{spec['batch']}.json"
    posts = json.loads(batch_file.read_text())
    for post, items in links.items():  # merged by file name: a re-upload of one slide keeps the other slides, the
        have = posts[post - 1].setdefault("visual_files", [])  # photo and every flag
        for it in items:
            hit = next((v for v in have if v["file"] == it["file"]), None)
            hit.update(it) if hit else have.append(it)
    batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    _link_in_doc(posts, links, folder["webViewLink"], batch_file)
    return {"folder": folder["webViewLink"], "posts": links}


def _link_in_doc(posts: list[dict], links: dict, folder_link: str, batch_file: pathlib.Path | None = None) -> None:
    """The Graphic link in the grey block of each post that got a visual: the block is rewritten from the batch file
    by content_engine_draft.refresh_meta (one place knows its layout)."""
    import content_engine_draft as ced
    if batch_file is not None and links:
        print(f"doc: linked {ced.refresh_meta(batch_file, sorted(links))} post(s)")


SINGLE, SLIDE = (360, 450), (240, 300)  # size in the doc, points, 4:5


def into_doc(spec_file: pathlib.Path, doc_id: str | None = None, tab_suffix: str = "") -> int:
    """The images under each post in the batch tab. Google Docs only copies an image in from a link anyone can open,
    so each file is opened to "anyone with the link" for the few seconds of the copy and closed again right after.
    Two passes: a "[[IMG name]]" marker at the end of each post, then each marker swapped for its image. A post that
    already has its images is skipped, so it can run again."""
    spec = json.loads(spec_file.read_text())
    posts = json.loads((C.BATCHES / f"{spec['batch']}.json").read_text())
    name = spec["batch"].split("_", 1)[1].replace("_", " ") + tab_suffix  # " (EN)": the English review copy
    doc_id = doc_id or C.need("google", "doc_id")
    drive, docs = _google("drive", "v3"), _google("docs", "v1")

    def read():
        doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
        tab = next(t for t in doc["tabs"] if t["tabProperties"]["title"] == name)
        objs = tab["documentTab"].get("inlineObjects", {})
        placed.clear()
        placed.update(re.sub(r".*[?&]id=", "", o["inlineObjectProperties"]["embeddedObject"].get("imageProperties", {}).get("sourceUri", ""))
                      for o in objs.values())
        paras = [(el["startIndex"], el["endIndex"], el["paragraph"].get("paragraphStyle", {}).get("namedStyleType"),
                  "".join(r.get("textRun", {}).get("content", "") for r in el["paragraph"].get("elements", [])),
                  any("inlineObjectElement" in r for r in el["paragraph"].get("elements", [])))
                 for el in tab["documentTab"]["body"]["content"] if "paragraph" in el]
        return tab["tabProperties"]["tabId"], paras

    placed: set = set()  # Drive ids of the images already in the tab (Docs keeps each image's source link)

    def _fid(post, fname):
        return next(v["link"].split("/d/")[1].split("/")[0] for v in post.get("visual_files", []) if v["file"] == fname)

    tab_id, paras = read()
    heads = [k for k, p in enumerate(paras) if is_post_heading(p[2], p[3], p[4])]
    reqs = []
    for i in sorted(range(len(posts)), reverse=True):
        pngs = [v["file"] for v in posts[i].get("visual_files", []) if v["file"].endswith((".png", ".jpg")) and v.get("in_doc", True)]
        if not pngs or i >= len(heads):
            continue
        last = heads[i + 1] - 1 if i + 1 < len(heads) else len(paras) - 1
        block = paras[heads[i]:last + 1]
        missing = [f for f in pngs if _fid(posts[i], f) not in placed and not any(f"[[IMG {f[:-4]}]]" in p[3] for p in block)]
        if not missing:
            continue  # every image (or its marker) is already there
        reqs.append({"insertText": {"location": {"index": paras[last][1] - 1, "tabId": tab_id},
                                    "text": "".join(f"\n[[IMG {f[:-4]}]]" for f in missing)}})
    if reqs:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
        tab_id, paras = read()
        # a marker inserted after a heading inherits its style; images are plain paragraphs, never headings
        plain = [{"updateParagraphStyle": {"range": {"startIndex": st, "endIndex": en, "tabId": tab_id},
                                           "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}}
                 for st, en, style, text, _ in paras if text.startswith("[[IMG ") and style != "NORMAL_TEXT"]
        if plain:
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": plain}).execute()
            tab_id, paras = read()

    ids = {v["file"][:-4]: v["link"].split("/d/")[1].split("/")[0] for p in posts for v in p.get("visual_files", [])}
    per_post = {}  # a carousel's slides go small, a single image (a graphic, a photo, a video cover) big
    for p in posts:
        pngs = [v["file"][:-4] for v in p.get("visual_files", []) if v["file"].endswith((".png", ".jpg")) and "_video_cover" not in v["file"]
                and v.get("in_doc", True)]
        per_post.update({k: len(pngs) for k in pngs})
    slides = {k for k, n in per_post.items() if n > 1}
    done = 0
    for start, end, _, text, _ in sorted(paras, reverse=True):
        m = re.fullmatch(r"\[\[IMG (\S+)\]\]\n", text)
        if not m or m.group(1) not in ids:
            continue
        fid = ids[m.group(1)]
        w, h = SLIDE if m.group(1) in slides else SINGLE
        perm = drive.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}, fields="id").execute()
        try:
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
                {"deleteContentRange": {"range": {"startIndex": start, "endIndex": end - 1, "tabId": tab_id}}},
                {"insertInlineImage": {"location": {"index": start, "tabId": tab_id},
                                       "uri": f"https://drive.google.com/uc?export=view&id={fid}",
                                       "objectSize": {"width": {"magnitude": w, "unit": "PT"},
                                                      "height": {"magnitude": h, "unit": "PT"}}}}]}).execute()
            done += 1
        finally:
            drive.permissions().delete(fileId=fid, permissionId=perm["id"]).execute()
    left = [k for k, fid in ids.items() if any(p["type"] == "anyone" for p in drive.files().get(
        fileId=fid, fields="permissions(type)").execute().get("permissions", []))]
    assert not left, f"still open to anyone: {left}"
    print(f"doc: {done} image(s) placed, all links closed again")
    return done


def is_post_heading(style: str, text: str, has_image: bool) -> bool:
    """A post's own heading ("✅ 3 · Title"), never an image or a marker that carries the heading style. Every reader of
    a batch tab counts posts with this, so an image in heading style never shifts the posts after it."""
    return (style == "HEADING_1" and not has_image and not text.strip().startswith("[[IMG ")
            and bool(re.search(r"[0-9A-Za-zÆØÅæøå]", text)))


def drive_id(link: str) -> str:
    return link.split("/d/")[1].split("/")[0]


def remove_from_doc(batch: str, fids: list[str], markers: list[str] = (), doc_id: str | None = None) -> int:
    """Take images (by their Drive ids) and their "[[IMG name]]" markers out of the batch tab. An image alone on its
    line takes the line with it. The files themselves stay where they are. Returns how many went."""
    name = batch.split("_", 1)[1].replace("_", " ")
    doc_id = doc_id or C.need("google", "doc_id")
    docs = _google("docs", "v1")
    doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    tab = next(t for t in doc["tabs"] if t["tabProperties"]["title"] == name)
    mine = {k for k, o in tab["documentTab"].get("inlineObjects", {}).items()
            if re.sub(r".*[?&]id=", "", o["inlineObjectProperties"]["embeddedObject"].get("imageProperties", {}).get("sourceUri", "")) in fids}
    paras = [el for el in tab["documentTab"]["body"]["content"] if "paragraph" in el]
    cut = []
    for k, el in enumerate(paras):
        runs = el["paragraph"].get("elements", [])
        text = "".join(r.get("textRun", {}).get("content", "") for r in runs).strip()
        hit = [r for r in runs if r.get("inlineObjectElement", {}).get("inlineObjectId") in mine]
        if text in {f"[[IMG {m}]]" for m in markers} or (hit and not text and len(hit) == sum("inlineObjectElement" in r for r in runs)):
            cut.append((el["startIndex"], el["endIndex"] if k + 1 < len(paras) else el["endIndex"] - 1))
        else:
            cut += [(r["startIndex"], r["endIndex"]) for r in hit]
    if cut:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
            {"deleteContentRange": {"range": {"startIndex": s, "endIndex": e, "tabId": tab["tabProperties"]["tabId"]}}}
            for s, e in sorted(cut, reverse=True) if e > s]}).execute()
    return len(cut)


# Every doc write under one lock (content_engine_lock.py)
from content_engine_lock import locked as _locked_doc  # noqa: E402
upload = _locked_doc(upload)
into_doc = _locked_doc(into_doc)
remove_from_doc = _locked_doc(remove_from_doc)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec")
    ap.add_argument("--post", type=int)
    ap.add_argument("--upload", action="store_true", help="put the files in Drive, link them from the doc and place them under each post")
    ap.add_argument("--into-doc", action="store_true", help="only place the already uploaded images under each post")
    ap.add_argument("--auto", action="store_true", help="spec = a batch JSON: design a graphic for every post that has none, then render, upload and place")
    a = ap.parse_args()
    if a.auto:
        spec_file = auto_spec(pathlib.Path(a.spec))
        made = render(spec_file)
        upload(spec_file, made)
        into_doc(spec_file)
        sys.exit(0)
    if a.into_doc:
        into_doc(pathlib.Path(a.spec))
        sys.exit(0)
    made = render(pathlib.Path(a.spec), a.post)
    for f in made:
        print(f)
    if a.upload:
        print(json.dumps(upload(pathlib.Path(a.spec), made), ensure_ascii=False, indent=1))
        into_doc(pathlib.Path(a.spec))
