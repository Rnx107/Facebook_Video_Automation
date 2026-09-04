#!/usr/bin/env python3
"""Schedule local clip files as regular Facebook Page video posts.

Usage:
    python schedule_facebook_posts.py schedule.csv [--errors-log errors.log]

CSV columns (in order): video_path, caption_path, scheduled_datetime, status
  - scheduled_datetime format: YYYY-MM-DD HH:MM, interpreted in TIMEZONE.
  - status: blank initially; script writes scheduled/failed/skipped and
    persists back to the CSV so re-runs never double-submit.

Flow per row: validate -> chunked upload (start/transfer/finish against
/{page-id}/videos) with published=false + scheduled_publish_time -> mark
scheduled. One bad row never crashes the batch.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable constants (tune here, not deep in the code).
# ---------------------------------------------------------------------------
# Single timezone all CSV datetimes are interpreted in.
TIMEZONE = "Asia/Kathmandu"

# Facebook requires scheduled_publish_time to be 10 min - 6 months from now
# (see Page Videos ref: scheduled_publish_time "should be between 10 mins and
# 6 months"). Both bounds enforced client-side so we fail fast with a clear
# message instead of a cryptic API error.
MIN_LEAD_TIME_MINUTES = 10
# ~6 months expressed in days so Meta can change it by editing one number.
# TODO: verify exact max window against current Meta docs if scheduling near the edge
# (docs say 6 months for videos; some feed-post docs say 30 days — videos use 6 months).
MAX_SCHEDULE_DAYS = 182

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov"}

DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# Fallback chunk size if the start-phase response does not advertise one.
# Normally we use Meta's suggested (end_offset - start_offset) instead.
DEFAULT_CHUNK_SIZE_BYTES = 8 * 1024 * 1024

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 60

ERRORS_LOG_DEFAULT = "errors.log"


# ---------------------------------------------------------------------------
# Errors / retry helpers
# ---------------------------------------------------------------------------
class FBAPIError(Exception):
    def __init__(self, message: str, code=None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _is_retryable_status(status: int) -> bool:
    # Retry 429 (rate limit) + 5xx (server/transient). Never retry other 4xx
    # (bad token, bad params) — retrying those just wastes calls.
    return status == 429 or (500 <= status < 600)


def _extract_fb_error(payload) -> tuple[str, object]:
    """Pull human message/code out of a Graph API error payload."""
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            return str(err.get("message", payload)), err.get("code")
        return str(payload), None
    return str(payload), None


def fb_post(url: str, data: dict, files: dict | None = None) -> dict:
    """POST with exponential-backoff retry ONLY for transient failures.

    Retries: timeouts, connection errors, HTTP 429, HTTP 5xx.
    No retry: other 4xx (auth/validation) — raises FBAPIError immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, data=data, files=files, timeout=REQUEST_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt == MAX_RETRIES:
                raise FBAPIError(f"Network error after {MAX_RETRIES} attempts: {e}") from e
            sleep = BACKOFF_BASE_SECONDS ** attempt
            log.warning("Transient network error (attempt %d/%d): %s. Retrying in %.0fs",
                        attempt, MAX_RETRIES, e, sleep)
            time.sleep(sleep)
            continue
        # Got an HTTP response — decide retry vs fail vs success.
        if 200 <= resp.status_code < 300:
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        msg, code = _extract_fb_error(payload)
        full = f"Facebook API HTTP {resp.status_code} (code={code}): {msg}"
        if _is_retryable_status(resp.status_code) and attempt < MAX_RETRIES:
            sleep = BACKOFF_BASE_SECONDS ** attempt
            log.warning("%s — retrying in %.0fs (attempt %d/%d)", full, sleep, attempt, MAX_RETRIES)
            time.sleep(sleep)
            last_exc = FBAPIError(full, code=code, status=resp.status_code)
            continue
        raise FBAPIError(full, code=code, status=resp.status_code)
    raise FBAPIError(f"Facebook API failed after {MAX_RETRIES} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Config / time helpers
# ---------------------------------------------------------------------------
def load_config() -> tuple[str, str, str]:
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    api_version = os.environ.get("FB_API_VERSION", "v23.0").strip() or "v23.0"
    if not page_id:
        raise SystemExit("ERROR: FB_PAGE_ID is not set. Copy .env.example to .env and fill it in.")
    if not token:
        raise SystemExit("ERROR: FB_PAGE_ACCESS_TOKEN is not set. Copy .env.example to .env and fill it in.")
    return page_id, token, api_version


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"ERROR: invalid TIMEZONE constant {TIMEZONE!r}.")


