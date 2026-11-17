#!/usr/bin/env python3
"""Content engine video: a shoot card for a video post, and the author's raw recording turned into a finished
LinkedIn video.

The author improvises on video from a few prompts, never reads a full script, and every video gets Danish captions
burned in.

  script  --batch <json> --post N   shoot card in the doc's "Video scripts" tab: the post's own first and last
                                     lines word for word (hook and ask) and 3-4 prompts to talk from
  process <file> [--name NAME]       Danish captions burned in (Whisper, then proofread as a whole text against the
                                     post it belongs to), quick zooms on the strongest lines and one or two of the
                                     speaker's own phrases on screen, 4:5 at 1080x1350, even loudness, a cover frame and
                                     an .srt; iPhone HDR is turned into normal colour first (macOS avconvert)
  redo <file id or name>             a recording already handled, made again with the pipeline as it is now; its
       [--said CAPTIONS SAID ...]    finished files in Drive replaced in place, so every link to them still works; --said
                                     is a word the author says is wrong, kept for every later redo; a changed post match
                                     moves it
  --auto                             every new file in the Drive "Video inbox" folder: processed, matched to its post,
                                     uploaded to "Finished videos", the cover placed under the post in the doc, a 5-line note
  --selftest                         the proofreading, zoom and pop-up rules, no network
"""
import argparse
import base64
import datetime as dt
import difflib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402
import content_engine_draft as ced  # noqa: E402

OUT = C.CACHE / "video"
STATE = C.STATE / "videos.json"
FONT = C.path(C.get("visuals", "font_bold"))
LOGO = C.get("visuals", "logo_dark")           # your logo for a dark corner of the picture (white)
LOGO_ON_LIGHT = C.get("visuals", "logo_light")  # and for a light corner


def _rgb(hex_colour: str) -> tuple:
    h = hex_colour.lstrip("#")
    return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))


NAVY, LIME = _rgb(C.get("visuals", "navy")), _rgb(C.get("visuals", "accent"))
W, H = 1080, 1350  # 4:5, LinkedIn's own recommended vertical ratio; 9:16 is accepted too
# Sound and picture polish. Phone recordings in a room tend to be quiet, with the room's hum well under the voice once
# levelled and most energy below 100 Hz (rumble, not voice), so no bass boost (the low end is the problem): see
# _voice(). The picture gets a touch of contrast, colour and sharpness.
LOOK = "eq=contrast=1.05:saturation=1.12,unsharp=5:5:0.6:5:5:0"
# The caption standard: one accent-coloured word at a time at the line's size, a soft navy shadow, no box.
# (word / line still exist.)
STYLES = ("word_bare",)
# A finished video must never go through again (a downloaded finished video uploaded again would get double
# captions): finished files carry MARK in their metadata and are named like FINISHED; the inbox job skips both.
MARK = "content engine finished video"
FINISHED = re.compile(r"^\d{4}-\d\d-\d\d (Batch \d+ post \d+|Video) ")
MARGIN, LOGO_H, WORD_SIZE = 44, 64, 58  # the logo sits MARGIN from the top right; one word = the line's own size
BOTTOM_SAFE = 120  # LinkedIn's progress bar and sound button live at the bottom of the video
GLOSSARY = C.get("video", "glossary")  # names and words Whisper should spell right, plus "Øh, øhm" so it writes fillers
TAB = "Video scripts"


def _llm(content, schema: dict, system: str, max_tokens: int = 4000, cache: str | None = None) -> dict:
    """cache: a tag; the same question with the same tag gets the stored answer, so a redo or a test variant of the same
    recording keeps its proofreading and moments (a changed prompt or model is a new question)."""
    import hashlib
    key = hashlib.md5(json.dumps([ced.MODEL, system, schema, content, cache], ensure_ascii=False).encode()).hexdigest()
    stored = OUT / "llm_cache" / f"{key}.json"
    if cache and stored.exists():
        return json.loads(stored.read_text())
    import llm_guard
    from anthropic import Anthropic
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=ced.MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}})
    res = json.loads(msg.content[0].text)
    if cache:
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text(json.dumps(res, ensure_ascii=False))
    return res


# ---------- the shoot card ----------
CARD_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["prompts", "minutes", "where"], "properties": {
    "prompts": {"type": "array", "minItems": 3, "maxItems": 4, "items": {
        "type": "object", "additionalProperties": False, "required": ["beat", "question", "touch"],
        "properties": {"beat": {"type": "string", "description": "2-4 Danish words naming the beat"},
                       "question": {"type": "string", "description": "one short Danish question the author answers in their own words"},
                       "touch": {"type": "string", "description": "the post's own words for this beat, short, in Danish"}}}},
    "minutes": {"type": "string", "description": "how long, e.g. 'ca. 60 sekunder'"},
    "where": {"type": "string", "description": "in English, one short sentence on where to film it: the visual note's place if it names one, otherwise a calm everyday spot that fits the story. One continuous shot: never ask for extra clips or cutaways, the whole file becomes the video. Never a claim about the story itself. Address the author as 'you', never by name."}}}
CARD_SYSTEM = """You turn a Danish LinkedIn post into a shoot card for its author, who improvises on video from a few
prompts and never reads a script. Write 3 or 4 prompts that walk through the post's beats in order, each a short Danish
question they can answer in their own words, plus the post's own words for that beat. Use only what the post says: no new claims, numbers or
examples. Danish with æ, ø, å, no long dashes, no marketing jargon. Keep every question under 14 words."""


def _paragraphs(text: str) -> list[list[str]]:
    paras, cur = [], []
    for line in text.strip().splitlines():
        if line.strip():
            cur.append(line.strip())
        elif cur:
            paras.append(cur)
            cur = []
    return paras + ([cur] if cur else [])


def shoot_card(post: dict, number: int) -> dict:
    paras = _paragraphs(post["text"])
    hook = paras[0][:2] if len(paras[0][0]) < 70 else paras[0][:1]  # the first seconds decide: the post's own first lines
    ask = paras[-1]
    res = _llm(f"Visual note: {post.get('visual', '')}\n\nThe post:\n{post['text']}", CARD_SCHEMA, CARD_SYSTEM)
    for p in res["prompts"]:
        issues = [i for i in ced.lint(p["question"] + "\n" + p["touch"]) if i not in ("list marker",)]
        if issues:
            raise SystemExit(f"card prompt broke a rule ({', '.join(issues)}): {p}")
    return {"post": number, "title": post["title"], "hook": hook, "ask": ask, **res}


def card_text(card: dict, batch: str) -> list[tuple[str, str]]:
    """(kind, text) lines: head, note (grey), label (bold), say (spoken, word for word), plain."""
    lines = [("head", f"{card['title']} · video for post {card['post']} in {batch}"),
             ("note", f"{card['minutes'][:1].upper() + card['minutes'][1:]}. {card['where']} Phone upright at eye height, "
                      "an arm's length away, light in front of you, quiet room. Stay silent for 1 second before you start. Talk, don't read: only the first and the last line are word for word."),
             ("label", "Start with this, word for word:")]
    lines += [("say", line) for line in card["hook"]]
    lines.append(("label", "Then talk from these:"))
    for i, p in enumerate(card["prompts"], 1):
        lines.append(("plain", f"{i}. {p['beat']}: {p['question']}"))
        lines.append(("note", f"The post says: {p['touch']}"))
    lines.append(("label", "End with this, word for word:"))
    lines += [("say", line) for line in card["ask"]]
    lines.append(("note", "Record as many takes as you like, but upload only the one you like best. Put the raw file in "
                          "the Video inbox folder in Drive. Within 10 minutes the silence, pauses and \"øh\" "
                          "are cut, Danish captions go on, the sound is evened out, and the finished video is linked under "
                          "the post in the doc."))
    return lines


SOP_LINK = C.get("google", "video_sop_url")  # optional: a one-page recording guide, linked atop the tab
INBOX_LINK = (f"https://drive.google.com/drive/folders/{C.get('google', 'video_inbox_id')}"
              if C.get("google", "video_inbox_id") else "")
DONE = "✅ Done · "


def mark_cards() -> str:
    """The Video scripts tab as a to-do list: the How to record link first, then one line with what is left and what
    is done, and every card whose video is finished marked "✅ Done · ". Rebuilt from the batch files (a post with its
    finished video_file is done), so it can run any time; the video job runs it after every finished video."""
    import glob
    docs = ced._docs()
    status = {}
    for f in glob.glob(str(C.BATCHES / "*Engine_Batch_*.json")):
        m = re.search(r"Engine_Batch_(\d+)\.json$", f)
        if not m:
            continue
        for i, p in enumerate(json.loads(pathlib.Path(f).read_text()), 1):
            if isinstance(p, dict) and p.get("format") == "video" and p.get("status") != "dropped":
                status[(int(m.group(1)), i)] = (bool(p.get("video_file")), p.get("title", ""))

    def read():
        doc = docs.documents().get(documentId=ced.DOC_ID, includeTabsContent=True).execute()
        tab = next(t for t in doc["tabs"] if t["tabProperties"]["title"] == TAB)
        paras = [(el["startIndex"], el["endIndex"], el["paragraph"].get("paragraphStyle", {}).get("namedStyleType"),
                  "".join(r.get("textRun", {}).get("content", "") for r in el["paragraph"].get("elements", [])))
                 for el in tab["documentTab"]["body"]["content"] if "paragraph" in el]
        return tab["tabProperties"]["tabId"], paras

    tab, paras = read()
    # 1. take out the old How to record heading and status line, and empty headings, wherever they are
    cut = [(s0, e0) for s0, e0, st, t in paras if t.startswith(("How to record:", "To record (", "Drop your recordings here:")) or (st == "HEADING_1" and not t.strip())]
    if cut:
        docs.documents().batchUpdate(documentId=ced.DOC_ID, body={"requests": [
            {"deleteContentRange": {"range": {"startIndex": a, "endIndex": b, "tabId": tab}}} for a, b in sorted(cut, reverse=True)]}).execute()
        tab, paras = read()
    # 2. done marks on the card headings
    reqs = []
    for s0, e0, st, t in sorted(paras, reverse=True):
        m = re.search(r"video for post (\d+) in Engine Batch (\d+)", t)
        if st != "HEADING_1" or not m:
            continue
        done = status.get((int(m.group(2)), int(m.group(1))), (False, ""))[0]
        if done and not t.startswith(DONE):
            reqs.append({"insertText": {"location": {"index": s0, "tabId": tab}, "text": DONE}})
        elif not done and t.startswith(DONE):
            reqs.append({"deleteContentRange": {"range": {"startIndex": s0, "endIndex": s0 + ced._u16(DONE), "tabId": tab}}})
    if reqs:
        docs.documents().batchUpdate(documentId=ced.DOC_ID, body={"requests": reqs}).execute()
    # 3. How to record + the status line on top
    todo = sorted(k for k, v in status.items() if not v[0])
    done = sorted(k for k, v in status.items() if v[0])
    head = "How to record: "
    line = (f"To record ({len(todo)}): " + ", ".join(f"Batch {b} post {n}" for b, n in todo)
            + f".  Done ({len(done)}): " + ", ".join(f"Batch {b} post {n}" for b, n in done) + ".")
    drop, drop_label = "Drop your recordings here: ", "Video inbox (Google Drive)"
    text = head + "Video recording SOP (one page, open it on your phone)\n" + drop + drop_label + "\n" + line + "\n"
    a = 1 + ced._u16(head)
    b = 1 + ced._u16(text.split("\n")[0])
    c = b + 1 + ced._u16(drop)                       # the Video inbox link
    c2 = b + 1 + ced._u16(drop + drop_label)
    links = ([{"updateTextStyle": {"range": {"startIndex": a, "endIndex": b, "tabId": tab}, "textStyle": {"link": {"url": SOP_LINK}}, "fields": "link"}}]
             if SOP_LINK else []) + ([{"updateTextStyle": {"range": {"startIndex": c, "endIndex": c2, "tabId": tab}, "textStyle": {"link": {"url": INBOX_LINK}}, "fields": "link"}}]
                                     if INBOX_LINK else [])
    docs.documents().batchUpdate(documentId=ced.DOC_ID, body={"requests": [
        {"insertText": {"location": {"index": 1, "tabId": tab}, "text": text}},
        {"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": b, "tabId": tab},
                                  "paragraphStyle": {"namedStyleType": "HEADING_1"}, "fields": "namedStyleType"}},
        {"updateParagraphStyle": {"range": {"startIndex": b + 1, "endIndex": 1 + ced._u16(text), "tabId": tab},
                                  "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
        *links,
        {"updateTextStyle": {"range": {"startIndex": b + 1, "endIndex": ced._u16(text), "tabId": tab},
                             "textStyle": {"bold": True}, "fields": "bold"}}]}).execute()
    return line


