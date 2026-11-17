#!/usr/bin/env python3
"""Content engine feedback: the author says it once, the engine does the edits.

Inputs, one step:
  --mail   replies to a done-note ("1, 2 og 5 er gode, 3 kortere, drop 4")
  --call   the latest interviewer call
  --loom   a Loom video with feedback (transcribed locally)
  --text   anything pasted (a dictated note)
  --auto   the hourly job: side jobs first, then hand edits in the doc, mail replies and new calls
Per post: approve (✅ in the doc heading), change (the post is rewritten under every rule and replaced in the doc,
old lines red, new lines green), drop (❌ in the heading). New general rules go into data/kb/voice_rules.md with the
author's own words as the source, and the feedback itself, word for word, into data/kb/feedback_log.md. Then a
five-line note lands in the author's inbox (Gmail insert, never a send). Every mail and call is handled once
(<batch>.feedback.json).

Usage:
  python3 scripts/content_engine_feedback.py --mail [--batch data/batches/<batch>.json] [--dry-run]
  python3 scripts/content_engine_feedback.py --call latest [--batch ...]
  python3 scripts/content_engine_feedback.py --text "1 og 2 er gode, 3 skal være kortere" --batch ...
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402
import content_engine_draft as ced  # noqa: E402
import llm_guard  # noqa: E402

KB = C.KB
BATCHES = C.BATCHES
GROUPS = ["What counts", "Tone of voice", "Posting", "Facts, privacy and money", "Images and video", "Working with the author"]

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts", "rules", "summary"], "properties": {
    "posts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
              "required": ["post", "decision", "instruction", "new_text", "cause"],
              "properties": {"post": {"type": "integer"},
                             "decision": {"type": "string", "enum": ["approve", "change", "drop", "none"]},
                             "cause": {"type": "string", "enum": ["error", "direction", "none"],
                                       "description": "for change/drop: 'error' = the post got something wrong that the engine "
                                       "could have known (a fact, a claim, an offer that isn't sold, unclear or clumsy Danish, maths "
                                       "too hard, a story told twice, against a rule already in the rulebook); 'direction' = the "
                                       "author adds something new the engine couldn't know (a fact, a story, a new preference or "
                                       "strategy). 'none' for approve/none"},
                             "instruction": {"type": "string", "description": "in English, what was asked for, short; empty for approve/none"},
                             "new_text": {"type": "string", "description": "only for change: the full Danish post with the change applied, else empty"}}}},
    "rules": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["group", "rule", "quote"],
              "properties": {"group": {"type": "string", "enum": GROUPS},
                             "rule": {"type": "string", "description": "the general rule in English, one sentence"},
                             "quote": {"type": "string", "description": "the author's own words, verbatim from the feedback, max 20 words"}}}},
    "summary": {"type": "string", "description": "in English, max two short lines: what was decided"}}}

TASK = """Below are the posts of one batch and the author's feedback on them (a mail reply or a call transcript).
For every post decide what they want: approve (they like it as it is, or said all are good), change (they want something
different: write the full new Danish post with exactly that change and nothing else changed, keeping every rule),
drop (they don't want it), or none (they didn't mention it). Posts can be named by number, by title or by topic.
Act on every point they make; never skip one. If they say something doesn't work but not what they want instead, make
new_text hold three short numbered versions of just that part ("Version 1: ...") so they can pick.
For every change or drop, say why in 'cause': 'error' when the draft was wrong in a way the engine could have known
(it breaks a fact, a rule already in the rulebook or plain sense), 'direction' when they bring something new (their own
fact, story, preference or strategy). When one change does both, pick 'error'. The score must tell the author's new
direction apart from the engine's mistakes.
Only add a rule when they state something general that should hold for future posts too, and quote their words verbatim.
Rules about how the interviewer should run a call (what to ask, lookups, length, ending) are learned elsewhere
(content_engine_interviewer.py learn): never add those here.
Never invent feedback they didn't give."""


