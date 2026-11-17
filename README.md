# Content Engine

An AI content agent for one person's LinkedIn, in Danish. A voice agent interviews the author once a week, Claude
writes a numbered batch of posts into a Google Doc, the author approves or corrects them there, and the engine learns
from every correction. Photos, graphics and captioned videos are made and placed under the posts, approved posts go
out on a plan, and LinkedIn's numbers and DMs flow back into the next batch.

It is the system we run for our own LinkedIn, with all our data taken out. Made by RM Marketing, MIT license.

**It runs on your own machine, and the setup is yours.** Everything runs on your own Mac with your own Google,
Claude, ElevenLabs and LinkedIn accounts; nothing goes through us. The code comes as is: no setup help, support or
updates are promised, and issues are turned off. Fork it and make it yours.

**Automatic posting is off until you turn it on.** LinkedIn's API terms ban using the API to automate posting, and
LinkedIn can shut an app off without warning. `[posting] mode` starts at `test` (it only logs what it would post);
read the terms before you set it to `live`, or post by hand from the plan sheet.

## An open-source alternative to Taplio, Supergrow and AuthoredUp

| | Content Engine | Taplio, Supergrow, AuthoredUp and similar |
|---|---|---|
| Where the posts come from | A weekly voice interview about what actually happened to you | Your prompts, templates and viral-post libraries |
| Your voice | Every word checked against words you have really said; corrections become rules | The tool's AI style, adjusted by settings |
| Photos, graphics, video | Picked from your own photo library, drawn, and your recordings cut and captioned | Mostly text, with image tools in some |
| Price | Free, MIT; you pay your own Claude and ElevenLabs use | Monthly subscription |
| Platform | Your own Mac; the posts are written in Danish | Web |

Pick one of them if you want a web app that works in a minute. Pick this if you want posts from your own stories, in
your own words, and the whole thing on your own machine.

Code and this README are in English. Danish is product behaviour: the posts, the Danish word checks, the captions.

## The weekly loop

1. **Sunday:** the scout reads the week's mail and picks at most two stories worth asking about. They go into the
   interviewer's brief.
2. **Once a week:** the author calls the interviewer (an ElevenLabs voice agent, 20 to 30 minutes, from a personal
   link). It offers three topics, digs for the moment, the number, the turn and the point, and never asks for what is
   already written down.
3. **Within the hour:** a finished call of 8+ minutes, 4+ days after the last batch, starts the next batch by itself:
   7 posts in a new tab of the posts doc, each with a graphic, a photo of the author where it fits, a shoot card for
   video posts, and a grey "Check" line with what to confirm. A five-line note lands in the author's own inbox.
4. **Approve:** a ✅ in a post's heading, an edit by hand in the doc, a reply to the note ("1 og 2 er gode, 3
   kortere, drop 4"), a Loom, or the next call. The hourly job turns that into edits (old lines red, new green) and
   into rules in the rulebook, quoting the author's own words.
5. **Video:** the author records from the card and drops the raw file in the Drive "Video inbox" folder. Within 10
   minutes it is cut to the speech, the "øh"s are out, the sound is cleaned, Danish captions are burned in, and the
   finished video is linked under its post.
6. **Posting:** approved posts with their media get a date on the plan sheet (your weekdays, an uneven time each
   day). The posting bot puts them on LinkedIn, at most one a day, or someone schedules them by hand from the sheet.
7. **Numbers:** daily, LinkedIn's post numbers are read; on Mondays clear gaps between kinds of posts become rules.
   DMs per post come from LinkedIn's data export. Both go into the files the drafter reads before the next batch.

## Architecture

```
 sources                       engine                                       out
 ─────────                     ──────                                       ───
 interviewer calls ─┐          draft ─ rule check + repair ─ hook pass      Google Doc: one tab per batch
 call transcripts ─┼──────►   ─ native-Danish read ─ own-words check  ──►  graphics, photos, video cards
 story seeds ───────┤          + numbers check                              done-note in your inbox
 week's mail (scout)┘                    ▲                                          │
                                         │ rules, feedback log                      ▼ approve / edit / reply / call
 knowledge base (data/kb) ───────────────┤                                  feedback job ─► rulebook
 LinkedIn numbers + DMs ─────────────────┘                                          │
                                                                                    ▼
                                                     plan sheet ─► posting bot ─► LinkedIn ─► numbers, DMs
```

**The five drafting steps** (content_engine_draft.py):
1. **Draft:** Claude writes the batch from the sources, the facts sheet, the house rules, the learned rules, what
   works, the feedback log, the two newest approved batches as style, and the author's own dictated sentences.
