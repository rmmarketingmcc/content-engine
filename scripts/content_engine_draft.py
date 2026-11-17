#!/usr/bin/env python3
"""Content engine drafter: calls in, a batch of Danish LinkedIn drafts out.

Reads one or more sources (a call transcript from any note taker by date, the latest interviewer call, a story-seeds file), writes N
Danish drafts in the author's voice and puts them in a new tab of the posts doc, numbered, so the author approves
with a check mark in the heading. Runs on the Claude subscription when Claude Code is installed (llm_guard).

What it knows: data/kb/about.md (facts), data/kb/icp.md (who the author wants as clients, so posts speak to them),
data/kb/linkedin_rules.md (house rules; prompts/linkedin_rules.md until you make your own), data/kb/voice_rules.md
(rules learned from the author's feedback), data/kb/what_works.md (what works on the profile, plus live numbers per post that linkedin_post_results.py rewrites
after every pull), data/kb/feedback_log.md (the feedback word for word), the two newest approved batches in
data/kb/posts_so_far.md as style examples, and every hook already posted (so no hook or wording repeats; important
topics do come back). The voice bank (voice_bank.py): 15 of the author's own dictated sentences on the batch's topics
as examples, and every word of every draft checked against the words the author has actually said; one they never
say is swapped.

Usage:
  python3 scripts/content_engine_draft.py --call 2026-10-26:"Content call" --interview latest \
      --seeds data/kb/story_seeds.md --n 7 --tab "Engine Batch 1" [--dry-run]
  python3 scripts/content_engine_draft.py --selftest
"""
import argparse
import datetime as dt
import glob
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402
import llm_guard  # noqa: E402

KB = C.KB
ICP = KB / "icp.md"          # who the author wants as clients
FACTS = KB / "about.md"      # facts about the author and the business
DOC_ID = C.get("google", "doc_id")
MODEL = C.get("llm", "model")
NAME = C.get("author", "name") or C.author()
FIRST = C.author()
# a post with no open question in its Check line is approved by itself from this date (empty: never)
AUTO_APPROVE_FROM = dt.date.fromisoformat(C.get("drafting", "auto_approve_from")) if C.get("drafting", "auto_approve_from") else None
DASHES = chr(0x2014) + chr(0x2013)  # em and en dash, kept out of the source on purpose
EMOJI = set(C.get("author", "emoji").replace(" ", ""))
NO_COLONS = bool(C.get("drafting", "no_colons"))
PROPOSED = C.get("drafting", "proposed_tag")

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"], "properties": {"posts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
    "required": ["title", "title_da", "role", "format", "text", "visual", "source", "check", "goal", "reader_gets",
                 "timing", "post_by", "timing_why"],
    "properties": {
        # two kinds of post: one with a deadline (something happening now), one for any time
        "timing": {"type": "string", "enum": ["now", "anytime"], "description": "now = about something happening now or tied "
                   "to a date or a season, so it must go out before a last date; anytime = still true and fresh in two months"},
        "post_by": {"type": "string", "description": "for timing 'now': the last date (YYYY-MM-DD) the post still makes sense; "
                    "for 'anytime': empty"},
        "timing_why": {"type": "string", "description": "in English, a few words: why now or why any time"},
        "title": {"type": "string", "description": "3 to 6 word working title in English"},
        "title_da": {"type": "string", "description": "the same working title in Danish, proper æøå"},
        "role": {"type": "string", "enum": ["personal", "get something", "problem and fix", "offer", "other"],
                 "description": "personal = the author's life, people, money decisions, humor; get something = a concrete thing "
                                "to get by commenting or writing to them (template, guide, checklist); problem and fix = a problem "
                                "the reader recognises, with the author's fix on offer; offer = mainly sells the service"},
        "format": {"type": "string", "enum": ["text", "photo", "graphic", "carousel", "video"],
                   "description": "photo and video mean a real photo or video of the author"},
        "text": {"type": "string", "description": "the full Danish post, ready to paste"},
        "visual": {"type": "string", "description": "in English, one line: photo, graphic or video idea, or empty"},
        "source": {"type": "string", "description": "in English: where the story comes from, with date and timestamp"},
        "check": {"type": "string", "description": "in English: facts or names the author must confirm before posting, or empty"},
        "reader_gets": {"type": "string", "description": "in English, one sentence: what the reader concretely gets out of this post "
                        "(a number to check in their own business, a way to see a mistake, a free thing). A post with no clear "
                        "answer is not worth writing."},
        "goal": {"type": "string", "description": "in English, one or two sentences: what this post is for and the response we "
                 "expect (e.g. comments with a keyword, DMs from owners in a trade, shares to a colleague, trust from a "
                 "past story), and why it is worth posting now"}}}}}}


def _system() -> str:
    business = f", owner of {C.get('author', 'business')}" if C.get("author", "business") else ""
    pitch = f" ({C.get('author', 'pitch')})" if C.get("author", "pitch") else ""
    dialect = f" {C.get('author', 'dialect_note')}" if C.get("author", "dialect_note") else ""
    return f"""You write LinkedIn posts in Danish as {NAME}{business}{pitch}. You write in their voice, first person, from their own words in the sources. You never invent facts, numbers, names or events: everything must come from the sources or the facts sheet.

Hard rules:
- Danish with æ, ø, å. Tone: {C.get('author', 'voice')}.{dialect}
- One thought per line, blank lines between short paragraphs. No list markers of any kind (no dashes, numbers or bullets at the start of a line); if something is a list, write it as short lines.
- No long dashes anywhere (only the normal hyphen inside words). No hashtags. At most one emoji per post, only from this set: {C.get('author', 'emoji')}.
- One post per story or event in a batch. Never write two posts about the same incident.
- Never name a client or identify one by niche plus detail. Use broad categories ("en VVS'er", "en lokal servicevirksomhed"). People may only be named if the source says it's OK.
- No exact client revenue, profit or budget. Percentages are fine.
- Build in public: the author's own business numbers (revenue, active clients, churn, new clients a month, close rate) are welcome, exact, in kroner, with their date, but only the figures in the facts sheet. Never one client's own fee. Time spent and what AI saves only when the facts sheet has the number; otherwise ask in 'check'.
- Money is always in Danish kroner, written like 5.000 kr. Never dollars or euros, even when the source says dollars: use the kroner figure given alongside it. If there is none, leave the amount out and ask for it in 'check'. Never convert currencies yourself.
- At most one swear word per post (fuck, shit, fandeme, kraftedeme, lort, pis, helvede, satan, røv count; "sgu" and "satme" don't).
- Never marketing jargon: headroom, capture, unlock, leverage, momentum, game changer, deep dive, workflow, streamlined, kalibreret, iterativt, moat, voldgrav, kalkuleret risiko and the like, and never the AI pattern "Det er ikke kun X. Det er Y".
- Never name a competitor, another agency or a tool vendor to make a point.
- Per batch (one week): at least one "get something" post, at most two offer posts, at least two posts built to start a DM, and at least half the posts with a real photo or video of the author (format photo or video). Every post gets an image, and most of them are of the author: a story or opinion post is a photo post, a graphic leads only where it carries the content (a list, numbers, steps, a carousel). The engine adds a graphic to every post: on a photo post it is only the stand-in until the photo is there, on a video post it is the cover. Carousels only when there are steps or a list, and a carousel post names the reader's pain early (what goes wrong for them today and what it costs them, in words from the material) so the second slide can show it. Flag any client screenshot in 'check' as needing the client's OK. Every video gets Danish captions.
- Never "Del gerne", never a "3 måder jeg kan hjælpe dig" footer. A comment keyword is only OK for a free guide (like "Skriv GUIDE i kommentarsporet").
- End with a real question or a soft one-line offer (a free check, a guide, a review). Vary it.
- 120 to 260 words per post. Hooks: a specific moment, a number or a contrarian line. Never open with "I dag" or a generic statement.
- Each part of a post has one job: the image is a relevant eye-catcher that makes them stop and wonder what it says; the hook (what shows before LinkedIn's "...se mere", about the first 200 characters or 3 short lines) is the reason they click "se mere", so it opens a gap the reader needs closed: a surprising number, a moment mid-scene, a claim they disagree with, never the point given away; the body is good enough that they like, comment, or send it to someone. Name concrete things over categories (Dinero og e-conomic, not "dit regnskabsprogram").
- Important topics come back on purpose: a topic posted about before is welcome, with a new moment, example or angle, and a story told before can come back told a new way. Never reuse an old post's hook or wording (see the list)."""


SYSTEM = _system()


def call_text(spec: str) -> str:
    """spec = 'YYYY-MM-DD:title words', a call transcript from any note taker in [sources] transcripts_dir (or the
    older meetgeek_dir). The title part is required, so a day's other meetings (client calls, team meetings) never
    become a source by accident."""
    import transcripts
    found = transcripts.find(spec)
    if not found:
        sys.exit(f"No transcript for {spec!r}: `python3 scripts/transcripts.py list` shows what the folder holds")
    return "\n\n".join(transcripts.as_text(t) for t in found)


