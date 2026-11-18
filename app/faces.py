"""
Face position detection for the "Smart fit" video style.

OpenCV's Haar face detector looks at ~8 evenly spaced frames of the clip and
only the horizontal center of the biggest face is kept. The median is returned
so the crop window is STATIC (one window for the whole clip), which avoids the
jitter that per-frame tracking causes.

If OpenCV is missing or no face is found, None is returned and the caller
centers the crop instead. Nothing here can stop a render.
"""
import logging
from pathlib import Path
from statistics import median
from typing import List, Optional

logger = logging.getLogger("cliptor")

# How many frames of the clip we look at, and how wide the detection copy is.
# Haar is slow on full 1080p frames, so we detect on a small copy.
FACE_SAMPLES = 8
DETECT_WIDTH = 480
# Ignore tiny detections (they are usually false positives, e.g. patterns).
MIN_FACE_RATIO = 0.05


def _load_detector():
    """Returns (cv2, detector) or (None, None) when OpenCV cannot be used."""
    try:
        import cv2
    except Exception as e:  # ImportError, or a broken wheel
        logger.warning(f"Face detection is unavailable ({e}); the crop will be centered.")
        return None, None

    model_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(model_path))
    if detector.empty():
        logger.warning("The Haar face model could not be loaded; the crop will be centered.")
        return None, None
    return cv2, detector


def detect_face_center_x(
    video_path: Path,
    start_time: float,
    end_time: float,
    samples: int = FACE_SAMPLES,
) -> Optional[float]:
    """
    Median horizontal center of the faces in one clip, in source pixels.

    Returns None when no face is found (or OpenCV is not installed), so the
    caller can fall back to a centered crop.
    """
    cv2, detector = _load_detector()
    if cv2 is None:
        return None

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        logger.warning("OpenCV could not open the video for face detection; the crop will be centered.")
        return None

    centers: List[float] = []
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        if width <= 0:
            return None

        scale = min(1.0, DETECT_WIDTH / width)
        length = max(0.0, float(end_time) - float(start_time))

        for index in range(max(1, samples)):
            # Sample the middle of each slice, so the very first/last frame
            # (often a fade or a title card) does not decide the crop.
            moment = float(start_time) + length * (index + 0.5) / max(1, samples)
            capture.set(cv2.CAP_PROP_POS_MSEC, moment * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue

            small = frame
            if scale < 1.0:
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            min_size = max(24, int(small.shape[1] * MIN_FACE_RATIO))
            faces = detector.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_size, min_size)
            )
            if len(faces) == 0:
                continue

            # The biggest face wins: it belongs to the person the clip is about.
            fx, _fy, fw, _fh = max(faces, key=lambda box: box[2] * box[3])
            centers.append((float(fx) + float(fw) / 2.0) / scale)
    except Exception as e:
        logger.warning(f"Face detection failed ({e}); the crop will be centered.")
        return None
    finally:
        capture.release()

    if not centers:
        logger.info("Smart fit: no face found, the crop stays centered.")
        return None

    center = float(median(centers))
    logger.info(f"Smart fit: face found in {len(centers)} of {samples} frames, crop center x={center:.0f}px.")
    return center
