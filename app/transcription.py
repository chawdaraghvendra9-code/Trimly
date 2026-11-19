"""
Audio transcription module using faster-whisper.
Runs locally on CPU with int8 quantization and word-level timestamps.
"""
import wave
from pathlib import Path
from typing import Dict, Any, List, Callable, Optional
import numpy as np
from faster_whisper import WhisperModel

from app.logger import logger
from app.models import whisper_cache_root
from app.video import get_audio_duration_seconds

# Global cache for the loaded Whisper model so we don't re-read from disk every time
_model_cache: Dict[str, WhisperModel] = {}

class TranscriptionError(Exception):
    """Base error for transcription issues."""
    pass

class NoSpeechFoundError(TranscriptionError):
    """Raised when the video has audio but no spoken words could be recognized."""
    pass

class ModelDownloadError(TranscriptionError):
    """Raised when downloading the faster-whisper model from Hugging Face fails."""
    pass

def load_wav_as_float32(wav_path: Path) -> np.ndarray:
    """
    Loads a 16 kHz mono 16-bit PCM WAV file into a normalized float32 NumPy array.
    Using Python's standard library 'wave' module guarantees 100% stability
    and avoids third-party container/codec incompatibilities.
    """
    try:
        with wave.open(str(wav_path), "rb") as wf:
            framerate = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            n_frames = wf.getnframes()
            raw_bytes = wf.readframes(n_frames)

        if n_frames == 0 or len(raw_bytes) == 0:
            return np.zeros(0, dtype=np.float32)

        # Convert 16-bit signed PCM integers to float32 between -1.0 and 1.0
        audio_array = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        return audio_array
    except Exception as e:
        logger.error(f"Failed to read WAV file {wav_path}: {e}")
        raise TranscriptionError(f"Could not read audio file: {e}") from e

def get_whisper_model(model_size: str = "small") -> WhisperModel:
    """
    Returns a cached WhisperModel instance, or loads it if not already loaded.
    Configured for CPU execution with int8 quantization.
    Supported model sizes: 'tiny', 'base', 'small', 'medium', 'large', 'large-v3'.
    """
    valid_sizes = {"tiny", "base", "small", "medium", "large", "large-v3"}
    size_cleaned = model_size.strip().lower()
    if size_cleaned not in valid_sizes:
        logger.warning(f"Unknown model size '{model_size}', falling back to 'small'")
        size_cleaned = "small"

    if size_cleaned in _model_cache:
        return _model_cache[size_cleaned]

    logger.info(f"Loading faster-whisper model '{size_cleaned}' (device=cpu, compute_type=int8)...")
    try:
        # Installed builds keep models in %LOCALAPPDATA%\Trimly\models.
        # A source checkout keeps the default Hugging Face cache.
        download_root = whisper_cache_root()
        model = WhisperModel(
            model_size_or_path=size_cleaned,
            device="cpu",
            compute_type="int8",
            download_root=str(download_root) if download_root else None
        )
        _model_cache[size_cleaned] = model
        logger.info(f"Model '{size_cleaned}' loaded successfully.")
        return model
    except Exception as e:
        logger.exception(f"Failed to load Whisper model '{size_cleaned}': {e}")
        raise ModelDownloadError(
            f"Could not load Whisper model '{size_cleaned}'. "
            "Please check your internet connection for the initial download, "
            "or set WHISPER_MODEL=tiny in your .env file for a smaller download."
        ) from e

def transcribe_audio_file(
    audio_path: Path,
    model_size: str = "small",
    progress_callback: Optional[Callable[[str, int], None]] = None
) -> Dict[str, Any]:
    """
    Transcribes a 16 kHz mono WAV file with word-level timestamps.
    Calls progress_callback(status_message, percent) as segments are processed.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {audio_path}")

    # Calculate duration of the audio to estimate progress percentage
    total_duration = get_audio_duration_seconds(audio_path)
    logger.info(f"Starting transcription for {audio_path.name} (Duration: {total_duration:.1f}s)")

    if progress_callback:
        progress_callback("Loading speech recognition model...", 15)

    model = get_whisper_model(model_size)

    if progress_callback:
        progress_callback("Decoding audio data...", 20)

    # Load audio as float32 numpy array
    audio_array = load_wav_as_float32(audio_path)

    if len(audio_array) == 0:
        logger.warning(f"Audio file {audio_path.name} is empty.")
        raise NoSpeechFoundError("The audio file is empty and contains no speech.")

    try:
        # Transcribe with word timestamps and voice activity detection (VAD) filter
        segments_generator, info = model.transcribe(
            audio_array,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500)
        )
    except Exception as e:
        logger.exception(f"Error during model.transcribe: {e}")
        raise TranscriptionError(f"Transcription failed: {str(e)}") from e

    formatted_segments: List[Dict[str, Any]] = []
    total_words = 0

    # Iterate over segments as they are decoded
    for idx, segment in enumerate(segments_generator):
        words_list = []
        if segment.words:
            for w in segment.words:
                words_list.append({
                    "word": w.word.strip(),
                    "start": round(w.start, 2),
                    "end": round(w.end, 2),
                    "probability": round(w.probability, 2)
                })
                total_words += 1

        formatted_segments.append({
            "id": idx + 1,
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text.strip(),
            "words": words_list
        })

        # Calculate progress percent (from 20% to 90%)
        if total_duration > 0 and progress_callback:
            seg_progress = min(90, int(20 + (segment.end / total_duration) * 70))
            progress_callback(f"Transcribing audio... {seg_progress}%", seg_progress)

    # Check if any speech was detected
    if not formatted_segments or total_words == 0:
        logger.warning(f"No speech was detected in audio file {audio_path.name}")
        raise NoSpeechFoundError(
            "No spoken words or speech were detected in this video. "
            "Please verify that the video has clear audio."
        )

    result = {
        "duration": round(total_duration, 2),
        "language": info.language,
        "language_probability": round(info.language_probability, 2),
        "total_segments": len(formatted_segments),
        "total_words": total_words,
        "segments": formatted_segments
    }

    logger.info(
        f"Transcription complete: {len(formatted_segments)} segments, "
        f"{total_words} words detected in language '{info.language}'"
    )

    if progress_callback:
        progress_callback("Transcription complete!", 95)

    return result
