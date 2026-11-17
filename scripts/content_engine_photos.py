#!/usr/bin/env python3
"""Content engine photos: real photos of the author under the photo posts.

Real photos of the author beat graphics, so at least half the posts are photo or video posts. Two sources:
  - a Drive folder ([google] photos_folder_id): each new photo is read once (Claude looks at a small copy: place, what
    the author is doing, mood, who else is in it) and only that description is kept in data/kb/photos.json, never the
    image. For every photo post without a photo the best match is put under the post in the doc; a photo with other
    people in it gets a Check line.
  - the Photos library on the Mac (macOS only): recent photos where the author looks like they do now, cropped 4:5
    around the face; an older one only for a post about that time.

  --library BATCH_JSON    pick straight from the Photos library on the Mac, crop 4:5 around the face
  --refresh BATCH_JSON    swap out library photos too old for their post, then pick (the daily job does this)
  --selftest              check the mechanical photo rules
  --install-app           (re)build ~/Applications/Content Engine Photos.app and the face finder
  --auto                  describe new photos in the Drive folder, then pick for the newest batch
  --pick BATCH_JSON       pick photos for one batch's photo posts from the Drive folder (--dry-run to only show the choices)
  --describe-local FILE   test the description on a local file
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import functools
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402
import content_engine_draft as ced  # noqa: E402
import soft_jpeg  # noqa: E402

LIB = C.KB / "photos.json"
WHO = ced.NAME
EXTRA_RULES = (" " + C.get("photos", "extra_rules").strip()) if C.get("photos", "extra_rules") else ""
DESC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["place", "what", "mood", "people", "good_for"],
               "properties": {"place": {"type": "string"}, "what": {"type": "string"}, "mood": {"type": "string"},
                              "people": {"type": "string", "enum": ["only the author", "the author with others", "others, not the author", "nobody"]},
                              "good_for": {"type": "string", "description": "what kind of post story this photo would fit"}}}
PICK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["picks"], "properties": {"picks": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["post", "photo", "why"],
                               "properties": {"post": {"type": "integer"}, "photo": {"type": "string", "description": "photo id, or '' when none fits"},
                                              "why": {"type": "string"}}}}}}


def _small_jpeg(path: pathlib.Path) -> bytes:
    """Any photo (HEIC from an iPhone too) as a ~1000 px JPEG, on the CPU (soft_jpeg: sips used the scaler chip)."""
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "small.jpg"
        if not soft_jpeg.to_jpeg(path, out, 1000):
            raise RuntimeError(f"can't read photo {path.name}")
        return out.read_bytes()


def describe(path: pathlib.Path) -> dict:
    """Claude looks at a small copy on the subscription (llm_guard's subscription route is text only, so this
    runs the same locked-down `claude -p` with one tool, Read, inside a temp folder that holds only the photo)."""
    import os
    import llm_guard
    with tempfile.TemporaryDirectory(prefix="photo_") as cwd:
        (pathlib.Path(cwd) / "photo.jpg").write_bytes(_small_jpeg(path))
        cmd = [llm_guard.CLAUDE_BIN, "-p", "--output-format", "json", "--tools", "Read", "--allowedTools", "Read",
               "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands",
               "--model", ced.MODEL, "--json-schema", json.dumps(DESC_SCHEMA), "--system-prompt",
               f"You describe a photo of {WHO} ({C.get('photos', 'describe_hint')}) for picking LinkedIn post images. "
               "They put it in their own folder, so the one person in it is them, also seen from behind or from the side. "
               "Plain English, short. Describe only what is visible; never guess names or places you can't see."]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.run(cmd, input="Read the image file photo.jpg in this folder and describe the photo.",
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    out = json.loads(proc.stdout)
    if out.get("is_error") or out.get("structured_output") is None:
        raise RuntimeError(f"photo description failed: {str(out.get('result'))[:200]}")
    return out["structured_output"]


def _lib() -> dict:
    return json.loads(LIB.read_text()) if LIB.exists() else {}


def sync_new() -> int:
    """Describe every photo in the Drive photos folder that isn't described yet."""
    import content_engine_visuals as vis
    from googleapiclient.http import MediaIoBaseDownload
    folder = C.get("google", "photos_folder_id")
    if not folder:
        return 0
    drive, lib, n = vis._google("drive", "v3"), _lib(), 0
    files = drive.files().list(q=f"'{folder}' in parents and trashed = false and mimeType contains 'image/'",
                               fields="files(id, name, createdTime)", pageSize=200).execute().get("files", [])
    for f in files:
        if f["id"] in lib:
            continue
        with tempfile.TemporaryDirectory() as d:
            raw = pathlib.Path(d) / f["name"]
            with raw.open("wb") as fh:
                dl, done = MediaIoBaseDownload(fh, drive.files().get_media(fileId=f["id"])), False
                while not done:
                    _, done = dl.next_chunk()
            lib[f["id"]] = {"name": f["name"], "added": f["createdTime"][:10], **describe(raw), "used_in": []}
        n += 1
    LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=1))
    return n


def pick(batch_file: pathlib.Path, dry: bool = False) -> list[dict]:
    import llm_guard
    from anthropic import Anthropic
    lib, posts = _lib(), json.loads(batch_file.read_text())
    want = [(i, p) for i, p in enumerate(posts, 1) if p.get("format") == "photo" and p.get("status") != "dropped"
            and not any(v["file"].startswith(f"post{i}_photo") for v in p.get("visual_files", []))]
    free = {k: v for k, v in lib.items() if not v.get("used_in")}
    if not want or not free:
        return []
    ask = ("Photos (id: description):\n" + "\n".join(f"{k}: {json.dumps({x: v[x] for x in DESC_SCHEMA['required']}, ensure_ascii=False)}"
                                                   for k, v in free.items()) +
           "\n\nPosts that need a photo:\n" + "\n\n".join(f"Post {i}, visual note: {p.get('visual', '')}\n{p['text'][:900]}" for i, p in want) +
           "\n\nPick the best photo for each post. Each photo once at most. Prefer photos of the author alone. A photo of "
           f"them from behind or from the side is fine when it fits the story.{EXTRA_RULES} Use '' when no photo fits the story well.")
    msg = llm_guard.guarded(Anthropic()).messages.create(
        model=ced.MODEL, max_tokens=1500, system=f"You pick real photos of {WHO} for their LinkedIn posts.",
        messages=[{"role": "user", "content": ask}], output_config={"format": {"type": "json_schema", "schema": PICK_SCHEMA}})
    picks = [x for x in json.loads(msg.content[0].text)["picks"] if x["photo"] in free]
    if dry or not picks:
        return picks
    import content_engine_visuals as vis
    from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
    drive = vis._google("drive", "v3")
    spec_file = batch_file.with_name(batch_file.stem + "_visuals.json")
    spec = json.loads(spec_file.read_text()) if spec_file.exists() else {"batch": batch_file.stem, "visuals": []}
    folder = vis.graphics_folder(drive, spec["batch"])
    for x in picks:
        with tempfile.TemporaryDirectory() as d:
            raw = pathlib.Path(d) / lib[x["photo"]]["name"]
            with raw.open("wb") as fh:
                dl, done = MediaIoBaseDownload(fh, drive.files().get_media(fileId=x["photo"])), False
                while not done:
                    _, done = dl.next_chunk()
            jpg = pathlib.Path(d) / f"post{x['post']}_photo.jpg"
            if not soft_jpeg.to_jpeg(raw, jpg, 2048):
                raise RuntimeError(f"can't read photo {raw.name}")
            up = drive.files().create(body={"name": jpg.name, "parents": [folder["id"]]},
                                      media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg"), fields="id, webViewLink").execute()
        p = posts[x["post"] - 1]
        p.setdefault("visual_files", []).append({"file": f"post{x['post']}_photo.jpg", "link": up["webViewLink"]})
        if lib[x["photo"]]["people"] != "only the author":
            p["check"] = (p.get("check", "") + " The photo shows other people: OK to post it?").strip()
        lib[x["photo"]]["used_in"].append(f"{batch_file.stem} #{x['post']}")
    batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=1))
    spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1))
    vis.into_doc(spec_file)
    return picks


