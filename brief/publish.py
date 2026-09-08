"""Publish stage: render HTML into docs/, commit it, send the ntfy push."""

from __future__ import annotations

import html
import logging
import os
import re
import subprocess
from datetime import datetime

import httpx

from .config import CENTRAL, DOCS_DIR, REPO_ROOT, SEEN_DB, Config, env

log = logging.getLogger(__name__)

PUSH_LINE_CHARS = 88

STYLE = """
:root {
  --bg: #fbfaf8; --fg: #1b1b1a; --muted: #6b6a66; --rule: #e2e0da;
  --link: #1f4fd8; --card: #ffffff; --tag-bg: #efece5; --tag-fg: #6b6a66;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161715; --fg: #eceae5; --muted: #9b9a94; --rule: #2e302c;
    --link: #8fb0ff; --card: #1d1f1c; --tag-bg: #2b2d29; --tag-fg: #a5a49e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.6 ui-serif, Georgia, "Times New Roman", serif;
  -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
header { border-bottom: 2px solid var(--fg); padding-bottom: .75rem; margin-bottom: 2rem; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
.dateline {
  font: 500 .78rem/1.4 ui-sans-serif, system-ui, sans-serif;
  color: var(--muted); text-transform: uppercase; letter-spacing: .07em;
}
h2 {
  font: 600 .8rem/1.4 ui-sans-serif, system-ui, sans-serif;
  text-transform: uppercase; letter-spacing: .1em; color: var(--muted);
  margin: 2.5rem 0 .75rem; padding-bottom: .4rem; border-bottom: 1px solid var(--rule);
}
article { padding: .9rem 0; border-bottom: 1px solid var(--rule); }
article:last-child { border-bottom: 0; }
.headline { font-size: 1.02rem; font-weight: 600; margin: 0 0 .3rem; }
.headline a { color: var(--fg); text-decoration: none; border-bottom: 1px solid var(--link); }
.headline a:hover { color: var(--link); }
.summary { margin: .3rem 0 .4rem; }
.meta {
  font: .76rem/1.5 ui-sans-serif, system-ui, sans-serif; color: var(--muted);
}
.outlet { font-weight: 600; color: var(--fg); }
.tag {
  display: inline-block; background: var(--tag-bg); color: var(--tag-fg);
  border-radius: 3px; padding: .05rem .4rem; margin-left: .4rem;
  font: 500 .68rem/1.5 ui-sans-serif, system-ui, sans-serif;
  text-transform: uppercase; letter-spacing: .05em;
}
.empty { color: var(--muted); font-style: italic; padding: .9rem 0; }
.notice {
  background: var(--tag-bg); border-left: 3px solid var(--muted);
  padding: .7rem .9rem; margin: 1rem 0; font-size: .88rem; color: var(--muted);
}
footer {
  margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--rule);
  font: .76rem/1.6 ui-sans-serif, system-ui, sans-serif; color: var(--muted);
}
footer a { color: var(--link); }
"""


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def render_html(
    cfg: Config,
    brief: dict,
    when: datetime,
    degraded: bool = False,
    archive: list[str] | None = None,
) -> str:
    titles = cfg.section_titles
    pretty_date = when.strftime("%A, %B %-d, %Y")

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Morning Brief — {_esc(when.strftime('%Y-%m-%d'))}</title>",
        f"<style>{STYLE}</style></head><body><div class='wrap'>",
        "<header><h1>Morning Brief</h1>",
        f"<div class='dateline'>{_esc(pretty_date)} · US Central</div></header>",
    ]

    if degraded:
        parts.append(
            "<div class='notice'>The summarizer was unavailable for this run. "
            "Below are the deduplicated headlines exactly as fetched, without "
            "summaries.</div>"
        )

    for section in cfg.sections:
        entries = brief.get(section.id) or []
        parts.append(f"<h2>{_esc(titles[section.id])}</h2>")

        if not entries:
            parts.append("<p class='empty'>Nothing significant.</p>")
            continue

        for entry in entries:
            item = entry["item"]
            summary = entry.get("summary", "")
            parts.append("<article>")
            parts.append(
                f"<p class='headline'><a href='{_esc(item.url)}' "
                f"rel='noopener noreferrer'>{_esc(item.title)}</a></p>"
            )
            if summary:
                parts.append(f"<p class='summary'>{_esc(summary)}</p>")

            meta = [f"<span class='outlet'>{_esc(item.outlet)}</span>"]
            if item.source_name and item.source_name != item.outlet:
                meta.append(_esc(item.source_name))
            if item.published:
                meta.append(
                    item.published.astimezone(CENTRAL).strftime("%b %-d, %-I:%M %p CT")
                )
            if item.also_reported_by:
                others = sorted(set(item.also_reported_by))[:4]
                meta.append("also: " + _esc(", ".join(others)))

            line = " · ".join(meta)
            # The tag means "no body text was fetched" -- not "no summary".
            # In degraded runs nothing has a summary, and the banner says so.
            if item.headline_only:
                line += "<span class='tag'>headline only</span>"
            parts.append(f"<p class='meta'>{line}</p></article>")

    parts.append(
        "<footer>Assembled from public RSS feeds and subscribed newsletter "
        "email. Summaries are written by Claude from fetched text only; items "
        "without body text are marked <em>headline only</em>. Follow the link "
        "for the full article at its source."
    )
    if archive:
        links = " · ".join(
            f"<a href='{_esc(name)}'>{_esc(name.replace('.html', ''))}</a>"
            for name in archive[:14]
        )
        parts.append(f"<br><br>Recent: {links}")
    parts.append("</footer></div></body></html>")

    return "\n".join(parts)