def interview_text(which: str) -> str:
    if "," in which:  # several calls (a week's carried-over call plus the new one), oldest first
        return "\n\n".join(filter(None, (interview_text(w) for w in which.split(","))))
    import content_engine_interviewer as iv
    agent_id = iv._state()["agent_id"]
    if which == "latest":
        convs = iv._call("GET", f"/v1/convai/conversations?agent_id={agent_id}&page_size=10").get("conversations", [])
        convs = [c for c in convs if (c.get("call_duration_secs") or 0) >= 60][:1]
        if not convs:
            return ""
        which = convs[0]["conversation_id"]
    d = iv._call("GET", f"/v1/convai/conversations/{which}")  # by id: a carried call can be older than the last 10
    if (d.get("metadata", {}).get("call_duration_secs") or 0) < 60:
        return ""
    when = dt.datetime.fromtimestamp(d.get("metadata", {}).get("start_time_unix_secs", 0)).strftime("%Y-%m-%d")
    lines = []
    for t in d.get("transcript", []):
        if t.get("message"):
            lines.append(f"[{t.get('time_in_call_secs', 0)}s] {FIRST if t['role'] == 'user' else 'Interviewer'}: {t['message']}")
        for r in t.get("tool_results") or []:  # what the interviewer looked up live, so the draft can use the real words
            if not r.get("is_error"):
                lines.append(f"[{t.get('time_in_call_secs', 0)}s] Lookup {r.get('tool_name')}: {str(r.get('result_value', ''))[:900]}")
    return f"### Interview call {when} (with the interviewer)\n" + "\n".join(lines)


def posted_sections() -> tuple[str, list[str]]:
    """(the two newest approved batches as examples, every hook already posted)."""
    f = KB / "posts_so_far.md"
    text = f.read_text() if f.exists() else ""
    batches = re.split(r"\n# (?=Batch|\[Batch|Revised|Pilot)", text)
    examples = "\n\n# ".join(batches[1:3])
    hooks = []
    for block in re.split(r"\n## ", text)[1:]:
        lines = [l.strip() for l in block.splitlines()[1:] if l.strip()]
        if lines:
            hooks.append(lines[0][:140])
    return examples, hooks


NEVER_WORDS = re.compile(r"(?i)\b(" + "|".join(C.get("author", "never_words")) + r")\b") if C.get("author", "never_words") else None


def lint(text: str) -> list[str]:
    issues = []
    if any(c in text for c in DASHES):
        issues.append("long dash")
    if re.search(r"(?m)^\s*([-*•]|\d+[.)])\s", text):
        issues.append("list marker")
    if re.search(r"\b(paa|ogsaa|foer|faa|naar|hoej|vaerdi|maaned|saa)\b", text):
        issues.append("ae/oe/aa instead of æøå")
    if FIRST and FIRST != "Author" and re.search(rf"\b{re.escape(FIRST)}\b", text):
        issues.append(f"third person {FIRST}")
    emojis = re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", text)
    if len(emojis) > 1 or any(e not in EMOJI for e in emojis):
        issues.append("emoji outside the set or more than one")
    # a comment keyword for a free guide is allowed ("Skriv GUIDE i kommentarsporet")
    if re.search(r"(?i)del gerne|(?<![\w/])#\w", text):
        issues.append("banned phrase or hashtag")
    if re.search(r"(?i)[$€]|\b(dollars?|usd|bucks|euros?|eur)\b", text):
        issues.append("dollars or euros instead of kroner")
    if len(SWEAR.findall(text)) > 1:
        issues.append("more than one swear word")
    jargon = sorted({m.group(0).lower() for m in JARGON.finditer(text)})
    if jargon:
        issues.append("marketing jargon: " + ", ".join(jargon))
    never = sorted({m.group(0).lower() for m in NEVER_WORDS.finditer(text)}) if NEVER_WORDS else []
    if never:
        issues.append("a word the author never uses: " + ", ".join(never))
    if NO_COLONS and re.search(r"(?<!\d):(?!\d|//)", text):  # a time like 14:46 or a link is fine
        issues.append("colon")
    return issues


SWEAR = re.compile(r"(?i)\b(fuck\w*|shit\w*|fandeme|fanden|kraftedeme|kraftæ\w*|lort\w*|pis|pisse\w*|helvede|satans?|røv\w*)\b")
JARGON = re.compile(r"(?i)\b(headroom|capture|unlock\w*|leverage|second wave|hovedmotoren|iterativt|momentum|game[- ]changer|"
                    r"deep[- ]dive|hovedbegrænsning|workflow|streamlined|kalibreret|kombineret spil|repeat-rate|impact-drevet|"
                    r"diminishing returns|signal-rigt|kontekst-bevidst|moat|voldgrav|kalkuleret risiko|gated?)\b"
                    r"|det er ikke kun [^.?!]{1,80}[.!] det er")


def batch_lint(posts: list[dict]) -> list[str]:
    """The weekly mix: DMs first, offers rare, the author's face in half the posts."""
    roles = [p.get("role") for p in posts]
    issues = []
    if roles.count("get something") < 1:
        issues.append('no "get something" post (a concrete thing to get by commenting or writing to the author)')
    if roles.count("offer") > 2:
        issues.append(f"{roles.count('offer')} offer posts, max 2")
    if sum(r in ("get something", "problem and fix", "offer") for r in roles) < 2:
        issues.append("fewer than two posts built to start a DM")
    faces = sum(p.get("format") in ("photo", "video") for p in posts)
    if faces * 2 < len(posts):
        issues.append(f"only {faces} of {len(posts)} posts with a real photo or video of the author, at least half")
    return issues


def face_floor(posts: list[dict]) -> list[int]:
    """The author's face first: every text post becomes a photo post. A graphic or carousel post keeps its graphic,
    it carries real content."""
    flipped = []
    for i, p in enumerate(posts, 1):
        if p.get("format") == "text" and p.get("status") != "dropped":
            p["format"], p["visual"] = "photo", p.get("visual") or "A recent photo of the author that fits the story"
            flipped.append(i)
    return flipped


def hook_reuse(posts: list[dict], hooks: list[str]) -> dict[int, str]:
    """Topics may come back, an old hook may not: a first line 80%+ like a posted one is flagged."""
    import difflib
    out = {}
    for i, p in enumerate(posts):
        first = next((l.strip() for l in p["text"].splitlines() if l.strip()), "").lower()
        if first and any(difflib.SequenceMatcher(None, first, h.lower()).ratio() >= 0.8 for h in hooks):
            out[i] = "reuses an old hook, write a new first line (the topic itself is fine)"
    return out


def _numbers(text: str) -> set[str]:
    """The distinctive numbers in a post (two digits or more, years and percent signs aside). Everyday numbers (12 and
    under, round ones like 15, 20, 24, 25) are left out, so two unrelated posts that both say "10 days" and "25
    clients" never count as the same story."""
    return {n for n in re.findall(r"\d[\d.,]*\d", text) if not re.fullmatch(r"20\d\d", n) and not _everyday(n)}


def _everyday(n: str) -> bool:
    try:
        v = float(n.replace(".", "").replace(",", "."))
    except ValueError:
        return False
    return v <= 12 or v in (24, 48, 1000) or (v <= 100 and v % 5 == 0)


def story_reuse(posts: list[dict], earlier: list[str]) -> dict[int, str]:
    """A story already told by the engine, caught by its numbers (the seed check can't see a story that came in
    through feedback). Two or more shared distinctive numbers with one earlier post = the same story."""
    out = {}
    for i, p in enumerate(posts):
        mine = _numbers(p["text"])
        for t in earlier:
            shared = mine & _numbers(t)
            if len(shared) >= 2:
                out[i] = f"tells a story already in an earlier engine post (same numbers: {', '.join(sorted(shared))}); pick another story"
                break
    return out


def earlier_texts() -> list[str]:
    import content_engine_feedback as fb
    out = []
    for f in sorted(fb.BATCHES.glob("*.json")):
        if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name):
            out += [p["text"] for p in json.loads(f.read_text()) if isinstance(p, dict) and p.get("status") != "dropped"]
    return out


