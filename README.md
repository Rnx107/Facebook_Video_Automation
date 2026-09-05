# Podcast Clips → Facebook Scheduler

Turns your podcast clips into **scheduled Facebook video posts**, twice a day,
with captions written by AI. You run simple terminal commands in order — no web dashboard,
no manual uploads, just automated scheduling.

**What it does, in plain words:**

1. Cleans up messy video filenames (`IMG_9931.mov` → `reel1.mov`, `reel2.mp4`, etc.)
2. Generates captions via Google Gemini AI (reads your full YouTube episode directly — supports bilingual English + Nepali metadata or standard captions)
3. Assigns each clip the next free posting time (7:00 AM / 7:00 PM Nepal time slots)
4. Uploads everything to your Facebook Page and schedules the posts

Facebook then publishes each video automatically at its scheduled time.
Your clips are 5–20 minutes, so they go up as **regular video posts** (not Reels —
Reels only allow ~90 seconds).

---

## One-time setup (do this once)

### 1. Install Python 3.10 or newer

Check if you already have it:

```bash
python3 --version
```

You need version 3.10+. If it's missing, install it from
[python.org](https://www.python.org/downloads/) (tick "Add python to PATH" on Windows).

### 2. Download this folder and open a terminal in it

All commands below run from this folder (the one containing `rename_reels.py`).

### 3. Install the required packages

```bash
pip install -r requirements.txt
```

If `pip` isn't found, try `pip3 install -r requirements.txt`.

### 4. Create your `.env` file (your secret keys)

```bash
cp .env.example .env
```

Now open `.env` in any text editor and fill in your values:

| Value | Where to get it |
|---|---|
| `GEMINI_API_KEY` | Free key from [Google AI Studio](https://aistudio.google.com/apikey) — sign in, click "Create API key" |
| `GEMINI_MODEL` | Gemini model name (default: `gemini-3.6-flash`, or `gemini-2.0-flash`) |
| `FB_PAGE_ID` | Your Facebook Page's ID: open your Page → About, or use [Find my FB ID](https://findmyfbid.in/). Just the number. |
| `FB_PAGE_ACCESS_TOKEN` | A Page token with `pages_manage_posts` and `pages_read_engagement` permission — create it in [Meta Graph API Explorer](https://developers.facebook.com/tools/explorer/) and exchange it for a long-lived Page token (see Troubleshooting below). |

Your `.env` should look like this when done:

```env
FB_PAGE_ID=123456789012345
FB_PAGE_ACCESS_TOKEN=EAAxxxx... (long string)
GEMINI_API_KEY=AIza... (your key)
GEMINI_MODEL=gemini-3.6-flash
```

> Never share your `.env` file or post it anywhere. It's already ignored by git.

---

## Everyday use (the 4 steps, in order)

Put your exported clip files in your clips folder (e.g. `Reels/` or `reels/`), then:

### Step 1 — Tidy the filenames

```bash
python3 rename_reels.py Reels/
```

- **Shows a preview first** (`IMG_9931.mov → reel1.mov`) and asks for confirmation (`y/N`) before renaming anything.
- Oldest export becomes `reel1`, next `reel2`, and so on.
- Non-video files are skipped.
- Every rename is recorded in `rename_log.csv` inside the folder so you can always trace what was what.
- **Tip:** add `--yes` to skip the confirmation prompt for scripts, or `--sort-by name` for alphabetical order instead of export time.

### Step 2 — Write captions using AI

You have two options depending on your podcast format:

#### Option A: Bilingual Episode Metadata (`generate_youtube_meta.py`) — *Recommended*

Generates a complete, ready-to-copy Facebook post formatted with:
1. Curiosity-driven English Title (~70 characters)
2. 3–4 lines of engaging English description
3. 3–4 lines of natural Nepali (Devanagari) description
4. Full YouTube video link (`Full video: <URL>`)
5. 8–12 relevant hashtags (English + Nepali mix)

Run this once for your episode to create a shared `caption.txt`:

```bash
python3 generate_youtube_meta.py "https://youtu.be/YOUR_EPISODE" Reels/caption.txt
```

*(If you omit the second argument, it defaults to `Reels/caption.txt`).*

**Save API credits with `--half`:**
For long 1–2 hour episodes, you can pass `--half` so Gemini only analyzes the first half of the video (sufficient to understand the episode topic and guests):

```bash
python3 generate_youtube_meta.py "https://youtu.be/YOUR_EPISODE" Reels/caption.txt --half --duration 7200
```

#### Option B: Standard English Caption (`generate_caption.py`)

Generates a single paragraph-style English caption + 3–5 hashtags:

```bash
python3 generate_caption.py "https://www.youtube.com/watch?v=YOUR_EPISODE" Reels/reel1.txt
```

#### How Caption Pairing Works

When scheduling in Step 3, `generate_schedule_csv.py` automatically finds captions using this lookup order:
1. **Per-clip caption:** `Reels/reel1.txt` (matches `reel1.mp4`)
2. **Per-clip caption in root:** `./reel1.txt`
3. **Shared caption in folder:** `Reels/caption.txt` (shares one episode caption across all clips in the folder)
4. **Shared caption in root:** `./caption.txt`

> **Workflow Tip:** If all clips in your folder belong to the same podcast episode, you only need to run Step 2 once with `Reels/caption.txt`! Every clip without an individual `.txt` will automatically use this shared caption.

### Step 3 — Assign posting times

```bash
python3 generate_schedule_csv.py --reels Reels/ --csv schedule.csv
```

- New reels with captions get the next free **7:00 AM** / **7:00 PM** Nepal Time (`Asia/Kathmandu`) slot and are appended to `schedule.csv`.
- Existing rows in `schedule.csv` are never modified or reassigned.
- Clips without any matching caption file are skipped with a warning.
- Prints a clear summary of which reel received which date/time slot.

### Step 4 — Upload and schedule on Facebook

```bash
python3 schedule_facebook_posts.py schedule.csv
```

This uploads each video using Facebook's chunked Resumable Upload API and schedules it for its designated time. When finished it prints a summary:

```
Done: 3 scheduled, 0 failed, 5 skipped (already scheduled).
```

- `scheduled` — done, Facebook will publish it automatically at its time.
- `failed` — check `errors.log` for the exact reason, fix it, and re-run.
- `skipped` — already scheduled earlier; never posted twice.

**Re-running is always safe.** Steps 1, 3, and 4 skip work that's already done, so you can safely re-run them whenever new clips arrive.

---

## Files in this folder

| File | What it is |
|---|---|
| `rename_reels.py` | Step 1 — normalize clip filenames (`reel1.mp4`, `reel2.mov`) |
| `generate_youtube_meta.py` | Step 2 (Option A) — bilingual English/Nepali metadata generator (supports `--half`) |
| `generate_caption.py` | Step 2 (Option B) — standard English caption generator |
| `generate_schedule_csv.py` | Step 3 — match captions & assign 7:00 AM / 7:00 PM NPT slots |
| `schedule_facebook_posts.py` | Step 4 — chunked resumable upload & scheduling on Facebook |
| `schedule.csv` | Your posting plan (created by Step 3, updated by Step 4) |
| `caption.txt` / `Reels/caption.txt` | Default output path for shared episode captions |
| `errors.log` | Detailed error reasons for any failed rows during upload |
| `requirements.txt` | Python library dependencies |
| `.env` | Your secret API keys (you create this; never share it) |
| `.env.example` | Blank template for `.env` |

---

## Troubleshooting

**"GEMINI_API_KEY is not set"** — you skipped setup step 4. Copy `.env.example`
to `.env` and paste your key.

**"FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN is not set"** — fill in the Facebook values in `.env`.

**"Gemini rejected the YouTube URL"** — the video must be **public** (not
private or unlisted). YouTube-input is a Gemini preview feature, so ensure the link
is reachable and valid.

**"Gemini quota/rate limit hit (code=429)"** — you've hit your API limit. If you
are processing 1–2 hour episodes, use `--half` with `generate_youtube_meta.py` to
halve video token consumption, or wait for your quota to reset.

**"scheduled_datetime is too soon"** — Facebook needs at least **10 minutes**
notice. Pick a later time (Step 3 handles this automatically).

**"scheduled_datetime is too far in future"** — Facebook video scheduling allows up to
**6 months (~182 days)** in advance.

**"duplicate scheduled_datetime"** — two clips wanted the exact same minute.
Change one time in `schedule.csv` and re-run Step 4.

**"refusing to rename ... already exists"** — the folder was partly renamed
before. Rename leftover files by hand (check `rename_log.csv`), then re-run Step 1.

**Facebook token expired / "Invalid OAuth token"** — Page tokens expire. Make a
new one in [Graph API Explorer](https://developers.facebook.com/tools/explorer/),
choose your Page, add `pages_manage_posts` and `pages_read_engagement`, generate,
and use the "Extend Access Token" debugger at
[developers.facebook.com/tools/debug/accesstoken](https://developers.facebook.com/tools/debug/accesstoken/)
to get a long-lived one. Paste it into `.env`.

**"video file not found"** — the `video_path` in `schedule.csv` is wrong or the
file was moved. Paths are relative to where you run the command (e.g.
`Reels/reel1.mp4` on Linux is case-sensitive).
