"""
Highlight suggestion engine for Cliptor Clip Studio.
Two-pass LLM pipeline with validation pass for self-contained, complete short clips,
and an offline audio-peak + sentence boundary fallback.
"""
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import requests
import numpy as np

from app.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    MIN_CLIP_SECONDS,
    PREFERRED_MIN_SECONDS,
    PREFERRED_MAX_SECONDS,
    MAX_CLIP_SECONDS
)
from app.logger import logger
from app.snapping import snap_to_sentence_boundaries, compute_overlap_ratio
from app.transcription import load_wav_as_float32


def _clean_json_response(raw_text: str) -> str:
    """Strips markdown code fences (```json ... ```) from LLM output."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _call_llm_api(messages: List[Dict[str, str]], base_url: str, api_key: str, model_name: str) -> str:
    """
    Calls an OpenAI-compatible /chat/completions endpoint.
    Works with OpenAI, OpenRouter, Groq, Ollama, etc.
    """
    endpoint = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.2
    }

    logger.info(f"Calling LLM API ({model_name}) at {endpoint}...")
    response = requests.post(endpoint, json=payload, headers=headers, timeout=90)

    if response.status_code == 401:
        raise ValueError("Invalid LLM API Key. Please verify LLM_API_KEY in your .env file.")
    elif response.status_code == 429:
        raise ValueError("LLM API rate limit or quota exceeded. Falling back to offline suggestions.")
    elif response.status_code != 200:
        raise ValueError(f"LLM API returned error {response.status_code}: {response.text[:300]}")

    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("LLM returned no choices in response.")

    return choices[0]["message"]["content"]


def _format_transcript_with_timestamps(segments: List[Dict[str, Any]]) -> str:
    """Formats segments into readable text with sentence timestamps."""
    lines = []
    for s in segments:
        lines.append(f"[{s['start']:.1f}s - {s['end']:.1f}s]: {s['text'].strip()}")
    return "\n".join(lines)


# =====================================================================
# PASS 1: Outline Chapters / Topics
# =====================================================================

def _pass1_outline_chapters(
    transcript_text: str,
    base_url: str,
    api_key: str,
    model_name: str
) -> List[Dict[str, Any]]:
    """
    Pass 1: Outlines the video transcript into coherent chapters/topics.
    Identifies beginnings, ends, and content types (list/steps, story, argument, joke, tutorial).
    """
    system_prompt = (
        "You are an expert video narrative analyst.\n"
        "Analyze the provided video transcript and break it down into high-level coherent chapters/topics.\n"
        "For each chapter, identify:\n"
        "- start: timestamp in seconds where the topic starts\n"
        "- end: timestamp in seconds where the topic finishes\n"
        "- title: short topic title\n"
        "- type: one of ['list/steps', 'story', 'argument', 'joke', 'tutorial', 'qa', 'standalone_thought']\n"
        "- summary: 1-sentence summary\n\n"
        "CRITICAL FOR LISTS & MULTI-STEP CONTENT:\n"
        "If the speaker introduces a list (e.g. 'Three things...', '5 steps'), the chapter MUST span from the introduction "
        "through ALL steps until the conclusion. Never cut off before the list completes!\n\n"
        "OUTPUT FORMAT: Return STRICT JSON only (a JSON array of chapter objects). No extra markdown outside JSON."
    )

    user_prompt = f"Transcript:\n\n{transcript_text}\n\nReturn the list of chapters as a JSON array."
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    raw = _call_llm_api(messages, base_url, api_key, model_name)
    try:
        cleaned = _clean_json_response(raw)
        chapters = json.loads(cleaned)
        return chapters if isinstance(chapters, list) else chapters.get("chapters", [])
    except Exception as e:
        logger.warning(f"Pass 1 chapter outline JSON parsing failed: {e}. Retrying once...")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": "Your response was not valid JSON. Return ONLY a valid JSON array of chapters."})
        raw_retry = _call_llm_api(messages, base_url, api_key, model_name)
        cleaned_retry = _clean_json_response(raw_retry)
        parsed = json.loads(cleaned_retry)
        return parsed if isinstance(parsed, list) else parsed.get("chapters", [])


# =====================================================================
# PASS 2: Select Clip Candidates from Chapters
# =====================================================================

def _pass2_select_candidates(
    transcript_text: str,
    chapters: List[Dict[str, Any]],
    base_url: str,
    api_key: str,
    model_name: str,
    max_clip_seconds: float = MAX_CLIP_SECONDS
) -> List[Dict[str, Any]]:
    """
    Pass 2: Selects 5 to 10 clip candidates based on chapters and completeness rules.
    """
    chapters_summary = json.dumps(chapters, indent=2)

    system_prompt = (
        "You are an elite short-form video editor for TikTok, YouTube Shorts, and Instagram Reels.\n"
        "Your mission is to pick 5 to 10 viral clip candidates that are 100% SELF-CONTAINED.\n\n"
        "MANDATORY COMPLETENESS RULES:\n"
        "1. Complete the Thought: A clip must completely finish the thought it starts. Never leave a promise unfulfilled.\n"
        "2. Complete Lists: If the speaker announces a list ('three things', 'four steps', 'two reasons'), the clip MUST "
        "include ALL points. Never end after Step 1 or Step 2. If a list cannot fit into the max length, do NOT pick it "
        "unless each part is completely standalone, in which case label it 'Part 1 of 3: [Title]'.\n"
        "3. No Dangling References: Never start a clip with backward references like 'As I said before', 'In the next step', 'Additionally'.\n"
        f"4. Duration: Minimum {MIN_CLIP_SECONDS}s, preferred {PREFERRED_MIN_SECONDS}s to {PREFERRED_MAX_SECONDS}s, hard maximum {max_clip_seconds}s. "
        "A clip may go up to the hard maximum if needed to complete a list or story.\n"
        "5. Strict Scoring: Be tough! Use the full 1 to 10 scale. Most clips should receive scores below 8 (5.0 to 7.5). "
        "Reserve 8.0-10.0 ONLY for exceptional, viral, punchy, complete masterpieces.\n\n"
        "OUTPUT FORMAT: Return STRICT JSON only (a JSON array of objects).\n"
        "Schema:\n"
        "[\n"
        "  {\n"
        "    \"start\": 12.0,\n"
        "    \"end\": 68.0,\n"
        "    \"title\": \"Catchy title (under 7 words)\",\n"
        "    \"type\": \"list/steps | story | argument | joke | tutorial\",\n"
        "    \"reason\": \"Why this works as a short-form video\",\n"
        "    \"completeness_note\": \"Explains how the clip fully resolves its premise and delivers on its hook\",\n"
        "    \"score\": 6.8\n"
        "  }\n"
        "]"
    )

    user_prompt = (
        f"Chapters:\n{chapters_summary}\n\n"
        f"Full Transcript:\n{transcript_text}\n\n"
        "Identify 5 to 10 complete clip candidates. Return ONLY the JSON array."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    raw = _call_llm_api(messages, base_url, api_key, model_name)
    try:
        cleaned = _clean_json_response(raw)
        cands = json.loads(cleaned)
        return cands if isinstance(cands, list) else cands.get("candidates", [])
    except Exception as e:
        logger.warning(f"Pass 2 candidate selection JSON parsing failed: {e}. Retrying once...")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": "Response was not valid JSON. Return ONLY a valid JSON array of candidates."})
        raw_retry = _call_llm_api(messages, base_url, api_key, model_name)
        cleaned_retry = _clean_json_response(raw_retry)
        parsed = json.loads(cleaned_retry)
        return parsed if isinstance(parsed, list) else parsed.get("candidates", [])


# =====================================================================
# PASS 3: Validation & Repair Pass
# =====================================================================

def _pass3_validate_candidate(
    candidate: Dict[str, Any],
    clip_text: str,
    base_url: str,
    api_key: str,
    model_name: str
) -> Dict[str, Any]:
    """
    Pass 3: Inspects an individual candidate clip to verify completeness,
    hook strength, and absence of dangling references.
    """
    system_prompt = (
        "You are a strict quality control reviewer for video clips.\n"
        "Examine this candidate clip transcript and determine if it is completely self-contained.\n\n"
        "Checklist:\n"
        "1. Does it start with an engaging hook without dangling references ('as mentioned earlier', 'step 2' without step 1)?\n"
        "2. Does it fully finish its thought without unfulfilled promises? (e.g. if speaker says 'Three things', are all 3 present?)\n"
        "3. Does it make sense completely on its own?\n\n"
        "OUTPUT FORMAT: Return STRICT JSON only:\n"
        "{\n"
        "  \"pass\": true | false,\n"
        "  \"issues\": [\"list of specific issues if any\"],\n"
        "  \"suggested_start\": <number in seconds or null>,\n"
        "  \"suggested_end\": <number in seconds or null>,\n"
        "  \"completeness_note\": \"Explanation of why this clip is complete or how it was fixed\"\n"
        "}"
    )

    user_prompt = (
        f"Clip Title: {candidate.get('title')}\n"
        f"Clip Type: {candidate.get('type')}\n"
        f"Time Range: {candidate.get('start')}s to {candidate.get('end')}s\n"
        f"Clip Transcript:\n\"{clip_text}\"\n\n"
        "Evaluate and return the JSON assessment."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    try:
        raw = _call_llm_api(messages, base_url, api_key, model_name)
        cleaned = _clean_json_response(raw)
        return json.loads(cleaned)
    except Exception as e:
        logger.warning(f"Validation pass failed for '{candidate.get('title')}': {e}. Defaulting to pass.")
        return {
            "pass": True,
            "issues": [],
            "suggested_start": None,
            "suggested_end": None,
            "completeness_note": candidate.get("completeness_note", "Self-contained clip")
        }


# =====================================================================
# Merge Adjacent Segments & Deduplicate Overlaps
# =====================================================================

def _merge_and_deduplicate(
    candidates: List[Dict[str, Any]],
    segments: List[Dict[str, Any]],
    max_clip_seconds: float = MAX_CLIP_SECONDS
) -> List[Dict[str, Any]]:
    """
    Ensures zero overlapping clips.
    If adjacent candidates cover the same topic and can fit within max_clip_seconds, merges them.
    Otherwise, keeps the higher-scoring candidate.
    """
    if not candidates:
        return []

    # Sort candidates by start time
    sorted_cands = sorted(candidates, key=lambda c: c["start"])
    merged_list: List[Dict[str, Any]] = []

    for cand in sorted_cands:
        if not merged_list:
            merged_list.append(cand)
            continue

        prev = merged_list[-1]
        overlap = compute_overlap_ratio(prev["start"], prev["end"], cand["start"], cand["end"])

        # Check if adjacent or overlapping
        is_adjacent_or_overlapping = (overlap > 0.05) or (abs(cand["start"] - prev["end"]) <= 2.0)
        same_topic = (prev.get("type") == cand.get("type"))

        potential_merged_dur = max(prev["end"], cand["end"]) - min(prev["start"], cand["start"])

        if is_adjacent_or_overlapping and same_topic and potential_merged_dur <= max_clip_seconds:
            # Merge into one complete clip
            new_start = min(prev["start"], cand["start"])
            new_end = max(prev["end"], cand["end"])
            snapped_s, snapped_e, merged_text = snap_to_sentence_boundaries(
                new_start, new_end, segments, min_len=MIN_CLIP_SECONDS, max_len=max_clip_seconds
            )
            prev["start"] = snapped_s
            prev["end"] = snapped_e
            prev["duration"] = round(snapped_e - snapped_s, 2)
            prev["text"] = merged_text
            prev["score"] = max(prev["score"], cand["score"])
            prev["completeness_note"] = f"Merged topic: {prev.get('completeness_note', '')}"
            logger.info(f"Merged adjacent candidates into single clip: {prev['title']} ({snapped_s}s - {snapped_e}s)")
        elif overlap > 0.15:
            # High overlap but cannot merge: keep the higher-scoring clip
            if cand["score"] > prev["score"]:
                merged_list[-1] = cand
        else:
            merged_list.append(cand)

    return merged_list


# =====================================================================
# OFFLINE FALLBACK (NO API KEY)
# =====================================================================

def _detect_type_from_text(text: str) -> str:
    """Heuristic helper to classify segment type when offline."""
    lower = text.lower()
    if any(k in lower for k in ["step", "first", "second", "third", "tip", "reason", "ways to"]):
        return "list/steps"
    elif any(k in lower for k in ["story", "when i was", "one day", "years ago", "happened"]):
        return "story"
    elif any(k in lower for k in ["because", "why", "believe", "wrong", "opinion"]):
        return "argument"
    elif any(k in lower for k in ["how to", "tutorial", "guide", "learn"]):
        return "tutorial"
    return "standalone_thought"


def _generate_fallback_highlights(
    transcript: Dict[str, Any],
    audio_wav: Optional[Path] = None,
    max_clip_seconds: float = MAX_CLIP_SECONDS
) -> List[Dict[str, Any]]:
    """
    Offline fallback operating with NO API key.
    Calculates sentence-level RMS energy and merges sentences into complete,
    non-overlapping units respecting list boundaries and duration constraints.
    """
    logger.info("Generating offline clip suggestions using complete sentence blocks and audio energy...")
    segments = transcript.get("segments", [])
    if not segments:
        return []

    total_start = segments[0]["start"]
    total_end = segments[-1]["end"]
    total_duration = total_end - total_start

    # If entire video fits within short duration, return 1 complete clip
    if total_duration <= PREFERRED_MAX_SECONDS:
        all_text = " ".join(s["text"].strip() for s in segments)
        return [{
            "start": round(total_start, 2),
            "end": round(total_end, 2),
            "duration": round(total_duration, 2),
            "title": "Complete Video Highlight",
            "type": _detect_type_from_text(all_text),
            "reason": "Complete video fits within vertical short clip duration (Basic suggestion)",
            "completeness_note": "Includes full opening, content, and conclusion (Basic suggestion)",
            "score": 7.2,
            "is_basic": True,
            "source": "Audio Loudness (Offline)",
            "text": all_text
        }]

    # Load audio array for RMS energy calculation
    audio_array = None
    sample_rate = 16000
    if audio_wav and audio_wav.exists():
        try:
            audio_array = load_wav_as_float32(audio_wav)
        except Exception as e:
            logger.warning(f"Could not load audio array: {e}")

    # Calculate loudness energy per segment
    segment_energies = []
    for s in segments:
        if audio_array is not None and len(audio_array) > 0:
            start_idx = max(0, int(s["start"] * sample_rate))
            end_idx = min(len(audio_array), int(s["end"] * sample_rate))
            if end_idx > start_idx:
                chunk = audio_array[start_idx:end_idx]
                rms = float(np.sqrt(np.mean(chunk**2)))
            else:
                rms = 0.05
        else:
            word_count = len(s.get("words", [])) or len(s["text"].split())
            dur = max(0.5, s["end"] - s["start"])
            rms = min(0.2, (word_count / dur) * 0.03)
        segment_energies.append(rms)

    # Search for complete multi-sentence blocks targeting 30 to 90 seconds (up to max_clip_seconds)
    candidates = []
    num_segs = len(segments)

    i = 0
    while i < num_segs:
        # Avoid starting on a dangling sentence
        start_text = segments[i]["text"].strip().lower()
        if any(start_text.startswith(w) for w in ["and ", "but ", "or ", "so ", "as i said", "next,"]):
            i += 1
            continue

        best_j = None
        for j in range(i, num_segs):
            dur = segments[j]["end"] - segments[i]["start"]
            if dur < MIN_CLIP_SECONDS:
                continue
            if dur > max_clip_seconds:
                break

            # If inside preferred range (30-90s)
            if PREFERRED_MIN_SECONDS <= dur <= PREFERRED_MAX_SECONDS:
                best_j = j

            # If a list indicator was introduced, make sure to include following steps
            if "step" in start_text or "three" in start_text or "tips" in start_text:
                if dur >= PREFERRED_MIN_SECONDS:
                    best_j = j

        if best_j is not None:
            dur = segments[best_j]["end"] - segments[i]["start"]
            win_energies = segment_energies[i:best_j + 1]
            avg_energy = float(np.mean(win_energies)) if win_energies else 0.05

            # Scoring: Strict scale, keeping average below 8
            base_score = 5.5 + min(2.0, (avg_energy / 0.15) * 1.8)
            if 30.0 <= dur <= 75.0:
                base_score += 0.4
            score = round(min(7.8, max(5.0, base_score)), 1)

            block_text = " ".join(segments[k]["text"].strip() for k in range(i, best_j + 1))
            cand_type = _detect_type_from_text(block_text)

            first_words = segments[i]["text"].strip().split()[:5]
            title_text = " ".join(first_words).rstrip(".,!?")

            candidates.append({
                "start": round(segments[i]["start"], 2),
                "end": round(segments[best_j]["end"], 2),
                "duration": round(dur, 2),
                "title": f"Highlight: \"{title_text}...\"",
                "type": cand_type,
                "reason": "Selected based on high audio energy and natural sentence bounds (Basic suggestion)",
                "completeness_note": "Self-contained sentence block with complete audio energy arc (Basic suggestion)",
                "score": score,
                "is_basic": True,
                "source": "Audio Loudness (Offline)",
                "text": block_text
            })

            # Move forward past this block to avoid overlapping clips
            i = best_j + 1
        else:
            i += 1

    return candidates


# =====================================================================
# MAIN HIGHLIGHT ENTRYPOINT
# =====================================================================

def pick_highlights(
    transcript: Dict[str, Any],
    audio_wav: Optional[Path] = None,
    max_clip_seconds: float = MAX_CLIP_SECONDS
) -> List[Dict[str, Any]]:
    """
    Main entry point for clip selection.
    Uses multi-pass LLM pipeline with completeness validation when an API key is present.
    Falls back gracefully to offline energy-based sentence clustering.
    """
    segments = transcript.get("segments", [])
    if not segments:
        logger.warning("pick_highlights called with empty transcript.")
        return []

    total_duration = transcript.get("duration", 0.0)
    final_candidates: List[Dict[str, Any]] = []

    # 1. Attempt LLM generation if API key is present
    if LLM_API_KEY:
        try:
            logger.info("Starting Multi-Pass LLM Highlight Pipeline...")

            # Videos under 60 minutes (3600s) are sent in one request without chunking
            transcript_text = _format_transcript_with_timestamps(segments)

            # PASS 1: Outline Chapters
            logger.info("Pass 1: Outlining transcript into chapters/topics...")
            chapters = _pass1_outline_chapters(transcript_text, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)
            logger.info(f"Pass 1 complete: {len(chapters)} chapters identified.")

            # PASS 2: Select Candidate Clips
            logger.info("Pass 2: Selecting clip candidates from chapters...")
            raw_candidates = _pass2_select_candidates(
                transcript_text, chapters, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, max_clip_seconds=max_clip_seconds
            )
            logger.info(f"Pass 2 complete: {len(raw_candidates)} initial candidates proposed.")

            # Snapping & Initial Normalization
            snapped_candidates = []
            for item in raw_candidates:
                try:
                    raw_s = float(item["start"])
                    raw_e = float(item["end"])
                    snapped_s, snapped_e, clip_text = snap_to_sentence_boundaries(
                        raw_s, raw_e, segments, min_len=MIN_CLIP_SECONDS, max_len=max_clip_seconds
                    )
                    score = float(item.get("score", 6.5))

                    snapped_candidates.append({
                        "start": snapped_s,
                        "end": snapped_e,
                        "duration": round(snapped_e - snapped_s, 2),
                        "title": str(item.get("title", "Untitled Clip")).strip(),
                        "type": str(item.get("type", "standalone_thought")).strip(),
                        "reason": str(item.get("reason", "Engaging moment")).strip(),
                        "completeness_note": str(item.get("completeness_note", "Self-contained")).strip(),
                        "score": round(max(1.0, min(10.0, score)), 1),
                        "is_basic": False,
                        "source": "LLM AI",
                        "text": clip_text
                    })
                except Exception as ex:
                    logger.warning(f"Error normalizing candidate {item}: {ex}")

            # PASS 3: Validation and Repair Pass
            logger.info("Pass 3: Validating completeness and hooks for candidates...")
            validated_candidates = []
            for cand in snapped_candidates:
                val_result = _pass3_validate_candidate(
                    cand, cand["text"], LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
                )
                passed = val_result.get("pass", True)

                if passed:
                    cand["completeness_note"] = val_result.get("completeness_note") or cand["completeness_note"]
                    validated_candidates.append(cand)
                else:
                    # Attempt suggested boundary repair
                    s_start = val_result.get("suggested_start")
                    s_end = val_result.get("suggested_end")
                    if s_start is not None and s_end is not None:
                        logger.info(f"Repairing candidate '{cand['title']}' using suggested bounds ({s_start}s - {s_end}s)...")
                        re_s, re_e, re_text = snap_to_sentence_boundaries(
                            float(s_start), float(s_end), segments, min_len=MIN_CLIP_SECONDS, max_len=max_clip_seconds
                        )
                        dur = re_e - re_s
                        if MIN_CLIP_SECONDS <= dur <= max_clip_seconds:
                            cand["start"] = re_s
                            cand["end"] = re_e
                            cand["duration"] = round(dur, 2)
                            cand["text"] = re_text
                            cand["completeness_note"] = f"Adjusted for completeness: {val_result.get('completeness_note', '')}"
                            validated_candidates.append(cand)
                        else:
                            logger.info(f"Dropped candidate '{cand['title']}': repair duration {dur:.1f}s outside limits.")
                    else:
                        logger.info(f"Dropped candidate '{cand['title']}' due to validation issues: {val_result.get('issues')}")

            # Merge adjacent same-topic segments and remove overlapping clips
            final_candidates = _merge_and_deduplicate(validated_candidates, segments, max_clip_seconds=max_clip_seconds)

        except Exception as e:
            logger.error(f"Multi-Pass LLM pipeline encountered an error: {e}. Falling back to offline method.")
            final_candidates = []

    # 2. Fall back to offline method if no API key or if LLM pipeline failed
    if not final_candidates:
        fallback_cands = _generate_fallback_highlights(transcript, audio_wav=audio_wav, max_clip_seconds=max_clip_seconds)
        final_candidates = _merge_and_deduplicate(fallback_cands, segments, max_clip_seconds=max_clip_seconds)

    # Sort final candidates by score descending to pick top 5 to 8
    final_candidates.sort(key=lambda c: c["score"], reverse=True)
    top_candidates = final_candidates[:8]

    # Re-sort chosen candidates by start time for natural video playback progression
    top_candidates.sort(key=lambda c: c["start"])

    logger.info(f"Highlight generation complete: {len(top_candidates)} final clips selected.")
    return top_candidates