def write_card(card: dict, batch: str) -> str:
    docs, doc_id = ced._docs(), ced.DOC_ID
    tabs = ced._tabs(doc_id)
    tab = next((t["id"] for t in tabs if t["title"] == TAB), None)
    if not tab:
        res = docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
            {"addDocumentTab": {"tabProperties": {"title": TAB, "index": 1}}}]}).execute()
        tab = res["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    body = next(t for t in doc["tabs"] if t["tabProperties"]["tabId"] == tab)["documentTab"]["body"]["content"]
    existing = "".join(r.get("textRun", {}).get("content", "") for el in body if "paragraph" in el
                       for r in el["paragraph"].get("elements", []))
    head = card_text(card, batch)[0][1]
    if head in existing:
        return f"already in the {TAB} tab: {head}"
    text, styles, pos = "", [], 1
    for kind, line in card_text(card, batch) + [("plain", "")]:
        s = line + "\n"
        styles.append((kind, pos, pos + ced._u16(s)))
        text += s
        pos += ced._u16(s)
    reqs = [{"insertText": {"location": {"index": 1, "tabId": tab}, "text": text}},
            {"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": pos, "tabId": tab},
                                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
            {"updateTextStyle": {"range": {"startIndex": 1, "endIndex": pos, "tabId": tab}, "textStyle": {},
                                 "fields": "italic,bold,foregroundColor,fontSize"}}]
    grey = {"color": {"rgbColor": {"red": 0.42, "green": 0.47, "blue": 0.57}}}
    for kind, s, e in styles:
        rng = {"startIndex": s, "endIndex": e - 1, "tabId": tab}
        if e - 1 <= s:
            continue
        if kind == "head":
            reqs.append({"updateParagraphStyle": {"range": {"startIndex": s, "endIndex": e, "tabId": tab},
                                                  "paragraphStyle": {"namedStyleType": "HEADING_1"}, "fields": "namedStyleType"}})
        elif kind == "note":
            reqs.append({"updateTextStyle": {"range": rng, "textStyle": {"italic": True, "foregroundColor": grey},
                                             "fields": "italic,foregroundColor"}})
        elif kind == "label":
            reqs.append({"updateTextStyle": {"range": rng, "textStyle": {"bold": True}, "fields": "bold"}})
        elif kind == "say":
            reqs.append({"updateTextStyle": {"range": rng, "textStyle": {"bold": True, "fontSize": {"magnitude": 13, "unit": "PT"}},
                                             "fields": "bold,fontSize"}})
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
    return f"https://docs.google.com/document/d/{doc_id}/edit?tab={tab}"


# ---------- the finished video ----------
def _probe(path: pathlib.Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {"duration": float(d["format"].get("duration", 0)), "w": v["width"], "h": v["height"],
            "audio": any(s["codec_type"] == "audio" for s in d["streams"]),
            "hdr": v.get("color_transfer") in ("arib-std-b67", "smpte2084")}


def _sdr(src: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """iPhone HDR video (HLG/Dolby Vision, the Camera app's default) to normal colour with Apple's own converter: this
    ffmpeg has no zscale, so an HDR file would come out washed out. Upright 1080x1920 stays 1080x1920."""
    sdr = out / "sdr.mp4"
    subprocess.run(["avconvert", "--source", str(src), "--preset", "Preset1920x1080", "--output", str(sdr), "--replace"],
                   check=True, capture_output=True)
    return sdr


_WHISPER = {}


def _whisper():
    """Whisper ([video] whisper_model, large-v3 by default), loaded once per run: the transcription and the sound check
    share it."""
    import whisper
    name = C.get("video", "whisper_model")
    if name not in _WHISPER:
        _WHISPER[name] = whisper.load_model(name)
    return _WHISPER[name]


def _words(wav: pathlib.Path) -> list[dict]:
    model = _whisper()
    res = model.transcribe(str(wav), language="da", word_timestamps=True, initial_prompt=GLOSSARY,
                           condition_on_previous_text=False, fp16=False)
    words = []
    for w in (w for seg in res["segments"] for w in seg.get("words", []) if w["word"].strip()):
        t = w["word"].strip()
        if words and (t[:1] == "-" or t in {".", ",", "?", "!", ":"}):  # "Google" + "-annoncering" is one word on screen
            words[-1] = {"w": words[-1]["w"] + t, "s": words[-1]["s"], "e": w["end"]}
        else:
            words.append({"w": t, "s": w["start"], "e": w["end"]})
    return words


def _captions(words: list[dict], max_chars: int = 28) -> list[dict]:
    """Short readable chunks: break at a sentence end, a pause or a long comma clause, never past max_chars;
    a chunk too short to read (under 0.7 s) is merged into a neighbour when it fits."""
    caps, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        text = " ".join(x["w"] for x in cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 9
        longer = nxt and len(text) + 1 + len(nxt["w"]) > max_chars
        end = w["w"][-1:] in ".?!" or (w["w"][-1:] in ",:" and len(text) >= 14)
        if end or gap > 0.45 or longer or not nxt:
            caps.append({"text": text, "s": cur[0]["s"], "e": w["e"], "words": [dict(x) for x in cur]})
            cur = []
    merged = []
    for c in caps:
        if merged and (c["e"] - c["s"] < 0.7 or merged[-1]["e"] - merged[-1]["s"] < 0.7) \
                and len(merged[-1]["text"]) + 1 + len(c["text"]) <= max_chars and c["s"] - merged[-1]["e"] < 0.6:
            merged[-1] = {"text": merged[-1]["text"] + " " + c["text"], "s": merged[-1]["s"], "e": c["e"],
                          "words": merged[-1]["words"] + c["words"]}
        else:
            merged.append(c)
    for a, b in zip(merged, merged[1:]):  # hold each caption until the next, but never overlap
        a["e"] = min(max(a["e"] + 0.15, a["s"] + 0.7), b["s"])
    if merged:
        merged[-1]["e"] = max(merged[-1]["e"] + 0.3, merged[-1]["s"] + 0.8)
    return merged


# ---------- the captions proofread as a whole text ----------
# Speech-to-text gets real words wrong where a word that sounds almost the same is meant (a "båd" in a story about a
# "bod"). One short caption at a time, without the post, can't see that; the whole transcript read next to its post can.
PROOF_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["fixes"], "properties": {"fixes": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["first", "last", "options"], "properties": {
        "first": {"type": "integer"}, "last": {"type": "integer"},
        "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}}}}}}}
PROOF_SYSTEM = """You proofread the Danish captions of a video: speech-to-text (Whisper) of a person talking freely on
camera, usually about the LinkedIn post given above the transcript. Read the whole transcript first, then use each
sentence, the story and the post to find what the speech-to-text got wrong:
- a real word that makes no sense in its sentence or story, where a word that sounds almost the same does ("bagerbåd", a
  boat, in a story about a bakery stall is "bagerbod")
- "og" and "at" are both said "å" in Danish, so the speech-to-text mixes them up: after a noun, an infinitive needs "at"
  ("en god måde og starte" is "en god måde at starte"); check every "og" and "at" this way. The same for other words
  that sound alike
- a Danish compound written as two words ("yndlings eksempler" is "yndlingseksempler"), names and terms from the
  glossary, a name of a brand, place or person without its capital letter, æ ø å, a capital letter after a full stop
The speaker talks freely, so where the transcript makes sense, keep their words, even when the post puts it differently.
Never rephrase, shorten, add, or correct their grammar or style: fix only what was misheard. Keep their punctuation.
Each fix: the number of the first and last word it replaces (inclusive, at most 5 words) and options: the corrected
text for exactly those words, most likely first. When the sentence allows more than one wording that sounds like what
was heard ("ligger ude", "lever ude" or "er ude"), give up to 3 options: the sound itself decides between them. Nothing
misheard: an empty list."""


def _letters(s: str) -> str:
    return re.sub(r"[^\wæøå]", "", s.lower())


def _sound(s: str) -> str:
    """Roughly how it sounds: "og" and "at" are both said "å", so swapping them is a mishearing, not a new word."""
    return _letters(re.sub(r"\b(og|at)\b", "å", s.lower()))


def _place(old: list[dict], text: str) -> list[dict]:
    """New words in the time of the words they replace: word for word when the count is the same, else spread by length."""
    new = text.split()
    if len(new) == len(old):
        return [{"w": t, "s": w["s"], "e": w["e"]} for t, w in zip(new, old)]
    out, s, e = [], old[0]["s"], old[-1]["e"]
    t = s
    for tok in new:
        d = (e - s) * len(tok) / sum(map(len, new))
        out.append({"w": tok, "s": t, "e": t + d})
        t += d
    return out


def _said(words: list[dict], said) -> tuple[list[dict], list[tuple[str, str]], set[int], list[str]]:
    """The author's own corrections, (what the captions said, what was really said), put in before the proofreading and
    never second-guessed by it or by the sound: they were there. Matched on whole words as Whisper heard them, first
    match. Returns the words, the changes, the word numbers the proofreader must leave alone, and the corrections that
    matched nothing."""
    changes, keep, missing = [], set(), []
    for heard, right in said:
        h = heard.split()
        a = next((i for i in range(len(words) - len(h) + 1) if [w["w"] for w in words[i:i + len(h)]] == h), None)
        if a is None or not h:
            missing.append(heard)
            continue
        new = _place(words[a:a + len(h)], right)
        words = words[:a] + new + words[a + len(h):]
        keep = {k if k < a + len(h) else k + len(new) - len(h) for k in keep} | set(range(a, a + len(new)))
        changes.append((heard, right))
    return words, changes, keep, missing


def _apply_fixes(words: list[dict], fixes: list[dict], judge=None, keep=()) -> tuple[list[dict], list[tuple[str, str]]]:
    """The proofreader's fixes put into the words, each keeping the time of the words it replaces (word for word when the
    count is the same, else spread by length). An option that replaces more than 5 words or sounds nothing like what was
    heard is dropped; the same stretch from both reads becomes one fix with both reads' options; `judge(a, b, options)`
    picks the one the sound supports (None: none of them, the words stay as heard); of two fixes that overlap, the one
    covering more words wins. A fix touching a word in `keep` (the author's own correction) is dropped."""
    options = {}
    for f in fixes:
        a, b = f["first"], f["last"]
        if a < 0 or b < a or b >= len(words) or b - a > 4 or keep and set(range(a, b + 1)) & set(keep):
            continue
        old = " ".join(w["w"] for w in words[a:b + 1])
        end = re.search(r"[,.;:!?]+$", old)
        for o in f["options"]:
            o = " ".join(re.sub("[" + ced.DASHES + "]", "-", o).split())
            if end and o and not re.search(r"[,.;:!?]$", o):  # the punctuation stays ("at starte," for "og starte,")
                o += end.group(0)
            if o and o != old and len(o.split()) <= b - a + 3 and o not in options.get((a, b), []) \
                    and difflib.SequenceMatcher(None, _sound(old), _sound(o)).ratio() >= 0.5:
                options.setdefault((a, b), []).append(o)
    picked = [(a, b, judge(a, b, opts) if judge else opts[0]) for (a, b), opts in options.items()]
    chosen = []
    for a, b, pick in sorted((f for f in picked if f[2]), key=lambda f: (f[0] - f[1], f[0])):
        if all(b < c or a > d for c, d, _ in chosen):
            chosen.append((a, b, pick))
    out, changes, i = [], [], 0
    for a, b, pick in sorted(chosen):
        out += words[i:a] + _place(words[a:b + 1], pick)
        changes.append((" ".join(w["w"] for w in words[a:b + 1]), pick))
        i = b + 1
    return out + words[i:], changes


LIKELY = 8.0  # how much less likely than what Whisper heard a fix may sound (log-likelihood). On test recordings the real
# fixes cost 2 to 5 (Whisper leans to the commoner word), wrong guesses 13 to 15. Calibrated on few recordings: watch it.


def _judge(wav: pathlib.Path, words: list[dict]):
    """judge(a, b, options): the option for words a..b that the sound supports best, scored by Whisper itself (how likely
    it finds each wording for that stretch of audio, four words either side as context); None when even the best is
    LIKELY less likely than what it heard. A proofreader's guess between real words can be wrong; the sound decides.
    Every check lands in judge.log."""
    import torch
    import whisper
    model = _whisper()
    audio = whisper.load_audio(str(wav))
    tok = whisper.tokenizer.get_tokenizer(True, num_languages=model.num_languages, language="da", task="transcribe")
    prefix = list(tok.sot_sequence_including_notimestamps)

    def judge(a: int, b: int, options: list[str]) -> str | None:
        lo, hi = max(0, a - 4), min(len(words) - 1, b + 4)
        t0, t1 = max(0.0, words[lo]["s"] - 0.25), words[hi]["e"] + 0.25
        mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(audio[int(t0 * 16000):int(t1 * 16000)]), n_mels=model.dims.n_mels)
        before, after = " ".join(w["w"] for w in words[lo:a]), " ".join(w["w"] for w in words[b + 1:hi + 1])
        with torch.no_grad():
            feats = model.embed_audio(mel[None])

            def score(middle: str) -> float:
                body = tok.encode(" " + " ".join(x for x in (before, middle, after) if x)) + [tok.eot]
                logp = torch.log_softmax(model.logits(torch.tensor([prefix + body]), feats).float(), -1)
                return sum(logp[0, len(prefix) - 1 + k, body[k]].item() for k in range(len(body)))
            heard, scores = score(" ".join(w["w"] for w in words[a:b + 1])), {o: score(o) for o in options}
        best = max(scores, key=scores.get)
        pick = best if scores[best] >= heard - LIKELY else None
        judge.log.append({"heard": " ".join(w["w"] for w in words[a:b + 1]), "pick": pick,
                          "options": {o: round(v - heard, 1) for o, v in scores.items()}})
        return pick
    judge.log = []
    return judge


def _proofread(words: list[dict], post: dict | None, judge=None, passes: int = 2, keep=()) -> tuple[list[dict], list[tuple[str, str]]]:
    """Two independent reads, every fix either one finds: one read alone can miss one or two mishearings, a different
    one each time."""
    if not words:
        return words, []
    from concurrent.futures import ThreadPoolExecutor
    about = f"The post the video belongs to:\n{post.get('title', '')}\n{post['text']}\n\n" if post else ""
    numbered = " ".join(f"[{i}]{w['w']}" for i, w in enumerate(words))
    ask = about + f"Glossary: {GLOSSARY}\n\nThe transcript, word by word with numbers:\n{numbered}"
    with ThreadPoolExecutor(passes) as pool:
        reads = list(pool.map(lambda k: _llm(ask, PROOF_SCHEMA, PROOF_SYSTEM, cache=f"read{k}")["fixes"], range(passes)))
    return _apply_fixes(words, [f for read in reads for f in read], judge, keep)


# ---------- punch-ins and text pop-ups ----------
# Quick punch-ins on the punchlines, a little closer on the joke, nothing in the opening sentence (a zoom before the
# first word looks odd), never in and out in quick succession, and a few of the speaker's own phrases on screen.
MOMENT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["moments"], "properties": {"moments": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["start", "end", "kind", "pop"], "properties": {
        "start": {"type": "integer"}, "end": {"type": "integer"},
        "kind": {"type": "string", "enum": ["hook", "punch", "funny", "ask", "key"]}, "pop": {"type": "string"}}}}}}
MOMENT_SYSTEM = ("You mark moments in a Danish talking-head video for editing emphasis. The transcript comes as numbered words. "
                 "Mark: hook = the opening statement (1); punch = 2 to 4 punchlines or key claims; funny = 0 to 2 humorous asides or "
                 "self-corrections (for example a swear word the speaker then corrects); ask = the closing line or conclusion (1); key = the one "
                 "or two short phrases that sum up the idea, after the opening sentence (1 to 2, best first). Each moment: start and "
                 "end word index (inclusive, a few words to one sentence), kind, and pop = 2 to 5 consecutive words copied exactly "
                 "from that moment, in order, nothing left out or changed, to show on screen; only for funny and key, else empty. "
                 "Never invent words.")
ZOOM, ZOOM_FUNNY = 0.12, 0.18  # how much closer a punch-in goes; the joke a little more (B had 15/22%)
ZOOM_COUNT = 2  # zoom only a few times per video
MIN_HOLD, MIN_REST = 1.5, 3.0  # a punch-in lasts at least this long, and two closer than MIN_REST become one
POP_Y = 200  # the pop-ups' centre line, under the logo and above the head
# How many pop-ups depends on how relevant the phrases are: every phrase scored at least POP_SCORE, one joke at most, POP_REST s of clear screen between two, and no more than
# one per POP_EVERY s of video on average (2 at least)
POP_SCORE, POP_REST, POP_EVERY = 7, 4.0, 12.0
SFX_DB = -12.0  # a click sound as a phrase pops up above the head; its peak, well under the voice (-14 LUFS, peaks near -1 dB), so it lands as a tick, not a bang
POP_SOUND = "mix"  # bottle and click taking turns (see _sfx_plan); "click" or "bottle" for one sound only


def _calm_until(words: list[dict]) -> float:
    """The opening sentence stays on the speaker's own framing: no punch-in or pop-up before it ends (3 to 12 s)."""
    end = next((w["e"] for w in words if w["w"][-1:] in ".?!"), 3.0)
    return min(max(end, 3.0), 12.0)


def _moments(words: list[dict]) -> list[dict]:
    numbered = " ".join(f"[{i}]{w['w']}" for i, w in enumerate(words))
    moments = []
    for m in _llm(numbered, MOMENT_SCHEMA, MOMENT_SYSTEM, cache="moments")["moments"]:
        a, b = max(0, m["start"]), min(len(words) - 1, m["end"])
        if a <= b:
            moments.append({**m, "start": a, "end": b, "t0": words[a]["s"], "t1": words[b]["e"]})
    return moments


def _zoom_pieces(moments: list[dict], length: float, calm: float) -> list[tuple[float, float, float]]:
    """(start, end, zoom) of the punch-ins: from the punchlines, the joke and the closing line, none in the opening
    sentence; two less than MIN_REST apart joined into one, anything shorter than MIN_HOLD left out; only ZOOM_COUNT
    kept (the joke's first, then the close, then the longest); the joke goes ZOOM_FUNNY close inside its punch-in.
    Overlaps are fine: the filter takes the largest zoom at each moment."""
    spans = sorted((m["t0"], min(m["t1"], length)) for m in moments if m["kind"] in ("punch", "funny", "ask") and m["t0"] >= calm)
    joined = []
    for a, b in spans:
        if joined and a - joined[-1][1] < MIN_REST:
            joined[-1] = (joined[-1][0], max(joined[-1][1], b))
        elif b > a:
            joined.append((a, b))
    pieces = [(a, b, ZOOM) for a, b in joined if b - a >= MIN_HOLD]
    funny = [m["t0"] for m in moments if m["kind"] == "funny"]
    close = max((m["t0"] for m in moments if m["kind"] == "ask"), default=-1.0)
    rank = lambda p: (not any(p[0] <= t < p[1] for t in funny), not p[0] <= close < p[1], p[0] - p[1])  # the joke, the close, the longest
    pieces = sorted(pieces, key=rank)[:ZOOM_COUNT]
    pieces += [(m["t0"], min(m["t1"], length), ZOOM_FUNNY) for m in moments if m["kind"] == "funny" and m["t0"] >= calm
               and min(m["t1"], length) - m["t0"] >= MIN_HOLD and any(a <= m["t0"] < b for a, b, _ in pieces)]
    return sorted(pieces)


ZOOM_STEP, ZOOM_MAX = 0.15, 0.30


# Where the "max" test style's zooms land, in turn: the crop's anchor across (0 left, 1 right) and down (1 shows the
# lower part, so the face rides high); never the top part, which would push the face down onto the captions.
SPOTS = ((0.0, 1.0), (1.0, 0.6), (0.0, 0.6), (1.0, 1.0), (0.5, 1.0))


def _max_pieces(moments: list[dict], words: list[dict], bounds: list[float], length: float):
    """Test style "max" (zoom as close and as often as possible): the
    framing switches between normal and ZOOM_STEP closer at every cut and every sentence or comma break (at least 1.5 s
    apart, never before the first word is out), and the punchlines, the joke and the close go ZOOM_MAX close. Each zoom
    lands somewhere else (SPOTS, in turn). Returns the pieces and their spots (a, b, across, down), strongest first."""
    first = words[0]["e"] if words else 0.0
    marks = []
    for t in sorted(set(bounds) | {w["e"] for w in words if w["w"][-1:] in ".?!,"}):
        if first + 1.0 <= t < length - 0.5 and (not marks or t - marks[-1] >= 1.5):
            marks.append(t)
    pieces = [(a, b, ZOOM_STEP) for k, (a, b) in enumerate(zip(marks, marks[1:] + [length])) if k % 2 == 0]
    pieces += [(m["t0"], min(m["t1"], length), ZOOM_MAX) for m in moments if m["kind"] in ("punch", "funny", "ask") and m["t0"] >= first]
    pieces.sort()
    spots = [(a, b) + SPOTS[k % len(SPOTS)] for k, (a, b, _) in enumerate(pieces)]
    spots.sort(key=lambda sp: -next(z for a, b, z in pieces if (a, b) == sp[:2]))
    return pieces, spots


def _zoom_filter(pieces: list[tuple[float, float, float]], cx: float, cy: float, spots=(), centres=()) -> str:
    """Scale up and crop back to the frame with the middle of the face (cx, cy) centred across and kept at its own
    height. A centre (a, b, across, down) is the face during that punch-in, so a head moved since the five sample
    frames is still centred. A spot (a, b, across, down) anchors the crop elsewhere while it lasts (the first one
    covering a moment wins). The crop works from the zoom itself, never iw/ih: crop reads those once, from the first
    (unzoomed) frame, so (iw-W) would always be 0 and every punch-in would grow from the top left corner."""
    expr = "0"
    for a, b, z in pieces:  # nested max(): pieces that overlap never stack
        expr = f"max({expr},{z}*between(t,{a:.2f},{b:.2f}))"
    fx, fy = f"{cx:.3f}", f"{cy:.3f}"
    for a, b, x, y in reversed(centres):
        fx, fy = f"if(between(t,{a:.2f},{b:.2f}),{x:.3f},{fx})", f"if(between(t,{a:.2f},{b:.2f}),{y:.3f},{fy})"
    spare_w, spare_h = f"{W}*{expr}", f"{H}*{expr}"  # how much bigger the scaled frame is
    ax, ay = f"clip({fx}*({W}+{spare_w})-{W // 2},0,{spare_w})", f"{spare_h}*{fy}"
    for a, b, x, y in reversed(spots):
        ax, ay = f"if(between(t,{a:.2f},{b:.2f}),{spare_w}*{x:.3f},{ax})", f"if(between(t,{a:.2f},{b:.2f}),{spare_h}*{y:.3f},{ay})"
    return (f"scale=w='{W}*(1+{expr})':h='{H}*(1+{expr})':eval=frame,"
            f"crop={W}:{H}:x='{ax}':y='{ay}'")


def _zoom_centres(src: pathlib.Path, out: pathlib.Path, crop: str, pieces, keep, lap) -> list[tuple[float, float, float, float]]:
    """(start, end, across, down): the middle of the face during each punch-in, from three frames inside it on the
    recording; a punch-in with no face found keeps the whole video's."""
    folder = out / "faces"
    folder.mkdir(exist_ok=True)
    centres = []
    for k, (a, b) in enumerate(sorted({(a, b) for a, b, _ in pieces})):
        shots = []
        for j, part in enumerate((0.25, 0.5, 0.75)):
            p = folder / f"z{k}_{j}.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{_source_time(a + (b - a) * part, keep, lap):.2f}", "-i", str(src),
                            "-frames:v", "1", "-vf", crop, str(p)], check=True)
            shots.append(p)
        faces = _faces(shots)
        if faces:
            centres.append((a, b, *_face_centre(faces)))
    return centres


