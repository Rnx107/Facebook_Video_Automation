#!/usr/bin/env python3
"""Generate a Facebook video-post caption from a full YouTube episode via Gemini.

Usage:
    python generate_caption.py <YOUTUBE_URL> <OUTPUT_TXT> [--prompt "custom instructions"]
    python generate_caption.py <YOUTUBE_URL> <OUTPUT_TXT> --model gemini-2.0-flash

Naming convention: save the caption next to its reel with the same stem, so
generate_schedule_csv.py pairs them automatically, e.g. caption for
reels/reel1.mp4 -> reels/reel1.txt:
    python generate_caption.py <EPISODE_URL> reels/reel1.txt

The YouTube URL is passed directly to Gemini as a file_data/file_uri part --
Gemini natively fetches public YouTube videos, so we do NOT download the
1-2 hr episode first.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import typing

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Editable prompt template. Tune this freely -- it is the only thing that
# controls caption style. Requests a general engaging caption of appropriate
# length for a regular Facebook video post (a few short paragraphs, NOT a
# terse Reels-style one-liner) plus 3-5 hashtags.
# ---------------------------------------------------------------------------
CAPTION_PROMPT_TEMPLATE = """\
You are writing a Facebook caption for a video clip taken from a longer podcast episode.
I will give you the full episode as a YouTube video. Watch/listen to it and then write:

1. An engaging Facebook video-post caption (3-8 sentences, conversational tone) that
   summarizes the episode's main topic and hook so a scroller understands what the
   clip is about and wants to watch. Do NOT write a super-short Reels-style one-liner;
   this is for a standard Facebook video post (5-20 min clip), so a fuller paragraph-style
   caption is appropriate.
2. On a new line at the end, add 3-5 relevant hashtags on a single line.

Rules:
- Plain text only. No markdown (no **bold**, no # headings, no bullet lists, no code fences).
- No placeholder text like "[insert title]". Write the actual caption.
- Keep it general (about the episode topic/hook), not a second-by-second transcript.

{custom_instruction}
"""

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)|youtu\.be/)[\w\-]{6,}",
    re.IGNORECASE,
)


def looks_like_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url.strip()))


def strip_markdown_artifacts(text: str) -> str:
    """Remove code fences / stray markdown so output is plain text UTF-8."""
    t = text.strip()
    # Remove wrapping ``` fences (with optional language tag).
    if t.startswith("```"):
        t = re.sub(r"^```\w*\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    # Drop any remaining fence lines inside.
    lines = [ln for ln in t.splitlines() if ln.strip() != "```"]
    t = "\n".join(lines).strip()
    # Un-bold / un-italic / un-strike common inline markers (keep hashtag words).
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"__(.+?)__", r"\1", t)
    return t.strip() + "\n"


def build_prompt(custom_instruction: str | None) -> str:
    extra = custom_instruction.strip() if custom_instruction else ""
    return CAPTION_PROMPT_TEMPLATE.format(custom_instruction=extra)


def generate_caption(youtube_url: str, custom_instruction: str | None, model: str) -> str:
    """Call Gemini with the YouTube URL as native video input. Raises on failure."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit(
            "ERROR: GEMINI_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    if not looks_like_youtube_url(youtube_url):
        raise SystemExit(f"ERROR: does not look like a YouTube URL: {youtube_url!r}")

    try:
        from google import genai
        from google.genai import errors as genai_errors
        from google.genai import types
    except ImportError:
        raise SystemExit("ERROR: google-genai is not installed. Run: pip install -r requirements.txt")

    prompt = build_prompt(custom_instruction)

    # NOTE (preview feature): passing a YouTube URL as file_data/file_uri is a
    # Gemini preview feature (free at time of writing; pricing/rate limits and
    # behavior may change — e.g. 1 video/request, public videos only, ~8h/day
    # limit per docs). If Google removes/changes this, the API will reject the
    # file_data part — we fail LOUDLY below instead of returning garbage, so
    # you notice immediately.
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=types.Content(
                parts=[
                    types.Part(
                        file_data=types.FileData(file_uri=youtube_url.strip())
                    ),
                    types.Part(text=prompt),
                ]
            ),
        )
    except NameError:
        raise
    except Exception as exc:  # classify below; genai_errors.APIError is the common case
        _classify_and_raise(exc)

    text = getattr(response, "text", None)
    if text:
        text = text.strip()
    # Fallback: walk candidates/parts manually (SDK versions differ).
    if not text:
        try:
            chunks: list[str] = []
            for cand in getattr(response, "candidates", None) or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", None) or []:
                    if getattr(part, "text", None):
                        chunks.append(part.text)
            text = "\n".join(chunks).strip()
        except Exception:
            text = ""
    if not text:
        raise SystemExit(
            "ERROR: Gemini returned an empty response. The video may be private/unlisted, "
            "age-restricted, or the model refused. Try a public video or a different prompt."
        )
    return strip_markdown_artifacts(text)


def _classify_and_raise(exc: Exception) -> typing.NoReturn:
    """Map Gemini errors to clear messages. Always raises SystemExit."""
    msg = str(exc)
    code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    low = msg.lower()

    # Import here to avoid hard dependency at module import time for tests.
    try:
        from google.genai import errors as genai_errors  # noqa: F401
    except Exception:
        pass

    if code == 429 or "429" in msg or "quota" in low or "rate" in low or "resource_exhausted" in low:
        raise SystemExit(f"ERROR: Gemini quota/rate limit hit (code={code}). Wait and retry. Details: {msg}")
    if code in (400, 403) or "invalid_argument" in low or "not found" in low or "unsupported" in low:
        raise SystemExit(
            "ERROR: Gemini rejected the YouTube URL (invalid, private/unlisted, or "
            f"YouTube-input preview changed). Code={code}. Details: {msg}"
        )
    if "api key" in low or "permission" in low or code in (401,):
        raise SystemExit(f"ERROR: Gemini auth failed. Check GEMINI_API_KEY. Details: {msg}")
    raise SystemExit(f"ERROR: Gemini API call failed (code={code}). Details: {msg}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate a Facebook caption from a YouTube episode via Gemini.")
    p.add_argument("youtube_url", help="Full-episode YouTube URL (public video)")
    p.add_argument("output", help="Output .txt filename, e.g. reel_meta_ep12.txt")
    p.add_argument("--prompt", "--instruction", dest="prompt", default=None,
                   help="Custom instruction/prompt override appended to the template")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        caption = generate_caption(args.youtube_url, args.prompt, args.model)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    # Print to stdout (the deliverable's required channel) ...
    print(caption, end="" if caption.endswith("\n") else "\n")
    # ... AND save UTF-8 plain text.
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(caption)
    except OSError as e:
        print(f"ERROR: could not write output file {args.output!r}: {e}", file=sys.stderr)
        return 1
    log.info("Saved caption to %s (%d chars)", args.output, len(caption))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
