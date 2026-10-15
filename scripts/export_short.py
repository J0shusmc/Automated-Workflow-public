"""Export a generated clip as a 1080x1920 H.264/AAC MP4 for Shorts."""
import argparse
import json
from pathlib import Path
import subprocess


def export_short(value):
    if isinstance(value, dict):
        value = value.get("path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Connect the generated video to the export node.")
    source = Path(value).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Video does not exist: {source}")
    destination_dir = Path(__file__).resolve().parents[1] / "data" / "shorts"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{source.stem}-1080x1920.mp4"
    # Preserve the complete picture, pad other aspect ratios, and preserve audio.
    # Lanczos resizing is an export resize, not AI detail reconstruction.
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(destination),
    ], check=True)
    print(f"video is ready: {destination}", flush=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Video artifact JSON or a video path")
    args = parser.parse_args()
    try:
        value = json.loads(args.input)
    except json.JSONDecodeError:
        value = args.input
    export_short(value)
