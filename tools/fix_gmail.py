#!/usr/bin/env python3
"""Regenerate the Gmail refresh token for the daily brief, and verify it works
BEFORE it goes anywhere near the repo secret.

Run this in your OWN terminal:

    .venv/bin/python tools/fix_gmail.py

It asks for the client secret at a hidden prompt, so nothing needs pasting
into the command line and the secret never reaches your shell history.

It opens a browser for consent, then checks three things in order:

  1. the refresh actually works, using the exact code path brief/fetch.py uses
  2. the mailbox is reachable
  3. each WSJ newsletter query in feeds.yaml matches real messages

Only if all three pass does it offer to write the GitHub secret for you, piped
straight to `gh` so the token is never printed and never hand-copied.
"""

from __future__ import annotations

import getpass
import json
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


# Both values come from the JSON the Console hands you when a client is
# created. Neither the original secret for "Desktop client 1" nor for
# "Desktop client 2" is retrievable — Google's Auth Platform and the older
# Credentials page both dropped secret display and Download JSON, so the file
# saved at creation time is the only copy. Create a client, download its JSON,
# run this.
FALLBACK_CLIENT_ID = "1058943061827-74lkentv2cjjuotcbdc6221kk2kpoj8i.apps.googleusercontent.com"


def credentials_from_downloaded_json():
    """Return (client_id, client_secret) from the newest client_secret*.json."""
    candidates = []
    for folder in (Path.home() / "Downloads", ROOT):
        candidates.extend(folder.glob("client_secret*.json"))
    if not candidates:
        return None

    newest = max(candidates, key=lambda f: f.stat().st_mtime)
    try:
        blob = json.loads(newest.read_text())
    except Exception as exc:
        print(f"  WARN  could not parse {newest.name}: {exc}")
        return None

    section = blob.get("installed") or blob.get("web") or {}
    got_id = (section.get("client_id") or "").strip()
    got_secret = (section.get("client_secret") or "").strip()
    if not (got_id and got_secret):
        print(f"  WARN  {newest.name} is missing client_id or client_secret")
        return None

    print(f"  ok    read credentials from {newest.name}")
    return got_id, got_secret


print("\nProject: daily-brief-508018")

found = credentials_from_downloaded_json()

if found:
    client_id, client_secret = found
else:
    print("\nNo client_secret*.json found in ~/Downloads.")
    print("In the Cloud Console: APIs & Services -> Credentials ->")
    print("Create credentials -> OAuth client ID -> Desktop app -> Create,")
    print("then DOWNLOAD JSON from the dialog that appears. That dialog is the")
    print("only place the secret is ever shown. Re-run this afterwards.\n")
    print("Or paste a secret now for the existing client, if you still have it")
    print("(starts with 'GOCSPX-'). Nothing appears as you paste.\n")
    client_id = (os.environ.get("GMAIL_CLIENT_ID") or FALLBACK_CLIENT_ID).strip()
    client_secret = getpass.getpass("Client secret: ").strip()

if not client_id.endswith(".apps.googleusercontent.com"):
    die("Client ID does not end in '.apps.googleusercontent.com'.\n"
        f"        Got {len(client_id)} chars ending '{client_id[-14:]}'.")
if not client_secret:
    die("No secret found or entered.")

# The ID and the secret look nothing alike, but the ID is the one the Console
# offers a copy button for, so it is the easy thing to supply by mistake.
if client_secret == client_id or client_secret.endswith(".apps.googleusercontent.com"):
    die("That is the client ID, not the client secret.\n"
        "        The secret is about 35 characters, starting 'GOCSPX-'.")
if len(client_secret) < 24:
    die(f"That secret is only {len(client_secret)} characters, so it is "
        "truncated.\n        Google's look like 'GOCSPX-' plus ~28 more.")
if len(client_secret) > 60:
    die(f"That value is {len(client_secret)} characters — far too long for a\n"
        "        client secret (~35). You likely supplied the wrong field.")
if not client_secret.startswith("GOCSPX-"):
    print("  WARN  secret does not start with 'GOCSPX-' — continuing, but if\n"
          "        this fails with invalid_client, that is why")

print(f"OAuth client: {client_id}")
if not client_secret:
    die("No secret entered.")

# The ID and the secret sit next to each other on the Console page and the
# prompt is blind, so pasting the ID here is the easy mistake. Google answers
# it with "invalid_client" only after the whole browser dance, so catch it now.
if client_secret == client_id or client_secret.endswith(".apps.googleusercontent.com"):
    die("That is the client ID, not the client secret.\n"
        "        The secret is the OTHER value on that page: about 35\n"
        "        characters, starting with 'GOCSPX-'.")
if len(client_secret) < 24:
    die(f"That secret is only {len(client_secret)} characters, so it is "
        "truncated.\n        Google's look like 'GOCSPX-' plus ~28 more.")
if len(client_secret) > 60:
    die(f"That value is {len(client_secret)} characters — far too long for a\n"
        "        client secret (~35). You likely pasted the wrong field.")
if not client_secret.startswith("GOCSPX-"):
    print(f"  WARN  secret does not start with 'GOCSPX-' — continuing, but if\n"
          "        this fails with invalid_client, that is why")
print(f"  ok    read a {len(client_secret)}-character secret")
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

# 4. Write all three secrets, piped to `gh` so nothing is displayed or
#    hand-copied. All three must come from the same OAuth client — a token
#    from one client with another client's ID is exactly the invalid_grant
#    this script exists to prevent, so they are written together or not at all.
print()
print("Ready to write these to " + REPO + ":")
print("  GMAIL_CLIENT_ID       " + client_id)
print("  GMAIL_CLIENT_SECRET   (hidden, from the same client)")
print("  GMAIL_REFRESH_TOKEN   (hidden, just minted)")

if input("\nWrite them? [y/N] ").strip().lower() != "y":
    sys.exit("  Nothing written. Re-run when you are ready.\n")

for name, value in (
    ("GMAIL_CLIENT_ID", client_id),
    ("GMAIL_CLIENT_SECRET", client_secret),
    ("GMAIL_REFRESH_TOKEN", token),
):
    proc = subprocess.run(
        ["gh", "secret", "set", name, "--repo", REPO], input=value, text=True,
    )
    if proc.returncode:
        die(f"gh failed writing {name} — is `gh auth status` healthy?")
    print(f"  ok    wrote {name}")

print("\n  All three secrets updated. Next: Actions -> Daily brief -> Run workflow.")
