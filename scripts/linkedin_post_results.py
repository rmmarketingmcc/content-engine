#!/usr/bin/env python3
"""LinkedIn post results: which kind of post works, found from the numbers and put into the engine by itself.

The scorecard only counts the author's edits to drafts; this reads how posts did once they were out. It keeps every
post's numbers week after week, tags what each post showed and what it was about, and turns a clear gap between
groups into a rule. Every Monday the rules from the numbers are rewritten in their own block of the engine's rulebook
(data/kb/voice_rules.md, which the drafter and the interviewer read), and a rule drops out when the gap does. DMs
count more than reach: a reach gap the DMs contradict becomes a lean that keeps both, never a cut. The author's veto:
`answer <id> no` takes one out and keeps it out.

The numbers come from LinkedIn's Member Post Analytics API (the Community Management API product, on a LinkedIn app
of its own; never the posting app in linkedin_post.py). `tick` runs daily: it reads the posts up to 60 days old that
have no reading from the last 7 days (lifetime impressions, reactions, comments, reposts: 4 calls a post, newest
first), and on Mondays runs `check`. After every pull the numbers go into the engine itself: the "Live numbers per
post" block at the end of data/kb/what_works.md is rewritten (groups, the posts that brought DMs, reached the most and
the fewest), and content_engine_draft.py and content_engine_scout.py read that file before every batch. Engine posts
get their tags from their batch (format -> media, role -> topic); a tag set by hand is never overwritten, so `tag` is
only for posts made by hand.
LinkedIn's Development tier allows 100 calls per member and 500 per app in 24 h; this stops at [linkedin]
analytics_call_cap (80), and a post not read today is read tomorrow.
  python3 scripts/linkedin_post_results.py tag                       # posts still missing what they showed / were about
  python3 scripts/linkedin_post_results.py tag <slug words> --media photo --topic story
  python3 scripts/linkedin_post_results.py check                     # groups, gaps, and the rules they make
  the veto: python3 scripts/linkedin_post_results.py answer <id> no [--note "..."]   (yes lifts it again)
`due` prints one line when something waits for a person; silent otherwise.

Connecting (once, and again if the token runs out without a refresh token):
  app      run twice: first with the app's client id on the clipboard, then the secret (never printed, clipboard cleared)
  auth     LinkedIn's consent page opens, the author clicks Allow, the token goes to tokens/<linkedin.analytics_token_file>
  status   whether the token works and how many days it has left

Which posts: the API reads numbers for a post we name, it can't list them. Known posts are every post the engine put
out (its batches and data/state/linkedin_posts.jsonl), every post from an export, and any added with `add <link>`.
Each pull compares them with LinkedIn's own total for all posts; `check` says when a share sits on posts we don't know
(posted by hand). LinkedIn's analytics export (`ingest "<the AggregateAnalytics_..._.xlsx>"`) works next to it; it
lists at most 50 posts ranked by impressions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import secrets
import statistics
import subprocess
import sys
import time
import urllib.parse
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

STORE = C.STATE / "linkedin_post_results.json"
DMS = C.STATE / "linkedin_post_dms.json"
BATCHES = C.BATCHES
POSTS_LOG = C.STATE / "linkedin_posts.jsonl"
APP = C.token("analytics_app_file")
TOKEN = C.token("analytics_token_file")
PORT = int(C.get("linkedin", "analytics_port"))  # its own port, next to the posting app's
REDIRECT = f"http://localhost:{PORT}/linkedin/callback"
SCOPES = "r_member_postAnalytics"
VERSION = C.get("linkedin", "api_version")
API = "https://api.linkedin.com/rest/memberCreatorPostAnalytics"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
METRICS = {"impressions": "IMPRESSION", "reactions": "REACTION", "comments": "COMMENT", "reposts": "RESHARE"}
READ_DAYS = 60     # posts this young get a reading every week; older ones keep their last
EVERY = 7          # days between two readings of one post
UNKNOWN = 0.8      # known posts under 80 % of LinkedIn's own total for the same days: posts we don't know about
CALL_CAP = int(C.get("linkedin", "analytics_call_cap"))  # the Development tier allows 100 per member in 24 h
CALLS = C.STATE / "linkedin_api_calls.json"  # when each call went out, for the cap

MEDIA = {
    "photo": "Photo of you",
    "video": "Video",
    "face_graphic": "Graphic with your face",
    "screenshot": "Screenshot of real numbers",
    "graphic": "Made graphic (text or numbers)",
    "illustration": "Drawn illustration",
    "carousel": "Carousel",
    "newsletter": "Newsletter article",
    "text": "Text only",
}
TOPICS = {"story": "Personal story", "tip": "Tip or how-to", "proof": "Client result", "offer": "Offer or guide"}
ENGINE_FORMAT = {"photo": "photo", "video": "video", "graphic": "graphic", "carousel": "carousel"}
ENGINE_ROLE = {"personal": "story", "problem and fix": "tip", "get something": "offer", "offer": "offer"}
WORKS = C.KB / "what_works.md"  # the drafter and the scout read it before every batch
LIVE = ("<!-- live: written by scripts/linkedin_post_results.py after every pull; edits here are overwritten -->",
        "<!-- /live -->")
MIN_AGE = 4        # days: younger posts are still collecting views
MIN_POSTS = 5      # per group before a gap counts
GAP = 0.6          # a group at or under 60 % of the best group's typical post is a finding
MIX = 0.3          # topic shares 30+ points apart between two media groups: picture and topic can't be told apart
DM_GAP = 0.5       # a group at or under half the other's DMs per post is a DM gap ...
MIN_DMS = 5        # ... once the better group has brought at least 5 DMs
RULES = C.KB / "voice_rules.md"  # the engine's rulebook: drafter and interviewer read it
AUTO = ("<!-- numbers: written by scripts/linkedin_post_results.py every Monday from LinkedIn's numbers; edits here "
        "are overwritten -->", "<!-- /numbers -->")
DUE_DAYS = 7


def load() -> dict:
    if STORE.exists():
        return json.loads(STORE.read_text())
    return {"posts": {}, "ingests": [], "findings": {}}


def save(d: dict) -> None:
    STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def post_id(url: str) -> str:
    m = re.search(r"(?:share|ugcPost|activity)[-:](\d{19})", url or "")
    return m.group(1) if m else ""


def slug(url: str) -> str:
    s = urllib.parse.unquote((url or "").rstrip("/").split("/")[-1])
    s = s.split("_", 1)[-1]
    return re.sub(r"-(?:share|ugcPost|activity)-\d{19}.*$", "", s)


def words(s: str) -> list[str]:
    return re.findall(r"[0-9a-zæøåéü]+", (s or "").lower())


def _date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%m/%d/%Y").date()


def engine_tags() -> dict[str, dict]:
    """post id -> {media, topic} for every engine post that went out: its planned format and its role."""
    out = {}
    for f in BATCHES.glob("*Engine_Batch_*.json"):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name):
            continue
        for p in json.loads(f.read_text()):
            if isinstance(p, dict) and p.get("posted"):
                out[post_id(p["posted"].get("urn", ""))] = {"media": ENGINE_FORMAT.get(p.get("format", ""), ""),
                                                            "topic": ENGINE_ROLE.get(p.get("role", ""), "")}
    return out


def _engine_tag(p: dict, t: dict | None) -> None:
    """Fills what the engine knows; a tag set by hand is never overwritten."""
    if not t or str(p.get("tagged_by", "")).startswith("session"):
        return
    if not p["media"] and t["media"]:
        p["media"], p["tagged_by"] = t["media"], "engine"
    if not p["topic"] and t["topic"]:
        p["topic"], p["tagged_by"] = t["topic"], "engine"


def ingest(path: pathlib.Path, d: dict) -> str:
    import warnings
    import openpyxl
    warnings.simplefilter("ignore")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    period = next(wb["DISCOVERY"].iter_rows(values_only=True))[1]  # ('Overall Performance', '10/14/2026 - 11/10/2026')
    start, end = (dt.datetime.strptime(x.strip(), "%m/%d/%Y").date() for x in period.split("-"))
    rows = list(wb["TOP POSTS"].iter_rows(values_only=True))[3:]
    eng = {r[0]: int(r[2]) for r in rows if r[0]}
    listed = [(r[4], _date(r[5]), int(r[6])) for r in rows if r[4]]
    eng_tags, n_new, n_read = engine_tags(), 0, 0
    for url, published, imp in listed:
        pid = post_id(url)
        if not pid:
            continue
        p = d["posts"].setdefault(pid, {"published": published.isoformat(), "slug": slug(url), "url": url.split("?")[0],
                                        "media": "", "topic": "", "tagged_by": "", "readings": []})
        n_new += not p["readings"]
        _engine_tag(p, eng_tags.get(pid))
        if published < start:  # published before the window: this export only holds part of its views
            continue
        p["readings"] = [r for r in p["readings"] if r["on"] != end.isoformat()]
        p["readings"].append({"on": end.isoformat(), "age": (end - published).days, "impressions": imp,
                              "engagements": eng.get(url, 0)})
        n_read += 1
    cut = min((x[2] for x in listed), default=0)
    d["ingests"].append({"on": dt.date.today().isoformat(), "file": path.name, "window": f"{start}..{end}",
                         "listed": len(listed), "cut": cut if len(listed) >= 50 else 0})
    return f"{path.name}: window {start} to {end}, {len(listed)} posts listed, {n_read} full readings, {n_new} new posts"


# ---------- the API pull ----------
class RateLimited(Exception):
    pass


def _private(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=1))
    path.chmod(0o600)


def id_date(pid: str) -> dt.date:
    """A LinkedIn post id carries its own time: the first 41 bits are milliseconds since 1970."""
    return dt.datetime.fromtimestamp((int(pid) >> 22) / 1000, ZoneInfo(C.get("plan", "timezone"))).date()


def store_app(reset: bool) -> None:
    """Run twice: the client id on the clipboard first, then the secret. The secret is never printed."""
    app = {} if reset or not APP.exists() else json.loads(APP.read_text())
    clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if len(clip) < 8 or any(c.isspace() for c in clip):
        sys.exit("the clipboard doesn't hold a single value: copy it on the app's Auth tab first")
    if not app.get("client_id"):
        if clip.startswith("WPL_") or len(clip) > 20:  # a secret, copied first: keep it off the screen and the file
            subprocess.run(["pbcopy"], input="", text=True)
            sys.exit("that looks like the secret, not the client id: clipboard cleared, copy the client id first")
        _private(APP, {"client_id": clip})
        print(f"client id stored (ends ...{clip[-4:]}); now copy the client secret and run `app` again")
        return
    if clip == app["client_id"]:
        sys.exit("the clipboard still holds the client id: copy the secret")
    _private(APP, {**app, "client_secret": clip})
    subprocess.run(["pbcopy"], input="", text=True)
    print(f"secret stored in {APP} (not shown); clipboard cleared. Next: auth")


def _tok_record(got: dict, old: dict | None = None) -> dict:
    now, old = int(time.time()), old or {}
    rec = {"access_token": got["access_token"], "expires_at": now + int(got["expires_in"]), "scope": got.get("scope", ""),
           "refresh_token": old.get("refresh_token", ""), "refresh_expires_at": old.get("refresh_expires_at", 0),
           "obtained": old.get("obtained") or dt.datetime.now().isoformat(timespec="seconds")}
    if got.get("refresh_token"):
        rec["refresh_token"] = got["refresh_token"]
        rec["refresh_expires_at"] = now + int(got.get("refresh_token_expires_in", 0))
    return rec


def auth(timeout: int = 900) -> None:
    import http.server
    import requests
    app = json.loads(APP.read_text()) if APP.exists() else {}
    if not app.get("client_secret"):
        sys.exit("the app isn't stored yet: run `app` (client id, then the secret, copied to the clipboard)")
    state, got = secrets.token_urlsafe(24), {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/linkedin/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            ok = got.get("state") == state and "code" in got
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "LinkedIn post numbers are connected. You can close this tab." if ok else \
                "It didn't connect: " + got.get("error_description", got.get("error", "unknown"))
            self.wfile.write(f"<p style='font:16px -apple-system,sans-serif;margin:40px'>{msg}</p>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Callback)
    server.timeout = 5
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": app["client_id"], "redirect_uri": REDIRECT, "state": state, "scope": SCOPES})
    subprocess.run(["open", url])
    print("LinkedIn's consent page is open in the browser: click Allow", flush=True)
    end = time.time() + timeout
    while not got and time.time() < end:
        server.handle_request()
    server.server_close()
    if got.get("state") != state or "code" not in got:
        sys.exit(f"no consent came back: {got.get('error_description') or got.get('error') or 'timed out'}")
    r = requests.post(TOKEN_URL, timeout=30, data={"grant_type": "authorization_code", "code": got["code"],
                      "redirect_uri": REDIRECT, "client_id": app["client_id"], "client_secret": app["client_secret"]})
    if not r.ok:
        sys.exit(f"LinkedIn refused the code: {r.status_code} {r.text[:300]}")
    _private(TOKEN, _tok_record(r.json()))
    status()


def _token() -> dict:
    import requests
    if not TOKEN.exists():
        sys.exit("LinkedIn post numbers aren't connected yet: run `linkedin_post_results.py auth`")
    tok = json.loads(TOKEN.read_text())
    now = time.time()
    if tok["expires_at"] < now + 7 * 86400 and tok.get("refresh_token") and tok.get("refresh_expires_at", 0) > now:
        app = json.loads(APP.read_text())
        r = requests.post(TOKEN_URL, timeout=30, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                          "client_id": app["client_id"], "client_secret": app["client_secret"]})
        if r.ok:
            tok = _tok_record(r.json(), tok)
            _private(TOKEN, tok)
    if tok["expires_at"] < now + 3600:
        sys.exit("the LinkedIn post-numbers token has run out: run `linkedin_post_results.py auth` (Allow once)")
    return tok


def _range(start: dt.date, end: dt.date) -> str:
    """Rest.li 2.0 date range, start inclusive, end exclusive."""
    f = lambda x: f"(day:{x.day},month:{x.month},year:{x.year})"  # noqa: E731
    return f"(start:{f(start)},end:{f(end)})"


def _entity(urn: str) -> str:
    return f"({'ugc' if urn.split(':')[2] == 'ugcPost' else 'share'}:{urllib.parse.quote(urn, safe='')})"


def _calls() -> list[int]:
    try:
        return [t for t in json.loads(CALLS.read_text()) if t > time.time() - 86400]
    except (OSError, ValueError):
        return []


def _count(tok: dict, query: str, calls: list[int]) -> int:
    """One number from memberCreatorPostAnalytics. The query goes in as LinkedIn's docs write it (Rest.li 2.0).
    Every call, retries too, counts toward our cap of 80 in 24 h."""
    import requests
    for attempt in range(3):
        if len(calls) >= CALL_CAP:
            raise RateLimited(f"our cap of {CALL_CAP} calls in 24 h (LinkedIn allows 100)")
        calls.append(int(time.time()))
        r = requests.get(f"{API}?{query}", timeout=60, headers={"Authorization": f"Bearer {tok['access_token']}",
                         "Linkedin-Version": VERSION, "X-Restli-Protocol-Version": "2.0.0"})
        if r.status_code < 500:
            break
        time.sleep(5 * (attempt + 1))
    if r.status_code == 429:
        raise RateLimited(r.text[:200])
    if r.status_code in (401, 403):
        sys.exit(f"LinkedIn refused the token ({r.status_code}): {r.text[:300]}")
    if not r.ok:
        raise LookupError(f"{r.status_code} {r.text[:200]}")
    return sum(int(e.get("count") or 0) for e in r.json().get("elements", []))


def _new_post(d: dict, pid: str, urn: str, url: str = "", text: str = "") -> dict:
    s = slug(url) if "/posts/" in url else "-".join(words(text)[:8]) or pid
    return d["posts"].setdefault(pid, {"published": id_date(pid).isoformat(), "slug": s, "urn": urn,
                                       "url": (url or f"https://www.linkedin.com/feed/update/{urn}/").split("?")[0],
                                       "media": "", "topic": "", "tagged_by": "", "readings": []})


def known(d: dict) -> dict[str, str]:
    """post id -> URN for every post we know; engine posts the store hasn't seen are added to it."""
    for f in BATCHES.glob("*Engine_Batch_*.json"):
        if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name):
            for p in json.loads(f.read_text()):
                urn = (p.get("posted") or {}).get("urn", "") if isinstance(p, dict) else ""
                if post_id(urn):
                    _new_post(d, post_id(urn), urn, text=p.get("text", ""))
    for line in POSTS_LOG.read_text().splitlines() if POSTS_LOG.exists() else []:
        urn = json.loads(line).get("urn", "")
        if post_id(urn):
            _new_post(d, post_id(urn), urn)
    eng_tags = engine_tags()
    out = {}
    for pid, p in d["posts"].items():
        _engine_tag(p, eng_tags.get(pid))
        kind = "ugcPost" if "ugcPost" in (p.get("urn") or p["url"]) else "share"
        if "activity" not in (p.get("urn") or p["url"]):
            out[pid] = f"urn:li:{kind}:{pid}"
    return out


