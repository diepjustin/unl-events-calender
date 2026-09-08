#!/usr/bin/env python3
"""Fetch UNL campus events, normalize them, and write data/events.json.

Run it with:

    python3 scripts/fetch_events.py

It also reads data/majors.yaml, data/suppression.yaml, and
data/downweight.yaml, and writes data/majors.json (the browser can't
parse YAML, so this is the one generated file the page actually depends
on besides events.json).

Sources:
  - events.unl.edu   -- the university's own calendar system (ICS feed)
  - unl.campuslabs.com/engage -- Engage's public "Public Events" RSS feed
    (NOT the Engage API -- that one requires campus pre-approval and this
    project does not use it. See MAINTAINING.md for why that distinction
    matters.)

Both are public, unauthenticated GET endpoints. No scraping, no login.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Campus local time. Used only for grouping recurring series by
# time-of-day -- see collapse_recurrence() for why UTC is the wrong clock.
CAMPUS_TZ = ZoneInfo("America/Chicago")

UNL_ICS_URL = "https://events.unl.edu/upcoming/?format=ics&limit=-1"
ENGAGE_RSS_URL = "https://unl.campuslabs.com/engage/events.rss"
UNIT_ICS_URL_TEMPLATE = "https://events.unl.edu/{slug}/upcoming/?format=ics&limit=-1"

# How far ahead to keep events. UNL's feed returns everything out to ~2029
# because recurring events are exploded into one entry per occurrence with
# no end date on the series; without a window this file would balloon and
# mostly show noise nobody can act on yet.
WINDOW_DAYS = 60

USER_AGENT = "unl-events-demo/0.1 (student project; contact via github.com/diepjustin)"

FETCH_RETRIES = 3
FETCH_BACKOFF_SECONDS = 2  # doubles each retry: 2s, 4s


def fetch(url: str) -> bytes:
    """GET url, retrying transient failures with exponential backoff. A
    nightly job failing on a single dropped connection produces a noisy,
    misleading "the feed is broken" alert for something that would have
    succeeded a few seconds later -- see MAINTAINING.md.

    Catches OSError (not just urllib.error.URLError): a connection reset
    or aborted mid-`resp.read()` -- after urlopen() already succeeded --
    raises a bare ConnectionResetError/ConnectionAbortedError, which is an
    OSError but NOT a URLError. Confirmed the hard way: an earlier version
    of this function only caught URLError and crashed the whole job on a
    reset that happened while downloading a unit calendar, rather than at
    connection time."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if attempt < FETCH_RETRIES:
                delay = FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1))
                print(f"  fetch failed ({exc}), retrying in {delay}s "
                      f"(attempt {attempt}/{FETCH_RETRIES})", file=sys.stderr)
                time.sleep(delay)
    raise last_error  # all retries exhausted


# ---------------------------------------------------------------------------
# events.unl.edu (ICS)
# ---------------------------------------------------------------------------

def unfold_ics_lines(raw_text: str) -> list[str]:
    """RFC 5545 line unfolding: a line starting with a single space or tab
    is a continuation of the previous line. UNL's feed wraps long SUMMARY/
    DESCRIPTION fields this way, so a naive line-by-line read truncates
    real content."""
    lines: list[str] = []
    for line in raw_text.split("\r\n" if "\r\n" in raw_text else "\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def unescape_ics_text(value: str) -> str:
    # RFC 5545 only requires escaping \, ; , and newlines, but UNL's
    # generator also escapes colons (observed: "https\://...",
    # "Health Equity Grand Rounds\: ..."), so that's unescaped here too.
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\:", ":")
        .replace("\\\\", "\\")
    )


