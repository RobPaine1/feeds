"""Fetch every source in sources.yaml, apply filters, write one RSS XML per
source plus an OPML and a small index.html.

Run locally:    python generate.py
Run in CI:      same — driven from .github/workflows/build-feeds.yml
"""
from __future__ import annotations

import gzip
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
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


def extract_channel_image(feed_meta) -> Optional[str]:
    """Best-effort channel image URL from a parsed feed's metadata.

    Tries, in order:
      1. ``<channel><image><url>`` (standard RSS publication logo)
      2. ``<itunes:image href="...">`` (podcast/show image — Substack puts the
         author photo here on podcast-enabled feeds)
      3. ``<logo>`` (Atom)
      4. ``<icon>`` (Atom favicon)
    Returns the first non-empty hit, or None.
    """
    if not feed_meta:
        return None
    img = feed_meta.get("image")
    if isinstance(img, dict):
        for key in ("url", "href", "link"):
            v = img.get(key)
            if v:
                return v
    elif isinstance(img, str) and img:
        return img
    for key in ("logo", "icon"):
        v = feed_meta.get(key)
        if v:
            return v
    return None


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


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string back into an aware UTC datetime; return None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def time_ago(dt: datetime, now: datetime) -> str:
    """Compact relative time: '2h ago', '3d ago', '4mo ago'. Past only."""
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def staleness_class(latest: Optional[datetime], now: datetime) -> str:
    if latest is None:
        return "no-data"
    days = (now - latest).days
    if days < 30:
        return "active"
    if days < 90:
        return "slow"
    return "stale"


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


