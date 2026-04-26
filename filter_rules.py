"""Port of the Swift FilterEngine + paywall + audio/video detectors.

Three things to know:

1. ``is_excluded(item, rules)`` mirrors ``FilterEngine.isExcluded(post:rules:)``
   from the Xcode app — same field/op semantics, case-insensitive everywhere.
2. ``detect_paywall(html)`` mirrors ``detectPaywall(rawHTML:)`` in
   ``FeedService.swift``.
3. ``detect_video_only(enclosure_type, description)`` mirrors
   ``detectVideoOnly(enclosureType:description:)``.

These are intentionally close to the Swift originals so behavior matches the
existing app and the rule files port without translation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


# --- User rules ---------------------------------------------------------------

@dataclass
class FilterRule:
    source: Optional[str]   # case-insensitive substring of source name; None = all
    field: str              # "title" or "content"
    op: str                 # "hasPrefix" | "contains" | "regex"
    value: str

    @classmethod
    def from_dict(cls, d: dict) -> "FilterRule":
        return cls(
            source=d.get("source"),
            field=d["field"],
            op=d["op"],
            value=d["value"],
        )


def _matches(item: dict, rule: FilterRule) -> bool:
    """Mirror of FilterEngine.matches(post:rule:)."""
    src_name = (item.get("source_name") or "").lower()
    if rule.source:
        if rule.source.lower() not in src_name:
            return False

    if rule.field == "title":
        haystack = (item.get("title") or "").lower()
    elif rule.field in ("content", "contentText"):
        haystack = (item.get("content_text") or "").lower()
    else:
        return False

    needle = rule.value.lower()
    if rule.op == "hasPrefix":
        return haystack.startswith(needle)
    if rule.op == "contains":
        return needle in haystack
    if rule.op == "regex":
        return re.search(rule.value, haystack, re.IGNORECASE) is not None
    return False


def is_excluded(item: dict, rules: Iterable[FilterRule]) -> Optional[str]:
    """Return the matching rule's value if any rule excludes the item, else None."""
    for rule in rules:
        if _matches(item, rule):
            return f"{rule.field} {rule.op} {rule.value!r}"
    return None


# --- Paywall detection --------------------------------------------------------

_PAYWALL_READMORE = re.compile(r"<a\s[^>]*>\s*Read more\s*</a>", re.IGNORECASE)
_PAYWALL_MARKERS = (
    "this post is for paid subscribers",
    "this post is for paying subscribers",
    "subscribe to keep reading",
    "keep reading with a 7-day free trial",
    "keep reading with a free trial",
    "start a 7-day free trial",
    "become a paid subscriber",
    "already a subscriber?",
    "for paid subscribers",
    "member-only",
)


def detect_paywall(raw_html: str) -> bool:
    if not raw_html:
        return False
    if _PAYWALL_READMORE.search(raw_html):
        return True
    lower = raw_html.lower()
    return any(marker in lower for marker in _PAYWALL_MARKERS)


# --- Audio / video detection --------------------------------------------------

def detect_video_only(enclosure_type: Optional[str], description: str) -> bool:
    if enclosure_type:
        t = enclosure_type.lower()
        if t.startswith("audio/") or t.startswith("video/"):
            return True
    trimmed = (description or "").strip().lower()
    if trimmed.startswith("watch now |") or trimmed.startswith("listen now |"):
        return True
    if trimmed.startswith("watch now:") or trimmed.startswith("listen now:"):
        return True
    return False
