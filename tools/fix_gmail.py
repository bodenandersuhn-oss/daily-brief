#!/usr/bin/env python3
"""Regenerate the Gmail refresh token for the daily brief, and verify it works
BEFORE it goes anywhere near the repo secret.

Run this in your OWN terminal:

    export GMAIL_CLIENT_ID='...'
    export GMAIL_CLIENT_SECRET='...'
    .venv/bin/python tools/fix_gmail.py

It opens a browser for consent, then checks three things in order:

  1. the refresh actually works, using the exact code path brief/fetch.py uses
  2. the mailbox is reachable
  3. each WSJ newsletter query in feeds.yaml matches real messages

Only if all three pass does it offer to write the GitHub secret for you, piped
straight to `gh` so the token is never printed and never hand-copied.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REPO = "bodenandersuhn-oss/daily-brief"
ROOT = Path(__file__).resolve().parent.parent


def die(msg: str) -> None:
    sys.exit(f"\n  FAIL  {msg}\n")


client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
if not client_id or not client_secret:
    die("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in your environment first.")

# A truncated or mistyped client ID fails at the consent screen with
# "OAuth client was not found" (401 invalid_client), which looks alarming but
# just means the string never reached a real client. Catch the shape here.
if not client_id.endswith(".apps.googleusercontent.com"):
    die("GMAIL_CLIENT_ID does not end in '.apps.googleusercontent.com'.\n"
        f"        Got {len(client_id)} chars ending '{client_id[-14:]}'.\n"
        "        It was truncated or mis-pasted. Copy the whole value.")
if len(client_secret) < 24:
    die(f"GMAIL_CLIENT_SECRET is only {len(client_secret)} chars — truncated.")

print(f"\nOAuth client: ...{client_id[-40:]}")
print("A browser window will open. Approve access for the Gmail account that\n"
      "receives your WSJ newsletters.\n")

flow = InstalledAppFlow.from_client_config(
    {"installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }},
    scopes=SCOPES,
)
# prompt=consent is required, or a repeat authorization returns no refresh token.
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

token = (creds.refresh_token or "").strip()
if not token:
    die("Google returned no refresh token. Revoke this app's access at\n"
        "        https://myaccount.google.com/permissions\n"
        "        and run this again.")
print(f"  ok    got a refresh token ({len(token)} chars)")

# 1. The exact path brief/fetch.py:_gmail_service() takes. This is the call
#    that has been returning invalid_grant in Actions.
print("\n  ..    refreshing the way brief/fetch.py does")
probe = Credentials(
    token=None,
    refresh_token=token,
    client_id=client_id,
    client_secret=client_secret,
    token_uri="https://oauth2.googleapis.com/token",
    scopes=SCOPES,
)
try:
    probe.refresh(Request())
except Exception as exc:
    die(f"refresh rejected: {exc}\n"
        "        The token and this client ID/secret do not belong together.")
print("  ok    refresh accepted")

# 2. Mailbox reachable.
service = build("gmail", "v1", credentials=probe, cache_discovery=False)
profile = service.users().getProfile(userId="me").execute()
print(f"  ok    mailbox: {profile['emailAddress']}")

# 3. Do the configured queries match anything? Auth can be perfect and the
#    brief still get nothing if you are not subscribed, or if WSJ changed a
#    subject line.
cfg = yaml.safe_load((ROOT / "feeds.yaml").read_text())
newsletters = cfg.get("gmail", {}).get("newsletters", [])
print(f"\n  ..    checking {len(newsletters)} newsletter queries from feeds.yaml")

total = 0
for nl in newsletters:
    q = nl["query"]
    resp = service.users().messages().list(
        userId="me", q=q, maxResults=10
    ).execute()
    n = len(resp.get("messages", []))
    total += n
    mark = "ok  " if n else "WARN"
    print(f"  {mark}  {nl['name']}: {n} message(s)")
    if not n:
        print(f"        query: {q}")

if not total:
    print("\n  WARN  Auth works, but no newsletter matched. Either you are not\n"
          "        subscribed yet, or the subject lines in feeds.yaml are stale.\n"
          "        Subscribe at wsj.com/newsletters, wait for one to arrive,\n"
          "        then re-run. The token below is still good.")

# 4. Write the secret without the token ever being displayed or hand-copied.
print()
if input(f"Write GMAIL_REFRESH_TOKEN to {REPO}? [y/N] ").strip().lower() == "y":
    proc = subprocess.run(
        ["gh", "secret", "set", "GMAIL_REFRESH_TOKEN", "--repo", REPO],
        input=token, text=True,
    )
    if proc.returncode:
        die("gh failed — is `gh auth status` healthy?")
    print("  ok    secret updated. Now: Actions -> Daily brief -> Run workflow.")
else:
    print("  Nothing written. Re-run when you are ready.")
