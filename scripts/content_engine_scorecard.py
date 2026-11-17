#!/usr/bin/env python3
"""Content engine scorecard: how much the author had to fix, batch by batch.

One row per batch, so the trend across batches shows: is the quality going up, or is the author just asking for more?

Per batch, from its own file (data/batches/<batch>.json):
  posts            posts in the batch
  untouched        approved as drafted
  direction        changed only because the author brought something new (a fact, a story, a preference): not a fault
  error            changed at least once because the engine got something wrong
  errors           how many error changes in the batch
  dropped / waiting
  pct_error        average share of a post's text fixed because of errors (0% = nothing to fix), over decided posts
  out / reach / dms  posts published, the typical one's impressions so far and the DMs in the 72 h after them all
                   (linkedin_post_results.py, from LinkedIn's API; only posts at least 4 days old count for reach)
Writes data/state/scorecard.json. Read-only otherwise.

  python3 scripts/content_engine_scorecard.py          # rebuild and print
"""
import importlib.util
import json
import pathlib
import re
import statistics
import sys
from difflib import SequenceMatcher

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

BATCHES = C.BATCHES
OUT = C.STATE / "scorecard.json"


def results() -> tuple[dict, dict]:
    """(post id -> its numbers, post id -> DMs after it) from linkedin_post_results.py's store; empty if missing."""
    try:
        spec = importlib.util.spec_from_file_location("li_results", ROOT / "scripts" / "linkedin_post_results.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        posts = mod.usable(mod.load())
        return {p["id"]: p for p in posts}, mod.dms_by_post(posts)
    except Exception:  # noqa: BLE001 - the edit score stands without the numbers
        return {}, {}


def score(batch_file: pathlib.Path, nums: tuple[dict, dict] = ({}, {})) -> dict:
    """The score tells the author's new direction apart from the engine's mistakes. Every change carries a cause
    (content_engine_feedback): 'error' (the engine could have known), 'direction' (the author brought something new),
    'edit' (their own edit in the doc)."""
    posts = [p for p in json.loads(batch_file.read_text()) if isinstance(p, dict)]
    row = {"batch": batch_file.stem.split("_", 1)[1].replace("_", " "), "written": batch_file.stem[:10], "posts": len(posts),
           "untouched": 0, "direction": 0, "error": 0, "dropped": 0, "waiting": 0, "errors": 0, "unsorted": 0}
    shares = []
    for p in posts:
        # 'rule_applied': a rule put into an already approved post afterwards, not a change to the first read, so it
        # never counts
        hist = [h for h in p.get("history") or [] if h.get("decision") in ("change", "edit", "drop")
                and "check" not in str(h.get("via", "")).lower() and h.get("cause") != "rule_applied"]
        errs = [h for h in hist if h.get("cause") == "error"]
        row["errors"] += len(errs)
        row["unsorted"] += sum(1 for h in hist if h.get("cause") in (None, "none"))
        # the share of the text the errors changed: each error change against the text right after it
        texts = [h.get("old_text") for h in hist if h.get("old_text") is not None] + [p.get("text") or ""]
        changed = [h for h in hist if h.get("old_text") is not None]
        share = sum(1 - SequenceMatcher(None, h["old_text"], texts[k + 1]).ratio()
                    for k, h in enumerate(changed) if h.get("cause") == "error")
        if p.get("status") == "dropped":
            row["dropped"] += 1
        elif errs:
            row["error"] += 1
        elif hist:
            row["direction"] += 1
        elif p.get("status") == "approved":
            row["untouched"] += 1
        else:
            row["waiting"] += 1
        if p.get("status") in ("approved", "changed") or hist:
            shares.append(min(1.0, share))
    row["pct_error"] = round(sum(shares) / len(shares) * 100) if shares else None
    ids = [m.group(1) for p in posts if isinstance(p.get("posted"), dict) and not p["posted"].get("deleted")
           for m in [re.search(r"(\d{19})", p["posted"].get("urn", ""))] if m]
    read = [nums[0][i]["impressions"] for i in ids if i in nums[0]]
    row.update(out=len(ids), reach=round(statistics.median(read)) if read else None,
               dms=sum(nums[1].get(i, 0) for i in ids))
    return row


def build() -> list[dict]:
    nums = results()
    rows = [score(f, nums) for f in sorted(BATCHES.glob("*.json"))
            if re.fullmatch(r"\d{4}-\d\d-\d\d_.*Engine_Batch_\d+\.json", f.name)]
    rows.sort(key=lambda r: int(re.search(r"\d+$", r["batch"]).group()))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return rows


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(__doc__)
        sys.exit(0)
    for r in build():
        pct = "-" if r["pct_error"] is None else f"{r['pct_error']}%"
        print(f"{r['batch']:16} {r['posts']} posts: {r['untouched']} as drafted, {r['direction']} new direction, "
              f"{r['error']} with an engine error ({r['errors']} errors), {r['dropped']} dropped, {r['waiting']} waiting; "
              f"{pct} of the text fixed for errors" + (f"; {r['unsorted']} change(s) without a cause" if r["unsorted"] else "")
              + (f"; {r['out']} out, typical {r['reach'] or '-'} impressions, {r['dms']} DMs after" if r.get("out") else ""))
