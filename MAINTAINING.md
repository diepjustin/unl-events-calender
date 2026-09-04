# Maintaining this project

This is written for whoever inherits this after the person who built it
graduates. You should not need to be a programmer to do most of what's here.
If a step below stops matching what you actually see, that's useful
information -- the site changed and this doc is now the thing that's wrong,
not you.

## What this is, in one paragraph

Every night, a script downloads events from two public UNL calendars,
cleans them up, and saves the result as a few files in `data/`. The page
(`index.html`) is a plain webpage that reads those files and does the
ranking in the visitor's own browser -- there's no server, no database, no
login. If you can edit a text file and click a button on GitHub, you can
maintain this.

## Adding or fixing a major

Open `data/majors.yaml`. It's a plain text file with instructions written
directly in it as comments (lines starting with `#`). Follow those. In
short: copy an existing block, change the name, and list the department
names / keywords that identify that major's events.

You don't need to touch any code to do this. After editing it, either wait
for the next nightly run, or run it yourself (see "Running it yourself"
below) to see your change reflected right away.

**If the department has its own calendar** (check
`events.unl.edu/<a-guess-at-the-slug>/upcoming/` -- e.g. `/law/`,
`/engineering/`, `/psychology/`), add that slug to `unit_slugs` on the
major, and to `UNIT_SLUGS` in `scripts/fetch_events.py`. This is the
strongest signal the page has -- stronger than `org_contains`, which is
usually just whichever individual student or staff member happened to
submit that particular event, and turns over every semester. See the
`UNIT_SLUGS` comment in `fetch_events.py` and the matching comment in
`majors.yaml` for the full explanation.

## Handling a takedown request

If an organization asks to not have their event algorithmically promoted
(or, separately, if you decide an event is too sensitive to rank -- a
support group, a recovery meeting, anything where being "recommended"
could out someone), add an entry to `data/suppression.yaml`. That file has
its own instructions in its comments. The event stays on UNL's public
calendar; this page just stops ranking it, and says so, visibly, in a
banner on the page.

## Downweighting routine noise

Some events are real and legitimate but nobody's browsing this page to
find them -- internal HR onboarding, staff benefits enrollment, an
attendance-tracking placeholder someone put on the public calendar by
habit. Unlike a takedown (above), these shouldn't be pulled from ranking
entirely -- they should just sink to the bottom instead of ranking
identically to a real student event. Add an entry to `data/downweight.yaml`
(same match shape as `suppression.yaml`, plus a `penalty` -- points
subtracted from the score). The page shows the subtraction as a visible
grey chip on the event, same transparency principle as every other
scoring signal: nothing is downweighted invisibly.

This starts with three rules built from patterns actually seen in this
project's live data (HR/benefits offices, one QR attendance placeholder)
-- deliberately not a broad keyword list, since a loose match here would
quietly bury real events. See the file's own comments before loosening any
rule.

## What to do when the feed breaks

The nightly job (see below) is set up to fail loudly if something's wrong,
rather than quietly publishing broken or empty data. Two safeguards run
automatically before anything gets written:

- **A transient network failure retries itself.** `fetch()` retries up to
  3 times with backoff (2s, then 4s) before giving up, so one dropped
  connection to UNL, Engage, or a unit calendar doesn't fail the whole
  job and page you for nothing.
- **A schema check runs after parsing, before anything is written.**
  `check_schema_health()` verifies that a healthy fraction of parsed
  events still have the fields ranking depends on (organizer, location,
  host, category). This catches the case the "zero events" check can't:
  a feed that changes shape *without* going all the way to zero, e.g. a
  renamed field that silently starts coming through empty while
  everything else still "works."

If you get a failure notification from GitHub Actions anyway:

1. Look at the failed run's log (Actions tab on GitHub -> click the red X).
   `scripts/fetch_events.py` prints what it's doing at each step, so the
   log usually tells you which of the two feeds broke, or which schema
   check failed and by how much.
2. Check the feed directly in a browser:
   - UNL: `https://events.unl.edu/upcoming/?format=ics&limit=-1`
   - Engage: `https://unl.campuslabs.com/engage/events.rss`
   If either one doesn't load, or looks like an error page instead of a
   calendar file, the problem is on UNL's or Engage's end -- there's
   nothing to fix here except maybe waiting.
3. If the feed loads but looks structurally different than before (new
   fields, a changed date format, etc.), the parser in
   `scripts/fetch_events.py` needs updating to match. This is a real code
   change -- if you're not comfortable making it, this is the point to ask
   someone who is, or to open an issue describing exactly what changed.
4. **Never comment out the "zero events" check or `check_schema_health()`**
   to make a failure go away. Both exist specifically to stop a broken
   parser from silently publishing an empty or garbage event list. If
   either is firing, something upstream is actually broken.

A unit calendar (see "Adding or fixing a major" above) failing to fetch is
handled separately and does NOT fail the whole job -- `tag_events_with_units()`
logs a warning and skips that one unit's tagging for this run, since one
department's calendar being briefly down shouldn't block publishing
everything else.

