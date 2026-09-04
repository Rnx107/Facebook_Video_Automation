#!/usr/bin/env python3
"""Assign each captioned-but-unscheduled reel to the next open posting slot.

Usage:
    python generate_schedule_csv.py [--reels reels/] [--csv schedule.csv]

Reads the reels/ folder (post-rename: reel1.mp4, reel2.mp4, ...) and appends
one row per NEW reel to schedule.csv — ready for schedule_facebook_posts.py.
Existing CSV rows are never touched or reassigned. Safe to re-run whenever
new reels land: reels that already have a row (any status) are skipped.

Slot rules (must match schedule_facebook_posts.py's validation exactly, so the
two scripts never disagree): candidate slots are 07:00 and 19:00 Nepal Time
every day; a slot is open only if it is more than 10 minutes in the future
AND no existing CSV row already uses that exact date/time.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Posting schedule. Change these two lines if the posting rhythm changes.
# ---------------------------------------------------------------------------
TIMEZONE = "Asia/Kathmandu"
SLOT_TIMES = ["07:00", "19:00"]  # twice daily, Nepal Time

# Must stay identical to schedule_facebook_posts.py's values, otherwise the
# two scripts disagree about what counts as double-booked or too-soon.
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
MIN_LEAD_TIME_MINUTES = 10

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov"}
CSV_COLUMNS = ["video_path", "caption_path", "scheduled_datetime", "status"]

REEL_RE = re.compile(r"^reel(\d+)$", re.IGNORECASE)


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"ERROR: invalid TIMEZONE constant {TIMEZONE!r}.")


def reel_sort_key(p: Path) -> tuple:
    """reel1 < reel2 < ... < reel10 (numeric, not alphabetical), then others by name."""
    m = REEL_RE.match(p.stem)
    if m:
        return (0, int(m.group(1)), "")
    return (1, 0, p.name.lower())


def find_caption_for(video: Path) -> Path | None:
    """Caption for reel1.mp4 is reel1.txt: same folder first, then cwd."""
    same_dir = video.parent / f"{video.stem}.txt"
    if same_dir.is_file():
        return same_dir
    cwd_match = Path.cwd() / f"{video.stem}.txt"
    if cwd_match.is_file():
        return cwd_match
    return None


def load_existing(csv_path: Path, tz: ZoneInfo) -> tuple[list[dict], set[str], set[int]]:
    """Return (rows, assigned_video_keys, taken_slot_timestamps).

    assigned: every video_path already in the CSV, ANY status — even `failed`
      (a failed upload needs manual review, not a silent second slot).
    taken: Unix timestamps of every parseable scheduled_datetime in the CSV.
    """
    if not csv_path.exists():
        return [], set(), set()
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(r) for r in reader] if reader.fieldnames else []
    assigned = {norm_video_key(r.get("video_path", "")) for r in rows if r.get("video_path", "").strip()}
    taken: set[int] = set()
    for r in rows:
        raw = (r.get("scheduled_datetime") or "").strip()
        if not raw:
            continue
        try:
            taken.add(int(datetime.strptime(raw, DATETIME_FORMAT).replace(tzinfo=tz).timestamp()))
        except ValueError:
            log.warning("Existing row has unparseable scheduled_datetime %r — ignoring it for slot-taking "
                        "(schedule_facebook_posts.py will flag that row)", raw)
    return rows, assigned, taken


def norm_video_key(video_path: str) -> str:
    """Normalize a CSV video_path so reels/reel1.mp4 and ./reels/reel1.mp4 match."""
    return str(Path(video_path.strip()).resolve())


def next_open_slots(n: int, taken: set[int], tz: ZoneInfo, now: datetime) -> list[datetime]:
    """Walk 7am -> 7pm -> next day 7am ... collecting the first n open slots."""
    slots: list[datetime] = []
    horizon = now.date() + timedelta(days=400)  # paranoia bound; ~6-month FB window is far smaller
    day = now.date()
    while len(slots) < n and day <= horizon:
        for t in SLOT_TIMES:
            hour, minute = int(t[:2]), int(t[3:5])
            cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            ts = int(cand.timestamp())
            # Same 10-minute lead-time rule Deliverable 2 enforces client-side.
            if ts <= int(now.timestamp()) + MIN_LEAD_TIME_MINUTES * 60:
                continue
            if ts in taken:
                continue
            slots.append(cand)
            taken.add(ts)  # reserve within this run so two new reels can't share it
            if len(slots) >= n:
                break
        day += timedelta(days=1)
    if len(slots) < n:
        raise SystemExit("ERROR: ran out of calendar searching for open slots (should never happen).")
    return slots


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Assign unscheduled reels to the next open 7am/7pm slots.")
    p.add_argument("--reels", default="reels/", help="Reels folder to scan (default: reels/)")
    p.add_argument("--csv", default="schedule.csv", help="Schedule CSV to append to (default: schedule.csv)")
    return p.parse_args(argv)


def build_schedule(reels_dir: Path, csv_path: Path) -> list[dict]:
    """Find new reels, assign slots, append rows. Returns the new rows."""
    tz = get_tz()
    if not reels_dir.is_dir():
        raise SystemExit(f"ERROR: reels folder not found: {reels_dir}")

    _, assigned, taken = load_existing(csv_path, tz)

    videos = sorted(
        (p for p in reels_dir.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS),
        key=reel_sort_key,
    )
    new_rows: list[dict] = []
    for video in videos:
        if norm_video_key(str(video)) in assigned:
            log.info("Skipping %s: already has a row in %s", video.name, csv_path)
            continue
        caption = find_caption_for(video)
        if caption is None:
            # Never schedule a post with no caption.
            log.warning("Skipping %s: no matching caption file (%s.txt) found next to the video or in %s",
                        video.name, video.stem, Path.cwd())
            continue
        new_rows.append({"video": video, "caption": caption})

    if not new_rows:
        return []

    now = datetime.now(tz)
    slots = next_open_slots(len(new_rows), set(taken), tz, now)

    out_rows: list[dict] = []
    for item, slot in zip(new_rows, slots):
        out_rows.append({
            "video_path": item["video"].as_posix(),
            "caption_path": item["caption"].as_posix(),
            "scheduled_datetime": slot.strftime(DATETIME_FORMAT),
            "status": "",
        })

    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerows(out_rows)  # append only — existing rows are never rewritten
    return out_rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        added = build_schedule(Path(args.reels), Path(args.csv))
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    # Final summary goes to stdout (logging handles the rest).
    if not added:
        print("Done: no new reels to schedule (everything captioned already has a row).")
    else:
        print(f"Done: assigned {len(added)} new reel(s):")
        for r in added:
            print(f"  {r['video_path']} -> {r['scheduled_datetime']} ({TIMEZONE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
