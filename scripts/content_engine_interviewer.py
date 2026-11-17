#!/usr/bin/env python3
"""The content engine's interview agent on ElevenLabs (a voice agent the author calls once a week).

The prompt starts from prompts/interviewer_prompt.md and lives in data/kb/interviewer_prompt.md, your own copy, which
the learn step writes the author's call rules into ({BRIEF} and the author's details are filled in per push), so the
interviewer is a file you can read and edit, not something clicked together in a UI. The API key is read from tokens/<elevenlabs.key_file>
(you create it in ElevenLabs and save it yourself). The agent id is kept in data/state/interviewer_agent.json.

Usage:
  python3 scripts/content_engine_interviewer.py voices            # voices in the configured language to choose from
  python3 scripts/content_engine_interviewer.py create --voice ID # create the agent once
  python3 scripts/content_engine_interviewer.py refresh           # push the brief built from live state
  python3 scripts/content_engine_interviewer.py brief --file F    # set a brief by hand
  python3 scripts/content_engine_interviewer.py link              # the author's personal talk link (carries the lookup key)
  python3 scripts/content_engine_interviewer.py tools [--attach]  # live lookups during the call (needs your own webhook server)
  python3 scripts/content_engine_interviewer.py learn [--dry-run] # the author's rules for the call itself, from each new call, into the prompt (hourly)
  python3 scripts/content_engine_interviewer.py topic add|done|list  # a topic the author hands over for the next calls
"""
import argparse
import datetime as dt
import json
import pathlib
import random
import re
import shutil
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

KEY_FILE = C.token("key_file")
TEMPLATE = C.PROMPTS / "interviewer_prompt.md"
PROMPT_FILE = C.KB / "interviewer_prompt.md"  # your own copy: the learn step writes into it
STATE_FILE = C.STATE / "interviewer_agent.json"
API = "https://api.elevenlabs.io"
FIRST = C.author()

NAME = C.get("elevenlabs", "agent_name")
# Not the same line every call, and straight to ideas: a new one goes live after every call (learn, hourly) and on
# every brief refresh.
OPENERS = C.get("elevenlabs", "openers") or [
    "Hey {first}! I've got three ideas for posts lined up. Want to hear them?",
    "Hi {first}. I picked three topics from your week. Shall I run through them?",
    "Hey! I found a few stories in your week that could be good posts. Want the first one?",
    "{first}, hi. Three options for today, and I know which one I'd start with. Ready?",
    "Hi! Let's get you some new posts. I have three topics ready. Shall I go?",
]
# On "pause" an agent without this says "Still here" every few seconds; skip_turn lets it stay quiet.
SKIP_TURN = {"skip_turn": {"name": "skip_turn", "description": f"Call this when {FIRST} asks for a pause or a moment "
             "(pause, vent lige, giv mig et øjeblik) or is silent during one: skip your turn and say nothing until they speak again.",
             "params": {"system_tool_type": "skip_turn"}}}


def _opener(last: str | None = None) -> str:
    """A random opener, never the one used last."""
    options = [o.format(first=FIRST) for o in OPENERS]
    return random.choice([o for o in options if o != last] or options)