def pull(d: dict) -> str:
    """Reads the posts up to 60 days old with no reading from the last 7 days, newest first, until the cap; then
    LinkedIn's own total over the same days, to see how much sits on posts we don't know."""
    tok, today, calls = _token(), dt.date.today(), _calls()
    urns = known(d)
    young = [pid for pid in urns if 0 <= (today - dt.date.fromisoformat(d["posts"][pid]["published"])).days <= READ_DAYS]
    last_api = lambda p: max((r["on"] for r in p["readings"] if r.get("source") == "api"), default="")  # noqa: E731
    todo = sorted((pid for pid in young if last_api(d["posts"][pid]) <= (today - dt.timedelta(days=EVERY)).isoformat()),
                  key=lambda pid: d["posts"][pid]["published"], reverse=True)
    start = min((dt.date.fromisoformat(d["posts"][pid]["published"]) for pid in young), default=today)
    read, skipped, stopped, total = 0, [], "", 0
    try:
        for pid in todo:
            p = d["posts"][pid]
            try:
                m = {k: _count(tok, f"q=entity&entity={_entity(urns[pid])}&queryType={q}", calls) for k, q in METRICS.items()}
            except LookupError as e:  # a deleted post, say: the others still get read
                skipped.append(f"{p['slug'][:40]} ({str(e)[:60]})")
                continue
            p["readings"] = [r for r in p["readings"] if r["on"] != today.isoformat()]
            p["readings"].append({"on": today.isoformat(), "age": (today - dt.date.fromisoformat(p["published"])).days,
                                  "impressions": m["impressions"], "engagements": m["reactions"] + m["comments"] + m["reposts"],
                                  **{k: m[k] for k in ("reactions", "comments", "reposts")}, "source": "api"})
            read += 1
        if young:
            total = _count(tok, f"q=me&queryType=IMPRESSION&dateRange={_range(start, today + dt.timedelta(days=1))}", calls)
    except RateLimited as e:
        stopped = f" Stopped at {e}; tomorrow's run reads the rest."
    finally:
        CALLS.write_text(json.dumps(calls))
    ours = sum(latest(d["posts"][pid])["impressions"] for pid in young if d["posts"][pid]["readings"])
    d["ingests"].append({"on": today.isoformat(), "file": "api", "window": f"{start}..{today}", "listed": read,
                         "of": len(todo), "account_impressions": total, "known_impressions": ours})
    save(d)
    return (f"API pull {today}: {read} of the {len(todo)} posts due read ({len(young)} posts up to {READ_DAYS} days old), "
            f"{len(calls)} calls in the last 24 h (cap {CALL_CAP})." +
            (f" Not read: {'; '.join(skipped)}." if skipped else "") + stopped)


