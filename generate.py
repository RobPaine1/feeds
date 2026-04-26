"""Fetch every source in sources.yaml, apply filters, write one RSS XML per
source plus an OPML and a small index.html.

Run locally:    python generate.py
Run in CI:      same — driven from .github/workflows/build-feeds.yml
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import feedparser
import yaml
from feedgen.feed import FeedGenerator

from filter_rules import (
    FilterRule,
    detect_paywall,
    detect_video_only,
    is_excluded,
)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"
FILTERS_FILE = ROOT / "filters.yaml"
NEWSLETTERS_FILE = ROOT / "newsletters.yaml"
STATE_FILE = ROOT / "feed_state.json"   # per-source ETag / Last-Modified cache
FEEDS_DIR = ROOT / "feeds"
OPML_FILE = ROOT / "opml.xml"
INDEX_FILE = ROOT / "index.html"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
HTTP_TIMEOUT = 20  # feedparser respects socket.setdefaulttimeout; see main()
REQUEST_RETRIES = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("feeds")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def slugify(value: str) -> str:
    """Lowercase, alphanumeric + hyphens. Mirrors typical URL-slug generation."""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "feed"


def derive_feed_url(homepage: str, feed_path: str = "/feed") -> str:
    """Append feed_path to a homepage URL (Substack convention)."""
    if not homepage:
        return ""
    base = homepage.rstrip("/")
    if not feed_path.startswith("/"):
        feed_path = "/" + feed_path
    return base + feed_path


def clean_html_to_text(raw: str) -> str:
    """Mirror cleanHTML in Swift: strip tags, decode entities, collapse whitespace."""
    if not raw:
        return ""
    stripped = re.sub(r"<[^>]+>", " ", raw)
    decoded = html_lib.unescape(stripped)
    return re.sub(r"\s+", " ", decoded).strip()


def first_image_from_html(raw: str) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"""<img[^>]+src=["']([^"']+)["']""", raw, re.IGNORECASE)
    return m.group(1) if m else None


def parse_pubdate(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def entry_to_item(entry, source_name: str) -> dict:
    """Normalize a feedparser entry into the dict shape filter_rules expects."""
    title = (entry.get("title") or "Untitled").strip()
    link = entry.get("link") or ""
    pub = entry.get("published") or entry.get("updated") or ""
    pub_dt = parse_pubdate(pub) or (
        datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if getattr(entry, "published_parsed", None) else None
    )

    content_html = ""
    if "content" in entry and entry.content:
        content_html = entry.content[0].get("value") or ""
    if not content_html:
        content_html = entry.get("summary") or entry.get("description") or ""
    description_raw = entry.get("summary") or entry.get("description") or ""

    enclosure_url = None
    enclosure_type = None
    enclosures = entry.get("enclosures") or []
    if enclosures:
        e0 = enclosures[0]
        enclosure_url = e0.get("href") or e0.get("url")
        enclosure_type = e0.get("type")

    image_url = None
    if enclosure_url and enclosure_type and (
        enclosure_type.startswith("image/")
    ):
        image_url = enclosure_url
    if not image_url:
        # Fall back to media:thumbnail/media:content if feedparser captured it.
        for key in ("media_thumbnail", "media_content"):
            media = entry.get(key) or []
            if media and isinstance(media, list):
                cand = media[0].get("url")
                if cand:
                    image_url = cand
                    break
    if not image_url:
        image_url = first_image_from_html(content_html) or first_image_from_html(description_raw)

    return {
        "source_name": source_name,
        "title": title,
        "link": link,
        "pubdate": pub_dt,
        "description_html": description_raw,
        "content_html": content_html,
        "content_text": clean_html_to_text(content_html),
        "enclosure_url": enclosure_url,
        "enclosure_type": enclosure_type,
        "image_url": image_url,
        "author": entry.get("author"),
    }


def fetch_feed(url: str, etag: Optional[str] = None,
               modified: Optional[str] = None) -> feedparser.FeedParserDict:
    """Fetch with retries.

    If `etag` or `modified` are passed, feedparser sends If-None-Match and
    If-Modified-Since headers; the response will have ``status == 304`` when
    the feed hasn't changed since last fetch.
    """
    last_exc = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            kwargs: dict = {"request_headers": {"User-Agent": USER_AGENT}}
            if etag:
                kwargs["etag"] = etag
            if modified:
                kwargs["modified"] = modified
            return feedparser.parse(url, **kwargs)
        except Exception as exc:  # noqa: BLE001 — feedparser raises a wide net
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_exc}")


# -----------------------------------------------------------------------------
# Per-source pipeline
# -----------------------------------------------------------------------------

def process_source(src: dict, rules: list[FilterRule], max_items: int,
                   prev_state: dict, new_state: dict) -> tuple[int, int, int]:
    """Fetch, filter, and write the per-source RSS file. Returns (kept, dropped, errors).

    Uses ``prev_state[slug]`` for conditional GET (etag + last-modified) and writes
    the next-fetch values into ``new_state[slug]``. On HTTP 304, leaves the existing
    ``feeds/<slug>.xml`` untouched.
    """
    name = src["name"]
    slug = src["slug"]
    feed_url = src["feed_url"]
    homepage = src.get("homepage", "")
    author = src.get("author")
    category = src.get("category")

    prev = prev_state.get(slug, {}) or {}
    prev_etag = prev.get("etag")
    prev_modified = prev.get("modified")
    prev_fail_count = int(prev.get("fail_count", 0))

    log.info("[%s] fetching %s", slug, feed_url)
    try:
        parsed = fetch_feed(feed_url, etag=prev_etag, modified=prev_modified)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] fetch failed: %s", slug, exc)
        # Preserve prior etag/modified so we still try conditional GET next time;
        # bump fail_count so health surfacing can highlight chronically-broken sources.
        new_state[slug] = {
            "etag": prev_etag,
            "modified": prev_modified,
            "fail_count": prev_fail_count + 1,
            "last_error": str(exc)[:200],
        }
        return 0, 0, 1

    status = getattr(parsed, "status", None)
    if status == 304:
        log.info("[%s] not modified (304) — skipping regeneration", slug)
        new_state[slug] = {
            "etag": getattr(parsed, "etag", None) or prev_etag,
            "modified": getattr(parsed, "modified", None) or prev_modified,
            "fail_count": 0,
        }
        return 0, 0, 0

    if parsed.bozo and not parsed.entries:
        log.error("[%s] parse failed: %s", slug, parsed.bozo_exception)
        new_state[slug] = {
            "etag": prev_etag,
            "modified": prev_modified,
            "fail_count": prev_fail_count + 1,
            "last_error": f"parse: {parsed.bozo_exception}"[:200],
        }
        return 0, 0, 1

    fg = FeedGenerator()
    fg.title(name)
    # feedgen uses the LAST link's href for the channel <link> element, so put
    # the homepage (alternate) last; the self link still emits as <atom:link>.
    fg.link([
        {"href": public_feed_url(slug), "rel": "self"},
        {"href": homepage or feed_url, "rel": "alternate"},
    ])
    fg.description(f"Filtered feed for {name}")
    fg.language("en")
    if author:
        fg.author({"name": author})

    kept = 0
    dropped = 0
    skipped_reasons: dict[str, int] = {}

    for entry in parsed.entries[:max_items * 2]:  # over-fetch in case many are filtered
        item = entry_to_item(entry, name)

        if not item["link"]:
            dropped += 1
            skipped_reasons["no-link"] = skipped_reasons.get("no-link", 0) + 1
            continue

        if detect_paywall(item["content_html"]):
            dropped += 1
            skipped_reasons["paywall"] = skipped_reasons.get("paywall", 0) + 1
            continue

        if detect_video_only(item["enclosure_type"], item["description_html"]):
            dropped += 1
            skipped_reasons["video"] = skipped_reasons.get("video", 0) + 1
            continue

        rule_hit = is_excluded(item, rules)
        if rule_hit:
            dropped += 1
            skipped_reasons[f"rule:{rule_hit}"] = skipped_reasons.get(f"rule:{rule_hit}", 0) + 1
            continue

        fe = fg.add_entry()
        fe.id(item["link"])
        fe.title(item["title"])
        fe.link(href=item["link"])
        if item["pubdate"]:
            fe.pubDate(item["pubdate"])
        if item["author"]:
            fe.author({"name": item["author"]})
        elif author:
            fe.author({"name": author})
        if category:
            fe.category({"term": category})
        if item["content_html"]:
            fe.content(item["content_html"], type="CDATA")
        if item["description_html"]:
            fe.description(item["description_html"])
        if item["image_url"]:
            # As an enclosure so RSS readers show a card image.
            try:
                fe.enclosure(item["image_url"], 0, "image/jpeg")
            except Exception:  # noqa: BLE001
                pass

        kept += 1
        if kept >= max_items:
            break

    out = FEEDS_DIR / f"{slug}.xml"
    fg.rss_file(str(out), pretty=True)
    log.info(
        "[%s] kept=%d dropped=%d  reasons=%s",
        slug, kept, dropped, dict(sorted(skipped_reasons.items())) or "{}",
    )
    new_state[slug] = {
        "etag": getattr(parsed, "etag", None),
        "modified": getattr(parsed, "modified", None),
        "fail_count": 0,
    }
    return kept, dropped, 0


# -----------------------------------------------------------------------------
# Output: OPML + index.html
# -----------------------------------------------------------------------------

def public_base_url() -> str:
    """Base URL for GitHub Pages. Override with PAGES_BASE env var if needed."""
    base = os.environ.get("PAGES_BASE", "").rstrip("/")
    if base:
        return base
    return ""  # caller falls back to relative paths


def public_feed_url(slug: str) -> str:
    base = public_base_url()
    return f"{base}/feeds/{slug}.xml" if base else f"feeds/{slug}.xml"


def write_opml(sources: list[dict]) -> None:
    """Flat OPML — one <outline type="rss"/> per source, sorted by name.

    No ``dateCreated`` element on purpose: the file should change only when the
    source list does, otherwise every workflow run would commit a noop.
    """
    base = public_base_url()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        "    <title>Personal feeds</title>",
        "  </head>",
        "  <body>",
    ]
    for s in sorted(sources, key=lambda x: x["name"].lower()):
        url = f"{base}/feeds/{s['slug']}.xml" if base else f"feeds/{s['slug']}.xml"
        html_url = s.get("homepage") or url
        lines.append(
            '    <outline type="rss" '
            f'text="{xml_escape(s["name"])}" '
            f'title="{xml_escape(s["name"])}" '
            f'xmlUrl="{xml_escape(url)}" '
            f'htmlUrl="{xml_escape(html_url)}"/>'
        )
    lines += ["  </body>", "</opml>", ""]
    OPML_FILE.write_text("\n".join(lines), encoding="utf-8")


def write_index(sources: list[dict]) -> None:
    """Flat index.html — one <li> per source, sorted alphabetically."""
    base = public_base_url()
    rows = ["<ul>"]
    for s in sorted(sources, key=lambda x: x["name"].lower()):
        url = f"{base}/feeds/{s['slug']}.xml" if base else f"feeds/{s['slug']}.xml"
        rows.append(
            f'<li><a href="{xml_escape(url)}">{xml_escape(s["name"])}</a>'
            f' &middot; <a href="{xml_escape(s.get("homepage", "#"))}">homepage</a></li>'
        )
    rows.append("</ul>")
    body = "\n".join(rows)
    opml_url = f"{base}/opml.xml" if base else "opml.xml"
    INDEX_FILE.write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Personal feeds</title>
<style>
  body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  a {{ color: #0a66c2; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 1.5rem; color: #555; }}
  ul {{ padding-left: 1.2rem; }}
  .opml {{ margin: 0.5rem 0 1.5rem; padding: 0.75rem 1rem; background: #f4f4f0; border-radius: 6px; }}
</style>
</head>
<body>
<h1>Personal feeds</h1>
<p class="opml">Bulk import in NetNewsWire: <a href="{xml_escape(opml_url)}">opml.xml</a></p>
{body}
</body>
</html>
""", encoding="utf-8")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def load_sources() -> tuple[list[dict], dict]:
    raw = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    feed_path = defaults.get("feed_path", "/feed")
    sources = []
    for s in raw.get("sources") or []:
        s = dict(s)
        s.setdefault("slug", slugify(s["name"]))
        if not s.get("feed_url"):
            s["feed_url"] = derive_feed_url(s.get("homepage", ""), feed_path)
        sources.append(s)
    return sources, defaults