NO_BRIEF = "No brief this week: offer the standing topics as your three options."
LLM = C.get("elevenlabs", "llm")
TTS_MODEL = C.get("elevenlabs", "tts_model")  # must support the call's language
LANGUAGE = C.get("elevenlabs", "language")
MAX_SECONDS = int(C.get("elevenlabs", "max_call_minutes")) * 60  # ElevenLabs' default is 10 minutes
LOOKUP_URL = C.get("elevenlabs", "lookup_url")
KEYS_FILE = C.token("lookup_keys_file")
# Live lookups: webhooks the agent calls during the call. The server that answers them is not part of this repo (it
# reads your own mail, messages and calls); set [elevenlabs] lookup_url to your own. (name, the one field the LLM
# fills in, what the tool is for, how to fill the field)
TOOLS = [
    ("search_mail", "query",
     "Search the author's work mailbox when they mention a mail, a reply, an offer or someone who wrote to them. "
     "Returns up to 5 mails with date, sender, subject and a short snippet.",
     "Gmail search words: a name, a company or a topic, optionally from:name, e.g. 'from:jens tilbud'"),
    ("read_mail", "id",
     "Read one mail found with search_mail when the snippet is not enough. Returns up to 1.500 characters without the quoted history.",
     "the id of a mail from search_mail"),
    ("search_linkedin_messages", "query",
     "Search the author's LinkedIn messages from the last six months when they mention someone who wrote to them on "
     "LinkedIn or a reaction to a post. Never ask them what they wrote: look it up. Returns up to 6 messages.",
     "a name or a few words from the topic, or 'latest' for the newest messages"),
    ("search_posts", "query",
     "Search the author's own LinkedIn posts and the engine's drafts when they mention a post. Returns up to 4 with batch, "
     "first line and the start of the text.",
     "words from the post's topic"),
    ("search_calls", "query",
     "Search the author's calls of the last 90 days, your own earlier calls with them included. Use it when they ask what "
     "you talked about last time, when they mention a call or a meeting, and before you offer a topic, to check they "
     "haven't told it already. Returns up to 5 with date, kind, title, who, a summary and matching lines.",
     "a name, company or topic from the call, or 'latest interviewer' for your last call with them"),
    ("read_call", "id",
     "Read more of one call found with search_calls: the summary and what was said.",
     "the id of a call from search_calls"),
    ("calendar", "query",
     "The author's calendar, 14 days back and 14 ahead. Use it when they mention a meeting, a day or their week. Returns "
     "up to 10 events with time, title and who.",
     "words from the meeting or a name, or a day: 'i dag', 'i går', 'i morgen', 'næste uge'"),
    ("open_web_page", "url",
     "Open a web page when they mention a page, their website or a client's site. Returns the title and up to 2.000 characters of text.",
     "a full address or a domain"),
]


def _key() -> str:
    if not KEY_FILE.exists():
        sys.exit(f"Missing {KEY_FILE}. Create a key in ElevenLabs (Developers > API keys), copy it, "
                 f"then run: pbpaste > {KEY_FILE}")
    return KEY_FILE.read_text().strip()


def _call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"xi-api-key": _key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs {method} {path} -> HTTP {e.code}: {e.read().decode()[:400]}")


def _about() -> str:
    parts = [C.get("author", "name"), C.get("author", "business"), C.get("author", "pitch")]
    return ", ".join(p for p in parts if p) or "the author"


def _prompt_text() -> str:
    if not PROMPT_FILE.exists():
        PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TEMPLATE, PROMPT_FILE)
    return PROMPT_FILE.read_text()


def _prompt(brief: str | None) -> str:
    """The prompt file with this week's brief; the lookup section only once the lookup tools are on the agent."""
    text = _prompt_text()
    drop, keep = ("no-lookups", "lookups") if _state().get("lookups_attached") else ("lookups", "no-lookups")
    text = re.sub(rf"<!-- {drop} -->.*?<!-- /{drop} -->\n?", "", text, flags=re.S)
    text = re.sub(rf"<!-- /?{keep} -->\n?", "", text)
    text = re.sub(r"<!-- /?learned -->\n?", "", text)
    text = text.replace("{AUTHOR}", FIRST).replace("{ABOUT}", _about())
    return text.replace("{BRIEF}", (brief or "").strip() or NO_BRIEF)


def _live_brief(agent_id: str) -> str | None:
    """This week's brief as it is on the agent now, so a prompt push never wipes it."""
    live = _call("GET", f"/v1/convai/agents/{agent_id}")["conversation_config"]["agent"]["prompt"]["prompt"]
    m = re.search(r"# This week's brief\n(.*?)\n\n# Ending", live, re.S)
    return None if not m or m.group(1).strip() == NO_BRIEF else m.group(1)


def _live_opener(agent_id: str) -> str | None:
    return _call("GET", f"/v1/convai/agents/{agent_id}")["conversation_config"]["agent"].get("first_message")


