#!/usr/bin/env python3
"""Google access for the content engine: one authorized-user token for Docs, Drive, Sheets and Gmail.

    python3 scripts/google_api.py login     # opens Google's consent page once, saves tokens/<google.token_file>
    python3 scripts/google_api.py check     # says whether the token works

`login` needs an OAuth client of type "Desktop app" from Google Cloud Console (APIs & Services > Credentials),
downloaded as JSON to tokens/<google.client_secret_file>. Enable the Docs, Drive, Sheets and Gmail APIs on that
project. The engine never sends mail: Gmail is used to read replies and to place short notes in your own inbox.
"""
import argparse
import json
import sys

import ce_config as C

SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/gmail.modify"]


def creds(gmail: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    key = "gmail_token_file" if gmail and C.get("google", "gmail_token_file") else "token_file"
    f = C.token(key)
    if not f.exists():
        sys.exit(f"No Google token at {f}: run `python3 scripts/google_api.py login` first.")
    c = Credentials.from_authorized_user_info(json.loads(f.read_text()))
    if (c.expired or not c.valid) and c.refresh_token:
        c.refresh(Request())
        f.write_text(c.to_json())
    return c


def service(api: str, version: str):
    from googleapiclient.discovery import build
    return build(api, version, credentials=creds(gmail=api == "gmail"), cache_discovery=False)


def login() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    secret = C.token("client_secret_file")
    if not secret.exists():
        sys.exit(f"Put your OAuth client JSON (Desktop app) at {secret} first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    c = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    C.TOKENS.mkdir(parents=True, exist_ok=True)
    out = C.token("token_file")
    out.write_text(c.to_json())
    out.chmod(0o600)
    print(f"saved {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["login", "check"])
    a = ap.parse_args()
    if a.cmd == "login":
        login()
    else:
        about = service("drive", "v3").about().get(fields="user(emailAddress)").execute()
        print("token works for", about["user"]["emailAddress"])
