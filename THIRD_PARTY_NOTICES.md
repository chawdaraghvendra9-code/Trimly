# Third-Party Notices

Trimly Clip Studio includes or depends on the projects below. This list is
provided for convenience; the licence files shipped with each project are the
authoritative terms.

Trimly does **not** bundle FFmpeg. Users install FFmpeg separately, and its
licence depends on the build they install (often LGPL-2.1+ or GPL-2.0+).

## Bundled fonts (`fonts/`)

| Font | Licence | Licence file |
| --- | --- | --- |
| Anton | SIL Open Font License 1.1 | `fonts/licenses/Anton-OFL.txt` |
| Bangers | SIL Open Font License 1.1 | `fonts/licenses/Bangers-OFL.txt` |
| Bebas Neue | SIL Open Font License 1.1 | `fonts/licenses/BebasNeue-OFL.txt` |
| Luckiest Guy | Apache License 2.0 | `fonts/licenses/LuckiestGuy-LICENSE.txt` |
| Montserrat ExtraBold | SIL Open Font License 1.1 | `fonts/licenses/Montserrat-OFL.txt` |
| Oswald Bold | SIL Open Font License 1.1 | `fonts/licenses/Oswald-OFL.txt` |
| Permanent Marker | Apache License 2.0 | `fonts/licenses/PermanentMarker-LICENSE.txt` |
| Press Start 2P | SIL Open Font License 1.1 | `fonts/licenses/PressStart2P-OFL.txt` |
| Rubik Black | SIL Open Font License 1.1 | `fonts/licenses/Rubik-OFL.txt` |

## Python dependencies (`requirements.txt`)

| Package | Licence |
| --- | --- |
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| Jinja2 | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| faster-whisper | MIT |
| CTranslate2 | MIT |
| PyAV (`av`) | BSD-3-Clause |
| onnxruntime | MIT |
| tokenizers | Apache-2.0 |
| huggingface-hub | Apache-2.0 |
| NumPy | BSD-3-Clause |
| requests | Apache-2.0 |
| urllib3 | Apache-2.0 |
| yt-dlp | Unlicense |
| fonttools | MIT |
| opencv-python-headless / OpenCV | Apache-2.0 |
| pytest | MIT |
| tqdm | MPL-2.0 and MIT |

Other transitive dependencies keep the licences declared in their own packages.

## Models and external tools

- **OpenAI Whisper model weights** (downloaded by faster-whisper): MIT.
- **FFmpeg / ffprobe**: installed separately, not bundled. See the licence of
  the build you install.
- **Node.js** (optional, streaming-site imports only): installed separately.

## AI services

If you configure an API key, transcript text is sent to the AI service you
choose (for example OpenRouter). That service's own terms and privacy policy
apply. Trimly is not affiliated with OpenRouter or any model provider.
