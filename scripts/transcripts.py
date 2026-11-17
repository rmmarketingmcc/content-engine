#!/usr/bin/env python3
"""Call transcripts from any note taker, for the drafter and the voice bank.

Put transcripts in one folder ([sources] transcripts_dir) and the engine reads them. Any note taker works (MeetGeek,
Fireflies, Otter, Fathom, tl;dv, Zoom, Teams, Google Meet, a phone's recorder app): export the transcript to the
folder, let the note taker or a Zapier/Make step save it there, or label the mails it sends you and run `sync-mail`.

Formats:
  .txt .md   "Name: text" lines (a "[00:01:02]" or "00:01:02" time in front is fine), or a "Name  0:03" line with
             the text under it (Otter, Teams, Fireflies exports)
  .vtt .srt  subtitles: WebVTT voice tags (<v Name>text) or "Name: text" in the cue (Zoom, Teams, Google Meet)
  .docx      Teams and Google Meet transcript documents
  .json      MeetGeek (meta + sentences), or a list of {speaker, text}
The date comes from the file name ("2026-10-26 Content call.txt") or else the file's own time; the title is the file
name without the date. [sources] meetgeek_dir is read the same way, for older setups.

A mail saved by sync-mail becomes "Name: text" lines: your own mails under your first name, so the voice bank learns
from them too, the rest under the sender's name. Quoted history in replies is cut.

    python3 scripts/transcripts.py list                      # per file: date, title, speakers, lines, your lines
    python3 scripts/transcripts.py show 2026-10-26:"Content call"
    python3 scripts/transcripts.py sync-mail [--label "Content engine"] [--days 30] [--dry-run]
    python3 scripts/transcripts.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import glob
import html
import json
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

EXTS = (".txt", ".md", ".vtt", ".srt", ".docx", ".json")
TIME = r"\(?\[?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]?\)?"
LINE_RE = re.compile(rf"^\s*(?:{TIME}\s*[-\u2013]?\s*)?(?P<who>[^\s:\[\]()<>][^:\[\]()<>]{{0,48}}?)\s*(?:{TIME})?\s*:\s+(?P<text>\S.*)$")
HEAD_RE = re.compile(rf"^\s*(?:(?P<t1>{TIME})\s*[-\u2013|]?\s*(?P<a>[^\d:\s][^:]{{0,48}}?)|(?P<b>[^\d:\s][^:]{{0,48}}?)\s*[-\u2013|,]?\s*(?P<t2>{TIME}))\s*$")
DATE_RE = re.compile(r"(20\d{2})[-_.](\d{2})[-_.](\d{2})")
DA = {"og", "det", "jeg", "ikke", "på", "er", "at", "har", "til", "med", "så", "vi", "kan", "skal", "hvad", "også"}
EN = {"the", "and", "is", "you", "that", "it", "to", "of", "we", "have", "what", "this", "with", "but", "so"}
LABEL = "Content engine"
SEPS = " -\u2013|,"  # what sits between a name and a time
NOT_NAMES = {"så", "sådan", "det", "nej", "ja", "men", "og", "altså", "okay", "ok", "fx", "husk", "bemærk", "note", "ps",
             "so", "yes", "no", "but", "and", "note", "example", "eksempel"}


def folders() -> list[pathlib.Path]:
    out = []
    for key in ("transcripts_dir", "meetgeek_dir"):
        v = C.get("sources", key)
        if v and C.path(v).is_dir() and C.path(v) not in out:
            out.append(C.path(v))
    return out


def speaker_name() -> str:
    return (C.get("sources", "speaker") or C.get("sources", "meetgeek_speaker") or C.author()).lower()


def language(text: str) -> str:
    """'da', 'en' or '' from the share of common words: enough to keep an English call out of a Danish voice bank."""
    words = re.findall(r"[a-zæøå]+", text.lower())[:3000]
    da, en = sum(w in DA for w in words), sum(w in EN for w in words)
    return "" if da + en < 20 else ("da" if da > en else "en")


def _lines_from_text(text: str) -> list[tuple[str, str, str]]:
    """[(time, speaker, text)] from plain text in either layout; a line without a speaker belongs to the last one."""
    lines = [l.strip() for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in lines if l and "-->" not in l and not re.fullmatch(r"\d+", l) and not re.fullmatch(TIME, l)]
    heads = {(h.group("a") or h.group("b") or "").strip(SEPS) for l in lines
             if (h := HEAD_RE.match(l)) and not LINE_RE.match(l)}
    named = collections.Counter(m.group("who").strip() for l in lines
                                if (m := LINE_RE.match(l)) and len(m.group("who").split()) <= 5)

    def is_speaker(name: str) -> bool:  # "Det er simpelt: vi ringer" is a sentence, not a speaker
        if len(heads) >= 2 or (heads and len(lines) > 3):
            return name in heads
        words = name.split()
        looks_like_a_name = len(words) <= 3 and all(w[:1].isupper() or w.isdigit() for w in words) and \
            not (len(words) == 1 and name.lower() in NOT_NAMES)
        return named[name] >= 2 or sum(named.values()) <= 2 or looks_like_a_name

    out: list[list[str]] = []
    who = ""
    for line in lines:
        h = HEAD_RE.match(line)
        if h and not LINE_RE.match(line):
            who = (h.group("a") or h.group("b") or "").strip(SEPS)
            continue
        m = LINE_RE.match(line)
        if m and len(m.group("who").split()) <= 5 and is_speaker(m.group("who").strip()):
            who = m.group("who").strip()
            t = re.search(TIME, line[:m.start("who") + 1] or "")
            out.append([t.group(0).strip("[]()") if t else "", who, m.group("text").strip()])
        elif out and out[-1][1] == who:
            out[-1][2] += " " + line
        else:
            out.append(["", who, line])
    return [tuple(x) for x in out]


def _vtt(text: str) -> list[tuple[str, str, str]]:
    out = []
    when = ""
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [l.strip() for l in block.split("\n") if l.strip() and l.strip() != "WEBVTT"]
        cue = [l for l in lines if "-->" in l]
        if cue:
            when = cue[0].split("-->")[0].strip()
        body = " ".join(l for l in lines if "-->" not in l and not re.fullmatch(r"\d+", l) and not l.startswith("NOTE"))
        if not body:
            continue
        v = re.match(r"<v\s+([^>]+)>(.*)", body)
        if v:
            who, said = v.group(1).strip(), re.sub(r"</?v[^>]*>", "", v.group(2)).strip()
        else:
            m = LINE_RE.match(body)
            who, said = (m.group("who").strip(), m.group("text").strip()) if m else ("", body)
        said = re.sub(r"<[^>]+>", "", said).strip()
        if out and out[-1][1] == who and who:  # one turn split over several cues
            out[-1] = (out[-1][0], who, out[-1][2] + " " + said)
        elif said:
            out.append((when, who, said))
    return out


def _docx(path: pathlib.Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        p = re.sub(r"<w:(tab|br)/>", " ", p)
        paras.append(html.unescape("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S))))
    return "\n".join(paras)


def _json(d) -> tuple[list[tuple[str, str, str]], dict]:
    if isinstance(d, dict) and ("sentences" in d or "meta" in d):  # MeetGeek
        sents = d.get("sentences") or []
        if isinstance(sents, dict):
            sents = sents.get("sentences", [])
        rows = [(str(s.get("timestamp", ""))[11:19], str(s.get("speaker", "")), str(s.get("transcript", ""))) for s in sents]
        m = d.get("meta") or {}
        return rows, {"title": m.get("title"), "date": str(m.get("timestamp_start_utc", ""))[:10],
                      "language": str(m.get("language") or "")[:2]}
    items = d if isinstance(d, list) else (d.get("transcript") or d.get("segments") or d.get("utterances") or []) \
        if isinstance(d, dict) else []
    rows = []
    for s in items:
        if isinstance(s, dict):
            who = s.get("speaker") or s.get("speaker_name") or s.get("name") or s.get("role") or ""
            said = s.get("text") or s.get("transcript") or s.get("message") or s.get("sentence") or ""
            rows.append((str(s.get("start") or s.get("timestamp") or ""), str(who), str(said)))
    return rows, {}


def parse(path: pathlib.Path) -> dict | None:
    """One transcript as {file, date, title, language, lines: [(time, speaker, text)]}, or None for an unreadable file."""
    try:
        ext = path.suffix.lower()
        meta: dict = {}
        if ext == ".json":
            rows, meta = _json(json.loads(path.read_text(encoding="utf-8")))
        elif ext == ".docx":
            rows = _lines_from_text(_docx(path))
        elif ext in (".vtt", ".srt"):
            rows = _vtt(path.read_text(encoding="utf-8", errors="replace"))
        else:
            rows = _lines_from_text(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    rows = [r for r in rows if r[2].strip()]
    if not rows:
        return None
    d = DATE_RE.search(path.stem)
    date = meta.get("date") or (f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else
                                dt.date.fromtimestamp(path.stat().st_mtime).isoformat())
    title = meta.get("title") or DATE_RE.sub("", path.stem).strip(" -_.") or path.stem
    return {"file": str(path), "date": date, "title": title, "lines": rows,
            "language": meta.get("language") or language(" ".join(r[2] for r in rows))}


def load_all() -> list[dict]:
    out = []
    for folder in folders():
        for f in sorted(glob.glob(str(folder / "**" / "*"), recursive=True)):
            p = pathlib.Path(f)
            if p.suffix.lower() in EXTS and not p.name.startswith("."):
                t = parse(p)
                if t:
                    out.append(t)
    return out


def find(spec: str) -> list[dict]:
    """spec = 'YYYY-MM-DD:title words'. The title part is required, so a day's other calls never become a source by
    accident."""
    date, _, title = spec.partition(":")
    if not title.strip():
        sys.exit(f"needs DATE:TITLE, e.g. 2026-10-26:Content call (got {spec!r})")
    return [t for t in load_all() if t["date"].startswith(date.strip()) and title.strip().lower() in t["title"].lower()]


def as_text(t: dict) -> str:
    return f"### Call {t['date']}: {t['title']}\n" + "\n".join(
        f"[{w}] {who}: {said}" if w else f"{who}: {said}" for w, who, said in t["lines"])


def author_lines(t: dict) -> list[str]:
    me = speaker_name()
    return [said for _, who, said in t["lines"] if me and me in who.lower()]


def _parts(payload: dict):
    if payload.get("parts"):
        for p in payload["parts"]:
            yield from _parts(p)
    else:
        yield payload


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sync_mail(label: str, days: int, dry: bool) -> list[str]:
    """Every mail with the label: its transcript attachments saved as they are, else its body as "Name: text" lines."""
    import gmail_util as G
    folder = C.path(C.need("sources", "transcripts_dir"))
    folder.mkdir(parents=True, exist_ok=True)
    seen_f = C.STATE / "transcripts_mail_seen.json"
    seen = set(json.loads(seen_f.read_text())) if seen_f.exists() else set()
    ids, token = [], None
    while True:
        r = G.gmail_get("users/me/messages", {"q": f'label:"{label}" newer_than:{days}d', "maxResults": 100,
                                               **({"pageToken": token} if token else {})})
        ids += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    saved = []
    for mid in [i for i in ids if i not in seen]:
        msg = G.gmail_get(f"users/me/messages/{mid}", {"format": "full"})
        h = {x["name"]: x["value"] for x in msg.get("payload", {}).get("headers", [])}
        try:
            day = G.parsedate_to_datetime(h.get("Date")).date().isoformat()
        except (TypeError, ValueError):
            day = dt.date.today().isoformat()
        subject = re.sub(r"[^\w æøåÆØÅ.-]+", " ", h.get("Subject") or "mail").strip()[:60] or "mail"
        files = []
        for part in _parts(msg.get("payload", {})):
            name = part.get("filename") or ""
            if name and pathlib.Path(name).suffix.lower() in EXTS and (part.get("body") or {}).get("attachmentId"):
                att = G.gmail_get(f"users/me/messages/{mid}/attachments/{part['body']['attachmentId']}")
                files.append((f"{day} {subject} {name}", _b64(att["data"])))
        if not files:
            text = ""
            for part in _parts(msg.get("payload", {})):
                data = (part.get("body") or {}).get("data")
                if data and part.get("mimeType") == "text/plain":
                    text += _b64(data).decode("utf-8", errors="replace")
            if not text:
                for part in _parts(msg.get("payload", {})):
                    data = (part.get("body") or {}).get("data")
                    if data and part.get("mimeType") == "text/html":
                        text += html.unescape(re.sub(r"<[^>]+>", "\n", _b64(data).decode("utf-8", errors="replace")))
            sender = G.addresses(h.get("From", ""))
            mine = bool(sender) and G._own(sender[0])
            who = C.author() if mine else (re.sub(r"\s*<.*", "", h.get("From", "")).strip('" ') or "Mail")
            paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", G.own_words(text))]
            body = "\n".join(f"{who}: {p}" for p in paras if p)
            if body:
                files.append((f"{day} {subject}.txt", body.encode("utf-8")))
        for name, data in files:
            target = folder / re.sub(r"[/\\]", " ", name)
            saved.append(str(target))
            if not dry:
                target.write_bytes(data)
        seen.add(mid)
    if not dry:
        C.STATE.mkdir(parents=True, exist_ok=True)
        seen_f.write_text(json.dumps(sorted(seen)))
    return saved


def selftest() -> None:
    cases = {
        "plain": ("Freja: Vi fik den første kunde i går.\nKunde: Hvad kostede det?\nFreja: 4.000 kr. og en kop kaffe.",
                  [("", "Freja", "Vi fik den første kunde i går."), ("", "Kunde", "Hvad kostede det?"),
                   ("", "Freja", "4.000 kr. og en kop kaffe.")]),
        "timed": ("[00:01:02] Freja: Det tog to uger.\n00:01:30 Jonas: Og så?",
                  [("00:01:02", "Freja", "Det tog to uger."), ("00:01:30", "Jonas", "Og så?")]),
        "header": ("Freja Eksempel  0:03\nVi starter med det svære.\nDet var en lang uge.\n\nJonas  0:41\nFortæl.",
                   [("", "Freja Eksempel", "Vi starter med det svære. Det var en lang uge."), ("", "Jonas", "Fortæl.")]),
        "dash header": ("Freja - 00:00\nHej med dig.\n00:12 - Jonas\nHej.",
                        [("", "Freja", "Hej med dig."), ("", "Jonas", "Hej.")]),
        "colon in a sentence": ("Freja: Det er simpelt.\nJonas: Hvordan?\nFreja: Sådan her: vi ringer.\nDet er nemt: ring op.\n"
                                "Jonas: Okay.",
                                [("", "Freja", "Det er simpelt."), ("", "Jonas", "Hvordan?"),
                                 ("", "Freja", "Sådan her: vi ringer. Det er nemt: ring op."), ("", "Jonas", "Okay.")]),
        "colon under a header": ("Freja  0:03\nDet er simpelt: vi ringer.\n\nJonas  0:41\nOkay.",
                                 [("", "Freja", "Det er simpelt: vi ringer."), ("", "Jonas", "Okay.")]),
    }
    bad = 0
    for name, (text, want) in cases.items():
        got = _lines_from_text(text)
        if got != want:
            bad += 1
            print(f"FAIL {name}: {got}")
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\n<v Freja>Vi ventede ti dage.</v>\n\n" \
          "2\n00:00:03.000 --> 00:00:05.000\n<v Freja>Så kom svaret.</v>\n\n3\n00:00:05.000 --> 00:00:06.000\nJonas: Hvorfor?"
    if _vtt(vtt) != [("00:00:01.000", "Freja", "Vi ventede ti dage. Så kom svaret."), ("00:00:05.000", "Jonas", "Hvorfor?")]:
        bad += 1
        print("FAIL vtt:", _vtt(vtt))
    srt = "1\n00:00:01,000 --> 00:00:02,000\nFreja: Det her er en test.\n\n2\n00:00:02,000 --> 00:00:03,000\nog den fortsætter"
    if _vtt(srt) != [("00:00:01,000", "Freja", "Det her er en test."), ("00:00:02,000", "", "og den fortsætter")]:
        bad += 1
        print("FAIL srt:", _vtt(srt))
    mg = {"meta": {"title": "Content call", "timestamp_start_utc": "2026-10-26T09:00:00Z", "language": "da-DK"},
          "sentences": [{"timestamp": "2026-10-26T09:00:05Z", "speaker": "Freja", "transcript": "Godmorgen."}]}
    rows, meta = _json(mg)
    if rows != [("09:00:05", "Freja", "Godmorgen.")] or meta["date"] != "2026-10-26" or meta["language"] != "da":
        bad += 1
        print("FAIL meetgeek json:", rows, meta)
    if _json([{"speaker": "Freja", "text": "Hej."}])[0] != [("", "Freja", "Hej.")]:
        bad += 1
        print("FAIL list json")
    if language("og det er jeg ikke så sikker på at vi kan " * 3) != "da" or language("the thing is that we have it " * 4) != "en":
        bad += 1
        print("FAIL language")
    if bad:
        sys.exit(f"selftest: {bad} failed")
    print("selftest ok: 12 cases")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", choices=["list", "show", "sync-mail"])
    ap.add_argument("spec", nargs="?", help="for show: DATE:TITLE")
    ap.add_argument("--label", default=C.get("sources", "mail_label") or LABEL)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    if a.cmd == "list":
        ts = load_all()
        if not ts:
            print("No transcripts: set [sources] transcripts_dir and put files there.")
        for t in ts:
            speakers = sorted({w for _, w, _ in t["lines"] if w})
            print(f"{t['date']}  {t['title'][:40]:40}  {t['language'] or '?':2}  {len(t['lines']):4} lines  "
                  f"{len(author_lines(t)):4} yours  {', '.join(speakers)[:60]}")
    elif a.cmd == "show":
        for t in find(a.spec or ""):
            print(as_text(t))
    elif a.cmd == "sync-mail":
        for f in sync_mail(a.label, a.days, a.dry_run):
            print(("would save " if a.dry_run else "saved ") + f)
    else:
        ap.print_help()
