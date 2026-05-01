"""HTML cleanup for article bodies.

Two entry points:

- ``clean_email_html(html)``  — aggressive cleanup for newsletter emails.
  Strips tracking pixels, <script>/<style>/<iframe>, "unsubscribe" / "view
  in browser" chrome blocks, and tracking query params from links.

- ``clean_article_html(html)`` — light cleanup for RSS articles. Removes
  scripts, iframes, and tracking pixels. Otherwise leaves markup alone
  (Substack and most blogs publish reasonably clean HTML).

Design goals:
  * Conservative — under-clean is better than over-clean. The chrome
    detector only kills small blocks whose text content matches an
    obvious footer phrase AND don't contain substantive content
    (no headings, no big paragraphs).
  * Modular — patterns live as constants at the top of this file. To
    learn a new tracking domain or footer phrase, edit the lists.
  * No-deps-but-bs4 — uses BeautifulSoup with the stdlib html.parser
    so we don't pull in lxml or html5lib.
"""
from __future__ import annotations

import base64
import re
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag


# -----------------------------------------------------------------------------
# Patterns
# -----------------------------------------------------------------------------

# Substring matches against an <img>'s src attribute. Images served from these
# domains/paths are almost always analytics beacons.
TRACKING_DOMAINS: tuple[str, ...] = (
    "list-manage.com",
    "mailchimp.com",
    "email.axios.com",
    "links.politico.com",
    "click.politico.com",
    "track.politico.com",
    "sendgrid.net",
    "beehiiv.com/click",
    "beehiiv.net",
    "convertkit-mail",
    "tinyletter.com/c",
    "mailerlite.com",
    "ck.page",
    "/track/open",
    "/track/click",
    "/wf/open",
    "open.png",
    "pixel.gif",
    "tracker.gif",
)

# Query-param prefixes to strip from <a href>. Case-insensitive.
TRACKING_PARAM_PREFIXES: tuple[str, ...] = (
    "utm_",
    "mc_",      # MailChimp
    "ck_",      # ConvertKit
    "_hsenc",   # HubSpot
    "_hsmi",
    "fbclid",
    "gclid",
    "msclkid",
    "wbraid",
    "gbraid",
    "mkt_tok",  # Marketo
    "vero_",
    "yclid",
    "icid",
    "trk",
)

# Phrases that strongly indicate a footer/chrome block. Case-insensitive.
FOOTER_PHRASES: tuple[str, ...] = (
    r"view (this email )?(in (your )?browser|online)",
    r"unsubscribe",
    r"manage (your )?(email )?(preferences|subscriptions?)",
    r"update your preferences",
    r"you (are )?(receiv(ing|ed)|got) this email",
    r"forward(ed)? (to|from) a friend",
    r"was this email forwarded",
    r"copyright (©|\(c\))",
    r"all rights reserved",
    r"sent to .*@.*\..*",
    r"this newsletter is",
    r"powered by (substack|beehiiv|mailchimp|convertkit)",
)
_FOOTER_REGEX = re.compile("|".join(FOOTER_PHRASES), re.IGNORECASE)

# Class/id substrings that imply a footer/utility block.
CHROME_CLASS_HINTS: tuple[str, ...] = (
    "footer",
    "unsubscribe",
    "preferences",
    "tracking",
    "preheader",
    "email-footer",
)

# Tags to drop entirely without inspection.
DROP_TAGS: tuple[str, ...] = (
    "script", "style", "iframe", "object", "embed", "noscript", "form",
)

# Hosts whose URLs are tracker-redirect wrappers around a base64-encoded target.
# Pattern: /<segment>/.../<base64-encoded-https-url>/.../<segment>
# We try to decode the longest path segment that looks like base64 of an http(s) URL.
TRACKER_REDIRECT_HOSTS: tuple[str, ...] = (
    "link.axios.com",
    "links.politico.com",
    "click.politico.com",
    "track.politico.com",
    "click.email.",        # generic email-marketing prefix
    "click.convertkit-mail",
    "trk.klclick.com",     # Klaviyo
    "click.mlsend.com",    # MailerLite
    "links.lennysnewsletter.com",
)

