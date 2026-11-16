"""
Helper script to generate a sample test video with spoken audio on Windows.
Uses Windows built-in SpeechSynthesizer and FFmpeg.
"""
import subprocess
import shutil
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def get_ffmpeg_path() -> str:
    # 1. Check if ffmpeg is in current PATH
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 2. Check WinGet Gyan.FFmpeg install location
    winget_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    for match in winget_dir.glob("**/ffmpeg.exe"):
        if match.is_file():
            return str(match)

    # 3. Check C:\ffmpeg\bin
    c_ffmpeg = Path("C:/ffmpeg/bin/ffmpeg.exe")
    if c_ffmpeg.exists():
        return str(c_ffmpeg)

    raise FileNotFoundError("FFmpeg executable not found. Please ensure FFmpeg is installed.")

def generate_sample_speech_audio(output_wav: Path):
    """Generates a WAV file with clear English speech using PowerShell speech synthesis."""
    speech_text = (
        "Welcome to Cliptor Clip Studio. "
        "Content creation is all about telling engaging stories in a very short amount of time. "
        "When you make short vertical videos, you want to capture the audience's attention right in the first three seconds. "
        "A strong hook makes people stop scrolling and listen to what you have to say. "
        "Cliptor helps you find those key moments automatically, crop them to 9 by 16, and add dynamic captions."
    )
    
    ps_script = f"""
Add-Type -AssemblyName System.Speech;
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer;
$synth.SetOutputToWaveFile('{str(output_wav).replace("'", "''")}');
$synth.Speak('{speech_text.replace("'", "''")}');
$synth.Dispose();
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)

def create_sample_video():
    ffmpeg_exe = get_ffmpeg_path()
    sample_wav = BASE_DIR / "sample_speech.wav"
    sample_mp4 = BASE_DIR / "sample_video.mp4"

    print("Step 1: Generating sample speech audio...")
    generate_sample_speech_audio(sample_wav)

    print("Step 2: Combining audio and test visual into MP4...")
    # Generate a 1920x1080 30fps video with dark blue background, clock display, and the spoken audio
    cmd = [
        ffmpeg_exe,
        "-y",
        "-f", "lavfi",
        "-i", "color=c=0x111827:s=1920x1080:r=30",
        "-i", str(sample_wav),
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(sample_mp4)
    ]
    subprocess.run(cmd, check=True)

    # Cleanup temporary WAV
    if sample_wav.exists():
        sample_wav.unlink()

    print(f"Sample video created successfully at: {sample_mp4}")

if __name__ == "__main__":
    create_sample_video()