SR = 48000
# One sound per pop-up, the same kind of effect but never the same sound twice in a row: bottle and click take turns,
# each round a little higher or lower. (start pitch Hz, rise Hz, decay s) of the bottle, (ring Hz, release s, release
# level) of the click.
BOTTLES = ((380, 750, 0.022), (450, 850, 0.018), (330, 650, 0.026))
CLICKS = ((3400, 0.075, 0.55), (3000, 0.060, 0.45), (3800, 0.090, 0.65))


def _sfx_plan(n: int) -> list[tuple[str, int]]:
    """(kind, variant) for n pop-ups in a row: POP_SOUND "mix" alternates bottle and click, a new variant each round."""
    if POP_SOUND in ("bottle", "click"):
        return [(POP_SOUND, 0)] * n
    return [(("bottle", "click")[k % 2], (k // 2) % 3) for k in range(n)]


def _sfx(kind: str, variant: int = 0):
    """One pop-up sound, made here so there is no sound file to license. click: a mouse click, press and a softer
    release, each a few ms of high-passed noise (the snap) over a short ring (the plastic). bottle: the hollow "plop"
    of a cork leaving a bottle, a tone sliding fast up from its start pitch as it dies away, a puff of air at the start."""
    import numpy as np
    rng = np.random.default_rng(7 + variant)
    if kind == "bottle":
        start, rise, decay = BOTTLES[variant % len(BOTTLES)]
        t = np.arange(int(0.11 * SR)) / SR
        pitch = start + rise * (1 - np.exp(-t / 0.015))
        tone = np.sin(2 * np.pi * np.cumsum(pitch) / SR) * np.exp(-t / decay) * (1 - np.exp(-t / 0.0008))
        sound = tone + 0.18 * np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t / 0.003)
    else:
        ring_hz, release_s, release_gain = CLICKS[variant % len(CLICKS)]
        t = np.arange(int(0.012 * SR)) / SR

        def tick(gain: float):
            snap = np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t / 0.0008)
            ring = (np.sin(2 * np.pi * ring_hz * t) + 0.6 * np.sin(np.pi * ring_hz * t)) * np.exp(-t / 0.0025)
            return gain * (0.6 * snap + 0.4 * ring)
        sound = np.zeros(int(0.12 * SR))
        sound[:len(t)] += tick(1.0)
        release = int(release_s * SR)
        sound[release:release + len(t)] += tick(release_gain)
    return sound * 10 ** (SFX_DB / 20) / np.abs(sound).max()


def _sfx_track(path: pathlib.Path, pops: list[dict], length: float) -> None:
    """The pop-ups' sounds on one track as long as the video, each landing as its pop-up does (_sfx_plan)."""
    import wave
    import numpy as np
    track = np.zeros(int((length + 0.5) * SR))
    for p, (kind, variant) in zip(pops, _sfx_plan(len(pops))):
        sound, at = _sfx(kind, variant), int(p["t0"] * SR)
        track[at:at + len(sound)] += sound[:max(0, len(track) - at)]
    with wave.open(str(path), "wb") as wv:
        wv.setnchannels(1), wv.setsampwidth(2), wv.setframerate(SR), wv.writeframes((np.clip(track, -1, 1) * 32767).astype(np.int16).tobytes())


POP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["phrases"], "properties": {"phrases": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["start", "end", "kind", "pop", "score"], "properties": {
        "start": {"type": "integer"}, "end": {"type": "integer"}, "kind": {"type": "string", "enum": ["funny", "key"]},
        "pop": {"type": "string"}, "score": {"type": "integer"}}}}}}
POP_SYSTEM = ("You pick the phrases of a Danish talking-head video to show on screen as short text pop-ups above the speaker's "
              "head, for viewers who watch without sound. The transcript comes as numbered words. Mark every phrase worth showing: "
              "the claims that carry the idea, a striking comparison or number, the punchline, a humorous aside or self-correction. "
              "Each: start and end word index (inclusive, a few words to one sentence), kind (funny or key), pop = 2 to 5 "
              "consecutive words copied exactly from that span, in order, nothing left out or changed, and score 1 to 10 for how "
              "much it adds for a viewer (10 = the line they should remember; 5 = it only repeats what was just said). Mark 3 to "
              "10 phrases. Never invent words.")


def _pop_phrases(words: list[dict]) -> list[dict]:
    """The phrases worth a pop-up, each scored for how much it adds (the moments' own funny/key are 1 or 2 at most)."""
    numbered = " ".join(f"[{i}]{w['w']}" for i, w in enumerate(words))
    found = []
    for m in _llm(numbered, POP_SCHEMA, POP_SYSTEM, cache="pops")["phrases"]:
        a, b = max(0, m["start"]), min(len(words) - 1, m["end"])
        if a <= b:
            found.append({**m, "start": a, "end": b, "t0": words[a]["s"], "t1": words[b]["e"]})
    return found


