"""
Small helper used by setup.bat.

Creates .env from .env.example when it is missing, then optionally writes the
OpenRouter API key the user pasted. The key is never printed back out.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_FILE = BASE_DIR / ".env.example"
ENV_FILE = BASE_DIR / ".env"


def ensure_env_file() -> None:
    """Copy .env.example to .env the first time setup runs."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")


def set_setting(lines, name: str, value: str):
    """Replace NAME=... in place, or add it at the end when it is missing."""
    prefix = name + "="
    replaced = False
    output = []
    for line in lines:
        if line.strip().startswith(prefix):
            output.append(f"{name}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{name}={value}")
    return output


def main() -> None:
    key = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    ensure_env_file()

    if key:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        lines = set_setting(lines, "LLM_API_KEY", key)
        # These two are needed for an OpenRouter key to work out of the box.
        if not any(line.strip().startswith("LLM_BASE_URL=") and line.split("=", 1)[1].strip()
                   for line in lines):
            lines = set_setting(lines, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
        if not any(line.strip().startswith("LLM_MODEL=") and line.split("=", 1)[1].strip()
                   for line in lines):
            lines = set_setting(
                lines, "LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free"
            )
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Saved your OpenRouter key to .env (AI suggestions enabled).")
    else:
        print("No key saved. Trimly will start in Basic suggestions mode.")


if __name__ == "__main__":
    main()
