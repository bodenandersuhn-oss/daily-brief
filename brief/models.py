"""Shared data shapes for the brief pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    """One fetched story, before summarization."""

    title: str
    url: str
    outlet: str          # "WSJ", "Federal Reserve", ...
    source_name: str     # which feed/newsletter within that outlet
    section: str         # section id from feeds.yaml
    published: datetime | None
    body: str            # raw blurb from the feed or email; may be ""
    origin: str          # "rss" or "gmail"
    also_reported_by: list[str] = field(default_factory=list)

    @property
    def headline_only(self) -> bool:
        """True when we fetched a headline but no usable body text."""
        return len(self.body.strip()) < 40

    def as_source_record(self, ref: int) -> dict:
        """The shape handed to the model. Deliberately minimal."""
        return {
            "ref": ref,
            "headline": self.title,
            "outlet": self.outlet,
            "url": self.url,
            "published": self.published.isoformat() if self.published else None,
            "source_text": self.body.strip(),
            "headline_only": self.headline_only,
            "also_reported_by": self.also_reported_by,
        }
