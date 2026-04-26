# feeds

A simple personal RSS aggregator. A GitHub Action runs three times a day,
fetches each source feed in `sources.yaml`, applies the filter rules in
`filters.yaml` (and built-in paywall + audio/video filters), and writes one
clean RSS file per source under `feeds/`. GitHub Pages serves the output;
NetNewsWire (or any RSS reader) subscribes to the URLs.

## Layout

- `sources.yaml` — list of sources (name, homepage, optional `feed_url`).
- `filters.yaml` — exclusion rules (title/content match by source pattern).
- `filter_rules.py` — filter engine.
- `generate.py` — fetcher + writer.
- `feeds/<slug>.xml` — generated per-source RSS files (committed by CI).
- `opml.xml` — bulk subscription file for RSS readers.
- `index.html` — simple landing page listing every feed.
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

## Local testing

```
pip install -r requirements.txt
python generate.py
```

Output lands in `feeds/` and at the repo root.