def add(d: dict, url: str) -> None:
    pid = post_id(url)
    if not pid or "activity" in url:
        sys.exit("that isn't a post link with its share or ugcPost id: on the post, ... > Copy link to post")
    urn = f"urn:li:{'ugcPost' if 'ugcPost' in url else 'share'}:{pid}"
    p = _new_post(d, pid, urn, url=url)
    save(d)
    print(f"{p['published']} {p['slug'][:50]}: known now, the next pull reads it")


def status() -> None:
    tok, today, calls = _token(), dt.date.today(), _calls()
    try:
        n = _count(tok, f"q=me&queryType=IMPRESSION&dateRange={_range(today - dt.timedelta(days=7), today + dt.timedelta(days=1))}", calls)
        works = f"the token works: {n} impressions on all your posts in the last 7 days"
    except RateLimited:  # the day's calls are used up; the token itself is still fine
        works = "today's calls are used up, so the token wasn't tried"
    finally:
        CALLS.write_text(json.dumps(calls))
    left = (tok["expires_at"] - time.time()) / 86400
    renews = tok.get("refresh_token") and tok.get("refresh_expires_at", 0) > time.time()
    print(f"{works} ({len(calls)} of {CALL_CAP} calls "
          f"used in 24 h). {left:.0f} days left (until "
          f"{dt.datetime.fromtimestamp(tok['expires_at']):%d %b %Y})" +
          (f", renews itself until {dt.datetime.fromtimestamp(tok['refresh_expires_at']):%d %b %Y}." if renews else
           ", no refresh token: `auth` again before then."))