def _rotate_opener() -> str:
    """A new opener on the live agent (after every call), nothing else touched."""
    agent_id = _state()["agent_id"]
    new = _opener(_live_opener(agent_id))
    _call("PATCH", f"/v1/convai/agents/{agent_id}", {"conversation_config": {"agent": {"first_message": new}}})
    return new


def _state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1))


def cmd_voices(_):
    res = _call("GET", f"/v1/shared-voices?language={LANGUAGE}&page_size=15")
    for v in res.get("voices", []):
        print(f'{v.get("voice_id")}  {v.get("name")!r:28} {v.get("gender", ""):7} {v.get("accent", ""):12} '
              f'{(v.get("description") or "")[:50]!r}  preview: {v.get("preview_url")}')


def _own_voice(voice_id: str) -> str:
    """A library (shared) voice must be added to the account before an agent can use it."""
    mine = {v["voice_id"] for v in _call("GET", "/v1/voices").get("voices", [])}
    if voice_id in mine:
        return voice_id
    shared = _call("GET", f"/v1/shared-voices?language={LANGUAGE}&page_size=100").get("voices", [])
    hit = next((v for v in shared if v.get("voice_id") == voice_id), None) or sys.exit(f"Voice {voice_id} not in the library")
    res = _call("POST", f"/v1/voices/add/{hit['public_owner_id']}/{voice_id}", {"new_name": hit["name"]})
    print("added library voice", hit["name"])
    return res.get("voice_id", voice_id)


def cmd_create(a):
    st = _state()
    if st.get("agent_id"):
        sys.exit(f"Agent already exists: {st['agent_id']} (use 'refresh' to update it)")
    a.voice = _own_voice(a.voice)
    body = {"name": NAME, "conversation_config": {
        "agent": {"first_message": _opener(), "language": LANGUAGE,
                  "prompt": {"prompt": _prompt(None), "llm": LLM, "built_in_tools": SKIP_TURN}},
        "tts": {"voice_id": a.voice, "model_id": TTS_MODEL},
        "conversation": {"max_duration_seconds": MAX_SECONDS}}}
    res = _call("POST", "/v1/convai/agents/create", body)
    _save_state({"agent_id": res["agent_id"], "name": NAME, "voice_id": a.voice})
    print("created agent", res["agent_id"])


def review_block(batch_file: str) -> str:
    """The drafts of a batch that still wait for an OK, for a call that goes through them first."""
    posts = json.loads(pathlib.Path(batch_file).read_text())
    name = pathlib.Path(batch_file).stem.split("_", 1)[1].replace("_", " ")
    waiting = [(i, p) for i, p in enumerate(posts, 1) if p.get("status") not in ("approved", "dropped")]
    if not waiting:
        return ""
    lines = [f"Drafts waiting for review ({name}, {len(waiting)} of {len(posts)}):"]
    for i, p in waiting:
        first = next((l.strip() for l in p["text"].splitlines() if l.strip()), "")
        lines.append(f'- {i} · {p["title"]} ({p["format"]}). First line: "{first}"' + (f' Check: {p["check"]}' if p.get("check") else ""))
    return "\n".join(lines)


def assemble_brief() -> str:
    """The brief from live state, never a file that goes stale. The call is for new post ideas, not for feedback on
    posts, so it carries the topics to offer (this week's scout questions, newest at most 8 days old) and only one
    line on where the batches stand, for context."""
    import content_engine_feedback as cef
    status = []
    for bf in cef.active_batches():
        posts = json.loads(bf.read_text())
        name = bf.stem.split("_", 1)[1].replace("_", " ")
        ok = sum(1 for p in posts if p.get("status") == "approved")
        status.append(f"{name}: {ok} of {len(posts)} approved")
    out = ("For context only, never to review on the call (that happens in the doc): " + "; ".join(status) + "."
           if status else "")
    own = open_topics()
    if own:
        out += (f"\n\nTopics {FIRST} asked for (offer these first):\n"
                + "\n".join(f'- {t["what_happened"]} Suggested question: "{t["ask"]}"' for t in own))
    newest = max(C.BRIEFS.glob("*_scout.json"), default=None)
    if newest and (dt.date.today() - dt.date.fromisoformat(newest.name[:10])).days <= 8:
        import content_engine_scout as sc
        block = sc.render(json.loads(newest.read_text()))
        if block:
            out += ("\n\nTopics to offer as options (from this week's mail and meetings):\n"
                    + block.split("\n", 1)[1].replace(' Ask: "', ' Suggested question: "'))
    return out.strip()


