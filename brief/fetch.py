"""Fetch stage: RSS entries and WSJ newsletter emails from the last N hours.

Every network call has a timeout and is wrapped. One dead feed logs a warning
and is skipped; it never takes the run down.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import Config, Feed, env
from .models import Item

log = logging.getLogger(__name__)

# Tracking junk that makes two copies of one URL look like two stories.
TRACKING_PARAMS = re.compile(
    r"^(utm_|mc_|pk_|ito$|ref$|ref_$|cmp$|CMP$|fbclid$|gclid$|igshid$|"
    r"mod$|reflink$|st$|__twitter_impression$|guccounter$|smid$|partner$)"
)
# Newsletter links are wrapped by trackers; the real URL often rides in one of
# these query params. If we find it, unwrap; otherwise keep the link as-is.
REDIRECT_PARAMS = ("url", "u", "target", "destination", "redirect_url", "r")

MAX_BODY_CHARS = 800


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

def clean_url(url: str) -> str:
    """Strip tracking params and unwrap redirect wrappers. Never raises."""
    if not url:
        return ""
    try:
        for _ in range(3):  # unwrap at most a few nested redirects
            parsed = urlparse(url)
            qs = parse_qs(parsed.query, keep_blank_values=False)
            inner = next(
                (
                    qs[p][0]
                    for p in REDIRECT_PARAMS
                    if p in qs and qs[p] and qs[p][0].startswith("http")
                ),
                None,
            )
            if not inner:
                break
            url = inner

        parsed = urlparse(url)
        kept = {
            k: v
            for k, v in parse_qs(parsed.query, keep_blank_values=False).items()
            if not TRACKING_PARAMS.match(k)
        }
        query = "&".join(f"{k}={v[0]}" for k, v in sorted(kept.items()))
        path = parsed.path.rstrip("/") or "/"
        return urlunparse(
            (parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, "")
        )
    except Exception:  # a malformed URL must not kill the run
        return url


def html_to_text(html: str) -> str:
    """Feed blurbs arrive as HTML. Reduce to plain text, capped."""
    if not html:
        return ""
    try:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_BODY_CHARS]


def _entry_datetime(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


# --------------------------------------------------------------------------
# RSS
# --------------------------------------------------------------------------

def fetch_feed(feed: Feed, cfg: Config, cutoff: datetime) -> list[Item]:
    """Fetch one feed. Returns [] on any failure — never raises."""
    settings = cfg.settings
    headers = {
        "User-Agent": settings["user_agent"],
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # One retry, for transient resets only. A 404 is not retried -- the feed
    # is simply gone, and hammering it would not help.
    response = None
    for attempt in (1, 2):
        try:
            response = httpx.get(
                feed.url,
                headers=headers,
                timeout=settings["timeout_seconds"],
                follow_redirects=True,
            )
            response.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            log.warning("SKIP %s / %s — %s", feed.outlet, feed.name, exc)
            return []
        except Exception as exc:
            if attempt == 1:
                log.debug("retrying %s / %s after %s", feed.outlet, feed.name, exc)
                time.sleep(1.5)
                continue
            log.warning("SKIP %s / %s — %s", feed.outlet, feed.name, exc)
            return []

    if response is None:
        return []

    try:
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        log.warning("SKIP %s / %s — unparseable: %s", feed.outlet, feed.name, exc)
        return []

    if parsed.bozo and not parsed.entries:
        log.warning(
            "SKIP %s / %s — no entries (%s)",
            feed.outlet, feed.name, getattr(parsed, "bozo_exception", "malformed"),
        )
        return []

    items: list[Item] = []
    for entry in parsed.entries[: settings["max_items_per_feed"]]:
        published = _entry_datetime(entry)
        # Undated entries are kept: the seen-database catches repeats, whereas
        # dropping them would silently lose feeds that omit pubDate.
        if published and published < cutoff:
            continue

        title = html_to_text(getattr(entry, "title", "")).strip()
        link = clean_url(getattr(entry, "link", "") or "")
        if not title or not link:
            continue

        body = ""
        for attr in ("summary", "description"):
            body = html_to_text(getattr(entry, attr, "") or "")
            if body:
                break
        if not body:
            content = getattr(entry, "content", None)
            if content:
                body = html_to_text(content[0].get("value", ""))

        # Some feeds repeat the headline as the blurb; that is not body text.
        if body and title and body[:60].lower() == title[:60].lower():
            body = body[len(title):].strip(" -–—:|")

        items.append(
            Item(
                title=title,
                url=link,
                outlet=feed.outlet,
                source_name=feed.name,
                section=feed.section,
                published=published,
                body=body,
                origin="rss",
            )
        )

    log.info("  %-22s %-28s %d item(s)", feed.outlet, feed.name, len(items))
    return items


def fetch_rss(cfg: Config, cutoff: datetime) -> list[Item]:
    feeds = cfg.all_feeds()
    log.info("Fetching %d RSS feeds...", len(feeds))
    items: list[Item] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for result in pool.map(lambda f: fetch_feed(f, cfg, cutoff), feeds):
            items.extend(result)
    return items


# --------------------------------------------------------------------------
# Gmail — WSJ newsletters
# --------------------------------------------------------------------------

class GmailAuthError(RuntimeError):
    """Gmail is configured but Google rejected the credentials."""


def _gmail_service():
    """Build a Gmail client from a refresh token. Returns None if unconfigured.

    Raises GmailAuthError if it *is* configured and the refresh is rejected —
    that is a broken pipeline, not an optional source, and must not pass as a
    warning.
    """
    client_id = env("GMAIL_CLIENT_ID")
    client_secret = env("GMAIL_CLIENT_SECRET")
    refresh_token = env("GMAIL_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        log.warning("Gmail not configured (missing GMAIL_* env vars) — skipping newsletters")
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        creds.refresh(Request())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise GmailAuthError(exc) from exc


def _decode_part(part) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _message_html(payload) -> str:
    """Walk the MIME tree and return the richest body we can find."""
    html_parts, text_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        if mime == "text/html":
            html_parts.append(_decode_part(part))
        elif mime == "text/plain":
            text_parts.append(_decode_part(part))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(html_parts) or "\n".join(text_parts)


def _extract_newsletter_items(html: str, base_url: str = "https://www.wsj.com") -> list[tuple[str, str, str]]:
    """Pull (headline, url, blurb) triples out of a newsletter email body.

    Newsletters are link-heavy: nav chrome, ads, unsubscribe footers. We keep
    anchors that look like article links with substantive anchor text, and take
    the surrounding block's remaining text as the blurb.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    skip_words = (
        "unsubscribe", "privacy policy", "cookie", "manage newsletters",
        "view in browser", "sign in", "subscribe", "contact us", "advertise",
        "download the app", "follow us", "terms of use", "customer service",
        "share this", "forward to a friend", "app store", "google play",
    )

    found: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not text or len(text) < 25 or len(text) > 240:
            continue
        low = text.lower()
        if any(word in low for word in skip_words):
            continue

        url = clean_url(urljoin(base_url, anchor["href"]))
        if not url.startswith("http") or url in seen_urls:
            continue
        seen_urls.add(url)

        # Blurb: text of the nearest block ancestor, minus the headline itself.
        blurb = ""
        node = anchor.parent
        for _ in range(4):
            if node is None:
                break
            block = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if len(block) > len(text) + 60:
                blurb = block.replace(text, " ", 1).strip(" -–—:|")
                break
            node = node.parent

        found.append((text, url, blurb[:MAX_BODY_CHARS]))

    return found


