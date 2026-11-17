#!/usr/bin/env python3
"""DMs per LinkedIn post: which posts start conversations, the engine's against the rest.

The content engine is judged on DMs, not likes. Read-only on LinkedIn: the author's own posts and messages from
LinkedIn's data export (Member Data Portability API, the token in tokens/<linkedin.dma_token_file>; the messages are
refreshed daily by linkedin_dma_sync.py).

Per post:
  named    DMs whose sender names the post (its topic, its comment word, a quote, a link), any time after it went up
  after    conversations opened by a potential client, an agency or a partner in the 72 hours after it went up (and
           before the next post), new ones and old ones they reopen after 14+ quiet days ('revived': people often buy
           after months of talking, so a post that brings an old contact back counts too): a softer signal, since
           people rarely say what made them write. Exact for engine posts (their publish time is in the batch file);
           for the others the export only has the time a post was created, which for a post scheduled in LinkedIn is
           earlier than when it went out, so read their 'after' loosely
Every conversation someone else opens is sorted once by a small model (who they are, whether they name a post and
which), cached by conversation id; only the label is kept, never the person's name or message.

Writes data/state/linkedin_post_dms.json (counts per post, no names) and prints the comparison.

  python3 scripts/linkedin_post_dms.py            # refresh the posts if older than 20 h, sort new conversations, report
  python3 scripts/linkedin_post_dms.py --days 60  # how far back to look (default 45)
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.parse

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

CACHE = C.CACHE / "linkedin_dma"
SHARES = CACHE / "shares.json"
SORTED = CACHE / "conversations_sorted.json"   # cid -> {who, names_post, post}; labels only
OUT = C.STATE / "linkedin_post_dms.json"
ME = C.get("linkedin", "profile_slug")  # the author's profile URL slug: tells their own messages apart
MODEL = C.get("llm", "small_model")
RELEVANT = {"potential_client", "agency", "partner"}
REVIVE_DAYS = 14  # an old conversation counts as reopened when they write after this many quiet days
WHO = ["potential_client", "agency", "partner", "peer", "recruiter_or_job", "sales_pitch", "personal", "other"]


def _utc(s: str) -> dt.datetime:
    return dt.datetime.strptime(s.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)


def pull_shares(force: bool = False) -> list[dict]:
    """The author's own posts from the export (Date, ShareLink, ShareCommentary), refreshed at most every 20 hours."""
    if SHARES.exists() and not force and time.time() - SHARES.stat().st_mtime < 20 * 3600:
        return json.loads(SHARES.read_text())
    tok = json.loads(C.token("dma_token_file").read_text())["access_token"]
    h = {"Authorization": f"Bearer {tok}", "LinkedIn-Version": "202312"}
    rows, start = [], 0
    while True:
        r = requests.get("https://api.linkedin.com/rest/memberSnapshotData", headers=h, timeout=60,
                         params={"q": "criteria", "domain": "MEMBER_SHARE_INFO", "start": start})
        r.raise_for_status()
        d = r.json()
        for e in d.get("elements", []):
            rows += e.get("snapshotData", [])
        if not any(l.get("rel") == "next" for l in d.get("paging", {}).get("links", [])):
            break
        start += 1
        time.sleep(0.3)
    seen, out = set(), []
    for r in rows:
        k = (r.get("Date"), r.get("ShareLink"))
        if k not in seen and r.get("Date"):
            seen.add(k)
            out.append({"date": r["Date"], "link": urllib.parse.unquote(r.get("ShareLink") or ""),
                        "text": (r.get("ShareCommentary") or "").replace('""', '"').strip('"')})
    if len(out) < 50:  # a short answer is a LinkedIn hiccup: keep the old copy
        return json.loads(SHARES.read_text()) if SHARES.exists() else out
    CACHE.mkdir(parents=True, exist_ok=True)
    SHARES.write_text(json.dumps(out, ensure_ascii=False))
    return out


def engine_posts() -> dict[str, dict]:
    """urn -> batch post, for every engine post that went out (linkedin_post.py writes the urn into the batch file)."""
    out = {}
    for f in C.BATCHES.glob("*Engine_Batch_*.json"):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name):
            continue
        b = re.search(r"Batch_(\d+)", f.name).group(1)
        for n, p in enumerate(json.loads(f.read_text()), 1):
            if isinstance(p, dict) and p.get("posted") and not p["posted"].get("deleted"):
                out[p["posted"]["urn"]] = {"key": f"{b}.{n}", "title": p.get("title_da") or p.get("title", ""), "text": p["text"]}
    return out


