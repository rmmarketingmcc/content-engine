#!/usr/bin/env python3
"""Content Engine Photos Reader: lists the author's recent photos straight from the Photos library on this Mac, for
the content engine's photo picker (content_engine_photos.py).

Reading the library directly is seconds, where asking Photos to export can take minutes per photo (iCloud downloads
while Photos still sorts new photos). Read-only: it opens a copy of the database (osxphotos) and copies image files
out; it never writes to the library. Photos only, never videos or screenshots.

macOS keeps the library behind Full Disk Access, granted to the small app ~/Applications/Content Engine Photos
Reader.app (and so to this script when that app runs it), never to Python as a whole. Rebuilding the app loses the
grant, so change this script instead.

  --scan          write <cache_dir>/photos/library.json: every photo of the last DAYS days with its date, people Photos
                  recognised, number of faces, Apple's quality score and the best local image file, and copy the newest
                  KEEP photos the author is in to the photo cache (only this app may open the library's files)
  --install-app   (re)build ~/Applications/Content Engine Photos Reader.app
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402
import soft_jpeg  # noqa: E402

WORK = C.CACHE / "photos"
OUT = WORK / "library.json"
APP = pathlib.Path.home() / "Applications" / "Content Engine Photos Reader.app"
LIBRARY = pathlib.Path(C.get("photos", "library")).expanduser()
CACHE = WORK / "cache"
ME = {n.lower() for n in C.get("photos", "me_names")}  # what Photos calls the author under People
KEEP = 300  # copies of the newest photos the author is in; only this app may open the library's own files
DAYS = 180


def _best_file(p) -> tuple[str, str]:
    """The biggest image of this photo that is on the Mac now: the original if iCloud has brought it, else the edited
    version, else the largest preview Photos keeps. ('', '') when none is local."""
    for path, kind in ((p.path_edited, "edited"), (p.path, "original")):
        if path and pathlib.Path(path).exists():
            return path, kind
    previews = [d for d in (p.path_derivatives or []) if pathlib.Path(d).exists()]
    if previews:
        return max(previews, key=lambda d: pathlib.Path(d).stat().st_size), "preview"
    return "", ""


def access() -> str:
    """Whether this run can see inside the library at all (Full Disk Access reached it), for the log."""
    import os
    try:
        return f"library readable: {len(os.listdir(LIBRARY / 'database'))} files in database/"
    except OSError as e:
        return f"library not readable: {type(e).__name__}: {e}"


def scan() -> dict:
    import osxphotos
    print(access(), flush=True)
    db = osxphotos.PhotosDB(dbfile=str(LIBRARY))
    since = dt.datetime.now().astimezone() - dt.timedelta(days=DAYS)
    rows = []
    for p in db.photos(images=True, movies=False, from_date=since):
        if p.ismovie or p.screenshot or p.hidden or p.intrash:
            continue
        path, kind = _best_file(p)
        score = getattr(p, "score", None)
        rows.append({"id": p.uuid, "date": p.date.date().isoformat(), "file": path, "kind": kind,
                     "people": [x for x in p.persons if x and x != "_UNKNOWN_"], "faces": len(p.face_info or []),
                     "selfie": bool(p.selfie), "w": p.width, "h": p.height,
                     "quality": round(score.overall, 3) if score and score.overall is not None else None})
    CACHE.mkdir(parents=True, exist_ok=True)
    mine = sorted((r for r in rows if r["file"] and ME & {n.lower() for n in r["people"]}), key=lambda r: r["date"], reverse=True)
    for r in mine[:KEEP]:  # a 2400 px JPEG next to the engine, where the rest of it can read it
        copy = CACHE / (re.sub(r"[^A-Za-z0-9]+", "_", r["id"]) + ".jpg")
        if not copy.exists():
            soft_jpeg.to_jpeg(r["file"], copy, 2400)
        if copy.exists():
            r["copy"] = copy.name
    data = {"scanned": dt.datetime.now().isoformat(timespec="seconds"), "days": DAYS, "photos": rows,
            "named": sorted({n for r in rows for n in r["people"]})}
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False))
    return data


def install_app() -> None:
    """An app of its own, so Full Disk Access can be given to just this, not to every Python script."""
    WORK.mkdir(parents=True, exist_ok=True)
    script = (f'do shell script "{ROOT}/.venv/bin/python3 {ROOT}/scripts/photo_library_reader.py --scan '
              f'> {WORK}/reader.log 2>&1"')
    APP.parent.mkdir(exist_ok=True)
    subprocess.run(["osacompile", "-o", str(APP), "-e", script], check=True)
    print(f"built {APP}; now give it Full Disk Access in System Settings > Privacy & Security")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--install-app", action="store_true")
    a = ap.parse_args()
    if a.install_app:
        return install_app()
    if a.scan:
        try:
            d = scan()
            print(f"{len(d['photos'])} photos from the last {DAYS} days; people named in Photos: {len(d['named'])}")
        except Exception:  # to the log only: a non-zero exit makes the app show an error window
            WORK.mkdir(parents=True, exist_ok=True)
            (WORK / "reader_error.txt").write_text(traceback.format_exc()[-2000:])
        return
    ap.print_help()


if __name__ == "__main__":
    main()