def _pops(moments: list[dict], words: list[dict], calm: float) -> list[dict]:
    """The speaker's own phrases on screen, best first: every one scored POP_SCORE or more (a phrase without a score
    counts as top), one joke at most, POP_REST s of clear screen between two, and at most one per POP_EVERY s of video
    (2 at least). Each shows from the moment it is said until a second after, at least 1.6 s. A phrase that isn't word
    for word what was said is left out."""
    said = [_letters(w["w"]) for w in words]
    most = max(2, int((words[-1]["e"] if words else 0.0) / POP_EVERY))
    pops = []
    jokes = 0
    for m in sorted((m for m in moments if m["kind"] in ("funny", "key") and m["pop"].strip() and m.get("score", 10) >= POP_SCORE),
                    key=lambda m: (-m.get("score", 10), m["kind"] != "funny")):
        if len(pops) == most or (m["kind"] == "funny" and jokes):
            continue
        want = [_letters(t) for t in m["pop"].split() if _letters(t)]
        starts = [i for i in range(len(said) - len(want) + 1) if want and said[i:i + len(want)] == want]
        if not starts:
            continue
        i = min(starts, key=lambda i: abs(i - m["start"]))
        t0 = words[i]["s"]
        t1 = max(words[i + len(want) - 1]["e"] + 1.0, t0 + 1.6)
        if t0 < calm or any(t0 < p["t1"] + POP_REST and p["t0"] < t1 + POP_REST for p in pops):
            continue
        text = re.sub(r"[,.;:!?]+$", "", " ".join(w["w"] for w in words[i:i + len(want)]))
        pops.append({"text": text[:1].upper() + text[1:], "t0": t0, "t1": t1})
        jokes += m["kind"] == "funny"
    return sorted(pops, key=lambda p: p["t0"])


def _sticker_png(text: str, scale: float, path: pathlib.Path) -> None:
    """One pop-up frame: the phrase in the accent colour on a slightly tilted navy tag, centred on POP_Y; scale animates the pop."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if text and scale > 0:
        font = ImageFont.truetype(str(FONT), int(60 * scale))
        x0, y0, x1, y1 = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), text, font=font)
        pw, ph = x1 - x0 + int(56 * scale), y1 - y0 + int(40 * scale)
        tag = Image.new("RGBA", (pw + 40, ph + 40), (0, 0, 0, 0))
        td = ImageDraw.Draw(tag)
        td.rounded_rectangle((20, 20, 20 + pw, 20 + ph), radius=int(22 * scale), fill=NAVY + (240,))
        td.text((20 + int(28 * scale) - x0, 20 + int(20 * scale) - y0), text, font=font, fill=LIME + (255,))
        tag = tag.rotate(4, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(tag, (W // 2 - tag.width // 2, POP_Y - tag.height // 2))
    img.save(path)


def _pops_concat(pops: list[dict], length: float, folder: pathlib.Path) -> pathlib.Path:
    folder.mkdir(exist_ok=True)
    blank = folder / "blank.png"
    _sticker_png("", 0, blank)
    lines, t = ["ffconcat version 1.0"], 0.0
    for k, p in enumerate(pops):
        if p["t0"] > t:
            lines += [f"file '{blank}'", f"duration {p['t0'] - t:.3f}"]
        frame = 1 / 30  # it pops: small, a little too big, settled, and shrinks away
        for j, (sc, dur) in enumerate(((0.55, frame), (0.9, frame), (1.08, frame), (1.0, p["t1"] - p["t0"] - 5 * frame), (0.75, frame))):
            f = folder / f"pop{k}_{j}.png"
            _sticker_png(p["text"], sc, f)
            lines += [f"file '{f}'", f"duration {max(dur, 0.01):.3f}"]
        t = p["t1"]
    lines += [f"file '{blank}'", f"duration {max(length - t, 0.1):.3f}", f"file '{blank}'"]
    (folder / "pops.ffconcat").write_text("\n".join(lines) + "\n")
    return folder / "pops.ffconcat"


def _levels(src: pathlib.Path) -> list[float]:
    """The sound's level in dB for every tenth of a second."""
    e = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn", "-af",
                        "aresample=48000,asetnsamples=n=4800,astats=metadata=1:reset=1,"
                        "ametadata=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    return [float(x) for x in re.findall(r"RMS_level=(-?\d+(?:\.\d+)?)", e)]


def _voice(src: pathlib.Path, first_word: float) -> str:
    """The sound chain, fitted to this recording (on a test clip, the voice-to-hum gap in a real pause went from 18 dB
    levelled only to about 35 dB with the learned denoise and the soft gate):
    - a high-pass at 70 Hz: the rumble under the voice goes, phones can't play it anyway
    - the room's hum learned from the quiet lead-in when there is one (afftdn sample_noise), else tracked
    - a soft gate, its threshold set between this recording's own hum and voice levels
    - a little warmth under 170 Hz, a little clarity at 3 kHz, gentle compression, then -14 LUFS measured first and set
      in one straight step, so the pauses are not lifted again"""
    levels = sorted(x for x in _levels(src) if x > -100)
    noise = levels[len(levels) // 10] if levels else -60.0
    loud = [x for x in levels if x > noise + 10]
    voice = loud[len(loud) // 2] if loud else noise + 20
    lead = _levels_window(src, 0.1, 0.7) if first_word >= 0.9 else None
    learned = lead is not None and lead <= noise + 6
    gate = 10 ** ((noise + 0.4 * (voice - noise)) / 20)
    # gentle on purpose: a stronger version measured thinner than the raw recording
    chain = ("highpass=f=70," + ("asendcmd=c='0.1 afftdn sn start; 0.8 afftdn sn stop'," if learned else "")
             + f"afftdn=nr=12:nf=-50:tn={0 if learned else 1},agate=threshold={gate:.5f}:ratio=2:attack=5:release=300:range=0.4,"
             "lowshelf=g=3:f=170,equalizer=f=3000:t=q:w=1.2:g=1.5,acompressor=threshold=-21dB:ratio=2.5:attack=8:release=120:makeup=1")
    e = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn", "-af",
                        chain + ",loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    m = json.loads(e[e.rindex("{"):e.rindex("}") + 1])
    return (chain + f",loudnorm=I=-14:TP=-1:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")


def _levels_window(src: pathlib.Path, start: float, length: float) -> float:
    e = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start), "-t", str(length), "-i", str(src), "-vn",
                        "-af", "astats=metadata=0", "-f", "null", "-"], capture_output=True, text=True).stderr
    hit = re.search(r"RMS level dB: (-?\d+(?:\.\d+)?)", e)
    return float(hit.group(1)) if hit else 0.0


def _band(size: int) -> tuple[int, int]:
    """(offset, height) of a caption's text band at this font size, from the tallest and deepest letters, so every
    box has the same height whatever its words."""
    from PIL import Image, ImageDraw, ImageFont
    x0, y0, x1, y1 = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), "ÅÆØÉÜgjpqy", font=ImageFont.truetype(str(FONT), size))
    return y0, y1 - y0


def _box_h(size: int) -> int:
    return _band(size)[1] + 2 * 20 + 6


def _caption_png(words: list[str], active: int | None, path: pathlib.Path, top: int, size: int = 58,
                 ink: tuple = (255, 255, 255), box: bool = True, edge: str | None = None) -> None:
    """One caption: a navy box whose top edge is always at `top` and whose height never changes, white words, the
    word being said in the accent colour. Without the box the letters get `edge`: "shadow" (a soft navy shadow),
    "outline", or None."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if words:
        d = ImageDraw.Draw(img)
        font = ImageFont.truetype(str(FONT), size)
        x0, _, x1, _ = d.textbbox((0, 0), " ".join(words), font=font)
        tw, (by0, bh) = x1 - x0, _band(size)
        pad_x, pad_y = 30, 20
        if box:
            d.rounded_rectangle((W // 2 - tw // 2 - pad_x, top, W // 2 + tw // 2 + pad_x, top + bh + 2 * pad_y + 6),
                                radius=24, fill=NAVY + (232,))
        x0_text, y, space = W // 2 - tw // 2 - x0, top + pad_y - by0, d.textlength(" ", font=font)
        if not box and edge == "shadow":
            shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sd, x = ImageDraw.Draw(shade), x0_text
            for word in words:
                sd.text((x, y + 3), word, font=font, fill=NAVY + (240,), stroke_width=max(2, size // 20), stroke_fill=NAVY + (240,))
                x += d.textlength(word, font=font) + space
            img.alpha_composite(shade.filter(ImageFilter.GaussianBlur(max(4, size // 9))))
        x = x0_text
        for k, word in enumerate(words):
            d.text((x, y), word, font=font, fill=(LIME if k == active else ink) + (255,),
                   **({"stroke_width": max(3, size // 12), "stroke_fill": NAVY + (255,)} if not box and edge == "outline" else {}))
            x += d.textlength(word, font=font) + space
    img.save(path)


def _frames(src: pathlib.Path, out: pathlib.Path, duration: float, crop: str) -> list[pathlib.Path]:
    """Five frames of the cropped video, for finding the face and reading the logo corner."""
    folder = out / "faces"
    folder.mkdir(exist_ok=True)
    paths = []
    for k, at in enumerate((0.15, 0.35, 0.55, 0.75, 0.9)):
        p = folder / f"f{k}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{duration * at:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", crop, str(p)], check=True)
        paths.append(p)
    return paths


def _brand_png(path: pathlib.Path, frames: list[pathlib.Path]) -> None:
    """Only your logo, small, in the top right, clear of the captions: the dark-background logo on a dark corner, the
    light one on a light corner, so it stays quiet but readable. No logo configured: nothing."""
    from PIL import Image, ImageStat
    corner = (W - MARGIN - 160, MARGIN - 10, W, MARGIN + 110)
    light = sum(ImageStat.Stat(Image.open(f).convert("L").crop(corner)).mean[0] for f in frames) / max(1, len(frames)) > 140
    pick = (LOGO_ON_LIGHT or LOGO) if light else (LOGO or LOGO_ON_LIGHT)
    if not pick or not C.path(pick).exists():
        Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(path)
        return
    logo = Image.open(C.path(pick)).convert("RGBA")
    logo = logo.crop(logo.getchannel("A").getbbox())
    logo = logo.resize((round(logo.width * LOGO_H / logo.height), LOGO_H), Image.LANCZOS)
    logo.putalpha(logo.getchannel("A").point(lambda a: int(a * 0.9)))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.alpha_composite(logo, (W - MARGIN - logo.width, MARGIN))
    img.save(path)


def _faces(frames: list[pathlib.Path]) -> list[dict]:
    """The face (the largest one) on each frame, from the Mac's own Vision: x, y, w, h as parts of the frame, y from the top."""
    binary = C.CACHE / "bin" / "photo_faces"  # built by content_engine_photos.py --install-app
    if not (binary.exists() and frames):
        return []
    r = subprocess.run([str(binary), *map(str, frames)], capture_output=True, text=True, timeout=120)
    found = [json.loads(line).get("faces") or [] for line in r.stdout.splitlines()]
    return [max(f, key=lambda f: f["w"] * f["h"]) for f in found if f]


