# Trimly Clip Studio

**Turn a long video into vertical 9:16 clips with burned-in captions — on your own PC.**

Trimly transcribes a podcast, interview or stream with Whisper, finds the best
self-contained moments, and renders each one as a short with animated captions,
an optional hook title and a background style you choose. No account, no
watermark and no per-minute billing.

**Your video and audio stay on your computer.** Only the transcript text is sent
to an AI service, and only when you configure an API key. With no key, Trimly
runs in **Basic suggestions mode** completely offline.

> Only use content you own or have permission to clip.

Feedback and questions: [Telegram group](https://t.me/+d6DQbGgQaW1jYmI1)

---

## Screenshots

> 📷 **Placeholder:** add `docs/screenshot-upload.png` here before publishing.
>
> 📷 **Placeholder:** add `docs/screenshot-studio.png` here before publishing.
>
> 📷 **Placeholder:** add `docs/screenshot-export.png` here before publishing.

Suggested shots: the upload page, the studio with clip cards, and a finished
9:16 clip with captions.

---

## Requirements

- **Windows 10 or 11** (64-bit)
- **Python 3.11, 3.12 or 3.13** (the `uv` steps below fetch Python 3.12 for you)
- **FFmpeg + ffprobe** on your `PATH`
- About **1 GB free disk** for the Whisper model and temporary job files
- Internet access on first run to download the Whisper model

Optional:

- An **OpenRouter** (or other OpenAI-compatible) API key for smarter highlights
- **Node.js 22+** only if you enable streaming-site imports (`ENABLE_YOUTUBE_IMPORT`)

FFmpeg is **not** bundled with Trimly. It is installed separately on your PC.

---

## Install as a command-line app (uv, recommended)

This installs `trimly` as a normal command you can run from any folder.
Replace `USERNAME` with the GitHub account that hosts this repository.

```powershell
winget install astral-sh.uv
winget install Gyan.FFmpeg
uv tool install --python 3.12 git+https://github.com/USERNAME/trimly.git
trimly
```

The last command starts the app on `http://127.0.0.1:8000` and opens your
browser. Keep the terminal window open while you work; press `Ctrl+C` to stop.

First run downloads the Whisper speech model once (there is a progress bar).
After that, Trimly works offline unless you add an AI key.

Useful commands:

```powershell
trimly                 # start on port 8000
trimly --port 8001     # use another port (the next free one if it is busy)
trimly setup           # save an optional OpenRouter API key and model list
trimly doctor          # check Python, FFmpeg, the AI key and the Whisper model
trimly --version       # print the version
```

Update later:

```powershell
uv tool upgrade trimly
```

If `trimly` is not found after installing, add uv's tool folder to your PATH
once and open a new terminal:

```powershell
uv tool update-shell
```

### Alternative: pipx

```powershell
py -3.12 -m pip install --user pipx
py -3.12 -m pipx ensurepath
pipx install --python 3.12 "git+https://github.com/USERNAME/trimly.git"
trimly
```

Update with `pipx upgrade trimly`, or reinstall with
`pipx reinstall --python 3.12 trimly`.

### Where installed builds keep files

Nothing is written inside the installed package folder.

| Item | Location |
| --- | --- |
| Settings | `%APPDATA%\Trimly\config.env` |
| Jobs, uploads, rendered clips | `%LOCALAPPDATA%\Trimly\jobs_data` |
| Logs (`trimly.log`, `logs\clip_XX.log`) | `%LOCALAPPDATA%\Trimly` and `...\logs` |
| Whisper models | `%LOCALAPPDATA%\Trimly\models` |
| Split-screen background clips | `%LOCALAPPDATA%\Trimly\backgrounds` |

Environment variables override `config.env`, so `LLM_API_KEY=...` in your
terminal always wins. `TRIMLY_DATA_DIR` and `TRIMLY_CONFIG_FILE` can move the
folders if you need to.

---

## Install from a ZIP (no command line)

1. Install Python 3.12 from <https://www.python.org/downloads/> and tick
   **"Add python.exe to PATH"**. (3.11 and 3.13 also work.)
2. Download and unzip the Trimly release folder.
3. Double-click **`setup.bat`**. It will:
   - check for Python 3.11–3.13 through the `py` launcher
   - create a `venv\` folder and install `requirements.txt`
   - check for FFmpeg and offer `winget install Gyan.FFmpeg`
   - ask for an OpenRouter API key (optional — press Enter to skip)
4. Double-click **`run.bat`**. The browser opens at
   <http://127.0.0.1:8000>.

Keep the black command window open while you use Trimly. Press `Ctrl+C` there
to stop the server.

> 📷 **Screenshot placeholder:** `docs/screenshot-setup.png`

### Manual install from the source folder (advanced)

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
winget install Gyan.FFmpeg   # skip if ffmpeg -version already works
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Running from the source folder keeps the old behaviour: data stays in
`jobs_data\`, `logs\` and `backgrounds\`, and settings are read from `.env`.

---

## How to use

1. **Add a video** — drag a file onto the upload box or click to browse.
   Supported: MP4, MOV, MKV, AVI, WEBM.
2. **Or import from a link** — direct MP4/MOV/WEBM/MKV links and public Google
   Drive or Dropbox share links work out of the box. Streaming sites are off by
   default (see `ENABLE_YOUTUBE_IMPORT`).
3. **Wait for processing** — Trimly extracts audio, transcribes it, suggests
   clips and renders each one. Processing takes roughly as long as the video.
4. **Review the clips** — in the studio you can nudge start/end times, change the
   hook title, pick a caption font and choose a video style.
5. **Download** — grab one clip or all clips as a ZIP.

The first transcription downloads the Whisper model once. Later jobs are faster.

### Basic suggestions mode

If `LLM_API_KEY` is empty, Trimly still works: it picks clips from audio
loudness and natural sentence boundaries and shows a banner in the app. Add a
key to `.env` for much better highlights:

```env
LLM_API_KEY=sk-or-...
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=meta-llama/llama-3.3-70b-instruct:free
```

Restart Trimly after changing the settings. Installed builds can save the key
for you with `trimly setup`; the source/ZIP build edits `.env`.

---

## Privacy

- Uploaded videos, extracted audio, transcripts and rendered clips stay in the
  local `jobs_data\` folder (`%LOCALAPPDATA%\Trimly\jobs_data` when installed).
  Nothing is uploaded to a Trimly server.
- **Only the transcript text is sent to the AI service you choose**, and only
  when `LLM_API_KEY` is set. The video and audio never leave your PC.
- With no key, no transcript leaves your PC at all.
- Link imports contact the site you paste (and Google Drive / Dropbox when used).
- There is no analytics, no account and no cloud storage.

Trimly's local logs (`trimly.log`, `logs\clip_XX.log`) are only on your machine.

---

## Content note

Only upload, download and clip content you own or have permission to clip.
Respect copyright, platform terms and other people's privacy. You are
responsible for what you publish.

---

## Configuration

All settings live in `.env` in the source/ZIP build, or in
`%APPDATA%\Trimly\config.env` when installed (`trimly setup` writes it). Every
value has a working default, so an empty file still runs. Environment variables
override the file.

| Setting | Default | What it does |
| --- | --- | --- |
| `LLM_API_KEY` | _(empty)_ | AI key. Empty = Basic suggestions mode |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint |
| `LLM_MODEL` | `meta-llama/llama-3.3-70b-instruct:free` | Model used for highlight picking |
| `LLM_MODELS` | _(same as LLM_MODEL)_ | Reserved; only `LLM_MODEL` is used today |
| `LLM_TIMEOUT` | `60` | Reserved; request timeout is fixed in code |
| `WHISPER_MODEL` | `base` | `tiny` fastest, `small`/`medium` more accurate |
| `TRANSCRIPTION_MODE` | `local` | `local` only; hosted mode is not implemented yet |
| `TRANSCRIPTION_API_BASE` | _(LLM base URL)_ | Reserved for future hosted transcription |
| `TRANSCRIPTION_API_KEY` | _(LLM key)_ | Reserved for future hosted transcription |
| `TRANSCRIPTION_MODEL` | `whisper-1` | Reserved for future hosted transcription |
| `MIN_CLIP_SECONDS` | `20` | Shortest clip |
| `PREFERRED_MIN_SECONDS` | `30` | Preferred lower bound |
| `PREFERRED_MAX_SECONDS` | `90` | Preferred upper bound |
| `MAX_CLIP_SECONDS` | `120` | Hard maximum clip length |
| `MAX_VIDEO_MINUTES` | `30` | Reject longer source videos |
| `MAX_FILE_SIZE_MB` | `1000` | Upload / download limit |
| `DEFAULT_CROP_STYLE` | `blurred_background` | Default vertical layout |
| `RENDER_TIMEOUT_SECONDS` | `300` | Base ffmpeg timeout per clip |
| `ENABLE_YOUTUBE_IMPORT` | `false` | Allow YouTube/TikTok/Instagram links |
| `YTDLP_MAX_HEIGHT` | `720` | Max height for streaming imports |
| `YTDLP_TIMEOUT_SECONDS` | `1800` | Max seconds per link download |
| `YTDLP_COOKIES_FILE` | _(empty)_ | Optional cookies.txt for blocked sites |
| `YTDLP_COOKIES_FROM_BROWSER` | _(empty)_ | Optional browser cookie source |

> Warning: do not point cookie settings at your main personal Google account.
> Use a throwaway account if a site asks you to prove you are not a bot.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| **`trimly` is not recognized** | Close and reopen the terminal, or run `uv tool update-shell` (pipx: `pipx ensurepath`). Check with `uv tool list`. |
| **Python 3.11–3.13 is missing** | Install Python 3.12 from python.org, or reinstall with `uv tool install --python 3.12 --force git+https://github.com/USERNAME/trimly.git`. |
| **`ffmpeg` / `ffprobe` was not found** | Run `winget install Gyan.FFmpeg`, then open a new terminal. `trimly doctor` shows the exact status. |
| **Port 8000 is already in use** | Close the other Trimly window, or start with `trimly --port 8001` (it also picks the next free port automatically). |
| **First transcription is very slow or the model download fails** | `trimly doctor` checks the model. It downloads once (needs internet); `WHISPER_MODEL=tiny` is the fastest. |
| **A clip fails to render, or "No speech detected"** | Open `%LOCALAPPDATA%\Trimly\logs\clip_XX.log` (source build: `logs\clip_XX.log`) for the ffmpeg error. Make sure the video has speech and `ffmpeg -version` works. |

Still stuck? Ask in the [Telegram group](https://t.me/+d6DQbGgQaW1jYmI1).

---

## Project structure

```
pyproject.toml       package metadata, `trimly` command, bundled assets
setup.bat            Windows first-time setup (ZIP build)
run.bat              start the local server and open the browser (ZIP build)
app/                 FastAPI routes, CLI, job runner, ffmpeg, transcription, highlights
app/cli.py           trimly / trimly setup / trimly doctor
app/paths.py         source vs installed paths (assets, jobs, logs, models)
templates/           upload page and studio page
static/              stylesheet and upload JavaScript
fonts/               bundled caption fonts + licences
tests/               pytest suite (developers)
jobs_data/           local job files in source mode (gitignored, never published)
logs/                per-clip ffmpeg logs in source mode (gitignored)
```

---

## License

MIT. See `LICENSE`. Bundled fonts and dependencies keep their own licences; see
`THIRD_PARTY_NOTICES.md`. FFmpeg is not bundled.