# A topic the author hands over, offered on every call until it is marked done or TOPIC_DAYS have passed. The scout
# picks from mail and meetings only, so a topic from anywhere else needs this way in.
TOPICS_FILE = C.BRIEFS / "topics.json"
TOPIC_DAYS = 21


def _topics() -> list[dict]:
    try:
        got = json.loads(TOPICS_FILE.read_text())
    except (OSError, ValueError):
        return []
    return got if isinstance(got, list) else []


def open_topics() -> list[dict]:
    today = dt.date.today()
    return [t for t in _topics() if not t.get("done")
            and (today - dt.date.fromisoformat(t["added"])).days <= TOPIC_DAYS]


def cmd_topic(a):
    """add: a topic (what happened, in English, and the question to ask); done: matched by a few words of its title;
    list: what the next call offers."""
    topics = _topics()
    if a.action == "add":
        if not (a.title and a.what and a.ask):
            sys.exit("add needs --title, --what and --ask")
        topics.append({"title": a.title, "what_happened": a.what, "ask": a.ask,
                       "added": dt.date.today().isoformat(), "done": None})
    elif a.action == "done":
        hits = [t for t in topics if not t.get("done") and (a.match or "").lower() in t["title"].lower()]
        if len(hits) != 1:
            sys.exit(f"{len(hits)} open topics match {a.match!r}: " + "; ".join(t["title"] for t in hits))
        hits[0]["done"] = dt.date.today().isoformat()
    if a.action != "list":
        TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOPICS_FILE.write_text(json.dumps(topics, ensure_ascii=False, indent=1) + "\n")
    for t in open_topics():
        print(f"- {t['title']} (added {t['added']})")
    print("(the next refresh, or the weekly scout run, puts these in the call brief)")


VOICES = {"en": C.get("elevenlabs", "voice_en"), "da": C.get("elevenlabs", "voice_da")}
KEYTERMS = C.get("elevenlabs", "keyterms")  # names and terms the speech recognition should know


def cmd_voice(a):
    """Questions in English with an English voice (the author can still answer in Danish), or back to the Danish voice."""
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    voice = VOICES[a.lang] or sys.exit(f"Set [elevenlabs] voice_{a.lang} in the config first.")
    _call("PATCH", f"/v1/convai/agents/{agent_id}", {"conversation_config": {
        "tts": {"voice_id": voice},
        "asr": {"keywords": KEYTERMS}}})
    st = _state()
    st["voice_id"], st["voice_lang"] = voice, a.lang
    _save_state(st)
    print(f"voice: {a.lang} ({voice}); speech recognition stays {LANGUAGE}")


def cmd_refresh(_):
    """Push the brief built from live state (after a batch, after feedback, and weekly with the scout)."""
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    brief = assemble_brief()
    _call("PATCH", f"/v1/convai/agents/{agent_id}",
          {"conversation_config": {"agent": {"first_message": _opener(_live_opener(agent_id)),
                                             "prompt": {"prompt": _prompt(brief), "llm": LLM, "built_in_tools": SKIP_TURN}}}})
    print("brief refreshed:\n" + brief)


def cmd_brief(a):
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    brief = pathlib.Path(a.file).read_text() if a.file else None
    if getattr(a, "review", None):
        block = review_block(a.review)
        brief = (block + "\n\n" + (brief or "")).strip() if block else brief
    _call("PATCH", f"/v1/convai/agents/{agent_id}",
          {"conversation_config": {"agent": {"first_message": _opener(_live_opener(agent_id)),
                                             "prompt": {"prompt": _prompt(brief), "llm": LLM, "built_in_tools": SKIP_TURN}}}})
    print("prompt and first message pushed, " + ("brief set" if brief else "brief cleared"))