def cover_time(video: pathlib.Path, length: float) -> float:
    """When the cover frame is taken: a sharp moment with the eyes open in the first 15 seconds (a cover taken at 1 s
    can catch a blink). Open eyes put bright whites next to the dark iris, so the eye band of the face spans more light
    to dark than closed lids."""
    fallback = min(1.0, length / 3)
    try:
        import cv2
        import numpy as np
    except ImportError:
        return fallback
    face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap, best, t = cv2.VideoCapture(str(video)), (-1.0, fallback), 1.0
    try:
        while t < min(15.0, length - 0.5):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, img = cap.read()
            if ok:
                g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                found = face.detectMultiScale(g, 1.1, 6, minSize=(g.shape[1] // 5, g.shape[1] // 5))
                if len(found):
                    x, y, w, h = max(found, key=lambda f: f[2] * f[3])
                    band = g[y + int(h * .30):y + int(h * .46), x + int(w * .15):x + int(w * .85)]
                    spread = float(np.percentile(band, 97) - np.percentile(band, 3))
                    sharp = cv2.Laplacian(g[y:y + h, x:x + w], cv2.CV_64F).var()
                    score = spread - (15 if sharp < 150 else 0)  # a blurred frame (a hand moving) only as a last resort
                    if score > best[0]:
                        best = (score, t)
            t += 0.5
    finally:
        cap.release()
    return best[1]


def _face_centre(faces: list[dict]) -> tuple[float, float]:
    """Where the punch-ins zoom to: the middle of the face. At the joke zoom the mouth moves about 20 px down, still
    well above the captions under the chin."""
    if not faces:
        return 0.5, 0.55
    mid = lambda xs: sorted(xs)[len(xs) // 2]
    return mid([f["x"] + f["w"] / 2 for f in faces]), mid([f["y"] + f["h"] / 2 for f in faces])


def _caption_y(faces: list[dict]) -> int:
    """The fixed top line of the captions: just under the chin so they never cover the mouth, and clear of LinkedIn's
    own controls at the bottom. The face on five frames of the cropped video; no face found = the usual spot."""
    tallest = max(_box_h(58), _box_h(WORD_SIZE))
    lowest = H - BOTTOM_SAFE - tallest  # the top line, so both styles clear LinkedIn's controls
    chins = [(f["y"] + f["h"]) * H for f in faces]
    if not chins:
        return min(int(H * 0.72), lowest)
    chin = sorted(chins)[len(chins) // 2]
    return int(min(max(chin + 18, H * 0.66), lowest))  # the top line of every caption, just under the chin


def _srt(caps: list[dict]) -> str:
    def t(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    return "\n".join(f"{i}\n{t(c['s'])} --> {t(c['e'])}\n{c['text']}\n" for i, c in enumerate(caps, 1))


FILLERS = {"øh", "øhm", "øhh", "øhmm", "øm", "ømm", "ehm", "eh", "æh", "æhm", "hm", "hmm", "mm", "mhm", "uh", "um", "umm", "erm"}
# Cut to the speech, measured on the sound itself: start and end only where the speaking is, and pauses shortened, so
# the video runs faster. Whisper's word times run up to half a second early (a mouth click can "start" a word), so the
# trim and the pauses come from the cleaned sound, not from the words. Where inside a pause to cut is picked on the
# picture: of all the frame pairs a pause allows, the two most alike are joined (a cut between half-closed and wide-open
# eyes looks very wrong). Cuts in the middle of speech are off.
HOP = 0.02  # the sound is read in 20 ms steps
QUIET_MIN, QUIET_KEEP = 0.3, 0.2  # a quiet shorter than QUIET_MIN is speech rhythm and stays; of a longer one at most QUIET_KEEP stays
LEAD, TAIL = 0.06, 0.12  # before the first sound and after the last
EDGE_MIN = 0.25  # a sound at the very start or end shorter than this is a click or a breath, not a word
XF = 0.04  # every cut: the sound of the two parts cross-faded this long, so room tone and voice run on
SOFT, SOFT_JUMP, HARD_JUMP = 3 / 30, 5.0, 10.0  # a cut whose picture still jumps more than SOFT_JUMP blends over SOFT
# instead; over HARD_JUMP (a head turn, a hand) a blend shows a ghost double image, so it stays a hard cut
# (mean grey difference between the two frames; while someone talks two frames in a row differ 1 to 3)
SOFT_CUTS = True
FPS = 30
CODE = __import__("hashlib").md5(pathlib.Path(__file__).read_bytes()).hexdigest()  # a failed recording is tried again once this changes


class BrokenVideo(RuntimeError):
    """The finished video failed its own check (_broken): never uploaded."""


def _snap(segs: list[tuple[float, float]], soft: set[int]) -> list[tuple[float, float]]:
    """Every cut on a whole frame, so the picture and the sound keep one clock. The parts used to be trimmed at any
    moment of a recording with an uneven frame rate (iPhone: 120 on paper, about 30 in fact), so each part's picture
    came out up to a frame longer or shorter than its sound; added up over the cuts, a soft cut's cross-fade began
    after the picture before it had ended, and ffmpeg stopped the picture there (the picture froze while the sound
    went on). Here a part's picture is a whole number
    of frames, and its sound is exactly that long plus the cross-fade it shares with the next part."""
    out = []
    for i, (a, b) in enumerate(segs):
        hard = i < len(segs) - 1 and i not in soft  # a hard cut shows the picture XF shorter; the sound overlaps by XF
        first = round(a * FPS)
        last = max(first + 1, round(((b - XF) if hard else b) * FPS))
        out.append((first / FPS, last / FPS + (XF if hard else 0.0)))
    return out


def _broken(path: pathlib.Path) -> str | None:
    """Why a finished video must not be handed over, or None: picture and sound of different length, a frozen picture
    (2.5 s or more without a single changing pixel: nobody talking on camera is that still) or a black stretch (1 s or
    more), so a broken cut never reaches a post."""
    pic, snd = _lengths(path)
    if abs(pic - snd) > 0.25:
        return f"the picture is {pic:.1f} s but the sound {snd:.1f} s"
    log = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vf", "freezedetect=n=0.001:d=2.5,blackdetect=d=1:pix_th=0.10",
                          "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    frozen = re.search(r"freeze_start: ([\d.]+)", log)
    if frozen:
        return f"the picture freezes at {float(frozen.group(1)):.1f} s"
    black = re.search(r"black_start:([\d.]+)", log)
    if black:
        return f"the picture goes black at {float(black.group(1)):.1f} s"
    return None


def _lengths(path: pathlib.Path) -> tuple[float, float]:
    """(picture, sound) of a finished video in seconds, read from the streams themselves."""
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                                  capture_output=True, text=True, check=True).stdout)
    get = lambda kind: max((float(x.get("duration") or 0) for x in d["streams"] if x["codec_type"] == kind), default=0.0)
    return get("video"), get("audio")


def _loudness(clean: pathlib.Path):
    """The cleaned sound in HOP steps (dB), the line between room and voice (a quarter of the way from the room to the
    normal voice, at least 6 dB over the room), and the talking level."""
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clean), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, np.int16).astype(float) / 32768
    n = int(16000 * HOP)
    db = 20 * np.log10(np.sqrt(np.mean(x[: len(x) // n * n].reshape(-1, n) ** 2, axis=1)) + 1e-9)
    floor, voice = np.percentile(db, 10), np.median(db)
    line = floor + max(6.0, 0.25 * (voice - floor))
    return db, line, (np.median(db[db > line]) if (db > line).any() else voice)


FILLER_HINT = "Øhm, så øh, det er jo sådan, øhm, at vi øh ... ømm, altså. "


def _filler_times(wav: pathlib.Path, out: pathlib.Path, db, line: float, heard: list[dict] = ()) -> list[tuple[float, float]]:
    """Where the speaker says "øh"/"øhm". The normal transcription leaves them out, so a second Whisper pass is told to
    write them, with the hint on every 30 s window; that pass is not used for the captions (it can also change real
    words). One counts only with sound under at least half of it (the hint invents them in silence too), and its edges
    move to the quietest moment within 0.12 s, so no half "ø" is left. Also a sound between two of the captions' words
    (`heard`) that neither pass wrote as a word: next to an "øh" of the second pass, or standing alone (0.08 s from both
    words: an "øh", a breath, a smack); one of 0.3 s or more hanging on the word before goes too, all but its first 0.1
    s (which can be the word's own end)."""
    import hashlib
    import numpy as np
    key = hashlib.md5(wav.read_bytes() + FILLER_HINT.encode()).hexdigest()
    stored = out / "fillers.json"
    cached = json.loads(stored.read_text()) if stored.exists() else {}
    if cached.get("md5") == key:
        words = cached["words"]
    else:
        res = _whisper().transcribe(str(wav), language="da", word_timestamps=True, initial_prompt=FILLER_HINT + GLOSSARY,
                                    condition_on_previous_text=False, fp16=False, carry_initial_prompt=True)
        words = [{"w": w["word"].strip(), "s": float(w["start"]), "e": float(w["end"])} for seg in res["segments"] for w in seg.get("words", [])]
        stored.write_text(json.dumps({"md5": key, "words": words}, ensure_ascii=False))

    def quietest(t: float) -> float:
        i0, i1 = max(0, int((t - 0.12) / HOP)), min(len(db), int((t + 0.12) / HOP) + 1)
        return (i0 + int(np.argmin(db[i0:i1]))) * HOP if i1 > i0 else t
    found = []
    for w in words:
        i0, i1 = int(w["s"] / HOP), max(int(w["s"] / HOP) + 1, int(w["e"] / HOP))
        if _letters(w["w"]) in FILLERS and i1 <= len(db) and (db[i0:i1] > line).mean() >= 0.5:
            a, b = quietest(w["s"]), quietest(w["e"])
            found.append((a, b) if b - a >= 0.08 else (w["s"], w["e"]))
    ums = [w for w in words if _letters(w["w"]) in FILLERS]
    said = [w for w in words if _letters(w["w"]) not in FILLERS]
    covered = np.zeros(len(db), bool)
    for w in heard:
        covered[max(0, int((w["s"] - 0.04) / HOP)):int((w["e"] + 0.04) / HOP) + 1] = True
    begin = None
    for i, on in enumerate(list((db > line) & ~covered) + [False]):
        if on and begin is None:
            begin = i
        elif not on and begin is not None:
            a, b, begin = begin * HOP, i * HOP, None
            if b - a < 0.12 or any(w["s"] < b and w["e"] > a for w in said):
                continue  # a click, or a word the second pass heard there
            prev = max((w["e"] for w in heard if w["e"] <= a + 0.04), default=-9.0)
            nxt = min((w["s"] for w in heard if w["s"] >= b - 0.04), default=9e9)
            if any(w["s"] < b + 0.3 and w["e"] > a - 0.3 for w in ums) or (a - prev >= 0.08 and nxt - b >= 0.08):
                found.append((a, b))
            elif b - a >= 0.3 and nxt - b >= 0.08:  # 0.3 s+ hanging on the word before: its first 0.1 s may be the word's end
                found.append((a + 0.1, b))
    return sorted(found)


def _speech(db, line: float, talk: float, fillers: list[tuple[float, float]] = ()) -> list[tuple[float, float]]:
    """(start, end) of every stretch of talking, from the cleaned sound (`_loudness`). Sounds under 0.08 s are clicks,
    and under 0.2 s without a peak within 8 dB of the voice are breaths, both count as quiet, as do the "øh"s
    (`_filler_times`); quiet under QUIET_MIN joins its neighbours, and a sound under EDGE_MIN before the first or after
    the last real stretch is dropped."""
    loud = list(db > line)
    for a, b in fillers:  # the ums go with the quiet
        i0, i1 = int(a / HOP), int(b / HOP) + 1
        loud[i0:i1] = [False] * len(loud[i0:i1])
    raw, begin = [], None
    for i, on in enumerate(loud + [False]):
        if on and begin is None:
            begin = i
        elif not on and begin is not None:
            if (i - begin) * HOP >= 0.2 or ((i - begin) * HOP >= 0.08 and db[begin:i].max() >= talk - 8):
                raw.append((begin * HOP, i * HOP))
            begin = None
    runs = []
    for a, b in raw:
        if runs and a - runs[-1][1] < QUIET_MIN and not any(a > fa and runs[-1][1] < fb for fa, fb in fillers):
            runs[-1] = (runs[-1][0], b)
        else:
            runs.append((a, b))
    while len(runs) > 1 and runs[0][1] - runs[0][0] < EDGE_MIN:
        runs.pop(0)
    while len(runs) > 1 and runs[-1][1] - runs[-1][0] < EDGE_MIN:
        runs.pop()
    return runs


def _cuts(src: pathlib.Path, out: pathlib.Path, crop: str, speech: list[tuple[float, float]], duration: float,
          fillers: list[tuple[float, float]] = ()):
    """The parts to keep, from LEAD before the first sound to TAIL after the last, every pause cut down to QUIET_KEEP;
    where in the pause is picked on the picture: of the frame pairs that remove enough, the two that look most alike.
    A gap holding an "øh" loses all of it: only real silence either side of it may stay. Also returns how much the
    picture still jumps at each cut (None where an "øh" decided the cut)."""
    from PIL import Image, ImageChops, ImageStat
    if not speech:
        return [(0.0, duration)], []
    folder = out / "cutframes"
    folder.mkdir(exist_ok=True)
    keep, jumps, start = [], [], max(0.0, speech[0][0] - LEAD)
    for (_, e), (s, _) in zip(speech, speech[1:]):
        um = [(max(a, e), min(b, s)) for a, b in fillers if a < s and b > e]
        if um:
            left = min(min(a for a, _ in um) - e, QUIET_KEEP)
            right = min(s - max(b for _, b in um), QUIET_KEEP - left)
            keep.append((start, e + left))
            jumps.append(None)
            start = s - right
            continue
        lo, hi = e + 0.04, s - 0.03
        for f in folder.glob("*.png"):
            f.unlink()
        subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", str(src),
                        "-vf", f"fps=30,{crop},scale=216:270", str(folder / "%04d.png")], check=True)
        frames = [(lo + k / 30, Image.open(f).convert("L")) for k, f in enumerate(sorted(folder.glob("*.png")))]
        frames = [(t, im) for t, im in frames if t - lo <= 1.0 or hi - t <= 1.0]  # a long pause: its two ends
        need = (s - e) - QUIET_KEEP
        best = min(((ImageStat.Stat(ImageChops.difference(a, b)).mean[0], -(tb - ta), ta, tb)
                    for i, (ta, a) in enumerate(frames) for tb, b in frames[i + 1:] if tb - ta >= need), default=None)
        o, i, jump = (best[2], best[3], round(best[0], 1)) if best else (e + QUIET_KEEP / 2, s - QUIET_KEEP / 2, None)
        keep.append((start, o + 1 / 30))  # the out frame is the last one shown
        jumps.append(jump)
        start = i
    keep.append((start, min(duration, speech[-1][1] + TAIL)))
    return keep, jumps


def _remap(words: list[dict], keep: list[tuple[float, float]], lap: list[float] = ()) -> tuple[list[dict], float]:
    """The words moved onto the cut video, "øh"/"øhm" left out of the captions; a word in a removed piece lands on the cut.
    lap[k]: how much part k overlaps the next one (the cross-fade at that cut)."""
    spans, length = [], 0.0
    for k, (a, b) in enumerate(keep):
        spans.append((a, b, length))
        length += b - a - (lap[k] if k < len(lap) else 0.0)

    def moved(t: float) -> float:
        for a, b, n in spans:
            if t <= b:
                return n + max(0.0, t - a)
        return length
    return [{"w": w["w"], "s": moved(w["s"]), "e": min(moved(w["e"]), length)} for w in words if _letters(w["w"]) not in FILLERS], length


def _source_time(t: float, keep: list[tuple[float, float]], lap: list[float] = ()) -> float:
    """A moment on the cut video back on the recording (the other way round from _remap)."""
    n = 0.0
    for k, (a, b) in enumerate(keep):
        step = b - a - (lap[k] if k < len(lap) else 0.0)
        if t < n + step or k == len(keep) - 1:
            return min(a + max(0.0, t - n), b)
        n += step
    return 0.0


def process(src: pathlib.Path, name: str | None = None, styles: tuple[str, ...] = None, soft_cuts: bool = SOFT_CUTS,
            zoom_style: str = "punch", said=()) -> dict:
    """styles: 'line' (a short line, the spoken word lime) and/or 'word' (one big lime word at a time); the first is
    the video that goes with the post. soft_cuts: a short cross-fade where a pause cut still jumps. zoom_style: "punch"
    (punch-ins on the strong lines) or "max" (a test: a switch at every cut and break, the strong lines closest).
    said: the author's own corrections, (what the captions said, what was said), see _said()."""
    styles = styles or STYLES
    name = name or re.sub(r"[^\w-]+", "_", src.stem)[:60]
    out = OUT / name
    out.mkdir(parents=True, exist_ok=True)
    info = _probe(src)
    if not info["audio"]:
        raise SystemExit("the file has no sound")
    if info["hdr"]:
        src = _sdr(src, out)
        info = _probe(src)
    wav = out / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    import hashlib
    heard, key = out / "words.json", hashlib.md5(wav.read_bytes()).hexdigest()  # a redo of the same recording skips Whisper
    cached = json.loads(heard.read_text()) if heard.exists() else {}
    words = cached["words"] if cached.get("md5") == key else _words(wav)
    heard.write_text(json.dumps({"md5": key, "words": words}, ensure_ascii=False))
    words, own, keep, unmatched = _said(words, said)
    hit = _match_post(" ".join(w["w"] for w in words))
    post = json.loads(hit[0].read_text())[hit[1] - 1] if hit else None
    try:  # on the recording's own timeline, where the sound check can hear it; a failed check never costs the video
        judge = _judge(wav, words)
        words, fixes = _proofread(words, post, judge, keep=keep)
        checks, proof_error = judge.log, None
    except Exception as e:  # noqa: BLE001
        fixes, checks, proof_error = [], [], f"captions not proofread ({type(e).__name__}: {e})"[:200]
    fixes = own + fixes
    if unmatched:
        proof_error = (f"your correction(s) for {', '.join(repr(u) for u in unmatched)} found nothing to replace; "
                       + (proof_error or ""))[:300]
    # the sound is cleaned on the whole recording (the hum is learned from the quiet lead-in), then cut with the picture
    clean = out / "clean.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-af", _voice(src, words[0]["s"] if words else 0.0),
                    "-ar", "48000", str(clean)], check=True)
    # out_range=tv: phones record full range, web players expect limited
    crop = f"scale={W}:{H}:force_original_aspect_ratio=increase:out_range=tv,crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.35"
    db, line, talk = _loudness(clean)
    fillers = _filler_times(wav, out, db, line, words)
    segs, jumps = _cuts(src, out, crop, _speech(db, line, talk, fillers), info["duration"], fillers)
    soft = {k for k, j in enumerate(jumps) if soft_cuts and (j is None or SOFT_JUMP < j <= HARD_JUMP)}  # an øh cut is inside speech: blend it
    segs = _snap(segs, soft)
    lap = [SOFT if k in soft else XF for k in range(len(segs) - 1)]
    cut_words, length = _remap(words, segs, lap)
    try:
        moments = _moments(cut_words)
    except Exception as e:  # noqa: BLE001
        moments = []
        proof_error = proof_error or f"no zooms or pop-ups ({type(e).__name__}: {e})"[:200]
    calm = _calm_until(cut_words)
    try:
        phrases = _pop_phrases(cut_words)
    except Exception as e:  # noqa: BLE001 (the moments' own joke and key phrases instead)
        phrases = []
        proof_error = proof_error or f"pop-ups from the moments only ({type(e).__name__}: {e})"[:200]
    pops = _pops(phrases or moments, cut_words, calm)
    (out / "edit.json").write_text(json.dumps({"fixes": fixes, "checks": checks, "moments": moments, "phrases": phrases, "pops": pops,
                                               "error": proof_error},
                                              ensure_ascii=False, indent=1))
    caps = _captions(cut_words)
    (out / "captions.srt").write_text(_srt(caps))
    (out / "transcript.txt").write_text(" ".join(c["text"] for c in caps) + "\n")
    frames = out / "caps"
    frames.mkdir(exist_ok=True)
    shots = _frames(src, out, info["duration"], crop)
    faces = _faces(shots)
    top = _caption_y(faces)
    bounds, t = [], 0.0
    for k, (a, b) in enumerate(segs[:-1]):
        t += b - a - lap[k]
        bounds.append(t)
    pieces, spots = (_zoom_pieces(moments, length, calm), []) if zoom_style == "punch" else _max_pieces(moments, cut_words, bounds, length)
    centres = _zoom_centres(src, out, crop, pieces, segs, lap) if pieces and not spots else []
    zoom = ("," + _zoom_filter(pieces, *_face_centre(faces), spots, centres)) if pieces else ""
    pop_input = ["-f", "concat", "-safe", "0", "-i", str(_pops_concat(pops, length, out / "pops"))] if pops else []
    mix, sound_out = "", "[acut]"
    if pops:  # a sound as each pop-up lands, mixed under the voice
        _sfx_track(out / "sfx.wav", pops, length)
        pop_input += ["-i", str(out / "sfx.wav")]
        sound_out = "[aout]"
        mix = "[acut][5:a]amix=inputs=2:normalize=0:duration=first[aout];"
    _caption_png([], None, frames / "blank.png", top)
    _brand_png(out / "tag.png", shots)
    n = len(segs)
    # one steady 30 fps picture first (frame k at k/30 s of the recording, the sound's clock), then each part cut on
    # whole frames (_snap), so every part's picture is exactly as long as its share of the sound
    cut = (f"[0:v]fps={FPS}:start_time=0,settb=AVTB,split={n}" + "".join(f"[s{i}]" for i in range(n)) + ";"
           + f"[3:a]asplit={n}" + "".join(f"[t{i}]" for i in range(n)) + ";"
           + "".join(f"[s{i}]trim=start_frame={round(a * FPS)}:end_frame={round((b - (XF if i < n - 1 and i not in soft else 0)) * FPS)},"
                     f"setpts=PTS-STARTPTS[v{i}];"
                     f"[t{i}]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS[a{i}];" for i, (a, b) in enumerate(segs)))
    chain, sound, t = "v0", "a0", segs[0][1] - segs[0][0]
    for i in range(1, n):  # the sound always cross-fades; the picture blends at a soft cut and joins hard (one XF shorter) at the rest
        part = segs[i][1] - segs[i][0]
        if i - 1 in soft:
            cut += f"[{chain}][v{i}]xfade=transition=fade:duration={SOFT:.3f}:offset={t - SOFT:.3f}[x{i}];"
        else:
            cut += f"[{chain}][v{i}]concat=n=2:v=1:a=0[x{i}];"
        cut += f"[{sound}][a{i}]acrossfade=d={lap[i - 1]:.3f}:c1=tri:c2=tri[y{i}];"
        t += part - lap[i - 1]
        chain, sound = f"x{i}", f"y{i}"
    cut += f"[{chain}]null[vcut];[{sound}]anull[acut];" + mix
    videos = {}
    for style in styles:
        lines, t = ["ffconcat version 1.0"], 0.0
        for i, c in enumerate(caps):
            if c["s"] > t + 0.01:
                lines += [f"file '{frames / 'blank.png'}'", f"duration {c['s'] - t:.3f}"]
            words_c = [w["w"] for w in c["words"]]
            cuts = [c["s"]]  # one frame per word: the word being said turns lime (line), or is the only word (word)
            for w in c["words"][1:]:
                cuts.append(min(max(w["s"], cuts[-1]), c["e"]))
            cuts.append(c["e"])
            for k in range(len(words_c)):
                if cuts[k + 1] - cuts[k] > 0.001:
                    f = frames / f"{style}_{i:04}_{k:02}.png"
                    if style.startswith("word"):  # one word to read at a time ("et ord-subtitelformat")
                        _caption_png([words_c[k]], None, f, top, size=WORD_SIZE, ink=LIME, box=style == "word",
                                     edge={"word_bare": "shadow", "word_outline": "outline"}.get(style))
                    else:
                        _caption_png(words_c, k, f, top)
                    lines += [f"file '{f}'", f"duration {cuts[k + 1] - cuts[k]:.3f}"]
            t = c["e"]
        lines += [f"file '{frames / 'blank.png'}'", f"duration {max(length - t, 0.1):.3f}", f"file '{frames / 'blank.png'}'"]
        (out / f"caps_{style}.ffconcat").write_text("\n".join(lines) + "\n")
        videos[style] = out / (f"{name}.mp4" if style == styles[0] else f"{name}_{style}.mp4")
        vf = (cut + f"[vcut]{crop},setsar=1,fps=30,{LOOK}{zoom}[v];[2:v]format=rgba[b];[v][b]overlay=0:0[vb];"
              f"[1:v]format=rgba[c];[vb][c]overlay=0:0:eof_action=pass"
              + (",format=yuv420p[out]" if not pops else "[vc];[4:v]format=rgba[p];[vc][p]overlay=0:0:eof_action=pass,format=yuv420p[out]"))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-f", "concat", "-safe", "0",
                        "-i", str(out / f"caps_{style}.ffconcat"), "-i", str(out / "tag.png"), "-i", str(clean), *pop_input,
                        "-filter_complex", vf, "-map", "[out]", "-map", sound_out,
                        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-maxrate", "8M", "-bufsize", "16M",  # LinkedIn re-encodes anyway;
                        # uncapped, the sharpening made 16 Mbit/s (185 MB for 92 s)
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-metadata", f"comment={MARK}",
                        "-movflags", "+faststart", str(videos[style])], check=True)
        why = _broken(videos[style])
        if why:  # a broken cut: stop here, so nothing is uploaded or placed under a post
            raise BrokenVideo(f"{videos[style].name}: {why}")
    final = videos[styles[0]]
    cover = out / f"{name}_cover.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_time(final, length):.2f}", "-i", str(final),
                    "-frames:v", "1", str(cover)], check=True)
    return {"video": final, "videos": videos, "cover": cover, "srt": out / "captions.srt",
            "transcript": (out / "transcript.txt").read_text(), "captions": len(caps), "seconds": round(length, 1),
            "cut": round(info["duration"] - length, 1), "parts": len(segs), "jumps": jumps, "ums": len(fillers), "soft": len(soft), "post": hit, "fixes": fixes,
            "zooms": len([p for p in pieces if p[2] != ZOOM_FUNNY]), "pops": [p["text"] for p in pops], "error": proof_error}