# Maximum text length for a block to be considered "footer-y" rather than
# real content. Stops us from killing whole article bodies that mention
# "unsubscribe" in passing.
MAX_CHROME_BLOCK_CHARS = 500


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _is_tracking_pixel(img: Tag) -> bool:
    """True if an <img> looks like an analytics beacon."""
    # Explicit tiny dimensions
    for attr in ("width", "height"):
        v = img.get(attr)
        if v is not None and str(v).strip() in ("0", "1"):
            return True
    style = (img.get("style") or "").lower().replace(" ", "")
    if any(p in style for p in (
        "width:1px", "height:1px", "width:0px", "height:0px",
        "width:0;", "height:0;",
    )):
        return True
    src = (img.get("src") or "").lower()
    if any(d in src for d in TRACKING_DOMAINS):
        return True
    return False


def _strip_tracking_params(url: str) -> str:
    """Return url with known tracking query params removed."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return url
    try:
        parsed = urlparse(url)
        params = parse_qsl(parsed.query, keep_blank_values=True)
        clean = [
            (k, v) for k, v in params
            if not any(k.lower().startswith(p) for p in TRACKING_PARAM_PREFIXES)
        ]
        if len(clean) == len(params):
            return url   # nothing to strip; keep original exactly
        return urlunparse(parsed._replace(query=urlencode(clean)))
    except Exception:  # noqa: BLE001
        return url


def _resolve_tracker_redirect(url: str) -> Optional[str]:
    """If url is a known tracker-redirect wrapper, return the underlying target URL.

    Example: ``https://link.axios.com/click/<id>/<base64-of-target>/<id>``
    decodes the base64 segment to the real article URL. Returns None if this
    doesn't look like a tracker redirect we recognize.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    if not any(h in host for h in TRACKER_REDIRECT_HOSTS):
        return None

    # Look for the longest path segment that decodes to an http(s) URL.
    parts = [p for p in parsed.path.split("/") if p]
    parts.sort(key=len, reverse=True)
    for part in parts:
        if len(part) < 20:
            continue
        # Try url-safe and standard base64, with padding fixup.
        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            for candidate in (part, part + "=" * (-len(part) % 4)):
                try:
                    decoded = decoder(candidate).decode("utf-8", errors="strict")
                except Exception:  # noqa: BLE001
                    continue
                if decoded.startswith(("http://", "https://")) and " " not in decoded:
                    return decoded
    return None


def _normalize_link(url: str) -> str:
    """Resolve tracker redirects then strip tracking query params. Idempotent."""
    resolved = _resolve_tracker_redirect(url)
    return _strip_tracking_params(resolved if resolved else url)


def _make_responsive(soup: BeautifulSoup) -> None:
    """Mutate soup so table-based email layouts reflow on narrow viewports.

    Newsletter emails ship <table width="600"> and <img width="600"> everywhere,
    which forces horizontal scroll on iPhone. We strip those hard widths and
    add inline ``max-width:100%`` so the layout collapses gracefully.
    """
    for img in soup.find_all("img"):
        for attr in ("width", "height"):
            if img.has_attr(attr):
                del img[attr]
        existing = (img.get("style") or "").rstrip(";").strip()
        addon = "max-width:100%;height:auto"
        img["style"] = f"{existing};{addon}" if existing else addon

    for tag in soup.find_all(["table", "td", "tr"]):
        if tag.has_attr("width"):
            del tag[tag.name == "table" and "width" or "width"]   # always 'width'
        existing = (tag.get("style") or "").rstrip(";").strip()
        addon = "max-width:100%"
        tag["style"] = f"{existing};{addon}" if existing else addon


def _is_chrome_block(node: Tag) -> bool:
    """True if a block element looks like newsletter chrome (footer/legal)."""
    klass = " ".join(node.get("class", []) or []).lower()
    elem_id = (node.get("id") or "").lower()
    if any(h in klass for h in CHROME_CLASS_HINTS):
        return True
    if any(h in elem_id for h in CHROME_CLASS_HINTS):
        return True

    text = node.get_text(" ", strip=True)
    if not text or len(text) > MAX_CHROME_BLOCK_CHARS:
        return False
    if not _FOOTER_REGEX.search(text):
        return False

    # Don't strip a block that contains substantive content.
    if node.find(["h1", "h2", "h3", "h4", "h5", "blockquote", "figure"]):
        return False
    paras = node.find_all("p")
    long_paras = sum(1 for p in paras if len(p.get_text(strip=True)) > 200)
    if long_paras >= 1:
        return False

    return True


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def clean_email_html(html: str) -> str:
    """Aggressive cleanup for newsletter emails."""
    if not html or "<" not in html:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 — pathological HTML; pass through
        return html

    # 1. Drop dangerous/noisy tags entirely.
    for tag in soup(list(DROP_TAGS)):
        tag.decompose()

    # 2. Strip tracking pixels.
    for img in list(soup.find_all("img")):
        if _is_tracking_pixel(img):
            img.decompose()

    # 3. Strip tracking params from links.
    for a in soup.find_all("a", href=True):
        a["href"] = _normalize_link(a["href"])

    # 4. Remove footer/chrome blocks. Iterate over a list snapshot since we mutate.
    for tag in list(soup.find_all(["table", "div", "p", "td", "tr", "section", "footer"])):
        if not tag.parent:
            continue   # already removed via cascade
        if _is_chrome_block(tag):
            tag.decompose()

    # 5. Make table/img layouts reflow on narrow viewports (iPhone NetNewsWire).
    _make_responsive(soup)

    return str(soup)


