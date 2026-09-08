"""Dedupe stage.

Two jobs:
  1. Collapse one story reported by six outlets into a single item.
  2. Never show the same story twice across days.

Backed by a SQLite store of canonical URLs and normalized titles. The database
lives in state/seen.db and is committed, so the history survives between
GitHub Actions runs (the runner filesystem does not).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .config import SEEN_DB
from .models import Item

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    url_hash   TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    outlet     TEXT,
    section    TEXT,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen(first_seen);
"""

# Dropped before comparing titles: too common to carry meaning.
STOPWORDS = frozenset("""
a an the and or but of to in on at for from by with as is are was were be been
being it its this that these those has have had will would could should may
says say said after before over under new amid into more than about
""".split())

# Trailing outlet branding: " - WSJ", " | Reuters", " \u2014 BBC News".
BRAND_SUFFIX = re.compile(r"\s*[-\u2013\u2014|]\s*[A-Z][\w .&']{1,28}$")

# The same actor appears abbreviated in one outlet and spelled out in another.
# Each pattern collapses to one canonical token so the two headlines match --
# and, just as importantly, so two headlines about *different* actors do not.
ENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(fed|fomc|federal reserve|the fed)\b"), "fed"),
    (re.compile(r"\b(ecb|european central bank)\b"), "ecb"),
    (re.compile(r"\b(boe|bank of england)\b"), "boe"),
    (re.compile(r"\b(boj|bank of japan)\b"), "boj"),
    (re.compile(r"\b(pboc|people'?s bank of china)\b"), "pboc"),
    (re.compile(r"\b(imf|international monetary fund)\b"), "imf"),
    (re.compile(r"\b(bls|bureau of labor statistics)\b"), "bls"),
    (re.compile(r"\b(bea|bureau of economic analysis)\b"), "bea"),
    (re.compile(r"\b(eia|energy information administration)\b"), "eia"),
    (re.compile(r"\b(ferc)\b"), "ferc"),
    (re.compile(r"\b(u\.?s\.?a?|united states|american?)\b"), "usa"),
    (re.compile(r"\b(u\.?k\.?|united kingdom|britain|british)\b"), "uk"),
    (re.compile(r"\b(euro ?zone|euro area|european union|eu)\b"), "eurozone"),
    (re.compile(r"\b(china|chinese)\b"), "china"),
    (re.compile(r"\b(japan(ese)?)\b"), "japan"),
    (re.compile(r"\b(german(y|an)?)\b"), "germany"),
    (re.compile(r"\b(france|french)\b"), "france"),
    (re.compile(r"\b(india(n)?)\b"), "india"),
    (re.compile(r"\b(microsoft|msft|azure)\b"), "microsoft"),
    (re.compile(r"\b(amazon|amzn|aws)\b"), "amazon"),
    (re.compile(r"\b(alphabet|googl?e?|deepmind)\b"), "alphabet"),
    (re.compile(r"\b(meta|facebook|instagram)\b"), "meta"),
    (re.compile(r"\b(nvidia|nvda)\b"), "nvidia"),
    (re.compile(r"\b(openai|chatgpt)\b"), "openai"),
    (re.compile(r"\b(anthropic|claude)\b"), "anthropic"),
    (re.compile(r"\b(apple|aapl)\b"), "apple"),
    (re.compile(r"\b(tesla|tsla)\b"), "tesla"),
    (re.compile(r"\b(intel|intc)\b"), "intel"),
    (re.compile(r"\b(oracle|orcl)\b"), "oracle"),
    (re.compile(r"\b(broadcom|avgo)\b"), "broadcom"),
]

ENTITY_TOKENS = frozenset(key for _, key in ENTITY_PATTERNS)

_SUFFIXES = ("ational", "ization", "iveness", "ingly", "edly", "ings", "ing",
             "ers", "er", "ed", "es", "s", "ly")


def _stem(token: str) -> str:
    """Crude suffix stripping -- enough to match cools/cooling, rate/rates."""
    if token in ENTITY_TOKENS:
        return token
    for suffix in _SUFFIXES:
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def normalize_title(title: str) -> str:
    """Lowercase, de-brand, canonicalize named actors, drop filler, stem."""
    text = BRAND_SUFFIX.sub("", title or "")
    text = text.lower()
    text = re.sub(r"[\u2018\u2019\u201c\u201d]", "", text)
    for pattern, key in ENTITY_PATTERNS:
        text = pattern.sub(f" {key} ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = {
        _stem(t) for t in text.split()
        if t not in STOPWORDS and (len(t) > 2 or t in ENTITY_TOKENS)
    }
    return " ".join(sorted(tokens))