def _mine(m: dict) -> bool:
    return bool(ME) and ME in (m.get("SENDER PROFILE URL") or "")


def _flat(msgs: list[dict], n: int) -> str:
    return "\n".join(" ".join((m.get("CONTENT") or "").split())[:n] for m in msgs)


def conversation_starts(since: dt.datetime) -> list[dict]:
    """Openings by someone else since `since`, oldest first: a new conversation they started, or an old one they write in
    again after REVIVE_DAYS of silence on both sides (the author's own follow-up after a pause never counts). key = the
    cache key for the label: the conversation id for a new one, id@time for a revival."""
    rows = json.loads((CACHE / "inbox.json").read_text())
    by = {}
    for r in rows:
        if r.get("IS MESSAGE DRAFT") == "Yes" or r.get("FOLDER") == "SPAM" or not r.get("DATE"):
            continue
        by.setdefault(r["CONVERSATION ID"], []).append(r)
    out = []
    for cid, msgs in by.items():
        msgs.sort(key=lambda r: r["DATE"])
        for i, m in enumerate(msgs):
            at = _utc(m["DATE"])
            if _mine(m) or at < since:
                continue
            if i and at - _utc(msgs[i - 1]["DATE"]) < dt.timedelta(days=REVIVE_DAYS):
                continue
            theirs = [x for x in msgs[i:] if not _mine(x)][:3]
            out.append({"cid": cid, "key": f"{cid}@{at.isoformat()}" if i else cid, "kind": "revived" if i else "new",
                        "at": at, "text": _flat(theirs, 600), "before": _flat(msgs[max(0, i - 2):i], 300) if i else ""})
    return sorted(out, key=lambda c: c["at"])


SORT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["who", "names_post", "post"], "properties": {
    "who": {"type": "string", "enum": WHO},
    "names_post": {"type": "boolean", "description": "they refer to one of the author's posts (its topic, a word asked for in the comments, a quote, 'dit opslag')"},
    "post": {"type": "integer", "description": "the number of the post they name from the list, 0 if none or unclear"}}}
_WHO = ", ".join(p for p in (C.get("author", "name"), C.get("author", "business"), C.get("author", "pitch")) if p) or "the author"
SORT_SYSTEM = f"""You sort LinkedIn conversations opened by someone else in the inbox of {_WHO}. who: potential_client = a
business owner or manager who could buy what the author sells; agency = an agency or freelancer wanting to work with
the author (partnership, white-label, subcontracting); partner = someone offering or asking about referrals or
collaboration that could bring clients; peer = a fellow professional chatting; recruiter_or_job; sales_pitch = someone
selling something to the author; personal; other.
Some are old conversations they reopen after weeks of silence: the earlier messages are shown for context; judge who
they are from all of it, and whether the new messages refer to a post.
names_post: true only when they clearly refer to one of the author's LinkedIn posts. post: its number from the list, else 0."""


def sort_new(starts: list[dict], posts: list[dict]) -> dict:
    """Label every not-yet-sorted conversation once (cached). posts: the author's posts, newest first, for naming."""
    import llm_guard
    from anthropic import Anthropic
    done = json.loads(SORTED.read_text()) if SORTED.exists() else {}
    todo = [c for c in starts if c["key"] not in done]
    if not todo:
        return done
    client = llm_guard.guarded(Anthropic())
    for c in todo:
        recent = [p for p in posts if c["at"] - dt.timedelta(days=21) <= p["at"] <= c["at"]][:12]
        listing = "\n".join(f"{k}. ({p['at']:%d %b}) {p['text'][:160]}" for k, p in enumerate(recent, 1)) or "(no posts in the 3 weeks before)"
        convo = (f"Earlier in this conversation, before {REVIVE_DAYS}+ quiet days:\n{c['before']}\n\nWhat they wrote now:\n"
                 if c["kind"] == "revived" else "The conversation:\n") + c["text"][:1500]
        msg = client.messages.create(model=MODEL, max_tokens=200, system=SORT_SYSTEM,
                                     output_config={"format": {"type": "json_schema", "schema": SORT_SCHEMA}},
                                     messages=[{"role": "user", "content": f"The author's posts in the 3 weeks before:\n{listing}\n\n{convo}"}])
        r = json.loads(msg.content[0].text)
        k = r["post"] if r["names_post"] and 1 <= r["post"] <= len(recent) else 0
        done[c["key"]] = {"who": r["who"], "names_post": bool(k), "post_link": recent[k - 1]["link"] if k else "",
                          "at": c["at"].isoformat(), "kind": c["kind"]}
        SORTED.write_text(json.dumps(done, ensure_ascii=False))
    return done


