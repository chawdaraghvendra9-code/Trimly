"""
Subtitle generation module for Cliptor Clip Studio.
Generates Advanced SubStation Alpha (.ass) subtitle files with 2-3 words per chunk,
bold white text, solid dark outline, safe mobile 9:16 margins, and time offset.
"""
from pathlib import Path
from typing import List, Dict, Any

def format_ass_time(seconds: float) -> str:
    """Formats seconds into ASS timestamp format: H:MM:SS.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"

def generate_ass_subtitles_for_clip(
    transcript_segments: List[Dict[str, Any]],
    clip_start: float,
    clip_end: float,
    output_ass_path: Path
) -> Path:
    """
    Extracts word timestamps falling between clip_start and clip_end,
    offsets them relative to clip_start (so clip starts at 0:00),
    groups into 2 to 3 words per subtitle chunk, and writes out a .ass file.
    """
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

    # 2. Group into chunks of 2 to 3 words
    chunks = []
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

            # Join words
            text = " ".join(w["word"] for w in group)
            chunks.append({
                "start": c_start,
                "end": c_end,
                "text": text
            })
        i += chunk_len

    # 3. Build ASS content
    # PlayRes 1080x1920 (9:16 vertical video)
    # Alignment 2 = bottom center
    # MarginV 260 = safe margin from bottom of phone screen (avoiding platform UI)
    # Outline 5 = bold dark outline for crystal-clear readability
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,66,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5.0,0,2,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogue_lines = []
    for c in chunks:
        start_str = format_ass_time(c["start"])
        end_str = format_ass_time(c["end"])
        # Escape any ASS special characters if present
        safe_text = c["text"].replace("\\", "\\\\").replace("{", "(").replace("}", ")")
        dialogue_lines.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{safe_text}")

    full_ass = ass_header + "\n".join(dialogue_lines) + "\n"

    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(full_ass)

    return output_ass_path
