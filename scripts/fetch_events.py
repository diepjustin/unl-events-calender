#!/usr/bin/env python3
"""Fetch UNL campus events, normalize them, and write data/events.json.

Run it with:

    python3 scripts/fetch_events.py

It also reads data/majors.yaml and data/suppression.yaml and writes
data/majors.json (the browser can't parse YAML, so this is the one
generated file the page actually depends on besides events.json).

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
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

UNL_ICS_URL = "https://events.unl.edu/upcoming/?format=ics&limit=-1"
ENGAGE_RSS_URL = "https://unl.campuslabs.com/engage/events.rss"

# How far ahead to keep events. UNL's feed returns everything out to ~2029
# because recurring events are exploded into one entry per occurrence with
# no end date on the series; without a window this file would balloon and
# mostly show noise nobody can act on yet.
WINDOW_DAYS = 60

USER_AGENT = "unl-events-demo/0.1 (student project; contact via github.com/diepjustin)"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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
            }
        )
    return normalized


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
        sources = sorted({ev["source"] for ev in group})
        if len(sources) > 1:
            head["cross_listed_sources"] = sources
        deduped.append(head)
    return deduped


def collapse_recurrence(events: list[dict]) -> list[dict]:
    """Same title + same source + same time-of-day, on different dates, is
    almost always one series (a weekly meeting, a "daily 9-5" sale, etc).
    The feeds don't give us a recurrence id to group by (see fetch job
    docstring / MAINTAINING notes), so we group on that heuristic instead
    and keep the soonest occurrence as the representative row."""
    groups: dict[tuple, list[dict]] = {}
    for ev in events:
        start = datetime.fromisoformat(ev["start"])
        key = (ev["source"], normalize_title_for_grouping(ev["title"]), start.hour, start.minute)
        groups.setdefault(key, []).append(ev)

    collapsed = []
    for group in groups.values():
        group.sort(key=lambda e: e["start"])
        head = dict(group[0])
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


def apply_suppression(events: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict]]:
    kept = []
    suppressed_notes = []
    for ev in events:
        matched_rule = None
        for rule in rules:
            match = rule.get("match", {})
            if match.get("source") and match["source"] != ev["source"]:
                continue
            if match.get("title_contains") and match["title_contains"].lower() not in ev["title"].lower():
                continue
            if match.get("org_contains") and match["org_contains"].lower() not in (ev.get("org") or "").lower():
                continue
            matched_rule = rule
            break
        if matched_rule:
            suppressed_notes.append({"id": rule.get("id", "unnamed"), "reason": matched_rule["reason"].strip()})
        else:
            kept.append(ev)
    return kept, suppressed_notes


# ---------------------------------------------------------------------------
# Subscribable .ics output -- one campus-wide file, one per major.
#
# NOTE ON DUPLICATED LOGIC: major_matches() below re-implements the same
# org/tag matching as scoreEvent() in index.html (word-boundary tag match,
# substring org match), because the ranked *page* scores client-side but a
# pre-generated .ics file has to be filtered at fetch time, server-side.
# If you change how matching works in one place, change it in the other --
# see MAINTAINING.md.
# ---------------------------------------------------------------------------

def major_matches(event: dict, major: dict) -> bool:
    org = (event.get("org") or "").lower()
    haystack = " ".join(
        filter(None, [event.get("category"), event.get("title"), event.get("description")])
    ).lower()
    org_hit = any(s.lower() in org for s in major.get("org_contains", []))
    tag_hit = any(
        re.search(r"\b" + re.escape(t.lower()) + r"\b", haystack) for t in major.get("tags", [])
    )
    return org_hit or tag_hit


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str, limit: int = 74) -> str:
    """RFC 5545 line folding, ASCII-width approximation (fine for our text,
    which is already plain-ASCII-safe after ics_escape)."""
    if len(line) <= limit:
        return line
    parts = [line[:limit]]
    rest = line[limit:]
    while rest:
        parts.append(" " + rest[: limit - 1])
        rest = rest[limit - 1 :]
    return "\r\n".join(parts)


def build_ics(events: list[dict], calendar_name: str) -> str:
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//unl-events-demo//fetch_events.py//EN",
        fold_ics_line("X-WR-CALNAME:" + ics_escape(calendar_name)),
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for ev in events:
        start = datetime.fromisoformat(ev["start"]).astimezone(timezone.utc)
        lines.append("BEGIN:VEVENT")
        lines.append("UID:" + ev["id"].replace(":", "-").replace(" ", "_") + "@unl-events-demo")
        lines.append("DTSTAMP:" + now_stamp)
        lines.append("DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"))
        if ev.get("end"):
            end = datetime.fromisoformat(ev["end"]).astimezone(timezone.utc)
            lines.append("DTEND:" + end.strftime("%Y%m%dT%H%M%SZ"))
        lines.append(fold_ics_line("SUMMARY:" + ics_escape(ev["title"])))
        if ev.get("location"):
            lines.append(fold_ics_line("LOCATION:" + ics_escape(ev["location"])))
        if ev.get("description"):
            lines.append(fold_ics_line("DESCRIPTION:" + ics_escape(ev["description"])))
        if ev.get("url"):
            lines.append(fold_ics_line("URL:" + ev["url"]))
        lines.append("STATUS:CONFIRMED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_ics_files(events: list[dict], majors: dict) -> None:
    ics_dir = DATA_DIR / "ics"
    ics_dir.mkdir(exist_ok=True)

    (ics_dir / "all-events.ics").write_text(
        build_ics(events, "UNL Campus Events — All"), encoding="utf-8"
    )

    for key, major in majors.items():
        matching = [e for e in events if major_matches(e, major)]
        (ics_dir / f"{key}.ics").write_text(
            build_ics(matching, "UNL Campus Events — " + major.get("label", key)),
            encoding="utf-8",
        )


def convert_majors_yaml_to_json() -> dict:
    """Reads majors.yaml and writes the browser-facing majors.json.

    Two things get normalized away here so index.html's scoreEvent() and
    major_matches() above don't need to know about YAML-only structure:
      - Keys starting with "_" (currently just `_colleges`) are internal
        anchor definitions, not real majors, and are dropped.
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
            "tags": major.get("tags", []),
        }

    with dst.open("w", encoding="utf-8") as f:
        json.dump(majors, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return majors


def main() -> None:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=WINDOW_DAYS)

    print(f"Fetching {UNL_ICS_URL}")
    unl_events = parse_unl_ics(fetch(UNL_ICS_URL))
    print(f"  parsed {len(unl_events)} confirmed UNL events (before windowing)")

    print(f"Fetching {ENGAGE_RSS_URL}")
    engage_events = parse_engage_rss(fetch(ENGAGE_RSS_URL))
    print(f"  parsed {len(engage_events)} confirmed Engage events (before windowing)")

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

    majors = convert_majors_yaml_to_json()
    write_ics_files(kept, majors)

    print(f"Wrote {len(kept)} events to data/events.json "
          f"({len(suppressed_notes)} suppressed, "
          f"{sum(1 for e in kept if e['occurrence_count'] > 1)} collapsed series)")
    print(f"Wrote {len(majors) + 1} .ics files to data/ics/")


if __name__ == "__main__":
    main()
