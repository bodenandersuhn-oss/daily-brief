"""Summarize stage: deduped items -> structured JSON, via the Claude API.

Accuracy design notes:

* The model never emits a URL or an outlet name. It returns a `ref` integer
  pointing at an item we fetched; links and outlets are joined back on from
  our own records. A hallucinated link is therefore impossible.
* Every returned summary is validated before it reaches the page: length,
  verbatim overlap with the source, and digits that do not appear in the
  source text. A summary that fails validation is discarded and the item
  falls back to headline-only. Accuracy beats completeness.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

from .config import Config, env
from .models import Item

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You write a daily market and AI news brief. You are given items that were \
actually fetched from RSS feeds and newsletter emails in the last day.

State only what the provided source text supports. If the source is \
ambiguous, say so rather than resolving it.

Rules, in order of importance:

1. Write in your own words. NEVER copy phrases or sentences from the source \
text. Copying is a copyright problem and makes the brief unreadable.
2. Two sentences maximum per item. One is often enough.
3. Do not include any number, figure, percentage, currency amount or date \
unless that exact figure appears in the source text for that item. Never \
compute, convert, round, or estimate a figure.
4. Do not add background, context, causes, or consequences that are not in \
the source text. If you only have a headline, do not invent detail from it -- \
return an empty summary for that item instead.
5. Rank items within a section by significance to a reader following markets, \
macroeconomics, and AI infrastructure. Most significant first.
6. At most 5 items per section. Choose fewer if fewer are significant. \
Never pad a section with filler to reach five.
7. If nothing in a section is significant, return an empty item list for it.

Return one entry per section you were given, using the section ids provided."""


def build_schema(section_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": section_ids},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ref": {"type": "integer"},
                                    "summary": {"type": "string"},
                                },
                                "required": ["ref", "summary"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["id", "items"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------
# Validation — the model is not trusted
# --------------------------------------------------------------------------

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
SHINGLE = 8  # consecutive shared words that count as pasted text


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "").rstrip(".") for m in NUMBER.finditer(text)}


def verbatim_overlap(summary: str, source: str, size: int = SHINGLE) -> str | None:
    """Return a pasted run of words if the summary lifted one, else None.

    A short summary is compared using a correspondingly shorter window, so a
    one-sentence lift is caught rather than sliding under the window length.
    """
    s_words, src_words = _words(summary), _words(source)
    window = min(size, len(s_words))
    if window < 5 or len(src_words) < window:
        return None  # too little text to judge fairly
    src_shingles = {
        " ".join(src_words[i : i + window])
        for i in range(len(src_words) - window + 1)
    }
    for i in range(len(s_words) - window + 1):
        shingle = " ".join(s_words[i : i + window])
        if shingle in src_shingles:
            return shingle
    return None


def validate_summary(summary: str, item: Item) -> tuple[str, str | None]:
    """Return (clean_summary, rejection_reason). Empty summary is allowed."""
    summary = re.sub(r"\s+", " ", (summary or "").strip())
    if not summary:
        return "", None

    sentences = [s for s in SENTENCE_SPLIT.split(summary) if s.strip()]
    if len(sentences) > 2:
        summary = " ".join(sentences[:2])

    if item.headline_only:
        # Nothing but a headline was fetched, so any prose is inference.
        return "", "headline-only item was given a summary"

    pasted = verbatim_overlap(summary, item.body)
    if pasted:
        return "", f"verbatim overlap with source: {pasted!r}"

    invented = _numbers(summary) - _numbers(item.body) - _numbers(item.title)
    if invented:
        return "", f"figures absent from source: {sorted(invented)}"

    return summary, None


# --------------------------------------------------------------------------
# API call
# --------------------------------------------------------------------------

