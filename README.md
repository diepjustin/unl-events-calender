# unl-events-calender

Live at **[diepjustin.github.io/unl-events-calender](https://diepjustin.github.io/unl-events-calender/)**.

UNL events, ranked by major. Every night, a workflow pulls from two public UNL
calendars, cleans the results up, and saves them as a few files in `data/`.
`index.html` is a plain webpage that reads those files and does the ranking
in the visitor's own browser — no server, no database, no login.

## Files

| file | role |
| --- | --- |
| `index.html` | the whole page — fetches `data/events.json` and ranks it by major |
| `scripts/fetch_events.py` | the nightly fetch and rebuild, run by `.github/workflows/nightly.yml` |
| `data/majors.yaml`, `data/downweight.yaml`, `data/suppression.yaml` | the tuning knobs — see below |
| `MAINTAINING.md` | **the real documentation** — written for whoever inherits this next, no programming background assumed |

## Maintaining this

**[`MAINTAINING.md`](MAINTAINING.md) is the source of truth for this project** —
adding or fixing a major, downweighting noisy event sources, what the nightly
workflow does, and what to do when it fails (the page shows a staleness banner
after 36 hours of no successful run). Read it before changing anything here.
