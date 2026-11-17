#!/usr/bin/env python3
"""Post approved content-engine posts to the author's LinkedIn profile: by hand ("post N") or by the plan ("due").

LinkedIn's API Terms (section 3.1 when this was written) ban using the API "to automate posting", so `due` goes
against the letter of those terms and LinkedIn can shut the app off without warning. Read them yourself before you
turn it on. It stays at one post a day, only the author's own approved posts, and it does nothing else on LinkedIn.

A LinkedIn developer app of your own with the products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID
Connect". Client id and secret in tokens/<linkedin.post_app_file>, the member token (60 days, no refresh token) in
tokens/<linkedin.post_token_file>.

  app CLIENT_ID      store the client id + the client secret copied to the clipboard (never printed, clipboard cleared)
  auth               LinkedIn's consent page opens, the author clicks Allow once, the token is saved; the same to renew
  status             who it posts as and how many days the token has left
  list               every post in the batch: what would go out with it, or what it waits for, and what is posted
  post N [--yes]     without --yes: exactly what would go out; with --yes: media up, post live, the link into the
                     batch file and the post's Check line in the doc, a line in data/state/linkedin_posts.jsonl
  delete URN --yes   take a post down again
  confirm N [N ...]  the post's Check line is answered: it becomes "Facts confirmed <date>."
  due [--dry] [--at "2026-11-04 09:31"]
                     post what the plan says is due ([posting] mode: test | live | off, test only logs what it would
                     post; host: mac | server, only that machine posts), never one scheduled by hand, at most 1 a day.
                     On a server every 15 minutes (cron); on the Mac hourly, where it brings home what the server
                     posted. --dry: everything short of LinkedIn, nothing is written
  pull               bring home what the server posted, now
  plan               the posting plan sheet: one row per engine post, a date (the [plan] weekdays, an uneven time each
                     day) and its files only once it's ready; runs hourly, posts nothing itself
  --selftest         escaping, media picking and the plan's rules, no network
"""
import argparse
import collections
import datetime as dt
import fcntl
import shlex
import http.server
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from zoneinfo import ZoneInfo

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

APP = C.token("post_app_file")
TOKEN = C.token("post_token_file")
LOG = C.STATE / "linkedin_posts.jsonl"
BATCHES = C.BATCHES
WORK = C.CACHE / "linkedin"
PORT = int(C.get("linkedin", "post_port"))  # the redirect URL on the app's Auth tab must match
REDIRECT = f"http://localhost:{PORT}/linkedin/callback"
SCOPES = "openid profile w_member_social"
VERSION = C.get("linkedin", "api_version")  # LinkedIn keeps each version live for at least a year
API = "https://api.linkedin.com/rest"
SERVER = C.get("posting", "server_ssh")  # user@host of the machine that posts while the Mac is off (optional)
SERVER_REPO = C.get("posting", "server_repo")
SERVER_PY = C.get("posting", "server_python") or "python3"
ON_SERVER = os.environ.get("CONTENT_ENGINE_ON_SERVER") == "1"  # set it in the server's cron line
RESERVED = set("|{}@[]()<>#\\*_~")  # Little Text: without a backslash LinkedIn reads these as markup


def little(text: str) -> str:
    """The post text as LinkedIn's Little Text, so (), #, * and the like show exactly as typed."""
    return "".join("\\" + c if c in RESERVED else c for c in text)


def _save(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=1))
    path.chmod(0o600)


# ---------- the app and the author's consent ----------
def store_app(client_id: str) -> None:
    secret = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if len(secret) < 10 or any(c.isspace() for c in secret):
        raise SystemExit("the clipboard doesn't hold a client secret: copy it on the app's Auth tab first")
    _save(APP, {"client_id": client_id.strip(), "client_secret": secret})
    subprocess.run(["pbcopy"], input="", text=True)
    print(f"stored the client id and secret in {APP} (secret not shown); clipboard cleared")