def parse_scheduled_datetime(raw: str, tz: ZoneInfo) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' as a TIMEZONE-local aware datetime."""
    s = (raw or "").strip()
    try:
        naive = datetime.strptime(s, DATETIME_FORMAT)
    except ValueError:
        raise ValueError(f"scheduled_datetime {raw!r} does not match format YYYY-MM-DD HH:MM")
    return naive.replace(tzinfo=tz)


# ---------------------------------------------------------------------------
# Chunked upload against /{page-id}/videos (NOT /video_reels).
#
# Why /videos and not /video_reels: our clips are 5-20 min, far above the
# ~90s Reels cap, and /video_reels enforces that cap. The standard Page video
# post endpoint has no such duration cap and supports scheduled_publish_time.
#
# Why chunked: clips can be hundreds of MB; a single POST would time out and
# cannot resume. start -> transfer (repeated) -> finish lets us resume and
# keeps memory bounded (one chunk in RAM at a time).
# ---------------------------------------------------------------------------
def start_upload_session(videos_url: str, file_size: int, access_token: str) -> dict:
    # TODO: verify field names against current Meta docs — start_offset/end_offset/
    # upload_session_id are per the Page Videos reference; fail loudly if Meta renames them.
    data = {
        "upload_phase": "start",
        "file_size": str(file_size),
        "access_token": access_token,
    }
    resp = fb_post(videos_url, data=data)
    if "upload_session_id" not in resp:
        raise FBAPIError(f"start phase missing upload_session_id in response: {resp}")
    return resp


def transfer_chunks(videos_url: str, upload_session_id: str, video_path: Path,
                    file_size: int, access_token: str,
                    suggested_chunk_size: int | None) -> None:
    chunk_size = suggested_chunk_size if suggested_chunk_size and suggested_chunk_size > 0 \
        else DEFAULT_CHUNK_SIZE_BYTES
    log.info("Uploading %s (%d bytes) in ~%d-byte chunks (session %s)",
             video_path.name, file_size, chunk_size, upload_session_id)
    offset = 0
    with open(video_path, "rb") as f:
        while offset < file_size:
            f.seek(offset)
            chunk = f.read(chunk_size)
            if not chunk:
                break
            # TODO: verify multipart field name 'video_file_chunk' + 'start_offset'
            # against current Meta docs; wrong names fail loudly via API error.
            data = {
                "upload_phase": "transfer",
                "upload_session_id": str(upload_session_id),
                "start_offset": str(offset),
                "access_token": access_token,
            }
            files = {"video_file_chunk": (video_path.name, chunk, "application/octet-stream")}
            resp = fb_post(videos_url, data=data, files=files)
            # Server tells us the next expected offset; trust it over our math.
            try:
                new_offset = int(resp.get("start_offset", offset + len(chunk)))
            except (TypeError, ValueError):
                new_offset = offset + len(chunk)
            if new_offset <= offset and (offset + len(chunk)) < file_size:
                raise FBAPIError(f"transfer stalled: server returned start_offset={resp}")
            offset = new_offset
            log.debug("Transferred; next offset %d/%d", offset, file_size)
    if offset < file_size:
        raise FBAPIError(f"Upload incomplete: {offset}/{file_size} bytes transferred")


def finish_upload(videos_url: str, upload_session_id: str, description: str,
                  scheduled_ts: int, access_token: str) -> dict:
    """Finish + schedule. published=false keeps it unpublished until
    scheduled_publish_time; Facebook's servers trigger the actual publish."""
    data = {
        "upload_phase": "finish",
        "upload_session_id": str(upload_session_id),
        "description": description,
        "published": "false",
        "scheduled_publish_time": str(scheduled_ts),
        "access_token": access_token,
        # NOTE: some docs mention unpublished_content_type=SCHEDULED for scheduled
        # videos. The spec only requires published=false + scheduled_publish_time,
        # so we set exactly that.
        # TODO: verify whether unpublished_content_type=SCHEDULED is also required
        # on the current API version — if finish rejects scheduling, try adding it.
    }
    return fb_post(videos_url, data=data)