def selftest() -> None:
    global FIRST, EMOJI, NEVER_WORDS, NO_COLONS
    FIRST, EMOJI, NO_COLONS = "Freja", set("😅🫠"), True  # fixed test settings, whatever config.toml says
    NEVER_WORDS = re.compile(r"(?i)\b(dramatisk\w*|krystalklar\w*)\b")
    bad = {"Det kostede os omkring 700 dollars.": "dollars or euros instead of kroner",
           "Det kostede 700 $.": "dollars or euros instead of kroner",
           "Hej " + DASHES[0] + " du": "long dash",
           "- første punkt": "list marker",
           "Det er ogsaa fint": "ae/oe/aa instead of æøå",
           "Freja mener det": "third person Freja",
           "Fedt 😄": "emoji outside the set or more than one",
           "Del gerne opslaget": "banned phrase or hashtag",
           "Fuck, det var fandeme dyrt": "more than one swear word",
           "Det mest interessante: tallet": "colon"}
    good = ("Det kostede et sted mellem 3.000 og 4.000 kr. 🫠\nSkriv GUIDE i kommentarsporet.\n"
            "Det er sgu satme godt, og alt det lort tager jeg gerne med. Kl. 14:46 på https://example.com.")
    fails = [f"missed {want!r} in {text!r}" for text, want in bad.items() if want not in lint(text)]
    fails += [f"missed jargon in {t!r}" for t in ("Vi unlocker dit momentum", "Det er ikke kun klik. Det er kunder.")
              if not any(i.startswith("marketing jargon") for i in lint(t))]
    fails += [f"missed a never-word in {t!r}" for t in ("Det var en dramatisk uge", "Det er krystalklart")
              if not any(i.startswith("a word the author never uses") for i in lint(t))]
    fails += [f"clean text flagged: {lint(good)}"] if lint(good) else []
    src = "samme dag 7 af 11 solgt (64 %), 152 tilbud, cirka 214.000 kr. mere, ændret 58 gange på 12 konti, 9.318 kr."
    for text, want in (("Det er godt 200.000 kr. mere.", []), ("64 % blev solgt.", []), ("Vi har ændret én kundes budget 59 gange.", ["59"]),
                       ("Kl. 14.46 kom svaret i 2026, 5 dage efter.", []), ("Det gav 18 % flere kunder.", ["18 %"]),
                       ("Snittet var 9.318 kr.", [])):
        if unsourced_numbers(text, src) != want:
            fails.append(f"numbers check: {text!r} gave {unsourced_numbers(text, src)}, want {want}")
    week = [{"role": r, "format": f} for r, f in [("personal", "photo"), ("get something", "graphic"), ("problem and fix", "photo"),
                                                   ("offer", "text"), ("personal", "video"), ("personal", "photo"), ("other", "text")]]
    fails += [f"good week flagged: {batch_lint(week)}"] if batch_lint(week) else []
    bad_week = [{"role": "offer", "format": "text"}] * 3 + [{"role": "personal", "format": "graphic"}] * 4
    if len(batch_lint(bad_week)) != 3:
        fails.append(f"bad week: expected 3 issues (no get-something, 3 offers, faces), got {batch_lint(bad_week)}")
    short = [{"format": f} for f in ("photo", "text", "graphic", "text", "video", "carousel", "text")]
    if face_floor(short) != [2, 4, 7] or [p["format"] for p in short] != ["photo", "photo", "graphic", "photo", "video", "carousel", "photo"]:
        fails.append(f"face floor: expected every text post (2, 4, 7) to become a photo post, got {[p['format'] for p in short]}")
    if face_floor([{"format": "graphic"}, {"format": "carousel"}]):
        fails.append("face floor turned a graphic or carousel post into a photo post")
    old = ["To opkald. To nej. Én uge."]
    if not hook_reuse([{"text": "To opkald. To nej. Én uge!\nResten"}], old):
        fails.append("missed a reused hook")
    if not story_reuse([{"text": "36 tilbud. 58 svar. Hos en anden 19 tilbud og 13 svar."}], ["Hos én kunde 36 tilbud, i samme periode 58 svar."]):
        fails.append("missed a story told again with the same numbers")
    if story_reuse([{"text": "I 2026 fik vi 3 nye aftaler og 47.150 kr."}], ["I 2026 skete der 3 ting."]):
        fails.append("a year and a single digit counted as the same story")
    if story_reuse([{"text": "Vi ventede 10 dage. Så kom 12 pakker, og 25 kunder, 20 kr."}],
                   ["12 stole i stuen, 10 i haven. 25 gæster, 20 %."]):
        fails.append("everyday numbers (10, 12, 20, 25) counted as the same story")
    two_vids = [{"format": f, "role": r} for f, r in [("graphic", "personal"), ("photo", "get something"), ("graphic", "problem and fix"),
                ("graphic", "personal"), ("graphic", "problem and fix"), ("video", "personal"), ("video", "personal")]]
    seq = order_posts(two_vids)
    if any(a["format"] == b["format"] == "video" for a, b in zip(seq, seq[1:])):
        fails.append("two videos back to back")
    if hook_reuse([{"text": "Endnu en kunde spurgte til priser i dag.\nResten"}], old):
        fails.append("a new hook on an old topic was flagged")
    batch = [{"format": f, "role": r, "title": str(k)} for k, (f, r) in enumerate(
        [("photo", "personal"), ("photo", "problem and fix"), ("video", "personal"), ("video", "problem and fix"),
         ("graphic", "get something"), ("carousel", "offer"), ("graphic", "offer")])]
    order = order_posts(batch)
    if sorted(x["title"] for x in order) != sorted(x["title"] for x in batch):
        fails.append("the order lost or doubled a post")
    if order[0]["role"] == "offer":
        fails.append("an offer opens the batch")
    if any(a["format"] == b["format"] == "video" for a, b in zip(order, order[1:])):
        fails.append("two videos right after each other")
    if any(a["role"] == b["role"] == "offer" for a, b in zip(order, order[1:])):
        fails.append("two offers right after each other")
    if sum((a["format"] in FACE) == (b["format"] in FACE) for a, b in zip(order, order[1:])) > 1:
        fails.append(f"face and graphic posts don't take turns: {[x['format'] for x in order]}")
    parts = meta_parts({"format": "video", "visual": "", "source": "s", "check": "", "visual_files": [
        {"file": "post7_1.png", "link": "g"}, {"file": "post7_video_cover.png", "link": "c"}]}, "cards")
    if ("to record, card in Video scripts", "cards") not in parts or ("post7_1.png", "g") not in parts:
        fails.append("a video post without its video doesn't link its card and graphic")
    if ("the finished video", "v") not in meta_parts({"format": "video", "source": "s", "video_file": "v"}, "cards"):
        fails.append("a finished video isn't linked")
    if drop_seeds(f"66. a\n67. **{PROPOSED} b\n68. c", [{"seed": 67}]) != "66. a\n68. c":
        fails.append("a seed whose approved post waits for the next batch is still in the sources")
    print("\n".join(fails) or f"selftest ok: {len(bad) + 15} cases")
    sys.exit(1 if fails else 0)


def engine_used() -> list[dict]:
    """Every post the engine already wrote (all engine batches, not dropped): until they are posted they aren't in the
    posts file, so a new batch would otherwise tell the same stories again."""
    out = []
    for bf in sorted(C.BATCHES.glob("*.json")):
        if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", bf.name):
            out += [{"title": p.get("title", ""), "hook": p["text"].strip().splitlines()[0][:140], "source": p.get("source", "")}
                    for p in json.loads(bf.read_text()) if isinstance(p, dict) and p.get("status") != "dropped"]
    return out


READY = KB / "ready_posts.json"


def drop_seeds(seeds_text: str, ready: list[dict]) -> str:
    """Posts the author already approved outside the doc wait in READY for the next batch. They go in first, word for
    word; their seeds leave the sources so the engine doesn't tell the story again."""
    taken = {str(q["seed"]) for q in ready if q.get("seed")}
    return "\n".join(l for l in seeds_text.splitlines() if l.split(".", 1)[0] not in taken)


FACE = {"photo", "video"}


def _order_score(seq: list[dict]) -> int:
    """How well a posting order follows the rules below; a video right after a video costs so much it never wins."""
    total = 0
    for k, p in enumerate(seq):
        prev, prev2, week = (seq[k - 1] if k else None), (seq[k - 2] if k > 1 else None), seq[k // 3 * 3:k]
        if not prev:
            total += (-5 if p["role"] == "offer" else 0) + (1 if p["role"] in ("personal", "problem and fix") else 0)
            continue
        v = 3 if (p["format"] in FACE) != (prev["format"] in FACE) else 0
        v -= 100 if p["format"] == prev["format"] == "video" else 0
        v -= 2 if p["role"] == prev["role"] else 0
        v -= 2 if p["format"] == prev["format"] else 0
        v -= 4 if p["role"] == "offer" and "offer" in (prev["role"], prev2["role"] if prev2 else "") else 0
        total += v - (1 if any(q["role"] == p["role"] for q in week) else 0)
    return total


def order_posts(posts: list[dict]) -> list[dict]:
    """The posting order, with some logic to it and similar images never right after each other. A post with the
    author's face (photo, video) and one without take turns as far as the mix allows, two videos or two offers never
    follow each other, the same kind or format never twice in a row, an offer never opens the batch, and each
    three-post week mixes its kinds. Every order is tried (a greedy pick can leave two videos last, back to back);
    ties keep the writer's order."""
    import itertools
    if len(posts) > 8:  # 9! is still fine, but a big batch keeps the writer's order rather than stall
        return list(posts)
    idx = range(len(posts))
    best = max(itertools.permutations(idx), key=lambda o: (_order_score([posts[k] for k in o]),
                                                          -sum(abs(a - b) for a, b in zip(o, idx))))
    return [posts[k] for k in best]


def reorder_batch(batch_file: pathlib.Path, order: list[int] | None = None) -> list[int]:
    """Put a written batch in a new order (order = old post numbers in their new order; default: order_posts), and
    move everything tied to a post number with it: the image files (on disk and their names in Drive), the graphics
    spec, the photo log, the video cards' headings. Then the tab is rebuilt. Returns the order used."""
    import content_engine_visuals as vis
    sync_doc_edits(batch_file)  # before the numbers move
    posts = json.loads(batch_file.read_text())
    if order is None:
        new = order_posts(posts)
        order = [next(k for k, p in enumerate(posts, 1) if p is q) for q in new]
    assert sorted(order) == list(range(1, len(posts) + 1)), order
    move = {old: new for new, old in enumerate(order, 1) if old != new}
    if not move:
        return order
    ren = lambda name, o: re.sub(rf"^post{o}_", f"post{move[o]}_", name)
    drive, out = vis._google("drive", "v3"), vis.OUT / batch_file.stem
    for o in move:  # files: first to a temporary name, so post 6 -> 7 and 7 -> 6 don't overwrite each other
        for f in out.glob(f"post{o}_*"):
            f.rename(f.with_name("tmp_" + ren(f.name, o)))
        for v in posts[o - 1].get("visual_files", []):
            v["file"] = ren(v["file"], o)
            drive.files().update(fileId=vis.drive_id(v["link"]), body={"name": v["file"]}).execute()
    for f in out.glob("tmp_post*"):
        f.rename(f.with_name(f.name[4:]))
    spec_file = batch_file.with_name(batch_file.stem + "_visuals.json")
    if spec_file.exists():
        spec = json.loads(spec_file.read_text())
        for v in spec["visuals"]:
            v["post"] = move.get(v["post"], v["post"])
        spec["visuals"].sort(key=lambda v: v["post"])
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1))
    import content_engine_photos as ph
    lib = ph._lib()
    for v in lib.get("library", {}).values():
        v["used_in"] = [f"{batch_file.stem} #{move.get(int(u.rsplit('#', 1)[1]), int(u.rsplit('#', 1)[1]))}"
                        if u.startswith(f"{batch_file.stem} #") else u for u in v.get("used_in", [])]
    ph.LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=1))
    name = batch_file.stem.split("_", 1)[1].replace("_", " ")
    tab = next((t["id"] for t in _tabs(DOC_ID) if t["title"] == "Video scripts"), None)
    if tab:  # card headings: "<title> · video for post N in <batch>", via a temporary token
        reqs = [{"replaceAllText": {"containsText": {"text": f"video for post {o} in {name}", "matchCase": True},
                                    "replaceText": f"video for post @{n}@ in {name}", "tabsCriteria": {"tabIds": [tab]}}}
                for o, n in move.items()]
        reqs += [{"replaceAllText": {"containsText": {"text": f"video for post @{n}@ in {name}", "matchCase": True},
                                     "replaceText": f"video for post {n} in {name}", "tabsCriteria": {"tabIds": [tab]}}}
                 for n in move.values()]
        _docs().documents().batchUpdate(documentId=DOC_ID, body={"requests": reqs}).execute()
    batch_file.write_text(json.dumps([posts[o - 1] for o in order], ensure_ascii=False, indent=1))
    rebuild_tab(batch_file, sync=False)  # synced above, before the numbers moved
    return order