def report(days: int) -> dict:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    eng = engine_posts()
    posts = []
    for s in pull_shares():
        at = _utc(s["date"] + (" UTC" if "UTC" not in s["date"] else "")) if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", s["date"]) else None
        if at and at >= since - dt.timedelta(days=21):
            urn = re.search(r"urn:li:(?:share|ugcPost|activity):\d+", s["link"])
            e = eng.get(urn.group(0)) if urn else None
            if not e:  # the export can carry the activity urn: match the engine post by its opening instead
                e = next((v for v in eng.values() if v["text"].strip()[:80] == s["text"].strip()[:80]), None)
            posts.append({**s, "at": at, "engine": e})
    # engine posts straight from the batch files: the exact time they went out, also before the export has them
    have = {(p["engine"] or {}).get("key") for p in posts}
    for f in C.BATCHES.glob("*Engine_Batch_*.json"):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name):
            continue
        b = re.search(r"Batch_(\d+)", f.name).group(1)
        for n, q in enumerate(json.loads(f.read_text()), 1):
            if isinstance(q, dict) and q.get("posted") and not q["posted"].get("deleted") and f"{b}.{n}" not in have:
                posts.append({"date": q["posted"]["at"], "link": q["posted"]["url"], "text": q["text"],
                              "at": dt.datetime.fromisoformat(q["posted"]["at"]), "engine": eng.get(q["posted"]["urn"])})
    posts.sort(key=lambda p: p["at"], reverse=True)
    starts = conversation_starts(since)
    labels = sort_new(starts, posts)
    rows = []
    for k, p in enumerate(sorted(posts, key=lambda p: p["at"])):
        if p["at"] < since:
            continue
        nxt = next((q["at"] for q in sorted(posts, key=lambda q: q["at"]) if q["at"] > p["at"]), None)
        end = min(p["at"] + dt.timedelta(hours=72), nxt) if nxt else p["at"] + dt.timedelta(hours=72)
        named = sum(1 for l in labels.values() if l.get("names_post") and l.get("post_link") == p["link"])
        after = [{**l, "kind": c["kind"]} for c in starts for l in [labels.get(c["key"], {})]
                 if p["at"] <= c["at"] < end and l.get("who") in RELEVANT]
        rows.append({"date": p["at"].astimezone(dt.timezone(dt.timedelta(hours=1))).strftime("%Y-%m-%d %H:%M"),
                     "source": "engine" if p["engine"] else "other", "post": p["engine"]["key"] if p["engine"] else "",
                     "opening": p["text"].strip().splitlines()[0][:90] if p["text"].strip() else "",
                     "named": named, "after": len(after),
                     "after_new": sum(1 for l in after if l["kind"] == "new"),
                     "after_revived": sum(1 for l in after if l["kind"] == "revived"),
                     "after_by": {w: sum(1 for l in after if l["who"] == w) for w in sorted(RELEVANT)}})
    summary = {}
    for src in ("engine", "other"):
        rs = [r for r in rows if r["source"] == src]
        per = lambda f: round(sum(r[f] for r in rs) / len(rs), 2) if rs else None
        summary[src] = {"posts": len(rs), "named_per_post": per("named"), "after_per_post": per("after"),
                        "after_new_per_post": per("after_new"), "after_revived_per_post": per("after_revived")}
    data = {"built": dt.datetime.now().isoformat(timespec="seconds"), "days": days, "summary": summary, "posts": rows}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=45)
    a = ap.parse_args()
    if not (C.get("linkedin", "profile_slug") and C.token("dma_token_file").exists() and (CACHE / "inbox.json").exists()):
        print("DMs per post: set [linkedin] profile_slug, save the DMA token and run linkedin_dma_sync.py --pull first")
        sys.exit(0)
    d = report(a.days)
    for r in d["posts"]:
        print(f"{r['date']}  {r['source']:6} {r['post']:5} named {r['named']}  relevant DMs in 72 h {r['after']}"
              f" ({r['after_new']} new, {r['after_revived']} reopened)  {r['opening']}")
    for src, s in d["summary"].items():
        print(f"{src}: {s['posts']} posts, {s['named_per_post']} DMs naming the post per post, {s['after_per_post']} relevant DMs"
              f" within 72 h per post ({s['after_new_per_post']} new, {s['after_revived_per_post']} reopened)"
              + (" (created time, not publish time: read loosely)" if src == "other" else ""))
