#!/usr/bin/env python3
"""Voice bank source: every word the author has dictated in Wispr Flow, exported from Wispr's local database into
data/kb/voice_bank/.

Wispr is usually the biggest pile of the author's own words there is, and it is only them talking, no other speakers
to filter out.

What goes in, per dictation (one JSON line each, oldest first):
  spoken   = asrText, what was actually said, fillers ("øh") and all
  written  = formattedText, Wispr's cleaned version (punctuation, line breaks)
  plus timestamp (UTC + the plan's time zone), app, domain of the page, language, words.
editedText is left out on purpose: it is the whole text box after the dictation, so it can hold text that was never
said (pasted mails, other people's messages). Dictations into macOS password prompts are always skipped.

The folder stays out of git: raw dictations hold client and personal details. The engine takes language from it,
never client facts.

Each run rewrites the export from scratch, so it is safe to rerun any time. The Wispr database is opened read-only;
Wispr can keep running.

Usage:
  python3 scripts/voice_bank_wispr.py            # export everything
  python3 scripts/voice_bank_wispr.py --dry-run  # counts only, write nothing
  python3 scripts/voice_bank_wispr.py --lang da  # one language only
"""
import argparse
import collections
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import urllib.parse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ce_config as C  # noqa: E402

WISPR_DB = pathlib.Path(C.get("sources", "wispr_db")).expanduser()
OUT_DIR = C.KB / "voice_bank"
DK = ZoneInfo(C.get("plan", "timezone"))

# Dictations into these apps are never exported (password dialogs).
SKIP_APPS = {"com.apple.LocalAuthentication.UIAgent"}

APP_NAMES = {
    "com.microsoft.VSCode": "vscode",
    "com.google.Chrome": "chrome",
    "com.tinyspeck.slackmacgap": "slack",
    "net.whatsapp.WhatsApp": "whatsapp",
    "com.electron.wispr-flow": "wispr",
    "com.anthropic.claudefordesktop": "claude_desktop",
    "com.openai.atlas": "atlas",
    "com.apple.iWork.Pages": "pages",
}


def _connect():
    if not WISPR_DB.exists():
        sys.exit(f"Wispr database not found: {WISPR_DB}")
    uri = "file:" + urllib.parse.quote(str(WISPR_DB)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _parse_ts(raw):
    # Wispr stores "2026-10-30 17:44:19.826 +00:00"
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return dt.datetime.strptime(raw.replace(" +00:00", " +0000"), fmt)
        except ValueError:
            continue
    return None


def _domain(url):
    if not url:
        return None
    host = urllib.parse.urlparse(url).hostname or ""
    return host.removeprefix("www.") or None


def _count_words(text):
    return len(text.split())


def fetch_dictations(con, lang=None):
    rows = con.execute(
        """
        SELECT transcriptEntityId, timestamp, app, url, asrText, formattedText,
               detectedLanguage, language, numWords, duration, status
        FROM History
        WHERE COALESCE(asrText, '') <> '' OR COALESCE(formattedText, '') <> ''
        ORDER BY timestamp
        """
    ).fetchall()
    out, skipped = [], collections.Counter()
    for r in rows:
        app = r["app"] or ""
        if app in SKIP_APPS:
            skipped["password_prompt"] += 1
            continue
        language = r["detectedLanguage"] or r["language"] or None
        if lang and language != lang:
            skipped["other_language"] += 1
            continue
        spoken = (r["asrText"] or "").strip()
        written = (r["formattedText"] or "").strip()
        ts = _parse_ts(r["timestamp"])
        out.append({
            "id": r["transcriptEntityId"],
            "ts_utc": ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds") if ts else r["timestamp"],
            "ts_dk": ts.astimezone(DK).isoformat(timespec="seconds") if ts else None,
            "app": APP_NAMES.get(app, app or None),
            "domain": _domain(r["url"]),
            "lang": language,
            "words": r["numWords"] or _count_words(spoken or written),
            "seconds": round(r["duration"], 1) if r["duration"] else None,
            "spoken": spoken or None,
            "written": written or None,
        })
    return out, skipped


def fetch_dictionary(con):
    rows = con.execute(
        """
        SELECT phrase, replacement, frequencyUsed, manualEntry, isSnippet, source
        FROM Dictionary WHERE isDeleted = 0 ORDER BY frequencyUsed DESC, phrase
        """
    ).fetchall()
    return [{
        "phrase": r["phrase"],
        "replacement": r["replacement"],
        "times_used": r["frequencyUsed"],
        "added_by_hand": bool(r["manualEntry"]),
        "snippet": bool(r["isSnippet"]),
        "source": r["source"],
    } for r in rows]


def summarize(dictations, skipped, dictionary):
    by_lang = collections.Counter()
    by_app = collections.Counter()
    by_month = collections.Counter()
    for d in dictations:
        by_lang[d["lang"] or "unknown"] += d["words"]
        by_app[d["app"] or "unknown"] += d["words"]
        by_month[(d["ts_dk"] or d["ts_utc"] or "")[:7]] += d["words"]
    return {
        "exported_at_dk": dt.datetime.now(DK).isoformat(timespec="seconds"),
        "source": str(WISPR_DB),
        "dictations": len(dictations),
        "words": sum(d["words"] for d in dictations),
        "first_dk": dictations[0]["ts_dk"] if dictations else None,
        "last_dk": dictations[-1]["ts_dk"] if dictations else None,
        "words_by_language": dict(by_lang.most_common()),
        "words_by_app": dict(by_app.most_common()),
        "words_by_month": dict(sorted(by_month.items())),
        "skipped": dict(skipped),
        "dictionary_entries": len(dictionary),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print counts, write nothing")
    ap.add_argument("--lang", help="only this language code, e.g. da")
    args = ap.parse_args()

    con = _connect()
    dictations, skipped = fetch_dictations(con, lang=args.lang)
    dictionary = fetch_dictionary(con)
    con.close()
    summary = summarize(dictations, skipped, dictionary)

    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = OUT_DIR / "wispr_dictations.jsonl.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            for d in dictations:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        tmp.replace(OUT_DIR / "wispr_dictations.jsonl")
        (OUT_DIR / "wispr_dictionary.json").write_text(
            json.dumps(dictionary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUT_DIR / "wispr_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.dry_run:
        print(f"\nWrote {OUT_DIR}/wispr_dictations.jsonl, wispr_dictionary.json, wispr_summary.json")


if __name__ == "__main__":
    main()