2. **Rule check and repair:** mechanical rules in code (no long dashes, no list markers, æøå, emoji set, kroner not
   dollars, one swear word at most, a jargon list, no reused hook, no story told twice, the weekly mix), one repair
   pass, then text posts become photo posts and the batch is put in a posting order.
3. **Hook pass:** four new openings per post from its own facts; the strongest of five is kept and the others stay
   in the doc.
4. **Native-Danish read:** a second read lists words a Dane would never write and swaps them.
5. **Own-words check:** every word is checked against the words the author has really said (Wispr dictations,
   their lines in call transcripts and their own mails); words they never say are swapped, the rest listed. Every
   number must stand in a source, or it is put on the Check line.

**The approval doc** is a Google Doc with one tab per batch (and a "Video scripts" tab for the shoot cards). The
engine writes it under one lock, reads hand edits back from it, and marks decisions in the headings.

**Scheduling and posting:** `linkedin_post.py plan` keeps a sheet in the engine's Drive folder; `due` posts what is
due. `[posting] host` decides which machine posts (the Mac, or a server over ssh so it posts with the Mac off).

**The DM feedback loop:** `linkedin_dma_sync.py` pulls the inbox from LinkedIn's data export, `linkedin_post_dms.py`
sorts each new conversation once (who they are, whether they name a post) and counts DMs per post,
`linkedin_post_results.py` reads reach and engagement and writes the live block in `data/kb/what_works.md` and the
"From the numbers" rules in `data/kb/voice_rules.md`.

## Components

| What | Entry point |
|------|-------------|
| Settings, folders, defaults | `scripts/ce_config.py` (reads `config.toml`) |
| Google login and access | `scripts/google_api.py login` |
| Drafter | `scripts/content_engine_draft.py --seeds data/kb/story_seeds.md --tab "Engine Batch 1"`, `--auto-batch`, `--selftest` |
| Feedback | `scripts/content_engine_feedback.py --auto` (hourly), `--mail`, `--call latest`, `--loom URL`, `--text "..."` |
| Interviewer (ElevenLabs) | `scripts/content_engine_interviewer.py voices / create / refresh / link / learn / topic` |
| Scout | `scripts/content_engine_scout.py --dry-run` / `--push` |
| Graphics and carousels | `scripts/content_engine_visuals.py <batch>.json --auto` |
| Photos | `scripts/content_engine_photos.py --auto`, `--library <batch>.json`, `--install-app`, `--selftest` |
| Photos library reader (macOS) | `scripts/photo_library_reader.py --install-app` (then Full Disk Access), `--scan` |
| Video | `scripts/content_engine_video.py --auto`, `process <file>`, `redo <name>`, `script --batch ... --post N`, `--selftest` |
| Posting and plan | `scripts/linkedin_post.py app / auth / status / list / post N / plan / due / --selftest` |
| Post numbers | `scripts/linkedin_post_results.py app / auth / tick / check / tag / selftest` |
| DMs per post | `scripts/linkedin_dma_sync.py --auto`, `scripts/linkedin_post_dms.py` |
| Scorecard | `scripts/content_engine_scorecard.py` |
| Voice bank | `scripts/voice_bank.py refresh / check / samples / --selftest`, `scripts/voice_bank_wispr.py` |
| Call transcripts (any note taker, mail) | `scripts/transcripts.py list / show DATE:TITLE / sync-mail / --selftest` |
| Loom to text | `scripts/loom_transcribe.py <url>` |
| Drive folder of videos to text | `scripts/drive_video_transcribe.py <folder_id> <out_dir>` |
| Claude calls (API or subscription) | `scripts/llm_guard.py` |
| Gmail helpers | `scripts/gmail_util.py` |
| Leak check | `scripts/leak_check.py --terms <your private list>` |
| Test | `python -m unittest scripts/tests/test_content_engine_feedback_auto.py` |

Prompts: `prompts/interviewer_prompt.md` (copied to `data/kb/` on first use; the `learn` step writes the author's
call rules into that copy) and `prompts/linkedin_rules.md` (house rules; put your own in `data/kb/linkedin_rules.md`).
Knowledge base templates with made-up examples: `templates/kb/`. An example drawing: `templates/art/`.
Scheduling: `launchd/*.plist`.

## Requirements

- **macOS** for the full engine (Photos library, the Vision face finder, `avconvert` for iPhone HDR video,
  `pbpaste`, `open`, launchd). Drafting, feedback, graphics and posting run anywhere with Python.
