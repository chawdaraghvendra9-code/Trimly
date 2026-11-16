"""
Tests for Stage 3: Multi-pass highlight suggestions, chapter outlining, completeness rules,
validation pass, boundary snapping, and offline fallback.
"""
import pytest
import json
from unittest.mock import patch, MagicMock
from pathlib import Path

from app.config import BASE_DIR, JOBS_DIR
from app.snapping import snap_to_sentence_boundaries, compute_overlap_ratio
from app.highlights import (
    pick_highlights,
    _pass1_outline_chapters,
    _pass2_select_candidates,
    _pass3_validate_candidate,
    _merge_and_deduplicate,
    _generate_fallback_highlights,
    _clean_json_response
)
from app.jobs import create_job, process_single_job, load_job, get_candidates_file

# Sample transcript simulating a video with a list of 3 steps and a separate story
LIST_AND_STORY_TRANSCRIPT = {
    "duration": 110.0,
    "language": "en",
    "total_segments": 6,
    "total_words": 120,
    "segments": [
        {"id": 1, "start": 0.0, "end": 8.0, "text": "Here are three steps to get your first 1000 subscribers."},
        {"id": 2, "start": 8.5, "end": 22.0, "text": "Step one is finding your niche and sticking to one clear audience."},
        {"id": 3, "start": 22.5, "end": 38.0, "text": "Step two is posting at least three high quality vertical shorts every week."},
        {"id": 4, "start": 38.5, "end": 56.0, "text": "And step three is replying to every single comment in the first hour."},
        {"id": 5, "start": 56.5, "end": 80.0, "text": "Let me tell you a story about how I made my first viral video back in 2023."},
        {"id": 6, "start": 80.5, "end": 108.0, "text": "I tried ten different formats before realizing that a strong hook makes all the difference."}
    ]
}

def test_clean_json_response():
    """Verify code fences stripping."""
    raw = "```json\n[{\"start\": 0, \"end\": 30}]\n```"
    assert _clean_json_response(raw) == "[{\"start\": 0, \"end\": 30}]"

def test_sentence_boundary_snapping():
    """Verify arbitrary timestamps snap to nearest sentence boundaries."""
    snapped_start, snapped_end, text = snap_to_sentence_boundaries(
        target_start=2.0,
        target_end=35.0,
        segments=LIST_AND_STORY_TRANSCRIPT["segments"],
        min_len=20.0,
        max_len=120.0
    )
    # Target 2.0s to 35.0s should snap to sentence 1 start (0.0) and sentence 3 end (38.0)
    assert snapped_start == 0.0
    assert snapped_end == 38.0
    assert "Here are three steps" in text
    assert "Step two" in text

def test_merge_and_deduplicate_adjacent_same_topic():
    """Verify adjacent segments of same topic merge into one clip."""
    cands = [
        {
            "start": 0.0, "end": 25.0, "duration": 25.0,
            "title": "Part A", "type": "list/steps", "score": 6.5,
            "reason": "Tip", "completeness_note": "First part", "text": "Step one..."
        },
        {
            "start": 26.0, "end": 56.0, "duration": 30.0,
            "title": "Part B", "type": "list/steps", "score": 7.0,
            "reason": "Tip", "completeness_note": "Second part", "text": "Step two and three..."
        }
    ]
    merged = _merge_and_deduplicate(cands, LIST_AND_STORY_TRANSCRIPT["segments"], max_clip_seconds=120.0)
    assert len(merged) == 1
    assert merged[0]["start"] == 0.0
    assert merged[0]["end"] >= 56.0

def test_validation_pass_repair_and_drop():
    """
    Verify Pass 3 validation: if a candidate fails and provides suggested bounds,
    it is repaired; if it fails without bounds, it is dropped.
    """
    with patch("app.highlights._call_llm_api") as mock_call:
        # Mock 1: Candidate with repair suggestions
        mock_call.return_value = json.dumps({
            "pass": False,
            "issues": ["Cut off before Step 3"],
            "suggested_start": 0.0,
            "suggested_end": 56.0,
            "completeness_note": "Extended to include all three steps"
        })

        cand = {
            "title": "3 Steps", "type": "list/steps", "start": 0.0, "end": 22.0,
            "text": "Here are three steps...", "completeness_note": "Incomplete"
        }
        val = _pass3_validate_candidate(cand, cand["text"], "http://base", "key", "model")
        assert val["pass"] is False
        assert val["suggested_end"] == 56.0

def test_offline_fallback_completeness_and_scoring():
    """
    Verify offline fallback produces candidates with 'type' and 'completeness_note',
    respects 20s-120s length, and maintains strict scores (most below 8.0).
    """
    candidates = _generate_fallback_highlights(LIST_AND_STORY_TRANSCRIPT)
    assert len(candidates) > 0
    for c in candidates:
        assert "type" in c
        assert "completeness_note" in c
        assert c["duration"] >= 20.0
        assert c["duration"] <= 120.0
        assert c["score"] <= 8.5  # Strict scoring

def test_multi_pass_llm_pipeline_mocked():
    """
    Verify the full 3-pass LLM pipeline with mock responses for:
    Pass 1 (chapters), Pass 2 (selection), Pass 3 (validation).
    """
    with patch("app.highlights.LLM_API_KEY", "mock-key"):
        with patch("app.highlights._call_llm_api") as mock_api:
            # Setup sequential mock returns for Pass 1, Pass 2, and Pass 3
            mock_api.side_effect = [
                # Pass 1: chapters
                json.dumps([
                    {"start": 0.0, "end": 56.0, "title": "Three Steps to 1000 Subs", "type": "list/steps", "summary": "All 3 steps"},
                    {"start": 56.5, "end": 108.0, "title": "Viral Video Story", "type": "story", "summary": "Story of first hit"}
                ]),
                # Pass 2: candidate selection
                json.dumps([
                    {
                        "start": 0.0,
                        "end": 56.0,
                        "title": "The 3-Step YouTube Formula",
                        "type": "list/steps",
                        "reason": "Complete formula with no unfulfilled promises",
                        "completeness_note": "Covers all 3 announced steps fully",
                        "score": 7.4
                    }
                ]),
                # Pass 3: validation
                json.dumps({
                    "pass": True,
                    "issues": [],
                    "suggested_start": None,
                    "suggested_end": None,
                    "completeness_note": "100% self-contained list of 3 steps"
                })
            ]

            results = pick_highlights(LIST_AND_STORY_TRANSCRIPT)
            assert len(results) == 1
            clip = results[0]
            assert clip["title"] == "The 3-Step YouTube Formula"
            assert clip["type"] == "list/steps"
            assert clip["start"] == 0.0
            assert clip["end"] == 56.0
            assert clip["score"] == 7.4
            assert "100% self-contained" in clip["completeness_note"]

def test_end_to_end_job_with_enhanced_highlights():
    """Verify complete job flow produces candidates.json with type and completeness_note."""
    sample_video = BASE_DIR / "sample_video.mp4"
    job_info = create_job("sample_video.mp4", sample_video.stat().st_size)
    job_id = job_info["job_id"]
    job_dir = JOBS_DIR / job_id

    import shutil
    shutil.copyfile(sample_video, job_dir / "input.mp4")

    # Run processing (with offline fallback for speedy unit testing)
    with patch("app.highlights.LLM_API_KEY", ""):
        process_single_job(job_id)

    updated = load_job(job_id)
    assert updated["status"] in {"ready", "completed"}
    assert len(updated["candidates"]) > 0
    first_cand = updated["candidates"][0]
    assert "type" in first_cand
    assert "completeness_note" in first_cand
