"""Entry point: python -m brief.run [--dry-run]

Pipeline: fetch -> dedupe -> summarize -> publish.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import publish as publish_mod
from . import summarize as summarize_mod
from .config import CENTRAL, load_config
from .dedupe import SeenStore, dedupe
from .fetch import fetch_all

log = logging.getLogger("brief")


def print_brief(cfg, brief: dict, when: datetime, degraded: bool) -> None:
    titles = cfg.section_titles
    bar = "=" * 72
    print(f"\n{bar}\nMORNING BRIEF — {when.strftime('%A, %B %d, %Y')} (US Central)")
    if degraded:
        print("!! summarizer unavailable — raw deduplicated headlines only")
    print(bar)

    for section in cfg.sections:
        entries = brief.get(section.id) or []
        print(f"\n{titles[section.id].upper()}\n{'-' * len(titles[section.id])}")
        if not entries:
            print("  Nothing significant.")
            continue
        for n, entry in enumerate(entries, 1):
            item = entry["item"]
            summary = entry.get("summary", "")
            tag = "  [headline only]" if item.headline_only else ""
            print(f"\n  {n}. {item.title}{tag}")
            if summary:
                print(f"     {summary}")
            meta = item.outlet
            if item.also_reported_by:
                meta += f" (also: {', '.join(sorted(set(item.also_reported_by))[:4])})"
            print(f"     {meta}")
            print(f"     {item.url}")

    print(f"\n{bar}")
    print("PUSH NOTIFICATION PREVIEW")
    print(f"  Title: Morning Brief — {when.strftime('%b %d')}")
    for line in publish_mod.push_body(cfg, brief).splitlines():
        print(f"  {line}")
    print(f"{bar}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brief", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="fetch and assemble, print to stdout; no push, no commit, no page",
    )
    parser.add_argument(
        "--no-record", action="store_true",
        help="do not write to the seen-database (repeatable test runs)",
    )
    parser.add_argument(
        "--no-summarize", action="store_true",
        help="skip the Claude API call and show raw deduplicated headlines",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    cfg = load_config()
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(CENTRAL)
    cutoff = cfg.cutoff(now)
    log.info(
        "Window: %s -> %s (%.0fh)",
        cutoff.astimezone(CENTRAL).strftime("%a %d %b %H:%M"),
        local_now.strftime("%a %d %b %H:%M"),
        (now - cutoff).total_seconds() / 3600,
    )

    items = fetch_all(cfg, cutoff)

    with SeenStore() as store:
        fresh = dedupe(items, cfg, store)

        if args.no_summarize:
            brief, degraded = summarize_mod.fallback(cfg, fresh), True
        else:
            result = summarize_mod.summarize(cfg, fresh)
            if result is None:
                brief, degraded = summarize_mod.fallback(cfg, fresh), True
            else:
                brief, degraded = result, False

        if args.no_record:
            log.info("--no-record: seen-database not updated")
        else:
            # Everything that survived dedupe counts as seen. Items the model
            # did not select would fall out of the 24h window tomorrow anyway.
            added = store.record(fresh)
            store.prune()
            log.info("Recorded %d new items in the seen-database", added)

    if args.dry_run:
        print_brief(cfg, brief, local_now, degraded)
        log.info("--dry-run: nothing published, nothing pushed")
        return 0

    slug = publish_mod.publish(cfg, brief, local_now, degraded)
    log.info("Done: %s", slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