def entities(title_norm: str) -> frozenset[str]:
    """The named actors a normalized title refers to."""
    return frozenset(t for t in title_norm.split() if t in ENTITY_TOKENS)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:32]


def _tokens(title_norm: str) -> frozenset[str]:
    return frozenset(title_norm.split())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similar(a_norm: str, b_norm: str, threshold: float) -> bool:
    """Do two normalized titles describe the same story?

    Named actors gate the comparison first: two headlines that each name a
    company or country, but never the same one, are different stories no
    matter how much boilerplate wording they share ("Microsoft opens
    datacenter in Ohio" vs "Amazon opens datacenter in Ohio").
    """
    ea, eb = entities(a_norm), entities(b_norm)
    if ea and eb and not (ea & eb):
        return False

    ta, tb = _tokens(a_norm), _tokens(b_norm)
    overlap = _jaccard(ta, tb)
    if overlap >= 0.70:
        return True
    if overlap < 0.30:            # nowhere near -- skip the costly compare
        return False
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class SeenStore:
    def __init__(self, path: Path = SEEN_DB):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def known_hashes(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT url_hash FROM seen")}

    def recent_titles(self, days: int) -> list[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT title_norm FROM seen WHERE first_seen >= ?", (since,)
            )
        ]

    def record(self, items: list[Item]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                url_hash(item.url),
                item.url,
                item.title,
                normalize_title(item.title),
                item.outlet,
                item.section,
                now,
            )
            for item in items
        ]
        cursor = self.conn.executemany(
            "INSERT OR IGNORE INTO seen "
            "(url_hash, url, title, title_norm, outlet, section, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return cursor.rowcount

    def prune(self, keep_days: int = 90) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cursor = self.conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
        self.conn.commit()
        return cursor.rowcount


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def _score(item: Item) -> tuple:
    """Which copy of a story to keep as the canonical one.

    Prefer a real blurb over a bare headline, then a longer blurb, then a
    newsletter over a wire feed (the newsletter framing is usually better).
    """
    return (
        0 if item.headline_only else 1,
        len(item.body),
        1 if item.origin == "gmail" else 0,
    )


def collapse_within_batch(items: list[Item], threshold: float) -> list[Item]:
    """Merge same-story duplicates inside today's fetch."""
    by_url: dict[str, Item] = {}
    for item in items:
        key = url_hash(item.url)
        existing = by_url.get(key)
        if existing is None:
            by_url[key] = item
        else:
            if _score(item) > _score(existing):
                item.also_reported_by = existing.also_reported_by
                by_url[key] = item
            keeper = by_url[key]
            if existing.outlet != keeper.outlet:
                keeper.also_reported_by.append(existing.outlet)

    candidates = sorted(by_url.values(), key=_score, reverse=True)

    kept: list[tuple[Item, str]] = []
    for item in candidates:
        norm = normalize_title(item.title)
        if not norm:
            continue
        match = next((k for k, k_norm in kept if similar(norm, k_norm, threshold)), None)
        if match is None:
            kept.append((item, norm))
        elif item.outlet != match.outlet and item.outlet not in match.also_reported_by:
            match.also_reported_by.append(item.outlet)

    merged = sum(1 for i, _ in kept if i.also_reported_by)
    log.info(
        "Collapsed %d fetched -> %d unique (%d merged across outlets)",
        len(items), len(kept), merged,
    )
    return [item for item, _ in kept]


def drop_already_seen(
    items: list[Item], store: SeenStore, threshold: float, window_days: int
) -> list[Item]:
    """Remove anything published in an earlier brief."""
    known = store.known_hashes()
    previous = [t for t in store.recent_titles(window_days) if t]

    # Inverted index so each new title is only compared against titles that
    # share at least one meaningful token.
    index: dict[str, list[str]] = {}
    for title in previous:
        for token in _tokens(title):
            index.setdefault(token, []).append(title)

    fresh: list[Item] = []
    by_url = by_title = 0
    for item in items:
        if url_hash(item.url) in known:
            by_url += 1
            continue

        norm = normalize_title(item.title)
        neighbours = {t for token in _tokens(norm) for t in index.get(token, ())}
        if any(similar(norm, other, threshold) for other in neighbours):
            by_title += 1
            continue

        fresh.append(item)

    log.info(
        "Dropped %d already-seen (%d by URL, %d by title) -> %d new",
        by_url + by_title, by_url, by_title, len(fresh),
    )
    return fresh


def dedupe(items: list[Item], cfg, store: SeenStore) -> list[Item]:
    threshold = cfg.settings["similarity_threshold"]
    window = cfg.settings["repeat_window_days"]
    collapsed = collapse_within_batch(items, threshold)
    return drop_already_seen(collapsed, store, threshold, window)
