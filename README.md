# Podcast Clips → Facebook Scheduler

Turns your podcast clips into **scheduled Facebook video posts**, twice a day,
with captions written by AI. You run 4 small commands in order — no website, no app,
just your terminal.

**What it does, in plain words:**

1. Cleans up messy video filenames (`IMG_9931.mov` → `reel1.mov`)
2. Writes a Facebook caption for each clip using AI (reads your full episode on YouTube)
3. Assigns each clip the next free posting time (7:00 AM / 7:00 PM Nepal time)
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

Now open `.env` in any text editor and fill in these 3 values:

| Value | Where to get it |
|---|---|
| `GEMINI_API_KEY` | Free key from [Google AI Studio](https://aistudio.google.com/apikey) — sign in, click "Create API key" |
| `FB_PAGE_ID` | Your Facebook Page's ID: open your Page → About, or use [Find my FB ID](https://findmyfbid.in/). Just the number. |
| `FB_PAGE_ACCESS_TOKEN` | A Page token with `pages_manage_posts` permission — create it in [Meta Graph API Explorer](https://developers.facebook.com/tools/explorer/): select your app, click "Generate Access Token", add the permission, then exchange it for a long-lived Page token (see Troubleshooting below). |

Your `.env` should look like this when done:

```
FB_PAGE_ID=123456789012345
FB_PAGE_ACCESS_TOKEN=EAAxxxx... (long string)
GEMINI_API_KEY=AIza... (your key)
```

> Never share your `.env` file or post it anywhere. It's already ignored by git.

---

## Everyday use (the 4 steps, in order)

Put your exported clip files in the `reels/` folder first (create it if needed),
then:

### Step 1 — Tidy the filenames

```bash
python3 rename_reels.py reels/
```

It **shows you a preview first** (`IMG_9931.mov → reel1.mov`) and asks for
confirmation before renaming anything. Oldest export becomes `reel1`, next `reel2`,
and so on. Non-video files are skipped. Every rename is recorded in
`reels/rename_log.csv` so you can always trace what was what.

Tip: add `--yes` to skip the confirmation question (for scripts).

### Step 2 — Write a caption for each clip (one command per clip)

```bash
python3 generate_caption.py "https://www.youtube.com/watch?v=YOUR_EPISODE" reels/reel1.txt
```

This sends the full YouTube episode to Google's AI and saves a Facebook caption
(+ hashtags) to `reels/reel1.txt`. Repeat for each new reel, always naming the
caption after its video (`reel2.mp4` → `reel2.txt`) — that's how the next step
finds them automatically.

### Step 3 — Assign posting times

```bash
python3 generate_schedule_csv.py --reels reels/ --csv schedule.csv
```

New reels with captions get the next free 7:00 AM / 7:00 PM (Nepal time) slot and
are added to `schedule.csv`. It prints what got which slot — glance at it before
continuing. Clips without a caption are skipped with a warning.

### Step 4 — Upload and schedule on Facebook

```bash
python3 schedule_facebook_posts.py schedule.csv
```

This uploads each video and schedules it. When finished it prints a summary like:

```
Done: 3 scheduled, 0 failed, 5 skipped (already scheduled).
```

- `scheduled` — done, Facebook will publish it at its time.
- `failed` — check `errors.log` for the reason, fix it, re-run.
- `skipped` — already scheduled earlier; never posted twice.

**Re-running is always safe.** Steps 1, 3 and 4 skip work that's already done,
so just re-run them whenever new clips arrive.

---

## Files in this folder

| File | What it is |
|---|---|
| `rename_reels.py` | Step 1 — tidy filenames |
| `generate_caption.py` | Step 2 — AI captions (needs `GEMINI_API_KEY`) |
| `generate_schedule_csv.py` | Step 3 — assign 7am/7pm slots |
| `schedule_facebook_posts.py` | Step 4 — upload + schedule (needs `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN`) |
| `schedule.csv` | Your posting plan (created by Step 3, updated by Step 4) |
| `errors.log` | Reasons for any failed rows |
| `reels/rename_log.csv` | Record of every filename change |
| `.env` | Your secret keys (you create this; never share it) |
| `.env.example` | Blank template for `.env` |

---

## Troubleshooting

**"GEMINI_API_KEY is not set"** — you skipped setup step 4. Copy `.env.example`
to `.env` and paste your key.

**"FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN is not set"** — same fix, fill in the
Facebook values in `.env`.

**"Gemini rejected the YouTube URL"** — the video must be **public** (not
private/unlisted). YouTube-input is a Gemini preview feature, so this error can
also mean Google changed something — the message says so explicitly.

**"scheduled_datetime is too soon"** — Facebook needs at least **10 minutes**
notice. Pick a later time (Step 3 handles this automatically).

**"duplicate scheduled_datetime"** — two clips wanted the exact same minute.
Change one time in `schedule.csv` and re-run Step 4.

**"refusing to rename ... already exists"** — the folder was partly renamed
before. Rename the leftover files by hand (check `rename_log.csv`), then re-run
Step 1.

**Facebook token expired / "Invalid OAuth token"** — Page tokens expire. Make a
new one in [Graph API Explorer](https://developers.facebook.com/tools/explorer/),
choose your Page, add `pages_manage_posts`, generate, and use the
"Extend Access Token" debugger at
[developers.facebook.com/tools/debug/accesstoken](https://developers.facebook.com/tools/debug/accesstoken/)
to get a long-lived one. Paste it into `.env`.

**"video file not found"** — the `video_path` in `schedule.csv` is wrong or the
file was moved. Paths are relative to where you run the command, e.g.
`reels/reel1.mp4` works when you run from this folder.