def _read(path: pathlib.Path, missing: str = "") -> str:
    return path.read_text() if path.exists() else missing


def draft(sources: str, n: int) -> list[dict]:
    from anthropic import Anthropic
    import voice_bank as vb
    try:  # the dictations again first: the author dictates every day
        vb.refresh()
    except Exception as e:
        print(f"voice bank not refreshed, using the last word list: {e}")
    mine = vb.samples(sources, 15)
    examples, hooks = posted_sections()
    used = engine_used()
    hooks += [u["hook"] for u in used]
    rules = _read(KB / "linkedin_rules.md") or _read(C.PROMPTS / "linkedin_rules.md")
    learned = _read(KB / "voice_rules.md")
    works = _read(KB / "what_works.md")
    icp = _read(ICP)
    recent = _read(KB / "feedback_log.md")[-12000:]
    user = (f"# Facts about the author and the business\n{_read(FACTS, '(missing)')}\n\n"
            + (f"# The author's recent feedback on earlier batches, word for word (newest last; the rules above are taken "
               f"from it, this is for what they missed)\n{recent}\n\n" if recent else "")
            + f"# Who the author wants as clients (internal: write for these readers; never quote its counts, floors or open "
            f"questions in a post)\n{icp or '(missing)'}\n\n"
            f"# House rules for LinkedIn\n{rules}\n\n# Rules learned from the author's feedback\n{learned or '(none yet)'}\n\n"
            f"# What works on the author's LinkedIn: their analytics, then the live numbers per post (DMs count, reach only "
            f"shows what gets read; lean towards what brought DMs; the rules the numbers make are in the rulebook "
            f"above, under 'From the numbers')\n"
            f"{works or '(no data yet)'}\n\n"
            f"# Approved posts, for style (don't copy their wording)\n{examples}\n\n"
            "# How the author actually talks: their own dictated sentences on these topics (borrow their way of saying things "
            "and their words, never facts, names, niches or numbers from them)\n" + "\n".join(f"* {x}" for x in mine) + "\n\n"
            "# Stories the engine already wrote, not posted yet: never write these stories again, pick other ones\n" +
            ("\n".join(f"* {u['title']} ({u['source'][:120]})" for u in used) or "(none)") + "\n\n"
            "# Hooks already posted (write new hooks; the topics may come back)\n" + "\n".join(f"* {h}" for h in hooks) +
            f"\n\n# Sources for this batch\n{sources}\n\n"
            f"# Task\nToday is {dt.date.today().isoformat()}. Write {n} new posts from the sources. Pick the strongest stories with a real moment, a number "
            f"or a strong opinion. What counts is DMs, not likes: at least two posts must be built to start a DM "
            f"(a problem a business owner recognises, and a concrete thing to get by writing to the author). "
            f"Mix formats; suggest video when the story lives from the author's voice or face. "
            f"Mark in 'check' anything the author must confirm (names, numbers, permission to tell the story).")
    client = llm_guard.guarded(Anthropic())
    msg = client.messages.create(model=MODEL, max_tokens=16000, system=SYSTEM,
                                 messages=[{"role": "user", "content": user}],
                                 output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    posts = json.loads(msg.content[0].text)["posts"]
    bad = {i: lint(p["text"]) for i, p in enumerate(posts) if lint(p["text"])}
    for i, why in hook_reuse(posts, hooks).items():
        bad.setdefault(i, []).append(why)
    for i, why in story_reuse(posts, earlier_texts()).items():
        bad.setdefault(i, []).append(why)
    mix = batch_lint(posts)
    if bad or mix:  # one repair pass for the mechanical rules
        fix = "Fix only these rule breaks and return all posts again, unchanged otherwise:\n" + \
              "\n".join(f"post {i + 1}: {', '.join(v)}" for i, v in bad.items()) + \
              ("\nthe batch as a whole: " + "; ".join(mix) if mix else "") + "\n\n" + json.dumps({"posts": posts}, ensure_ascii=False)
        msg = client.messages.create(model=MODEL, max_tokens=16000, system=SYSTEM,
                                     messages=[{"role": "user", "content": fix}],
                                     output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
        posts = json.loads(msg.content[0].text)["posts"]
    flipped = face_floor(posts)
    if flipped:
        print(f"photo posts now (the author's face in at least half): {flipped}")
    for i, h in hook_pass(posts).items():
        print(f"hook: post {i}: {h['old']!r} -> {h['new']!r} ({h['why']})")
    for c in native_check(posts):
        print(f"native Danish: post {c['post']}: {c['phrase']!r} -> {c['replacement']!r} ({c['why']})")
    allowed = set(vb.tokens(sources + _read(FACTS)))
    for c in vocab_fix(posts, allowed):
        print(f"own words: post {c['post']}: {c['phrase']!r} -> {c['replacement']!r} ({c['why']})")
    facts = sources + _read(FACTS)
    for i, p in enumerate(posts, 1):
        bad = flag_numbers(p, facts)
        if bad:
            print(f"post {i}: numbers not in the sources, on its Check line: {', '.join(bad)}")
    for i, p in enumerate(posts, 1):
        if p.get("never_said"):
            print(f"post {i}: still words the author never says, to fix in the doc: {', '.join(p['never_said'])}")
    return order_posts(posts)


HOOK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"], "properties": {"posts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["post", "options", "best", "why"],
    "properties": {"post": {"type": "integer"},
                   "options": {"type": "array", "items": {"type": "string"}, "description": "option 1 is the current opening, verbatim; then 4 new ones"},
                   "best": {"type": "integer", "description": "1-based index into options"},
                   "why": {"type": "string", "description": "in English, a few words"}}}}}}
HOOK = f"""You sharpen the opening of Danish LinkedIn posts by {NAME}.
On LinkedIn only the opening shows before "...se mere": about 200 characters, or 3 short lines. Its only job is to make
the right reader click "se mere". For each post you get its opening (everything before the first blank line) and the post.
Write 4 new openings in the author's voice, each built only from facts already in the post (never a new fact, number or
name): a surprising number, a moment mid-scene, a line the reader disagrees with, or a question they can't answer
straight away. Each at most 200 characters, no long dashes, no emojis, no hashtags, never clickbait, never the post's
point given away. Then pick the strongest of the 5 (option 1 = the current opening, verbatim): the one a reader
scrolling fast would most need to finish. Keep option 1 when it is as strong; changing a good opening has a cost. The
new opening must still lead into the next line of the post naturally."""


def hook_pass(posts: list[dict]) -> dict[int, dict]:
    """Four new openings per post from its own facts, the strongest of five kept; the options stay on the post
    ('hook_options') so the author can pick another in the doc. Applies in place, returns {post: {old, new, why}} for
    the changed ones."""
    from anthropic import Anthropic
    ask = "\n\n".join(f"## Post {i}\nOpening:\n{p['text'].strip().split(chr(10) * 2)[0]}\n\nWhole post:\n{p['text']}"
                       for i, p in enumerate(posts, 1))
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=MODEL, max_tokens=6000, system=HOOK, messages=[{"role": "user", "content": ask}],
        output_config={"format": {"type": "json_schema", "schema": HOOK_SCHEMA}})
    out = {}
    for h in json.loads(msg.content[0].text)["posts"]:
        if not (1 <= h["post"] <= len(posts)) or not (1 <= h["best"] <= len(h["options"])):
            continue
        p = posts[h["post"] - 1]
        old = p["text"].strip().split("\n\n")[0]
        p["hook_options"] = h["options"]
        p["hook_why"] = h["why"]  # shown in the grey block: why this hook
        new = re.sub("[" + DASHES + "]", "-", h["options"][h["best"] - 1]).strip()
        if h["best"] != 1 and new and new != old and len(new) <= 220 and not lint(new):
            p["text"] = p["text"].replace(old, new, 1)
            out[h["post"]] = {"old": old, "new": new, "why": h["why"]}
    return out


