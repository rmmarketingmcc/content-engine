"""Gmail for the content engine: read mail, cut a reply down to the sender's own words, and place a note in your
own inbox. Nothing here ever sends a mail.

Your own addresses and domains ([author] own_addresses, own_domains) decide which side of a mail is "them".
"""
from __future__ import annotations

import base64
import email.utils
import json
import re
import urllib.request
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

import ce_config as C

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
MAX_BLOCK_CHARS = 4000
ADDR_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")


def _headers() -> dict:
    import google_api
    return {"Authorization": f"Bearer {google_api.creds(gmail=True).token}"}


def gmail_get(path: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(f"{GMAIL_API}/{path}", headers=_headers(), params=params or {}, timeout=(5, 30))
    r.raise_for_status()
    return r.json()


def _walk_parts(payload: dict):
    parts = payload.get("parts")
    if parts:
        for p in parts:
            yield from _walk_parts(p)
    else:
        yield payload


def body_text(msg: dict) -> str:
    texts = []
    for part in _walk_parts(msg.get("payload", {})):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        try:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            texts.append(text)
        elif mime == "text/html" and not texts:
            texts.append(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(texts)[:MAX_BLOCK_CHARS]


def get_headers(msg_id: str) -> dict:
    try:
        msg = gmail_get(f"users/me/messages/{msg_id}",
                        params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
    except Exception:  # noqa: BLE001
        return {}
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _own(addr: str) -> bool:
    own = {a.lower() for a in C.get("author", "own_addresses")}
    domains = {d.lower() for d in C.get("author", "own_domains")}
    return addr in own or addr.split("@", 1)[-1] in domains


def addresses(value: str) -> list[str]:
    return [a.lower() for a in ADDR_RE.findall(value or "")]


def counterpart_addresses(headers: dict) -> list[str]:
    out = [a for a in addresses(headers.get("From", "")) + addresses(headers.get("To", "")) if not _own(a)]
    return list(dict.fromkeys(out))


def fetch_message_blocks(msg_ids: list[str]) -> list[dict]:
    """Each mail as {msg_id, label, text, date_iso, inbound}; inbound = not from one of your own addresses."""
    blocks = []
    for mid in msg_ids:
        try:
            msg = gmail_get(f"users/me/messages/{mid}", params={"format": "full"})
        except Exception:  # noqa: BLE001
            continue
        text = body_text(msg)
        if not text.strip():
            continue
        h = {x["name"]: x["value"] for x in msg.get("payload", {}).get("headers", [])}
        try:
            date_iso = parsedate_to_datetime(h.get("Date")).date().isoformat()
        except Exception:  # noqa: BLE001
            date_iso = None
        sender = addresses(h.get("From") or "")
        blocks.append({"msg_id": mid, "label": f"email '{h.get('Subject') or '(no subject)'}' ({(h.get('Date') or '')[:16]})",
                       "text": text, "date_iso": date_iso, "inbound": bool(sender) and not any(_own(a) for a in sender)})
    return blocks


# Where a reply's quoted history starts (Gmail in English and Danish, Outlook headers). A "... skrev ...:" line counts
# only with a year, a clock time or an address on it, so a sentence like "Som jeg skrev i går:" is not cut.
_REPLY_CUT_RE = re.compile(
    r"^(On .{5,200} wrote:|"
    r"(?=[^\n]*(?:\b(?:19|20)\d{2}\b|\b\d{1,2}[.:]\d{2}\b|@))(?:Den .{5,200} skrev.{0,200}:|.{0,120}\bskrev .{0,120}:\s*$)"
    r"|-{2,}\s*(Original Message|Oprindelig meddelelse)|From: .+|Fra: .+)$",
    re.MULTILINE)


def own_words(text: str) -> str:
    """The sender's own part of a mail: cut at the quoted reply and without '>' lines."""
    text = text.replace("\r\n", "\n")
    m = _REPLY_CUT_RE.search(text)
    head = text[:m.start()] if m else text
    return "\n".join(l for l in head.splitlines() if not l.lstrip().startswith(">"))


def inbox(subject: str, lines: list[str]) -> str:
    """A short note placed straight in your own inbox as unread (Gmail insert, never a send)."""
    import google_api
    msg = MIMEText("\n".join(lines[:5]), "plain", "utf-8")
    msg["From"] = C.need("author", "mail_from")
    msg["To"] = C.need("author", "mail_to")
    msg["Subject"], msg["Date"] = subject, email.utils.formatdate(localtime=True)
    body = json.dumps({"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(), "labelIds": ["INBOX", "UNREAD"]}).encode()
    req = urllib.request.Request(f"{GMAIL_API}/users/me/messages?internalDateSource=dateHeader", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {google_api.creds(gmail=True).token}",
                                          "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["id"]
