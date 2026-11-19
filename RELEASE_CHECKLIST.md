# Public release checklist

## 1. Never publish these

| Path / item | Why | Status |
| --- | --- | --- |
| `.env` | Holds your real API key | gitignored, never tracked |
| `jobs_data/` | User videos, audio, transcripts, clips | gitignored (except `.gitkeep`) |
| `logs/`, `trimly.log`, `cliptor.log` | Personal paths and ffmpeg output | gitignored |
| `venv/` | Machine-specific Python environment | gitignored |
| `backgrounds/` | User background footage | gitignored |
| `uploads/` | Old uploaded files | gitignored |
| `cookies.txt` | Can contain account cookies | gitignored |
| `sample_video.mp4` | Generated test media | untracked + gitignored |
| `docs/cliptor-promo.mp4` | Demo video, not needed in the source zip | gitignored |
| `AGENTS.md` | Internal handoff notes | excluded from `git archive` |
| `docs/*.png` screenshots | May show filenames / personal content | **review by eye before committing** |

`sample_video.mp4` was tracked in older history. It is synthetic (made by
`create_sample_video.py`), but if you ever added real footage, purge history
before making the repository public.

**Important:** old commits also tracked `cliptor.log` (removed later). Logs can
contain local paths and request details. Before publishing the repository, either
use a fresh history (recommended) or purge those files with `git filter-repo`.
If any real API key was ever pasted into a log, revoke it at the provider.

## 2. Before you build the zip

- [ ] `git status` shows only files you intend to publish.
- [ ] No `.env`, `venv/`, `jobs_data/`, `logs/`, video or log file is staged.
- [ ] `LICENSE` is replaced with a real licence.
- [ ] Repository history is clean, or you publish from a fresh orphan branch.
- [ ] Screenshots are checked for personal names, paths or API keys.
- [ ] `.env.example` has no real key.
- [ ] README install steps work on a clean Windows machine (see below).
- [ ] `pytest` passes locally.
- [ ] App starts with no API key and shows the Basic suggestions banner.
- [ ] `ENABLE_YOUTUBE_IMPORT=false` hides YouTube text and rejects YouTube links.
- [ ] Direct MP4 and Google Drive / Dropbox links still work.
- [ ] `THIRD_PARTY_NOTICES.md` is present and `fonts/licenses/` is included.

## 3. Test commands

```powershell
# Activate the project environment
.\venv\Scripts\Activate.ps1

# Fast checks (no real ffmpeg renders)
python -m pytest tests/test_import.py tests/test_fonts.py -q

# Full suite (slow: real ffmpeg renders)
python -m pytest -q
```

Manual smoke test:

```powershell
Copy-Item .env.example .env -Force
.\run.bat
```

Open <http://127.0.0.1:8000>, upload a short clip, and confirm processing.
Then remove the key from `.env` and confirm the banner appears.

## 4. Build the release ZIP

Commit first — `git archive` only includes committed files:

```powershell
git add -A
git commit -m "Prepare Trimly Clip Studio public release"
git tag v1.0.0

git archive --format=zip `
  --prefix=Trimly-Clip-Studio-1.0.0/ `
  -o "$env:USERPROFILE\Desktop\Trimly-Clip-Studio-1.0.0.zip" `
  HEAD
```

If you want a truly clean public history (recommended, because old commits
contain `cliptor.log` and `sample_video.mp4`), create a fresh branch instead:

```powershell
git checkout --orphan public-release
git add -A
git commit -m "Trimly Clip Studio 1.0.0"
git tag v1.0.0
```

Then use `public-release` as `HEAD` for the commands above.

`AGENTS.md` and `sample_video.mp4` are skipped by `.gitattributes`. `.env`,
`venv/`, `jobs_data/` and `logs/` are untracked, so they are never included.

Verify the archive contents:

```powershell
tar -tf "$env:USERPROFILE\Desktop\Trimly-Clip-Studio-1.0.0.zip"
```

The list must **not** contain `.env`, `venv/`, `jobs_data/`, `logs/`,
`*.mp4`, `AGENTS.md` or `trimly.log`.

## 5. Publish on GitHub

```powershell
git push origin main
git push origin v1.0.0

gh release create v1.0.0 `
  "$env:USERPROFILE\Desktop\Trimly-Clip-Studio-1.0.0.zip" `
  --title "Trimly Clip Studio v1.0.0" `
  --notes "First public Windows release. See README for setup, privacy and troubleshooting."
```

## 6. Clean-machine test

1. Use a fresh Windows 10/11 PC or a new local Windows user.
2. Install Python 3.12 (or let `setup.bat` offer it) and unzip the release.
3. Double-click `setup.bat`: venv, packages, FFmpeg, optional key.
4. Double-click `run.bat`: browser opens and the upload page loads.
5. Upload a 30–60 second clip with speech and wait for rendered clips.
6. Repeat with no API key: the Basic suggestions banner must be visible.
7. Try a direct MP4 link and a public Drive/Dropbox link.
8. Try a YouTube link with the default `ENABLE_YOUTUBE_IMPORT=false`: it must be
   rejected with a friendly message, with no YouTube wording in the upload UI.
9. Delete `venv/` and confirm `setup.bat` rebuilds it without errors.