- **Python 3.12+** and `pip install -r requirements.txt`, then `python -m playwright install chromium`.
- **ffmpeg** with libx264 and **yt-dlp** (`brew install ffmpeg yt-dlp`). Xcode command line tools for `swiftc`.
- **Whisper large-v3** for Danish captions and Looms: downloaded by openai-whisper on first use (about 3 GB).
- **Claude:** Claude Code installed and logged in (simple calls then run on your Claude subscription), or an
  Anthropic API key in `ANTHROPIC_API_KEY` with `[llm] backend = "api"`. The photo picker needs Claude Code (it
  shows Claude the image through the CLI).
- **ElevenLabs:** an account with Agents (Conversational AI) and an API key. Weekly calls of 20 to 30 minutes need a
  plan with enough agent minutes; check their pricing.
- **Google:** a Cloud project with the Docs, Drive, Sheets and Gmail APIs on, and an OAuth client of type Desktop
  app. Scopes: `documents`, `drive`, `spreadsheets`, `gmail.modify` (the engine reads replies and places notes in
  your own inbox; it never sends).
- **LinkedIn developer apps:**
  - posting: products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect" (scopes `openid profile
    w_member_social`), redirect URL `http://localhost:8765/linkedin/callback`
  - numbers (optional): a second app with the "Community Management API" (scope `r_member_postAnalytics`, apply for
    access), redirect URL `http://localhost:8767/linkedin/callback`
  - DMs (optional): a token for the "Member Data Portability API" (member's own data), saved as
    `tokens/linkedin_dma_token.json` `{"access_token": "..."}`. It is made outside this repo.
  - Read LinkedIn's API terms before you turn the posting bot on: they forbid automated posting.
- Optional: **Wispr Flow** (the dictations are the voice bank's best source), and call transcripts from any note
  taker (see "Connecting a note taker or mail").

## Setup

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.toml config.toml          # fill it in (see Config below)
mkdir -p data/logs && cp -R templates/kb data/kb   # then replace the examples in data/kb with your own
.venv/bin/python scripts/content_engine_draft.py --selftest   # and the other --selftest commands
```

1. **Google:** put the OAuth client JSON at `tokens/google_client_secret.json`, run `scripts/google_api.py login`.
   Make a Google Doc for the posts and a Drive folder for the engine with two subfolders, "Video inbox" and "Finished
   videos"; put their ids in `[google]`.
2. **Claude:** install Claude Code and log in, or set `ANTHROPIC_API_KEY` and `[llm] backend = "api"`.
3. **ElevenLabs:** save the API key as `tokens/elevenlabs_api_key.txt`, then `content_engine_interviewer.py voices`,
   `create --voice <id>`, `refresh`, and `link` for the author's talk link.
4. **LinkedIn:** copy the posting app's client secret, run `linkedin_post.py app <client id>`, then `auth` and
   `status`. Keep `[posting] mode = "test"` until a few `due --dry` runs look right. The same for
   `linkedin_post_results.py app` / `auth` if you use the numbers.
5. **Photos (macOS, optional):** `content_engine_photos.py --install-app` and `photo_library_reader.py --install-app`,
   then give "Content Engine Photos Reader" Full Disk Access in System Settings. Set `[photos] me_names` to what
   Photos calls you under People.
6. **First batch:** write a few seeds in `data/kb/story_seeds.md`, then
   `content_engine_draft.py --seeds data/kb/story_seeds.md --tab "Engine Batch 1"`.
7. **Schedule:** fill in `__REPO__`, `__HOME__` and `__USER__` in `launchd/*.plist` and load them (see the comment at
   the top of each). The hourly feedback job runs the other jobs too.

## Connecting a note taker or mail

The engine reads call transcripts from one folder, `[sources] transcripts_dir`. Any note taker works, as long as its
transcript ends up there as a file:

- **Export by hand:** MeetGeek, Fireflies, Otter, Fathom, tl;dv, Zoom, Teams and Google Meet all export a transcript
  (.txt, .vtt, .srt, .docx or .json). Save it as `2026-10-26 Content call.txt`: the date in front, the title after.
- **Automatic:** let the note taker save to a synced folder (Google Drive for desktop, Dropbox), or add a Zapier or
  Make step "new transcript, save file" pointed at the folder.
- **By mail:** most note takers mail you after a call. Give those mails a Gmail label (by hand or with a filter),
  set `[sources] mail_label`, and `transcripts.py sync-mail` saves them in the folder: attachments as they are, the
  mail text as lines. A mail you wrote yourself counts as your own words, so a few long mails you wrote by hand
  also feed the voice bank.

Then set `[sources] speaker` to your name as the note taker writes it. `transcripts.py list` shows each file with how
many lines are yours; `content_engine_draft.py --call 2026-10-26:"Content call"` makes a batch from one of them. Only
calls you pick by date and title become a source, never a client call by accident. The voice bank reads your lines
from every file in the folder.

## Config

Everything is in `config.toml`; `config.example.toml` lists every key with a comment, and `scripts/ce_config.py`
holds the defaults. The sections:

| Section | Keys |
|---------|------|
| `[paths]` | `data_dir`, `tokens_dir`, `cache_dir` |
| `[author]` | `name`, `first_name`, `language`, `business`, `pitch`, `voice`, `dialect_note`, `emoji`, `own_words`, `never_words`, `gap_marker`, `mail_from`, `mail_to`, `own_addresses`, `own_domains` |
| `[llm]` | `model`, `small_model`, `backend`, `claude_bin` |
| `[drafting]` | `batch_size`, `auto_approve_from`, `no_colons`, `english_tab`, `proposed_tag` |
| `[google]` | `token_file`, `gmail_token_file`, `client_secret_file`, `doc_id`, `drive_root_id`, `photos_folder_id`, `video_inbox_id`, `finished_videos_id`, `video_sop_url` |
| `[elevenlabs]` | `key_file`, `agent_name`, `llm`, `tts_model`, `language`, `max_call_minutes`, `voice_en`, `voice_da`, `keyterms`, `openers`, `lookup_url`, `lookup_keys_file` |
| `[linkedin]` | `profile_slug`, `api_version`, `post_app_file`, `post_token_file`, `post_port`, `analytics_app_file`, `analytics_token_file`, `analytics_port`, `analytics_call_cap`, `dma_token_file`, `dma_server_ssh`, `dma_server_index` |
| `[plan]` | `timezone`, `weekdays`, `daily_from`, `anchor`, `slot_from`, `slot_to`, `slot_apart_minutes`, `pauses`, `scheduler_tz`, `runway_min` |
| `[posting]` | `mode`, `host`, `server_ssh`, `server_repo`, `server_python`, `server_log` |
| `[photos]` | `library`, `me_names`, `reference_photo`, `search_language`, `describe_hint`, `extra_rules` |
| `[visuals]` | `brand_name`, `navy`, `accent`, `cloud`, `ink2`, `line`, `font_regular`, `font_bold`, `font_light`, `logo_light`, `logo_dark`, `art_dir` |
| `[video]` | `glossary`, `whisper_model` |
| `[scout]` | `days`, `max_stories`, `max_mails`, `contacts_file`, `banned_names_file`, `facts_plugin`, `private_topics` |
| `[sources]` | `transcripts_dir`, `speaker`, `mail_label`, `wispr_db`, `meetgeek_dir`, `meetgeek_speaker` |

`data/`, `tokens/` and `config.toml` are git-ignored: they hold the author's words, photos, numbers and keys.

## What is not included

- **Our data.** No knowledge base, rules, stories, posts, voice bank, photos, numbers, feedback or prompts' learned
  rules. The files in `templates/` show the format with made-up examples.
- **Our CRM hooks.** In our setup the scout also read our CRM (meetings, account changes, pipeline moves, wins), our
  client registry and lead files. Here they are seams you fill yourself: `[scout] facts_plugin` (a Python file with
  `collect(start, end) -> dict`), `contacts_file` and `banned_names_file`. Without them the scout reads every personal
  mail of the week. The Gmail helpers it shared with our CRM jobs are vendored as `gmail_util.py`.
- **The interviewer's lookup server.** The live lookups (mail, LinkedIn messages, posts, calls, calendar, web pages)
  need a webhook server that reads your own data. Ours read our CRM and client accounts, so it is not here; the
  `tools` command and the prompt section are, for when you build your own (`[elevenlabs] lookup_url`). Without it
  the interviewer runs without lookups.
- **Removed:** our team dashboard refresh after every feedback round, the job that indexed our calls and client
  numbers for the lookups, the Mac-to-server state sync, Slack and Productive, the client-number lookups, and our
  brand assets (logos, fonts, drawings). Loom transcription runs on plain Whisper instead of our call recorder's
  pipeline.
- **Not tested here end to end.** This copy compiles and its selftests and unit tests pass, but it has not run a
  full week against fresh accounts. Expect to fix setup details on the first run.
