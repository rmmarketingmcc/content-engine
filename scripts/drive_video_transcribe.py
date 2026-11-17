#!/usr/bin/env python3
"""
Stream Drive videos through local Whisper, save transcripts, delete intermediates.

For each MP4 in a Drive folder tree:
  1. Download MP4 to temp dir
  2. Extract 16kHz mono WAV via ffmpeg (much smaller)
  3. Delete MP4
  4. Transcribe WAV with Whisper (MPS on Apple Silicon)
  5. Save transcript to output dir
  6. Delete WAV

Use it to turn a folder of old videos (webinars, talks, earlier recordings) into text the engine can draft from, or
into more of the author's own words for the voice bank.

Usage:
    python3 scripts/drive_video_transcribe.py <folder_id> <output_dir> [model] [language]
    model defaults to "large-v3" (Danish needs it; "small.en" is fast for English only), language to "da".
"""

import os
import sys
import json
import time
import re
import subprocess
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
DRIVE_API = "https://www.googleapis.com/drive/v3"
GFOLDER = "application/vnd.google-apps.folder"


def safe_name(name: str) -> str:
    name = re.sub(r"[/\\:*?\"<>|]", "_", name)
    return name[:180]


def get_creds():
    import google_api
    return google_api.creds()


def headers(creds):
    return {"Authorization": f"Bearer {creds.token}"}


def list_folder(creds, folder_id: str) -> list[dict]:
    out = []
    page_token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id,name,mimeType,size)",
            "pageSize": 200,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(f"{DRIVE_API}/files", headers=headers(creds), params=params, timeout=30)
        if r.status_code != 200:
            print(f"  [!] list failed: {r.status_code}", file=sys.stderr)
            break
        data = r.json()
        out.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def collect_mp4s(creds, folder_id: str, path: list[str] = None) -> list[dict]:
    """Recursively walk folder tree, collect {id, name, size, path} for video/mp4 files."""
    if path is None:
        path = []
    out = []
    for it in list_folder(creds, folder_id):
        if it["mimeType"] == GFOLDER:
            out.extend(collect_mp4s(creds, it["id"], path + [safe_name(it["name"])]))
        elif it["mimeType"] == "video/mp4":
            out.append({
                "id": it["id"],
                "name": safe_name(it["name"]),
                "size": int(it.get("size", 0)),
                "path": path,
            })
    return out


def download_file(creds, file_id: str, dest: Path):
    """Stream download to disk."""
    r = requests.get(
        f"{DRIVE_API}/files/{file_id}",
        headers=headers(creds),
        params={"alt": "media", "supportsAllDrives": "true"},
        timeout=1800,
        stream=True,
    )
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            if chunk:
                f.write(chunk)


def extract_audio(video_path: Path, audio_path: Path) -> bool:
    """Extract 16kHz mono WAV via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-i", str(video_path),
        "-ac", "1", "-ar", "16000", "-vn",
        "-c:a", "pcm_s16le",
        str(audio_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  [!] ffmpeg failed: {res.stderr[:300]}", file=sys.stderr)
        return False
    return True


def transcribe(audio_path: Path, model_name: str, device: str = "mps", language: str = "da") -> str | None:
    """Run local Whisper. Returns transcript text or None on failure."""
    import whisper
    model = transcribe._model_cache.get(model_name)
    if model is None:
        print(f"  loading whisper model {model_name} on {device}...", flush=True)
        model = whisper.load_model(model_name, device=device)
        transcribe._model_cache[model_name] = model
    try:
        # fp16 not supported on MPS fully - use fp32
        result = model.transcribe(
            str(audio_path),
            fp16=False,
            verbose=False,
            language=language,
        )
        return result.get("text", "").strip()
    except Exception as e:
        print(f"  [!] whisper failed: {e}", file=sys.stderr)
        return None
transcribe._model_cache = {}


def process_one(creds, video: dict, out_root: Path, model_name: str, tmp_root: Path, language: str = "da"):
    rel_path = Path(*video["path"]) if video["path"] else Path(".")
    out_dir = out_root / rel_path
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = out_dir / (video["name"].replace(".mp4", "") + ".txt")

    if transcript_path.exists() and transcript_path.stat().st_size > 500:
        print(f"  [SKIP existing] {transcript_path}")
        return "skip"

    tmp_root.mkdir(parents=True, exist_ok=True)
    video_tmp = tmp_root / f"{video['id']}.mp4"
    audio_tmp = tmp_root / f"{video['id']}.wav"

    size_mb = video["size"] / 1e6
    print(f"\n[{rel_path}/{video['name']}] size={size_mb:.1f}MB")
    try:
        t0 = time.time()
        print(f"  downloading...", flush=True)
        download_file(creds, video["id"], video_tmp)
        dl_s = time.time() - t0

        print(f"  download done in {dl_s:.0f}s, extracting audio...", flush=True)
        if not extract_audio(video_tmp, audio_tmp):
            return "error"
        video_tmp.unlink()  # free disk space before transcription

        audio_mb = audio_tmp.stat().st_size / 1e6
        t1 = time.time()
        print(f"  audio {audio_mb:.1f}MB, transcribing with {model_name}...", flush=True)
        text = transcribe(audio_tmp, model_name, language=language)
        tx_s = time.time() - t1

        if text is None or len(text) < 100:
            print(f"  [!] transcript too short/empty", file=sys.stderr)
            audio_tmp.unlink(missing_ok=True)
            return "error"

        transcript_path.write_text(text, encoding="utf-8")
        audio_tmp.unlink(missing_ok=True)
        print(f"  done: {len(text):,} chars in {tx_s:.0f}s ({audio_mb*8/tx_s:.1f}x realtime). Saved {transcript_path.name}")
        return "ok"
    except Exception as e:
        print(f"  [!] error: {e}", file=sys.stderr)
        for p in (video_tmp, audio_tmp):
            p.unlink(missing_ok=True)
        return "error"


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 else 1)
    folder_id = sys.argv[1]
    out_root = Path(sys.argv[2])
    model_name = sys.argv[3] if len(sys.argv) > 3 else "large-v3"
    language = sys.argv[4] if len(sys.argv) > 4 else "da"

    tmp_root = Path(tempfile.gettempdir()) / "whisper_pipeline"
    creds = get_creds()

    print(f"Discovering videos in {folder_id}...")
    videos = collect_mp4s(creds, folder_id)
    total_mb = sum(v["size"] for v in videos) / 1e6
    print(f"Found {len(videos)} MP4s totalling {total_mb:.0f} MB ({total_mb/1024:.1f} GB)")

    stats = {"ok": 0, "skip": 0, "error": 0}
    for i, v in enumerate(videos, 1):
        print(f"\n=== [{i}/{len(videos)}] ===")
        # Refresh creds if needed before each (token can expire during long runs)
        creds = get_creds()
        status = process_one(creds, v, out_root, model_name, tmp_root, language)
        stats[status] += 1
        # Be polite
        time.sleep(0.2)

    print(f"\nDONE: ok={stats['ok']}, skipped={stats['skip']}, errors={stats['error']}")


if __name__ == "__main__":
    main()
