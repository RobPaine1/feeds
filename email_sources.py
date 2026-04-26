"""Gmail IMAP -> per-sender RSS XML.

Reads the last N messages from the configured Gmail INBOX, groups them by
sender against `newsletters.yaml`, and emits one RSS file per matched sender
into the same `feeds/` directory as the Substack feeds.

Senders that don't match any entry are tallied in `unknown_senders.json`
(sender -> count + sample subjects) so you can see exactly what's hitting
the inbox and decide whether to add them to `newsletters.yaml`.

Authentication is via env vars:
    IMAP_USER  — full Gmail address
    IMAP_PASS  — Gmail App Password (16 chars, no spaces)

If either is missing, the module returns an empty list and the rest of the
pipeline continues unchanged. So Substack feeds still build even if you
haven't set up the Gmail piece yet.
"""
from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
from datetime import datetime, timezone
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape as xml_escape

from feedgen.feed import FeedGenerator


log = logging.getLogger("email")

IMAP_HOST = "imap.gmail.com"
DEFAULT_FETCH_LIMIT = 200    # how many recent messages to scan per run
MAX_ITEMS_PER_FEED = 50

ROOT = Path(__file__).resolve().parent
FEEDS_DIR = ROOT / "feeds"
UNKNOWN_FILE = ROOT / "unknown_senders.json"


# -----------------------------------------------------------------------------
# IMAP fetch
# -----------------------------------------------------------------------------