def fetch_gmail(cfg: Config, cutoff: datetime) -> list[Item]:
    gmail_cfg = cfg.gmail
    if not gmail_cfg.get("enabled"):
        return []

    service = _gmail_service()
    if service is None:
        return []

    # Gmail's `after:` takes a UNIX timestamp and is day-granular in practice,
    # so we filter precisely on the message's own internalDate below.
    after = int(cutoff.timestamp())
    items: list[Item] = []

    for newsletter in gmail_cfg.get("newsletters", []):
        name = newsletter.get("name", "WSJ newsletter")
        query = f"{newsletter['query']} after:{after}"
        section = newsletter.get("section", "us_economy")
        try:
            listing = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=5)
                .execute()
            )
        except Exception as exc:
            log.warning("SKIP Gmail %s — list failed: %s", name, exc)
            continue

        messages = listing.get("messages", []) or []
        if not messages:
            log.info("  Gmail %-28s no messages in window", name)
            continue

        count = 0
        for meta in messages:
            try:
                message = (
                    service.users()
                    .messages()
                    .get(userId="me", id=meta["id"], format="full")
                    .execute()
                )
            except Exception as exc:
                log.warning("SKIP Gmail message %s — %s", meta["id"], exc)
                continue

            try:
                received = datetime.fromtimestamp(
                    int(message.get("internalDate", "0")) / 1000, tz=timezone.utc
                )
            except (ValueError, TypeError):
                received = None
            if received and received < cutoff:
                continue

            body = _message_html(message.get("payload", {}))
            for headline, url, blurb in _extract_newsletter_items(body):
                items.append(
                    Item(
                        title=headline,
                        url=url,
                        outlet="WSJ",
                        source_name=name,
                        section=section,
                        published=received,
                        body=blurb,
                        origin="gmail",
                    )
                )
                count += 1

        log.info("  Gmail %-28s %d item(s)", name, count)

    return items


def fetch_all(cfg: Config, cutoff: datetime) -> tuple[list[Item], list[str]]:
    """Fetch everything. Returns (items, broken).

    `broken` lists sources that are configured but not working. The brief still
    publishes without them — half a brief beats none — but the caller exits
    non-zero so the run does not report success. A silent warning here is how
    the WSJ half stayed dead for a day behind a green checkmark.
    """
    items = fetch_rss(cfg, cutoff)
    broken: list[str] = []
    try:
        items.extend(fetch_gmail(cfg, cutoff))
    except GmailAuthError as exc:
        log.error("Gmail auth failed — newsletters missing from this brief: %s", exc)
        broken.append(f"Gmail auth rejected ({exc})")
    except Exception as exc:  # belt and braces: Gmail must never kill the run
        log.error("Gmail stage failed entirely: %s", exc)
        broken.append(f"Gmail stage failed ({exc})")
    log.info("Fetched %d items total", len(items))
    return items, broken