NATIVE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["changes"], "properties": {"changes": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["post", "phrase", "replacement", "why"],
    "properties": {"post": {"type": "integer"}, "phrase": {"type": "string", "description": "exactly as it appears in the post"},
                   "replacement": {"type": "string"}, "why": {"type": "string", "description": "in English, a few words"}}}}}}
_OWN = ", ".join(C.get("author", "own_words")) or "sgu, satme"
NATIVE = f"""You are a native Danish reader who runs a small business. You read LinkedIn posts written in Danish for
{NAME}. List every word or phrase a Dane would never write in a casual LinkedIn post: translated English, invented or
stiff words, wrong idioms, wrong gender or inflection, business words owners don't use (like "arbitrage"). Give the
phrase exactly as it appears and a natural replacement in the same casual style. Leave the author's own words alone
({_OWN}), and names, numbers and anything that already reads as natural Danish. An empty list is the normal answer for
good Danish."""


def native_check(posts: list[dict]) -> list[dict]:
    """A second read by another model: which words would a Dane never use? Applies the swaps in place and returns
    them, so every change is visible in the batch file."""
    from anthropic import Anthropic
    numbered = "\n\n".join(f"## Post {i}\n{p['text']}" for i, p in enumerate(posts, 1))
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=MODEL, max_tokens=4000, system=NATIVE, messages=[{"role": "user", "content": numbered}],
        output_config={"format": {"type": "json_schema", "schema": NATIVE_SCHEMA}})
    done = []
    for c in json.loads(msg.content[0].text)["changes"]:
        if 1 <= c["post"] <= len(posts) and c["phrase"] and c["phrase"] in posts[c["post"] - 1]["text"]:
            p = posts[c["post"] - 1]
            p["text"] = p["text"].replace(c["phrase"], c["replacement"], 1)
            p.setdefault("native_check", []).append(c)
            done.append(c)
    return done


VOCAB = f"""{NAME} speaks casual Danish. These LinkedIn drafts in their name use words they have never said: not once in
their own spoken words (their dictations and their meetings). For each listed word, give the phrase it sits in, exactly
as it appears in the post, and a replacement in words they do say: the plain everyday word, their casual register, the
same meaning, the sentence still correct Danish. Change nothing else. A word that is the story's own subject (a thing,
a place, a dish) may stay: leave it out."""


def vocab_fix(posts: list[dict], allowed: set[str] = frozenset()) -> list[dict]:
    """Every word checked against the words the author has actually said (voice_bank.py: dictations and their meeting
    lines, names and the story's own words left alone); one they never say is swapped for one they do, in two rounds.
    What is left stays for the author to fix in the doc and is listed in the post's 'never_said'. Applies in place and
    returns the swaps."""
    import voice_bank as vb
    from anthropic import Anthropic
    done = []
    for _ in range(2):
        flagged = {i: vb.unknown(p["text"], allowed) for i, p in enumerate(posts, 1)}
        flagged = {i: w for i, w in flagged.items() if w}
        if not flagged:
            break
        ask = "\n\n".join(f"## Post {i}\nWords they never say: {', '.join(w)}\n\n{posts[i - 1]['text']}" for i, w in flagged.items())
        msg = llm_guard.guarded(Anthropic()).messages.create(
            model=MODEL, max_tokens=4000, system=VOCAB, messages=[{"role": "user", "content": ask}],
            output_config={"format": {"type": "json_schema", "schema": NATIVE_SCHEMA}})
        for c in json.loads(msg.content[0].text)["changes"]:
            if 1 <= c["post"] <= len(posts) and c["phrase"] and c["phrase"] in posts[c["post"] - 1]["text"]:
                p = posts[c["post"] - 1]
                p["text"] = p["text"].replace(c["phrase"], re.sub("[" + DASHES + "]", "-", c["replacement"]), 1)
                p.setdefault("own_words", []).append(c)
                done.append(c)
    for p in posts:
        left = vb.unknown(p["text"], allowed)
        if left:
            p["never_said"] = left
        else:
            p.pop("never_said", None)
    return done


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _docs():
    import google_api
    C.need("google", "doc_id")
    return google_api.service("docs", "v1")


def cards_url() -> str:
    """The doc's Video scripts tab, where every video post has its card."""
    tab = next((t["id"] for t in _tabs(DOC_ID) if t["title"] == "Video scripts"), None)
    return f"https://docs.google.com/document/d/{DOC_ID}/edit" + (f"?tab={tab}" if tab else "")


BR = "\u000b"  # a line break inside one paragraph: every label on its own line, still one grey block for the readers
META_LABEL = re.compile("(?:^|" + BR + ")((?:Format|Timing|Hook|Other hooks|Reader gets|Goal|Check|Visual|Source|Graphic|Photo|Video):)")


def meta_parts(p: dict, cards: str) -> list[tuple[str, str | None]]:
    """The grey block under a post's heading as (text, link) pieces, one label per line. The graphic, the author's
    photo and the finished video are linked; a video not recorded yet links to its card."""
    lines = [f"Format: {p['format']}"]
    hook = (p.get("text") or "").strip().split("\n\n")[0].replace("\n", " ")
    if hook and p.get("hook_why"):  # the hook, why it stops the right reader, and the other options
        lines.append(f"Hook: {hook} ({p['hook_why'].rstrip('.')}).")
        others = [o for o in p.get("hook_options") or [] if o.strip() and o.strip() != hook]
        if others:
            lines.append("Other hooks: " + " / ".join(o.replace("\n", " ") for o in others[:4]))
    if p.get("timing"):
        lines.append("Timing: " + (f"time-bound, post by {p['post_by']}" if p["timing"] == "now" and p.get("post_by") else
                                   "time-bound" if p["timing"] == "now" else "any time")
                     + (f" ({p['timing_why'].rstrip('.')})." if p.get("timing_why") else "."))
    lines += [f"{k}: {p[f]}" for k, f in (("Reader gets", "reader_gets"), ("Goal", "goal"), ("Check", "check"), ("Visual", "visual"),
                                          ("Source", "source")) if p.get(f)]
    parts = [(BR.join(lines), None)]
    files = p.get("visual_files", [])
    graphic = next((f for f in files if f["file"].endswith(".pdf")), next((f for f in files if re.fullmatch(r"post\d+_\d+\.png", f["file"])), None))
    if graphic:
        parts += [(BR + "Graphic: ", None), ("carousel PDF + slides" if graphic["file"].endswith(".pdf") else graphic["file"], graphic["link"])]
    photo = next((f for f in files if f["file"].endswith("_photo.jpg")), None)
    if photo:
        parts += [(BR + "Photo: ", None), ("your photo", photo["link"])]
    if p.get("video_file"):
        parts += [(BR + "Video: ", None), ("the finished video", p["video_file"])]
    elif p.get("format") == "video":
        parts += [(BR + "Video: ", None), ("to record, card in Video scripts", cards)]
    return parts


HOOK_WHY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"], "properties": {"posts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["post", "why"],
    "properties": {"post": {"type": "integer"}, "why": {"type": "string", "description": "in English, one short sentence"}}}}}}


def hook_notes(batch_file: pathlib.Path, numbers: list[int] | None = None) -> int:
    """Why each post's current opening makes the right reader stop and click "se mere", into 'hook_why' and the grey
    block. numbers: only these posts (their hook changed); default every post without a reason."""
    from anthropic import Anthropic
    posts = json.loads(batch_file.read_text())
    todo = [i for i, p in enumerate(posts, 1) if (i in numbers if numbers else not p.get("hook_why"))
            and p.get("status") != "dropped" and not p.get("posted")]
    if not todo:
        return 0
    ask = "\n\n".join(f"## Post {i}\nOpening:\n{posts[i - 1]['text'].strip().split(chr(10) * 2)[0]}\n\nWhole post:\n{posts[i - 1]['text']}"
                       for i in todo)
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=MODEL, max_tokens=2000, system=HOOK + "\n\nNow only explain: for each post, why its current opening makes "
        "the right reader stop scrolling and click \"se mere\". One short sentence each, concrete, no praise.",
        messages=[{"role": "user", "content": ask}], output_config={"format": {"type": "json_schema", "schema": HOOK_WHY_SCHEMA}})
    for h in json.loads(msg.content[0].text)["posts"]:
        if h["post"] in todo:
            posts[h["post"] - 1]["hook_why"] = h["why"].strip()
    batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    return refresh_meta(batch_file, todo)


NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?(?:\s*(%|procent|kr\.?|kroner))?")
ROUGH = re.compile(r"(?i)\b(ca\.?|cirka|godt|knap|omkring|næsten|over|under|mere end|mindre end|op til)\s*$")
NUMBER_NOTE = "Numbers not found in the source:"


def _nums(text: str) -> list[tuple[float, str, bool]]:
    """(value, as written, rounded) for every number worth checking: not a time (kl. 14.46), a year, or a bare 1-9."""
    out = []
    for m in NUM.finditer(text):
        raw, before = m.group(0).strip(), text[max(0, m.start() - 14):m.start()]
        if re.search(r"(?i)kl\.?\s*$", before) or re.fullmatch(r"\d{1,2}\.\d{2}", m.group(1)):
            continue
        v = float(m.group(1).replace(".", "") + ("." + m.group(2) if m.group(2) else ""))
        if (1990 <= v <= 2035 and not m.group(3) and not m.group(2)) or (v < 10 and not m.group(3)):
            continue
        out.append((v, raw, bool(ROUGH.search(before))))
    return out