def fetch_inbox(host: str, user: str, password: str,
                mailbox: str = "INBOX",
                limit: int = DEFAULT_FETCH_LIMIT) -> list[email.message.Message]:
    """Connect, fetch the last `limit` messages, return parsed Message objects."""
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, password)
    except imaplib.IMAP4.error as e:
        raise RuntimeError(
            f"IMAP login failed: {e}. Verify IMAP_USER is the full email and "
            f"IMAP_PASS is a Gmail App Password (https://myaccount.google.com/apppasswords)."
        )
    try:
        typ, _ = M.select(mailbox, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"IMAP select {mailbox!r} failed")
        typ, data = M.search(None, "ALL")
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        nums = data[0].split()
        if not nums:
            return []
        target = nums[-limit:]
        msgs = []
        for n in target:
            typ, msg_data = M.fetch(n, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            try:
                msgs.append(email.message_from_bytes(msg_data[0][1], policy=policy.default))
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to parse message %s: %s", n, exc)
        return msgs
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


# -----------------------------------------------------------------------------
# Email -> dict
# -----------------------------------------------------------------------------

def extract_html(msg: email.message.Message) -> str:
    """Best-effort extract the HTML body; fall back to a <pre>-wrapped plain text."""
    html: Optional[str] = None
    text: Optional[str] = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            try:
                if ctype == "text/html" and html is None:
                    html = part.get_content()
                elif ctype == "text/plain" and text is None:
                    text = part.get_content()
            except Exception:  # noqa: BLE001
                continue
    else:
        ctype = msg.get_content_type()
        try:
            if ctype == "text/html":
                html = msg.get_content()
            elif ctype == "text/plain":
                text = msg.get_content()
        except Exception:  # noqa: BLE001
            pass
    if html:
        return html
    if text:
        return f"<pre>{xml_escape(text)}</pre>"
    return ""


def parse_message(msg: email.message.Message) -> dict:
    from_h = (msg.get("From") or "").strip()
    subject = (msg.get("Subject") or "(no subject)").strip()
    msg_id = (msg.get("Message-ID") or "").strip().strip("<>")
    date_h = msg.get("Date")

    pub: Optional[datetime] = None
    if date_h:
        try:
            pub = parsedate_to_datetime(date_h)
            if pub and pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pub = None
    if pub is None:
        pub = datetime.now(timezone.utc)

    return {
        "from": from_h,
        "subject": subject,
        "pub": pub,
        "msg_id": msg_id,
        "html": extract_html(msg),
    }


def find_first_url(html: str) -> Optional[str]:
    if not html:
        return None
    m = re.search(r'href=["\'](https?://[^"\']+)["\']', html)
    return m.group(1) if m else None


def find_first_image(html: str) -> Optional[str]:
    if not html:
        return None
    m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', html)
    return m.group(1) if m else None


def match_sender(from_h: str, newsletters: list[dict]) -> Optional[dict]:
    lower = (from_h or "").lower()
    for n in newsletters:
        match = (n.get("match") or "").lower()
        if match and match in lower:
            return n
    return None


# -----------------------------------------------------------------------------
# Per-sender RSS writer
# -----------------------------------------------------------------------------

def write_email_feed(newsletter: dict, items: list[dict],
                     public_feed_url_fn: Callable[[str], str]) -> None:
    fg = FeedGenerator()
    fg.title(newsletter["name"])
    fg.link([
        {"href": public_feed_url_fn(newsletter["slug"]), "rel": "self"},
        {"href": newsletter.get("homepage") or "https://example.com/", "rel": "alternate"},
    ])
    fg.description(f"Email newsletter: {newsletter['name']}")
    fg.language("en")

    items.sort(key=lambda x: x["pub"], reverse=True)
    for item in items[:MAX_ITEMS_PER_FEED]:
        fe = fg.add_entry()
        guid = item["msg_id"] or (
            f"email-{newsletter['slug']}-{item['pub'].strftime('%Y%m%dT%H%M%S')}"
        )
        fe.id(guid)
        fe.guid(guid, permalink=False)
        fe.title(item["subject"])
        link = (
            find_first_url(item["html"])
            or newsletter.get("homepage")
            or f"https://example.com/{newsletter['slug']}/{guid}"
        )
        fe.link(href=link)
        fe.pubDate(item["pub"])
        if item["html"]:
            fe.content(item["html"], type="CDATA")
            fe.description(item["subject"])
        img = find_first_image(item["html"])
        if img:
            try:
                fe.enclosure(img, 0, "image/jpeg")
            except Exception:  # noqa: BLE001
                pass

    out = FEEDS_DIR / f"{newsletter['slug']}.xml"
    fg.rss_file(str(out), pretty=True)


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------

def process_email_newsletters(newsletters_config: list[dict],
                              public_feed_url_fn: Callable[[str], str],
                              new_state: Optional[dict] = None) -> list[dict]:
    """Fetch IMAP, group by configured senders, write per-sender feeds.

    Returns the list of newsletter configs that produced at least one feed
    item (so the caller can include them in the master OPML/index). If
    ``new_state`` is provided, writes ``latest_post`` (ISO datetime of the
    newest matched message) per slug for the index page's freshness sort.
    """
    user = (os.environ.get("IMAP_USER") or "").strip()
    password = (os.environ.get("IMAP_PASS") or "").strip()
    if not user or not password:
        log.info("IMAP_USER / IMAP_PASS not set — skipping email newsletters")
        return []
    if not newsletters_config:
        log.info("no entries in newsletters.yaml — nothing to fetch")
        # Still useful to fetch and dump unknown senders so the user can see
        # what's hitting the inbox before adding entries.

    log.info("connecting to %s as %s", IMAP_HOST, user)
    try:
        msgs = fetch_inbox(IMAP_HOST, user, password)
    except Exception as exc:  # noqa: BLE001
        log.error("IMAP fetch failed: %s", exc)
        return []
    log.info("fetched %d messages from INBOX", len(msgs))

    by_slug: dict[str, list[dict]] = {n["slug"]: [] for n in newsletters_config}
    unknown: dict[str, dict] = {}

    for msg in msgs:
        try:
            parsed = parse_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping unparseable message: %s", exc)
            continue
        n = match_sender(parsed["from"], newsletters_config)
        if n:
            by_slug[n["slug"]].append(parsed)
        else:
            sender = parsed["from"] or "(unknown sender)"
            entry = unknown.setdefault(sender, {"count": 0, "subjects": []})
            entry["count"] += 1
            if len(entry["subjects"]) < 5:
                entry["subjects"].append(parsed["subject"])

    processed: list[dict] = []
    for n in newsletters_config:
        items = by_slug.get(n["slug"], [])
        if not items:
            log.info("[%s] no matching messages", n["slug"])
            continue
        write_email_feed(n, items, public_feed_url_fn)
        log.info("[%s] wrote feed with %d items", n["slug"], len(items))
        processed.append(n)
        if new_state is not None:
            latest = max(it["pub"] for it in items)
            new_state[n["slug"]] = {
                "fail_count": 0,
                "latest_post": latest.isoformat(),
            }

    # Write the unknown-senders log (sorted by count desc).
    if unknown:
        ranked = dict(sorted(unknown.items(), key=lambda kv: -kv[1]["count"]))
        UNKNOWN_FILE.write_text(json.dumps(ranked, indent=2), encoding="utf-8")
        log.info(
            "logged %d unknown senders to %s (top: %s)",
            len(ranked), UNKNOWN_FILE.name, next(iter(ranked)),
        )
    elif UNKNOWN_FILE.exists():
        # Keep file updated (empty) rather than stale.
        UNKNOWN_FILE.write_text("{}\n", encoding="utf-8")

    return processed
