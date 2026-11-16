# Cliptor Clip Studio: handoff

## What it is
Local web app: upload a video, transcribe it, an LLM suggests short clips, render vertical 9:16 clips with burned-in captions.

## Stack
Python, FastAPI (uvicorn), faster-whisper, ffmpeg, plain HTML/JS. Highlights come from an LLM through OpenRouter free models (settings in .env: OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODELS). Windows 10/11.

## Run
[exact command I use, e.g. uvicorn main:app --reload] then open [http://127.0.0.1:8000]

## Status
- Stage 1 upload: done
- Stage 2 transcription with timestamps: done
- Stage 3 clip suggestions: done. Quality fixes (full transcript instead of chunks, two-pass topics, validation pass): [applied / not applied]
- Stage 4-5 rendering: in progress. Rendering got stuck on clip 5 and a 10-minute video took 15+ minutes. [add any other details]

## Pending tasks (in this order)
1. Make rendering robust: ffmpeg with -y and -nostdin, arguments as a list, stderr written to logs/clip_XX.log, per-clip timeout, per-clip status in the UI (queued, rendering, done, failed with retry), captions as an escaped .ass file with correct Windows path handling, clamp clip end times to video duration, log the exact ffmpeg command.
2. Add timing logs for every stage and show them in the UI.
3. Speed up transcription: faster-whisper with vad_filter=True, beam_size=1, default model "base".

## Rules
- The owner is a beginner: keep code simple with short comments.
- Only change what the current task asks. Show a short plan before big changes.
- Never put user input into shell strings.
- Do not open, read or edit uploads/, jobs/, logs/, .env or any video file.
- Known harmless log line: GET /health 404.