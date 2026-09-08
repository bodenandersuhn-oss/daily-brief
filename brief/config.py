"""Configuration: feeds.yaml plus environment variables.

Secrets are read from the environment only. Nothing here writes a credential
to disk, and no credential belongs in a committed file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = REPO_ROOT / "feeds.yaml"
DOCS_DIR = REPO_ROOT / "docs"
STATE_DIR = REPO_ROOT / "state"
SEEN_DB = STATE_DIR / "seen.db"

CENTRAL = ZoneInfo("America/Chicago")

DEFAULTS = {
    "user_agent": "daily-brief/1.0 (+personal news aggregator)",
    "timeout_seconds": 20,
    "lookback_hours": 24,
    "monday_lookback_hours": 72,
    "max_items_per_section": 5,
    "max_items_per_feed": 40,
    "similarity_threshold": 0.72,
    "repeat_window_days": 7,
}


@dataclass
class Feed:
    outlet: str
    name: str
    url: str
    section: str


@dataclass
class Section:
    id: str
    title: str
    feeds: list[Feed]


@dataclass
class Config:
    settings: dict
    sections: list[Section]
    gmail: dict

    @property
    def section_titles(self) -> dict[str, str]:
        return {s.id: s.title for s in self.sections}

    def all_feeds(self) -> list[Feed]:
        return [f for s in self.sections for f in s.feeds]

    def cutoff(self, now: datetime | None = None) -> datetime:
        """Start of the lookback window, in UTC.

        Mondays reach back further so the weekend is not silently dropped.
        """
        now = now or datetime.now(timezone.utc)
        is_monday = now.astimezone(CENTRAL).weekday() == 0
        hours = self.settings[
            "monday_lookback_hours" if is_monday else "lookback_hours"
        ]
        return now - timedelta(hours=hours)


def load_config(path: Path | None = None) -> Config:
    raw = yaml.safe_load((path or FEEDS_FILE).read_text())

    settings = {**DEFAULTS, **(raw.get("settings") or {})}

    sections: list[Section] = []
    for entry in raw.get("sections", []):
        feeds = [
            Feed(
                outlet=f["outlet"],
                name=f.get("name", f["outlet"]),
                url=f["url"],
                section=entry["id"],
            )
            for f in (entry.get("feeds") or [])
        ]
        sections.append(Section(id=entry["id"], title=entry["title"], feeds=feeds))

    return Config(settings=settings, sections=sections, gmail=raw.get("gmail") or {})


def env(name: str, required: bool = False) -> str | None:
    value = os.environ.get(name) or None
    if required and not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            "See README.md for the full list of repo secrets."
        )
    return value
