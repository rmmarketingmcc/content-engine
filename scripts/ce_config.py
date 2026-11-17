"""The content engine's settings: config.toml at the repo root (or the file in $CONTENT_ENGINE_CONFIG).

Every ID, path, name and account lives there, never in the code. Copy config.example.toml to config.toml and fill
it in. A missing file is fine for --help and the selftests: everything falls back to the defaults below, and a
command that needs a value it doesn't have stops with the key to set.

    import ce_config as C
    C.get("author", "name")          # a value, or its default
    C.need("google", "doc_id")       # a value, or a clear exit naming the key
    C.KB, C.BATCHES, C.STATE         # the data folders
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys

try:
    import tomllib
except ImportError:  # Python < 3.11
    sys.exit("The content engine needs Python 3.12 or newer (tomllib).")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_FILE = pathlib.Path(os.environ.get("CONTENT_ENGINE_CONFIG") or ROOT / "config.toml").expanduser()

DEFAULTS: dict[str, dict] = {
    "paths": {"data_dir": "data", "tokens_dir": "tokens", "cache_dir": "~/.cache/content-engine"},
    "author": {"name": "", "first_name": "Author", "language": "da", "business": "", "pitch": "", "voice": "casual, warm and direct",
               "emoji": "😅 🙏 🤓 👀", "own_words": [], "never_words": [], "dialect_note": "",
               "gap_marker": "[AUTHOR", "mail_from": "", "mail_to": "", "own_addresses": [], "own_domains": []},
    "llm": {"model": "claude-opus-5-5", "small_model": "claude-haiku-5-5", "backend": "auto", "claude_bin": ""},
    "drafting": {"batch_size": 7, "auto_approve_from": "", "no_colons": False, "english_tab": False,
                 "proposed_tag": "[Proposed by the author]"},
    "google": {"token_file": "google_token.json", "gmail_token_file": "", "client_secret_file": "google_client_secret.json",
               "doc_id": "", "drive_root_id": "", "photos_folder_id": "", "video_inbox_id": "",
               "finished_videos_id": "", "video_sop_url": ""},
    "elevenlabs": {"key_file": "elevenlabs_api_key.txt", "agent_name": "Content Interviewer", "llm": "claude-sonnet-5-5",
                   "tts_model": "eleven_flash_v2_5", "language": "da", "max_call_minutes": 35, "voice_en": "",
                   "voice_da": "", "keyterms": [], "openers": [], "lookup_url": "",
                   "lookup_keys_file": "engine_lookup_keys.json"},
    "linkedin": {"profile_slug": "", "api_version": "202610", "post_app_file": "linkedin_post_app.json",
                 "post_token_file": "linkedin_post_token.json", "post_port": 8765,
                 "analytics_app_file": "linkedin_analytics_app.json", "analytics_token_file": "linkedin_analytics_token.json",
                 "analytics_port": 8767, "analytics_call_cap": 80, "dma_token_file": "linkedin_dma_token.json",
                 "dma_server_ssh": "", "dma_server_index": ""},
    "plan": {"timezone": "Europe/Copenhagen", "weekdays": [0, 2, 4], "daily_from": "", "anchor": "2026-01-01",
             "slot_from": "06:40", "slot_to": "11:55", "slot_apart_minutes": 40, "pauses": [], "scheduler_tz": "",
             "runway_min": 2},
    "posting": {"mode": "test", "host": "mac", "server_ssh": "", "server_repo": "", "server_python": "",
                "server_log": ""},
    "photos": {"library": "~/Pictures/Photos Library.photoslibrary", "me_names": ["me"], "reference_photo": "",
               "search_language": "Danish", "describe_hint": "a business owner", "extra_rules": ""},
    "visuals": {"brand_name": "", "navy": "#192230", "accent": "#84CE18", "cloud": "#F8FAFC", "ink2": "#3E4B63",
                "line": "#D9E0EA", "font_regular": "/System/Library/Fonts/Supplemental/Arial.ttf",
                "font_bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "font_light": "/System/Library/Fonts/Supplemental/Arial.ttf", "logo_light": "", "logo_dark": "",
                "art_dir": ""},
    "video": {"glossary": "Øh, øhm. LinkedIn, Google.", "whisper_model": "large-v3"},
    "scout": {"days": 7, "max_stories": 2, "max_mails": 30, "contacts_file": "", "banned_names_file": "",
              "facts_plugin": "", "private_topics": []},
    "sources": {"transcripts_dir": "", "speaker": "", "mail_label": "Content engine", "meetgeek_dir": "",
                "meetgeek_speaker": "", "wispr_db": "~/Library/Application Support/Wispr Flow/flow.sqlite"},
}

_loaded: dict | None = None


def _load() -> dict:
    global _loaded
    if _loaded is None:
        _loaded = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    return _loaded


def get(section: str, key: str, default=None):
    """The value from config.toml, else the built-in default, else `default`."""
    v = _load().get(section, {}).get(key)
    if v is None or v == "":
        v = DEFAULTS.get(section, {}).get(key, default)
    return default if v is None else v


def need(section: str, key: str):
    """The value, or an exit that names the key to set (for a command that can't run without it)."""
    v = get(section, key)
    if v in (None, "", []):
        sys.exit(f"Set [{section}] {key} in {CONFIG_FILE} (see config.example.toml).")
    return v


def _dir(key: str) -> pathlib.Path:
    p = pathlib.Path(get("paths", key)).expanduser()
    return p if p.is_absolute() else ROOT / p


def path(value: str) -> pathlib.Path:
    """A path from the config: ~ expanded, relative to the repo root."""
    p = pathlib.Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


DATA = _dir("data_dir")
TOKENS = _dir("tokens_dir")
CACHE = _dir("cache_dir")
KB = DATA / "kb"                 # the knowledge base the writer reads (templates/kb/ shows the format)
BATCHES = DATA / "batches"       # one JSON file per batch, plus its feedback state and graphics spec
BRIEFS = DATA / "briefs"         # the interviewer's weekly briefs
STATE = DATA / "state"           # agent ids, logs, results: everything the jobs keep between runs
PROMPTS = ROOT / "prompts"


def token(key: str) -> pathlib.Path:
    """A file in the tokens folder, named by the config key in [google], [elevenlabs] or [linkedin]."""
    for section in ("google", "elevenlabs", "linkedin"):
        if key in DEFAULTS[section]:
            return TOKENS / get(section, key)
    raise KeyError(key)


def claude_bin() -> str:
    return get("llm", "claude_bin") or shutil.which("claude") or "/opt/homebrew/bin/claude"


def author() -> str:
    """The author's first name, as it stands in transcripts and prompts."""
    return get("author", "first_name")


def ensure_dirs() -> None:
    for d in (DATA, KB, BATCHES, BRIEFS, STATE, TOKENS, CACHE):
        d.mkdir(parents=True, exist_ok=True)
