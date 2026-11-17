#!/usr/bin/env python3
"""Leak check: nothing private in the repo. Exits 1 and lists every hit, or exits 0.

Run it before every commit and before you share the repo. It checks every file git would commit (tracked files plus
untracked files that aren't ignored) and, with --history, every commit message and author line.

Two kinds of checks:
  1. Patterns, always on: e-mail addresses (example.com/.dk/.org and GitHub noreply allowed), absolute home paths,
     API keys and tokens (Anthropic, Google, GitHub, ElevenLabs agent and conversation ids, LinkedIn member urns),
     IP addresses, phone numbers, long random-looking ids (Drive/Doc ids and the like), and long dashes (en and em).
  2. Your private terms, from a file you keep OUTSIDE the repo (--terms, or $LEAK_TERMS_FILE): one term per line
     (client and lead names, people, domains, account ids, your own name if you want it out), # for comments.
     Matched whole-word and case-insensitive. The list itself never goes into the repo: it is the private part.

False hits: scripts/leak_allow.txt, one per line: a term to skip everywhere, or path:term to skip it in one file only.

    python3 scripts/leak_check.py                              # patterns only
    python3 scripts/leak_check.py --terms ~/private/terms.txt  # patterns + your private terms
    python3 scripts/leak_check.py --terms ~/private/terms.txt --history
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOW_FILE = ROOT / "scripts" / "leak_allow.txt"
OK_EMAIL_DOMAINS = re.compile(r"@(example\.(com|dk|org|net)|users\.noreply\.github\.com)$", re.I)
DASHES = chr(0x2013) + chr(0x2014)  # en and em dash, kept out of the source on purpose
PATTERNS = [
    ("e-mail address", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("absolute home path", re.compile(r"/Users/[A-Za-z]|/home/[a-z]")),
    ("Anthropic or other sk- key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("ElevenLabs key", re.compile(r"\b(?:xi-[A-Za-z0-9]{16,}|sk_[a-f0-9]{24,})")),
    ("ElevenLabs agent id", re.compile(r"\bagent_[0-9a-z]{12,}")),
    ("ElevenLabs conversation id", re.compile(r"\bconv_[0-9a-z]{12,}")),
    ("LinkedIn member urn", re.compile(r"urn:li:person:[A-Za-z0-9_-]{4,}")),
    ("IP address", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("phone number", re.compile(r"\+\d{2}[ \d]{8,12}\d")),
    ("long dash", re.compile("[" + DASHES + "]")),
]
ID_LIKE = re.compile(r"(?<![A-Za-z0-9_/.-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def files() -> list[str]:
    """What a commit would carry: tracked files plus untracked, not ignored ones."""
    out = _git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    return sorted(set(f for f in out if (ROOT / f).is_file()))


def _id_like(tok: str) -> bool:
    """A random-looking id (a Drive or Doc id, a key): mixed case and digits, not an ordinary identifier."""
    if not (re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok) and re.search(r"\d", tok)):
        return False
    parts = re.split(r"[_-]", tok)
    return not all(re.fullmatch(r"[A-Za-z]+\d*|\d+", p) for p in parts if p)


def load_allow() -> tuple[set[str], set[tuple[str, str]]]:
    everywhere, per_file = set(), set()
    for line in ALLOW_FILE.read_text().splitlines() if ALLOW_FILE.exists() else []:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and not line.startswith("http"):
            path, term = line.split(":", 1)
            per_file.add((path.strip(), term.strip().lower()))
        else:
            everywhere.add(line.lower())
    return everywhere, per_file


def load_terms(path: str | None) -> list[str]:
    if not path:
        return []
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        sys.exit(f"terms file not found: {p}")
    terms = {l.strip().lower() for l in p.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")}
    return sorted(t for t in terms if len(t) >= 3)


def texts(history: bool) -> list[tuple[str, str]]:
    out = []
    for name in files():
        data = (ROOT / name).read_bytes()
        if b"\0" in data[:8000]:
            continue  # binary
        out.append((name, data.decode("utf-8", errors="ignore")))
    if history:
        try:
            log = _git("log", "--format=%H%n%an <%ae>%n%B%n--end--")
        except subprocess.CalledProcessError:
            log = ""  # no commits yet
        for block in log.split("--end--"):
            lines = block.strip().splitlines()
            if lines:
                out.append((f"commit {lines[0][:10]}", "\n".join(lines[1:])))
    return out


def check(terms: list[str], history: bool) -> list[str]:
    allow, allow_in = load_allow()
    pats = [(t, re.compile(r"(?<![\w@.-])" + re.escape(t) + r"(?![\w@-])", re.I)) for t in terms if t not in allow]
    hits = []

    def hit(where: str, text: str, at: int, what: str, found: str) -> None:
        if found.lower() in allow or (where, found.lower()) in allow_in:
            return
        hits.append(f"{where}:{text.count(chr(10), 0, at) + 1}: {what}: {found!r}")

    for where, text in texts(history):
        for what, pat in PATTERNS:
            if where == "scripts/leak_check.py" and what != "long dash":
                continue  # this file spells out the patterns it looks for
            for m in pat.finditer(text):
                if what == "e-mail address" and OK_EMAIL_DOMAINS.search(m.group(0)):
                    continue
                hit(where, text, m.start(), what, m.group(0))
        for m in ID_LIKE.finditer(text):
            if _id_like(m.group(0)):
                hit(where, text, m.start(), "id-like string", m.group(0))
        if where == "scripts/leak_allow.txt":
            continue  # it lists only terms already judged harmless
        low = text.lower()
        for t, pat in pats:
            if t in low:
                for m in pat.finditer(text):
                    hit(where, text, m.start(), "private term", m.group(0))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--terms", default=os.environ.get("LEAK_TERMS_FILE"), help="your private terms, one per line (kept outside the repo)")
    ap.add_argument("--history", action="store_true", help="also every commit message and author line")
    a = ap.parse_args()
    terms = load_terms(a.terms)
    hits = check(terms, a.history)
    scope = f"{len(files())} files" + (", commit history" if a.history else "") + f", {len(PATTERNS) + 1} patterns, {len(terms)} private terms"
    if hits:
        print(f"Leak check: {len(hits)} hit(s) in {scope}:")
        print("\n".join(f"  {h}" for h in hits[:300]))
        print(f"A false hit: add it to {ALLOW_FILE.relative_to(ROOT)} (term, or path:term).")
        return 1
    print(f"Leak check: clean ({scope}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
