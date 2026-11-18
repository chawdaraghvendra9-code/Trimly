# Cliptor Clip Studio

**Turn a long video into vertical 9:16 clips with burned-in captions — on your own machine.**

Cliptor takes a podcast, interview or stream, transcribes it with Whisper, asks an LLM to find the best self-contained moments, and renders each one as a short with animated captions, a hook title and a chosen background style.

No uploads to a clipping service. No watermark. No per-minute billing. Your video never leaves your PC; only transcript text goes to an LLM if you configure one.

<!--
  Add a 15-20 second screen recording here once you have one, then delete this comment:
  ![Cliptor demo](docs/demo.gif)
-->

## Features

- **Local transcription** with faster-whisper (no audio upload)
- **Smart highlight picking** in two passes (chapters, then 5-10 self-contained candidates, top 8 rendered)
- **Offline fallback** if no LLM key is set — audio peaks + sentence boundaries
- **Vertical 1080 × 1920 output** with an ASS caption file and animated word highlighting
- **Five video styles** — Smart fit, Split screen, Blurred, Solid, Gradient
- **Split screen**: clipped speaker on top, your own looping background clip below, captions at the seam
- **Nine caption fonts**, saved per project
- **Per-clip editing**: nudge start/end, change the hook, re-render one clip without touching the others
- **Import from a link**: YouTube, TikTok, Instagram, direct files, public Google Drive / Dropbox links (via yt-dlp)
- **Crash-safe rendering**: ffmpeg args as lists, per-clip timeout, per-clip logs, status per clip

## Requirements

- **Python 3.10 – 3.12**
- **FFmpeg** and **ffprobe** on your `PATH`
- ~1 GB free disk for the Whisper model and job files (the first transcription downloads the model)
- Windows 10/11, macOS or Linux

Optional:

- An OpenAI-compatible LLM key (OpenRouter free models work well) for much better highlight picking
- `cookies.txt` for sites that block anonymous downloads

## Quick start (Windows)

```powershell
git clone https://github.com/chawdaraghvendra9-code/Cliptor.git
cd Cliptor

py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
winget install Gyan.FFmpeg        # skip if `ffmpeg -version` already works

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

## Quick start (macOS / Linux)

```bash
git clone https://github.com/chawdaraghvendra9-code/Cliptor.git
cd Cliptor

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
brew install ffmpeg               # Debian/Ubuntu: sudo apt install ffmpeg

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Configuration

Settings live in `.env` (copied from `.env.example`). Everything has a working default.

| Variable | Default | What it does |
| --- | --- | --- |
| `LLM_API_KEY` | _(empty)_ | API key for highlight picking. Empty = offline fallback |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint (OpenRouter, Groq, LocalAI) |
| `LLM_MODEL` | `gpt-4o-mini` | Model used for chapter + clip selection |
| `LLM_MODELS` | _(same)_ | Comma-separated fallback list tried when a model is slow or rate limited |
| `WHISPER_MODEL` | `base` | `tiny` = fastest, `small`/`medium` = more accurate |
| `MIN_CLIP_SECONDS` | `20` | Shortest clip |
| `MAX_CLIP_SECONDS` | `60` | Longest clip |
| `MAX_VIDEO_MINUTES` | `30` | Reject longer source videos |
| `MAX_FILE_SIZE_MB` | `1000` | Upload limit |
| `YTDLP_MAX_HEIGHT` | `720` | Max download height for link imports |
| `RENDER_TIMEOUT_SECONDS` | `300` | Base per-clip ffmpeg timeout (scales with clip length) |

For OpenRouter, set:

```env
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=meta-llama/llama-3.3-70b-instruct:free
LLM_API_KEY=sk-or-...
```

## How it works

```
upload / link
   ↓
ffprobe validation
   ↓
audio extract (ffmpeg)
   ↓
Whisper transcription with word timestamps
   ↓
pass 1: chapters  →  pass 2: 5-10 clip candidates
   ↓
caption .ass + ffmpeg filter graph per clip
   ↓
9:16 MP4 clips in jobs_data/<job_id>/
```

