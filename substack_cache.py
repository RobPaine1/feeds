"""Fetch *.substack.com feeds from a residential IP and commit them as a cache.

Substack (via Cloudflare) blocks requests from cloud-datacenter IPs outright,
so GitHub Actions runners get HTTP 403 for every *.substack.com/feed URL —
regardless of User-Agent or TLS fingerprint. This script is the other half of
the workaround: run it from a home machine on a schedule (launchd/cron), and
it snapshots each blocked feed's raw XML into `substack_cache/`, then commits
and pushes. `generate.py` (running in CI) falls back to these snapshots
whenever a live fetch fails.

Sources are selected automatically: every entry in sources.yaml whose feed URL
is hosted on *.substack.com. Feeds on custom domains (e.g. astralcodexten.com)
are not blocked and keep being fetched live by CI.

Usage:
    python3 substack_cache.py            # fetch + commit + push
    python3 substack_cache.py --no-push  # fetch + commit only
    python3 substack_cache.py --dry-run  # fetch only, no git at all

Requires only python3 + PyYAML, and (for pushing) git credentials.
"""
from __future__ import annotations

import gzip
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"
CACHE_DIR = ROOT / "substack_cache"
META_FILE = CACHE_DIR / "meta.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
HTTP_TIMEOUT = 20
REQUEST_RETRIES = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("substack_cache")


def load_substack_sources() -> list[dict]:
    """Entries from sources.yaml whose feed URL lives on *.substack.com."""
    data = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    feed_path = defaults.get("feed_path", "/feed")
    out = []
    for src in data.get("sources") or []:
        homepage = (src.get("homepage") or "").rstrip("/")
        feed_url = src.get("feed_url") or (homepage + feed_path if homepage else "")
        host = urlparse(feed_url).hostname or ""
        if host == "substack.com" or host.endswith(".substack.com"):
            out.append({"slug": src["slug"], "feed_url": feed_url})
    return out


def fetch(url: str) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/rss+xml, application/atom+xml, "
            "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
        ),
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_exc: Exception | None = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                body = gzip.decompress(body)
            elif "deflate" in enc:
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            return body
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_exc}")


def looks_like_feed(body: bytes) -> bool:
    head = body.lstrip()[:500]
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head


def run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True,
    )


def commit_and_push(push: bool) -> None:
    run_git("add", str(CACHE_DIR.relative_to(ROOT)))
    diff = run_git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        log.info("no cache changes to commit")
        return
    msg = "Update substack cache [skip ci]"
    cp = run_git("commit", "-m", msg)
    if cp.returncode != 0:
        log.error("git commit failed: %s", cp.stderr.strip())
        return
    if not push:
        log.info("committed (push skipped)")
        return
    # CI commits to the same branch frequently; rebase-and-retry on rejection.
    for attempt in range(3):
        cp = run_git("push")
        if cp.returncode == 0:
            log.info("pushed cache update")
            return
        log.warning("push rejected (attempt %d): %s", attempt + 1, cp.stderr.strip()[:200])
        run_git("pull", "--rebase")
    log.error("giving up on push after 3 attempts")


def main() -> int:
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args
    push = "--no-push" not in args and not dry_run

    sources = load_substack_sources()
    if not sources:
        log.info("no *.substack.com sources found — nothing to do")
        return 0
    CACHE_DIR.mkdir(exist_ok=True)

    meta: dict = {}
    if META_FILE.exists():
        try:
            meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = failed = 0
    for src in sources:
        slug, url = src["slug"], src["feed_url"]
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] %s", slug, exc)
            meta.setdefault(slug, {})["last_error"] = str(exc)[:200]
            failed += 1
            continue
        if not looks_like_feed(body):
            log.error("[%s] response doesn't look like a feed (first bytes: %r)",
                      slug, body[:80])
            meta.setdefault(slug, {})["last_error"] = "response is not XML"
            failed += 1
            continue
        (CACHE_DIR / f"{slug}.xml").write_bytes(body)
        meta[slug] = {"fetched_at": now, "bytes": len(body)}
        ok += 1
        log.info("[%s] cached %d bytes", slug, len(body))
        time.sleep(0.5)  # be polite

    META_FILE.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    log.info("done: %d cached, %d failed", ok, failed)

    if not dry_run:
        commit_and_push(push)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