def latest(p: dict) -> dict | None:
    return max(p["readings"], key=lambda r: r["on"]) if p["readings"] else None


def usable(d: dict) -> list[dict]:
    out = []
    for pid, p in d["posts"].items():
        r = latest(p)
        if r and r["age"] >= MIN_AGE:
            out.append({**p, "id": pid, "impressions": r["impressions"], "engagements": r["engagements"]})
    return out


def _dm_entries(posts: list[dict]) -> dict[str, dict]:
    """post id -> its row in linkedin_post_dms.py's file (DM conversations within 72 h), matched on the opening words."""
    if not DMS.exists():
        return {}
    out = {}
    for x in json.loads(DMS.read_text()).get("posts", []):
        w = words(x.get("opening", ""))[:4]
        for p in posts:
            if w and words(p["slug"])[:4] == w:
                out[p["id"]] = x
    return out


def dms_by_post(posts: list[dict]) -> dict[str, int]:
    return {pid: int(x.get("after") or 0) for pid, x in _dm_entries(posts).items()}


def groups(posts: list[dict], key: str, dms: dict[str, int]) -> dict[str, dict]:
    out = {}
    for g in sorted({p[key] for p in posts if p[key]}):
        ps = [p for p in posts if p[key] == g]
        out[g] = {"n": len(ps), "imp": statistics.median(p["impressions"] for p in ps),
                  "eng": statistics.median(p["engagements"] for p in ps),
                  "dms": sum(dms.get(p["id"], 0) for p in ps), "posts": ps}
    return out


def topic_mix(ps: list[dict]) -> dict[str, float]:
    tagged = [p["topic"] for p in ps if p["topic"]]
    return {t: tagged.count(t) / len(tagged) for t in set(tagged)} if tagged else {}