def unsourced_numbers(text: str, source: str) -> list[str]:
    """Every number in a post must stand in its source. A rounded one ("ca. 300.000") may be up to 10 % off a source
    number."""
    have = [v for v, _, _ in _nums(source)]
    bad = []
    for v, raw, rough in _nums(text):
        if not any(abs(v - h) <= (0.1 * h if rough else 1e-9) for h in have) and raw not in bad:
            bad.append(raw)
    return bad


def flag_numbers(p: dict, source: str) -> list[str]:
    """Put the unsourced numbers on the post's Check line (replacing an older note), so they are seen before approving."""
    bad = unsourced_numbers(p.get("text", ""), source)
    check = re.sub(re.escape(NUMBER_NOTE) + r"[^.]*\. Check them\.\s*", "", p.get("check", "")).strip()
    p["check"] = (check + (f" {NUMBER_NOTE} {', '.join(bad)}. Check them." if bad else "")).strip()
    return bad


def seed_text(p: dict) -> str:
    """The story seed a post was written from (its source names it), for the numbers check."""
    m = re.search(r"Seed (\d+)", p.get("source", ""))
    if not m:
        return ""
    s = re.search(rf"(?m)^{m.group(1)}\. .*$", _read(KB / "story_seeds.md"))
    return s.group(0) if s else ""


TIMING_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"], "properties": {"posts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["key", "timing", "post_by", "why"],
    "properties": {"key": {"type": "string"}, "timing": SCHEMA["properties"]["posts"]["items"]["properties"]["timing"],
                   "post_by": SCHEMA["properties"]["posts"]["items"]["properties"]["post_by"], "why": {"type": "string"}}}}}}


def _bkey(bf: pathlib.Path, i: int) -> str:
    return f"B{re.search(r'Batch_(\d+)', bf.name).group(1)} #{i}"  # two batches can share a date, never a number


def label_timing(batch_files: list[pathlib.Path], calendar: str = "") -> int:
    """Time-bound or any time, for every post not posted, dropped or labelled yet."""
    from anthropic import Anthropic
    todo = []
    for bf in batch_files:
        for i, p in enumerate(json.loads(bf.read_text()), 1):
            if not p.get("timing") and p.get("status") != "dropped" and not (p.get("posted") and not p["posted"].get("deleted")):
                todo.append((bf, i, p))
    if not todo:
        return 0
    ask = "\n\n".join(f"## {_bkey(bf, i)} (written {bf.stem[:10]})\nSource: {p.get('source', '')[:300]}\n{p['text'][:1500]}"
                       for bf, i, p in todo)
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=MODEL, max_tokens=8000,
        system=(f"Today is {dt.date.today().isoformat()}. For each LinkedIn post say whether it is time-bound ('now': about "
                "something happening now, a dated event, words like 'i søndags' or 'i denne uge', or a season, so it must go "
                "out before a last date) or 'anytime' (still true and fresh in two months). For 'now' give the last date it "
                "still makes sense (YYYY-MM-DD), meaning when it would read as stale or wrong to a reader, not how soon it "
                "would ideally go; a relative day word ('i søndags') alone is a small rewrite, so give it two weeks from the "
                "event. The key is the '## ' line up to the space before '('." + (f" Posting calendar: {calendar}" if calendar else "")),
        messages=[{"role": "user", "content": ask}], output_config={"format": {"type": "json_schema", "schema": TIMING_SCHEMA}})
    got = {x["key"].strip(): x for x in json.loads(msg.content[0].text)["posts"]}
    n = 0
    for bf in batch_files:
        posts, changed = json.loads(bf.read_text()), False
        for i, p in enumerate(posts, 1):
            x = got.get(_bkey(bf, i))
            if x and not p.get("timing"):
                p.update({"timing": x["timing"], "post_by": x["post_by"] if x["timing"] == "now" else "", "timing_why": x["why"]})
                changed, n = True, n + 1
        if changed:
            bf.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    return n


def label_spans(line: str, at: int) -> list[tuple[int, int]]:
    """Doc ranges of the labels in a grey block that starts at index `at`, to set them in bold."""
    return [(at + _u16(line[:m.start(1)]), at + _u16(line[:m.end(1)])) for m in META_LABEL.finditer(line)]


def refresh_meta(batch_file: pathlib.Path, numbers: list[int] | None = None) -> int:
    """Rewrite the grey line under these posts' headings from the batch file (Check, graphic, photo and video links),
    nothing else in the tab: for a video or photo that arrives after the batch was written."""
    posts = json.loads(batch_file.read_text())
    name, docs, cards = batch_file.stem.split("_", 1)[1].replace("_", " "), _docs(), cards_url()
    doc = docs.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()
    tab = next((t for t in doc["tabs"] if t["tabProperties"]["title"] == name), None)
    if not tab:
        return 0
    tab_id = tab["tabProperties"]["tabId"]
    paras = [el for el in tab["documentTab"]["body"]["content"] if "paragraph" in el]
    text = lambda el: "".join(r.get("textRun", {}).get("content", "") for r in el["paragraph"].get("elements", []))
    found = []
    for k, el in enumerate(paras[:-1]):
        m = re.match(r"^(?:\S+ )?(\d+) · ", text(el)) if el["paragraph"].get("paragraphStyle", {}).get("namedStyleType") == "HEADING_1" else None
        if m and (numbers is None or int(m.group(1)) in numbers) and int(m.group(1)) <= len(posts) and text(paras[k + 1]).startswith("Format:"):
            found.append((int(m.group(1)), paras[k + 1]))
    grey = {"color": {"rgbColor": {"red": 0.42, "green": 0.47, "blue": 0.57}}}
    for i, el in sorted(found, key=lambda f: -f[1]["startIndex"]):
        parts = meta_parts(posts[i - 1], cards)
        s, line = el["startIndex"], "".join(t for t, _ in parts)
        reqs = [{"deleteContentRange": {"range": {"startIndex": s, "endIndex": el["endIndex"] - 1, "tabId": tab_id}}},
                {"insertText": {"location": {"index": s, "tabId": tab_id}, "text": line}},
                {"updateTextStyle": {"range": {"startIndex": s, "endIndex": s + _u16(line), "tabId": tab_id},
                                     "textStyle": {"italic": True, "foregroundColor": grey}, "fields": "italic,foregroundColor,link,bold"}}]
        reqs += [{"updateTextStyle": {"range": {"startIndex": a, "endIndex": b, "tabId": tab_id}, "textStyle": {"bold": True},
                                      "fields": "bold"}} for a, b in label_spans(line, s)]
        pos = s
        for t, url in parts:
            if url:
                reqs.append({"updateTextStyle": {"range": {"startIndex": pos, "endIndex": pos + _u16(t), "tabId": tab_id},
                                                 "textStyle": {"link": {"url": url}}, "fields": "link"}})
            pos += _u16(t)
        docs.documents().batchUpdate(documentId=DOC_ID, body={"requests": reqs}).execute()
    return len(found)


def fill_tab(docs, tab: str, posts: list[dict], header: str) -> None:
    """Write a whole batch into an empty tab: per post a heading (✅/✏️/❌ by status), the grey meta line (with the
    Graphic link once there are images), the text (a changed post shows old lines red and new lines green) and one
    "[[IMG name]]" marker per image, which content_engine_visuals.into_doc swaps for the image itself."""
    import content_engine_feedback as fb
    text, heads, notes, links, spans, pos, cards, bold = "", [], [], [], [], 1, cards_url(), []

    def add(s, kind=None):
        nonlocal text, pos
        start = pos
        text += s
        pos += _u16(s)
        if kind == "head":
            heads.append((start, pos))
        elif kind == "note":
            notes.append((start, pos - 1))
        return start
    add(header + "\n", "note")
    for i, p in enumerate(posts, 1):
        mark = {"approved": "✅ ", "dropped": "❌ ", "changed": "✏️ "}.get(p.get("status"), "")
        add(f"{mark}{i} · {heading(p)}\n", "head")
        files = p.get("visual_files", [])
        s = pos
        bold += label_spans("".join(t for t, _ in meta_parts(p, cards)), s)
        for t, url in meta_parts(p, cards):
            at = add(t)
            if url:
                links.append((at, pos, url))
        add("\n")
        notes.append((s, pos - 1))
        add("\n")  # a blank line between the grey block and the post's first line
        old = next((h["old_text"] for h in reversed(p.get("history", [])) if h.get("old_text")), None)
        if p.get("status") == "changed" and old:
            body, marks = fb.marked_body(old, p["text"])
        else:
            body, marks = p["text"].strip() + "\n", []
        b0 = add(body)
        spans += [(b0 + s0, b0 + s1, kind) for s0, s1, kind in marks]
        add("\n")
        for f in files:
            if f["file"].endswith((".png", ".jpg")) and f.get("in_doc", True):  # a stand-in graphic replaced by a photo stays out
                add(f"[[IMG {f['file'][:-4]}]]\n")
    reqs = [{"insertText": {"location": {"index": 1, "tabId": tab}, "text": text}},
            # a cleared tab can keep a heading or a colour on its last paragraph: start from plain text everywhere
            {"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": pos, "tabId": tab},
                                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
            {"updateTextStyle": {"range": {"startIndex": 1, "endIndex": pos, "tabId": tab}, "textStyle": {},
                                 "fields": "italic,bold,strikethrough,foregroundColor,link"}}]
    # every post starts on its own page
    reqs += [{"updateParagraphStyle": {"range": {"startIndex": s, "endIndex": e, "tabId": tab},
                                       "paragraphStyle": {"namedStyleType": "HEADING_1", "pageBreakBefore": True},
                                       "fields": "namedStyleType,pageBreakBefore"}}
             for s, e in heads]
    reqs += [{"updateTextStyle": {"range": {"startIndex": a, "endIndex": b, "tabId": tab}, "textStyle": {"bold": True},
                                  "fields": "bold"}} for a, b in bold]
    reqs += [{"updateTextStyle": {"range": {"startIndex": s, "endIndex": e, "tabId": tab},
                                  "textStyle": {"italic": True, "foregroundColor": {"color": {"rgbColor": {"red": 0.42, "green": 0.47, "blue": 0.57}}}},
                                  "fields": "italic,foregroundColor"}} for s, e in notes if e > s]
    reqs += [{"updateTextStyle": {"range": {"startIndex": s, "endIndex": e, "tabId": tab}, "textStyle": {"link": {"url": url}},
                                  "fields": "link"}} for s, e, url in links]
    for s, e, kind in spans:
        style = {"foregroundColor": {"color": {"rgbColor": fb.RED if kind == "red" else fb.GREEN}}}
        if kind == "red":
            style["strikethrough"] = True
        reqs.append({"updateTextStyle": {"range": {"startIndex": s, "endIndex": e, "tabId": tab}, "textStyle": style,
                                         "fields": "foregroundColor,strikethrough"}})
    docs.documents().batchUpdate(documentId=DOC_ID, body={"requests": reqs}).execute()


