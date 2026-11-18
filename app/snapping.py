"""
Sentence boundary snapping and text extraction for clip candidates.
Ensures clips do not start or end abruptly mid-sentence and conform to duration limits.
"""
from typing import List, Dict, Any, Tuple
from app.config import MIN_CLIP_SECONDS, MAX_CLIP_SECONDS

def snap_to_sentence_boundaries(
    target_start: float,
    target_end: float,
    segments: List[Dict[str, Any]],
    min_len: float = MIN_CLIP_SECONDS,
    max_len: float = MAX_CLIP_SECONDS
) -> Tuple[float, float, str]:
    """
    Snaps target_start and target_end to complete sentence (segment) boundaries.
    Ensures length is between min_len (default 20s) and max_len (default 120s).
    Returns: (snapped_start, snapped_end, combined_text)
    """
    if not segments:
        return target_start, target_end, ""

    # Sort segments by start time
    sorted_segs = sorted(segments, key=lambda s: s["start"])

    # If the entire video duration is shorter than min_len, use the whole span
    total_start = sorted_segs[0]["start"]
    total_end = sorted_segs[-1]["end"]
    if (total_end - total_start) <= min_len:
        all_text = " ".join(s["text"].strip() for s in sorted_segs)
        return total_start, total_end, all_text

    # 1. Find the segment start closest to target_start
    best_start_idx = 0
    min_start_diff = float("inf")
    for idx, seg in enumerate(sorted_segs):
        diff = abs(seg["start"] - target_start)
        if diff < min_start_diff:
            min_start_diff = diff
            best_start_idx = idx

    # 2. Find the segment end closest to target_end (must be at or after best_start_idx)
    best_end_idx = best_start_idx
    min_end_diff = float("inf")
    for idx in range(best_start_idx, len(sorted_segs)):
        diff = abs(sorted_segs[idx]["end"] - target_end)
        if diff < min_end_diff:
            min_end_diff = diff
            best_end_idx = idx

    # 3. Enforce minimum length (min_len)
    # Expand forward (or backward) by adding adjacent segments if duration is too short
    while (sorted_segs[best_end_idx]["end"] - sorted_segs[best_start_idx]["start"] < min_len):
        if best_end_idx + 1 < len(sorted_segs):
            best_end_idx += 1
        elif best_start_idx > 0:
            best_start_idx -= 1
        else:
            break

    # 4. Enforce maximum length (max_len)
    # If duration exceeds max_len, trim the last segment until it fits within max_len
    while (sorted_segs[best_end_idx]["end"] - sorted_segs[best_start_idx]["start"] > max_len) and (best_end_idx > best_start_idx):
        prev_end_duration = sorted_segs[best_end_idx - 1]["end"] - sorted_segs[best_start_idx]["start"]
        if prev_end_duration >= min_len:
            best_end_idx -= 1
        else:
            break

    snapped_start = round(sorted_segs[best_start_idx]["start"], 2)
    snapped_end = round(sorted_segs[best_end_idx]["end"], 2)

    # Combine text of all included segments
    included_text = " ".join(
        sorted_segs[i]["text"].strip()
        for i in range(best_start_idx, best_end_idx + 1)
        if sorted_segs[i]["text"].strip()
    )

    return snapped_start, snapped_end, included_text


def snap_to_word_boundaries(
    target_start: float,
    target_end: float,
    segments: List[Dict[str, Any]],
    lead_in: float = 0.2,
    tail: float = 0.3
) -> Tuple[float, float]:
    """
    Snaps a clip to real spoken words using word-level transcript timestamps.
    Starts at the first word that overlaps the window and ends at the last word,
    then adds a small lead-in before and tail after so speech is never clipped.
    If the transcript has no word timestamps, the window is returned unchanged.
    """
    words: List[Tuple[float, float]] = []
    for seg in segments:
        for w in seg.get("words") or []:
            try:
                w_start = float(w["start"])
                w_end = float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            # Keep words that overlap the requested window (do not cut a word in half)
            if w_end > target_start and w_start < target_end:
                words.append((w_start, w_end))

    if not words:
        return round(target_start, 2), round(target_end, 2)

    words.sort()
    start = max(0.0, words[0][0] - lead_in)
    end = words[-1][1] + tail
    return round(start, 2), round(end, 2)


def compute_overlap_ratio(start1: float, end1: float, start2: float, end2: float) -> float:
    """
    Computes overlap ratio between two time windows relative to the smaller clip.
    Returns a float between 0.0 (no overlap) and 1.0 (exact match).
    """
    intersection_start = max(start1, start2)
    intersection_end = min(end1, end2)
    intersection = max(0.0, intersection_end - intersection_start)

    if intersection <= 0.0:
        return 0.0

    duration1 = end1 - start1
    duration2 = end2 - start2
    min_duration = min(duration1, duration2)

    return intersection / min_duration if min_duration > 0 else 0.0