## Video styles

| Style | What you get |
| --- | --- |
| **Smart fit** (default) | Speaker cropped to 4:5 / 1:1 / 3:4, face-centered static crop, rounded corners, soft shadow, optional slow punch-in, blurred/solid/gradient fill |
| **Split screen** | Speaker clip on top (~58%), your looping background clip below, captions at the seam, optional divider line |
| **Blurred** | Whole clip scaled up and blurred behind a centered crop |
| **Solid** | Chosen color behind the clip |
| **Gradient** | Two-color gradient behind the clip |

The style is saved per project and applied when you re-render a clip.

### Split screen backgrounds

Put your own `.mp4`, `.webm` or `.mov` files in `backgrounds/`, or upload them from the studio page. Limits: **60 seconds** and **100 MB** per file; files are validated with ffprobe and looped to the clip length.

The folder is gitignored, so no stock footage ships with the repo.

> Only use background footage you own or that has a licence allowing reuse.

## Captions

Nine fonts ship in `fonts/` (Anton, Bangers, Bebas Neue, Luckiest Guy, Montserrat ExtraBold, Oswald Bold, Permanent Marker, Press Start 2P, Rubik Black), plus the plain default. Each has its licence in `fonts/licenses/` — all OFL or free-for-use; check before redistributing.

Captions are generated as an escaped `.ass` file per clip, with word-level highlighting for the tuned fonts.

## Importing from a link

Supported: YouTube, TikTok, Instagram, direct video URLs (MP4/MOV/WEBM/MKV), public Google Drive and Dropbox share links. Downloads run through `yt-dlp`; other streaming sites are not supported.

If a site asks you to confirm you are not a bot, export a throwaway `cookies.txt` and set `YTDLP_COOKIES_FILE`. Do **not** point it at your personal Google account.

> Only download and clip content you own or have permission to use.

## Project structure

```
app/
  main.py           FastAPI routes, page rendering, validation
  jobs.py           job state, pipeline orchestration, render options
  video.py          ffmpeg filter graphs, all video styles, probing
  backgrounds.py    split-screen background scan/validate/store
  faces.py          Haar-cascade face detection for Smart fit
  transcription.py  faster-whisper wrapper
  highlights.py     two-pass LLM selection + offline fallback
  subtitles.py      ASS caption generation
  fonts.py          font discovery and per-font tuning
  snapping.py       clip boundary snapping
  downloader.py     yt-dlp link import
templates/          Jinja pages (upload + studio)
static/             stylesheet and upload JS
fonts/              bundled caption fonts + licences
tests/              pytest suite
jobs_data/          per-job files (gitignored)
backgrounds/        your split-screen clips (gitignored)
logs/               per-clip ffmpeg logs (gitignored)
```

## Tests

```powershell
python -m pytest -q
```

140+ tests, including real ffmpeg renders of synthetic backgrounds (1080×1920, H.264, audio stream present). Expect about 10 minutes on a laptop.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ffprobe was not found` | Install FFmpeg and reopen the terminal (`winget install Gyan.FFmpeg`) |
| First transcription is slow | The Whisper model downloads once; later runs are much faster |
| Highlights are weak | Add an `LLM_API_KEY` (OpenRouter free tier is enough) |
| `401` / `403` from the LLM | Wrong key, base URL or model name in `.env` |
| Port 8000 already in use | `--port 8001`, then open `http://127.0.0.1:8001` |
| A clip fails to render | Read `logs/clip_XX.log`; the exact ffmpeg command is logged |
| Split screen is black below | No background found — add a file in `backgrounds/` or pick "No clip (dark)" |

## Roadmap

- One-click installer / desktop build
- Hosted demo (read-only) for people who do not want to install anything
- Auto-reframe with speaker tracking instead of a static crop
- Batch export presets per platform

## Notes

- Keep videos under the configured limit; 4K sources are downscaled, not rejected.
- `jobs_data/`, `logs/`, `backgrounds/` and `.env` are gitignored — they never get pushed.
- This is a local tool: there is no account, no cloud storage and no analytics.
