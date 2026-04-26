# feeds

A simple personal RSS aggregator. A GitHub Action runs three times a day,
fetches each source feed in `sources.yaml`, applies the filter rules in
`filters.yaml` (and built-in paywall + audio/video filters), and writes one
clean RSS file per source under `feeds/`. GitHub Pages serves the output;
NetNewsWire (or any RSS reader) subscribes to the URLs.

## Layout

- `sources.yaml` — list of RSS sources (name, homepage, optional `feed_url`).
- `newsletters.yaml` — list of email senders (Gmail-via-IMAP) to expose as feeds.
- `filters.yaml` — exclusion rules (title/content match by source pattern).
- `filter_rules.py` — filter engine.
- `email_sources.py` — IMAP fetch + per-sender RSS for email newsletters.
- `generate.py` — orchestrator: runs both pipelines, writes OPML/index.
- `feeds/<slug>.xml` — generated per-source RSS files (committed by CI).
- `opml.xml` — bulk subscription file for RSS readers.
- `index.html` — simple landing page listing every feed.
- `unknown_senders.json` — emails received but not matched by `newsletters.yaml`.
- `.github/workflows/build-feeds.yml` — runs `generate.py` on schedule.

## Subscribing

In NetNewsWire (or any reader) on iPhone or Mac, import OPML from:

`https://<your-username>.github.io/feeds/opml.xml`

Or add individual feeds, e.g.:

`https://<your-username>.github.io/feeds/feeds/astral-codex-ten.xml`

## Schedule

The workflow runs at 14:00, 19:00, and 01:00 UTC (~7am, noon, 6pm Pacific
in winter; off by an hour when DST shifts). Adjust the cron in
`.github/workflows/build-feeds.yml` if you want different times. You can
also trigger a run manually from the Actions tab.

## Filtering

Three layers, applied in order; an item is dropped if any matches.

1. **Paywall detection** (built in). Looks for Substack paywall markers
   like a trailing `<a>Read more</a>` or marker text such as "this post
   is for paid subscribers".
2. **Audio/video detection** (built in). Drops items whose enclosure is
   `audio/*` or `video/*`, or whose description begins with "Watch now |"
   or "Listen now |".
3. **User rules** in `filters.yaml`. Each rule has:
   - `source` — case-insensitive substring of the source name; omit to
     apply to all sources.
   - `field` — `title` or `content`.
   - `op` — `hasPrefix`, `contains`, or `regex`.
   - `value` — the string or regex to match.

## Adding a source

Add a block to `sources.yaml`:

```yaml
sources:
  - slug: my-new-feed              # used in URL; lowercase, hyphens
    name: My New Feed
    author: Author Name            # optional
    category: Some Category        # optional
    homepage: https://example.substack.com/
    # feed_url: https://example.substack.com/feed   # optional override
```

If `feed_url` is omitted, it's derived from `homepage` by appending
`/feed` (Substack convention).

Commit and push. The next scheduled run will pick it up. To pull in the
new feed immediately, trigger the workflow manually from the Actions tab.

## Email newsletters

Email-only newsletters can be turned into RSS feeds by routing them through
a dedicated Gmail account. The pipeline polls Gmail over IMAP, groups
messages by sender, and emits one RSS file per sender — same shape as the
Substack feeds, same OPML, same NetNewsWire experience.

### One-time setup

1. **Create a dedicated Gmail account** (e.g., `yourname-feeds@gmail.com`).
   Use this address only for newsletter subscriptions.
2. **Enable 2-Step Verification** on it: <https://myaccount.google.com/security>.
3. **Generate an App Password** at <https://myaccount.google.com/apppasswords>.
   Pick "Mail" as the app. You'll get a 16-character password — copy it
   verbatim (no spaces).
4. **Add GitHub secrets** in this repo:
   `Settings → Secrets and variables → Actions → New repository secret`.
   - `IMAP_USER` — the full Gmail address.
   - `IMAP_PASS` — the 16-character App Password.
5. **Subscribe to newsletters** using the new Gmail, then trigger the
   workflow manually (Actions tab → Build feeds → Run workflow).

### Adding a newsletter

After the first run with subscriptions arriving, look at `unknown_senders.json`
in the repo — it lists every From-header that hit the inbox but didn't match
any entry in `newsletters.yaml`, sorted by message count, with a few sample
subjects. Use it to pick a `match` substring (a domain or address fragment
that uniquely identifies the sender), then add an entry to
`newsletters.yaml`:

```yaml
newsletters:
  - slug: stratechery
    name: Stratechery
    homepage: https://stratechery.com/
    match: stratechery.com
```

Commit, push, run the workflow. The new feed appears at
`feeds/<slug>.xml` and in `opml.xml`.

If `IMAP_USER` / `IMAP_PASS` aren't set, the email step is silently skipped
and the Substack pipeline runs as normal.

## Local testing

```
pip install -r requirements.txt
python generate.py                       # Substack feeds only
IMAP_USER=...@gmail.com IMAP_PASS=... \
  python generate.py                     # both Substack and email
```

Output lands in `feeds/` and at the repo root.