def write_pages(cfg: Config, brief: dict, when: datetime, degraded: bool = False) -> str:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    slug = when.strftime("%Y-%m-%d") + ".html"

    (DOCS_DIR / slug).write_text(
        render_html(cfg, brief, when, degraded), encoding="utf-8"
    )

    archive = sorted(
        (p.name for p in DOCS_DIR.glob("20*-*-*.html")), reverse=True
    )
    (DOCS_DIR / "index.html").write_text(
        render_html(cfg, brief, when, degraded, archive=archive), encoding="utf-8"
    )
    # Stop GitHub Pages running the output through Jekyll.
    (DOCS_DIR / ".nojekyll").touch()

    log.info("Wrote docs/%s and docs/index.html", slug)
    return slug


# --------------------------------------------------------------------------
# Push
# --------------------------------------------------------------------------

def pages_url(slug: str) -> str:
    """The GitHub Pages URL for today's brief."""
    override = env("PAGES_URL")
    if override:
        return f"{override.rstrip('/')}/{slug}"
    repository = env("GITHUB_REPOSITORY")  # "owner/repo", set by Actions
    if repository and "/" in repository:
        owner, repo = repository.split("/", 1)
        return f"https://{owner}.github.io/{repo}/{slug}"
    return ""


def _truncate(text: str, limit: int = PUSH_LINE_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def push_body(cfg: Config, brief: dict) -> str:
    """One line per section: the top item, truncated to fit."""
    titles = cfg.section_titles
    lines = []
    for section in cfg.sections:
        entries = brief.get(section.id) or []
        label = titles[section.id]
        if not entries:
            lines.append(f"{label}: nothing significant")
        else:
            lines.append(f"{label}: {_truncate(entries[0]['item'].title)}")
    return "\n".join(lines)


def send_push(cfg: Config, brief: dict, when: datetime, slug: str) -> bool:
    topic = env("NTFY_TOPIC")
    if not topic:
        log.warning("NTFY_TOPIC is not set — skipping push")
        return False

    url = pages_url(slug)
    headers = {
        "Title": f"Morning Brief — {when.strftime('%b %-d')}",
        "Priority": "default",
        "Tags": "newspaper",
    }
    if url:
        headers["Click"] = url

    try:
        response = httpx.post(
            f"https://ntfy.sh/{topic}",
            content=push_body(cfg, brief).encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
    except Exception as exc:
        log.error("ntfy push failed: %s", exc)
        return False

    log.info("Push sent to ntfy.sh/%s (click -> %s)", topic, url or "no URL")
    return True


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )


def commit_and_push(slug: str) -> bool:
    """Commit the rendered page and the dedupe database, then push.

    The seen-database is committed on purpose: the Actions runner is
    ephemeral, so without it in the repo, dedupe would forget everything
    between runs and every brief would repeat yesterday's stories.
    """
    if _git("rev-parse", "--git-dir").returncode != 0:
        log.warning("Not a git repository — skipping commit")
        return False

    if os.environ.get("GITHUB_ACTIONS") == "true":
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email",
             "41898282+github-actions[bot]@users.noreply.github.com")

    paths = [str(DOCS_DIR.relative_to(REPO_ROOT))]
    if SEEN_DB.exists():
        paths.append(str(SEEN_DB.relative_to(REPO_ROOT)))

    _git("add", "-A", *paths)
    if not _git("diff", "--cached", "--quiet").returncode:
        log.info("Nothing to commit")
        return True

    result = _git("commit", "-m", f"Brief for {slug.replace('.html', '')}")
    if result.returncode != 0:
        log.error("git commit failed: %s", result.stderr.strip())
        return False

    result = _git("push")
    if result.returncode != 0:
        log.error("git push failed: %s", result.stderr.strip())
        return False

    log.info("Committed and pushed")
    return True


def publish(
    cfg: Config, brief: dict, when: datetime, degraded: bool = False
) -> str:
    slug = write_pages(cfg, brief, when, degraded)
    commit_and_push(slug)
    send_push(cfg, brief, when, slug)
    return slug