def parse_ics_datetime(value: str) -> datetime:
    # Feed only ever sends UTC (trailing Z); if that ever changes this will
    # raise instead of silently mis-parsing a local time as UTC.
    if not value.endswith("Z"):
        raise ValueError(f"expected a UTC ICS datetime (trailing Z), got: {value!r}")
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def parse_unl_ics(raw_bytes: bytes) -> list[dict]:
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = unfold_ics_lines(text)

    events = []
    current: dict | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        base_key = key.split(";")[0]

        if base_key == "ORGANIZER":
            cn_match = re.search(r"CN=([^;:]*)", key)
            current["organizer"] = unescape_ics_text(cn_match.group(1)) if cn_match else ""
        elif base_key in ("DTSTART", "DTEND"):
            current[base_key.lower()] = value
        elif base_key in ("UID", "SUMMARY", "DESCRIPTION", "LOCATION", "URL", "STATUS"):
            current[base_key.lower()] = unescape_ics_text(value)

    normalized = []
    for raw in events:
        if raw.get("status", "CONFIRMED") != "CONFIRMED":
            continue
        if "dtstart" not in raw or "uid" not in raw or "summary" not in raw:
            continue
        try:
            start = parse_ics_datetime(raw["dtstart"])
        except ValueError:
            continue
        end = None
        if "dtend" in raw:
            try:
                end = parse_ics_datetime(raw["dtend"])
            except ValueError:
                end = None

        normalized.append(
            {
                "id": f"unl:{raw['uid']}",
                "source": "unl",
                "title": raw["summary"].strip(),
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "location": raw.get("location", "").strip(),
                "org": raw.get("organizer", "").strip(),
                "category": None,
                "url": raw.get("url", "").strip(),
                "description": truncate(raw.get("description", "").strip()),
                "units": [],  # filled in by tag_events_with_units(), UNL events only
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# UNL's federated per-college/department calendars (events.unl.edu/<slug>/)
#
# events.unl.edu isn't one calendar -- each college and many departments
# have their own (confirmed live: /law/, /engineering/, /casnr/, /cba/,
# /music/, /english/, /psychology/, /art/, /history/, /chemistry/,
# /physics/, /math/, /cojmc/, /architecture/, /cehs/, /sociology/,
# /philosophy/, /economics/, /finance/, /marketing/, /management/,
# /polisci/, /agecon/ -- checked 2026-09-05). That's a far more stable
# signal than the ORGANIZER field, which is usually an individual
# student's or staff member's name and turns over every semester (checked
# by sampling ORGANIZER values across a dozen of these unit feeds -- the
# large majority were personal names, not department names).
#
# There's no public directory listing every unit slug (no sitemap, and
# events.unl.edu/robots.txt disallows /api/, which is the kind of path
# that might have one -- respected here rather than probed), so this list
# is necessarily incomplete and manually maintained. A slug returning 404
# just means that department doesn't have (or hasn't been found to have)
# its own calendar; majors.yaml falls back to org_contains/tags matching
# for those.
#
# Fetching a unit's feed doesn't turn up new events -- it's the same
# underlying events as the site-wide pull, just pre-filtered by UID. So
# this only tags events, it never adds any.
UNIT_SLUGS = [
    "law", "engineering", "casnr", "cba", "music", "english", "psychology",
    "art", "history", "chemistry", "physics", "math", "cojmc",
    "architecture", "cehs", "sociology", "philosophy", "economics",
    "finance", "marketing", "management", "polisci", "agecon",
]


def extract_uids(raw_bytes: bytes) -> set[str]:
    """Pull just the UID values out of an ICS feed, without full parsing --
    all a unit feed is used for is answering "which UIDs does this unit
    claim", so there's no need to parse SUMMARY/DTSTART/etc. again."""
    text = raw_bytes.decode("utf-8", errors="replace")
    uids = set()
    for line in unfold_ics_lines(text):
        if line.startswith("UID:"):
            uids.add(line[len("UID:"):].strip())
    return uids


def tag_events_with_units(events: list[dict]) -> None:
    """Mutates each UNL event's `units` list in place. A unit feed that
    fails to fetch is skipped with a warning rather than failing the whole
    job -- one department's calendar being briefly down shouldn't take the
    rest of the site's tagging with it."""
    uid_to_units: dict[str, list[str]] = {}
    for slug in UNIT_SLUGS:
        url = UNIT_ICS_URL_TEMPLATE.format(slug=slug)
        try:
            uids = extract_uids(fetch(url))
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            print(f"  WARNING: couldn't fetch unit calendar '{slug}' ({exc}), "
                  f"skipping its tagging this run", file=sys.stderr)
            continue
        for uid in uids:
            uid_to_units.setdefault(uid, []).append(slug)

    tagged = 0
    for ev in events:
        if ev["source"] != "unl":
            continue
        uid = ev["id"].split(":", 1)[1]
        units = uid_to_units.get(uid)
        if units:
            ev["units"] = units
            tagged += 1
    print(f"  tagged {tagged} UNL events with a federated unit "
          f"(out of {len(UNIT_SLUGS)} unit calendars checked)")


# ---------------------------------------------------------------------------
# Engage (RSS with an "events" XML namespace carrying structured fields)
# ---------------------------------------------------------------------------

def parse_engage_rss(raw_bytes: bytes) -> list[dict]:
    # defusedxml, not stdlib ElementTree: this feed is remote content, and
    # stdlib's XML parser is exploitable via crafted entities (XXE, billion
    # laughs) if the source is ever compromised or MITM'd.
    import defusedxml.ElementTree as ET

    text = raw_bytes.decode("utf-8", errors="replace")
    root = ET.fromstring(text)
    ns = {"events": "events"}

    normalized = []
    for item in root.iter("item"):
        status_el = item.find("events:status", ns)
        status = status_el.text.strip().lower() if status_el is not None and status_el.text else "confirmed"
        if status == "cancelled":
            continue

        start_el = item.find("events:start", ns)
        if start_el is None or not start_el.text:
            continue
        try:
            start = parsedate_to_datetime(start_el.text.strip())
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        end = None
        end_el = item.find("events:end", ns)
        if end_el is not None and end_el.text:
            try:
                end = parsedate_to_datetime(end_el.text.strip())
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                end = None

        guid_el = item.find("guid")
        link_el = item.find("link")
        title_el = item.find("title")
        location_el = item.find("events:location", ns)
        host_el = item.find("events:host", ns)
        category_el = item.find("category")
        desc_el = item.find("description")

        event_id = (guid_el.text or "").strip() if guid_el is not None else ""
        event_id = event_id.rsplit("/", 1)[-1] if event_id else (link_el.text or "").strip()

        description = ""
        if desc_el is not None and desc_el.text:
            m = re.search(
                r'p-description[^"]*"[^>]*>(.*?)</div>', desc_el.text, re.S
            )
            snippet = m.group(1) if m else desc_el.text
            description = html.unescape(re.sub(r"<[^>]+>", " ", snippet))
            description = re.sub(r"\s+", " ", description).strip()

        normalized.append(
            {
                "id": f"engage:{event_id}",
                "source": "engage",
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "location": (location_el.text or "").strip() if location_el is not None else "",
                "org": (host_el.text or "").strip() if host_el is not None else "",
                "category": (category_el.text or "").strip() if category_el is not None else None,
                "url": (link_el.text or "").strip() if link_el is not None else "",
                "description": truncate(description),
                "units": [],  # Engage doesn't federate by unit; `host` is already a clean org name
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# Shared post-processing: window filter, recurrence collapse, suppression
# ---------------------------------------------------------------------------

def truncate(text: str, limit: int = 320) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def normalize_title_for_grouping(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def dedupe_cross_listing(events: list[dict]) -> list[dict]:
    """The same event is sometimes listed on both events.unl.edu and Engage
    (a student org submits it to both). Same title + same exact start time,
    regardless of source, is that case -- collapse to one row instead of
    showing the same lecture twice with two different organizer labels."""
    groups: dict[tuple, list[dict]] = {}
    for ev in events:
        key = (normalize_title_for_grouping(ev["title"]), ev["start"])
        groups.setdefault(key, []).append(ev)

    deduped = []
    for group in groups.values():
        if len(group) == 1:
            deduped.append(group[0])
            continue
        group.sort(key=lambda e: len(e.get("description") or ""), reverse=True)
        head = dict(group[0])
        orgs = []
        for ev in group:
            org = (ev.get("org") or "").strip()
            if org and org not in orgs:
                orgs.append(org)
        head["org"] = " / ".join(orgs)
        # Union unit tags across the copies. Otherwise the Engage copy
        # winning on description length (Engage events never have units)
        # would overwrite the UNL copy's tag with []. Defensive: when this
        # was added on 2026-09-08 the 6 live cross-listed rows happened to
        # have no unit tag on either side, so nothing was actually being
        # lost that day -- but the code path was real.
        head["units"] = union_units(group)
        sources = sorted({ev["source"] for ev in group})
        if len(sources) > 1:
            head["cross_listed_sources"] = sources
        deduped.append(head)
    return deduped


def union_units(events: list[dict]) -> list[str]:
    units: list[str] = []
    for ev in events:
        for u in ev.get("units") or []:
            if u not in units:
                units.append(u)
    return units


def collapse_recurrence(events: list[dict]) -> list[dict]:
    """Same title + same source + same time-of-day, on different dates, is
    almost always one series (a weekly meeting, a "daily 9-5" sale, etc).
    The feeds don't give us a recurrence id to group by (see fetch job
    docstring / MAINTAINING notes), so we group on that heuristic instead
    and keep the soonest occurrence as the representative row.

    Time-of-day is taken in campus local time (CAMPUS_TZ), NOT UTC. A weekly
    9am Central meeting is 14:00Z until the clocks fall back and 15:00Z
    after, so grouping on UTC hour split every recurring series in two at
    the DST boundary -- confirmed live on 2026-09-08 with four series
    breaking at Nov 1 ("CAS Inquire", "Zine Making Workshop", ...)."""
    groups: dict[tuple, list[dict]] = {}
    for ev in events:
        start = datetime.fromisoformat(ev["start"]).astimezone(CAMPUS_TZ)
        key = (ev["source"], normalize_title_for_grouping(ev["title"]), start.hour, start.minute)
        groups.setdefault(key, []).append(ev)

    collapsed = []
    for group in groups.values():
        group.sort(key=lambda e: e["start"])
        head = dict(group[0])
        head["units"] = union_units(group)  # a later occurrence may be the one a unit feed listed
        if len(group) > 1:
            head["occurrence_count"] = len(group)
            head["other_dates"] = [e["start"] for e in group[1:6]]
        else:
            head["occurrence_count"] = 1
            head["other_dates"] = []
        collapsed.append(head)
    return collapsed


def load_suppression_rules() -> list[dict]:
    path = DATA_DIR / "suppression.yaml"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        rules = yaml.safe_load(f) or []
    return rules


def rule_matches(ev: dict, match: dict) -> bool:
    """Shared by apply_suppression() and apply_downweight() -- both use the
    same source/title_contains/org_contains match shape."""
    if match.get("source") and match["source"] != ev["source"]:
        return False
    if match.get("title_contains") and match["title_contains"].lower() not in ev["title"].lower():
        return False
    if match.get("org_contains") and match["org_contains"].lower() not in (ev.get("org") or "").lower():
        return False
    return True


def apply_suppression(events: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict]]:
    kept = []
    suppressed_notes = []
    for ev in events:
        matched_rule = next((r for r in rules if rule_matches(ev, r.get("match", {}))), None)
        if matched_rule:
            suppressed_notes.append({"id": matched_rule.get("id", "unnamed"), "reason": matched_rule["reason"].strip()})
        else:
            kept.append(ev)
    return kept, suppressed_notes


def load_downweight_rules() -> list[dict]:
    path = DATA_DIR / "downweight.yaml"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def apply_downweight(events: list[dict], rules: list[dict]) -> None:
    """Mutates each event in place: adds `noise_penalty` (int, 0 if no rule
    matched) and `noise_reason` (str or None). Unlike suppression, matching
    rules don't stop at the first hit -- penalties from multiple rules
    stack, and the page shows every reason that contributed."""
    for ev in events:
        matched = [r for r in rules if rule_matches(ev, r.get("match", {}))]
        ev["noise_penalty"] = sum(r.get("penalty", 0) for r in matched)
        ev["noise_reason"] = "; ".join(r["reason"].strip() for r in matched) or None


def convert_majors_yaml_to_json() -> dict:
    """Reads majors.yaml and writes the browser-facing majors.json.

    Two things get normalized away here so index.html's scoreEvent()
    doesn't need to know about YAML-only structure:
      - Keys starting with "_" (currently `_colleges` and `_college_units`)
        are internal anchor definitions, not real majors, and are dropped.
      - `extra_org_contains` (a major's own additions on top of its
        college's shared org list) is merged into `org_contains` so each
        major in the output has one flat org_contains list, same as before
        college anchors existed.
    """
    src = DATA_DIR / "majors.yaml"
    dst = DATA_DIR / "majors.json"
    with src.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    majors = {}
    for key, major in raw.items():
        if key.startswith("_"):
            continue
        org_contains = list(major.get("org_contains") or [])
        for extra in major.get("extra_org_contains") or []:
            if extra not in org_contains:
                org_contains.append(extra)
        majors[key] = {
            "label": major.get("label", key),
            "org_contains": org_contains,
            "unit_slugs": list(major.get("unit_slugs") or []),
            "tags": major.get("tags", []),
        }

    with dst.open("w", encoding="utf-8") as f:
        json.dump(majors, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return majors


def check_schema_health(unl_events: list[dict], engage_events: list[dict]) -> None:
    """Catches the feed changing shape *without* going to zero events --
    e.g. a renamed field that silently starts coming through empty. Exits
    loudly rather than publishing data that "parsed fine" but is
    missing most of what makes ranking work. Thresholds are deliberately
    generous (real gaps exist -- a Zoom event has no LOCATION, some
    Engage listings skip a category) so this only fires on an actual
    structural break, not routine missing-field noise."""

    def fraction_with(events: list[dict], field: str) -> float:
        if not events:
            return 1.0  # an empty list isn't this check's problem -- windowing/fetch handles that
        return sum(1 for e in events if e.get(field)) / len(events)

    checks = [
        ("UNL events with an organizer", fraction_with(unl_events, "org"), 0.5),
        ("UNL events with a location", fraction_with(unl_events, "location"), 0.3),
        ("Engage events with a host org", fraction_with(engage_events, "org"), 0.7),
        ("Engage events with a category", fraction_with(engage_events, "category"), 0.5),
    ]

    failures = [(label, actual, expected) for label, actual, expected in checks if actual < expected]
    if failures:
        print("ERROR: feed schema looks different than expected -- a field UNL or "
              "Engage always used to send is now mostly empty. This usually means "
              "the site changed its output shape and the parser in this file needs "
              "updating to match. See MAINTAINING.md.", file=sys.stderr)
        for label, actual, expected in failures:
            print(f"  {label}: {actual:.0%} (expected at least {expected:.0%})", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=WINDOW_DAYS)

    print(f"Fetching {UNL_ICS_URL}")
    unl_events = parse_unl_ics(fetch(UNL_ICS_URL))
    print(f"  parsed {len(unl_events)} confirmed UNL events (before windowing)")

    tag_events_with_units(unl_events)

    print(f"Fetching {ENGAGE_RSS_URL}")
    engage_events = parse_engage_rss(fetch(ENGAGE_RSS_URL))
    print(f"  parsed {len(engage_events)} confirmed Engage events (before windowing)")

    check_schema_health(unl_events, engage_events)

    all_events = unl_events + engage_events
    windowed = [e for e in all_events if now <= datetime.fromisoformat(e["start"]) <= window_end]
    print(f"  {len(windowed)} events within the next {WINDOW_DAYS} days")

    if not windowed:
        print("ERROR: zero events after fetch + windowing. Feed shape probably "
              "changed -- see MAINTAINING.md before assuming this is a fluke.",
              file=sys.stderr)
        sys.exit(1)

    deduped = dedupe_cross_listing(windowed)
    print(f"  {len(deduped)} events after cross-listing dedupe "
          f"({len(windowed) - len(deduped)} duplicate listings collapsed)")

    collapsed = collapse_recurrence(deduped)
    collapsed.sort(key=lambda e: e["start"])

    rules = load_suppression_rules()
    kept, suppressed_notes = apply_suppression(collapsed, rules)

    downweight_rules = load_downweight_rules()
    apply_downweight(kept, downweight_rules)
    downweighted_count = sum(1 for e in kept if e["noise_penalty"] > 0)
    print(f"  {downweighted_count} events downweighted as routine/administrative noise")

    payload = {
        "generated_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "sources": {
            "unl": {"raw_count": len(unl_events)},
            "engage": {"raw_count": len(engage_events)},
        },
        "events": kept,
        "suppressed": suppressed_notes,
    }

    DATA_DIR.mkdir(exist_ok=True)
    with (DATA_DIR / "events.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    convert_majors_yaml_to_json()

    print(f"Wrote {len(kept)} events to data/events.json "
          f"({len(suppressed_notes)} suppressed, "
          f"{sum(1 for e in kept if e['occurrence_count'] > 1)} collapsed series)")


if __name__ == "__main__":
    main()