def upload_and_schedule(video_path: Path, description: str, scheduled_ts: int,
                        page_id: str, access_token: str, api_version: str) -> str:
    videos_url = f"https://graph.facebook.com/{api_version}/{page_id}/videos"
    file_size = video_path.stat().st_size

    start_resp = start_upload_session(videos_url, file_size, access_token)
    session_id = str(start_resp["upload_session_id"])
    # Use Meta's suggested chunk size (end_offset - start_offset) rather than
    # hardcoding one, per the Resumable Upload docs.
    suggested = None
    try:
        if "start_offset" in start_resp and "end_offset" in start_resp:
            suggested = int(start_resp["end_offset"]) - int(start_resp["start_offset"])
    except (TypeError, ValueError):
        suggested = None

    transfer_chunks(videos_url, session_id, video_path, file_size, access_token, suggested)

    finish_resp = finish_upload(videos_url, session_id, description, scheduled_ts, access_token)
    # TODO: verify success field name — docs show {"id": "<VIDEO_ID>"} and/or
    # {"success": true, "video_id": ...} depending on version. Accept either.
    video_id = str(finish_resp.get("id") or finish_resp.get("video_id") or "")
    if not video_id and not finish_resp.get("success"):
        raise FBAPIError(f"finish phase returned unexpected response (no video id): {finish_resp}")
    return video_id or "(success, no id returned)"


# ---------------------------------------------------------------------------
# CSV pipeline
# ---------------------------------------------------------------------------
EXPECTED_COLUMNS = ["video_path", "caption_path", "scheduled_datetime", "status"]


def validate_row(video_raw: str, caption_raw: str, dt_raw: str,
                 tz: ZoneInfo, now: datetime) -> tuple[int, str]:
    """Run pre-upload validations. Returns (scheduled_unix_ts, description). Raises ValueError."""
    v = (video_raw or "").strip()
    c = (caption_raw or "").strip()
    if not v:
        raise ValueError("video_path is empty")
    vp = Path(v)
    if not vp.is_file():
        raise ValueError(f"video file not found: {v}")
    if vp.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError(f"video file extension {vp.suffix!r} not in allowlist {sorted(ALLOWED_VIDEO_EXTENSIONS)}")
    if vp.stat().st_size == 0:
        raise ValueError(f"video file is empty (0 bytes): {v}")
    if not c:
        raise ValueError("caption_path is empty")
    cp = Path(c)
    if not cp.is_file():
        raise ValueError(f"caption file not found: {c}")
    try:
        caption_text = cp.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"could not read caption file {c}: {e}")
    if not caption_text.strip():
        raise ValueError(f"caption file is empty: {c}")
    dt = parse_scheduled_datetime(dt_raw, tz)
    ts = int(dt.timestamp())
    now_ts = int(now.timestamp())
    min_ts = now_ts + MIN_LEAD_TIME_MINUTES * 60
    if ts < min_ts:
        raise ValueError(
            f"scheduled_datetime {dt_raw!r} is too soon (needs >= {MIN_LEAD_TIME_MINUTES} min "
            f"in the future; now={now.strftime(DATETIME_FORMAT)} {TIMEZONE})"
        )
    max_ts = now_ts + MAX_SCHEDULE_DAYS * 24 * 3600
    if ts > max_ts:
        raise ValueError(
            f"scheduled_datetime {dt_raw!r} is too far out (max ~{MAX_SCHEDULE_DAYS} days / 6 months)"
        )
    return ts, caption_text