def _http_get(url: str, etag: Optional[str], modified: Optional[str]) -> tuple[int, dict, bytes]:
    """Single HTTP GET with conditional-GET headers + Accept + gzip support.

    Returns ``(status, response_headers, body_bytes)``. Raises on network errors.
    A 304 response is returned as ``(304, headers, b"")``.
    """
    headers = {
        "User-Agent": USER_AGENT,
        # Crucial: tell Cloudflare/Substack we're a feed reader. Without this,
        # *.substack.com sometimes returns an HTML interstitial to cloud-IP
        # requests, which then blows up feedparser's XML parser.
        "Accept": (
            "application/rss+xml, application/atom+xml, "
            "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
        ),
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, {k.lower(): v for k, v in (e.headers or {}).items()}, b""
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e

    enc = resp_headers.get("content-encoding", "").lower()
    if "gzip" in enc:
        body = gzip.decompress(body)
    elif "deflate" in enc:
        try:
            body = zlib.decompress(body)
        except zlib.error:
            body = zlib.decompress(body, -zlib.MAX_WBITS)

    return status, resp_headers, body


def fetch_feed(url: str, etag: Optional[str] = None,
               modified: Optional[str] = None) -> feedparser.FeedParserDict:
    """Fetch + parse a feed with retries and conditional-GET support.

    Returns a feedparser dict with ``.status`` set to the HTTP code (304 means
    not-modified). On parse errors, the first 300 bytes of the raw response
    are accessible via ``parsed["raw_prefix"]`` for diagnostic logging.
    """
    last_exc = None
    status = None
    resp_headers: dict = {}
    body = b""
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            status, resp_headers, body = _http_get(url, etag, modified)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"failed to fetch {url}: {last_exc}")

    if status == 304:
        parsed = feedparser.FeedParserDict()
        parsed["status"] = 304
        parsed["entries"] = []
        parsed["bozo"] = False
        parsed["etag"] = etag
        parsed["modified"] = modified
        parsed["feed"] = feedparser.FeedParserDict()
        return parsed

    parsed = feedparser.parse(body)
    parsed["status"] = status
    parsed["raw_prefix"] = body[:300]
    parsed["content_type"] = resp_headers.get("content-type", "")
    # feedparser.parse(bytes) doesn't fill etag/modified — pull them from headers.
    if "etag" in resp_headers and not parsed.get("etag"):
        parsed["etag"] = resp_headers["etag"]
    if "last-modified" in resp_headers and not parsed.get("modified"):
        parsed["modified"] = resp_headers["last-modified"]
    return parsed


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

    # Force a non-conditional fetch in two cases:
    #   (1) Bootstrap: no latest_post yet — we need entries to compute it.
    #   (2) Recovery: prior fetch/parse failed. Otherwise the cached ETag plus
    #       a 304 response would lock us out from re-trying indefinitely.
    bootstrap = not prev.get("latest_post") or prev_fail_count > 0
    fetch_etag = None if bootstrap else prev_etag
    fetch_modified = None if bootstrap else prev_modified

    why = ""
    if not prev.get("latest_post"):
        why = "  (bootstrap)"
    elif prev_fail_count > 0:
        why = f"  (recovery from {prev_fail_count} prior failure{'s' if prev_fail_count != 1 else ''})"
    log.info("[%s] fetching %s%s", slug, feed_url, why)
    try:
        parsed = fetch_feed(feed_url, etag=fetch_etag, modified=fetch_modified)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] fetch failed: %s", slug, exc)
        # Preserve prior etag/modified so we still try conditional GET next time;
        # bump fail_count so health surfacing can highlight chronically-broken sources.
        new_state[slug] = {
            "etag": prev_etag,
            "modified": prev_modified,
            "fail_count": prev_fail_count + 1,
            "latest_post": prev.get("latest_post"),
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
            "latest_post": prev.get("latest_post"),
        }
        return 0, 0, 0

    if parsed.bozo and not parsed.entries:
        log.error("[%s] parse failed: %s", slug, parsed.bozo_exception)
        # Log enough of the response to diagnose Cloudflare interstitials,
        # rate-limit pages, and other surprises.
        ct = parsed.get("content_type", "?")
        prefix = parsed.get("raw_prefix", b"")
        log.error("[%s]   content-type: %s", slug, ct)
        log.error("[%s]   first 300 bytes: %r", slug, prefix)
        new_state[slug] = {
            "etag": prev_etag,
            "modified": prev_modified,
            "fail_count": prev_fail_count + 1,
            "latest_post": prev.get("latest_post"),
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
    # Channel-level image — NetNewsWire and other readers display this as the
    # feed's avatar/logo. Pulled from the source feed's own metadata so we
    # inherit whatever Substack/Atom feed publishes (publication logo, author
    # photo, etc.).
    channel_image = extract_channel_image(getattr(parsed, "feed", None))
    if channel_image:
        try:
            fg.image(
                url=channel_image,
                title=name,
                link=homepage or feed_url,
            )
        except Exception:  # noqa: BLE001 — feedgen sometimes rejects odd URLs
            pass

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

    # Capture the most recent pubDate across ALL parsed entries (including
    # filtered ones). A source with all items filtered is still alive — we
    # want to reflect that in the freshness indicator.
    latest_post: Optional[datetime] = None
    for entry in parsed.entries:
        for key in ("published", "updated"):
            v = entry.get(key)
            if v:
                pub = parse_pubdate(v)
                if pub and (latest_post is None or pub > latest_post):
                    latest_post = pub
                break

    new_state[slug] = {
        "etag": getattr(parsed, "etag", None),
        "modified": getattr(parsed, "modified", None),
        "fail_count": 0,
        "latest_post": (
            latest_post.isoformat() if latest_post else prev.get("latest_post")
        ),
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


def write_index(sources: list[dict], state: dict, refresh_time: datetime) -> None:
    """Index page sorted by recent activity, with per-source freshness indicator."""
    base = public_base_url()

    annotated = []
    for s in sources:
        st = state.get(s["slug"], {}) or {}
        latest = parse_iso(st.get("latest_post"))
        annotated.append((s, latest))

    # Sort: known dates desc, then alphabetical for unknowns at the bottom.
    def sort_key(pair):
        s, latest = pair
        if latest is None:
            return (1, s["name"].lower())
        return (0, -latest.timestamp())

    annotated.sort(key=sort_key)

    rows = ['<ul class="feed-list">']
    for s, latest in annotated:
        url = f"{base}/feeds/{s['slug']}.xml" if base else f"feeds/{s['slug']}.xml"
        homepage = s.get("homepage") or "#"
        klass = staleness_class(latest, refresh_time)
        ago = time_ago(latest, refresh_time) if latest else "no posts"
        rows.append(
            f'<li class="{klass}">'
            f'<a class="name" href="{xml_escape(url)}">{xml_escape(s["name"])}</a>'
            f'<span class="ago">{xml_escape(ago)}</span>'
            f'<a class="home" href="{xml_escape(homepage)}">homepage</a>'
            "</li>"
        )
    rows.append("</ul>")
    body = "\n".join(rows)

    opml_url = f"{base}/opml.xml" if base else "opml.xml"
    refresh_iso = refresh_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    refresh_human = refresh_time.strftime("%Y-%m-%d %H:%M UTC")
    feed_count = len(sources)
    active_count = sum(1 for _, l in annotated if l and (refresh_time - l).days < 30)

    INDEX_FILE.write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Personal feeds</title>
<style>
  body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  a {{ color: #0a66c2; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .stats {{ color: #666; font-size: 0.9rem; margin: 0 0 0.75rem; }}
  .stats time {{ color: #444; }}
  .opml {{ margin: 0.75rem 0 1.5rem; padding: 0.75rem 1rem; background: #f4f4f0; border-radius: 6px; }}
  .feed-list {{ list-style: none; padding: 0; margin: 0; }}
  .feed-list li {{ display: flex; align-items: baseline; gap: 0.75rem; padding: 0.45rem 0; border-bottom: 1px solid #f0f0f0; }}
  .feed-list .name {{ flex: 1; font-weight: 500; text-decoration: none; }}
  .feed-list .name:hover {{ text-decoration: underline; }}
  .feed-list .ago {{ font-size: 0.85rem; color: #888; min-width: 5.5rem; text-align: right; }}
  .feed-list .home {{ font-size: 0.85rem; color: #888; text-decoration: none; }}
  .feed-list .home:hover {{ text-decoration: underline; }}
  .feed-list li.active .ago {{ color: #2a8e2a; }}
  .feed-list li.slow .ago {{ color: #b88c00; }}
  .feed-list li.stale .ago {{ color: #c33; }}
  .feed-list li.no-data {{ opacity: 0.55; }}
</style>
</head>
<body>
<h1>Personal feeds</h1>
<p class="stats">{feed_count} feeds &middot; {active_count} active &middot; refreshed <time datetime="{refresh_iso}" id="refresh">{refresh_human}</time></p>
<p class="opml">Bulk import in NetNewsWire: <a href="{xml_escape(opml_url)}">opml.xml</a></p>
{body}
<script>
  // Append a relative-time hint based on the reader's local clock.
  (function() {{
    var t = document.getElementById('refresh');
    if (!t || !t.dateTime) return;
    var dt = new Date(t.dateTime);
    var diff = (Date.now() - dt.getTime()) / 1000;
    if (isNaN(diff) || diff < 0) return;
    var s;
    if      (diff < 90)         s = Math.round(diff) + 's ago';
    else if (diff < 3600)       s = Math.round(diff / 60) + 'm ago';
    else if (diff < 86400)      s = Math.round(diff / 3600) + 'h ago';
    else                        s = Math.round(diff / 86400) + 'd ago';
    t.textContent += ' (' + s + ')';
  }})();
</script>
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
    refresh_time = datetime.now(timezone.utc)

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
            load_newsletters(), public_feed_url, new_state,
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
    write_index(combined, new_state, refresh_time)
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