def heading(p: dict) -> str:
    """'Danish title / English title'."""
    return f"{p['title_da']} / {p['title']}" if p.get("title_da") else p["title"]


def write_tab(posts: list[dict], title: str, header: str) -> str:
    docs = _docs()
    res = docs.documents().batchUpdate(documentId=DOC_ID, body={"requests": [
        {"addDocumentTab": {"tabProperties": {"title": title, "index": 0}}}]}).execute()
    tab = res["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    fill_tab(docs, tab, posts, header)
    return f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab}"


EN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posts"], "properties": {"posts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["post", "text"],
    "properties": {"post": {"type": "integer"}, "text": {"type": "string"}}}}}}


def english_tab(batch_file: pathlib.Path) -> str | None:
    """An English copy of a batch tab, next to the Danish one, for a reviewer who doesn't read Danish ([drafting]
    english_tab = true). Same headings, grey block and order; the text is a faithful translation, redone only for
    posts whose Danish text changed since (kept as text_en with the Danish text it was made from). Read-only for the
    engine: edits made in the English tab are not read back. Returns the tab link."""
    from anthropic import Anthropic
    posts = json.loads(batch_file.read_text())
    stale = [(i, p) for i, p in enumerate(posts, 1) if p.get("text_en_from") != p["text"]]
    if stale:
        ask = "\n\n".join(f"## Post {i}\n{p['text']}" for i, p in stale)
        msg = llm_guard.guarded(Anthropic()).messages.create(
            model=MODEL, max_tokens=12000, output_config={"format": {"type": "json_schema", "schema": EN_SCHEMA}},
            system=f"Translate Danish LinkedIn posts by {NAME} into natural English for an English-speaking reviewer. "
                   "Keep every line break, number, kroner amount (as 'kr.'), name and emoji exactly; keep the "
                   "casual first-person voice; add nothing and leave nothing out. Quoted Danish phrases keep the Danish with "
                   "the English in brackets after it.",
            messages=[{"role": "user", "content": ask}])
        got = {x["post"]: x["text"] for x in json.loads(msg.content[0].text)["posts"]}
        for i, p in stale:
            if got.get(i):
                p["text_en"], p["text_en_from"] = got[i], p["text"]
        batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    name = batch_file.stem.split("_", 1)[1].replace("_", " ")
    en_name = f"{name} (EN)"
    copy = [{**{k: v for k, v in p.items() if k not in ("title_da", "history")}, "text": p.get("text_en") or p["text"]} for p in posts]
    docs, tabs = _docs(), _tabs(DOC_ID)
    tab = next((t["id"] for t in tabs if t["title"] == en_name), None)
    if tab:
        doc = docs.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()
        end = next(t for t in doc["tabs"] if t["tabProperties"]["tabId"] == tab)["documentTab"]["body"]["content"][-1]["endIndex"]
        if end > 2:
            docs.documents().batchUpdate(documentId=DOC_ID, body={"requests": [
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1, "tabId": tab}}}]}).execute()
    else:
        da_index = next((k for k, t in enumerate(tabs) if t["title"] == name), 0)
        res = docs.documents().batchUpdate(documentId=DOC_ID, body={"requests": [
            {"addDocumentTab": {"tabProperties": {"title": en_name, "index": da_index + 1}}}]}).execute()
        tab = res["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    fill_tab(docs, tab, copy, f"{name} in English, for review · translated from the Danish tab {dt.date.today():%d %b %Y}. "
                              "The Danish tab is the one that gets posted; edits here are not read.")
    spec = batch_file.with_name(batch_file.stem + "_visuals.json")
    if spec.exists():  # the same images as the Danish tab
        import content_engine_visuals as vis
        vis.into_doc(spec, tab_suffix=" (EN)")
    return f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab}"


def english_tab_safe(batch_file: pathlib.Path) -> None:
    if not C.get("drafting", "english_tab"):
        return
    try:
        english_tab(batch_file)
    except Exception as e:  # the English copy never blocks the Danish work
        print(f"English tab not updated: {type(e).__name__}: {e}")


def sync_doc_edits(batch_file: pathlib.Path) -> list[int]:
    """The author's hand edits in the doc into the batch file before anything rewrites the tab, so a rebuild never
    writes over them. Kept as their version with a history line, same as the hourly feedback run does."""
    import content_engine_feedback as fb
    posts = json.loads(batch_file.read_text())
    name = batch_file.stem.split("_", 1)[1].replace("_", " ")
    edits = fb.doc_edits(name, posts)
    for i, t in edits:
        p = posts[i - 1]
        p.setdefault("history", []).append({"when": dt.date.today().isoformat(), "via": "edit in the doc", "cause": "edit",
                                            "decision": "edit", "instruction": "", "old_text": p["text"]})
        p["text"] = t
    if edits:
        batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    return [i for i, _ in edits]


def rebuild_tab(batch_file: pathlib.Path, sync: bool = True) -> str:
    """Rewrite an existing batch tab from its batch file (same tab, so every link to it keeps working). The author's
    edits in the doc are taken into the batch file first, unless the caller already did and the doc no longer matches
    the file's order (reorder_batch: a second sync after a reorder would read every moved post as a hand edit)."""
    if sync:
        sync_doc_edits(batch_file)
    posts = json.loads(batch_file.read_text())
    name = batch_file.stem.split("_", 1)[1].replace("_", " ")
    docs = _docs()
    doc = docs.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()
    tab = next(t for t in doc["tabs"] if t["tabProperties"]["title"] == name)
    tab_id = tab["tabProperties"]["tabId"]
    content = tab["documentTab"]["body"]["content"]
    header = "".join(r.get("textRun", {}).get("content", "") for r in (content[1]["paragraph"]["elements"] if len(content) > 1 and "paragraph" in content[1] else [])).rstrip("\n")
    end = content[-1]["endIndex"]
    if end > 2:
        docs.documents().batchUpdate(documentId=DOC_ID, body={"requests": [
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1, "tabId": tab_id}}}]}).execute()
    fill_tab(docs, tab_id, posts, header or f"{name} · rebuilt {dt.date.today():%d %b %Y} by the content engine")
    english_tab_safe(batch_file)
    spec = batch_file.with_name(batch_file.stem + "_visuals.json")
    if spec.exists():  # fill_tab leaves [[IMG]] markers; without this a rebuild strips every image
        import content_engine_visuals as vis
        vis.into_doc(spec)
    return f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab_id}"


CALL_MIN, BATCH_GAP = 8 * 60, 4  # a call long enough to carry stories; days since the last batch