# ---------- Drive: Video inbox in, Finished videos out ----------
MATCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["post"], "properties": {"post": {"type": "integer"}}}
MATCH_SYSTEM = """The author records videos for LinkedIn posts: they talk freely to camera from the post's prompts, so the
recording tells the same story or makes the same point as its post, in other words. Some recordings belong to no post at
all. Given the transcript and a few numbered candidate posts, answer the number of the post this recording is the video
for, or 0 when it tells a different story than all of them. Sharing a topic or common words is not enough."""


def _match_post(transcript: str) -> tuple[pathlib.Path, int] | None:
    """The video post this recording belongs to, or None. Word overlap only makes the shortlist: a long free talk
    shares a third of the common words with almost any post, so overlap alone picks wrong posts. The model reads the
    shortlist and picks the post being told, or none."""
    said = set(re.findall(r"[a-zæøå]{4,}", transcript.lower()))
    rows = []
    for bf in sorted(C.BATCHES.glob("*Engine_Batch_*.json"), reverse=True):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", bf.name):  # not _visuals.json or .feedback.json
            continue
        for i, p in enumerate(json.loads(bf.read_text()), 1):
            if not isinstance(p, dict) or p.get("format") != "video" or p.get("status") == "dropped":
                continue
            words = set(re.findall(r"[a-zæøå]{4,}", p["text"].lower()))
            rows.append((len(said & words) / max(1, len(words)), bf, i, p))
    short = sorted(rows, key=lambda r: -r[0])[:5]
    if not short or short[0][0] < 0.15:
        return None
    ask = f"The transcript:\n{transcript}\n\n" + "\n\n".join(
        f"Post {k}: {p.get('title', '')}\n{p['text']}" for k, (_, _, _, p) in enumerate(short, 1))
    try:
        k = _llm(ask, MATCH_SCHEMA, MATCH_SYSTEM, max_tokens=200, cache="match")["post"]
    except Exception:  # noqa: BLE001 (no post rather than a wrong one; the note asks which)
        return None
    return (short[k - 1][1], short[k - 1][2]) if 1 <= k <= len(short) else None


def finished_name(hit: tuple[pathlib.Path, int] | None, transcript: str, day: dt.date) -> str:
    """A name that says what it is: date, batch, post and
    title, or for a video that fits no post, date and its first words."""
    if hit:
        title = json.loads(hit[0].read_text())[hit[1] - 1].get("title", "")
        b = re.search(r"Batch_(\d+)", hit[0].name).group(1)
        base = f"{day:%Y-%m-%d} Batch {b} post {hit[1]} {title}"
    else:
        base = f"{day:%Y-%m-%d} Video " + " ".join(re.findall(r"[\wæøåÆØÅ]+", transcript)[:5])
    return re.sub(r'[\\/:*?"<>|]+', "", base).strip()[:90]


