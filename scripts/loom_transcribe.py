#!/usr/bin/env python3
"""Loom link in, transcript out: yt-dlp downloads the shared video, local Whisper transcribes it.

Shared Loom links download without a login or the Loom API. Loom's own captions don't come through yt-dlp, so the
audio is transcribed on your own machine and never leaves it. Language is detected per video; force it with --lang.

Out: <out>/<loom id>.txt with a title line and "[mm:ss] text" lines; an existing transcript is reused.

Usage:
  python3 scripts/loom_transcribe.py <loom url> [<loom url> ...] [--lang da] [--out DIR]
"""
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

import ce_config as C

OUT = C.CACHE / "looms"
_model = None
GARBAGE = re.compile(r"[Ѐ-ӿ　-鿿가-힯]")  # Cyrillic, CJK, Hangul: a hallucination in Danish audio
# lines Whisper is known to invent over silence (subtitle credits from its training data)
PHANTOM = re.compile(r"(?i)(tekster af|undertekster|subtitles by|thanks for watching|tak fordi du så med)")


def loom_id(url: str) -> str:
    m = re.search(r"loom\.com/(?:share|embed)/([0-9a-f]{32})", url)
    if not m:
        sys.exit(f"not a Loom share link: {url}")
    return m.group(1)


def _whisper():
    global _model
    if _model is None:
        import whisper
        _model = whisper.load_model(C.get("video", "whisper_model"))
    return _model


def transcribe(url: str, lang: str | None, out: pathlib.Path) -> pathlib.Path:
    vid = loom_id(url)
    txt = out / f"{vid}.txt"
    if txt.exists():
        return txt
    title = subprocess.run(["yt-dlp", "--no-update", "--no-playlist", "--print", "title", url],
                           capture_output=True, text=True).stdout.strip()
    # Loom serves audio as its own stream, sometimes two (cdn and raw), and one can be silent (Whisper then writes the
    # same word over and over). Try each audio stream, then the full transcoded file.
    res = None
    for fmt in ("bestaudio", "hls-raw-audio-audio", "hls-cdn-audio-audio", "http-transcoded"):
        with tempfile.TemporaryDirectory() as tmp:
            got = subprocess.run(["yt-dlp", "--no-update", "--no-playlist", "-q", "-f", fmt, "-o", f"{tmp}/v.%(ext)s", url],
                                 capture_output=True, text=True)
            if got.returncode != 0:
                if "Failed to resolve" in got.stderr or "Unable to download" in got.stderr:
                    raise subprocess.CalledProcessError(got.returncode, "yt-dlp", got.stderr[-200:])
                continue  # this format doesn't exist for this video
            src = next(pathlib.Path(tmp).glob("v.*"))
            wav = pathlib.Path(tmp) / "a.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
            r = _whisper().transcribe(str(wav), language=lang, condition_on_previous_text=False, fp16=False,
                                      no_speech_threshold=0.6, initial_prompt=title or None)
            segs = [s for s in r["segments"] if s["text"].strip() and not PHANTOM.search(s["text"]) and not GARBAGE.search(s["text"])]
            res = {"segments": segs, "language": r.get("language", lang)}
        texts = [s["text"].strip().lower() for s in res["segments"]]
        if len(texts) < 6 or len(set(texts)) > 3:
            break
    else:
        raise subprocess.CalledProcessError(1, "whisper", "no speech found in any stream")
    lines = [f"# {title or vid} ({url}) · language {res.get('language')}"]
    lines += [f"[{int(s['start']) // 60:02d}:{int(s['start']) % 60:02d}] {s['text'].strip()}" for s in res["segments"]]
    out.mkdir(parents=True, exist_ok=True)
    txt.write_text("\n".join(lines) + "\n")
    return txt


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--lang", help="force the language, e.g. da or en (default: detect)")
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    a = ap.parse_args()
    for u in a.urls:
        try:
            print(transcribe(u, a.lang, a.out), flush=True)
        except subprocess.CalledProcessError as e:
            print(f"FAILED {u}: {e}", flush=True)