# ---------- straight from the Photos library on the Mac ----------
APP = pathlib.Path.home() / "Applications" / "Content Engine Photos.app"
WORK = C.CACHE / "photos"
FACES = C.CACHE / "bin" / "photo_faces"
# Recent photos only, so the author's face doesn't jump between posts; an older one only when the post is about that
# time. The last 3 months first, up to 6 when fewer than 4 photos fit, and always checked against close-ups of the
# newest photos.
RECENT_DAYS, MAX_DAYS = 90, 180
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["searches", "from_year", "to_year", "quote"], "properties": {
    "searches": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}},
    "from_year": {"type": "integer", "description": "0 = a photo of the author as they look now (almost always)"},
    "to_year": {"type": "integer", "description": "0 = a photo of the author as they look now"},
    "quote": {"type": "string", "description": "the exact words in the post that place its story in that year; '' for now"}}}
_LANG = C.get("photos", "search_language")
PLAN_SYSTEM = (f"You plan the photo of {WHO} for their LinkedIn post. searches: up to 3 single {_LANG} words for the Apple "
               f"Photos search field (the Mac's language is {_LANG}) that describe the place or thing a fitting photo of the "
               "author would show, e.g. 'computer', 'solnedgang', 'strand', 'kontor', 'kaffe', 'scene'; one word each, lower "
               "case, most fitting first. from_year and to_year: almost always 0, meaning a photo of the author as they look "
               "now. Only when the post is a story about one past time and a photo of them from back then would show the "
               "reader that moment (like 'Da jeg startede i 2016' or "
               "'For fem år siden stod jeg ...'), give that year or years, counted from today's date, and put the exact words "
               "from the post that place the story there in quote. A post that only mentions the past in passing gets 0.")
PICK1_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["pick", "why", "others_in_photo", "looks_like_now"], "properties": {
    "pick": {"type": "integer", "description": "the number on the sheet, 0 when none fits"},
    "why": {"type": "string"}, "others_in_photo": {"type": "boolean"},
    "looks_like_now": {"type": "boolean", "description": "in the picked photo the author looks like the N tiles: hair, beard, glasses, build"}}}
# The notes this file writes into a post's Check line, so a later run can take its own old ones out again
OWN_NOTES = [r"The photo has other people in it: OK to post it\?",
             r"The photo is from your library, taken \d{4}-\d\d-\d\d: swap it if you have a newer one\.",
             r"The photo is from \d{4}, because the post is about then \(\"[^\"]*\"\): swap it if you have a better one from that time\.",
             r"Waiting for a recent photo of you[^.]*\.",
             r"No recent photo of you fits this post yet[^.]*\."]
# Newly synced iCloud photos can reach the Mac long before Photos' search can find them, so the pick waits for the
# search and says so
SORTING = ("Waiting for a recent photo of you: your new photos are on the Mac now, but Photos is still sorting them and "
           "can't search them yet, so it comes once that's done.")


def _sorting(info: dict) -> bool:
    """Recent photos are on the Mac, but Photos' search finds none of them yet."""
    return info.get("recent:all", {}).get("in_window", 0) >= 20 and not info.get("search:person", {}).get("in_window")


_HOLDING = False


@contextlib.contextmanager
def _one_run():
    """One photo run at a time: every run clears the shared export folder, so a second one would delete the first
    one's copies mid-pick."""
    global _HOLDING
    if _HOLDING:
        yield
        return
    WORK.mkdir(parents=True, exist_ok=True)
    with open(WORK / "lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another photo run is still busy") from None
        _HOLDING = True
        try:
            yield
        finally:
            _HOLDING = False


def _locked(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        with _one_run():
            return fn(*a, **k)
    return run


def install_app() -> None:
    """The helper app that may ask Photos for pictures (only search + export copies), plus the Vision face finder.
    Rebuilding the app makes macOS ask for its permission again."""
    import plistlib
    APP.parent.mkdir(exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    script = (ROOT / "scripts" / "photos_export.applescript").read_text().replace("__WORK_DIR__", str(WORK) + "/")
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as fh:
        fh.write(script)
    subprocess.run(["osacompile", "-o", str(APP), fh.name], check=True)
    pathlib.Path(fh.name).unlink()
    info = APP / "Contents" / "Info.plist"
    pl = plistlib.loads(info.read_bytes())
    pl.update({"CFBundleIdentifier": "local.content-engine.photos", "CFBundleName": "Content Engine Photos", "LSUIElement": True,
               "NSAppleEventsUsageDescription": "Content Engine Photos asks Photos for pictures of you for your LinkedIn posts. "
                                                "It only searches and exports copies; it never changes or deletes anything."})
    info.write_bytes(plistlib.dumps(pl))
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(APP)], check=True, capture_output=True)
    FACES.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["swiftc", "-O", str(ROOT / "scripts" / "photo_faces.swift"), "-o", str(FACES)], check=True)
    print(f"built {APP} and {FACES}")