def cmd_kb(a):
    """Upload a text file to the agent's knowledge base, replacing an older doc with the same name."""
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    agent = _call("GET", f"/v1/convai/agents/{agent_id}")
    prompt_cfg = agent["conversation_config"]["agent"]["prompt"]
    kb = [d for d in (prompt_cfg.get("knowledge_base") or []) if d.get("name") != a.name]
    doc = _call("POST", "/v1/convai/knowledge-base/text", {"text": pathlib.Path(a.file).read_text(), "name": a.name})
    kb.append({"type": "text", "name": a.name, "id": doc["id"], "usage_mode": "auto"})
    _call("PATCH", f"/v1/convai/agents/{agent_id}",
          {"conversation_config": {"agent": {"prompt": {"knowledge_base": kb, "rag": {"enabled": True}}}}})
    print(f"knowledge base: {len(kb)} doc(s), added {a.name!r} ({doc['id']})")


def cmd_last(a):
    """Transcript of the latest call(s), oldest line first."""
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    convs = _call("GET", f"/v1/convai/conversations?agent_id={agent_id}&page_size={a.n}").get("conversations", [])
    for c in reversed(convs):
        d = _call("GET", f"/v1/convai/conversations/{c['conversation_id']}")
        meta = d.get("metadata", {})
        print(f"=== {c['conversation_id']} · {meta.get('start_time_unix_secs')} · {meta.get('call_duration_secs')} s · {d.get('status')}")
        for t in d.get("transcript", []):
            if t.get("message"):
                print(f"[{t.get('time_in_call_secs', 0):>4}s] {t['role']}: {t['message']}")
            for tc in t.get("tool_calls") or []:
                print(f"         tool call: {tc.get('tool_name')} {str(tc.get('params_as_json'))[:120]}")


def personal_link() -> str:
    """The author's own link: the lookup key rides along as a secret dynamic variable, so lookups only work on their
    calls. Never put this link on a shared page."""
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    key = json.loads(KEYS_FILE.read_text())["lookup_key"] if KEYS_FILE.exists() else ""
    return f"https://elevenlabs.io/app/talk-to?agent_id={agent_id}" + (f"&var_secret__lookup_key={key}" if key else "")


def cmd_link(_):
    print(personal_link())


LEARNED_STATE = C.STATE / "interviewer_learned.json"
LEARN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rules"], "properties": {
    "rules": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["rule", "quote"],
              "properties": {"rule": {"type": "string", "description": "one general rule in English for the interviewer, "
                                      "addressed as 'you', one or two sentences"},
                             "quote": {"type": "string", "description": "the author's own words, verbatim from one of their lines, max 20 words"}}}}}}
LEARN_TASK = f"""Below are the instructions of {FIRST}'s LinkedIn interviewer (an AI voice agent) and the transcript of one
of their calls with it. Find every place where {FIRST} tells the interviewer how it should run a call: what to ask or never
ask, looking things up, how many ideas a call should give, pace, length, tone, language or voice, when and how to end.
Write each as one general rule for the interviewer, addressed as "you", for example "Never ask them X; do Y instead."
Take the quote verbatim from one of {FIRST}'s own lines ({FIRST}:), never from the interviewer's lines. Skip everything
about the posts, the business or the stories: only how the call is run. Skip a rule the instructions already say
clearly. Skip requests meant only for that moment ("try again now", "go back to that"): take only what they want for
every call. Never write a rule that contradicts the instructions unless they clearly say the instruction itself is
wrong. Return an empty list when they said nothing of the kind; never invent a rule."""


def _own_lines(transcript: str) -> str:
    pat = rf"^\[\d+s\] {re.escape(FIRST)}: "
    return " ".join(re.sub(pat, "", l) for l in transcript.splitlines() if re.match(pat, l))


def _squash(s: str) -> str:
    return re.sub(r"[^\wæøå]+", " ", s.lower()).strip()