def _lock():
    """The 10-minute job, a redo and a manual run must never work on videos at the same time; None = another one is busy."""
    import fcntl
    OUT.mkdir(parents=True, exist_ok=True)
    lock = open(OUT / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another video run is busy")
        return None
    return lock


def _download(drive, file_id: str, name: str) -> pathlib.Path:
    from googleapiclient.http import MediaIoBaseDownload
    work = OUT / file_id
    work.mkdir(parents=True, exist_ok=True)
    raw = work / ("raw" + (pathlib.Path(name).suffix or ".mov"))
    with raw.open("wb") as fh:
        dl = MediaIoBaseDownload(fh, drive.files().get_media(fileId=file_id), chunksize=16 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return raw


def _note_lines(res: dict) -> list[str]:
    """What the proofreading and the punch-ins did, for the inbox note, so it can be checked."""
    lines = []
    if res.get("fixes"):
        lines.append("Captions proofread against the post and checked against the sound; mishearings fixed: "
                     + "; ".join(f"{a} to {b}" for a, b in res["fixes"][:10]) + ("; and more" if len(res["fixes"]) > 10 else "") + ".")
    if res.get("zooms") or res.get("pops"):
        lines.append(f"{res.get('zooms', 0)} punch-ins on your strongest lines"
                     + (", on screen: " + ", ".join(f'"{p}"' for p in res["pops"]) if res.get("pops") else "") + ".")
    if res.get("error"):
        lines.append(f"Not done this time: {res['error']}")
    return lines


def _link_post(hit: tuple[pathlib.Path, int], video_link: str, cover_link: str) -> None:
    """The finished video under its post: the cover in the doc, the link in the post's grey line and the card in Video
    scripts marked done."""
    import content_engine_visuals as vis
    posts = json.loads(hit[0].read_text())
    posts[hit[1] - 1].setdefault("visual_files", []).append({"file": f"post{hit[1]}_video_cover.png", "link": cover_link})
    posts[hit[1] - 1]["video_file"] = video_link
    hit[0].write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    spec = hit[0].with_name(hit[0].stem + "_visuals.json")
    if spec.exists():
        vis.into_doc(spec)
    ced.refresh_meta(hit[0], [hit[1]])
    try:
        mark_cards()
    except Exception as e:  # the video is finished either way
        print(f"(Video scripts not marked: {type(e).__name__})")


def _unlink_post(post: str, cover_id: str) -> None:
    """A video taken back off a post it never belonged to ("2026-11-02_Engine_Batch_4 #3"): its cover out of the doc and
    the batch file, the link out of the grey line, the card in Video scripts open again."""
    import content_engine_visuals as vis
    stem, n = post.rsplit(" #", 1)
    bf, n = C.BATCHES / f"{stem}.json", int(n)
    posts = json.loads(bf.read_text())
    p = posts[n - 1]
    p["visual_files"] = [v for v in p.get("visual_files", []) if cover_id not in v["link"]]
    if not p["visual_files"]:
        del p["visual_files"]
    p.pop("video_file", None)
    bf.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    vis.remove_from_doc(stem, [cover_id])
    ced.refresh_meta(bf, [n])
    try:
        mark_cards()
    except Exception as e:  # noqa: BLE001
        print(f"(Video scripts not marked: {type(e).__name__})")


def auto() -> None:
    if not (C.get("google", "video_inbox_id") and C.get("google", "finished_videos_id")):
        return  # video not set up: [google] video_inbox_id and finished_videos_id
    import content_engine_visuals as vis
    from googleapiclient.http import MediaFileUpload
    cfg = {"Video inbox": {"id": C.need("google", "video_inbox_id")}, "Finished videos": {"id": C.need("google", "finished_videos_id")}}
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    drive = vis._google("drive", "v3")
    lock = _lock()  # noqa: F841 (held until the run ends)
    if not lock:
        return
    q = f"'{cfg['Video inbox']['id']}' in parents and trashed = false and mimeType contains 'video/'"
    files = drive.files().list(q=q, fields="files(id, name, size, md5Checksum, createdTime)").execute().get("files", [])
    for f in sorted(files, key=lambda f: f["createdTime"]):
        if f["id"] in state and not (state[f["id"]].get("failed") and state[f["id"]].get("code") != CODE):
            continue
        twin = next((k for k, s in state.items() if f.get("md5Checksum") and s.get("md5") == f["md5Checksum"]
                     and not s.get("duplicate_of")), None)
        if FINISHED.match(f["name"]):  # one of our own finished videos, not a recording
            state[f["id"]] = {"name": f["name"], "skipped": "a finished video, not a recording", "md5": f.get("md5Checksum"),
                              "processed": dt.datetime.now().isoformat(timespec="seconds")}
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            print(f"{f['name']}: a finished video, not a recording, skipped")
            continue
        if twin:  # the same file uploaded twice: keep the first, never a second finished video
            state[f["id"]] = {"name": f["name"], "duplicate_of": twin, "md5": f["md5Checksum"],
                              "processed": dt.datetime.now().isoformat(timespec="seconds")}
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            print(f"{f['name']}: the same file as one already handled, skipped")
            continue
        raw = _download(drive, f["id"], f["name"])
        tag = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=comment", "-of", "csv=p=0", str(raw)],
                             capture_output=True, text=True).stdout
        if MARK in tag:  # renamed, but still one of ours
            state[f["id"]] = {"name": f["name"], "skipped": "a finished video (marker), not a recording", "md5": f.get("md5Checksum"),
                              "processed": dt.datetime.now().isoformat(timespec="seconds")}
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            print(f"{f['name']}: carries the finished-video marker, skipped")
            continue
        try:
            res = process(raw, name=re.sub(r"[^\w-]+", "_", pathlib.Path(f["name"]).stem)[:60])
        except BrokenVideo as e:  # on record, so it isn't made again every 10 minutes, and in the inbox, so it isn't silent
            state[f["id"]] = {"name": f["name"], "md5": f.get("md5Checksum"), "failed": str(e), "code": CODE,
                              "processed": dt.datetime.now().isoformat(timespec="seconds")}
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            import content_engine_feedback as fb
            fb.inbox(f"Video not finished: {f['name']}",
                     [f"Your video \"{f['name']}\" failed its own check before upload: {e}.",
                      "Nothing was uploaded or placed under a post. It is made again by itself once the video engine is fixed;",
                      "look at content_engine_video.py, the BrokenVideo check."])
            print(f"{f['name']}: not handed over ({e})")
            continue
        hit = res["post"]
        up = {}
        base = finished_name(hit, res["transcript"], dt.date.today())
        for key, mime in (("video", "video/mp4"), ("srt", "application/x-subrip"), ("cover", "image/png")):
            path = res[key]
            fname = base + {"video": ".mp4", "srt": " captions.srt", "cover": " cover.png"}[key]
            folder = cfg["Finished videos"]["id"]
            up[key] = drive.files().create(body={"name": fname, "parents": [folder]}, media_body=MediaFileUpload(str(path), mimetype=mime, resumable=True, chunksize=1024 * 1024),
                                           fields="id, webViewLink").execute(num_retries=6)  # slow Wi-Fi: small pieces, retried
        for style, path in list(res.get("videos", {}).items())[1:]:  # the test variant next to it (one word at a time)
            label = {"word": " (ét ord)", "word_bare": " (skygge)", "word_plain": " (uden kant)", "line": " (linje)"}.get(style, f" ({style})")
            up[style] = drive.files().create(body={"name": base + label + ".mp4", "parents": [cfg["Finished videos"]["id"]]},
                                             media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=1024 * 1024),
                                             fields="id, webViewLink").execute(num_retries=6)
        state[f["id"]] = {"name": f["name"], "md5": f.get("md5Checksum"), "processed": dt.datetime.now().isoformat(timespec="seconds"),
                          "post": f"{hit[0].stem} #{hit[1]}" if hit else None, "seconds": res["seconds"],
                          "video": up["video"]["webViewLink"], "cover": up["cover"]["webViewLink"],
                          "files": {k: up[k]["id"] for k in ("video", "srt", "cover")},
                          **{f"video_{k}": v["webViewLink"] for k, v in up.items() if k not in ("video", "srt", "cover")}}
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        note = [f"Your video \"{f['name']}\" is ready: {res['seconds']} s, {res['captions']} Danish captions burned in, 4:5.",
                up["video"]["webViewLink"]] + [f"To compare ({k.replace('_', ' ')}): {v['webViewLink']}" for k, v in up.items()
                                               if k not in ("video", "srt", "cover")] + _note_lines(res)
        if hit:
            _link_post(hit, up["video"]["webViewLink"], up["cover"]["webViewLink"])
            note.append(f"It belongs to post {hit[1]} in {hit[0].stem.split('_', 1)[1].replace('_', ' ')}; the cover is under the post in the doc.")
        else:
            note.append("I couldn't tell which post it belongs to: reply with the post number.")
        note.append("Check the captions once; reply with any word I got wrong.")
        import content_engine_feedback as fb
        fb.inbox(f"Video ready: {f['name']}", note)
        print("\n".join(note))


def redo(key: str, said=()) -> None:
    """A recording already handled, made again with the pipeline as it is now (a word the author says is wrong, or a
    new standard); its finished video, captions and cover in Drive get the new content under the same file, so the
    links in the batch, the doc and the plan keep working. said: new corrections, (what the captions said, what was
    said), kept in the state so every later redo keeps them. When the post it belongs to changed, it moves: off the old
    post, onto the new one, the files renamed."""
    import content_engine_visuals as vis
    from googleapiclient.http import MediaFileUpload
    state = json.loads(STATE.read_text())
    fid = next((k for k, s in state.items() if key in (k, s.get("name"), pathlib.Path(s.get("name", "")).stem)
                and s.get("video") and not ({"duplicate_of", "skipped", "mistake"} & s.keys())), None)
    if not fid:
        raise SystemExit(f"no handled recording called {key}")
    s, drive = state[fid], vis._google("drive", "v3")
    lock = _lock()  # noqa: F841
    if not lock:
        return
    s["said"] = [list(p) for p in s.get("said", []) if p[0] not in {h for h, _ in said}] + [list(p) for p in said]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))  # kept even when the upload fails
    raw =next((OUT / fid).glob("raw.*"), None) or _download(drive, fid, s["name"])
    res = process(raw, name=re.sub(r"[^\w-]+", "_", pathlib.Path(s["name"]).stem)[:60], said=s["said"])
    ids = dict(s.get("files") or {})
    for kind in ("video", "cover"):
        ids.setdefault(kind, re.search(r"/d/([\w-]+)", s[kind]).group(1))
    if "srt" not in ids:  # older entries: the captions file sits next to the video under the same name
        base = re.sub(r"\.mp4$", "", drive.files().get(fileId=ids["video"], fields="name").execute()["name"])
        q = "name = '{}' and trashed = false".format((base + " captions.srt").replace("\\", "\\\\").replace("'", "\\'"))
        hits = drive.files().list(q=q, fields="files(id)").execute().get("files", [])
        if hits:
            ids["srt"] = hits[0]["id"]
    for kind, mime in (("video", "video/mp4"), ("srt", "application/x-subrip"), ("cover", "image/png")):
        if kind in ids:
            drive.files().update(fileId=ids[kind], media_body=MediaFileUpload(str(res[kind]), mimetype=mime, resumable=True, chunksize=1024 * 1024)
                                 ).execute(num_retries=6)
    hit = res["post"]
    post = f"{hit[0].stem} #{hit[1]}" if hit else None
    moved = post != s.get("post")
    if moved:
        if s.get("post"):
            _unlink_post(s["post"], ids["cover"])
        if hit:
            _link_post(hit, s["video"], s["cover"])
        base = finished_name(hit, res["transcript"], dt.date.fromisoformat(s["processed"][:10]))
        for kind, end in (("video", ".mp4"), ("srt", " captions.srt"), ("cover", " cover.png")):
            if kind in ids:
                drive.files().update(fileId=ids[kind], body={"name": base + end}).execute(num_retries=6)
    s.update({"files": ids, "seconds": res["seconds"], "post": post, "redone": dt.datetime.now().isoformat(timespec="seconds")})
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print("\n".join([f"{s['name']} made again and replaced in Drive: {res['seconds']} s, {res['captions']} captions.", s["video"]]
                    + ([f"Now under: {post or 'no post (none tells this story)'}; files renamed."] if moved else [])
                    + _note_lines(res)))