Separately: the page itself shows a banner if the data it's reading is
more than 36 hours old (see `STALE_THRESHOLD_HOURS` in `index.html`). That
means the nightly job hasn't run successfully in a while, even if nobody
noticed the GitHub Actions failure. Treat that banner as a real signal.

## Why two sources, and why Engage isn't scraped

- `events.unl.edu` is UNL's own calendar system. Its ICS feed is public
  and unauthenticated.
- `unl.campuslabs.com/engage` (branded "NvolveU") is where student orgs
  post their own events. It has a real public RSS/ICS export
  (`.../engage/events.rss`, `.../engage/events.ics`) meant for exactly
  this kind of external calendar use.
- Engage *also* has a private, contract-gated API. This project
  deliberately does not use it -- Anthology (Engage's vendor) restricts
  that API to pre-approved campus integrations, and The Daily Nebraskan is
  not the campus IT department. **Do not add scraping of Engage's HTML
  pages, and do not add API-key-based Engage integration**, without
  redoing that legal check first. The public RSS/ICS feed is genuinely a
  different, sanctioned thing from the API -- don't let anyone conflate
  them when "just add more Engage data" comes up as a feature request.

## The nightly automation

`.github/workflows/unl-events-nightly.yml` runs `scripts/fetch_events.py`
once a day, and if the output changed, commits it back to the repo. That
commit triggers the normal GitHub Pages publish (same as any other push to
`main`), so the live page updates automatically within a few minutes of
the nightly job finishing. You don't need to do anything for this to keep
working, as long as the job itself doesn't start failing (see above).

## Running it yourself

You need Python 3 and to install one-time dependencies:

```
pip install -r requirements.txt
python3 scripts/fetch_events.py
```

That's the one command. It re-fetches both feeds fresh and rewrites
`data/events.json` and `data/majors.json`.
Open `index.html` through a local server (not by double-clicking it --
browsers block a plain file from loading `data/events.json` next to it)
and reload the page to see the result. `python3 -m http.server` in this
folder, then visiting `http://localhost:8000/`, works fine.

## What the data files are (all in `data/`)

- `events.json` -- the actual event list the page reads. Generated. Don't
  hand-edit it; your edits get overwritten the next time the script runs.
- `majors.yaml` -- human-editable. The major -> org/tag mapping. See above.
- `majors.json` -- generated from `majors.yaml`. Don't hand-edit.
- `suppression.yaml` -- human-editable. The suppression list. See above.
- `downweight.yaml` -- human-editable. The noise-downweighting list. See
  "Downweighting routine noise" above.

## Editorial judgment calls already made, and why

- **No audience-based scoring** ("this event is for undergrads," etc.),
  even though it was in the original plan. Neither UNL's nor Engage's
  public feed exposes that per-event without scraping each event's own
  HTML page one at a time, which this project avoids on principle (fragile,
  slow, and edges toward exactly the kind of scraping the Engage section
  above says not to do). If UNL or Engage ever add that field to their
  feeds, it'd be a reasonable scoring signal to bring back.
- **All ~128 UNL undergraduate majors are in `majors.yaml`** (source: UNL's
  own catalog at catalog.unl.edu/undergraduate/majors/, checked
  2026-09-04 -- minors and certificates were deliberately excluded, they
  aren't majors). Most entries share their college's org list via a YAML
  anchor (see the "COLLEGE ANCHORS" comment at the top of the file) rather
  than each retyping it. Only a handful of org names are marked "observed"
  against a real event -- the rest are UNL's official college/department
  names, unverified against live events, and will need retuning as you see
  how they actually perform. If UNL adds or renames a major, this file
  needs a matching update -- it won't happen automatically.
- **The CAPS well-being group is no longer suppressed.** It was originally
  excluded from ranking as a deliberate editorial choice (the reasoning is
  preserved as a comment in `data/suppression.yaml`), then put back into
  ranking like any other event at the project owner's explicit request on
  2026-09-05. The suppression mechanism itself is unchanged and has no
  active rules right now -- use it the same way if a future case calls
  for it.
- **Org matching now prefers federated unit calendars over organizer
  names where a unit slug is known** (~23 units confirmed as of
  2026-09-05, out of UNL's many colleges and departments -- see
  `UNIT_SLUGS` in `fetch_events.py`). This was driven by directly sampling
  `ORGANIZER` values across a dozen department feeds and finding the large
  majority were individual people's names, not departments -- a signal
  that quietly stops working every time that person graduates or hands
  the job to someone else. There's no public directory of unit slugs (no
  sitemap, and `events.unl.edu/robots.txt` disallows `/api/`, which was
  left alone rather than probed), so this list is necessarily incomplete
  and grows only by someone finding and adding more.
- **Accessibility was checked with axe-core** (the real tool, not just
  manual review) against the cold-start view, a fully ranked view, the
  open combobox, and the error-banner state -- zero violations across
  axe's full default ruleset (WCAG 2.1 A/AA plus its best-practice
  checks) as of 2026-09-05. One real finding came out of that pass and
  was fixed: the nameplate bar wasn't inside a landmark region, fixed by
  making it a `<header>`. If you make layout changes, especially outside
  `<main>`, it's worth re-running axe rather than assuming the page is
  still clean.