def _add_learned(rules: list[dict], day: str, path: pathlib.Path = PROMPT_FILE) -> list[dict]:
    """Rules into the learned section of the prompt file, skipping any whose quote is already in it."""
    text = _prompt_text() if path == PROMPT_FILE else path.read_text()
    start, end = text.index("<!-- learned -->\n") + len("<!-- learned -->\n"), text.index("<!-- /learned -->")
    added, lines = [], ""
    for r in rules:
        if _squash(r["quote"]) in _squash(text + lines):
            continue
        lines += f'- {r["rule"].strip()} ({day}: "{r["quote"].strip()}")\n'
        added.append(r)
    if added:
        path.write_text(text[:end] + lines + text[end:])
    return added


def _push_prompt() -> None:
    """The prompt file to the live agent, keeping the brief that is on it now; model and voice untouched."""
    agent_id = _state()["agent_id"]
    _call("PATCH", f"/v1/convai/agents/{agent_id}",
          {"conversation_config": {"agent": {"prompt": {"prompt": _prompt(_live_brief(agent_id))}}}})


def cmd_learn(a):
    """After every call, what the author tells the interviewer about how to run a call goes straight into its own
    instructions and live on the agent, and a short note says which rules. Without this such rules only reach the post
    writer's rulebook (voice_rules.md) and have to be said again. Every finished call is read once, whether it became a
    batch or not; a quote must stand word for word in the author's own lines."""
    import content_engine_draft as ced
    agent_id = _state().get("agent_id") or sys.exit("No agent yet, run 'create' first")
    convs = _call("GET", f"/v1/convai/conversations?agent_id={agent_id}&page_size=20").get("conversations", [])
    st = json.loads(LEARNED_STATE.read_text()) if LEARNED_STATE.exists() else None
    if st is None and not a.call:  # first run: the calls so far count as read
        LEARNED_STATE.parent.mkdir(parents=True, exist_ok=True)
        LEARNED_STATE.write_text(json.dumps({"seen": [c["conversation_id"] for c in convs], "log": []}, indent=1))
        return print(f"started: {len(convs)} earlier calls marked as read")
    st = st or {"seen": [], "log": []}
    if a.call:
        todo = [c for c in convs if c["conversation_id"] == a.call]
    else:
        todo = [c for c in convs if c["conversation_id"] not in st["seen"] and c.get("status") == "done"]
    import llm_guard
    from anthropic import Anthropic
    learned = []
    for c in reversed(todo):  # oldest first
        cid = c["conversation_id"]
        day = dt.datetime.fromtimestamp(c.get("start_time_unix_secs") or 0).strftime("%-d %b")
        transcript = ced.interview_text(cid) if (c.get("call_duration_secs") or 0) >= 60 else ""
        rules = []
        if transcript:
            msg = llm_guard.guarded(Anthropic()).messages.create(
                model=ced.MODEL, max_tokens=4000,
                messages=[{"role": "user", "content": f"# The interviewer's instructions\n{_prompt(None)}\n\n# The call\n{transcript}\n\n# Task\n{LEARN_TASK}"}],
                output_config={"format": {"type": "json_schema", "schema": LEARN_SCHEMA}})
            own = _squash(_own_lines(transcript))
            rules = [r for r in json.loads(msg.content[0].text)["rules"] if _squash(r["quote"]) and _squash(r["quote"]) in own]
        if a.dry_run:
            print(f"{cid} ({day}): " + ("; ".join(f'{r["rule"]} "{r["quote"]}"' for r in rules) or "no rules"))
            continue
        added = _add_learned(rules, day)
        learned += [(day, r) for r in added]
        if cid not in st["seen"]:
            st["seen"].append(cid)
        st["log"].append({"when": dt.datetime.now().isoformat(timespec="seconds"), "call": cid, "added": [r["rule"] for r in added]})
        LEARNED_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    if todo and not a.dry_run:
        _rotate_opener()  # a new first line after every call, whether it taught a rule or not
    if not learned:
        return
    _push_prompt()
    import content_engine_feedback as cef
    days = sorted({d for d, _ in learned})
    lines = [f"From your call on {' and '.join(days)}, the interviewer has {len(learned)} new rule{'s' if len(learned) > 1 else ''}, "
             "live from your next call:"]
    lines += [f"- {r['rule']}" for _, r in learned[:3]] + ([f"(+{len(learned) - 3} more)"] if len(learned) > 3 else [])
    cef.inbox(f"Interviewer: {len(learned)} new rule{'s' if len(learned) > 1 else ''} from your call", lines)
    print(f"interviewer learned {len(learned)} rule(s), pushed live, note in the inbox")