def selftest() -> None:
    w = lambda *ts: [{"w": t, "s": float(i), "e": i + 0.8} for i, t in enumerate(ts)]
    words = w("Et", "af", "mine", "yndlings", "eksempler", "og", "en", "god", "måde", "og", "starte,", "det", "er", "bagerbåden.")
    fixed, changes = _apply_fixes(words, [{"first": 13, "last": 13, "options": ["bagerboden."]},
                                          {"first": 9, "last": 10, "options": ["at starte,"]},
                                          {"first": 3, "last": 4, "options": ["yndlingseksempler"]}])
    said = " ".join(x["w"] for x in fixed)
    m = lambda kind, t0, t1, start=0, pop="": {"kind": kind, "t0": t0, "t1": t1, "start": start, "end": start, "pop": pop}
    talk = w(*"Det er sjovt hold nu op og så videre og så videre og så ingen nye kunder her".split())
    s = lambda kind, start, pop, score: {**m(kind, start, start, start, pop), "score": score}
    long_talk = w(*(["ord"] * 20 + "første vigtige pointe".split() + ["ord"] * 20 + "anden vigtige pointe".split()
                    + ["ord"] * 2 + "tæt på den".split() + ["ord"] * 40))
    cases = [
        (said == "Et af mine yndlingseksempler og en god måde at starte, det er bagerboden.", "the three fixes go in"),
        (fixed[12]["s"] == 13.0 and fixed[12]["w"] == "bagerboden." and abs(fixed[3]["e"] - 4.8) < 1e-9, "a fix keeps the time of what it replaces"),
        (len(changes) == 3, "every fix is reported"),
        (_apply_fixes(words, [{"first": 0, "last": 1, "options": ["Et af"]}])[1] == [], "no change, no fix"),
        ((lambda r: " ".join(x["w"] for x in r[0]) == "Et af to tilbud i dag." and r[2] == {2} and r[3] == ["ikke her"])(
            _said(w("Et", "af", "4-2", "tilbud", "i", "dag."), [("4-2", "to"), ("ikke her", "x")])),
         "the author's own correction goes in, even where it sounds nothing alike, and one that matches nothing is reported"),
        ((lambda r: [x["w"] for x in r[0]] == ["faktisk", "to", "i", "dag,", "jeg", "har", "valgt", "to"] and r[2] == {0, 1, 7})(
            _said(w("praksis", "2", "i", "dag,", "jeg", "har", "valgt", "4-2"), [("4-2", "to"), ("praksis 2", "faktisk to")])),
         "several corrections keep each other's words protected"),
        ((lambda r: [x["w"] for x in r[0]] == ["med", "AI", "nu.", "ok"] and abs(r[0][2]["e"] - 1.8) < 1e-9 and r[0][3]["s"] == 3.0
          and r[2] == {0, 1, 2})(_said(w("mig", "af.") + [{"w": "ok", "s": 3.0, "e": 3.8}], [("mig af.", "med AI nu.")])),
         "a correction with more words spreads over the time of what it replaces"),
        (_apply_fixes(w("faktisk", "to", "i", "dag"), [{"first": 0, "last": 1, "options": ["praktisk to"]}], keep={0, 1})[1] == [],
         "the proofreader never touches the author's own corrections"),
        (_apply_fixes(words, [{"first": 2, "last": 2, "options": ["Google Ads kampagner"]}])[1] == [], "a fix that looks nothing like it is dropped"),
        (_apply_fixes(words, [{"first": 0, "last": 6, "options": ["Et af mine"]}])[1] == [], "a fix over more than 5 words is dropped"),
        (len(_apply_fixes(words, [{"first": 3, "last": 4, "options": ["yndlingseksempler"]}, {"first": 4, "last": 5, "options": ["eksempler at"]}])[1]) == 1,
         "of two overlapping fixes only one goes in"),
        (_apply_fixes(words, [{"first": 10, "last": 10, "options": ["starten,"]}, {"first": 9, "last": 10, "options": ["at starte,"]}])[1] == [("og starte,", "at starte,")],
         "the fix covering more words wins"),
        (_apply_fixes(words, [{"first": 5, "last": 5, "options": ["at"]}])[1] == [("og", "at")], "og and at sound the same, so a swap is allowed"),
        (_apply_fixes(words, [{"first": 9, "last": 10, "options": ["at starte"]}])[1] == [("og starte,", "at starte,")], "the punctuation stays"),
        (_apply_fixes(words, [{"first": 13, "last": 13, "options": ["bagerboden."]}], lambda a, b, o: None)[1] == [],
         "a fix the sound doesn't support is left out"),
        (_apply_fixes(words, [{"first": 9, "last": 10, "options": ["og startet,", "at starte,"]}], lambda a, b, o: o[-1])[1] == [("og starte,", "at starte,")],
         "the sound picks among the options"),
        ((lambda seen: (_apply_fixes(words, [{"first": 9, "last": 10, "options": ["at starte,"]}, {"first": 9, "last": 10, "options": ["og startet,"]}],
                                     lambda a, b, o: seen.append(o) or o[0]), seen)[1])([]) == [["at starte,", "og startet,"]],
         "both reads' options for the same words go to the sound check together"),
        (len(_apply_fixes(words, [{"first": 13, "last": 13, "options": ["bagerboden."]}] * 2)[1]) == 1, "the same fix from both reads goes in once"),
        (_apply_fixes(words, [{"first": 13, "last": 13, "options": ["bager" + ced.DASHES[0] + "boden."]}])[0][13]["w"] == "bager-boden.",
         "no long dash gets in"),
        (_zoom_pieces([m("hook", 0.1, 8.6), m("punch", 2.0, 5.0)], 90, 8.6) == [], "no punch-in in the opening sentence"),
        (_zoom_pieces([m("punch", 38.9, 40.1), m("punch", 41.1, 53.8)], 90, 8.6) == [(38.9, 53.8, ZOOM)], "two close together become one"),
        (_zoom_pieces([m("punch", 20.0, 21.0)], 90, 8.6) == [], "too short to be worth it"),
        ((49.1, 53.2, ZOOM_FUNNY) in _zoom_pieces([m("punch", 41.1, 53.8), m("funny", 49.1, 53.2)], 90, 8.6), "the joke goes closer"),
        (_zoom_pieces([m("ask", 83.0, 95.0)], 91.2, 8.6) == [(83.0, 91.2, ZOOM)], "never past the end"),
        ([p["text"] for p in _pops([m("funny", 3, 5, 3, "hold nu op"), m("key", 10, 12, 10, "ingen nye kunder")], talk, 2.0)]
         == ["Hold nu op", "Ingen nye kunder"], "the speaker's own phrases pop up"),
        (_pops([m("key", 10, 12, 10, "ingen kunder")], talk, 2.0) == [], "a phrase that wasn't said is left out"),
        ([p["text"] for p in _pops([m("key", 10, 12, 10, "ingen kunder"), m("key", 10, 12, 10, "nye kunder")], talk, 2.0)]
         == ["Nye kunder"], "the next key phrase takes over"),
        (len(_pops([m("funny", 3, 5, 3, "hold nu op"), m("funny", 7, 9, 7, "og så videre"),
                    m("key", 10, 12, 10, "ingen nye kunder")], talk, 2.0)) == 2, "one joke, at most two in all"),
        (_pops([m("funny", 3, 5, 3, "hold nu op")], talk, 4.0) == [], "nothing pops up in the opening sentence"),
        ([p["text"] for p in _pops([s("key", 20, "første vigtige pointe", 9), s("key", 43, "anden vigtige pointe", 6)], long_talk, 2.0)]
         == ["Første vigtige pointe"], "a phrase scored under POP_SCORE stays off"),
        ([p["text"] for p in _pops([s("key", 43, "anden vigtige pointe", 7), s("key", 48, "tæt på den", 9)], long_talk, 2.0)]
         == ["Tæt på den"], "of two too close together, the higher score wins"),
        (len(_pops([s("key", 20, "første vigtige pointe", 8), s("key", 43, "anden vigtige pointe", 8), s("key", 48, "tæt på den", 8)],
                   long_talk, 2.0)) == 2, "two pop-ups with clear screen between them both stay"),
        ((lambda plan: len(set(plan)) == 6 and all(a != b for a, b in zip(plan, plan[1:])) and {k for k, _ in plan} == {"bottle", "click"})(
            _sfx_plan(6)), "the pop-up sounds vary: never the same twice in a row, both kinds"),
        ((lambda xs: all(abs(abs(x).max() - 10 ** (SFX_DB / 20)) < 1e-6 for x in xs) and len({x.tobytes() for x in xs}) == 6)(
            [_sfx(k, v) for k in ("bottle", "click") for v in range(3)]), "six different sounds, all at the same level"),
        (_calm_until(w("Hej.", "med", "dig")) == 3.0 and _calm_until(w(*["ord"] * 15, "slut.")) == 12.0 and _calm_until(w("ord", "ord")) == 3.0,
         "the calm opening is the first sentence, 3 to 12 s"),
    ]
    import tempfile
    import wave
    import numpy as np
    rng, sr = np.random.default_rng(1), 16000
    sound = 0.002 * rng.standard_normal(4 * sr)  # the room
    for a, b in ((0.2, 0.32), (1.0, 2.0), (2.1, 2.3), (2.8, 3.5)):  # a click, then speech with a short gap, then more
        sound[int(a * sr):int(b * sr)] = 0.2 * rng.standard_normal(int(b * sr) - int(a * sr))
    sound[int(2.5 * sr):int(2.62 * sr)] = 0.01 * rng.standard_normal(int(2.62 * sr) - int(2.5 * sr))  # a breath in the pause
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "t.wav"
        with wave.open(str(f), "wb") as wv:
            wv.setnchannels(1), wv.setsampwidth(2), wv.setframerate(sr), wv.writeframes((sound * 32767).astype(np.int16).tobytes())
        heard = _speech(*_loudness(f))
        heard_um = _speech(*_loudness(f), [(2.8, 3.5)])
    near = lambda got, want: len(got) == len(want) and all(abs(x - y) <= 0.04 for g, h in zip(got, want) for x, y in zip(g, h))
    cut_words, cut_len = _remap([{"w": "Hej", "s": 1.2, "e": 1.5}, {"w": "øh", "s": 2.2, "e": 2.5}, {"w": "ja", "s": 2.6, "e": 2.7},
                                 {"w": "så", "s": 3.1, "e": 3.3}], [(1.0, 2.0), (3.0, 4.0)])
    cases += [
        (near(heard, [(1.0, 2.3), (2.8, 3.5)]), "speech from the sound: the click at the start goes, a short gap stays, a breath is quiet"),
        (near(heard_um, [(1.0, 2.3)]), "an øh counts as quiet"),
        ([w["w"] for w in cut_words] == ["Hej", "ja", "så"] and abs(cut_words[0]["s"] - 0.2) < 1e-9 and abs(cut_words[2]["s"] - 1.1) < 1e-9
         and abs(cut_len - 2.0) < 1e-9, "words follow the cut, an øh leaves the captions"),
        (abs(cut_words[1]["s"] - 1.0) < 1e-9, "a word in a removed piece lands on the cut"),
        (abs(_remap([{"w": "så", "s": 3.1, "e": 3.3}], [(1.0, 2.0), (3.0, 4.0)], [XF])[1] - (2.0 - XF)) < 1e-9, "a cut's cross-fade overlaps the parts"),
        (len(_zoom_pieces([m("punch", 10, 14), m("punch", 20, 24), m("funny", 30, 33), m("punch", 40, 46), m("ask", 60, 66)], 70, 5))
         == ZOOM_COUNT + 1, "only a few punch-ins (plus the joke's closer step)"),
        ((lambda ps: any(a == 30 for a, _, _ in ps) and any(a == 60 for a, _, _ in ps))(
            _zoom_pieces([m("punch", 10, 14), m("punch", 20, 24), m("funny", 30, 33), m("punch", 40, 46), m("ask", 60, 66)], 70, 5)),
         "the joke and the close get them"),
        (all(z == ZOOM_STEP for _, _, z in _max_pieces([], w("Hej.", "a,", "b", "c.", "d", "e."), [1.4], 6.0)[0])
         and len(_max_pieces([], w("Hej.", "a,", "b", "c.", "d", "e."), [1.4], 6.0)[0]) >= 1, "max: switches at breaks"),
        (_max_pieces([m("punch", 2.0, 4.0)], w("Hej.", "a"), [], 6.0)[0][-1][2] == ZOOM_MAX, "max: the strong lines closest"),
        ((lambda ps: len({sp[2:] for sp in ps[1]}) > 1 and ps[1][0][:2] == (2.0, 4.0))(
            _max_pieces([m("punch", 2.0, 4.0)], w("Hej.", "a,", "b", "c.", "d", "e.", "f,", "g"), [1.4], 9.0)),
         "max: zooms land in different spots, the strong line's spot wins"),
        ("if(between(t,1.00,2.00),1080*max(0,0.3*between(t,1.00,2.00))*0.000" in _zoom_filter([(1.0, 2.0, ZOOM_MAX)], 0.5, 0.5, [(1.0, 2.0, 0.0, 1.0)]),
         "a spot moves the crop"),
        ("clip(if(between(t,1.00,2.00),0.540,0.474)*(1080+1080*max(0,0.12*between(t,1.00,2.00)))-540,0,1080*max(0,0.12*between(t,1.00,2.00)))"
         in _zoom_filter([(1.0, 2.0, ZOOM)], 0.474, 0.55, (), [(1.0, 2.0, 0.54, 0.5)]), "a punch-in centres the face as it is in that moment"),
        ("iw" not in _zoom_filter([(1.0, 2.0, ZOOM)], 0.5, 0.5, [(1.0, 2.0, 0.0, 1.0)]).split("crop=")[1], "the crop never reads iw/ih (fixed at the first frame)"),
        (abs(_source_time(1.1, [(1.0, 2.0), (3.0, 4.0)]) - 3.1) < 1e-9 and abs(_source_time(0.2, [(1.0, 2.0), (3.0, 4.0)]) - 1.2) < 1e-9,
         "a moment on the cut finds its place on the recording"),
    ]
    raw_parts, soft_at = [(0.013, 1.71), (1.954, 3.333), (3.51, 6.02), (6.4, 9.877)], {1}
    snapped = _snap(raw_parts, soft_at)
    pic = [(round((b - (XF if i < 3 and i not in soft_at else 0)) * FPS) - round(a * FPS)) / FPS for i, (a, b) in enumerate(snapped)]
    sound_len = sum(b - a for a, b in snapped) - sum(SOFT if k in soft_at else XF for k in range(3))
    cases += [(all(abs(a * FPS - round(a * FPS)) < 1e-9 for a, _ in snapped), "every part starts on a whole frame"),
              (abs(sound_len - (sum(pic) - SOFT * len(soft_at))) < 1e-6,
               "picture and sound come out exactly as long (a soft cut can't run past the picture)")]
    fails = [why for ok, why in cases if not ok]
    print("\n".join(fails) or f"selftest ok: {len(cases)} cases")
    if fails:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("script")
    s.add_argument("--batch", required=True)
    s.add_argument("--post", type=int, required=True)
    s.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("process")
    p.add_argument("file")
    p.add_argument("--name")
    r = sub.add_parser("redo")
    r.add_argument("key", help="the inbox file id or the recording's name, e.g. IMG_0001")
    r.add_argument("--said", nargs=2, action="append", default=[], metavar=("CAPTIONS", "SAID"),
                   help='a correction, kept for every later redo: --said "4-2" "to" (words as Whisper heard them)')
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.auto:
        return auto()
    if a.cmd == "redo":
        return redo(a.key, [tuple(p) for p in a.said])
    if a.cmd == "script":
        bf = pathlib.Path(a.batch)
        post = json.loads(bf.read_text())[a.post - 1]
        card = shoot_card(post, a.post)
        batch = bf.stem.split("_", 1)[1].replace("_", " ")
        for kind, line in card_text(card, batch):
            print(f"[{kind}] {line}")
        if not a.dry_run:
            print(write_card(card, batch))
    elif a.cmd == "process":
        res = process(pathlib.Path(a.file), a.name)
        print(json.dumps({k: str(v) for k, v in res.items() if k != "transcript"}, indent=1))
        print("transcript:", res["transcript"][:400])
    else:
        ap.print_help()


from content_engine_lock import locked as _locked_doc  # noqa: E402
mark_cards = _locked_doc(mark_cards)


if __name__ == "__main__":
    main()
