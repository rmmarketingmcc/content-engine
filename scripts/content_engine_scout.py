#!/usr/bin/env python3
"""Content engine scout: the week's mail (and any facts your own plugin adds) in, at most a few story questions for
the interviewer out.

Few on purpose: zero is fine, and nothing becomes a post directly. Each story becomes one question in the
interviewer's brief; only what the author then tells on the call can become a post (their words, their OK to use it).

Sources:
  - the week's mail with the people that matter ([scout] contacts_file: a JSON file {"by_email": {address: kind},
    "by_domain": {domain: kind}} with kind client, lead or partner); without that file, every personal mail of the week
  - optional: your own facts plugin ([scout] facts_plugin, a Python file with collect(start, end) -> dict), for wins,
    meetings, pipeline moves or anything else you track. Not included: write your own.
The model also reads data/kb/icp.md (who the author wants as clients), so it picks stories those readers recognise.
Every story passes guard(): no names from [scout] banned_names_file or the contacts file, no money, no private topics.

Usage:
  python3 scripts/content_engine_scout.py --dry-run   # print, save nothing
  python3 scripts/content_engine_scout.py --push      # save the brief and push it to the interviewer
  python3 scripts/content_engine_scout.py --days 7
"""
import argparse
import datetime as dt
import importlib.util
import json
import logging
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402
import llm_guard  # noqa: E402

KB = C.KB
BRIEFS = C.BRIEFS
ICP = KB / "icp.md"
MODEL = C.get("llm", "model")
MAX_MAILS = int(C.get("scout", "max_mails"))
MAX_STORIES = int(C.get("scout", "max_stories"))  # higher quality, not more
log = logging.getLogger("content_engine_scout")

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["stories"], "properties": {"stories": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
    "required": ["title", "what_happened", "why", "ask", "source"],
    "properties": {
        "title": {"type": "string", "description": "working title in English, max 60 characters"},
        "what_happened": {"type": "string", "description": "1-2 sentences in English, facts only"},
        "why": {"type": "string", "description": "in English: which kind of post and why it could work, one sentence"},
        "ask": {"type": "string", "description": "the one question the interviewer asks the author, casual Danish, max 25 words"},
        "source": {"type": "string", "description": "where the fact comes from, e.g. 'mail from a client, 22 Oct'"}}}}}}


def _system() -> str:
    who = ", ".join(p for p in (C.get("author", "name"), C.get("author", "business"), C.get("author", "pitch")) if p) or "the author"
    return f"""You find LinkedIn post material for {who}. An interviewer calls them every week; you give it at most {MAX_STORIES} things from this week worth asking about. Zero is the normal answer for a quiet week. Only pick a story you would bet becomes a post they approve as it is: noise costs more than a missed story.

What counts is not likes but DMs: a post is good when a business owner recognises their own situation and writes to the author. Good: a client's own words (thanks, a complaint and how it was solved, why they chose the author), a result with a before and after, a decision made, a mistake owned, something that surprised them. Skip routine work and internal tooling. Important topics should come back: a new moment on a topic posted about before is welcome. Only skip a story that is already told, drafted or saved (see the list). Lead with the human moment, not the know-how.

Hard rules for every word you write:
- Never name a client, lead, partner, person or company, and no place names. Describe by type ("en VVS'er", "et bureau").
- No money amounts in any currency, no prices, no budgets. Percentages and counts are fine.
- Nothing about taxes, companies, accounting, insurance, health or pay.
- Never invent anything. Only use what the facts say."""


SYSTEM = _system()
MONEY = re.compile(r"\d[\d.,]*\s*(k\b|kr\b|kr\.|kroner|dkk|eur\b|euro|€|\$|usd|gbp|£)|[€$£]\s*\d", re.I)
PRIVATE_TOPICS = re.compile(r"\b(" + "|".join([
    "skat", "tax", "udbytte", "dividend", "forsikring", "insurance", "medicin", "medication", "health", "sygdom", "revisor",
    "accountant", "bogføring", "bookkeeping", "faktura", "invoice", "løn", "salary", "hourly rate", "timepris",
    *C.get("scout", "private_topics")]) + r")\b", re.I)


def _contacts() -> dict:
    f = C.get("scout", "contacts_file")
    return json.loads(C.path(f).read_text()) if f else {}


def banned_names() -> set[str]:
    """Names that must never reach a story: one per line in [scout] banned_names_file, plus the contacts' domain stems."""
    names = set()
    f = C.get("scout", "banned_names_file")
    if f and C.path(f).exists():
        names |= {l.strip().lower() for l in C.path(f).read_text().splitlines() if len(l.strip()) >= 3}
    for dom in _contacts().get("by_domain", {}):
        stem = dom.split(".")[0].lower()
        if len(stem) >= 5:
            names.add(stem)
    return names


def guard(story: dict, names: set[str]) -> list[str]:
    text = " ".join(str(v) for v in story.values())
    low = text.lower()
    problems = []
    if MONEY.search(text):
        problems.append(f"money amount: {MONEY.search(text).group(0)!r}")
    if PRIVATE_TOPICS.search(text):
        problems.append(f"private topic: {PRIVATE_TOPICS.search(text).group(0)!r}")
    hits = sorted(n for n in names if re.search(rf"(?<![\wæøå]){re.escape(n)}(?![\wæøå])", low))
    if hits:
        problems.append(f"name: {', '.join(hits[:5])}")
    return problems


def plugin_facts(start: dt.date, end: dt.date) -> dict:
    """Your own facts for the week ([scout] facts_plugin), or nothing."""
    f = C.get("scout", "facts_plugin")
    if not f:
        return {}
    spec = importlib.util.spec_from_file_location("scout_facts_plugin", C.path(f))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.collect(start, end) or {}