def cmd_tools(a):
    """Create or update the lookup webhooks (TOOLS); --attach puts them on the agent."""
    st = _state()
    agent_id = st.get("agent_id") or sys.exit("No agent yet, run 'create' first")
    lookup = LOOKUP_URL or sys.exit("Set [elevenlabs] lookup_url to your own lookup server first (not part of this repo).")
    keys = json.loads(KEYS_FILE.read_text())
    if not st.get("lookup_secret_id"):  # proves to the server that a call comes from this agent
        st["lookup_secret_id"] = _call("POST", "/v1/convai/secrets", {"type": "new", "name": "engine_lookup_agent_key",
                                                                       "value": keys["agent_key"]})["secret_id"]
    ids = st.setdefault("lookup_tools", {})
    for name, field, desc, field_desc in TOOLS:
        cfg = {"type": "webhook", "name": name, "description": desc, "response_timeout_secs": 20,
               "api_schema": {"url": f"{lookup}/{name}", "method": "POST",
                              "request_headers": {"X-Engine-Agent": {"secret_id": st["lookup_secret_id"]},
                                                  "X-Engine-Key": {"variable_name": "secret__lookup_key"}},
                              "request_body_schema": {
                                  "type": "object", "description": "What to look up", "required": [field],
                                  "properties": {field: {"type": "string", "description": field_desc},
                                                 "conversation_id": {"type": "string", "dynamic_variable": "system__conversation_id"}}}}}
        if name in ids:
            _call("PATCH", f"/v1/convai/tools/{ids[name]}", {"tool_config": cfg})
        else:
            ids[name] = _call("POST", "/v1/convai/tools", {"tool_config": cfg})["id"]
        print(f"tool {name}: {ids[name]}")
    _save_state(st)
    if a.attach:
        brief = _live_brief(agent_id)
        st["lookups_attached"] = True
        _save_state(st)
        _call("PATCH", f"/v1/convai/agents/{agent_id}",
              {"conversation_config": {"agent": {"prompt": {"tool_ids": list(ids.values()),
                                                             "prompt": _prompt(brief), "llm": LLM}}}})
        print(f"attached {len(ids)} lookup tools, prompt pushed with the lookup section (brief kept)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("voices")
    c = sub.add_parser("create")
    c.add_argument("--voice", required=True)
    b = sub.add_parser("brief")
    b.add_argument("--file")
    b.add_argument("--review", help="batch json whose drafts the call goes through first")
    sub.add_parser("link")
    tl = sub.add_parser("tools")
    tl.add_argument("--attach", action="store_true")
    la = sub.add_parser("last")
    la.add_argument("-n", type=int, default=1)
    sub.add_parser("refresh")
    vo = sub.add_parser("voice")
    vo.add_argument("lang", choices=["en", "da"])
    k = sub.add_parser("kb")
    k.add_argument("--file", required=True)
    k.add_argument("--name", required=True)
    le = sub.add_parser("learn")
    le.add_argument("--call", help="one call id (default: every call not read yet)")
    le.add_argument("--dry-run", action="store_true", help="print the rules found, change nothing")
    tp = sub.add_parser("topic", help="a topic the author hands over (add / done / list)")
    tp.add_argument("action", choices=["add", "done", "list"])
    tp.add_argument("--title")
    tp.add_argument("--what", help="what happened, in English, facts only")
    tp.add_argument("--ask", help="the question to put to the author, max 25 words")
    tp.add_argument("--match")
    args = ap.parse_args()
    {"voices": cmd_voices, "create": cmd_create, "brief": cmd_brief, "link": cmd_link, "last": cmd_last,
     "kb": cmd_kb, "tools": cmd_tools, "refresh": cmd_refresh, "voice": cmd_voice, "learn": cmd_learn,
     "topic": cmd_topic}[args.cmd](args)
