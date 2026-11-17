#!/usr/bin/env python3
"""Voice bank: the words the author actually uses, and their own sentences, for the content engine.

Every word of a draft is checked against the words the author has really said; a word they never say is swapped for
one they do, and what is left is listed for them to fix in the doc. The goal: the author fixes as little as possible.

The author's own spoken words only, from two places:
  Wispr Flow, their dictations (voice_bank/wispr_dictations.jsonl, exported by voice_bank_wispr.py)
  Calls, their own lines in call transcripts from any note taker, in the author's language ([sources] transcripts_dir,
  their speaker name [sources] speaker; transcripts.py), and mails they wrote, saved there by transcripts.py sync-mail
Not their posts: approved or posted texts were written by someone else or the engine, and a word let through once
would then pass forever.
The word list (voice_bank/vocab.json) and everything else in voice_bank/ stays out of git: dictations hold client and
personal details. The engine borrows the author's language from it, never facts, names or numbers.

  refresh                re-export Wispr and rebuild the word list
  check <file or text>   the words in a text the author has never used
  samples <file or text> their own sentences closest to a topic
  --selftest
"""
import collections
import datetime as dt
import glob
import json
import math
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ce_config as C  # noqa: E402

KB = C.KB
BANK = KB / "voice_bank"
VOCAB = BANK / "vocab.json"
LANG = C.get("author", "language") or "da"  # the language of the posts and of the words checked
WORD = re.compile(r"[a-zæøåéü]+")
# an inflection of a word the author uses is theirs too (bilen, kvajer, sultne): Danish endings to try, longest first
ENDINGS = ("ernes", "enes", "erne", "ene", "ens", "ets", "ede", "ende", "est", "ere", "er", "en", "et", "ne", "es", "e", "s", "t", "r", "n")
COMMON = {"og", "at", "det", "der", "en", "et", "er", "jeg", "du", "de", "vi", "den", "til", "på", "med", "for", "som", "af", "i",
          "ikke", "har", "kan", "så", "men", "om", "hvis", "man", "skal", "vil", "være", "have", "eller", "når", "fra", "ud", "op",
          "lige", "også", "bare", "noget", "nogle", "mere", "meget", "hvad", "hvor", "alle", "sig", "din", "dit", "dine", "min",
          "mit", "mine", "jo", "nu", "her", "der", "dem", "os", "mig", "dig", "sin", "sit", "sine", "hans", "hun", "han"}
_cache: dict = {}


def tokens(text: str) -> list[str]:
    return WORD.findall(text.lower())


def refresh(quiet: bool = True) -> dict:
    """Wispr exported again (dictations come in every day), then the word list rebuilt from both places."""
    if pathlib.Path(C.get("sources", "wispr_db")).expanduser().exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "voice_bank_wispr.py")], check=True,
                       capture_output=quiet, cwd=ROOT)
    counts, per = collections.Counter(), collections.Counter()
    for line in (BANK / "wispr_dictations.jsonl").open() if (BANK / "wispr_dictations.jsonl").exists() else []:
        d = json.loads(line)
        if d.get("lang") == LANG:
            words = tokens(f"{d.get('spoken') or ''} {d.get('written') or ''}")
            counts.update(words)
            per["wispr"] += len(words)
    import transcripts  # calls from any note taker: only the author's own lines, only in the posts' language
    for tr in transcripts.load_all():
        if tr["language"] and not tr["language"].startswith(LANG):
            continue
        for said in transcripts.author_lines(tr):
            words = tokens(said)
            counts.update(words)
            per["calls"] += len(words)
    data = {"built": dt.datetime.now().isoformat(timespec="seconds"), "words_per_source": dict(per), "distinct": len(counts),
            "words": dict(counts)}
    BANK.mkdir(parents=True, exist_ok=True)
    VOCAB.write_text(json.dumps(data, ensure_ascii=False))
    _cache.clear()
    return data


def vocab() -> dict[str, int]:
    if "words" not in _cache:
        _cache["words"] = json.loads(VOCAB.read_text())["words"] if VOCAB.exists() else refresh()["words"]
    return _cache["words"]


def known(word: str, words: dict[str, int]) -> bool:
    """The author has said it, an inflection of it (bilen from bil, kvajer from kvaje), or a compound of two words
    they have said, both at least four letters (bestyrelses + lokale): Danish makes those up all the time."""
    def said(w: str) -> bool:
        if w in words:
            return True
        for end in ENDINGS:
            stem = w[:-len(end)]
            if w.endswith(end) and len(stem) >= 3 and (stem in words or any(stem + e in words for e in ("e", "er", "en", "et"))):
                return True
        return False
    if len(word) <= 2 or said(word):
        return True
    for i in range(4, len(word) - 3):
        first, rest = word[:i], word[i:]
        if (first in words or (first[-1] in "se" and first[:-1] in words)) and said(rest):
            return True
    return False


