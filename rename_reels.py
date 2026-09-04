#!/usr/bin/env python3
"""Normalize messy editor-export filenames to reel1/reel2/... in a folder.

Usage:
    python rename_reels.py [reels/] [--yes] [--sort-by mtime|name]

Run this FIRST, before captions/scheduling. It renames only video files
(.mp4/.mov), oldest export first, preserving each file's original extension:

    IMG_9931.mov      -> reel1.mov
    final_final_v2.mp4 -> reel2.mp4

Safety: prints a preview and asks for confirmation before touching anything
(use --yes to skip the prompt in scripts). Never overwrites an existing file.
Writes every rename to rename_log.csv inside the folder as a permanent record.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ordering for numbering. "mtime" (oldest file first) is the default because
# export order usually reflects the order clips were finished. One-line change
# to "name" for alphabetical order instead.
# ---------------------------------------------------------------------------
SORT_BY = "mtime"  # "mtime" | "name"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov"}

RENAME_LOG_NAME = "rename_log.csv"


def list_video_files(folder: Path, sort_by: str) -> tuple[list[Path], list[Path]]:
    """Return (videos_sorted, skipped). Skipped = non-video files and dirs."""
    entries = [p for p in folder.iterdir()]
    videos: list[Path] = []
    skipped: list[Path] = []
    for p in entries:
        if p.is_dir():
            log.info("Skipping %s: not a file (directory)", p.name)
            skipped.append(p)
        elif p.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
            log.info("Skipping %s: not a video file (need .mp4/.mov)", p.name)
            skipped.append(p)
        else:
            videos.append(p)
    if sort_by == "mtime":
        videos.sort(key=lambda p: (p.stat().st_mtime_ns, p.name.lower()))
    elif sort_by == "name":
        videos.sort(key=lambda p: p.name.lower())
    else:
        raise SystemExit(f"ERROR: unknown sort order {sort_by!r} (use 'mtime' or 'name').")
    return videos, skipped


def plan_renames(videos: list[Path]) -> list[tuple[Path, Path]]:
    """Map each video to reel<N><original extension>, N starting at 1."""
    return [(src, src.parent / f"reel{i}{src.suffix}") for i, src in enumerate(videos, start=1)]


def check_collisions(plan: list[tuple[Path, Path]]) -> None:
    """Fail fast if any target already exists and isn't that same file.

    This matters when the script is run twice on a partially processed folder:
    instead of clobbering reel1.mp4, we stop with a clear error.
    """
    for src, dst in plan:
        if src.resolve() == dst.resolve():
            continue  # already correctly named — no-op, not a collision
        if dst.exists():
            raise SystemExit(
                f"ERROR: refusing to rename {src.name} -> {dst.name}: "
                f"{dst.name} already exists. The folder may be partially renamed "
                f"already — fix names manually and re-run."
            )
    # Also guard against two sources mapping to the same target (paranoia;
    # can't happen with sequential numbering, but fail loudly if it ever does).
    targets = [dst.name for _, dst in plan]
    if len(set(targets)) != len(targets):
        raise SystemExit(f"ERROR: internal naming collision in rename plan: {targets}")


def append_rename_log(folder: Path, performed: list[tuple[Path, Path]]) -> Path:
    """Append old->new rows to <folder>/rename_log.csv. Returns the log path."""
    log_path = folder / RENAME_LOG_NAME
    is_new = not log_path.exists()
    with open(log_path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["old_name", "new_name"])
        for src, dst in performed:
            w.writerow([src.name, dst.name])
    return log_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rename messy video exports to reel1/reel2/....")
    p.add_argument("folder", nargs="?", default="reels/",
                   help="Target folder (default: reels/)")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt (for scripted use)")
    p.add_argument("--sort-by", default=SORT_BY, choices=["mtime", "name"],
                   help=f"Numbering order (default: {SORT_BY})")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"ERROR: folder not found: {folder}", file=sys.stderr)
        return 1

    videos, skipped = list_video_files(folder, args.sort_by)
    if not videos:
        print(f"No video files (.mp4/.mov) found in {folder} — nothing to do.")
        if skipped:
            print(f"Skipped {len(skipped)} non-video file(s); see log above.")
        return 0

    plan = plan_renames(videos)
    try:
        check_collisions(plan)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1

    # Preview FIRST — renaming feels destructive, so no surprises.
    print("Planned renames (oldest export first):")
    n_noop = 0
    for src, dst in plan:
        if src.name == dst.name:
            print(f"  {src.name} (already correct — will skip)")
            n_noop += 1
        else:
            print(f"  {src.name} -> {dst.name}")
    if skipped:
        print(f"Skipping {len(skipped)} non-video file(s): "
              + ", ".join(p.name for p in skipped))

    to_do = [(s, d) for s, d in plan if s.name != d.name]
    if not to_do:
        print("Everything is already named correctly — nothing to rename.")
        return 0

    if not args.yes:
        try:
            answer = input(f"Rename {len(to_do)} file(s)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled — nothing was renamed.", file=sys.stderr)
            return 1
        if answer not in ("y", "yes"):
            print("Cancelled — nothing was renamed.")
            return 1

    # Targets were verified free above, so rename in plan order.
    performed: list[tuple[Path, Path]] = []
    for src, dst in to_do:
        src.rename(dst)
        performed.append((src, dst))
        log.info("Renamed %s -> %s", src.name, dst.name)

    log_path = append_rename_log(folder, performed)
    # Final summary goes to stdout (logging handles the rest).
    print(f"Done: renamed {len(performed)} file(s). Record saved to {log_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