def findings(posts: list[dict], dms: dict[str, int]) -> list[dict]:
    """Every pair of groups (5+ posts each) with a clear gap in reach or in DMs per post, as a rule for the engine.
    These go into the rulebook by themselves (no yes/no first), and drop out again when the numbers stop showing the
    gap. DMs count most: a reach gap the DMs contradict is never a rule to cut the group, it becomes a "lean" that
    keeps both."""
    out = []
    for key, names in (("media", MEDIA), ("topic", TOPICS)):
        big = {g: v for g, v in groups(posts, key, dms).items() if v["n"] >= MIN_POSTS}
        rate = {g: v["dms"] / v["n"] for g, v in big.items()}
        gs = sorted(big)
        for i, a in enumerate(gs):
            for b in gs[i + 1:]:
                hi_r, lo_r = (a, b) if big[a]["imp"] >= big[b]["imp"] else (b, a)
                hi_d, lo_d = (a, b) if rate[a] >= rate[b] else (b, a)
                reach = big[lo_r]["imp"] <= GAP * big[hi_r]["imp"]
                dm = big[hi_d]["dms"] >= MIN_DMS and rate[lo_d] <= DM_GAP * rate[hi_d]
                if not reach and not dm:
                    continue
                n = lambda g: names[g].lower()  # noqa: E731
                x = big[hi_r]["imp"] / big[lo_r]["imp"] if big[lo_r]["imp"] else 9
                times = f"{x:.1f}".rstrip("0").rstrip(".")
                dpp = lambda g: f"{rate[g]:.1f}"  # noqa: E731
                if reach and dm and hi_r == hi_d:
                    kind, rule = "both", (f"Prefer {n(hi_r)} over {n(lo_r)}: those posts reach about {times}x as many "
                                          f"people and bring {dpp(hi_r)} DMs per post against {dpp(lo_r)}.")
                elif reach and dm:
                    kind, rule = "lean", (f"{names[hi_d]} posts bring {dpp(hi_d)} DMs per post against {dpp(lo_d)} for "
                                          f"{n(lo_d)}, while {n(hi_r)} posts reach about {times}x as many people. Lean "
                                          f"the batch toward {n(hi_d)} for DMs and keep {n(hi_r)} for reach; where one "
                                          f"post can be both, make it both.")
                elif dm:
                    kind, rule = "dms", (f"For DMs, prefer {n(hi_d)} over {n(lo_d)}: {dpp(hi_d)} DMs per post against "
                                         f"{dpp(lo_d)}, with about the same reach.")
                elif big[lo_r]["dms"] and rate[lo_r] >= rate[hi_r]:
                    continue  # more reach, but no more DMs: DMs count, so nothing to cut
                else:
                    kind, rule = "reach", (f"For reach, prefer {n(hi_r)} over {n(lo_r)}: about {times}x as many people, "
                                           f"and DMs per post {dpp(hi_r)} against {dpp(lo_r)}.")
                f = {"key": f"{key}:{a}|{b}", "kind": kind, "on": key, "groups": [a, b],
                     "text": (f"{names[a]}: {big[a]['n']} posts, typical {big[a]['imp']:.0f} impressions, {dpp(a)} DMs per "
                              f"post. {names[b]}: {big[b]['n']} posts, typical {big[b]['imp']:.0f}, {dpp(b)} DMs per post."),
                     "rule": rule}
                if key == "media":  # is it the picture, or what those posts were about?
                    ma, mb = topic_mix(big[a]["posts"]), topic_mix(big[b]["posts"])
                    if any(abs(ma.get(t, 0) - mb.get(t, 0)) >= MIX for t in set(ma) | set(mb)):
                        f["caveat"] = (f"Part of it may be the topic: {n(a)} posts were {_mix(ma)}; {n(b)} posts "
                                       f"{_mix(mb)}.")
                if sum(dms.values()) < 20 and kind != "reach":
                    f["caveat_dm"] = f"Only {sum(dms.values())} DMs in all, so the DM side is still thin."
                out.append(f)
    return out


def _mix(m: dict[str, float]) -> str:
    return ", ".join(f"{TOPICS.get(t, t).lower()} {round(s * 100)} %" for t, s in sorted(m.items(), key=lambda x: -x[1]))


def _hook(p: dict, entry: dict | None) -> str:
    t = re.sub(r"\s+", " ", (entry or {}).get("opening") or p["slug"].replace("-", " ")).strip().strip('"“”')
    return t if len(t) <= 90 else t[:90].rsplit(" ", 1)[0] + " ..."