BATCH_FILE = re.compile(r"\d{4}-\d\d-\d\d_.*Engine_Batch_(\d+)\.json")


def batch_files() -> list[pathlib.Path]:
    """The batch post files only, oldest first (by date, then batch number). The folder also holds *.feedback.json,
    *_visuals.json and auto_batches.json, which must never be read as a batch."""
    files = [f for f in BATCHES.glob("*.json") if BATCH_FILE.fullmatch(f.name)]
    return sorted(files, key=lambda f: (f.name[:10], int(BATCH_FILE.fullmatch(f.name).group(1))))


def latest_batch() -> pathlib.Path:
    files = batch_files()
    if not files:
        raise SystemExit(f"no Engine batch file (YYYY-MM-DD_Engine_Batch_N.json) in {BATCHES}")
    return files[-1]


class TabNotFound(LookupError):
    """The batch's tab is not in the content engine doc (renamed, deleted or a wrong batch name)."""


def _tab_key(title: str) -> str:
    return re.sub(r"[\s_]+", " ", title).strip().casefold()


def find_tab(doc: dict, name: str) -> dict:
    """A batch's tab in the doc: by its exact title or its tab id ("t.abc..."), else by the title ignoring case,
    spaces and underscores (a tab renamed by hand). Never a bare StopIteration: TabNotFound names the tabs there are."""
    tabs = doc.get("tabs") or []
    for t in tabs:
        if name in (t["tabProperties"]["title"], t["tabProperties"].get("tabId")):
            return t
    hits = [t for t in tabs if _tab_key(t["tabProperties"]["title"]) == _tab_key(name)]
    if len(hits) == 1:
        return hits[0]
    titles = ", ".join(repr(t["tabProperties"]["title"]) for t in tabs) or "none"
    why = f"{len(hits)} tabs match it loosely" if hits else "no tab has that title"
    raise TabNotFound(f"tab {name!r} not found in the content engine doc {ced.DOC_ID} ({why}); tabs there: {titles}")


LOOM = re.compile(r"https://(?:www\.)?loom\.com/share/[0-9a-f]{32}")


def loom_text(url: str) -> str:
    """Feedback on video: a Loom becomes text on your own machine first."""
    import loom_transcribe
    path = loom_transcribe.transcribe(url, None, loom_transcribe.OUT)
    return f"(Loom {url})\n" + re.sub(r"(?m)^\[\d\d:\d\d\] ", "", path.read_text())


def mail_feedback(name: str, seen: set) -> list[tuple[str, str]]:
    """Replies to the done-note of this batch: (message id, the author's own words, plus any Loom in them as text).
    Only replies this mailbox sent itself (from:me), never an inbound one: anyone can send a mail with the right
    subject, and an approval here puts a post on the author's LinkedIn."""
    import gmail_util
    q = f'subject:"Re: {name} is ready" from:me newer_than:60d'
    ids = [m["id"] for m in gmail_util.gmail_get("users/me/messages", params={"q": q, "maxResults": 20}).get("messages", [])]
    out = []
    for b in gmail_util.fetch_message_blocks([i for i in ids if i not in seen]):
        if b.get("inbound"):
            continue
        text = gmail_util.own_words(b["text"]).strip()
        for url in dict.fromkeys(LOOM.findall(text)):
            text += "\n\n" + loom_text(url)
        if text:
            out.append((b["msg_id"], text))
    return out


def call_feedback(which: str, seen: set) -> list[tuple[str, str]]:
    import content_engine_interviewer as iv
    agent = iv._state().get("agent_id")
    if not agent:  # no interviewer set up yet
        return []
    convs = iv._call("GET", f"/v1/convai/conversations?agent_id={agent}&page_size=10").get("conversations", [])
    convs = [c for c in convs if (which == "latest" or c["conversation_id"] == which) and c["conversation_id"] not in seen
             and (c.get("call_duration_secs") or 0) >= 60][:1]
    if not convs:
        return []
    return [(convs[0]["conversation_id"], ced.interview_text(convs[0]["conversation_id"]))]


