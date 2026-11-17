#!/usr/bin/env python3
"""Keep the author's LinkedIn messages fresh: pull the inbox, index 6 months, optionally push the index to a server.

LinkedIn's DMA snapshot (Member Data Portability API, token in tokens/<linkedin.dma_token_file>) has every message.
The raw pull stays on this machine in <cache_dir>/linkedin_dma/ (never git). Only a 6-month index with messages cut
to 600 characters can go to a server ([linkedin] dma_server_ssh and dma_server_index), where your own lookup server
for the interviewer can read it (not part of this repo).

Usage:
  python3 scripts/linkedin_dma_sync.py --auto      # pull + index (+ push), at most once every 20 hours
  python3 scripts/linkedin_dma_sync.py --pull --push
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

CACHE = C.CACHE / "linkedin_dma"
LOCAL_INDEX = C.CACHE / "engine" / "linkedin_messages.json"
SERVER = C.get("linkedin", "dma_server_ssh")
SERVER_INDEX = C.get("linkedin", "dma_server_index")
ME = C.get("linkedin", "profile_slug")  # the author's profile URL slug: tells their own messages apart
DAYS = 183


def _private(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(path, 0o600)


def pull() -> int:
    tok = json.loads(C.token("dma_token_file").read_text())["access_token"]
    h = {"Authorization": f"Bearer {tok}", "LinkedIn-Version": "202312"}
    rows, start = [], 0
    while True:
        r = requests.get("https://api.linkedin.com/rest/memberSnapshotData", headers=h, timeout=60,
                         params={"q": "criteria", "domain": "INBOX", "start": start})
        if r.status_code == 404 and start > 0:  # the last page can still say "next"; the page after it is a 404
            break
        r.raise_for_status()
        d = r.json()
        for e in d.get("elements", []):
            rows += e.get("snapshotData", [])
        if not any(l.get("rel") == "next" for l in d.get("paging", {}).get("links", [])):
            break
        start += 1
        time.sleep(0.3)
    seen, unique = set(), []  # LinkedIn's pages can overlap: the same message can come back twice
    for r in rows:
        k = (r.get("CONVERSATION ID"), r.get("DATE"), r.get("FROM"), r.get("CONTENT"))
        if k not in seen:
            seen.add(k)
            unique.append(r)
    rows = unique
    old = CACHE / "inbox.json"
    had = len(json.loads(old.read_text())) if old.exists() else 0
    if had and len(rows) < had * 0.95:  # the inbox only grows: far fewer means pages went missing (a 404 mid-way)
        raise SystemExit(f"only {len(rows)} of the {had} messages already on file came back, kept the old copy")
    _private(CACHE / "inbox.json", rows)
    return len(rows)


def index() -> list[dict]:
    rows = json.loads((CACHE / "inbox.json").read_text())
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%d")
    out = []
    for r in rows:
        day = (r.get("DATE") or "")[:10]
        if day < since or r.get("IS MESSAGE DRAFT") == "Yes" or r.get("FOLDER") == "SPAM" or not (r.get("CONTENT") or "").strip():
            continue
        me = bool(ME) and ME in (r.get("SENDER PROFILE URL") or "")
        out.append({"date": day, "from": C.author() if me else (r.get("FROM") or "")[:60],
                    "with": (r.get("TO") if me else r.get("FROM") or "")[:60], "me": me,
                    "text": " ".join((r.get("CONTENT") or "").split())[:600]})
    out.sort(key=lambda x: x["date"], reverse=True)
    _private(LOCAL_INDEX, out)
    return out


def push() -> None:
    if not (SERVER and SERVER_INDEX):
        sys.exit("Set [linkedin] dma_server_ssh and dma_server_index to push the index to a server.")
    folder = pathlib.Path(SERVER_INDEX).parent
    subprocess.run(["ssh", "-o", "ConnectTimeout=10", SERVER, f"mkdir -p {folder} && chmod 700 {folder}"], check=True)
    subprocess.run(["scp", "-q", "-o", "ConnectTimeout=10", str(LOCAL_INDEX), f"{SERVER}:{SERVER_INDEX}"], check=True)
    subprocess.run(["ssh", "-o", "ConnectTimeout=10", SERVER, f"chmod 600 {SERVER_INDEX}"], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--auto", action="store_true", help="pull, index (and push, if a server is set) if the last pull is 20+ hours old")
    a = ap.parse_args()
    if not (a.pull or a.push or a.auto):
        return ap.print_help()
    stamp = CACHE / "last_sync.txt"
    if a.auto:  # the hourly job: once a day
        if not C.token("dma_token_file").exists():
            return
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 20 * 3600:
            return
        a.pull, a.push = True, bool(SERVER and SERVER_INDEX)
    if a.pull:
        print(f"pulled {pull()} LinkedIn messages")
    idx = index()
    print(f"indexed {len(idx)} messages from the last {DAYS} days")
    if a.auto:
        stamp.write_text(dt.datetime.now().isoformat(timespec="seconds"))
    if a.push:
        push()
        print("pushed to the server")


if __name__ == "__main__":
    main()