def _n(n: int, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


def live_block(d: dict) -> str:
    """The numbers per post, for the drafter and the scout: groups, then the posts that did best and worst."""
    posts = usable(d)
    if not posts:
        return ""
    ents = _dm_entries(posts)
    dms = {pid: int(x.get("after") or 0) for pid, x in ents.items()}
    read = max((i["on"] for i in d["ingests"] if i.get("file") == "api"), default="?")
    out = [LIVE[0], f"## Live numbers per post (LinkedIn's API, read {read})",
           f"{len(posts)} posts at least {MIN_AGE} days old. Impressions and engagements (reactions, comments, reposts) are "
           "each post's total so far. DMs are the conversations in the 72 h after it, and they are what counts. A group "
           f"of under {MIN_POSTS} posts is a hint, not a pattern. Picture and topic go together (most photos are stories), "
           "so a gap between two groups can come from either. A clear gap becomes a rule by itself every Monday, in the "
           "\"From the numbers\" block of the rulebook."]
    for key, names, title in (("media", MEDIA, "By what the post showed"), ("topic", TOPICS, "By what it was about")):
        gs = groups(posts, key, dms)
        if gs:
            out.append(f"\n{title} (the typical post):")
            out += [f"- {names.get(g, g)}: {_n(v['n'], 'post')}, {v['imp']:.0f} impressions, {v['eng']:.0f} engagements, "
                    f"{v['dms'] / v['n']:.1f} DMs per post" for g, v in sorted(gs.items(), key=lambda x: -x[1]["imp"])]

    def row(p: dict) -> str:
        tags = "/".join(x for x in (MEDIA.get(p["media"], ""), TOPICS.get(p["topic"], "")) if x).lower() or "not tagged"
        leads = ((ents.get(p["id"]) or {}).get("after_by") or {}).get("potential_client", 0)
        return (f"- \"{_hook(p, ents.get(p['id']))}\" ({dt.date.fromisoformat(p['published']):%-d %b}, {tags}): "
                f"{_n(dms.get(p['id'], 0), 'DM')}" + (f" ({leads} from {'a potential client' if leads == 1 else 'potential clients'})"
                                                       if leads else "") +
                f", {p['impressions']} impressions, {p['engagements']} engagements")
    top_dm = [p for p in sorted(posts, key=lambda p: (-dms.get(p["id"], 0), -p["impressions"])) if dms.get(p["id"])][:5]
    by_imp = sorted(posts, key=lambda p: -p["impressions"])
    for title, ps in (("Brought the most DMs", top_dm), ("Reached the most people", by_imp[:5]),
                      ("Reached the fewest", by_imp[-3:][::-1] if len(by_imp) > 8 else [])):
        if ps:
            out += [f"\n{title}:"] + [row(p) for p in ps]
    try:
        sm = json.loads(DMS.read_text())["summary"]
        e, o = sm["engine"], sm["other"]
        out.append(f"\nThe engine's posts against the rest (last {json.loads(DMS.read_text())['days']} days): "
                   f"{e['after_per_post']:.1f} vs {o['after_per_post']:.1f} DMs per post ({e['posts']} vs {o['posts']} posts; "
                   f"new conversations alone {e['after_new_per_post']:.2f} vs {o['after_new_per_post']:.2f}).")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "\n".join(out + [LIVE[1]])


def write_works(d: dict, path: pathlib.Path = WORKS) -> bool:
    """Puts the live block in what_works.md (replacing the last one), so the next batch is drafted from real numbers."""
    block = live_block(d)
    if not block or not path.exists():
        return False
    text = path.read_text()
    if LIVE[0] in text and LIVE[1] in text:
        head, rest = text.split(LIVE[0], 1)
        new = head + block + rest.split(LIVE[1], 1)[1]
    else:
        new = text.rstrip() + "\n\n" + block + "\n"
    if new != text:
        path.write_text(new)
    return new != text


def check(d: dict, quiet: bool = False) -> None:
    """quiet: for the job's log: only which questions wait."""
    posts = usable(d)
    if not posts:
        sys.exit("no posts with a reading yet: run `pull` (or `ingest` an export) first")
    say = (lambda *a, **k: None) if quiet else print  # noqa: E731
    dms = dms_by_post(posts)
    last = d["ingests"][-1] if d["ingests"] else {}
    src = "API pull" if last.get("file") == "api" else "export"
    say(f"Posts with numbers, at least {MIN_AGE} days old: {len(posts)} (latest {src} {last.get('window', '?')})")
    if last.get("cut"):
        say(f"Blind spot: posts under {last['cut']} impressions in that window are not in the export.")
    if last.get("of", 0) > last.get("listed", 0):
        say(f"Only {last['listed']} of the {last['of']} posts due were read (call cap): the rest come with the next run.")
    acc = next((i for i in reversed(d["ingests"]) if i.get("account_impressions")), {})
    if acc and acc["known_impressions"] < UNKNOWN * acc["account_impressions"]:
        say(f"Blind spot: LinkedIn counts {acc['account_impressions']} impressions on all your posts in {acc['window']}, "
            f"the posts we know {acc['known_impressions']}. The rest sits on posts we don't know (posted by hand?) or on "
            "older posts still getting views. Add one: linkedin_post_results.py add <post link>")
    untagged = [p for p in posts if not p["media"] or not p["topic"]]
    if untagged:
        say(f"Not tagged yet: {len(untagged)} posts (run `tag`); they are left out of the groups below.")
    for key, names in (("media", MEDIA), ("topic", TOPICS)):
        say(f"\nBy {'what the post showed' if key == 'media' else 'what it was about'}:")
        for g, v in sorted(groups(posts, key, dms).items(), key=lambda x: -x[1]["imp"]):
            say(f"  {names.get(g, g):32} {v['n']:3} posts   typical {v['imp']:6.0f} impressions  {v['eng']:4.0f} engagements"
                f"   {v['dms']} DMs after")
    total_dms = sum(dms.values())
    say(f"\nDMs within 72 h of a post: {total_dms} in all" + (" (too few to compare groups)" if total_dms < 10 else ""))
    recent = [p for p in d["posts"].values() if p["media"]
              and p["published"] >= (dt.date.today() - dt.timedelta(days=28)).isoformat()]
    face = [p for p in recent if p["media"] in ("photo", "video", "face_graphic")]
    if recent:
        say(f"Your face on {len(face)} of the {len(recent)} tagged posts of the last 28 days (the drafter aims for at least half).")
    fs = findings(posts, dms)
    now = {f["key"] for f in fs}
    for k, f in d["findings"].items():  # a gap the numbers no longer show drops out of the rulebook by itself
        if k not in now and f.get("status") in ("auto", "open"):  # "open": an older finding format
            f.update(status="gone", gone=dt.date.today().isoformat())
    applied = []
    for f in fs:
        old = d["findings"].get(f["key"])
        if old and old["status"] == "no":  # a veto stands until it is lifted
            continue
        fid = old["id"] if old else f"L{len(d['findings']) + 1}"
        d["findings"][f["key"]] = {**f, "id": fid, "status": "auto", "first_seen": (old or {}).get("first_seen",
                                   dt.date.today().isoformat()), "last_seen": dt.date.today().isoformat()}
        applied.append(d["findings"][f["key"]])
        say(f"\n[{fid}] in the rulebook by itself: {f['rule']}" + "".join(f" {f[c]}" for c in ("caveat", "caveat_dm") if f.get(c)))
    if not applied:
        say("\nNo gap clear enough for a rule.")
    changed = write_rules(d)
    if changed:
        d["rules_note"] = {"on": dt.date.today().isoformat(), "rules": [f"[{f['id']}] {f['rule']}" for f in applied],
                           "shown": False}
        msg = f"rulebook updated: {len(applied)} rule(s) from the numbers"
        print(msg) if quiet else say(f"\n{msg} (stop one: linkedin_post_results.py answer <id> no)")
    save(d)
    write_works(d)


def tag(d: dict, match: str | None, media: str | None, topic: str | None) -> None:
    if not match:
        todo = sorted((p for p in d["posts"].values() if p["readings"] and (not p["media"] or not p["topic"])),
                      key=lambda p: p["published"], reverse=True)
        for p in todo:
            print(f"{p['published']}  media={p['media'] or '?':12} topic={p['topic'] or '?':6}  {p['slug'][:60]}")
        print(f"{len(todo)} to tag. media: {', '.join(MEDIA)}; topic: {', '.join(TOPICS)}")
        return
    if media and media not in MEDIA or topic and topic not in TOPICS:
        sys.exit(f"media is one of {', '.join(MEDIA)}; topic one of {', '.join(TOPICS)}")
    hits = [p for p in d["posts"].values() if words(match) == words(p["slug"])[:len(words(match))]]
    if len(hits) != 1:
        sys.exit(f"{len(hits)} posts start with '{match}': " + "; ".join(p["slug"][:50] for p in hits[:5]))
    p = hits[0]
    p["media"] = media or p["media"]
    p["topic"] = topic or p["topic"]
    p["tagged_by"] = f"session {dt.date.today()}"  # "session": set by hand, never overwritten
    save(d)
    print(f"{p['published']} {p['slug'][:50]}: media={p['media']} topic={p['topic']}")


def rules_block(d: dict) -> str:
    live = sorted((f for f in d["findings"].values() if f.get("status") == "auto"), key=lambda f: f["id"])
    read = max((f["last_seen"] for f in live), default="")
    return "\n".join([AUTO[0], f"## From the numbers (LinkedIn, read {read or 'not yet'}; in by themselves, out again "
                       "when the numbers stop showing it)",
                       "A lean, never a ban: the author's own rules above win, and a batch still mixes formats. DMs "
                       "count more than reach."]
                      + [f"- [{f['id']}] {f['rule']}" + "".join(f" {f[c]}" for c in ("caveat", "caveat_dm") if f.get(c))
                         for f in live] + ([] if live else ["- (no gap clear enough yet)"]) + [AUTO[1]])


def write_rules(d: dict, path: pathlib.Path = RULES) -> bool:
    """The rules from the numbers into the engine's rulebook, replacing the last set."""
    if not path.exists():
        return False
    text, block = path.read_text(), rules_block(d)
    if AUTO[0] in text and AUTO[1] in text:
        head, rest = text.split(AUTO[0], 1)
        new = head + block + rest.split(AUTO[1], 1)[1]
    else:
        new = text.rstrip() + "\n\n" + block + "\n"
    if new != text:
        path.write_text(new)
    return new != text


def answer(d: dict, fid: str, yes: bool, note: str) -> None:
    """The veto: `no` takes a rule out of the rulebook and keeps it out; `yes` lifts the veto."""
    f = next((f for f in d["findings"].values() if f["id"] == fid), None)
    if not f:
        sys.exit(f"no finding {fid}")
    f.update(status="auto" if yes else "no", answered=dt.date.today().isoformat(), note=note)
    write_rules(d)
    save(d)
    print(f"{fid}: " + ("back in the rulebook" if yes else "out of the rulebook, and it stays out") + f": {f['rule']}")


def due(d: dict) -> None:
    try:  # the posts planned ahead, once only a few are left
        rw = json.loads((C.STATE / "runway.json").read_text())
        if rw["ahead"] <= int(C.get("plan", "runway_min")):
            print(f"LinkedIn: only {rw['ahead']} post(s) planned ahead (last {rw['last_date'] or 'none'}; waiting for you: "
                  f"{', '.join(f'{c} on {w}' for w, c in rw['waiting'].items()) or 'nothing'}). Fill the plan.")
    except (OSError, ValueError, KeyError):
        pass
    tok = json.loads(TOKEN.read_text()) if TOKEN.exists() else None
    last = max((i["on"] for i in d["ingests"] if not tok or i.get("file") == "api"), default="")
    days = (dt.date.today() - dt.date.fromisoformat(last)).days if last else 99
    rn = d.get("rules_note") or {}
    if rn and not rn.get("shown"):  # once, so the author hears what the engine now leans on
        print(f"LinkedIn: the content engine's rulebook changed from the numbers on {rn['on']} (no action needed): "
              + " ".join(rn["rules"]) + " To stop one: linkedin_post_results.py answer <id> no")
        rn["shown"] = True
        save(d)
    if tok:
        ends, now = tok["expires_at"], time.time()
        renews = tok.get("refresh_token") and tok.get("refresh_expires_at", 0) > ends
        if not renews and ends - now < 14 * 86400:
            print(f"LinkedIn post numbers stop {dt.datetime.fromtimestamp(ends):%d %b} (token ends): "
                  "python3 scripts/linkedin_post_results.py auth, then Allow once.")
    age = lambda p: (dt.date.today() - dt.date.fromisoformat(p["published"])).days  # noqa: E731
    untagged = [p for p in d["posts"].values() if (not p["media"] or not p["topic"]) and p["readings"] and
                latest(p)["age"] >= MIN_AGE and (age(p) <= READ_DAYS if tok else True)]
    if days >= DUE_DAYS and tok:
        print(f"LinkedIn results: the daily pull hasn't run for {'ever' if days == 99 else f'{days} days'}. Run "
              "python3 scripts/linkedin_post_results.py pull --check (no export needed), and look at the "
              "linkedin-post-results job.")
    elif days >= DUE_DAYS:
        print(f"LinkedIn results check is due (last export {'never' if days == 99 else f'{days} days ago'}). "
              "Download LinkedIn's 28-day analytics export and `ingest` it, or connect the API (`app`, `auth`).")
    elif untagged:
        print(f"LinkedIn results: {len(untagged)} post(s) not tagged yet (what they showed / were about), so `check` leaves "
              "them out: python3 scripts/linkedin_post_results.py tag")


def tick(d: dict) -> None:
    """The daily job: reads the posts that are due (up to the cap); on Mondays also `check`."""
    if not TOKEN.exists():
        return
    print(pull(d))
    if write_works(d):
        print(f"{WORKS.name}: live numbers updated for the next batch")
    if dt.date.today().weekday() == 0:
        check(d, quiet=True)


def selftest() -> None:
    assert post_id("https://www.linkedin.com/posts/freja-eksempel_to-mails-share-7522938047692923456-abcd") == "7522938047692923456"
    assert slug("https://www.linkedin.com/posts/freja-eksempel_to-mails-to-svar-share-7522938047692923456-ab") == "to-mails-to-svar"
    ps = [{"id": str(i), "media": "photo", "topic": "story", "impressions": 1000 + i, "engagements": 10} for i in range(5)]
    ps += [{"id": str(9 + i), "media": "graphic", "topic": "tip", "impressions": 400 + i, "engagements": 3} for i in range(5)]
    fs = findings(ps, {})
    assert [(f["key"], f["kind"]) for f in fs] == [("media:graphic|photo", "reach"), ("topic:story|tip", "reach")], fs
    assert "caveat" in fs[0], "graphics were all tips and photos all stories: the topic caveat must show"
    assert not findings(ps[:4] + ps[5:9], {}), "under five posts a group never counts"
    tips_dm = {str(9 + i): 2 for i in range(5)}  # the tips reach less but bring the DMs: keep both, never cut tips
    fs = findings(ps, tips_dm)
    assert [f["kind"] for f in fs] == ["lean", "lean"] and "toward tip or how-to" in fs[1]["rule"], fs
    same = {str(i): 1 for i in range(5)}  # more reach and more DMs: a plain preference
    assert findings(ps, same)[0]["kind"] == "both"
    few = {str(9): 1}
    assert [f["kind"] for f in findings(ps, few)] == [], "less reach but more DMs: never a rule to cut that group (DMs count)"
    fk = {"findings": {"a": {"id": "L1", "status": "auto", "rule": "Prefer x.", "last_seen": "2026-11-17"},
                       "b": {"id": "L2", "status": "no", "rule": "Prefer y.", "last_seen": "2026-11-17"}}}
    rb = rules_block(fk)
    assert "[L1] Prefer x." in rb and "Prefer y." not in rb, "a vetoed rule stays out"
    assert id_date("7522938047692923456") == dt.date(2026, 11, 2), "the post id carries its publish time"
    assert _entity("urn:li:ugcPost:7525474762781483584") == "(ugc:urn%3Ali%3AugcPost%3A7525474762781483584)"
    assert _entity("urn:li:share:7526561926386672192") == "(share:urn%3Ali%3Ashare%3A7526561926386672192)"
    assert _range(dt.date(2024, 5, 4), dt.date(2024, 5, 6)) == "(start:(day:4,month:5,year:2024),end:(day:6,month:5,year:2024))"
    try:
        _count({"access_token": ""}, "q=me", [int(time.time())] * CALL_CAP)
        raise AssertionError("the cap must stop the call before it goes out")
    except RateLimited:
        pass
    import tempfile
    fake = {"ingests": [{"on": "2026-11-17", "file": "api"}], "posts": {
        str(i): {"published": "2026-11-01", "slug": f"post-nummer-{i}", "media": "photo" if i < 5 else "graphic",
                 "topic": "story", "tagged_by": "", "readings": [{"on": "2026-11-17", "age": 16, "impressions": 100 * (i + 1),
                                                                  "engagements": i}]} for i in range(10)}}
    block = live_block(fake)
    assert block.startswith(LIVE[0]) and block.endswith(LIVE[1]) and "Photo of you: 5 posts" in block, block
    assert "Reached the fewest" in block and "post nummer 0" in block
    with tempfile.TemporaryDirectory() as tmp:
        w = pathlib.Path(tmp) / "w.md"
        w.write_text("# Notes\nkeep me\n")
        assert write_works(fake, w) and not write_works(fake, w), "a second run with the same numbers changes nothing"
        fake["posts"]["9"]["readings"][0]["impressions"] = 5
        write_works(fake, w)
        t = w.read_text()
        assert t.startswith("# Notes\nkeep me") and t.count(LIVE[0]) == 1, "the block is replaced, the notes stay"
    p = {"media": "", "topic": "tip", "tagged_by": "session 2026-11-10"}
    _engine_tag(p, {"media": "photo", "topic": "story"})
    assert p == {"media": "", "topic": "tip", "tagged_by": "session 2026-11-10"}, "a tag set by hand is never overwritten"
    p = {"media": "", "topic": "", "tagged_by": ""}
    _engine_tag(p, {"media": "photo", "topic": "story"})
    assert p["media"] == "photo" and p["topic"] == "story"
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest").add_argument("file", type=pathlib.Path)
    t = sub.add_parser("tag")
    t.add_argument("match", nargs="?")
    t.add_argument("--media")
    t.add_argument("--topic")
    sub.add_parser("check")
    a = sub.add_parser("answer")
    a.add_argument("id")
    a.add_argument("verdict", choices=["yes", "no"])
    a.add_argument("--note", default="")
    sub.add_parser("due")
    sub.add_parser("selftest")
    sub.add_parser("app").add_argument("--reset", action="store_true")
    sub.add_parser("auth")
    sub.add_parser("status")
    sub.add_parser("pull").add_argument("--check", action="store_true", help="run `check` right after")
    sub.add_parser("add").add_argument("url")
    sub.add_parser("tick")
    args = ap.parse_args()
    if args.cmd == "selftest":
        selftest()
        sys.exit(0)
    if args.cmd in ("app", "auth", "status"):
        store_app(args.reset) if args.cmd == "app" else auth() if args.cmd == "auth" else status()
        sys.exit(0)
    if args.cmd == "due":
        try:
            due(load())
        except Exception:
            pass
        sys.exit(0)
    data = load()
    if args.cmd == "ingest":
        print(ingest(args.file, data))
        save(data)
    elif args.cmd == "tag":
        tag(data, args.match, args.media, args.topic)
    elif args.cmd == "check":
        check(data)
    elif args.cmd == "answer":
        answer(data, args.id, args.verdict == "yes", args.note)
    elif args.cmd == "add":
        add(data, args.url)
    elif args.cmd == "tick":
        tick(data)
    elif args.cmd == "pull":
        print(pull(data))
        if args.check:
            check(data)
        elif write_works(data):
            print(f"{WORKS.name}: live numbers updated for the next batch")