def unknown(text: str, allowed: set[str] = frozenset(), words: dict[str, int] | None = None) -> list[str]:
    """The words in a text that the author has never used, leaving out names (a capital letter inside a sentence) and
    the `allowed` words (the story's own words, from its sources). Each word once, in order."""
    words = vocab() if words is None else words
    out = []
    for m in re.finditer(r"[A-Za-zÆØÅæøåéü]+", text):
        w, lead = m.group(0), text[max(0, m.start() - 3):m.start()]
        name = w[0].isupper() and m.start() > 0 and not re.search(r"[.!?:\n]\s*$|^\s*$", lead)
        lw = w.lower()
        if name or lw in allowed or lw in out or known(lw, words):
            continue
        out.append(lw)
    return out


def samples(topic: str, k: int = 15) -> list[str]:
    """The author's own dictated sentences (Wispr) closest to a topic: the content words they share, rare ones counting
    most. Only sentences of 8 to 30 words with no digits, names or addresses, so they carry the author's way of talking
    and no facts, and only words said at least 3 times, which leaves out the misheard ones."""
    if "pool" not in _cache:
        pool, words = [], vocab()
        dictations = BANK / "wispr_dictations.jsonl"
        for line in dictations.open() if dictations.exists() else []:
            d = json.loads(line)
            if d.get("lang") != LANG:
                continue
            for s in re.split(r"(?<=[.!?])\s+", (d.get("written") or "").replace("\n", " ")):
                n = len(s.split())
                if 8 <= n <= 30 and not re.search(r"[\d@]|snabel|www|http", s) and not re.search(r"(?<![.!?]\s)(?<!^)\b[A-ZÆØÅ][a-zæøå]+", s.strip()) \
                        and all(words.get(w, 0) >= 3 for w in tokens(s)):
                    pool.append(s.strip())
        df = collections.Counter(w for s in pool for w in set(tokens(s)))
        _cache["pool"], _cache["df"] = pool, df
    pool, df = _cache["pool"], _cache["df"]
    want = {w for w in tokens(topic) if len(w) >= 4 and w not in COMMON and df.get(w)}
    idf = {w: math.log(len(pool) / df[w]) for w in want}
    scored = sorted(((sum(idf[w] for w in set(tokens(s)) & want), s) for s in pool), reverse=True)
    picked, seen = [], set()
    for score, s in scored:
        key = frozenset(tokens(s))
        if score <= 0 or any(len(key & o) > 0.6 * len(key) for o in seen):
            continue
        picked.append(s)
        seen.add(key)
        if len(picked) == k:
            break
    return picked


def selftest() -> None:
    words = {"bil": 3, "kvaje": 1, "bestyrelse": 2, "lokale": 4, "sjovt": 9, "sgu": 50, "strand": 2, "bod": 1}
    cases = [
        (known("bilen", words) and known("biler", words), "an inflection of a word the author uses"),
        (known("kvajer", words), "a verb form"),
        (known("bestyrelseslokale", words), "a compound of two words the author uses"),
        (not known("morsomt", words), "a word the author never uses"),
        (unknown("Det var hylende morsomt på stranden.", words=words) == ["det", "var", "hylende", "morsomt", "i"]
         or "morsomt" in unknown("Det var hylende morsomt.", words=words), "flags unknown words"),
        (unknown("Jeg mødte Kirsten i Nordby.", allowed={"jeg", "mødte", "i"}, words=words) == [], "names and allowed words pass"),
        (unknown("Hylende sjovt. Morsomt.", words=words) == ["hylende", "morsomt"], "a capital after a full stop is no name"),
    ]
    fails = [why for ok, why in cases if not ok]
    print("\n".join(fails) or f"selftest ok: {len(cases)} cases")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if not args or args[0] == "--selftest":
        selftest()
    arg = " ".join(args[1:])
    text = pathlib.Path(arg).read_text() if arg and pathlib.Path(arg).exists() else arg
    if args[0] == "refresh":
        d = refresh(quiet=False)
        print(f"word list rebuilt: {d['distinct']} distinct words; words per source: {d['words_per_source']}")
    elif args[0] == "check":
        print("never used:", ", ".join(unknown(text)) or "none")
    elif args[0] == "samples":
        print("\n".join(samples(text)))