def list_ids(days: int, cap: int = 300) -> list[str]:
    import gmail_util
    q = (f"newer_than:{days}d -in:chats -in:spam -in:trash "
         "-category:promotions -category:social -category:updates -category:forums")
    try:
        return [m["id"] for m in gmail_util.gmail_get("users/me/messages", params={"q": q, "maxResults": cap}).get("messages", [])]
    except Exception as e:  # noqa: BLE001
        log.warning(f"gmail list failed: {e}")
        return []


def mail_facts(days: int) -> list[dict]:
    """The week's mail with your contacts: who wrote (by kind only), when, the sender's own words."""
    import gmail_util
    reg = _contacts()
    kinds = {}
    for mid in list_ids(days):
        for a in gmail_util.counterpart_addresses(gmail_util.get_headers(mid)):
            kind = reg.get("by_email", {}).get(a) or reg.get("by_domain", {}).get(a.split("@", 1)[-1]) if reg else "contact"
            if kind:
                kinds[mid] = kind if isinstance(kind, str) else kind.get("kind", "contact")
                break
        if len(kinds) >= MAX_MAILS:
            break
    out = []
    for b in gmail_util.fetch_message_blocks(list(kinds)):
        text = re.sub(r"\s+", " ", gmail_util.own_words(b["text"])).strip()[:700]
        if text:
            out.append({"from": "them" if b["inbound"] else "the author", "who": kinds[b["msg_id"]],
                        "date": b["date_iso"], "subject": b["label"], "text": text})
    return out


def told_already() -> list[str]:
    """Hooks and titles of everything posted, drafted or seeded, so the scout skips them."""
    import content_engine_draft as ced
    _, hooks = ced.posted_sections()
    # the folder also holds *.feedback.json and *_visuals.json (dicts): only post lists have titles
    titles = []
    for f in sorted(C.BATCHES.glob("*.json")):
        d = json.loads(f.read_text())
        titles += [p["title"] for p in d if isinstance(p, dict) and p.get("title")] if isinstance(d, list) else []
    seeds_file = KB / "story_seeds.md"
    seeds = re.findall(r"^\d+\. \*\*(.+?)\*\*", seeds_file.read_text(), re.M) if seeds_file.exists() else []
    return hooks + titles + seeds


def ask(facts: dict, feedback: str = "") -> list[dict]:
    from anthropic import Anthropic
    works = (KB / "what_works.md").read_text() if (KB / "what_works.md").exists() else ""
    icp = ICP.read_text() if ICP.exists() else ""
    user = (f"# Who the author wants as clients (internal: pick stories these readers would recognise; never put its counts, "
            f"floors or open questions in a story)\n{icp or '(missing)'}\n\n"
            f"# What works on the author's LinkedIn (analytics, then the live numbers per post: DMs count most)\n{works}\n\n"
            f"# Already posted, drafted or saved as a story (never suggest the same story again; the same topic is fine)\n"
            + "\n".join(f"* {t[:140]}" for t in told_already()) +
            f"\n\n# This week's facts\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
            f"# Task\nPick at most {MAX_STORIES} stories worth asking about. Zero is fine.")
    if feedback:
        user += "\n\nYour last answer broke the rules. Rewrite without these:\n" + feedback
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM, messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    return json.loads(msg.content[0].text)["stories"][:MAX_STORIES]


def scout(days: int) -> list[dict]:
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    facts = plugin_facts(start, end)
    facts["mail"] = mail_facts(days)
    names = banned_names()
    stories = ask(facts)
    problems = {i: guard(s, names) for i, s in enumerate(stories)}
    if any(problems.values()):
        stories = ask(facts, "\n".join(f"- story {i + 1}: {'; '.join(p)}" for i, p in problems.items() if p))
    kept = [s for s in stories if not guard(s, names)]
    for s in stories:
        if s not in kept:
            log.warning(f"dropped {s['title']!r}: {'; '.join(guard(s, names))}")
    return kept


def render(stories: list[dict]) -> str:
    if not stories:
        return ""
    lines = ["From this week's mail and meetings. Ask about these only if the author doesn't bring something bigger; "
             "they decide what is public, and nothing here becomes a post unless they tell it."]
    lines += [f"- {s['what_happened']} Ask: \"{s['ask']}\" ({s['source']})" for s in stories]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=C.get("scout", "days"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true", help="save the brief and push it to the interviewer")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stories = scout(a.days)
    for i, s in enumerate(stories, 1):
        print(f"{i}. {s['title']}\n   {s['what_happened']}\n   why: {s['why']}\n   ask: {s['ask']}\n   source: {s['source']}")
    if not stories:
        print("nothing worth a question this week")
    if a.dry_run:
        sys.exit(0)
    BRIEFS.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{dt.date.today()}_scout.json").write_text(json.dumps(stories, ensure_ascii=False, indent=1))
    brief = render(stories)
    path = BRIEFS / f"{dt.date.today()}.md"
    # a hand-written brief for the same day keeps its text; the scout part is replaced on a rerun
    own = re.sub(r"\n*<!-- scout -->.*", "", path.read_text(), flags=re.S) if path.exists() else ""
    path.write_text((own.strip() + "\n\n" if own.strip() else "") + "<!-- scout -->\n" + brief + "\n")
    print("saved", path)
    if a.push:  # the whole brief from live state (batches, these questions), even in a quiet week
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "content_engine_interviewer.py"), "refresh"], check=True)