def _norm(text: str) -> str:
    lines = [l.rstrip() for l in text.strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def _is_post_heading(style: str, text: str, has_image: bool) -> bool:
    """A post's own heading ("✅ 3 · Title"); one shared rule with the image placer (content_engine_visuals)."""
    import content_engine_visuals as vis
    return vis.is_post_heading(style, text, has_image)


def doc_texts(name: str) -> dict[int, str]:
    """Each post's current text as it stands in the doc: the paragraphs between the grey meta line and the images or
    the next heading, without struck-through (old, red) text."""
    docs = _docs()
    doc = docs.documents().get(documentId=ced.DOC_ID, includeTabsContent=True).execute()
    tab = find_tab(doc, name)
    out, post, lines, meta_seen = {}, 0, [], False
    for el in tab["documentTab"]["body"]["content"]:
        p = el.get("paragraph")
        if not p:
            continue
        style = p.get("paragraphStyle", {}).get("namedStyleType")
        if style == "HEADING_1":
            elems = p.get("elements", [])
            if not _is_post_heading(style, "".join(r.get("textRun", {}).get("content", "") for r in elems),
                                    any("inlineObjectElement" in r for r in elems)):
                continue  # an image or a lone mark in heading style: neither a new post nor post text
            if post:
                out[post] = _norm("\n".join(lines))
            post += 1
            lines, meta_seen = [], False
            continue
        if not post:
            continue
        if not meta_seen:
            meta_seen = True  # the grey meta line right under the heading
            continue
        runs = p.get("elements", [])
        if any("inlineObjectElement" in r for r in runs):
            continue
        text = "".join(r.get("textRun", {}).get("content", "") for r in runs
                       if not r.get("textRun", {}).get("textStyle", {}).get("strikethrough")).rstrip("\n")
        if text.startswith("[[IMG "):
            continue
        if any(r.get("textRun", {}).get("textStyle", {}).get("strikethrough") for r in runs) and not text.strip():
            continue  # a removed (red) line
        lines.append(text)
    if post:
        out[post] = _norm("\n".join(lines))
    return out


EDIT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rules"], "properties": {"rules": SCHEMA["properties"]["rules"]}}


def doc_edits(name: str, posts: list[dict]) -> list[tuple[int, str]]:
    """Posts the author changed by hand in the doc: (post number, their version)."""
    from difflib import SequenceMatcher
    try:
        current = doc_texts(name)
    except TabNotFound as e:  # one batch without its tab must not stop the other batches, mails and calls
        print(f"{name}: {e}; no edits read this time", file=sys.stderr)
        return []
    if len(current) != len(posts):  # a heading lost or doubled: every post after it would read as an edit
        print(f"{name}: {len(current)} posts in the doc, {len(posts)} in the file; no edits read this time")
        return []
    out = []
    for i, p in enumerate(posts, 1):
        if not (current.get(i) and p.get("status") != "dropped" and current[i] != _norm(p["text"])):
            continue
        # hand edits change words and lines, never most of a post: a near-total change is the doc misread
        if SequenceMatcher(None, _norm(p["text"]), current[i]).ratio() < 0.5:
            print(f"{name} post {i}: the doc text is mostly different from the file; not taken as an edit")
            continue
        out.append((i, current[i]))
    return out


def rules_from_edits(pairs: list[tuple[str, str]]) -> list[dict]:
    """What is the author removing, and what are they putting in instead?"""
    from anthropic import Anthropic
    shown = "\n\n".join(f"## Before\n{a}\n## After the edit\n{b}" for a, b in pairs)
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=ced.MODEL, max_tokens=3000, system=ced.SYSTEM,
        messages=[{"role": "user", "content": f"# Rules so far\n{(KB / 'voice_rules.md').read_text()}\n\n# The edits\n{shown}\n\n"
                   "# Task\nThe author edited these posts by hand. What general rules do the edits show (words swapped, what is "
                   "cut, tone)? Only rules that will hold for future posts and are not in the rules already. The quote must be "
                   "the words the author wrote in (from 'After the edit'), verbatim. An empty list is fine."}],
        output_config={"format": {"type": "json_schema", "schema": EDIT_SCHEMA}})
    return json.loads(msg.content[0].text)["rules"]