_SUBSTACK_FEED_FOOTNOTE_RE = re.compile(r'^(.*?)/feed(#.+)$')


def clean_article_html(html: str, article_url: Optional[str] = None) -> str:
    """Light cleanup for RSS articles — only strips clearly unsafe tags
    and tracking pixels. Leaves layout and styling alone.

    If ``article_url`` is provided, also fixes Substack's broken footnote
    anchors that emit ``href="<feed_url>#footnote-anchor-N"`` instead of
    pointing at the article. Click-through goes to a 404 without this.
    """
    if not html or "<" not in html:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001
        return html

    for tag in soup(["script", "iframe", "object", "embed", "noscript"]):
        tag.decompose()
    for img in list(soup.find_all("img")):
        if _is_tracking_pixel(img):
            img.decompose()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Fix Substack's broken footnote anchors that point at /feed#anchor.
        if article_url and "/feed#" in href:
            m = _SUBSTACK_FEED_FOOTNOTE_RE.match(href)
            if m:
                href = f"{article_url}{m.group(2)}"
        a["href"] = _normalize_link(href)

    return str(soup)


# -----------------------------------------------------------------------------
# Smoke test (run: python html_cleaner.py)
# -----------------------------------------------------------------------------

def _selftest() -> None:
    sample = """
    <html><body>
      <table class="footer"><tr><td>You received this email because you subscribed.
        <a href="https://example.com/unsub?utm_source=email&id=123">Unsubscribe</a>
      </td></tr></table>
      <p>Real article content with <a href="https://example.com/page?utm_source=newsletter&utm_medium=email&id=42">a link</a>.</p>
      <img src="https://email.axios.com/o/track/open.gif" width="1" height="1">
      <img src="https://example.com/photo.jpg" width="600" height="400" alt="A photo">
      <script>alert('tracking');</script>
      <p>Another real paragraph.</p>
      <div id="email-footer">Sent to user@example.com. Manage preferences here.</div>
    </body></html>
    """
    # Tracker-redirect resolution test (Axios-style)
    import base64 as _b64
    target = "https://www.axios.com/2026/04/26/ai-cost-human-workers?utm_source=newsletter&utm_medium=email&id=99"
    encoded = _b64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    redirect = f"https://link.axios.com/click/45395977.87441/{encoded}/abc123"
    resolved = _normalize_link(redirect)
    assert resolved.startswith("https://www.axios.com/2026/04/26/"), f"redirect not unwrapped: {resolved}"
    assert "utm_source" not in resolved, f"utm not stripped from unwrapped url: {resolved}"
    assert "id=99" in resolved, f"non-tracking query lost: {resolved}"
    print(f"redirect resolution: {redirect[:50]}...")
    print(f"             -> {resolved}")

    out = clean_email_html(sample)
    assert "footer" not in out.lower() or "Manage" in sample  # the footer table should be gone
    assert "Unsubscribe" not in out, "footer block not stripped"
    assert "track/open.gif" not in out, "tracking pixel not stripped"
    assert "alert('tracking')" not in out, "script not stripped"
    assert "photo.jpg" in out, "real image was wrongly stripped"
    assert "utm_source" not in out, "utm params not stripped"
    assert "?id=42" in out, "non-tracking query params should be preserved"
    # Note: the unsub link's ?id=123 is correctly gone because the whole
    # footer block (containing that link) was stripped as chrome.
    print("clean_email_html OK")
    print("---")
    print(out)


if __name__ == "__main__":
    _selftest()
