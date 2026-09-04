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

## Handling a takedown request

If an organization asks to not have their event algorithmically promoted
(or, separately, if you decide an event is too sensitive to rank -- a
support group, a recovery meeting, anything where being "recommended"
could out someone), add an entry to `data/suppression.yaml`. That file has
its own instructions in its comments. The event stays on UNL's public
calendar; this page just stops ranking it, and says so, visibly, in a
banner on the page.

## What to do when the feed breaks

The nightly job (see below) is set up to fail loudly if something's wrong,
rather than quietly publishing broken or empty data. If you get a failure
notification from GitHub Actions:

1. Look at the failed run's log (Actions tab on GitHub -> click the red X).
   `scripts/fetch_events.py` prints what it's doing at each step, so the
   log usually tells you which of the two feeds broke and how.
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
4. **Never comment out the "zero events" check** in `fetch_events.py`
   (search for `ERROR: zero events`) to make a failure go away. That check
   exists specifically to stop a broken parser from silently publishing an
   empty or garbage event list. If it's firing, something upstream is
   actually broken.

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

That's the one command. It re-fetches both feeds fresh, rewrites
`data/events.json`, `data/majors.json`, and every file in `data/ics/`.
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
- `ics/*.ics` -- generated. Subscribable calendar files, one per major
  plus one for everything, linked from the page.

## Editorial judgment calls already made, and why

- **No audience-based scoring** ("this event is for undergrads," etc.),
  even though it was in the original plan. Neither UNL's nor Engage's
  public feed exposes that per-event without scraping each event's own
  HTML page one at a time, which this project avoids on principle (fragile,
  slow, and edges toward exactly the kind of scraping the Engage section
  above says not to do). If UNL or Engage ever add that field to their
  feeds, it'd be a reasonable scoring signal to bring back.
- **Only ~8 majors are mapped so far**, not UNL's full catalog of 150+.
  Add more as you go -- see "Adding or fixing a major" above. The file
  makes clear which entries are lightly checked vs. observed against real
  events.
- **The CAPS well-being group stays suppressed** (`data/suppression.yaml`).
  This was a deliberate choice made when this project started, not a bug.
  Read the reason in that file before changing it.