def decide(posts: list[dict], feedback: str) -> dict:
    from anthropic import Anthropic
    numbered = "\n\n".join(f"## Post {i} · {p['title']}\n{p['text']}" for i, p in enumerate(posts, 1))
    user = (f"# Rules\n{(KB / 'voice_rules.md').read_text()}\n\n# The batch\n{numbered}\n\n# The feedback\n{feedback}\n\n# Task\n{TASK}")
    client = llm_guard.guarded(Anthropic())
    msg = client.messages.create(model=ced.MODEL, max_tokens=16000, system=ced.SYSTEM, messages=[{"role": "user", "content": user}],
                                 output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    out = json.loads(msg.content[0].text)
    bad = {d["post"]: ced.lint(d["new_text"]) for d in out["posts"] if d["decision"] == "change" and ced.lint(d["new_text"])}
    if bad:  # one repair pass, same as the drafter
        fix = "Fix only these rule breaks in the rewritten posts and return everything again:\n" + \
              "\n".join(f"post {k}: {', '.join(v)}" for k, v in bad.items()) + "\n\n" + json.dumps(out, ensure_ascii=False)
        msg = client.messages.create(model=ced.MODEL, max_tokens=16000, system=ced.SYSTEM, messages=[{"role": "user", "content": fix}],
                                     output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
        out = json.loads(msg.content[0].text)
    rewritten = [d for d in out["posts"] if d["decision"] == "change" and d["new_text"].strip()]
    if rewritten:  # the same native-Danish read and word check as new drafts get; the feedback and the post's
        tmp = [{"text": d["new_text"]} for d in rewritten]  # old words count as the author's own
        ced.native_check(tmp)
        import voice_bank as vb
        ced.vocab_fix(tmp, set(vb.tokens(feedback + numbered)))
        for d, t in zip(rewritten, tmp):
            d["new_text"] = t["text"]
    return out


def _docs():
    return ced._docs()


RED, GREEN = {"red": 0.80, "green": 0.16, "blue": 0.16}, {"red": 0.20, "green": 0.56, "blue": 0.05}


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def marked_body(old: str, new: str) -> tuple[str, list[tuple[int, int, str]]]:
    """The revision format: what doesn't work is red, the new version green. Unchanged lines stay plain; removed lines
    red and struck through, new lines green."""
    import difflib
    a, b = old.strip().splitlines(), new.strip().splitlines()
    text, spans = "", []

    def put(lines, kind):
        nonlocal text
        for line in lines:
            start = _u16(text)
            text += line + "\n"
            if kind and line.strip():
                spans.append((start, start + _u16(line), kind))
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            put(a[i1:i2], None)
        else:
            put(a[i1:i2], "red")
            put(b[j1:j2], "green")
    return text, spans


def apply_in_doc(name: str, posts: list[dict], decisions: list[dict]) -> None:
    """Mark ✅/✏️/❌ in each post's heading; a change shows old lines red and new lines green, an approval cleans the
    post back to plain text. Bottom-up, so indexes stay valid."""
    docs = _docs()
    doc = docs.documents().get(documentId=ced.DOC_ID, includeTabsContent=True).execute()
    tab = find_tab(doc, name)
    tab_id = tab["tabProperties"]["tabId"]
    paras = [(el["startIndex"], el["endIndex"], el["paragraph"].get("paragraphStyle", {}).get("namedStyleType"),
              "".join(r.get("textRun", {}).get("content", "") for r in el["paragraph"].get("elements", [])),
              any("inlineObjectElement" in r for r in el["paragraph"].get("elements", [])))
             for el in tab["documentTab"]["body"]["content"] if "paragraph" in el]
    heads = [k for k, p in enumerate(paras) if _is_post_heading(p[2], p[3], p[4])]
    reqs = []
    for d in sorted(decisions, key=lambda d: -d["post"]):
        i = d["post"]
        if d["decision"] in ("none",) or i < 1 or i > len(heads):
            continue
        h = heads[i - 1]
        start, end, _, text, _ = paras[h]
        # body = the paragraphs after the grey meta line, up to the post's images (or the next heading): a rewrite
        # leaves the images under the post where they are
        body = []
        for p in paras[h + 2: heads[i] if i < len(heads) else len(paras)]:
            if p[4] or p[3].startswith("[[IMG ") or p[2] == "HEADING_1":
                break
            if not body and not p[3].strip():
                continue  # the blank line between the grey block and the post stays
            body.append(p)
        new_body, spans = None, []
        if d["decision"] == "change" and d["new_text"].strip():
            new_body, spans = marked_body(posts[i - 1]["text"], d["new_text"])
        elif d["decision"] == "approve" and text.startswith("✏️"):
            new_body = posts[i - 1]["text"].strip() + "\n"  # approved after a change: clean, no red or green left
        if new_body is not None and body:
            b0, b1 = body[0][0], body[-1][1] - 1
            reqs.append({"deleteContentRange": {"range": {"startIndex": b0, "endIndex": b1, "tabId": tab_id}}})
            reqs.append({"insertText": {"location": {"index": b0, "tabId": tab_id}, "text": new_body}})
            reqs.append({"updateTextStyle": {"range": {"startIndex": b0, "endIndex": b0 + _u16(new_body), "tabId": tab_id},
                                             "textStyle": {}, "fields": "foregroundColor,strikethrough"}})
            for s0, s1, kind in spans:
                style = {"foregroundColor": {"color": {"rgbColor": RED if kind == "red" else GREEN}}}
                if kind == "red":
                    style["strikethrough"] = True
                reqs.append({"updateTextStyle": {"range": {"startIndex": b0 + s0, "endIndex": b0 + s1, "tabId": tab_id},
                                                 "textStyle": style, "fields": "foregroundColor,strikethrough"}})
        mark = {"approve": "✅ ", "drop": "❌ ", "change": "✏️ "}[d["decision"]]
        if text.startswith(("✅", "❌", "✏️")):  # a later decision replaces the earlier mark
            old_mark = next(m for m in ("✅ ", "❌ ", "✏️ ") if text.startswith(m))
            reqs.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": start + _u16(old_mark), "tabId": tab_id}}})
        reqs.append({"insertText": {"location": {"index": start, "tabId": tab_id}, "text": mark}})
    if reqs:
        docs.documents().batchUpdate(documentId=ced.DOC_ID, body={"requests": reqs}).execute()


def add_rules(rules: list[dict], source: str) -> int:
    path = KB / "voice_rules.md"
    t = path.read_text()
    added = 0
    for r in rules:
        if r["quote"].strip().lower() in t.lower():
            continue
        line = f'- {r["rule"].rstrip(".")}: "{r["quote"].strip()}" ({source}).'
        head = f"## {r['group']}"
        if head not in t:
            continue
        i = t.index(head)
        # a section ends at the next heading or at a written block (the numbers block linkedin_post_results.py
        # rewrites every Monday; a rule put inside it would be lost)
        j = min((k for k in (t.find("\n## ", i + 1), t.find("\n<!-- ", i + 1)) if k >= 0), default=len(t))
        t = t[:j].rstrip("\n") + "\n" + line + "\n" + ("\n" if j < len(t) else "") + t[j:].lstrip("\n")
        added += 1
    path.write_text(t)
    return added


def note(name: str, summary: str, decisions: list[dict], rules_added: int, dry: bool) -> str:
    counts = {k: [str(d["post"]) for d in decisions if d["decision"] == k] for k in ("approve", "change", "drop")}
    lines = [f"{name}: your feedback is done in the doc."]
    lines += [f"{label} {', '.join(v)}." for label, v in (("Approved:", counts["approve"]), ("Rewritten:", counts["change"]),
                                                         ("Dropped:", counts["drop"])) if v]
    if rules_added:
        lines.append(f"{rules_added} new rule{'s' if rules_added > 1 else ''} in the rulebook.")
    lines = lines[:4] + [summary.strip()[:200]] if summary.strip() else lines[:5]
    if dry:
        return "\n".join(lines)
    inbox(f"{name}: feedback done", lines[:5])
    return "\n".join(lines[:5])


def active_batches() -> list[pathlib.Path]:
    """Engine batches with a post not yet posted or dropped."""
    out = []
    for f in batch_files():
        posts = json.loads(f.read_text())
        if any(isinstance(p, dict) and p.get("status") != "dropped" and not p.get("posted") for p in posts):
            out.append(f)
    return out


def keep_doc_edits(batch: pathlib.Path, name: str, posts: list[dict], state: dict, state_file: pathlib.Path, dry: bool) -> None:
    """The author's own edits in the doc: kept as the post, learned from, no note (they made them themselves)."""
    edits = doc_edits(name, posts)
    if not edits:
        return
    pairs = [(posts[i - 1]["text"], t) for i, t in edits]
    for i, t in edits:
        p = posts[i - 1]
        p.setdefault("history", []).append({"when": dt.date.today().isoformat(), "via": "edit in the doc", "cause": "edit",
                                            "decision": "edit", "instruction": "", "old_text": p["text"]})
        p["text"] = t
    batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    learned = rules_from_edits(pairs) if not dry else []
    added = add_rules(learned, f"edit in the doc, {dt.date.today():%d %b}") if learned else 0
    state["log"].append({"when": dt.datetime.now().isoformat(timespec="seconds"), "via": "doc edit",
                         "summary": f"posts {[i for i, _ in edits]} edited by hand, {added} rule(s) learned"})
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print(f"{name}: hand edits kept: posts {[i for i, _ in edits]}, {added} new rule(s)")


FEEDBACK_LOG = KB / "feedback_log.md"


def log_feedback(name: str, label: str, texts: list[str]) -> None:
    """All feedback, word for word, in one file the drafter reads. The rules picked from it go to voice_rules.md;
    this keeps what a rule didn't capture."""
    if not FEEDBACK_LOG.exists():
        FEEDBACK_LOG.write_text("# Feedback on the content engine, word for word (newest last)\n")
    with FEEDBACK_LOG.open("a") as fh:
        for t in texts:
            fh.write(f"\n## {dt.date.today():%d %b %Y} · {name} · via {label}\n{t.strip()}\n")


def apply_feedback(batch: pathlib.Path, name: str, posts: list[dict], state: dict, state_file: pathlib.Path,
                   items: list[tuple[str, str]], label: str) -> None:
    """Decide on a batch's feedback (approve, change, drop), put it in the doc and the batch file, note it in the inbox."""
    log_feedback(name, label, [t for _, t in items])
    out = decide(posts, "\n\n".join(t for _, t in items))
    acted = [d for d in out["posts"] if d["decision"] != "none"]
    if acted:
        apply_in_doc(name, posts, acted)
        for d in acted:
            p = posts[d["post"] - 1]
            p.setdefault("history", []).append({"when": dt.date.today().isoformat(), "via": label, "decision": d["decision"],
                                                "instruction": d["instruction"], "cause": d.get("cause", "none"),
                                                **({"old_text": p["text"]} if d["decision"] == "change" else {})})
            p["status"] = {"approve": "approved", "change": "changed", "drop": "dropped"}[d["decision"]]
            if d["decision"] == "change":
                old = p["text"]
                p["text"] = d["new_text"].strip()
                ced.flag_numbers(p, " ".join([p.get("source", ""), ced.seed_text(p), old] + [t for _, t in items]))
        batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    added = add_rules(out["rules"], f"{label}, {dt.date.today():%d %b}")
    state["seen"] += [i for i, _ in items]
    state["log"].append({"when": dt.datetime.now().isoformat(timespec="seconds"), "via": label, "summary": out["summary"]})
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    if acted:
        print(note(name, out["summary"], acted, added, False))
        try:  # a changed hook gets its new reason in the grey block
            ced.hook_notes(batch, [d["post"] for d in acted if d["decision"] == "change"])
        except Exception as e:
            print(f"hook notes not refreshed: {type(e).__name__}")
        refresh_interviewer()
        ced.english_tab_safe(batch)


def refresh_interviewer() -> None:
    """The interviewer's brief follows every approval, change and drop, so a call never goes through settled posts."""
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "content_engine_interviewer.py"), "refresh"],
                       check=True, capture_output=True, timeout=120)
    except Exception as e:  # a failed push never blocks the feedback itself
        print(f"interviewer brief not refreshed: {type(e).__name__}")