def save_csv(csv_path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def process_csv(csv_path: Path, errors_log: Path) -> tuple[int, int, int]:
    page_id, token, api_version = load_config()
    tz = get_tz()

    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise SystemExit(f"ERROR: {csv_path} is empty or has no header row.")
        fieldnames = list(reader.fieldnames)
        # Ensure the managed 'status' column exists even on hand-made CSVs.
        if "status" not in fieldnames:
            fieldnames.append("status")
        rows = [dict(r) for r in reader]
    for col in ("video_path", "caption_path", "scheduled_datetime"):
        if col not in fieldnames:
            raise SystemExit(f"ERROR: CSV missing required column {col!r} (need {EXPECTED_COLUMNS})")

    n_scheduled = n_failed = n_skipped = 0
    now = datetime.now(tz)

    # First pass: parse timestamps for duplicate detection among rows in this run.
    ts_by_row: dict[int, int] = {}  # row index -> unix ts (only if parseable & not skipped)
    seen_ts: dict[int, int] = {}    # unix ts -> first row index
    duplicate_rows: set[int] = set()
    for i, row in enumerate(rows):
        if (row.get("status") or "").strip().lower() == "scheduled":
            continue
        try:
            dt = parse_scheduled_datetime(row.get("scheduled_datetime", ""), tz)
            ts = int(dt.timestamp())
        except ValueError:
            continue  # validation pass below will report the parse error
        if ts in seen_ts:
            duplicate_rows.add(i)
            log.warning("Row %d duplicates scheduled_datetime of row %d (%s) — will fail it, not double-book",
                        i + 2, seen_ts[ts] + 2, row.get("scheduled_datetime"))
        else:
            seen_ts[ts] = i
        ts_by_row[i] = ts

    for i, row in enumerate(rows):
        lineno = i + 2  # 1-based + header
        status = (row.get("status") or "").strip().lower()
        if status == "scheduled":
            n_skipped += 1
            log.info("Row %d: already scheduled — skipping (idempotency)", lineno)
            continue
        if i in duplicate_rows:
            reason = f"duplicate scheduled_datetime {row.get('scheduled_datetime')!r} already used by another row in this run"
            row["status"] = "failed"
            n_failed += 1
            log.error("Row %d: FAILED validation: %s", lineno, reason)
            _append_error(errors_log, lineno, row.get("video_path", ""), reason)
            save_csv(csv_path, fieldnames, rows)
            continue
        try:
            ts, description = validate_row(
                row.get("video_path", ""), row.get("caption_path", ""),
                row.get("scheduled_datetime", ""), tz, now)
        except ValueError as e:
            row["status"] = "failed"
            n_failed += 1
            log.error("Row %d: FAILED validation: %s", lineno, e)
            _append_error(errors_log, lineno, row.get("video_path", ""), f"validation: {e}")
            save_csv(csv_path, fieldnames, rows)  # persist so re-runs skip/fail fast, never double-submit
            continue
        # Passed validation — upload.
        try:
            video_id = upload_and_schedule(
                Path(row["video_path"].strip()), description, ts, page_id, token, api_version)
        except FBAPIError as e:
            row["status"] = "failed"
            n_failed += 1
            log.error("Row %d: Facebook API failure: %s", lineno, e)
            _append_error(errors_log, lineno, row.get("video_path", ""), str(e))
            save_csv(csv_path, fieldnames, rows)
            continue
        except OSError as e:
            row["status"] = "failed"
            n_failed += 1
            log.error("Row %d: file error during upload: %s", lineno, e)
            _append_error(errors_log, lineno, row.get("video_path", ""), f"file error: {e}")
            save_csv(csv_path, fieldnames, rows)
            continue
        row["status"] = "scheduled"
        n_scheduled += 1
        log.info("Row %d: scheduled OK (video id %s)", lineno, video_id)
        save_csv(csv_path, fieldnames, rows)

    return n_scheduled, n_failed, n_skipped


def _append_error(errors_log: Path, lineno: int, video_path: str, reason: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with open(errors_log, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] row {lineno} video={video_path!r}: {reason}\n")
    except OSError as e:
        log.error("Could not write to %s: %s", errors_log, e)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Upload + schedule Facebook Page video posts from a CSV.")
    p.add_argument("csv", help="Input CSV, e.g. schedule.csv")
    p.add_argument("--errors-log", default=ERRORS_LOG_DEFAULT, help=f"Error log file (default: {ERRORS_LOG_DEFAULT})")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 1
    try:
        n_ok, n_fail, n_skip = process_csv(csv_path, Path(args.errors_log))
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    except Exception as e:  # never crash silently on unexpected errors
        log.exception("Fatal error: %s", e)
        print(f"ERROR: fatal failure: {e}", file=sys.stderr)
        return 1
    # Final summary goes to stdout (logging handles the rest).
    print(f"Done: {n_ok} scheduled, {n_fail} failed, {n_skip} skipped (already scheduled).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