@_locked
def library_search(asks: list[tuple], days: int = 730) -> tuple[list[dict], dict]:
    """Ask the photos app for the newest photos per ask, (kind, words, max[, newer than days, older than days]):
    "search" uses Photos' own search field (Danish), "album" an album's name, "recent" every photo in the window;
    max 0 only counts. Without the two bounds an ask looks at the last `days` days. Then read faces and Apple's
    quality score for every copy. Returns (rows, per ask: found, in_window, newest, newest_in_window, errors)."""
    import shutil
    if not APP.exists() or not FACES.exists():
        install_app()
    if subprocess.run(["pgrep", "-f", "Content Engine Photos.app/Contents/MacOS/applet"], capture_output=True).returncode == 0:
        raise RuntimeError("the photos app is still busy with an earlier run")  # a killed run leaves the app exporting
    shutil.rmtree(WORK / "export", ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    WORK.chmod(0o700)
    for f in ("manifest.tsv", "done", "error.txt"):
        (WORK / f).unlink(missing_ok=True)
    (WORK / "request.txt").write_text(f"{days}\n" + "".join("\t".join(str(x) for x in a) + "\n" for a in asks))
    subprocess.run(["open", "-W", "-g", str(APP)], check=True, timeout=3600)
    if (WORK / "error.txt").exists():
        raise RuntimeError(f"photos app: {(WORK / 'error.txt').read_text()[:300]}")
    rows, info = [], {}
    for line in (WORK / "manifest.tsv").read_text().splitlines() if (WORK / "manifest.tsv").exists() else []:
        parts = (line.split("\t") + [""] * 6)[:6]
        if parts[0] == "#":
            info.setdefault(parts[1], {}).update({"found": int(parts[2] or 0), "in_window": int(parts[3] or 0),
                                                  "newest": parts[4][:10], "newest_in_window": parts[5][:10]})
        elif parts[0] == "!":
            info.setdefault(parts[1], {}).setdefault("errors", []).append(parts[2][:120])
        elif parts[0].isdigit():
            files = sorted((WORK / "export" / parts[0]).glob("*"))
            if files:
                rows.append({"n": int(parts[0]), "ask": parts[1], "id": parts[2], "file": parts[3], "date": parts[4][:10],
                             "path": str(files[0])})
    if rows:
        out = subprocess.run([str(FACES)] + [r["path"] for r in rows], capture_output=True, text=True, timeout=1800).stdout
        faces = {d["path"]: d for d in (json.loads(l) for l in out.splitlines() if l.strip())}
        for r in rows:
            r.update({k: faces.get(r["path"], {}).get(k) for k in ("w", "h", "faces", "aesthetics", "utility")})
    return rows, info


# The library, copied in the background. Exporting from Photos can be slow (iCloud downloads while Photos still sorts
# newly synced photos), so an hourly run that asks for many photos at once is killed before it uses any. Instead an
# export round of ROUND photos runs on its own; each hourly run takes in what the last round finished and starts the
# next, and the pick chooses from those copies.
CACHE = WORK / "cache"
ROUND = 12


def _cache() -> dict:
    f = CACHE / "index.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _applet_busy() -> bool:
    return subprocess.run(["pgrep", "-f", "Content Engine Photos.app/Contents/MacOS/applet"], capture_output=True).returncode == 0


def _next_asks(dates: list[str], today: dt.date) -> list[tuple]:
    """The next export round: photos newer than the newest copy (once that is a day old), then further back than the
    oldest copy, to MAX_DAYS. (kind, words, max, newer than days, older than days), as the photos app reads them."""
    if not dates:
        return [("recent", "all", ROUND, MAX_DAYS + 1, 0)]
    asks = []
    since = (today - dt.date.fromisoformat(max(dates))).days
    if since >= 1:
        asks.append(("recent", "all", ROUND, since, 0))
    back = (today - dt.date.fromisoformat(min(dates))).days + 1
    if back <= MAX_DAYS:
        asks.append(("recent", "all", ROUND, MAX_DAYS + 1, back))
    return asks


READER = pathlib.Path.home() / "Applications" / "Content Engine Photos Reader.app"
KEEP = 300  # copies at most: the newest photos the author is in


def _reader_scan() -> dict | None:
    """The photo list straight from the library (photo_library_reader.py, run by its own app that has Full Disk
    Access); None while it can't read the library."""
    if not READER.exists():
        return None
    (WORK / "reader_error.txt").unlink(missing_ok=True)
    subprocess.run(["open", "-W", "-g", str(READER)], timeout=900)
    f = WORK / "library.json"
    if (WORK / "reader_error.txt").exists() or not f.exists():
        return None
    d = json.loads(f.read_text())
    return d if dt.datetime.now() - dt.datetime.fromisoformat(d["scanned"]) < dt.timedelta(hours=1) else None


def _take_in(idx: dict, items: list[tuple[str, str, pathlib.Path, dict]]) -> int:
    """Copies (2400 px) of new photos into the cache with their faces read; a photo without a face keeps only its date."""
    new = []
    for key, day, src, extra in items:
        copy = CACHE / (re.sub(r"[^A-Za-z0-9]+", "_", key) + ".jpg")
        if src != copy and not soft_jpeg.to_jpeg(src, copy, 2400):
            continue
        idx[key] = {"date": day, "file": copy.name, **extra}
        new.append(key)
    from PIL import Image, ImageOps
    for k in new:  # the phone's rotation into the pixels, so faces, contact sheets and crops all see it upright
        path = CACHE / idx[k]["file"]
        with Image.open(path) as im:
            turned = im.getexif().get(0x0112, 1) != 1
            if turned:
                ImageOps.exif_transpose(im).convert("RGB").save(path, quality=92)
    if new:
        out = subprocess.run([str(FACES)] + [str(CACHE / idx[k]["file"]) for k in new], capture_output=True, text=True, timeout=1800).stdout
        faces = {d["path"]: d for d in (json.loads(l) for l in out.splitlines() if l.strip())}
        for k in new:
            f = faces.get(str(CACHE / idx[k]["file"]), {})
            idx[k].update({x: f.get(x) for x in ("w", "h", "faces", "aesthetics", "utility")})
            if not f.get("faces"):
                (CACHE / idx[k]["file"]).unlink(missing_ok=True)
                idx[k]["file"] = ""
    return len(new)


@_locked
def library_sync() -> str:
    """Take in the photos the last export round finished, as copies (2400 px) with their faces read; a copy without a
    face isn't kept (only its date, so it isn't asked for again). Copies older than MAX_DAYS go. Then start the next
    round in the background, never waiting for Photos."""
    import shutil
    CACHE.mkdir(parents=True, exist_ok=True)
    WORK.chmod(0o700)
    scan = _reader_scan()
    if scan:  # straight from the library: seconds, nothing downloaded, Photos' own face and people data
        idx = _cache()
        pool = [r for r in scan["photos"] if r.get("copy")]  # the reader copies the newest photos Photos knows are the author
        n = _take_in(idx, [(r["id"], r["date"], CACHE / r["copy"], {"me": True, "people": r["people"]})
                           for r in pool if r["id"] not in idx])
        keep = {r["id"] for r in pool}
        for k in [k for k in idx if k not in keep]:  # no longer among the newest: the copy goes
            if idx[k].get("file"):
                (CACHE / idx[k]["file"]).unlink(missing_ok=True)
            del idx[k]
        (CACHE / "index.json").write_text(json.dumps(idx, indent=1))
        return (f"library read directly: {len(scan['photos'])} photos in {scan['days']} days, {len(pool)} of you copied; "
                f"took in {n}, {sum(1 for v in idx.values() if v.get('file'))} copies with a face")
    if _applet_busy():
        return "an export round is still running"
    idx, items = _cache(), []
    manifest = WORK / "manifest.tsv"
    for line in manifest.read_text().splitlines() if manifest.exists() else []:
        parts = (line.split("\t") + [""] * 6)[:6]
        files = sorted((WORK / "export" / parts[0]).glob("*")) if parts[0].isdigit() else []
        if not files or parts[2] in idx:
            continue
        if files[0].suffix.lower() not in (".jpg", ".jpeg", ".heic", ".png"):  # a video: read, never a post photo
            idx[parts[2]] = {"date": parts[4][:10], "file": ""}
            continue
        items.append((parts[2], parts[4][:10], files[0], {}))
    new = [k for k, *_ in items][:_take_in(idx, items)]
    shutil.rmtree(WORK / "export", ignore_errors=True)
    for f in ("manifest.tsv", "done", "error.txt"):
        (WORK / f).unlink(missing_ok=True)
    cut = (dt.date.today() - dt.timedelta(days=MAX_DAYS)).isoformat()
    for k in [k for k, v in idx.items() if v["date"] < cut]:
        if idx[k]["file"]:
            (CACHE / idx[k]["file"]).unlink(missing_ok=True)
        del idx[k]
    (CACHE / "index.json").write_text(json.dumps(idx, indent=1))
    asks = _next_asks([v["date"] for v in idx.values()], dt.date.today())
    if asks:
        if not APP.exists() or not FACES.exists():
            install_app()
        (WORK / "request.txt").write_text(f"{MAX_DAYS}\n" + "".join("\t".join(map(str, a)) + "\n" for a in asks))
        subprocess.Popen(["open", "-g", str(APP)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (f"took in {len(new)} photo(s); {len(idx)} read so far, {sum(1 for v in idx.values() if v.get('file'))} with a face; "
            + ("next round started" if asks else "all of the last 6 months read"))


def crop_45(w: int, h: int, face: dict | None) -> tuple[int, int, int, int]:
    """The largest 4:5 frame in the photo, the face a bit above the middle (zoomed in only when the face is tiny)."""
    cw, ch = (round(h * 0.8), h) if w / h > 0.8 else (w, round(w / 0.8))
    fx, fy = (w / 2, h * 0.4) if not face else ((face["x"] + face["w"] / 2) * w, (face["y"] + face["h"] / 2) * h)
    if face and face["h"] * h < 0.09 * ch:  # a tiny face in a wide shot: come closer, but keep at least 1080 px wide
        scale = max(face["h"] * h / (0.13 * ch), 1080 / cw if cw >= 1080 else 1)
        cw, ch = round(cw * scale), round(ch * scale)
    x0 = min(max(fx - cw / 2, 0), w - cw)
    y0 = min(max(fy - 0.38 * ch, 0), h - ch)
    return round(x0), round(y0), round(x0 + cw), round(y0 + ch)


def face_box(w: int, h: int, face: dict) -> tuple[int, int, int, int]:
    """A 4:5 close-up around one face (head and a bit of shoulder), for comparing how the author looks."""
    ch = face["h"] * h * 2.6
    cw = ch * 0.8
    if cw > w:
        cw, ch = w, w / 0.8
    if ch > h:
        cw, ch = h * 0.8, h
    cx, cy = (face["x"] + face["w"] / 2) * w, (face["y"] + face["h"] / 2) * h
    x0, y0 = min(max(cx - cw / 2, 0), w - cw), min(max(cy - ch * 0.45, 0), h - ch)
    return round(x0), round(y0), round(x0 + cw), round(y0 + ch)


def _main_face(row: dict) -> dict | None:
    return max(row["faces"], key=lambda f: f["w"] * f["h"]) if row.get("faces") else None


def _day(iso: str) -> str:
    return f"{dt.date.fromisoformat(iso[:10]):%-d %b %Y}" if iso else ""


def _sheet(cands: list[dict], out: pathlib.Path, refs: list[dict], portrait: bool = False) -> None:
    """Reference tiles first (N1.. = close-ups from the newest photos, how the author looks now; R = a known photo,
    [photos] reference_photo), then the candidates numbered from 1, each cropped as it would post, every tile with the
    date it was taken."""
    from PIL import Image, ImageDraw, ImageFont
    cols, tw, th = 4, 300, 375
    ref = C.path(C.get("photos", "reference_photo")) if C.get("photos", "reference_photo") else None
    tiles = [("R", Image.open(ref).convert("RGB"), None, "known photo")] if portrait and ref and ref.exists() else []
    for j, r in enumerate(refs, 1):
        im = Image.open(r["path"]).convert("RGB")
        tiles.append((f"N{j}", im.crop(face_box(im.width, im.height, _main_face(r))), None, _day(r["date"])))
    tiles += [(str(i + 1), Image.open(c["path"]).convert("RGB"), _main_face(c), _day(c["date"])) for i, c in enumerate(cands)]
    sheet = Image.new("RGB", (cols * tw, ((len(tiles) + cols - 1) // cols) * th), "white")
    bold = str(C.path(C.get("visuals", "font_bold")))
    font, small = ImageFont.truetype(bold, 30), ImageFont.truetype(bold, 21)
    d = ImageDraw.Draw(sheet)
    for k, (label, im, face, when) in enumerate(tiles):
        if not label.startswith("N"):  # the N close-ups are already 4:5
            im = im.crop(crop_45(im.width, im.height, face))
        im = im.resize((tw - 8, th - 8))
        x, y = (k % cols) * tw + 4, (k // cols) * th + 4
        sheet.paste(im, (x, y))
        ref = not label.isdigit()
        d.rounded_rectangle((x + 8, y + 8, x + 78, y + 54), radius=10, fill=(132, 206, 24) if ref else (25, 34, 48))
        d.text((x + 43, y + 31), label, font=font, fill=(25, 34, 48) if ref else "white", anchor="mm")
        if when:
            tw_ = d.textlength(when, font=small)
            d.rounded_rectangle((x + 8, y + th - 50, x + 24 + tw_, y + th - 16), radius=8, fill=(25, 34, 48))
            d.text((x + 16, y + th - 33), when, font=small, fill="white", anchor="lm")
    sheet.save(out, quality=88)


def _claude_on_image(image: pathlib.Path, prompt: str, schema: dict, system: str) -> dict:
    """claude -p on the subscription with one tool, Read, in a temp folder that holds only this image."""
    import os
    import shutil
    import llm_guard
    with tempfile.TemporaryDirectory(prefix="pick_") as cwd:
        shutil.copy(image, pathlib.Path(cwd) / "sheet.jpg")
        cmd = [llm_guard.CLAUDE_BIN, "-p", "--output-format", "json", "--tools", "Read", "--allowedTools", "Read",
               "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands",
               "--model", ced.MODEL, "--json-schema", json.dumps(schema), "--system-prompt", system]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        for attempt in range(3):  # a dropped connection is worth another go
            proc = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
            out = json.loads(proc.stdout)
            if not (out.get("is_error") and "reach the API" in str(out.get("result"))):
                break
            time.sleep(20)
    if out.get("is_error") or out.get("structured_output") is None:
        raise RuntimeError(f"photo pick failed: {str(out.get('result'))[:200]}")
    return out["structured_output"]


def _era(text: str, plan: dict, today: dt.date) -> dict | None:
    """An older photo only when the post itself is a story about that time and says so in its own words."""
    fy = plan.get("from_year") or 0
    ty = plan.get("to_year") or fy
    quote = " ".join((plan.get("quote") or "").split()).replace('"', "'")
    if not fy or len(quote) < 4 or quote.lower() not in " ".join(text.split()).replace('"', "'").lower():
        return None
    if not 2000 <= fy <= ty < today.year or ty - fy > 2:
        return None
    return {"from": dt.date(fy, 1, 1), "to": dt.date(ty, 12, 31), "quote": quote, "years": str(fy) if fy == ty else f"{fy} to {ty}"}


def _bounds(start: dt.date, end: dt.date) -> tuple[int, int]:
    """A date window as the helper app's "newer than N days, older than M days", counted from the real today."""
    real = dt.date.today()
    return (real - start).days + 1, max((real - end).days, 0)


def _with_notes(check: str, notes: list[str]) -> str:
    """A Check line without this file's own old notes, plus the new ones."""
    for pat in OWN_NOTES:
        check = re.sub(pat, "", check)
    return " ".join((check + " " + " ".join(notes)).split())


PICK_SYSTEM = (f"You pick a real photo of {WHO} for their LinkedIn post. {{who}} Natural and friendly, fitting the story's "
               "mood; it doesn't have to show the story's own place or work, a good photo of the author beats none, also one "
               "from behind or from the side. Never any nudity: no bare chest, no swimwear, no towel. Never a private-looking "
               "photo (bed, bathroom, partying, children, pulling faces), never one where the author is not clearly the "
               "subject. Only a sharp, well-lit photo in focus, never blurry, dark or grainy. It must look like a good "
               "LinkedIn photo: face at eye level or a little above, friendly or calm, looking at the camera or naturally "
               "aside. Never a close-up from below, a mirror selfie, looking down at a phone, half-closed eyes or an odd "
               "expression. Quality first: never pick a weaker photo just because it is from another day. Prefer the author "
               f"alone.{EXTRA_RULES.replace('{', '{{').replace('}', '}}')} Answer 0 only when no photo is acceptable at all.")
LOOK_NOW = ("The author must clearly be the person in it and the subject, and must look the way they do now (the N "
            "tiles): same hair, beard, glasses and build. A face changes over time and the posts must not jump between "
            "looks, so skip any candidate where they look noticeably different from the N tiles, however good the photo "
            "is. Among equally good photos, prefer the newest.")
LOOK_THEN = ("The author must clearly be the person in it (tile R is a known photo of them) and the subject. The photo is "
             "from the time the post is about, so they may look younger or different than in R.")


MIN_WIDTH = 1440  # the posted photo is 1440x1800; a smaller crop would be blown up and soft


def _sharp_enough(v: dict) -> bool:
    """The 4:5 crop around the face is at least MIN_WIDTH wide."""
    if not (v.get("w") and v.get("h") and v.get("faces")):
        return False
    x0, y0, x1, y1 = crop_45(v["w"], v["h"], max(v["faces"], key=lambda f: f["w"] * f["h"]))
    return x1 - x0 >= MIN_WIDTH


def _photo_day(entry: dict) -> str:
    return (entry.get("taken") or "")[:10]


def posts_photo_entries(posts: list[dict], lib: dict) -> list[dict]:
    """The library photos already under this batch's posts (their days count as used)."""
    tags = {t for t in (f"#{i}" for i in range(1, len(posts) + 1))}
    return [v for v in lib.get("library", {}).values() if any(u.split(" ")[-1] in tags for u in v.get("used_in", []))]


@_locked
def rejected() -> dict:
    """Photos taken off a post for their look (unflattering angle, a close-up from below): never picked again. Ids
    only, kept next to the photo cache."""
    f = WORK / "rejected.json"
    return json.loads(f.read_text()) if f.exists() else {}


def reject(photo_id: str, why: str) -> None:
    r = rejected()
    r[photo_id] = {"why": why, "on": dt.date.today().isoformat()}
    (WORK / "rejected.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))


def library_pick(batch_file: pathlib.Path, dry: bool = False, today: dt.date | None = None) -> list[dict]:
    """For every photo post without a photo: search the library, pick the best photo of the author, crop it 4:5 around
    the face and put it under the post. Recent photos where they look the way they do now (RECENT_DAYS, MAX_DAYS); an
    older one only for a post that is a story about that time. No fitting photo: a short Check note says why.
    `today` pretends another date (to test on older photos). The candidates' copies are deleted afterwards."""
    import shutil
    import llm_guard
    from anthropic import Anthropic
    from PIL import Image
    today = today or dt.date.today()
    posts, lib = json.loads(batch_file.read_text()), _lib()
    used = {k for k, v in lib.get("library", {}).items() if v.get("used_in")}
    no_go = set(rejected())
    want = [(i, p) for i, p in enumerate(posts, 1) if p.get("format") == "photo" and p.get("status") != "dropped"
            and not any(v["file"].startswith(f"post{i}_photo") for v in p.get("visual_files", []))]
    if not want:
        return []
    client = llm_guard.guarded(Anthropic())
    plan, era = {}, {}
    for i, p in want:
        msg = client.messages.create(
            model=ced.MODEL, max_tokens=400, system=PLAN_SYSTEM, output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            messages=[{"role": "user", "content": f"Today is {today:%-d %B %Y}.\nVisual note: {p.get('visual', '')}\n\n{p['text'][:1500]}"}])
        plan[i] = json.loads(msg.content[0].text)
        era[i] = _era(p["text"], plan[i], today)
    groups = {}  # one search per time window: a past time a post is about, then now (which also takes those that found nothing)
    for i, _ in want:
        if era[i]:
            groups.setdefault((era[i]["from"], era[i]["to"]), []).append(i)
    groups["now"] = [i for i, _ in want if not era[i]]
    picks, notes, refs, taken, sorting, on_mac, searched = [], {}, [], set(), False, {}, False
    # the days of every photo already under an engine post: a new pick goes to another day first, so the feed doesn't
    # show the same afternoon post after post
    days_used = {_photo_day(v) for v in lib.get("library", {}).values() if v.get("used_in")} - {""}
    # inside one batch a day is never used twice; a photo post with no other day left becomes a graphic post and keeps
    # its graphic
    batch_days = {_photo_day(v) for v in lib.get("library", {}).values()
                  if any(u.startswith(f"{batch_file.stem} #") for u in v.get("used_in", []))} - {""}
    try:
        for key in list(groups):
            ids = groups[key]
            if not ids:
                continue
            now = key == "now"
            start, end = (today - dt.timedelta(days=MAX_DAYS), today) if now else key
            words = sorted({w.strip().lower() for i in ids for w in plan[i]["searches"] if w.strip()})
            nb, ob = _bounds(start, end)
            if now:  # from the copies library_sync keeps, never a fresh export
                rows = [{"id": k, "ask": "search:selfie" if v.get("me") else "recent:all", "date": v["date"], "path": str(CACHE / v["file"]),
                         **{x: v.get(x) for x in ("faces", "aesthetics", "utility")}}
                        for k, v in _cache().items() if v.get("file") and start.isoformat() <= v["date"] <= end.isoformat()
                        and _sharp_enough(v)]
                info = {"recent:all": {"in_window": len(rows)}}
            else:
                asks = [("search", f"selfie {w}", 12, nb, ob) for w in words] + [("search", w, 15, nb, ob) for w in words]
                try:
                    rows, info = library_search(asks + [("search", "selfie", 30, nb, ob)])
                    searched = True
                except RuntimeError:  # Photos busy with the background round: a photo of the author now instead
                    groups["now"] += ids
                    continue
            good = [r for r in rows if r.get("faces") and not r.get("utility") and r["id"] not in used and r["id"] not in no_go
                    and (r.get("aesthetics") or 0) > -0.3 and max(f["h"] for f in r["faces"]) >= 0.06]  # a person, not a crowd far away
            fresh_from = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()
            if now:
                sorting, on_mac = _sorting(info), info.get("recent:all", {})
                for r in sorted((r for r in good if r["ask"].startswith("search:selfie") and len(r["faces"]) == 1
                                 and r["faces"][0]["h"] >= 0.12), key=lambda r: r["date"], reverse=True):
                    if len(refs) < 3 and r["date"] not in {x["date"] for x in refs}:  # one per day: the look, not one moment
                        refs.append(r)
                if len([r for r in good if r["date"] >= fresh_from]) >= 4:  # enough from the last 3 months: stay there
                    good = [r for r in good if r["date"] >= fresh_from]
            rank = lambda r: (r["date"] < fresh_from, len(r["faces"]) != 1, not r["ask"].startswith("search:selfie"), -(r.get("aesthetics") or 0))
            for i in ids:
                p = posts[i - 1]
                pool = [r for r in good if r["id"] not in taken and r["date"] not in batch_days]
                if not pool and now and any(r["id"] not in taken for r in good):
                    p["format"] = "graphic"
                    for v in _graphics(posts, i):
                        v.pop("in_doc", None)
                    notes[i] = ("A graphic instead of a photo: the only photos of you that fit were from a day already used "
                                "in this batch. A few new photos of you give the next batch more to pick from.")
                    picks.append({"post": i, "pick": 0, "why": "every fitting photo is from a day already in this batch"})
                    continue
                # the best from other days and the best from days already used, side by side: the picker trades
                # variety for quality only when quality is equal
                other_days = sorted((r for r in pool if r["date"] not in days_used), key=rank)[:5]
                pool = other_days + [r for r in sorted(pool, key=rank) if r not in other_days][:9 - len(other_days)]
                mine = [r for r in pool if any(r["ask"].endswith(w) for w in plan[i]["searches"])]
                cands = sorted(mine, key=rank)[:9]
                cands += [r for r in sorted(pool, key=rank) if r not in cands][:9 - len(cands)]
                if not cands:
                    if not now:  # nothing from back then: a photo of the author now
                        groups["now"].append(i)
                    elif sorting:
                        notes[i] = SORTING
                    elif not on_mac.get("in_window"):  # no copies with a face yet
                        notes[i] = ("Waiting for a recent photo of you: your photos are still being read from iCloud, "
                                    "a round every hour, and one is placed as soon as it fits.")
                    else:
                        p["format"] = "graphic"  # photos are the engine's job: never ask the author for one
                        for v in _graphics(posts, i):
                            v.pop("in_doc", None)  # its graphic shows instead
                        notes[i] = "A graphic instead of a photo: no recent photo of you fits that isn't used already."
                    picks.append({"post": i, "pick": 0, "why": f"no candidates for {plan[i]['searches']}", "sorting": now and sorting})
                    continue
                sheet = WORK / f"sheet_post{i}.jpg"
                _sheet(cands, sheet, refs if now else [], portrait=not now or not refs)
                dates = ", ".join(f"{k}: {_day(c['date'])}" for k, c in enumerate(cands, 1))
                lead = (f"Tiles N1 to N{len(refs)} are close-ups from the author's newest photos ({', '.join(_day(r['date']) for r in refs)}): "
                        "that is how they look now." if now and refs else "Tile R is a known photo of the author.")
                story = "" if now else f" The post is a story about {era[i]['years']} (\"{era[i]['quote']}\"), so the photo is from then."
                same = (f" Photos already used under the author's other posts were taken {', '.join(sorted(days_used))}: when one of "
                        "similar quality shows a clearly different place, pose or outfit, prefer it, so the feed doesn't look "
                        "the same post after post." if days_used else "")
                choice = _claude_on_image(
                    sheet, f"Read sheet.jpg. {lead} Tiles 1 to {len(cands)} are candidates, each cropped 4:5 as it would be "
                           f"posted, with the date it was taken ({dates}).{story}{same}\n"
                           f"The post (Danish), visual note: {p.get('visual', '')}\n{p['text'][:900]}\n\n"
                           "Pick the photo that fits this post best.", PICK1_SCHEMA,
                    PICK_SYSTEM.format(who=LOOK_NOW if now and refs else LOOK_THEN if not now else
                                       "The author must clearly be the person in it (tile R) and the subject. Prefer the newest."))
                choice["post"] = i
                if now and refs and 0 < choice["pick"] <= len(cands) and not choice["looks_like_now"]:
                    choice["why"] = f"rejected, the author looks different from now: {choice['why']}"
                    choice["pick"] = 0
                if 0 < choice["pick"] <= len(cands):
                    c = cands[choice["pick"] - 1]
                    taken.add(c["id"])
                    days_used.add(c["date"])
                    batch_days.add(c["date"])
                    choice.update({"photo_id": c["id"], "taken": c["date"], "era": "" if now else era[i]["quote"]})
                    if not dry:
                        im = Image.open(c["path"]).convert("RGB")
                        im = im.crop(crop_45(im.width, im.height, _main_face(c)))
                        if im.width > 1440:
                            im = im.resize((1440, 1800), Image.LANCZOS)
                        out = WORK / f"post{i}_photo.jpg"
                        im.save(out, quality=90)
                        choice["file"] = str(out)
                elif now:
                    p["format"] = "graphic"
                    for v in _graphics(posts, i):
                        v.pop("in_doc", None)  # its graphic shows instead
                    notes[i] = "A graphic instead of a photo: no recent photo of you fits that isn't used already."
                elif not now:
                    groups["now"].append(i)
                picks.append(choice)
        if not dry:
            # the run takes minutes: take the batch file as it is NOW and carry over only this run's photo changes, to
            # posts that are still photo posts (a write-back of the copy read at the start would undo a format change
            # or a feedback rewrite made meanwhile)
            fresh = json.loads(batch_file.read_text())
            still = {i for i, _ in want if fresh[i - 1].get("format") == "photo"}
            for i in still:
                for k in ("format", "visual_files", "check"):
                    if k in posts[i - 1]:
                        fresh[i - 1][k] = posts[i - 1][k]
            posts[:] = fresh
            notes = {i: n for i, n in notes.items() if i in still}
            picks = [x for x in picks if x.get("post") in still]
            for i, note in notes.items():
                _set_check(batch_file, posts, i, _with_notes(posts[i - 1].get("check", ""), [note]))
            if any(x.get("file") for x in picks):
                _place(batch_file, posts, [x for x in picks if x.get("file")], lib)
            batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    finally:  # a search's copies and the contact sheets never stay on disk (the background round's are left alone)
        if searched:
            shutil.rmtree(WORK / "export", ignore_errors=True)
        for f in WORK.glob("sheet_post*.jpg"):
            f.unlink()
    return picks


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _set_check(batch_file: pathlib.Path, posts: list[dict], i: int, new: str) -> None:
    """A post's Check note, in the batch file and in the grey block under the post's heading in the doc (rewritten
    whole by content_engine_draft.refresh_meta, the one place that knows its layout)."""
    import content_engine_draft as ced
    old, new = posts[i - 1].get("check", ""), " ".join(new.split())
    posts[i - 1]["check"] = new
    if old == new:
        return
    batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    ced.refresh_meta(batch_file, [i])


def _graphics(posts: list[dict], i: int) -> list[dict]:
    """The post's own graphic (post<N>_<k>.png): on a photo post it is the stand-in until the photo is there."""
    return [v for v in posts[i - 1].get("visual_files", []) if re.fullmatch(rf"post{i}_\d+\.png", v["file"])]


def _place(batch_file: pathlib.Path, posts: list[dict], picks: list[dict], lib: dict) -> None:
    """Upload each chosen photo and put it under its post. On a photo post the photo is the image, so the stand-in
    graphic leaves the doc; it stays in Drive and in the grey line."""
    import content_engine_visuals as vis
    from googleapiclient.http import MediaFileUpload
    drive = vis._google("drive", "v3")
    spec_file = batch_file.with_name(batch_file.stem + "_visuals.json")
    spec = json.loads(spec_file.read_text()) if spec_file.exists() else {"batch": batch_file.stem, "visuals": []}
    folder = vis.graphics_folder(drive, spec["batch"])
    for x in picks:
        name = f"post{x['post']}_photo.jpg"
        up = drive.files().create(body={"name": name, "parents": [folder["id"]]},
                                  media_body=MediaFileUpload(x["file"], mimetype="image/jpeg"), fields="id, webViewLink").execute()
        p = posts[x["post"] - 1]
        p.setdefault("visual_files", []).append({"file": name, "link": up["webViewLink"]})
        notes = ["The photo has other people in it: OK to post it?"] if x.get("others_in_photo") else []
        if x.get("era"):
            notes.append(f"The photo is from {x['taken'][:4]}, because the post is about then (\"{x['era']}\"): swap it if you "
                         "have a better one from that time.")
        _set_check(batch_file, posts, x["post"], _with_notes(p.get("check", ""), notes))
        stand_in = [v for v in _graphics(posts, x["post"]) if v.get("in_doc", True)]
        if p.get("format") == "photo" and stand_in:
            vis.remove_from_doc(spec["batch"], [vis.drive_id(v["link"]) for v in stand_in], [v["file"][:-4] for v in stand_in])
            for v in stand_in:
                v["in_doc"] = False
        lib.setdefault("library", {})[x["photo_id"]] = {"taken": x.get("taken"), "used_in": [f"{batch_file.stem} #{x['post']}"],
                                                        "why": x.get("why", "")[:200], "era": x.get("era", "")}
        pathlib.Path(x["file"]).unlink(missing_ok=True)
    batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
    LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=1))
    spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1))
    vis.into_doc(spec_file)
    ced.refresh_meta(batch_file, [x["post"] for x in picks])  # "Photo: your photo" linked in the grey line


def _stale(batch_file: pathlib.Path, posts: list[dict], lib: dict, today: dt.date) -> list[int]:
    """Photo posts whose photo came from the library and is older than MAX_DAYS without the post being about then."""
    cut, out = (today - dt.timedelta(days=MAX_DAYS)).isoformat(), []
    for i, p in enumerate(posts, 1):
        tag = f"{batch_file.stem} #{i}"
        entry = next((v for v in lib.get("library", {}).values() if tag in v.get("used_in", [])), None)
        if entry and not entry.get("era") and (entry.get("taken") or "9") < cut and \
                any(v["file"] == f"post{i}_photo.jpg" for v in p.get("visual_files", [])):
            out.append(i)
    return out


def _unplace(batch_file: pathlib.Path, posts: list[dict], i: int, lib: dict) -> None:
    """Take a library photo back out of a post: out of the doc, the post's files and the list of used photos, and the
    file to Drive's bin. The post's graphic comes back into the doc as the stand-in."""
    import content_engine_visuals as vis
    p = posts[i - 1]
    entry = next(v for v in p["visual_files"] if v["file"] == f"post{i}_photo.jpg")
    vis.remove_from_doc(batch_file.stem, [vis.drive_id(entry["link"])], [f"post{i}_photo"])
    vis._google("drive", "v3").files().update(fileId=vis.drive_id(entry["link"]), body={"trashed": True}).execute()
    p["visual_files"].remove(entry)
    for v in _graphics(posts, i):
        v.pop("in_doc", None)
    tag = f"{batch_file.stem} #{i}"
    for k in [k for k, v in lib.get("library", {}).items() if tag in v.get("used_in", [])]:
        del lib["library"][k]
    _set_check(batch_file, posts, i, _with_notes(p.get("check", ""), []))


@_locked
def library_refresh(batch_file: pathlib.Path, today: dt.date | None = None) -> list[dict]:
    """Swap out library photos that are too old for their post, then pick for every photo post without a photo."""
    import content_engine_visuals as vis
    today = today or dt.date.today()
    posts, lib = json.loads(batch_file.read_text()), _lib()
    stale = _stale(batch_file, posts, lib, today)
    for i in stale:
        _unplace(batch_file, posts, i, lib)
    if stale:
        batch_file.write_text(json.dumps(posts, ensure_ascii=False, indent=1))
        LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=1))
        spec_file = batch_file.with_name(batch_file.stem + "_visuals.json")
        if spec_file.exists():
            vis.into_doc(spec_file)  # the stand-in graphics back in place
        print(f"took out photos older than {MAX_DAYS} days under post(s) {stale}")
    return library_pick(batch_file, today=today)


@_locked
def library_auto() -> None:
    """From the hourly job: keep the library copies coming (library_sync), then pick for the newest batch's photo posts
    without a photo whenever new photos with a face came in, and once a day to swap out ones too old for their post.
    Only once the photo apps are installed (--install-app, photo_library_reader.py --install-app)."""
    if not (READER.exists() or APP.exists()):
        return
    print(library_sync())
    batches = sorted(b for b in C.BATCHES.glob("*.json")
                     if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", b.name))
    if not batches:
        return
    posts, lib = json.loads(batches[-1].read_text()), _lib()
    if not _stale(batches[-1], posts, lib, dt.date.today()) and not any(
            p.get("format") == "photo" and p.get("status") != "dropped" and
            not any(v["file"].startswith(f"post{i}_photo") for v in p.get("visual_files", [])) for i, p in enumerate(posts, 1)):
        return
    stamp, faces = WORK / "last_library_run", sum(1 for v in _cache().values() if v.get("file"))
    last = stamp.read_text().split() if stamp.exists() else []
    fresh = stamp.exists() and dt.datetime.now().timestamp() - stamp.stat().st_mtime < 20 * 3600
    if fresh and last[1:2] == [str(faces)]:  # nothing new to choose from since the last pick
        return
    stamp.write_text(f"{dt.datetime.now().isoformat(timespec='seconds')} {faces}")
    placed = [x for x in library_refresh(batches[-1]) if x.get("file") or x.get("photo_id")]
    if placed:
        print(f"photos from your library placed under post(s) {[x['post'] for x in placed]} in {batches[-1].stem}")


def selftest() -> None:
    """The mechanical parts of the photo rules, without Photos, Drive or Claude."""
    today, fails = dt.date(2026, 10, 28), []
    post = "Da jeg startede for mig selv i 2016, havde jeg ingen kunder.\n\nI dag har jeg 9."
    if not _era(post, {"from_year": 2016, "to_year": 2016, "quote": "Da jeg startede for mig selv i 2016"}, today):
        fails.append("a story about 2016 got no photo from 2016")
    for plan, why in [({"from_year": 2016, "to_year": 2016, "quote": "Da jeg begyndte i 2016"}, "a quote not in the post"),
                      ({"from_year": 2026, "to_year": 2026, "quote": "I dag har jeg 9"}, "this year is now, not history"),
                      ({"from_year": 2012, "to_year": 2016, "quote": "Da jeg startede"}, "a five-year span"),
                      ({"from_year": 0, "to_year": 0, "quote": ""}, "a post about now")]:
        if _era(post, plan, today):
            fails.append(f"history photo allowed for {why}")
    check = ("Confirm X. The photo is from your library, taken 2024-11-16: swap it if you have a newer one. Waiting for a recent "
             "photo of you: the newest one on this Mac is from 7 Dec 2024, so it comes once iCloud has brought the newer ones in.")
    if _with_notes(check, []) != "Confirm X.":
        fails.append(f"own notes not taken out: {_with_notes(check, [])!r}")
    if _with_notes("Confirm X.", ["The photo has other people in it: OK to post it?"]) != "Confirm X. The photo has other people in it: OK to post it?":
        fails.append("a new note was not added")
    if _with_notes(f"Confirm X. {SORTING}", []) != "Confirm X.":
        fails.append("the still-sorting note is not taken out again")
    for info, want, why in [({"recent:all": {"in_window": 1490}, "search:person": {"in_window": 0}}, True, "1,490 new photos, search finds none"),
                            ({"recent:all": {"in_window": 1490}, "search:person": {"in_window": 12}}, False, "search finds the new photos"),
                            ({"recent:all": {"in_window": 3}, "search:person": {"in_window": 0}}, False, "too few photos to tell")]:
        if _sorting(info) != want:
            fails.append(f"still-sorting check wrong for {why}")
    for w, h, face in [(4032, 3024, {"x": .45, "y": .2, "w": .1, "h": .13}), (3024, 4032, {"x": .02, "y": .7, "w": .2, "h": .15})]:
        for name, box in (("crop_45", crop_45(w, h, face)), ("face_box", face_box(w, h, face))):
            x0, y0, x1, y1 = box
            if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h) or abs((x1 - x0) / (y1 - y0) - 0.8) > 0.01:
                fails.append(f"{name} not a 4:5 frame inside the photo: {box} in {w}x{h}")
    if _bounds(dt.date.today() - dt.timedelta(days=MAX_DAYS), dt.date.today()) != (MAX_DAYS + 1, 0):
        fails.append("the window's day bounds are off")
    t = dt.date(2026, 10, 31)
    for dates, want, why in [([], [("recent", "all", ROUND, MAX_DAYS + 1, 0)], "an empty cache starts at the newest"),
                             (["2026-10-31", "2026-10-20"], [("recent", "all", ROUND, MAX_DAYS + 1, 12)], "today's are in: go further back"),
                             (["2026-10-28", "2026-10-20"], [("recent", "all", ROUND, 3, 0), ("recent", "all", ROUND, MAX_DAYS + 1, 12)],
                              "newer ones first, then further back"),
                             (["2026-10-31", "2026-05-04"], [], "the whole six months read")]:
        if _next_asks(dates, t) != want:
            fails.append(f"export round wrong when {why}: {_next_asks(dates, t)}")
    print("\n".join(fails) or "selftest ok: 19 cases")
    sys.exit(1 if fails else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--pick", metavar="BATCH_JSON")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--describe-local", metavar="FILE")
    ap.add_argument("--library", metavar="BATCH_JSON")
    ap.add_argument("--install-app", action="store_true")
    ap.add_argument("--refresh", metavar="BATCH_JSON", help="swap out photos too old for their post, then pick")
    ap.add_argument("--today", help="pretend another date (YYYY-MM-DD), to test --library on older photos")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else None
    if a.selftest:
        selftest()
    elif a.refresh:
        print(json.dumps(library_refresh(pathlib.Path(a.refresh), today), ensure_ascii=False, indent=1))
    elif a.install_app:
        install_app()
    elif a.library:
        print(json.dumps(library_pick(pathlib.Path(a.library), a.dry_run, today), ensure_ascii=False, indent=1))
    elif a.describe_local:
        print(json.dumps(describe(pathlib.Path(a.describe_local)), ensure_ascii=False, indent=1))
    elif a.pick:
        print(json.dumps(pick(pathlib.Path(a.pick), a.dry_run), ensure_ascii=False, indent=1))
    elif a.auto:
        # the photos in the Drive folder first: they are quick, and the library search can run for the whole hour on
        # slow Wi-Fi
        n = sync_new()
        newest = sorted(b for b in C.BATCHES.glob("*.json")
                        if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", b.name))  # not _visuals.json
        picks = pick(newest[-1]) if n and newest else []
        if n:
            print(f"{dt.datetime.now():%H:%M} described {n} new photo(s), placed {len(picks)}")
        try:
            library_auto()
        except Exception as e:
            print(f"library pick skipped: {e}")
    else:
        ap.print_help()



# Every doc write under one lock (content_engine_lock.py)
from content_engine_lock import locked as _locked_doc  # noqa: E402
_place = _locked_doc(_place)
_unplace = _locked_doc(_unplace)


if __name__ == "__main__":
    main()