def _payload(cfg: Config, items: list[Item]) -> tuple[str, dict[int, Item], list[str]]:
    """Build the user message and the ref -> Item map."""
    limit = cfg.settings.get("max_candidates_per_section", 60)
    ref_map: dict[int, Item] = {}
    blocks: list[str] = []
    ref = 0

    for section in cfg.sections:
        in_section = [i for i in items if i.section == section.id]
        in_section.sort(
            key=lambda i: (i.published is not None, i.published or 0), reverse=True
        )
        in_section = in_section[:limit]

        records = []
        for item in in_section:
            ref += 1
            ref_map[ref] = item
            records.append(item.as_source_record(ref))

        blocks.append(
            f"## Section id: {section.id} ({section.title})\n"
            + (json.dumps(records, indent=1) if records else "[]")
        )

    message = (
        "Here are the items fetched for today's brief, grouped by section.\n"
        "`source_text` is everything we have for an item. When "
        "`headline_only` is true there is no body text at all -- return an "
        "empty summary string for that item.\n\n" + "\n\n".join(blocks)
    )
    return message, ref_map, [s.id for s in cfg.sections]


def summarize(cfg: Config, items: list[Item]) -> dict | None:
    """Return {section_id: [ {item, summary}, ... ]} or None if the API fails."""
    if not items:
        log.info("No items to summarize")
        return {section.id: [] for section in cfg.sections}

    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY is not set — cannot summarize")
        return None

    message, ref_map, section_ids = _payload(cfg, items)
    log.info("Summarizing %d items with %s", len(ref_map), MODEL)

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=3)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": build_schema(section_ids)},
            },
        )
    except anthropic.APIStatusError as exc:
        log.error("Claude API returned %s: %s", exc.status_code, exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Could not reach the Claude API: %s", exc)
        return None
    except Exception as exc:
        log.error("Claude API call failed: %s", exc)
        return None

    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("Model response was not valid JSON: %s", exc)
        return None

    usage = getattr(response, "usage", None)
    if usage:
        log.info(
            "Tokens: %s in / %s out",
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )

    return _assemble(cfg, parsed, ref_map)


def _assemble(cfg: Config, parsed: dict, ref_map: dict[int, Item]) -> dict:
    """Join model output back onto our fetched items, validating as we go."""
    cap = cfg.settings["max_items_per_section"]
    by_section = {section.id: [] for section in cfg.sections}
    used: set[int] = set()
    rejected = 0

    for entry in parsed.get("sections", []):
        section_id = entry.get("id")
        if section_id not in by_section:
            log.warning("Model returned unknown section id %r — ignored", section_id)
            continue

        for record in entry.get("items", []):
            ref = record.get("ref")
            item = ref_map.get(ref)
            if item is None:
                log.warning("Model returned unknown ref %r — dropped", ref)
                continue
            if ref in used:
                continue
            if item.section != section_id:
                log.warning(
                    "Model filed ref %s under %s but it came from %s — dropped",
                    ref, section_id, item.section,
                )
                continue
            used.add(ref)

            summary, reason = validate_summary(record.get("summary", ""), item)
            if reason:
                rejected += 1
                log.warning("Rejected summary for %r — %s", item.title[:60], reason)

            by_section[section_id].append({"item": item, "summary": summary})

    for section_id, entries in by_section.items():
        if len(entries) > cap:
            by_section[section_id] = entries[:cap]

    total = sum(len(v) for v in by_section.values())
    log.info("Brief assembled: %d items (%d summaries rejected)", total, rejected)
    return by_section


def fallback(cfg: Config, items: list[Item]) -> dict:
    """Used when the API fails: raw deduped headlines, no summaries."""
    cap = cfg.settings["max_items_per_section"]
    by_section = {}
    for section in cfg.sections:
        in_section = [i for i in items if i.section == section.id]
        in_section.sort(
            key=lambda i: (i.published is not None, i.published or 0), reverse=True
        )
        by_section[section.id] = [
            {"item": i, "summary": ""} for i in in_section[:cap]
        ]
    log.warning("Using fallback: raw headlines, no model summaries")
    return by_section