def auth(timeout: int = 900) -> None:
    app = json.loads(APP.read_text())
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
            msg = "LinkedIn posting is connected. You can close this tab." if ok else \
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
        raise SystemExit(f"no consent came back: {got.get('error_description') or got.get('error') or 'timed out'}")
    r = requests.post("https://www.linkedin.com/oauth/v2/accessToken", timeout=30, data={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
        "client_id": app["client_id"], "client_secret": app["client_secret"]})
    if not r.ok:
        raise SystemExit(f"LinkedIn refused the code: {r.status_code} {r.text[:300]}")
    tok = r.json()
    me = requests.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"},
                      timeout=30).json()
    _save(TOKEN, {"access_token": tok["access_token"], "expires_at": int(time.time()) + int(tok["expires_in"]),
                  "scope": tok.get("scope", ""), "sub": me["sub"], "name": me.get("name", ""),
                  "obtained": dt.datetime.now().isoformat(timespec="seconds")})
    print(f"connected: posts go out as {me.get('name')}, token valid {int(tok['expires_in']) // 86400} days")
    if not SERVER:
        return
    for _ in range(5):  # a server can drop connections at times: a few tries
        r = subprocess.run(["scp", "-q", "-o", "ConnectTimeout=15", str(TOKEN), f"{SERVER}:{SERVER_REPO}/tokens/{TOKEN.name}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(10)
    print("the server has the new token" if r.returncode == 0 else
          f"the server did NOT get the new token, its bot stops when the old one ends: {r.stderr.strip()[:200]}")


def _token() -> dict:
    if not TOKEN.exists():
        raise SystemExit("LinkedIn posting isn't connected yet: run `linkedin_post.py auth`")
    tok = json.loads(TOKEN.read_text())
    if tok["expires_at"] < time.time() + 3600:
        raise SystemExit("the LinkedIn token has run out: run `linkedin_post.py auth` (Allow once, or nothing while logged in)")
    return tok


def status() -> None:
    tok = _token()
    me = requests.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    days = (tok["expires_at"] - time.time()) / 86400
    print(f"posts as {tok['name']} (urn:li:person:{tok['sub']}); token {'works' if me.ok else f'REFUSED {me.status_code}'}, "
          f"{days:.0f} days left (until {dt.datetime.fromtimestamp(tok['expires_at']):%d %b %Y})")


# ---------- what goes out with a post ----------
def latest_batch() -> pathlib.Path:
    found = [f for f in BATCHES.glob("*.json") if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Batch_\d+\.json", f.name)]
    if not found:
        raise SystemExit(f"no batch file in {BATCHES} yet")
    return max(found, key=lambda f: (f.name[:10], int(re.search(r"Batch_(\d+)", f.name).group(1))))


def media(post: dict, n: int, graphic: bool = False) -> tuple[str, list[dict]]:
    """('image' | 'document' | 'video' | 'text' | 'wait: why', the Drive files that go up with it)."""
    files = {v["file"]: v for v in post.get("visual_files", [])}
    fmt = post.get("format", "text")
    if fmt == "video":
        if not post.get("video_file"):
            return "wait: the video isn't recorded yet (shoot card in the doc's Video scripts tab)", []
        cover = files.get(f"post{n}_video_cover.png")
        return "video", [{"file": f"post{n}_video.mp4", "link": post["video_file"]}] + ([cover] if cover else [])
    if fmt == "photo" and not graphic:
        photo = files.get(f"post{n}_photo.jpg")
        return ("image", [photo]) if photo else ("wait: a recent photo of you isn't on the post yet", [])
    if f"post{n}_carousel.pdf" in files:
        return "document", [files[f"post{n}_carousel.pdf"]]
    pngs = [v for k, v in files.items() if re.fullmatch(rf"post{n}_\d+\.png", k)]
    if len(pngs) > 1:
        return "document", pngs  # slides without a PDF become one here
    return ("image", pngs) if pngs else ("text", [])


def _title(post: dict) -> str:
    """A document's or video's title on LinkedIn: the post's own first line."""
    first = post["text"].strip().split("\n", 1)[0].strip()
    return first if len(first) <= 80 else first[:77].rsplit(" ", 1)[0] + "..."


def _download(entries: list[dict], folder: pathlib.Path) -> list[pathlib.Path]:
    import content_engine_visuals as vis
    from googleapiclient.http import MediaIoBaseDownload
    drive = vis._google("drive", "v3")
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for e in entries:
        path = folder / e["file"]
        with path.open("wb") as fh:
            dl, done = MediaIoBaseDownload(fh, drive.files().get_media(fileId=vis.drive_id(e["link"])), chunksize=16 * 1024 * 1024), False
            while not done:
                _, done = dl.next_chunk()
        out.append(path)
    return out


def _pdf(pngs: list[pathlib.Path], out: pathlib.Path) -> pathlib.Path:
    from PIL import Image
    imgs = [Image.open(p).convert("RGB") for p in pngs]
    imgs[0].save(out, save_all=True, append_images=imgs[1:])
    return out


# ---------- LinkedIn calls ----------
def _h(tok: dict, **extra) -> dict:
    return {"Authorization": f"Bearer {tok['access_token']}", "Linkedin-Version": VERSION,
            "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json", **extra}


def _call(method: str, url: str, tok: dict, headers: dict | None = None, **kw) -> requests.Response:
    r = requests.request(method, url, headers=_h(tok, **(headers or {})), timeout=120, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"LinkedIn said {r.status_code} on {url.split('?')[0]}: {r.text[:400]}")
    return r


def _owner(tok: dict) -> str:
    return f"urn:li:person:{tok['sub']}"


def _put(url: str, data: bytes, tok: dict | None = None, **headers) -> requests.Response:
    if tok:
        headers["Authorization"] = f"Bearer {tok['access_token']}"
    r = requests.put(url, data=data, headers=headers, timeout=600)
    if r.status_code >= 400:
        raise SystemExit(f"upload refused: {r.status_code} {r.text[:300]}")
    return r


def up_image(tok: dict, path: pathlib.Path) -> str:
    v = _call("POST", f"{API}/images?action=initializeUpload", tok, json={"initializeUploadRequest": {"owner": _owner(tok)}}).json()["value"]
    _put(v["uploadUrl"], path.read_bytes(), tok)
    return v["image"]


def up_document(tok: dict, path: pathlib.Path) -> str:
    v = _call("POST", f"{API}/documents?action=initializeUpload", tok, json={"initializeUploadRequest": {"owner": _owner(tok)}}).json()["value"]
    _put(v["uploadUrl"], path.read_bytes(), tok)
    return v["document"]


def up_video(tok: dict, path: pathlib.Path, cover: pathlib.Path | None) -> str:
    v = _call("POST", f"{API}/videos?action=initializeUpload", tok, json={"initializeUploadRequest": {
        "owner": _owner(tok), "fileSizeBytes": path.stat().st_size, "uploadCaptions": False,
        "uploadThumbnail": bool(cover)}}).json()["value"]
    etags = []
    with path.open("rb") as fh:
        for part in v["uploadInstructions"]:  # 4 MB parts, in order; each part's ETag closes the upload
            fh.seek(part["firstByte"])
            r = _put(part["uploadUrl"], fh.read(part["lastByte"] - part["firstByte"] + 1), **{"Content-Type": "application/octet-stream"})
            etags.append(r.headers["ETag"].strip('"'))
    if cover and v.get("thumbnailUploadUrl"):
        _put(v["thumbnailUploadUrl"], cover.read_bytes(), **{"media-type-family": "STILLIMAGE", "Content-Type": "application/octet-stream"})
    _call("POST", f"{API}/videos?action=finalizeUpload", tok, json={"finalizeUploadRequest": {
        "video": v["video"], "uploadToken": v.get("uploadToken", ""), "uploadedPartIds": etags}})
    for _ in range(40):  # a post on a video still processing goes live by itself once it's ready
        r = requests.get(f"{API}/videos/{urllib.parse.quote(v['video'], safe='')}", headers=_h(tok), timeout=30)
        if not r.ok or r.json().get("status") in ("AVAILABLE", "PROCESSING_FAILED"):
            break
        time.sleep(6)
    if r.ok and r.json().get("status") == "PROCESSING_FAILED":
        raise SystemExit(f"LinkedIn couldn't process the video: {r.json().get('processingFailureReason')}")
    return v["video"]


def create(tok: dict, text: str, kind: str, asset: str | None, title: str) -> str:
    body = {"author": _owner(tok), "commentary": little(text), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    if asset:
        body["content"] = {"media": {"id": asset, **({"title": title} if kind in ("document", "video") else {})}}
    return _call("POST", f"{API}/posts", tok, json=body).headers["x-restli-id"]


# ---------- commands ----------
def _when(at: dt.datetime) -> str:
    """The plan's time zone first, then the local time from the machine's own zone, so the label follows the author."""
    dk = f"{at.astimezone(DK):%d %b %H:%M} {TZ_LABEL}"
    zone = pathlib.Path("/etc/localtime").resolve().as_posix().split("zoneinfo/")[-1]
    try:
        return f"{dk} / {at.astimezone(ZoneInfo(zone)):%H:%M} {zone.split('/')[-1].replace('_', ' ')}"
    except Exception:
        return dk


def listing(batch: pathlib.Path) -> None:
    for n, p in enumerate(json.loads(batch.read_text()), 1):
        kind, entries = media(p, n)
        done = p.get("posted") if p.get("posted") and not p["posted"].get("deleted") else None
        state = (f"POSTED {p['posted']['url']}" if done else "dropped" if p.get("status") == "dropped" else
                 kind if kind.startswith("wait") else f"ready: {kind}" + (f" ({', '.join(e['file'] for e in entries)})" if entries else ""))
        print(f"{n}. {p['title']}: {state}")


def post(n: int, batch: pathlib.Path, yes: bool, title: str | None, graphic: bool, before_send=None,
         day: str | None = None) -> dict | None:
    """Returns what was posted. before_send runs right before the post itself goes to LinkedIn, after the uploads.
    Refuses when anything already went out that day in the plan's time zone (DAY_CAP)."""
    import content_engine_draft as ced
    if yes and not ON_SERVER and _autopost().get("host") == "server":
        return _post_on_server(n, batch, title, graphic)  # one machine posts, under one lock and one log
    day = day or dt.datetime.now(DK).date().isoformat()
    posts = json.loads(batch.read_text())
    p = posts[n - 1]
    kind, entries = media(p, n, graphic)
    title = title or _title(p)
    issues = ced.lint(p["text"])
    print(f"{batch.stem.split('_', 1)[1].replace('_', ' ')}, post {n}: {p['title']}, "
          f"{kind}{' with ' + ', '.join(e['file'] for e in entries) if entries else ''}")
    if kind in ("document", "video"):
        print(f"Title on LinkedIn: {title}")
    print(f"Check line: {p.get('check') or '(empty)'}")
    print("---\n" + p["text"].strip() + "\n---")
    live = p.get("posted") if p.get("posted") and not p["posted"].get("deleted") else None
    stop = ("the post is dropped" if p.get("status") == "dropped" else
            f"already posted {live['at']}: {live['url']}" if live else
            f"a post already went out on {day}, and it is never more than {DAY_CAP} a day" if posted_on(day) else
            kind[6:] if kind.startswith("wait") else
            f"{len(p['text'])} characters, LinkedIn's limit is 3000" if len(p["text"]) > 3000 else
            "the text breaks a rule: " + ", ".join(issues) if issues else "")
    if stop:
        raise SystemExit(f"not posting: {stop}")
    if not yes:
        print("preview only: add --yes to post it")
        return
    tok = _token()
    local = _download(entries, WORK / batch.stem) if entries else []
    asset = None
    if kind == "image":
        asset = up_image(tok, local[0])
    elif kind == "document":
        asset = up_document(tok, local[0] if local[0].suffix == ".pdf" else _pdf(local, WORK / batch.stem / f"post{n}_carousel.pdf"))
    elif kind == "video":
        asset = up_video(tok, local[0], local[1] if len(local) > 1 else None)
    if posted_on(day):  # again after the uploads, which can take minutes
        raise DayFull(f"not posting: a post went out on {day} while this one uploaded")
    if before_send:
        before_send()
    urn = create(tok, p["text"].strip(), kind, asset, title)
    now = dt.datetime.now(dt.timezone.utc)
    url = f"https://www.linkedin.com/feed/update/{urn}/"
    p["posted"] = {"at": now.isoformat(timespec="seconds"), "urn": urn, "url": url, "media": kind, "check_before": p.get("check", "")}
    if ON_SERVER:  # its repo is a copy the Mac overwrites and the doc lock is per machine: due() logs it, pull_server() brings it home
        for f in local:
            f.unlink(missing_ok=True)
        print(f"posted: {url}")
        return p["posted"]
    batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))  # first, so a failed doc update can never cause a second post
    with LOG.open("a") as f:
        f.write(json.dumps({"at": p["posted"]["at"], "batch": batch.stem, "post": n, "urn": urn, "url": url, "media": kind}) + "\n")
    for f in local:
        f.unlink(missing_ok=True)
    print(f"posted: {url}")
    try:
        import content_engine_photos as ph
        ph._set_check(batch, posts, n, f"Posted on LinkedIn {_when(now)}: {url}")
        batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    except Exception as e:  # noqa: BLE001 - the post is live either way
        print(f"(the doc's Check line wasn't updated: {e})")
    return p["posted"]


def delete(urn: str, yes: bool) -> None:
    if not yes:
        raise SystemExit("add --yes to take the post down")
    _call("DELETE", f"{API}/posts/{urllib.parse.quote(urn, safe='')}", _token(), headers={"X-RestLi-Method": "DELETE"})
    for batch in BATCHES.glob("*.json"):
        posts = json.loads(batch.read_text())
        hit = [p for p in posts if isinstance(p, dict) and p.get("posted", {}).get("urn") == urn]
        for p in hit:
            p["posted"]["deleted"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        if hit:
            batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    print(f"deleted {urn}")


# ---------- the posting plan ----------
PLAN_DAYS = tuple(C.get("plan", "weekdays"))  # posting weekdays, Monday = 0 (leave days free for posts made by hand)
DAILY_FROM = dt.date.fromisoformat(C.get("plan", "daily_from")) if C.get("plan", "daily_from") else None  # then every day


def engine_day(d: dt.date) -> bool:
    return bool(DAILY_FROM and d >= DAILY_FROM) or d.weekday() in PLAN_DAYS
# A post up by midday, at a different time every posting day, to learn which time works: every posting day gets its own
# odd minute between SLOT_FROM and SLOT_TO (never on a :x0 or :x5), drawn from the date, so the same day always gives
# the same time (the plan sheet and the 15-minute job agree), and never within SLOT_APART minutes of the posting day
# before. The results check can still read time of day from it.
_hm = lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1])  # noqa: E731
SLOT_FROM, SLOT_TO, SLOT_APART = _hm(C.get("plan", "slot_from")), _hm(C.get("plan", "slot_to")), int(C.get("plan", "slot_apart_minutes"))


SLOT_ANCHOR = dt.date.fromisoformat(C.get("plan", "anchor"))  # from here every posting day is drawn after the one before
_SLOTS: dict = {}


def _slot_draw(d: dt.date, k: int) -> int:
    import hashlib
    m = SLOT_FROM + int(hashlib.sha256(f"content-engine-slot-{d.isoformat()}-{k}".encode()).hexdigest(), 16) % (SLOT_TO - SLOT_FROM)
    return m + 2 if m % 5 == 0 else m


def _slot_minute(d: dt.date) -> int:
    """The day's minute: the first draw at least SLOT_APART from the posting day before and not used in the 8 before
    that, walked day by day from SLOT_ANCHOR, so the answer never depends on what was asked first."""
    if not engine_day(d) or d < SLOT_ANCHOR:
        return _slot_draw(d, 0)
    if d not in _SLOTS:
        done = sorted(_SLOTS)
        day = done[-1] + dt.timedelta(days=1) if done else SLOT_ANCHOR
        recent = [_SLOTS[x] for x in done[-9:]]
        while day <= d:
            if engine_day(day):
                ok = lambda x: not recent or (abs(x - recent[-1]) >= SLOT_APART and x not in recent)
                m = next((x for x in (_slot_draw(day, k) for k in range(200)) if ok(x)), _slot_draw(day, 0))
                _SLOTS[day] = m
                recent = (recent + [m])[-9:]
            day += dt.timedelta(days=1)
    return _SLOTS[d]


def slot_time(d: dt.date) -> dt.time:
    m = _slot_minute(d)
    return dt.time(m // 60, m % 60)
SCHEDULER_TZ = C.get("plan", "scheduler_tz")  # someone who schedules by hand: their time zone in the sheet (optional)
PLAN_HEAD = ["Post", "Title", "Status", "Date", "Time", "Kind", "Text", "Files", "How to add the files", "Scheduled by hand"]
BOT_TAB = "Bot"    # the tab the bot reads; always named in a range, so a tab in front never moves it
VIEW_TAB = "Plan"  # the author's tab, first in the sheet: what goes out when, in the plan's and the local time
CONFIRMED = r"Facts confirmed \d{1,2} [A-Z][a-z]{2}\."


def open_question(check: str) -> str:
    """What is still asked on a Check line: minus the confirmation and the photo job's notes that only inform
    (its "other people in the photo" question stays)."""
    import content_engine_photos as ph
    for pat in [CONFIRMED, *ph.OWN_NOTES[1:]]:
        check = re.sub(pat, "", check or "")
    return " ".join(check.split())


def readiness(p: dict, n: int) -> tuple[str, str, list[dict]]:
    """'ready', 'waiting: why', 'posted' or 'dropped', with the post's kind and files. Ready = approved (which is the
    author's OK on the Check line too) and the photo or video in."""
    if p.get("posted") and not p["posted"].get("deleted"):
        return "posted", "", []
    if p.get("status") == "dropped":
        return "dropped", "", []
    kind, files = media(p, n)
    if p.get("status") != "approved":
        return "waiting: approval", kind, files
    if GAP in p.get("text", ""):  # a gap the engine left for the author to fill: never posted
        return "waiting: a gap in the text to fill", kind, files
    if kind.startswith("wait"):
        return f"waiting: {'video' if 'video' in kind else 'photo'}", kind, files
    # the approval is the OK on the facts too: the author approves with the Check line in front of them
    # one exception: a photo placed with other people in it (their OK, not the author's facts)
    import content_engine_photos as ph
    if re.search(ph.OWN_NOTES[0], p.get("check", "")) and not re.search(CONFIRMED, p.get("check", "")):
        return "waiting: OK on the people in the photo", kind, files
    return "ready", kind, files


# Days when no engine post gets a date (a campaign of posts with their own dates, a holiday): [plan] pauses
PAUSES = tuple((dt.date.fromisoformat(a), dt.date.fromisoformat(b)) for a, b in C.get("plan", "pauses"))
GAP = C.get("author", "gap_marker")  # a gap the drafter left in a post, like "[AUTHOR: which example?]"


def paused(d: dt.date) -> bool:
    return any(a <= d <= b for a, b in PAUSES)


def next_slot(taken: set, start: dt.date) -> dt.date:
    d = start
    while not engine_day(d) or d in taken or paused(d):
        d += dt.timedelta(days=1)
    return d


def confirm(ns: list[int], batch: pathlib.Path) -> None:
    import content_engine_photos as ph
    posts = json.loads(batch.read_text())
    today = dt.date.today()
    for n in ns:
        p = posts[n - 1]
        keep = [m.group(0) for pat in ph.OWN_NOTES for m in re.finditer(pat, p.get("check", ""))]
        p.setdefault("history", []).append({"when": today.isoformat(), "via": "chat", "decision": "facts confirmed",
                                            "instruction": "", "check_before": p.get("check", "")})
        ph._set_check(batch, posts, n, " ".join([f"Facts confirmed {today.day} {today:%b}.", *keep]))
        batch.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
        print(f"post {n}: facts confirmed")


# When only RUNWAY_MIN or fewer posts are planned ahead, the author hears it, with what would fill the plan.
# Planned ahead = an engine post with a date from today on, not posted yet (ready, or scheduled by hand).
RUNWAY_MIN = int(C.get("plan", "runway_min"))
RUNWAY_FILE = C.STATE / "runway.json"  # also read by linkedin_post_results.py due


def runway_alert_due(n: int, last: dict | None, today: dt.date) -> bool:
    """At RUNWAY_MIN or fewer: once, again when it drops further, and every 2 days while it stays low."""
    if n > RUNWAY_MIN:
        return False
    return not last or n < last["ahead"] or (today - dt.date.fromisoformat(last["date"])).days >= 2


def runway(ahead: list, waiting: dict, sheet_link: str) -> str:
    """Write the count, send the alert when it's due; returns the line for the top of the Plan tab."""
    today = dt.date.today()
    st = json.loads(RUNWAY_FILE.read_text()) if RUNWAY_FILE.exists() else {}
    n, last = len(ahead), (ahead[-1][0] if ahead else None)
    wait = ", ".join(f"{c} on {why}" for why, c in sorted(waiting.items(), key=lambda x: -x[1]))
    out = {"at": dt.datetime.now().isoformat(timespec="minutes"), "ahead": n, "last_date": last.isoformat() if last else None,
           "next": [{"post": k, "date": d.isoformat()} for d, k in ahead[:3]], "waiting": waiting,
           "alert": st.get("alert") if n <= RUNWAY_MIN else None, "late_noted": st.get("late_noted", [])}
    if runway_alert_due(n, st.get("alert"), today):
        import content_engine_feedback as cef
        cef.inbox(f"LinkedIn: only {n} post{'' if n == 1 else 's'} planned ahead", [
            f"Only {n} post{'' if n == 1 else 's'} planned ahead" + (f", the last on {last:%a} {last.day} {last:%b}." if last else "."),
            (f"Waiting for you: {wait}." if wait else "No post is waiting for you: the plan needs a new batch."),
            "To fill it: approve the waiting posts in the doc, send the photos or videos, or do an interviewer call for a new batch.",
            sheet_link])
        out["alert"] = {"ahead": n, "date": today.isoformat()}
    RUNWAY_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return (f"Planned ahead now: {n} post{'' if n == 1 else 's'}" + (f", the last on {last:%a} {last.day} {last:%b}" if last else "")
            + (f". Waiting for you: {wait}." if wait else "."))


def assign_dates(items: list[dict], taken: set, start: dt.date) -> dict:
    """Time-bound posts first, the soonest last date first, each only inside its window; posts for any
    time fill the days after, in batch order. A time-bound post with no free day left in its window gets None."""
    taken, out = set(taken), {}
    now = sorted((x for x in items if x.get("timing") == "now"), key=lambda x: (x.get("post_by") or "9999-12-31", x["i"]))
    rest = sorted((x for x in items if x.get("timing") != "now"), key=lambda x: x["i"])
    for x in now + rest:
        d = next_slot(taken, start)
        if x.get("timing") == "now" and x.get("post_by") and d > dt.date.fromisoformat(x["post_by"]):
            out[x["key"]] = None
            continue
        taken.add(d)
        out[x["key"]] = d
    return out


def late_note(late: list, sheet_link: str) -> None:
    """Once per post: a time-bound post that can no longer go out before its last date."""
    st = json.loads(RUNWAY_FILE.read_text()) if RUNWAY_FILE.exists() else {}
    new = [(k, p) for k, p in late if k not in st.get("late_noted", [])]
    if not new:
        return
    import content_engine_feedback as cef
    cef.inbox(f"LinkedIn: {len(new)} time-bound post{'' if len(new) == 1 else 's'} past {'its' if len(new) == 1 else 'their'} date",
              [f"Post {k} ({p.get('title', '')}) had to go out by {p.get('post_by')}." for k, p in new]
              + ["Post it now, rewrite it so it fits a later date, or drop it: answer here or in the doc.", sheet_link])
    st["late_noted"] = st.get("late_noted", []) + [k for k, _ in new]
    RUNWAY_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n")


def plan() -> None:
    """The plan sheet, in a folder of its own inside the engine folder, so someone who schedules by hand sees the posts
    and their files, never the engine doc. The "Scheduled by hand" column (J) is never touched; a date, once given,
    stays."""
    import content_engine_visuals as vis
    cfg_file = C.STATE / "plan.json"  # the sheet's and folder's ids, and which files were copied there
    cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
    drive, sheets = vis._google("drive", "v3"), vis._google("sheets", "v4")
    if "plan" not in cfg:
        folder = drive.files().create(body={"name": "Posts to schedule", "parents": [C.need("google", "drive_root_id")],
                                            "mimeType": "application/vnd.google-apps.folder"}, fields="id, webViewLink").execute()
        sheet = drive.files().create(body={"name": "LinkedIn posts to schedule", "parents": [folder["id"]],
                                           "mimeType": "application/vnd.google-apps.spreadsheet"}, fields="id, webViewLink").execute()
        widths = [60, 220, 190, 120, 230, 170, 460, 300, 300, 150]
        sheets.spreadsheets().batchUpdate(spreadsheetId=sheet["id"], body={"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {"range": {"sheetId": 0, "startRowIndex": 1},
                            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                            "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}}] + [
            {"updateDimensionProperties": {"range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                           "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(widths)]}).execute()
        cfg["plan"] = {"folder": folder, "sheet": sheet, "copied": {}}
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
    pl, sid, width = cfg["plan"], cfg["plan"]["sheet"]["id"], len(PLAN_HEAD)
    rows_before = sheets.spreadsheets().values().get(spreadsheetId=sid, range=f"{BOT_TAB}!A2:J").execute().get("values", [])
    have = {r[0]: r + [""] * (width - len(r)) for r in rows_before if r}
    # Every open date from the day after tomorrow on is given again each run, so a time-bound post goes first inside
    # its window and the posts for any time fill the rest. Fixed: a date scheduled by hand, and the next two days
    # (already announced).
    start, rows, counts, view = dt.date.today() + dt.timedelta(days=2), [], {}, []
    taken = set()
    for r in have.values():
        try:
            d = dt.datetime.strptime(r[3], "%a %d %b %Y").date()
        except ValueError:
            continue
        if r[9].strip() or d < start:
            taken.add(d)
    pl["daily"] = True
    pending = []
    dated, waiting = [], collections.Counter()
    labels = {"image": "Image", "document": "Carousel (PDF document)", "video": "Video", "text": "Text only"}
    found = [f for f in BATCHES.glob("*.json") if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name)]
    auto = _autopost().get("mode") == "live"

    def finish(row, key, p, kind, files, old, d):
        """A dated row: its date, time, files and how to add them; returns its time."""
        taken.add(d)
        if d >= dt.date.today():
            dated.append((d, key))
        at = dt.datetime.combine(d, slot_time(d), DK)
        links = []
        for f in files:
            k = f"{key}|{f['file']}"
            if k not in pl["copied"]:
                c = drive.files().copy(fileId=vis.drive_id(f["link"]), fields="id, webViewLink", body={
                    "name": f"Post {key} {f['file']}", "parents": [pl["folder"]["id"]]}).execute()
                pl["copied"][k] = c["webViewLink"]
            links.append(pl["copied"][k])
        how = {"image": "Add the image to the post.", "document": f"Add the PDF as a document. Document title: {_title(p)}",
               "video": "Add the video, then set the cover image as its thumbnail.", "text": "No file."}.get(kind, "")
        row[2:9] = ["Scheduled" if old[9].strip() else
                    f"Posts by itself at {at:%H:%M}: nothing to do" if auto else "Ready: schedule it", f"{d:%a} {d.day} {d:%b %Y}",
                    old[4] if old[9].strip() and old[4] else  # scheduled by hand at the time shown then
                    f"{at:%H:%M} {TZ_LABEL}" + (f" = {at.astimezone(ZoneInfo(SCHEDULER_TZ)):%H:%M} your time" if SCHEDULER_TZ else ""),
                    labels.get(kind, kind),
                    p["text"].strip(), "\n".join(links), how]
        return at

    for bf in sorted(found, key=lambda f: int(re.search(r"Batch_(\d+)", f.name).group(1))):
        b = re.search(r"Batch_(\d+)", bf.name).group(1)
        for n, p in enumerate(json.loads(bf.read_text()), 1):
            key, (state, kind, files) = f"{b}.{n}", readiness(p, n)
            old, at = have.get(key, [""] * width), None
            counts[state.split(":")[0]] = counts.get(state.split(":")[0], 0) + 1
            row = [key, p.get("title", ""), "", "", "", "", "", "", "", old[9]]
            # a post with a date of its own ("date" in the batch file, e.g. a series of posts tied to days): kept as
            # given, also inside a pause, and no other post gets that day
            own = dt.date.fromisoformat(p["date"]) if p.get("date") else None
            if own and state not in ("posted", "dropped"):
                taken.add(own)
            if state in ("posted", "dropped"):
                row[2] = "Posted already: nothing to do" if state == "posted" else "Dropped: nothing to do"
            elif state.startswith("waiting") and not old[9].strip():
                row[2] = f"Waiting for you ({state.split(': ', 1)[1]})" + (f", goes out {own:%a} {own.day} {own:%b}" if own else "")
                waiting[state.split(": ", 1)[1]] += 1
            else:  # ready, or already scheduled by hand
                fixed = own or (dt.datetime.strptime(old[3], "%a %d %b %Y").date() if old[3] else None)
                if not (own or (fixed and (old[9].strip() or fixed < start))):
                    pending.append({"key": key, "timing": p.get("timing"), "post_by": p.get("post_by"), "i": len(pending),
                                    "args": (row, key, p, state, kind, files, old)})
                    rows.append(row)
                    continue
                at = finish(row, key, p, kind, files, old, fixed)
            rows.append(row)
            view.append(_view_row(key, p, state, kind, at, bool(old[9].strip()), len(view)))
    late = []
    assigned = assign_dates(pending, taken, start)  # time-bound first, inside its window; then the rest in batch order
    for x in pending:
        row, key, p, state, kind, files, old = x["args"]
        d = assigned[key]
        if d is None:
            row[2] = f"Waiting for you (past its last date, {p.get('post_by')})"
            waiting["past its last date"] += 1
            late.append((key, p))
            at = None
        else:
            at = finish(row, key, p, kind, files, old, d)
        view.append(_view_row(key, p, "waiting: past its last date" if d is None else state, kind, at, False, len(view)))
    if late:
        late_note(late, pl["sheet"]["webViewLink"])
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
    if [r[:width] for r in rows] != [have.get(r[0], [""] * width)[:width] for r in rows] or len(rows) != len(have):
        sheets.spreadsheets().values().update(spreadsheetId=sid, range=f"{BOT_TAB}!A1:J{len(rows) + 1}", valueInputOption="RAW",
                                              body={"values": [PLAN_HEAD] + rows}).execute()
        print(f"plan: {', '.join(f'{v} {k}' for k, v in counts.items())}; {pl['sheet']['webViewLink']}")
    _write_view(sheets, sid, view, runway(sorted(dated), dict(waiting), pl["sheet"]["webViewLink"]))


def _local_zone() -> ZoneInfo:
    """The local time zone: the machine's own, which follows the author when they travel; the plan's zone if it can't
    be read."""
    try:
        return ZoneInfo(str(pathlib.Path("/etc/localtime").readlink()).split("zoneinfo/", 1)[1])
    except (OSError, IndexError, KeyError, ValueError):
        return DK


def _view_row(key: str, p: dict, state: str, kind: str, at, by_hand: bool, i: int) -> tuple:
    """One line of the Plan tab, with what it sorts by: posted and planned posts by time, waiting ones last."""
    tz = _local_zone()
    kinds = {"image": "Image", "document": "Carousel", "video": "Video", "text": "Text only"}
    text = p.get("text", "").strip()
    link = ""
    if state == "posted":
        at = dt.datetime.fromisoformat(p["posted"]["at"]).astimezone(DK)
        status, link = "Posted", f"https://www.linkedin.com/feed/update/{p['posted']['urn']}/"
        kind = p["posted"].get("media", "")
    elif state == "dropped":
        status = "Dropped"
    elif at:
        status = "Scheduled by hand" if by_hand else "Posts by itself"
    else:
        what = state.split(": ", 1)[1]
        status = f"Waiting for {'the video (next free day once it is in)' if what == 'video' else what}"
        kind = "video" if "video" in kind else "image" if "photo" in kind else kind
    when = ["", "", "", ""]
    if at:
        local = at.astimezone(tz)
        when = [f"Week {at.isocalendar().week}", f"{at:%a} {at.day} {at:%b %Y}", f"{at:%H:%M}",
                f"{local:%H:%M}" + (f" ({local:%a})" if local.date() != at.date() else "")]
    cells = when + [key, p.get("title", ""), kinds.get(kind, kind.capitalize()),
                    text.splitlines()[0][:110] if text else "", status, link]
    return (0 if at else 1, at.timestamp() if at else 0, i), cells


def _write_view(sheets, sid: str, view: list, ahead: str = "") -> None:
    """The author's tab, first in the sheet, rewritten only when something changed."""
    city = str(_local_zone()).split("/")[-1].replace("_", " ")
    days = ", ".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[k] for k in sorted(PLAN_DAYS))
    top = (f"Engine posts go out on {days}" + (f", every day from {DAILY_FROM:%d %b}" if DAILY_FROM else "")
           + f", never more than {DAY_CAP} a day, each day at its own uneven time between {C.get('plan', 'slot_from')} and "
           f"{C.get('plan', 'slot_to')} ({TZ_LABEL})." + (f" {ahead}" if ahead else ""))
    head = ["Week", "Day", f"{TZ_LABEL} time", f"{city} time", "Post", "Title", "Kind", "First line", "Status", "LinkedIn"]
    want = [[top], head] + [cells for _, cells in sorted(view, key=lambda v: v[0])]
    tabs = {t["properties"]["title"]: t["properties"]["sheetId"]
            for t in sheets.spreadsheets().get(spreadsheetId=sid, fields="sheets.properties").execute()["sheets"]}
    if VIEW_TAB not in tabs:
        new = sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"addSheet": {"properties": {
            "title": VIEW_TAB, "index": 0, "gridProperties": {"frozenRowCount": 2}}}}]}).execute()
        tab = new["replies"][0]["addSheet"]["properties"]["sheetId"]
        widths = [70, 130, 70, 100, 50, 240, 80, 430, 230, 330]
        sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
            {"mergeCells": {"range": {"sheetId": tab, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0,
                                      "endColumnIndex": len(head)}, "mergeType": "MERGE_ALL"}},
            {"repeatCell": {"range": {"sheetId": tab}, "cell": {"userEnteredFormat": {
                "wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}},
            {"repeatCell": {"range": {"sheetId": tab, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {
                "textFormat": {"italic": True}}}, "fields": "userEnteredFormat.textFormat.italic"}},
            {"repeatCell": {"range": {"sheetId": tab, "startRowIndex": 1, "endRowIndex": 2}, "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}},
            {"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [{"sheetId": tab, "startRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": len(head)}],
                "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=$I3="Posted"'}]},
                                "format": {"textFormat": {"foregroundColor": {"red": .55, "green": .55, "blue": .55}}}}}}}] + [
            {"updateDimensionProperties": {"range": {"sheetId": tab, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                           "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(widths)]}).execute()
    have = sheets.spreadsheets().values().get(spreadsheetId=sid, range=f"{VIEW_TAB}!A1:J").execute().get("values", [])
    if [r + [""] * (len(head) - len(r)) for r in have[2:]] == want[2:] and have[:2] == [[top], head]:
        return
    sheets.spreadsheets().values().clear(spreadsheetId=sid, range=f"{VIEW_TAB}!A1:J").execute()
    sheets.spreadsheets().values().update(spreadsheetId=sid, range=f"{VIEW_TAB}!A1", valueInputOption="RAW",
                                          body={"values": want}).execute()


# Every step on every slot, one line each (test: what it WOULD have posted). A server keeps its own log at
# [posting] server_log, outside a repo copy that may be overwritten from the Mac.
SERVER_LOG = pathlib.Path(C.get("posting", "server_log") or C.STATE / "linkedin_autopost.jsonl")
AUTOPOST_LOG = SERVER_LOG if ON_SERVER else C.STATE / "linkedin_autopost.jsonl"
MAX_TRIES = 3
DK = ZoneInfo(C.get("plan", "timezone"))  # the plan's time zone
TZ_LABEL = C.get("plan", "timezone").split("/")[-1].replace("_", " ")
# Never two posts in one day: in code, not in the config file. The bot and "post N" by hand both stop once anything
# went out that day, counted from every record either machine keeps. Posts made in LinkedIn itself can't be seen: the
# posting app can't read the profile.
DAY_CAP = 1


class DayFull(SystemExit):
    pass


def _dk_day(iso) -> str:
    try:
        return dt.datetime.fromisoformat(iso).astimezone(DK).date().isoformat()
    except (TypeError, ValueError):
        return ""


def posted_on(day: str) -> set:
    """What went out, or may have, on that day: the batch files (a post taken down again counts too),
    data/state/linkedin_posts.jsonl and the bot's log (a send whose answer was lost counts)."""
    out = set()
    for bf in BATCHES.glob("*.json"):
        try:
            posts = json.loads(bf.read_text())
        except (ValueError, OSError):
            continue
        for p in posts if isinstance(posts, list) else []:
            if isinstance(p, dict) and isinstance(p.get("posted"), dict) and _dk_day(p["posted"].get("at")) == day:
                out.add(p["posted"].get("urn") or bf.stem)
    for line in LOG.open() if LOG.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if _dk_day(r.get("at")) == day:
            out.add(r.get("urn") or r.get("url") or line)
    for d in _rows():
        if d.get("on") == day and d.get("mode") == "live" and d.get("state") in ("sending", "posted", "unknown", "mac"):
            out.add(d.get("urn") or d.get("key"))
    return out


def _key(batch: pathlib.Path, n: int) -> str:
    m = re.search(r"Batch_(\d+)", batch.stem)
    return f"{m.group(1) if m else batch.stem}.{n}"


def _take_lock(wait: bool):
    """The host's one posting lock (the bot and "post N" by hand): the open file while held, None if busy."""
    WORK.mkdir(parents=True, exist_ok=True)
    fh = (WORK.parent / "due.lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        fh.close()
        return None
    return fh


def post_logged(n: int, batch: pathlib.Path, title: str | None, graphic: bool) -> dict | None:
    """"post N" by hand on the host: the bot's lock and log, so the once-a-day check sees both."""
    lock = _take_lock(wait=True)
    try:
        on = dt.datetime.now(DK).date().isoformat()
        key = _key(batch, n)
        posted = post(n, batch, True, title, graphic, lambda: _note(key, "sending", on), day=on)
        _note(key, "posted", on, batch=batch.stem, post=n, by="hand", **(posted or {}))
        return posted
    finally:
        lock.close()


def _post_on_server(n: int, batch: pathlib.Path, title: str | None, graphic: bool) -> None:
    """"post N" from the Mac while the server is the host: it goes out from the server, never from the Mac too. The
    batch file goes up first, since the server's copy can be behind."""
    if not (SERVER and SERVER_REPO):
        raise SystemExit("not posting: [posting] host is server, but server_ssh or server_repo is empty")
    rel = f"data/batches/{batch.name}"
    for _ in range(5):
        if subprocess.run(["scp", "-q", "-o", "ConnectTimeout=15", str(batch), f"{SERVER}:{SERVER_REPO}/{rel}"],
                          capture_output=True).returncode == 0:
            break
        time.sleep(10)
    else:
        raise SystemExit("not posting: the server, which does all the posting, can't be reached. Nothing went out.")
    cmd = " ".join([f"cd {shlex.quote(SERVER_REPO)} && CONTENT_ENGINE_ON_SERVER=1 {SERVER_PY} scripts/linkedin_post.py post", str(n),
                    "--batch", shlex.quote(rel), "--yes", *(["--title", shlex.quote(title)] if title else []),
                    *(["--graphic"] if graphic else [])])
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SERVER, cmd],
                       capture_output=True, text=True, timeout=1800)
    print(r.stdout.strip())
    pull_server()
    if r.returncode:
        raise SystemExit(r.stderr.strip()[-600:] or "the server's answer didn't come back: check your profile "
                         "(it can't post a second one today either way)")


def _autopost() -> dict:
    return {"mode": C.get("posting", "mode"), "host": C.get("posting", "host")}


def _rows() -> list[dict]:
    return [json.loads(l) for l in AUTOPOST_LOG.open() if l.strip()] if AUTOPOST_LOG.exists() else []


def _note(key: str, state: str, on: str, **more) -> None:
    AUTOPOST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUTOPOST_LOG.open("a") as fh:
        fh.write(json.dumps({"key": key, "state": state, "on": on, "mode": "live", **more}, ensure_ascii=False) + "\n")


def _tell(subject: str, lines: list[str]) -> None:
    """To the author's inbox like the engine's other notes (the Mac may be off)."""
    print(subject, *lines, sep="\n")
    try:
        import content_engine_feedback as fb
        fb.inbox(subject, lines)
    except Exception as e:  # noqa: BLE001
        print(f"(the note didn't reach the inbox: {e})")


def _lost_answers(on: str) -> None:
    """A post marked 'sending' with no answer after it: LinkedIn may have taken it, so it is never tried again (a second
    post is worse than a missing one) and the author is told once."""
    seen: dict[str, set] = {}
    for d in _rows():
        seen.setdefault(d["key"], set()).add(d.get("state"))
    for key, states in seen.items():
        if "sending" in states and not states & {"posted", "unknown"}:
            _note(key, "unknown", on)
            _tell(f"LinkedIn post {key}: check your profile", [
                "The bot started posting it and never got LinkedIn's answer, so it may or may not be live. It won't try again by itself.",
                f"If it isn't on your profile, post it by hand: linkedin_post.py post {key.split('.')[1]} --batch <batch {key.split('.')[0]}> --yes"])


def _token_warning(today: dt.date) -> None:
    """The token lasts 60 days and only the Mac can renew it (the browser): told 14 days ahead, then every 4 days."""
    if not TOKEN.exists():
        return
    ends = json.loads(TOKEN.read_text())["expires_at"]
    warned = [d["on"] for d in _rows() if d.get("state") == "token_warned"]
    if (ends - time.time()) / 86400 < 14 and (not warned or (today - dt.date.fromisoformat(max(warned))).days >= 4):
        _note("token", "token_warned", today.isoformat(), mode="")
        _tell(f"LinkedIn posting stops {dt.datetime.fromtimestamp(ends):%d %b}", [
            "The token that lets the bot post runs out then. Renew it on the Mac: python3 scripts/linkedin_post.py auth",
            "It opens LinkedIn in your browser, one click at most" + (", and copies itself to the server." if SERVER else ".")])


def pull_server() -> int:
    """Brings home what the server posted: the link into the batch file, a line in data/state/linkedin_posts.jsonl and
    the post's Check line in the doc. Silent when the server can't be reached: the next run tries again."""
    if not SERVER:
        return 0
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SERVER, f"cat {SERVER_LOG}"],
                           capture_output=True, text=True, timeout=90)
    except (subprocess.SubprocessError, OSError):
        return 0
    if r.returncode:
        return 0
    home = 0
    for line in r.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:  # a line cut off mid-write
            continue
        if not isinstance(row, dict) or row.get("state") != "posted":
            continue
        bf = BATCHES / f"{row['batch']}.json"
        posts = json.loads(bf.read_text())
        p = posts[row["post"] - 1]
        if (p.get("posted") or {}).get("urn") == row["urn"]:
            continue
        p["posted"] = {k: row.get(k, "") for k in ("at", "urn", "url", "media", "check_before")}
        bf.write_text(json.dumps(posts, ensure_ascii=False, indent=1))  # first, like post()
        with LOG.open("a") as f:
            f.write(json.dumps({"at": row["at"], "batch": row["batch"], "post": row["post"], "urn": row["urn"],
                                "url": row["url"], "media": row.get("media", ""), "from": "server"}) + "\n")
        print(f"from the server: {row['key']} posted {row['url']}")
        home += 1
        try:
            import content_engine_photos as ph
            ph._set_check(bf, posts, row["post"], f"Posted on LinkedIn {_when(dt.datetime.fromisoformat(row['at']))}: {row['url']}")
            bf.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
        except Exception as e:  # noqa: BLE001 - the post is live either way
            print(f"(the doc's Check line wasn't updated: {e})")
    return home


def due(now: dt.datetime | None = None, dry: bool = False) -> list[str]:
    """Post what is due by the plan, through LinkedIn's own API on the author's token only (no browser automation, no
    likes, comments or DMs), and only a post that is approved, has its photo or video and has its facts confirmed
    (readiness() == "ready"), whose date in the plan sheet has come (its slot time), that was NOT scheduled by hand
    (column J empty, so it never goes out twice), never more than DAY_CAP (1) a day, whatever posted it. [posting] mode:
    "test" (default) only logs what it would post; "live" posts; "off" does nothing. [posting] host: only that machine
    posts, so two machines can never both post. Every slot is acted on once: a failure before the post itself is
    retried MAX_TRIES times; once the post has gone to LinkedIn it is never sent again, whatever happens next."""
    cfg = _autopost()
    host, mode = cfg.get("host", "mac"), cfg.get("mode", "test")
    if host == "server" and not ON_SERVER and not dry:
        pull_server()
    if mode == "off" or (not dry and host != ("server" if ON_SERVER else "mac")):
        return []
    lock = _take_lock(wait=False)  # held until this returns
    if not lock:  # the run before is still uploading a video, or a post by hand is going out
        return []
    import content_engine_visuals as vis
    plan_file = C.STATE / "plan.json"
    dcfg = json.loads(plan_file.read_text()) if plan_file.exists() else {}
    if "plan" not in dcfg:
        return []
    rows = vis._google("sheets", "v4").spreadsheets().values().get(
        spreadsheetId=dcfg["plan"]["sheet"]["id"], range=f"{BOT_TAB}!A2:J").execute().get("values", [])
    now = now or dt.datetime.now(DK)
    on = now.date().isoformat()
    if not dry:
        _lost_answers(on)
        _token_warning(now.date())
    done = _rows()
    taken = [d for d in done if d.get("mode") == mode and d.get("state") != "failed"]
    acted = {d["key"] for d in taken}
    tries = collections.Counter(d["key"] for d in done if d.get("state") == "failed")
    out = []
    for r in rows:
        r = r + [""] * (10 - len(r))
        key, date_s, by_hand = r[0], r[3], r[9].strip()
        if out or (mode == "live" and posted_on(on)):  # never more than DAY_CAP (1) a day
            break
        if not key or not date_s or by_hand or key in acted or tries[key] >= MAX_TRIES:
            continue
        try:
            day = dt.datetime.strptime(date_s, "%a %d %b %Y").date()
            at = dt.datetime.combine(day, slot_time(day), DK)
        except ValueError:
            continue
        if at > now or now - at > dt.timedelta(hours=12):  # due, and not a slot left over from days ago
            continue
        b, n = key.split(".")
        bf = next((f for f in BATCHES.glob(f"*_Engine_Batch_{b}.json")), None)
        if not bf:
            continue
        p = json.loads(bf.read_text())[int(n) - 1]
        state, kind, _ = readiness(p, int(n))
        if state != "ready":
            continue
        entry = {"batch": bf.stem, "post": int(n), "slot": at.isoformat(), "mode": mode, "kind": kind,
                 "title": p.get("title", ""), "first_line": p["text"].strip().splitlines()[0][:120]}
        if dry:  # everything short of LinkedIn: the sheet, the batch, the rules, the files from Drive, the token
            print(f"DRY RUN at {now:%d %b %H:%M} {TZ_LABEL}: {key} is due (slot {at:%H:%M})")
            post(int(n), bf, False, None, False, day=on)
            for f in _download(media(p, int(n))[1], WORK / "dry"):
                print(f"fetched from Drive: {f.name}, {f.stat().st_size // 1024} KB")
                f.unlink()
            status()
            out.append(f"dry run, would post {key} ({kind})")
            continue
        if mode == "live":
            try:
                entry.update(post(int(n), bf, True, None, False, lambda: _note(key, "sending", on), day=on) or {})
            except (Exception, SystemExit) as e:  # noqa: BLE001 - told once, never a silent miss
                if isinstance(e, DayFull):
                    break
                if key in {d["key"] for d in _rows() if d.get("state") == "sending"}:
                    _lost_answers(on)  # it may have gone out: the day check stops anything more today
                    break
                _note(key, "failed", on, error=str(e)[:300])
                if tries[key] + 1 >= MAX_TRIES:
                    _tell(f"LinkedIn post {key} didn't go out", [
                        f"{MAX_TRIES} tries failed, the last one with: {str(e)[:300]}", "It won't try again by itself.",
                        "Post it by hand once the cause is fixed."])
                continue
        _note(key, "posted" if mode == "live" else "would", on, **entry)
        out.append(f"{'posted' if mode == 'live' else 'test, would post'} {key} ({kind}): {entry['first_line']}")
    for line in out:
        print("autopost:", line)
    if ON_SERVER and not out and not dry:  # one line per run, so the log's age shows the job is alive
        print(f"{now:%d %b %H:%M} {TZ_LABEL}: nothing due")
    return out


def selftest() -> None:
    global PLAN_DAYS, DAILY_FROM, PAUSES, SLOT_ANCHOR, SLOT_FROM, SLOT_TO, SLOT_APART, GAP
    # fixed test settings, whatever config.toml says
    PLAN_DAYS, DAILY_FROM, PAUSES = (0, 2, 4), dt.date(2026, 12, 1), ((dt.date(2026, 12, 1), dt.date(2026, 12, 24)),)
    SLOT_ANCHOR, SLOT_FROM, SLOT_TO, SLOT_APART, GAP = dt.date(2026, 11, 2), 6 * 60 + 40, 11 * 60 + 55, 40, "[AUTHOR"
    _SLOTS.clear()
    fails = []
    for raw, want in (("Hej (test) #1 *x* _y_ ~z~ a@b [c] <d> {e} |f| \\g",
                       "Hej \\(test\\) \\#1 \\*x\\* \\_y\\_ \\~z\\~ a\\@b \\[c\\] \\<d\\> \\{e\\} \\|f\\| \\\\g"),
                      ("4.000-5.000 kr. for æøå, sgu.", "4.000-5.000 kr. for æøå, sgu.")):
        if little(raw) != want:
            fails.append(f"little({raw!r}) gave {little(raw)!r}")
    g = {"file": "post1_1.png", "link": "https://drive.google.com/file/d/a/view"}
    ph = {"file": "post1_photo.jpg", "link": "https://drive.google.com/file/d/b/view"}
    cases = [
        (media({"format": "photo", "visual_files": [g]}, 1)[0].startswith("wait"), "a photo post without its photo waits"),
        (media({"format": "photo", "visual_files": [g]}, 1, graphic=True) == ("image", [g]), "--graphic posts the stand-in"),
        (media({"format": "photo", "visual_files": [g, ph]}, 1) == ("image", [ph]), "a photo post goes out with its photo"),
        (media({"format": "graphic", "visual_files": [{"file": "post6_1.png", "link": ""},
                                                      {"file": "post6_carousel.pdf", "link": ""}]}, 6)[0] == "document", "a carousel goes as its PDF"),
        (media({"format": "graphic", "visual_files": [{"file": "post6_1.png", "link": ""},
                                                      {"file": "post6_2.png", "link": ""}]}, 6)[0] == "document", "slides without a PDF too"),
        (media({"format": "video", "visual_files": [{"file": "post7_1.png", "link": ""}]}, 7)[0].startswith("wait"), "a video post waits for its video"),
        (media({"format": "video", "video_file": "v", "visual_files": [{"file": "post7_video_cover.png", "link": "c"}]}, 7)[0] == "video",
         "a video post with its video and cover"),
        (media({"format": "text"}, 2) == ("text", []), "a text post"),
        (media({"format": "text", "visual_files": [{"file": "post2_1.png", "link": ""}]}, 2)[0] == "image", "a text post with its graphic"),
        (_title({"text": "Du kan have verdens bedste produkt.\nMen..."}) == "Du kan have verdens bedste produkt.", "title = first line"),
        (readiness({"status": "approved", "format": "graphic", "visual_files": [g], "check": "Confirm the prices."}, 1)[0]
         == "ready", "the approval is the OK on the Check line too"),
        (readiness({"status": "approved", "format": "graphic", "visual_files": [g], "check": "Facts confirmed 30 Oct."}, 1)[0]
         == "ready", "a confirmed post is ready"),
        (readiness({"status": "approved", "format": "photo", "visual_files": [g, ph],
                    "check": "Facts confirmed 30 Oct. The photo is from your library, taken 2026-10-12: swap it if you have a newer one."},
                   1)[0] == "ready", "an informing photo note doesn't hold it back"),
        (readiness({"status": "approved", "format": "photo", "visual_files": [g, ph],
                    "check": "The photo has other people in it: OK to post it?"}, 1)[0]
         == "waiting: OK on the people in the photo", "a photo with other people in it still waits"),
        (readiness({"status": "approved", "format": "photo", "visual_files": [g], "check": ""}, 1)[0] == "waiting: photo",
         "a photo post waits for its photo"),
        (readiness({"status": "approved", "posted": {"urn": "x"}}, 1)[0] == "posted", "a posted post is done"),
        (next_slot({dt.date(2026, 11, 2)}, dt.date(2026, 11, 1)) == dt.date(2026, 11, 4), "next free Mon/Wed/Fri"),
    ]
    days = [d for d in (dt.date(2026, 11, 2) + dt.timedelta(days=k) for k in range(140)) if engine_day(d)]
    times = [slot_time(d) for d in days]
    mins = [t.hour * 60 + t.minute for t in times]
    cases += [
        (all(dt.time(6, 40) <= t <= dt.time(12, 0) for t in times), "every slot between 06:40 and noon"),
        (all(t.minute % 5 for t in times), "never on a round :x0 or :x5 minute"),
        (all(len(set(times[i:i + 9])) == len(times[i:i + 9]) for i in range(len(times))), "no time twice within 3 weeks"),
        (all(abs(a - b) >= SLOT_APART for a, b in zip(mins, mins[1:])), "never within 40 minutes of the posting day before"),
        (slot_time(dt.date(2026, 11, 13)) == slot_time(dt.date(2026, 11, 13)), "the same day always gives the same time"),
        (next_slot({dt.date(2026, 11, 13)}, dt.date(2026, 11, 13)) == dt.date(2026, 11, 16), "before daily_from only the weekdays"),
        (next_slot({dt.date(2026, 12, 25)}, dt.date(2026, 12, 25)) == dt.date(2026, 12, 26), "from daily_from every day"),
        (next_slot(set(), dt.date(2026, 12, 2)) == dt.date(2026, 12, 25), "never inside a pause"),
        (readiness({"status": "approved", "format": "text", "text": "Et eksempel.\n[AUTHOR, hvilket eksempel?]"}, 1)[0]
         .startswith("waiting: a gap"), "an approved post with an [AUTHOR ...] gap is never posted"),
        (assign_dates([{"key": "a", "timing": "anytime", "i": 0}, {"key": "b", "timing": "now", "post_by": "2026-12-30", "i": 1},
                       {"key": "c", "timing": "now", "post_by": "2026-11-20", "i": 2}], set(), dt.date(2026, 11, 16))
         == {"c": dt.date(2026, 11, 16), "b": dt.date(2026, 11, 18), "a": dt.date(2026, 11, 20)}, "time-bound first, soonest date first"),
        (assign_dates([{"key": "x", "timing": "now", "post_by": "2026-11-17", "i": 0}], {dt.date(2026, 11, 16)},
                      dt.date(2026, 11, 16)) == {"x": None}, "no free day in its window: late"),
        (not runway_alert_due(3, None, dt.date(2026, 11, 13)), "3 planned ahead: no alert"),
        (runway_alert_due(2, None, dt.date(2026, 11, 13)), "2 planned ahead: alert"),
        (not runway_alert_due(2, {"ahead": 2, "date": "2026-11-12"}, dt.date(2026, 11, 13)), "same count next day: quiet"),
        (runway_alert_due(1, {"ahead": 2, "date": "2026-11-13"}, dt.date(2026, 11, 13)), "drops further: alert again"),
        (runway_alert_due(2, {"ahead": 2, "date": "2026-11-11"}, dt.date(2026, 11, 13)), "still low 2 days on: again"),
    ]
    fails += [why for ok, why in cases if not ok]
    print("\n".join(fails) or f"selftest ok: {len(cases) + 2} cases")
    if fails:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["app", "auth", "status", "list", "post", "delete", "confirm", "plan", "due", "pull"])
    ap.add_argument("arg", nargs="*", help="client id (app), post number(s) (post, confirm) or post urn (delete)")
    ap.add_argument("--batch", type=pathlib.Path, help="batch json, default the newest")
    ap.add_argument("--title", help="title of a carousel or video on LinkedIn, default the post's first line")
    ap.add_argument("--graphic", action="store_true", help="a photo post goes out with its graphic instead of the photo")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry", action="store_true", help="due: everything short of LinkedIn, nothing is written")
    ap.add_argument("--at", help="due: pretend it is this time in the plan's time zone, e.g. '2026-11-04 09:31'")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.cmd is None:
        return ap.print_help()
    batch = (ROOT / a.batch if a.batch and not a.batch.is_absolute() else a.batch) or (latest_batch() if a.cmd in (
        "list", "post", "confirm") else None)
    arg = a.arg[0] if a.arg else None
    if a.cmd == "app":
        store_app(arg)
    elif a.cmd == "auth":
        auth()
    elif a.cmd == "status":
        status()
    elif a.cmd == "list":
        listing(batch)
    elif a.cmd == "post":
        if ON_SERVER and a.yes:
            post_logged(int(arg), batch, a.title, a.graphic)
        else:
            post(int(arg), batch, a.yes, a.title, a.graphic)
    elif a.cmd == "delete":
        delete(arg, a.yes)
    elif a.cmd == "confirm":
        confirm([int(x) for x in a.arg], batch)
    elif a.cmd == "plan":
        plan()
    elif a.cmd == "due":
        if a.at and not a.dry:
            raise SystemExit("--at only with --dry: a real run goes by the real time")
        due(dt.datetime.fromisoformat(a.at).replace(tzinfo=DK) if a.at else None, a.dry)
    elif a.cmd == "pull":
        pull_server()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
