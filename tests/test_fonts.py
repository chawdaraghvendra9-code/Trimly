"""
Caption font tests.

The important one renders a 3-second sample caption with every font and checks
FFmpeg's verbose log. libass silently falls back to a default font when the
family name does not match, so an exit code of 0 is NOT enough: the log must
show the requested family and the font file from fonts/.
"""
import subprocess
from pathlib import Path

import pytest

from app.fonts import (
    DEFAULT_FONT_STYLE,
    FONT_STYLES,
    FONTS_DIR,
    copy_font_to_job,
    get_font_style,
    list_font_styles,
)
from app.subtitles import generate_ass_subtitles_for_clip
from app.video import find_ffmpeg_executable

FONT_STYLES_TO_TEST = [
    style for style in list_font_styles() if style.get("filename")
]


def _transcript():
    """Words spread across 3 seconds, enough for one or two caption chunks."""
    return [
        {
            "start": 0.0,
            "end": 3.0,
            "text": "Caption font test",
            "words": [
                {"word": "Caption", "start": 0.1, "end": 0.8},
                {"word": "font", "start": 0.9, "end": 1.5},
                {"word": "test", "start": 1.6, "end": 2.4},
            ],
        }
    ]


def test_fonts_folder_is_scanned_with_real_metadata():
    fonts = [style for style in FONT_STYLES.values() if style.get("filename")]
    assert len(fonts) == 9, "the nine shipped fonts should be found"

    by_filename = {style["filename"]: style for style in fonts}
    assert "Montserrat-ExtraBold.ttf" in by_filename
    # Family names come from the font file, not the file name.
    assert by_filename["Montserrat-ExtraBold.ttf"]["family"] == "Montserrat ExtraBold"
    assert by_filename["BebasNeue-Regular.ttf"]["family"] == "Bebas Neue"
    assert by_filename["PressStart2P-Regular.ttf"]["family"] == "Press Start 2P"

    for style in fonts:
        assert not style["filename"].endswith("VariableFont_wght.ttf")
        assert (FONTS_DIR / style["filename"]).exists()


def test_default_bold_style_and_looks():
    assert DEFAULT_FONT_STYLE in FONT_STYLES
    bold = get_font_style(DEFAULT_FONT_STYLE)
    assert bold["family"] == "Montserrat ExtraBold"
    assert bold["highlight"] is True
    # Lower third: text sits about 70-75% down a 1920px frame.
    assert 450 <= bold["margin_v"] <= 560

    assert get_font_style("bebas_neue")["uppercase"] is True
    assert get_font_style("anton")["uppercase"] is True
    assert get_font_style("bebas_neue")["size_multiplier"] > 1.0
    assert get_font_style("anton")["size_multiplier"] > 1.0
    assert get_font_style("press_start_2p")["size_multiplier"] < 0.8
    assert get_font_style("permanent_marker")["outline"] < 5.0


def test_unknown_style_falls_back_to_default():
    assert get_font_style("does-not-exist")["key"] == DEFAULT_FONT_STYLE
    assert get_font_style(None)["key"] == DEFAULT_FONT_STYLE


def test_used_font_is_copied_into_the_job_folder(tmp_path):
    style = get_font_style("anton")
    copied = copy_font_to_job(style["filename"], tmp_path)

    assert copied is not None
    assert copied.parent == tmp_path / "fonts"
    assert copied.name == "Anton-Regular.ttf"
    assert copied.stat().st_size > 1000


@pytest.mark.parametrize(
    "style",
    FONT_STYLES_TO_TEST,
    ids=[style["key"] for style in FONT_STYLES_TO_TEST],
)
def test_every_font_is_really_used_by_libass(style, tmp_path):
    """Render 3 seconds of captions and require libass to load this font file."""
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")

    job_dir = tmp_path / style["key"]
    job_dir.mkdir(parents=True, exist_ok=True)

    ass_path = job_dir / "sample.ass"
    generate_ass_subtitles_for_clip(
        transcript_segments=_transcript(),
        clip_start=0.0,
        clip_end=3.0,
        output_ass_path=ass_path,
        font_style=style,
    )

    copied = copy_font_to_job(style["filename"], job_dir)
    assert copied is not None and copied.exists()

    output = job_dir / "sample.mp4"
    command = [
        ffmpeg,
        "-y",
        "-nostdin",
        "-loglevel", "verbose",
        "-f", "lavfi",
        "-i", "color=c=black:s=320x568:d=3",
        "-vf", "subtitles=sample.ass:fontsdir=fonts",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-t", "3",
        str(output.name),
    ]

    result = subprocess.run(
        command,
        cwd=str(job_dir),
        capture_output=True,
        text=True,
        timeout=180,
    )
    log = (result.stdout or "") + "\n" + (result.stderr or "")

    assert result.returncode == 0, log[-3000:]
    assert output.exists(), log[-3000:]

    # libass prints one fontselect line per resolved family.
    fontselect_lines = [line for line in log.splitlines() if "fontselect" in line.lower()]
    assert fontselect_lines, f"libass printed no fontselect lines:\n{log[-3000:]}"

    family_lines = [line for line in fontselect_lines if style["family"] in line]
    assert family_lines, (
        f"libass did not select {style['family']!r}:\n" + "\n".join(fontselect_lines)
    )

    # The file from fonts/ must be the one used (proves it is not a fallback).
    assert style["filename"] in log, (
        f"libass did not load {style['filename']} for {style['family']!r}:\n"
        + "\n".join(fontselect_lines)
    )


def test_bold_style_highlights_the_current_word():
    style = get_font_style(DEFAULT_FONT_STYLE)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ass_path = Path(tmp) / "highlight.ass"
        generate_ass_subtitles_for_clip(
            transcript_segments=_transcript(),
            clip_start=0.0,
            clip_end=3.0,
            output_ass_path=ass_path,
            font_style=style,
        )
        content = ass_path.read_text(encoding="utf-8")

    assert "Style: Default,Montserrat ExtraBold" in content
    assert r"{\c&H0000FFFF&}" in content, "the active word should be yellow"
    # One line per spoken word, each with only one yellow word.
    dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 3
    for line in dialogue_lines:
        assert line.count(r"{\c&H0000FFFF&}") == 1