def load_rules() -> list[FilterRule]:
    if not FILTERS_FILE.exists():
        return []
    raw = yaml.safe_load(FILTERS_FILE.read_text(encoding="utf-8")) or []
    return [FilterRule.from_dict(r) for r in raw]


def load_newsletters() -> list[dict]:
    if not NEWSLETTERS_FILE.exists():
        return []
    raw = yaml.safe_load(NEWSLETTERS_FILE.read_text(encoding="utf-8")) or {}
    return raw.get("newsletters") or []


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s (%s) — starting fresh", STATE_FILE.name, exc)
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    import socket
    socket.setdefaulttimeout(HTTP_TIMEOUT)

    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    sources, defaults = load_sources()
    rules = load_rules()
    max_items = int(defaults.get("max_items", 50))

    prev_state = load_state()
    new_state: dict = {}

    log.info("loaded %d sources, %d rules, %d cached state entries",
             len(sources), len(rules), len(prev_state))

    total_kept = total_dropped = total_errors = total_unchanged = 0
    for s in sources:
        kept, dropped, errors = process_source(
            s, rules, max_items, prev_state, new_state,
        )
        total_kept += kept
        total_dropped += dropped
        total_errors += errors
        if kept == 0 and dropped == 0 and errors == 0:
            total_unchanged += 1

    # Email newsletters (only fires when IMAP_USER + IMAP_PASS are set in env).
    # The import is local so a missing dependency doesn't break the Substack pass.
    email_sources_processed: list[dict] = []
    try:
        from email_sources import process_email_newsletters
        email_sources_processed = process_email_newsletters(
            load_newsletters(), public_feed_url
        )
    except Exception as exc:  # noqa: BLE001
        log.error("email pipeline failed: %s", exc)

    # Combine RSS + email entries for the master OPML and landing page.
    combined = list(sources)
    for n in email_sources_processed:
        combined.append({
            "slug": n["slug"],
            "name": n["name"],
            "homepage": n.get("homepage"),
            "category": n.get("category"),
        })

    write_opml(combined)
    write_index(combined)
    save_state(new_state)

    log.info(
        "done. rss kept=%d dropped=%d errors=%d unchanged=%d  email_feeds=%d",
        total_kept, total_dropped, total_errors, total_unchanged,
        len(email_sources_processed),
    )
    # Treat any successful run as success even if individual feeds failed —
    # we don't want one flaky source to fail the whole CI run.
    return 0


if __name__ == "__main__":
    sys.exit(main())