def auto_batch(check: bool = False) -> str:
    """The weekly call with the interviewer starts the next batch by itself: a finished call of CALL_MIN or more,
    never used for a batch before, at least BATCH_GAP days after the newest batch, becomes Engine Batch N+1 (posts
    from the call and the story seeds, same pipeline as by hand), then the done-note. The call is marked as already
    read for the new batch, so the hourly feedback run doesn't treat its stories as feedback on the posts made from
    them. check: say what would happen, change nothing."""
    import subprocess
    import content_engine_interviewer as iv
    folder = C.BATCHES
    used_file = folder / "auto_batches.json"
    used = json.loads(used_file.read_text()) if used_file.exists() else {"calls": []}
    if not iv._state().get("agent_id"):
        return "no interviewer agent yet (content_engine_interviewer.py create)"
    convs = iv._call("GET", f"/v1/convai/conversations?agent_id={iv._state()['agent_id']}&page_size=5").get("conversations", [])
    fresh = [c for c in convs if c["conversation_id"] not in used["calls"] and c.get("status") == "done"
             and (c.get("call_duration_secs") or 0) >= CALL_MIN]
    if not fresh:
        return "no new interviewer call of 8 minutes or more"
    call = fresh[0]
    day = dt.datetime.fromtimestamp(call.get("start_time_unix_secs") or 0).date()
    batches = [f for f in folder.glob("*.json") if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_(\d+)\.json", f.name)]
    last = max(batches, key=lambda f: int(re.search(r"Batch_(\d+)", f.name).group(1)), default=None)
    number = int(re.search(r"Batch_(\d+)", last.name).group(1)) + 1 if last else 1
    if last and (day - dt.date.fromisoformat(last.name[:10])).days < BATCH_GAP:
        if not check:  # a second call in the same week: feedback on the current batch, and its stories wait for the next
            used["calls"].append(call["conversation_id"])
            used.setdefault("carry", []).append(call["conversation_id"])
            used_file.write_text(json.dumps(used, indent=1))
        return f"call {day} is under {BATCH_GAP} days after {last.stem}: feedback now, its stories go into the next batch"
    tab = f"Engine Batch {number}"
    if check:
        return f"would write {tab} from the call on {day} ({call.get('call_duration_secs')} s)"
    calls = used.pop("carry", []) + [call["conversation_id"]]
    used["calls"].append(call["conversation_id"])
    used_file.write_text(json.dumps(used, indent=1))  # first, so a crash halfway never makes the batch twice
    subprocess.run([sys.executable, str(ROOT / "scripts" / "content_engine_draft.py"), "--interview", ",".join(calls),
                    "--seeds", str(KB / "story_seeds.md"), "--n", str(C.get("drafting", "batch_size")), "--tab", tab],
                   check=True, cwd=ROOT, timeout=2400)
    made = folder / f"{dt.date.today()}_{tab.replace(' ', '_')}.json"
    made.with_name(made.stem + ".feedback.json").write_text(json.dumps(
        {"seen": calls, "log": [{"when": dt.datetime.now().isoformat(timespec="seconds"), "via": "auto batch",
                                 "summary": f"written from the call on {day}"}]}, indent=1))
    return f"{tab} written from the call on {day}; " + done_note(made)


def done_note(batch_file: pathlib.Path) -> str:
    """The done-note, placed straight in the author's own inbox as unread (Gmail insert, not send: nothing leaves the
    mailbox and nobody else gets it). Once per batch, five lines."""
    import content_engine_feedback as fb
    import gmail_util
    fb.refresh_interviewer()  # the new batch's posts are in the next call's brief
    english_tab_safe(batch_file)
    marker = batch_file.with_suffix(".done")
    if marker.exists():
        return f"already noted {marker.read_text().strip()}"
    posts = json.loads(batch_file.read_text())
    name = batch_file.stem.split("_", 1)[1].replace("_", " ")
    tab = next((t for t in _tabs(DOC_ID) if t["title"] == name), None)
    link = f"https://docs.google.com/document/d/{DOC_ID}/edit" + (f"?tab={tab['id']}" if tab else "")
    videos = sum(p["format"] == "video" for p in posts)
    images = sum(bool(p.get("visual_files")) for p in posts)
    lines = [f"{name} is in your doc: {len(posts)} posts" + (f", {videos} suggested as video" if videos else "")
             + (f", {images} with images" if images else "") + ".", link,
             'Each post has a grey "Check" line with what you need to confirm.'] + \
            ([f"Videos to record: post {', '.join(str(i) for i, p in enumerate(posts, 1) if p['format'] == 'video' and not p.get('video_file'))}"
              f", each with a card (start line, 3 points, end line): {cards_url()}"]
             if any(p["format"] == "video" and not p.get("video_file") for p in posts) else []) + [
             'Reply here ("1, 2 og 5 er gode, 3 kortere, drop 4") or go through them on your next call. The engine does the edits.']
    subject = f"{name} is ready: {len(posts)} posts"
    mid = gmail_util.inbox(subject, lines)
    marker.write_text(f"{dt.datetime.now():%Y-%m-%d %H:%M} message {mid}\n")
    return f"in your inbox: {subject}"


def _tabs(doc_id: str) -> list[dict]:
    doc = _docs().documents().get(documentId=doc_id, includeTabsContent=False, fields="tabs(tabProperties)").execute()
    return [{"title": t["tabProperties"]["title"], "id": t["tabProperties"]["tabId"]} for t in doc.get("tabs", [])]


# Every doc write under one lock (content_engine_lock.py: two jobs editing a tab at once can break it)
from content_engine_lock import locked as _locked_doc  # noqa: E402
reorder_batch = _locked_doc(reorder_batch)
refresh_meta = _locked_doc(refresh_meta)
write_tab = _locked_doc(write_tab)
english_tab = _locked_doc(english_tab)
rebuild_tab = _locked_doc(rebuild_tab)
sync_doc_edits = _locked_doc(sync_doc_edits)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--done", metavar="BATCH_JSON", help="put the done-note for a finished batch in your inbox (once)")
    ap.add_argument("--auto-batch", action="store_true", help="hourly: a new interviewer call starts the next batch")
    ap.add_argument("--check", action="store_true", help="with --auto-batch: say what would happen, change nothing")
    ap.add_argument("--call", "--meetgeek", dest="calls", action="append", default=[],
                    help="DATE:TITLE of a call transcript from any note taker, e.g. 2026-10-26:Content call")
    ap.add_argument("--interview", help="'latest' or an ElevenLabs conversation id (several: comma-separated)")
    ap.add_argument("--seeds", help="story seeds markdown file")
    ap.add_argument("--n", type=int, default=C.get("drafting", "batch_size"))
    ap.add_argument("--tab", default=f"Engine batch {dt.date.today():%d %b}")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="check the mechanical rules and exit")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    C.ensure_dirs()
    if a.done:
        print(done_note(pathlib.Path(a.done)))
        sys.exit(0)
    if a.auto_batch:
        print(auto_batch(a.check))
        sys.exit(0)
    queue = json.loads(READY.read_text()) if READY.exists() else []
    ready = [q for q in queue if not q.get("batch")][:a.n]
    parts = [call_text(d) for d in a.calls]
    if a.interview:
        parts.append(interview_text(a.interview))
    if a.seeds:
        seeds_text = drop_seeds(pathlib.Path(a.seeds).read_text(), queue)  # queued once = told, also after its batch
        # a seed the author proposed is worth more than anything the engine spotted: tagged in story_seeds.md, they lead
        # the sources and get a post first
        mine = [l for l in seeds_text.splitlines() if re.match(rf"^\d+\. \*\*{re.escape(PROPOSED)}", l)]
        if mine:
            parts.insert(0, "### PROPOSED BY THE AUTHOR (HARD: they asked for these themselves. Each one gets a post in this "
                            "batch before any other source, up to the batch size)\n" + "\n".join(mine))
        parts.append("### Story seeds from earlier calls\n" + seeds_text)
    sources = "\n\n".join(p for p in parts if p.strip())
    if not sources:
        sys.exit("No sources found")
    posts = [dict(q["post"], status="approved") for q in ready] + (draft(sources, a.n - len(ready)) if a.n > len(ready) else [])
    left = {i + 1: lint(p["text"]) for i, p in enumerate(posts) if lint(p["text"])}
    out = C.BATCHES
    batch_json = out / f"{dt.date.today()}_{re.sub(r'[^A-Za-z0-9]+', '_', a.tab)}.json"
    batch_json.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    if a.dry_run:
        for i, p in enumerate(posts, 1):
            print(f"\n=== {i} · {p['title']} [{p['format']}] {p['source']}\n{p['text']}")
    else:
        header = f"{a.tab} · written {dt.date.today():%d %b %Y} by the content engine · sources: " + \
                 ", ".join([f"call {d}" for d in a.calls] + (["interviewer call"] if a.interview else []) +
                           (["story seeds"] if a.seeds else [])) + " · mark ✅ in the heading when approved"
        print("written:", write_tab(posts, a.tab, header))
        for q in ready:  # taken: the next batch won't put them in again
            q["batch"], q["batched_on"] = a.tab, str(dt.date.today())
        if ready:
            READY.write_text(json.dumps(queue, ensure_ascii=False, indent=1))
        try:  # a graphic under every post, made from the post's own words
            import content_engine_visuals as vis
            spec_file = vis.auto_spec(batch_json)
            vis.upload(spec_file, vis.render(spec_file))
            vis.into_doc(spec_file)
            posts = json.loads(batch_json.read_text())
        except Exception as e:
            print(f"graphics not made: {e}")
        try:  # and a real photo of the author from their own library on every photo post
            import content_engine_photos as photos
            photos.library_pick(batch_json)
            posts = json.loads(batch_json.read_text())
        except Exception as e:
            print(f"photos not picked: {e}")
        try:  # every video post gets its card: how to say it, quick and easy
            import content_engine_video as cv
            for i, p in enumerate(posts, 1):
                if p["format"] == "video":
                    print(cv.write_card(cv.shoot_card(p, i), a.tab))
            refresh_meta(batch_json)
        except Exception as e:
            print(f"video cards not made: {e}")
        # approved: the ones already approved outside the doc, and from AUTO_APPROVE_FROM every post with no open question
        ok = [{"post": i + 1, "decision": "approve", "instruction": "", "new_text": ""} for i, p in enumerate(posts)
              if i < len(ready) or (AUTO_APPROVE_FROM and dt.date.today() >= AUTO_APPROVE_FROM and not p["check"].strip())]
        if ok:
            import content_engine_feedback as fb
            fb.apply_in_doc(a.tab, posts, ok)
            for d in ok:
                posts[d["post"] - 1]["status"] = "approved"
            batch_json.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
            print("approved (outside the doc, or no open question):", [d["post"] for d in ok])
    print(f"{len(posts)} posts" + (f", still flagged: {left}" if left else ", all mechanical checks passed"))
