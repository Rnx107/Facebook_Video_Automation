#!/usr/bin/env python3
"""Generate YouTube metadata (title, bilingual description, link, tags) from
a full YouTube episode via Gemini.

Usage:
    python generate_youtube_meta.py <YOUTUBE_URL> <OUTPUT_TXT>
    python generate_youtube_meta.py <YOUTUBE_URL> <OUTPUT_TXT> --half
    python generate_youtube_meta.py <YOUTUBE_URL> <OUTPUT_TXT> --half --duration 7200
    python generate_youtube_meta.py <YOUTUBE_URL> <OUTPUT_TXT> --prompt "extra instructions"
    python generate_youtube_meta.py <YOUTUBE_URL> <OUTPUT_TXT> --model gemini-2.0-flash

The --half flag tells Gemini to only process roughly the first half of the
video (via videoMetadata.endOffset), which cuts token usage significantly for
long 1-2 hr episodes. The model still sees enough to understand the topic,
guests, and overall theme. You must supply --duration (total video length in
seconds) alongside --half, OR the script will default to assuming 7200s (2 hr).

Output format (written to the .txt file and stdout):
    Line 1:  YouTube Title
    Lines 2-5: English description (3-4 lines)
    Lines 6-9: Nepali description (3-4 lines)
    Line 10: Full YouTube link
    Line 11: Tags (comma-separated)
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
# Prompt template. This is the only thing that controls output style/format.
# ---------------------------------------------------------------------------
META_PROMPT_TEMPLATE = """\
You are writing a Facebook post for a Nepali podcast channel called "Common Ground".
I will give you a YouTube video (a full podcast episode). Watch/listen to it carefully
and produce the following output. The output must be READY TO COPY-PASTE directly as
a Facebook post — no section labels, no headings, no markdown, no bullet points.

Write EXACTLY in this format (follow the blank-line spacing precisely):

<A compelling title line in English, max ~70 characters. Curiosity-driven. No "TITLE:" prefix.>

<3-4 lines of engaging English description summarizing the episode's key topic, guest,
and why someone should watch. Conversational tone. Do NOT write "English Description:"
or any label — just write the description directly.>

<3-4 lines of Nepali (Devanagari script) description covering the same content.
Natural Nepali, not machine-translated stiffness. Do NOT write "Nepali Description:"
or any label — just write the Nepali text directly.>

Full video: {youtube_url}

<8-12 hashtags on a single line, each starting with #, separated by spaces.
Mix of English and Nepali hashtags. Include channel name, guest name, topic keywords.
Example: #CommonGround #GuestName #Topic1 #नेपाल #शिक्षा>

Rules:
- Plain text only. No markdown (no **bold**, no # headings, no bullet lists, no code fences).
- No placeholder text. Write the actual content.
- Do NOT write labels like "Title:", "English Description:", "Nepali Description:", "Tags:", "Link:" etc.
- The YouTube link line must say "Full video: " followed by the exact URL I provide.
- Every hashtag must start with # and contain no spaces within the tag.

{custom_instruction}
"""

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# Default assumed video duration if --half is used without --duration.
DEFAULT_DURATION_SECONDS = 7200  # 2 hours

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)|youtu\.be/)[\w\-]{6,}",
    re.IGNORECASE,
)


def looks_like_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url.strip()))


def strip_markdown_artifacts(text: str) -> str:
    """Remove code fences / stray markdown so output is plain text UTF-8."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    lines = [ln for ln in t.splitlines() if ln.strip() != "```"]
    t = "\n".join(lines).strip()
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"__(.+?)__", r"\1", t)
    return t.strip() + "\n"


def build_prompt(youtube_url: str, custom_instruction: str | None) -> str:
    extra = custom_instruction.strip() if custom_instruction else ""
    return META_PROMPT_TEMPLATE.format(
        youtube_url=youtube_url.strip(),
        custom_instruction=extra,
    )


def generate_meta(
    youtube_url: str,
    custom_instruction: str | None,
    model: str,
    half_video: bool = False,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
) -> str:
    """Call Gemini with the YouTube URL. Optionally limit to first half of video."""
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

    prompt = build_prompt(youtube_url, custom_instruction)

    # Build the video part. When --half is used, we attach videoMetadata with
    # an endOffset so Gemini only processes the first ~50% of the video. This
    # substantially reduces token consumption for long episodes (1-2 hr).
    #
    # NOTE: videoMetadata with startOffset/endOffset is supported when the
    # video is provided via file_data (including YouTube URLs). The offset
    # uses protobuf Duration format — the Python SDK accepts a string like
    # "3600s" or a dict {"seconds": 3600}.
    video_part_kwargs: dict[str, typing.Any] = {}
    if half_video:
        half_seconds = duration_seconds // 2
        log.info(
            "Half-video mode: processing first %d seconds (of ~%d total) to save credits.",
            half_seconds,
            duration_seconds,
        )
        video_part_kwargs["video_metadata"] = types.VideoMetadata(
            end_offset=f"{half_seconds}s",
        )

    # Construct the video Part. We use the explicit constructor rather than
    # the from_uri helper so we can attach video_metadata when needed.
    video_part = types.Part(
        file_data=types.FileData(
            file_uri=youtube_url.strip(),
            mime_type="video/mp4",
        ),
        **video_part_kwargs,
    )

    # NOTE (preview feature): passing a YouTube URL as file_data/file_uri is a
    # Gemini preview feature. If Google removes/changes this, the API will
    # reject the file_data part — we fail LOUDLY below instead of returning
    # garbage, so you notice immediately.
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=[video_part, prompt],
        )
    except NameError:
        raise
    except Exception as exc:
        _classify_and_raise(exc)

    text = getattr(response, "text", None)
    if text:
        text = text.strip()
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
    p = argparse.ArgumentParser(
        description="Generate YouTube metadata (title, bilingual description, link, tags) from a YouTube episode via Gemini."
    )
    p.add_argument("youtube_url", help="Full-episode YouTube URL (public video)")
    p.add_argument("output", nargs="?", default="Reels/caption.txt",
                   help="Output .txt filename (default: Reels/caption.txt)")
    p.add_argument(
        "--prompt", "--instruction", dest="prompt", default=None,
        help="Custom instruction appended to the template",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    p.add_argument(
        "--half", action="store_true",
        help="Only process the first ~half of the video to save API credits. "
             "Useful for long (1-2 hr) episodes where the first half gives "
             "enough context for metadata generation.",
    )
    p.add_argument(
        "--duration", type=int, default=DEFAULT_DURATION_SECONDS,
        help=f"Total video duration in seconds (used with --half to calculate "
             f"the midpoint). Default: {DEFAULT_DURATION_SECONDS}s = 2 hours.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        meta = generate_meta(
            args.youtube_url,
            args.prompt,
            args.model,
            half_video=args.half,
            duration_seconds=args.duration,
        )
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    print(meta, end="" if meta.endswith("\n") else "\n")
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(meta)
    except OSError as e:
        print(f"ERROR: could not write output file {args.output!r}: {e}", file=sys.stderr)
        return 1
    log.info("Saved metadata to %s (%d chars)", args.output, len(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