def side_jobs() -> None:
    """The hourly run also: the posting bot, new videos finished, a new interviewer call turned into the next batch,
    the interviewer's own rules learned, new photos placed, LinkedIn messages refreshed once a day, the posting plan.
    Each runs on its own; one failing never stops the feedback run."""
    py = sys.executable
    # the posting bot first: a due post goes out at its time, never an hour late behind the photo and video jobs;
    # videos next (someone is waiting for those, and the photo job can run long); the plan last, so a photo or video
    # that landed in this run already counts
    for args, limit in ((["linkedin_post.py", "due"], 900), (["content_engine_video.py", "--auto"], 3000),
                        (["content_engine_draft.py", "--auto-batch"], 2700),
                        (["content_engine_interviewer.py", "learn"], 600),
                        (["content_engine_photos.py", "--auto"], 3000),
                        (["linkedin_dma_sync.py", "--auto"], 900), (["linkedin_post_dms.py"], 900), (["linkedin_post.py", "plan"], 600)):
        try:
            r = subprocess.run([py, str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True, timeout=limit)
            if r.stdout.strip() or r.returncode:
                print(f"{args[0]}: {(r.stdout.strip() or r.stderr.strip()[-300:])}")
        except Exception as e:
            print(f"{args[0]} failed: {e}")


def inbox(subject: str, lines: list[str]) -> str:
    """A short note placed straight in the author's inbox as unread: Gmail insert, never a send."""
    import gmail_util
    return gmail_util.inbox(subject, lines)


# Every doc write under one lock (content_engine_lock.py)
from content_engine_lock import locked as _locked_doc  # noqa: E402
apply_in_doc = _locked_doc(apply_in_doc)
keep_doc_edits = _locked_doc(keep_doc_edits)
apply_feedback = _locked_doc(apply_feedback)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--mail", action="store_true")
    src.add_argument("--call", metavar="latest|ID")
    src.add_argument("--loom", metavar="URL")
    src.add_argument("--text")
    src.add_argument("--auto", action="store_true", help="hourly job: new mail replies and new calls, silent when there is nothing")
    ap.add_argument("--batch", type=pathlib.Path)
    ap.add_argument("--dry-run", action="store_true", help="decide and print, change nothing")
    a = ap.parse_args()
    batch = a.batch or latest_batch()
    name = batch.stem.split("_", 1)[1].replace("_", " ")
    state_file = batch.with_name(batch.stem + ".feedback.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {"seen": [], "log": []}
    posts = json.loads(batch.read_text())
    seen = set(state["seen"])
    if a.auto:
        side_jobs()
        # No early exit once every post is approved or dropped: edits in the doc, mail replies and calls about an
        # approved batch still carry rules, so they are read every run. Every batch not fully posted is read; a call
        # only gives feedback on the newest.
        for older in active_batches():
            if older != batch:
                o_posts, o_name = json.loads(older.read_text()), older.stem.split("_", 1)[1].replace("_", " ")
                o_state_file = older.with_name(older.stem + ".feedback.json")
                o_state = json.loads(o_state_file.read_text()) if o_state_file.exists() else {"seen": [], "log": []}
                keep_doc_edits(older, o_name, o_posts, o_state, o_state_file, a.dry_run)
                o_items = [x for x in mail_feedback(o_name, set(o_state["seen"]))]
                if o_items and not a.dry_run:
                    apply_feedback(older, o_name, o_posts, o_state, o_state_file, o_items, "mail reply")
        keep_doc_edits(batch, name, posts, state, state_file, a.dry_run)
        items = mail_feedback(name, seen) + call_feedback("latest", seen)
        label = "mail reply" if items and not items[0][0].startswith("conv_") else "review call"
    elif a.mail:
        items, label = mail_feedback(name, seen), "mail reply"
    elif a.call:
        items, label = call_feedback(a.call, seen), "review call"
    elif a.loom:
        items, label = [] if a.loom in seen else [(a.loom, loom_text(a.loom))], "Loom"
    else:
        items, label = [("text:" + dt.datetime.now().isoformat(timespec="seconds"), a.text)], "note"
    if not items:
        if a.auto:
            sys.exit(0)
        sys.exit(f"no new feedback for {name}")
    feedback = "\n\n".join(t for _, t in items)
    out = decide(posts, feedback)
    acted = [d for d in out["posts"] if d["decision"] != "none"]
    if not acted:  # e.g. a stories-only call: no post to change, so no note (quiet by default)
        if not a.dry_run:
            if label != "review call":  # the words into the drafter's log too (a call stays on ElevenLabs, its rules below)
                log_feedback(name, label, [t for _, t in items])
            added = add_rules(out["rules"], f"{label}, {dt.date.today():%d %b}")
            state["seen"] += [i for i, _ in items]
            state["log"].append({"when": dt.datetime.now().isoformat(timespec="seconds"), "via": label,
                                 "summary": f"no post decisions, {added} rule(s) added"})
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        sys.exit(0)
    for d in acted:
        print(f"post {d['post']}: {d['decision']}" + (f" ({d['instruction']})" if d["instruction"] else ""))
    for r in out["rules"]:
        print(f"rule [{r['group']}]: {r['rule']} \"{r['quote']}\"")
    if a.dry_run:
        print(note(name, out["summary"], acted, len(out["rules"]), True))
        sys.exit(0)
    apply_in_doc(name, posts, acted)
    for d in acted:
        p = posts[d["post"] - 1]
        p.setdefault("history", []).append({"when": dt.date.today().isoformat(), "via": label, "decision": d["decision"],
                                            "instruction": d["instruction"], "cause": d.get("cause", "none"),
                                            **({"old_text": p["text"]} if d["decision"] == "change" else {})})
        p["status"] = {"approve": "approved", "change": "changed", "drop": "dropped"}[d["decision"]]
        if d["decision"] == "change":
            old = p["text"]
            p["text"] = d["new_text"].strip()
            ced.flag_numbers(p, " ".join([p.get("source", ""), ced.seed_text(p), old, feedback]))
    batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    added = add_rules(out["rules"], f"{label}, {dt.date.today():%d %b}")
    state["seen"] += [i for i, _ in items]
    state["log"].append({"when": dt.datetime.now().isoformat(timespec="seconds"), "via": label, "summary": out["summary"]})
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print(note(name, out["summary"], acted, added, False))
    # the same follow-through as a mail reply or a call: the raw-words log and the interviewer's brief
    if label != "review call":
        log_feedback(name, label, [t for _, t in items])
    try:
        ced.hook_notes(batch, [d["post"] for d in acted if d["decision"] == "change"])
    except Exception as e:
        print(f"hook notes not refreshed: {type(e).__name__}")
    refresh_interviewer()
    ced.english_tab_safe(batch)
