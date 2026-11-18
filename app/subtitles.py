"""
Subtitle generation module for Cliptor Clip Studio.
Generates Advanced SubStation Alpha (.ass) subtitle files with 2-3 words per
chunk, a configurable caption font style, and a time offset for the clip.

Pass font_style=None for the original "Plain" look (Arial). Every other look
comes from app.fonts, which scans the fonts/ folder with fontTools.
"""
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.fonts import get_font_style

# Original caption size at PlayRes 1080x1920. Styles scale this number.
BASE_FONT_SIZE = 66

# Hook title: big text at the top for the first few seconds.
HOOK_FONT_SCALE = 1.15
HOOK_TOP_MARGIN = 230  # about 12% down from the top of a 1920px frame
HOOK_FADE_MS = 300

# Captions sit in the lower third (the Bold style uses MarginV 500, about 74%).
CAPTION_SAFE_MARGIN_V = 500

# Thin progress bar at the very bottom, drawn with ASS vector commands.
PROGRESS_HEIGHT = 10
PROGRESS_STEPS = 60


def format_ass_time(seconds: float) -> str:
    """Formats seconds into ASS timestamp format: H:MM:SS.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _escape_ass_text(text: str) -> str:
    """Keeps user words from being read as ASS override tags."""
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _ass_color(value: str, fallback: str = "#38BDF8") -> str:
    """'#RRGGBB' -> ASS '&HBBGGRR&'. Invalid values use the fallback color."""
    text = value if isinstance(value, str) else ""
    valid = (
        len(text) == 7
        and text.startswith("#")
        and all(ch in "0123456789abcdefABCDEF" for ch in text[1:])
    )
    if not valid:
        text = fallback
    red, green, blue = text[1:3], text[3:5], text[5:7]
    return f"&H{blue}{green}{red}&".upper()


def _ass_style_line(style: Dict[str, Any]) -> str:
    """Builds the V4+ Style line for one look."""
    font_size = max(10, round(BASE_FONT_SIZE * float(style.get("size_multiplier", 1.0))))
    bold = -1 if style.get("bold") else 0
    outline = float(style.get("outline", 5.0))
    shadow = float(style.get("shadow", 0.0))
    # Never let captions drop into the Reels/TikTok interface area.
    margin_v = max(int(style.get("margin_v", 260)), CAPTION_SAFE_MARGIN_V)

    return (
        "Style: Default,"
        f"{style.get('family', 'Arial')},{font_size},"
        "&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"  # white, yellow, black, shadow
        f"{bold},0,0,0,100,100,0,0,1,{outline:g},{shadow:g},2,90,90,{margin_v},1"
    )


def _hook_style_line(style: Dict[str, Any]) -> str:
    """Top-aligned, slightly larger version of the caption font."""
    font_size = max(
        12,
        round(BASE_FONT_SIZE * float(style.get("size_multiplier", 1.0)) * HOOK_FONT_SCALE),
    )
    bold = -1 if style.get("bold") else 0
    outline = float(style.get("outline", 5.0))
    shadow = float(style.get("shadow", 0.0))

    return (
        "Style: Hook,"
        f"{style.get('family', 'Arial')},{font_size},"
        "&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        f"{bold},0,0,0,100,100,0,0,1,{outline:g},{shadow:g},8,90,90,"
        f"{HOOK_TOP_MARGIN},1"
    )


def _chunk_lines(
    chunk: Dict[str, Any],
    style: Dict[str, Any],
) -> Iterator[Tuple[float, float, str]]:
    """
    Yields (start, end, text) for one chunk.

    Plain looks use one line per chunk. Looks with highlight=True use one line
    per spoken word, so only the current word is yellow while the two or three
    words of the chunk stay on screen.
    """
    words = chunk["words"]

    if not style.get("highlight"):
        text = " ".join(w["display"] for w in words)
        yield chunk["start"], chunk["end"], _escape_ass_text(text)
        return

    for index, word in enumerate(words):
        start = chunk["start"] if index == 0 else word["start"]
        end = words[index + 1]["start"] if index + 1 < len(words) else chunk["end"]
        if end <= start:
            end = start + 0.05

        parts: List[str] = []
        for other_index, other in enumerate(words):
            token = _escape_ass_text(other["display"])
            if other_index == index:
                # Yellow for the word being spoken, then back to white.
                parts.append(f"{{\\c&H0000FFFF&}}{token}{{\\c&H00FFFFFF&}}")
            else:
                parts.append(token)
        yield start, end, " ".join(parts)


def generate_ass_subtitles_for_clip(
    transcript_segments: List[Dict[str, Any]],
    clip_start: float,
    clip_end: float,
    output_ass_path: Path,
    font_style: Optional[Dict[str, Any]] = None,
    hook_title: Optional[str] = None,
    hook_duration: float = 3.0,
    progress_bar: bool = False,
    progress_color: str = "#38BDF8",
) -> Path:
    """
    Extracts word timestamps falling between clip_start and clip_end,
    offsets them relative to clip_start (so clip starts at 0:00),
    groups into 2 to 3 words per subtitle chunk, and writes out a .ass file.

    font_style is a dict from app.fonts.get_font_style(); None keeps the
    original Plain look. hook_title adds an optional top headline for the first
    hook_duration seconds (clamped to the clip length). progress_bar adds a thin
    bottom bar that fills over the clip.
    """
    style = font_style or get_font_style("plain")
    uppercase = bool(style.get("uppercase"))

    # 1. Collect all words within the clip range
    clip_words: List[Dict[str, Any]] = []
    for seg in transcript_segments:
        words = seg.get("words", [])
        if words:
            for w in words:
                w_start = float(w["start"])
                w_end = float(w["end"])
                # Check if word falls in clip window (with slight tolerance)
                if w_end > (clip_start - 0.05) and w_start < (clip_end + 0.05):
                    clip_words.append({
                        "word": str(w["word"]).strip(),
                        "start": max(0.0, w_start - clip_start),
                        "end": max(0.05, w_end - clip_start)
                    })
        else:
            # Fallback if segment didn't have word breakdown
            s_start = float(seg["start"])
            s_end = float(seg["end"])
            if s_end > clip_start and s_start < clip_end:
                words_in_seg = seg["text"].strip().split()
                if words_in_seg:
                    dur_per_word = max(0.1, (s_end - s_start) / len(words_in_seg))
                    for idx, txt in enumerate(words_in_seg):
                        w_s = s_start + idx * dur_per_word
                        w_e = w_s + dur_per_word
                        if w_e > clip_start and w_s < clip_end:
                            clip_words.append({
                                "word": txt,
                                "start": max(0.0, w_s - clip_start),
                                "end": max(0.05, w_e - clip_start)
                            })

    # Sort words by start time
    clip_words.sort(key=lambda w: w["start"])

    # Apply the look's casing once, then use "display" everywhere below.
    for word in clip_words:
        word["display"] = word["word"].upper() if uppercase else word["word"]

    # 2. Group into chunks of 2 to 3 words
    chunks: List[Dict[str, Any]] = []
    i = 0
    while i < len(clip_words):
        # Choose 2 or 3 words (prefer 3, use 2 if 4 remain so we get 2+2)
        remaining = len(clip_words) - i
        chunk_len = 3
        if remaining == 4:
            chunk_len = 2
        elif remaining < 3:
            chunk_len = remaining

        group = clip_words[i:i + chunk_len]
        if group:
            c_start = group[0]["start"]
            c_end = group[-1]["end"]
            # Ensure minimum duration for readability
            if (c_end - c_start) < 0.35:
                c_end = c_start + 0.35

            chunks.append({
                "start": c_start,
                "end": c_end,
                "words": group,
            })
        i += chunk_len

    # 3. Build ASS content
    # PlayRes 1080x1920 (9:16 vertical video); alignment 2 = bottom center.
    ass_header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"{_ass_style_line(style)}\n"
        f"{_hook_style_line(style)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    dialogue_lines: List[str] = []

    # Optional hook title: a separate event using the same font, at the top.
    hook_text = (hook_title or "").strip()
    clip_length = max(0.1, float(clip_end) - float(clip_start))
    hook_end = min(max(1.0, float(hook_duration)), clip_length, 6.0)
    if hook_text:
        safe_hook = _escape_ass_text(hook_text).replace("\r", "").replace("\n", "\\N")
        fade_ms = HOOK_FADE_MS if hook_end > 0.8 else max(80, int(hook_end * 1000 / 3))
        dialogue_lines.append(
            f"Dialogue: 1,{format_ass_time(0)},{format_ass_time(hook_end)},Hook,,0,0,0,,"
            f"{{\\fad({fade_ms},{fade_ms})}}{safe_hook}"
        )

    for chunk in chunks:
        for start, end, text in _chunk_lines(chunk, style):
            dialogue_lines.append(
                f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{text}"
            )

    # Optional thin progress bar at the very bottom. A few short events make
    # the bar grow smoothly without any ffmpeg drawbox expressions.
    if progress_bar:
        bar_color = _ass_color(progress_color)
        bar_y = 1920 - PROGRESS_HEIGHT
        for step in range(PROGRESS_STEPS):
            start = clip_length * step / PROGRESS_STEPS
            end = clip_length * (step + 1) / PROGRESS_STEPS
            width = max(1, round(1080 * (step + 1) / PROGRESS_STEPS))
            drawing = f"m 0 0 l {width} 0 {width} {PROGRESS_HEIGHT} 0 {PROGRESS_HEIGHT}"
            dialogue_lines.append(
                f"Dialogue: 2,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,"
                f"{{\\an7\\pos(0,{bar_y})\\p1\\c{bar_color}}}{drawing}{{\\p0}}"
            )

    full_ass = ass_header + "\n".join(dialogue_lines) + "\n"

    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(full_ass)

    return output_ass_path
