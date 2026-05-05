"""
Metadata extraction from a video file using two external tools:

  - ffprobe : structural data (codec, container, color side-data, bitrate,
              every stream)
  - exiftool: EXIF + Sony-proprietary tags (camera body, lens, focal length,
              aperture, ISO, shutter, picture profile, etc.)

Both run as subprocesses with a short timeout so a hung tool can't lock up
the UI. Either or both may be missing on the system; we degrade gracefully.

Tool-locating strategy (in order):
  1. Same path the harness already used (`shutil.which`)
  2. The known-installed locations from this project's setup
  3. Bundled FFmpeg directory in the user's Downloads (legacy install)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Known fallback locations on this user's machine. Generic enough to ship.
_FFPROBE_CANDIDATES = (
    "ffprobe",
    "ffprobe.exe",
    r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
    r"C:\Users\suraj\Downloads\ffmpeg-master-latest-win64-gpl-shared\ffmpeg-master-latest-win64-gpl-shared\bin\ffprobe.exe",
)
_EXIFTOOL_CANDIDATES = (
    "exiftool",
    "exiftool.exe",
    "ExifTool.exe",
    r"C:\Users\suraj\AppData\Local\Programs\Exiftool\ExifTool.exe",
    r"C:\Program Files\Exiftool\ExifTool.exe",
)

PROBE_TIMEOUT_SECS = 15


def _find(name_candidates: tuple[str, ...]) -> str | None:
    for c in name_candidates:
        # Plain names go through PATH; absolute paths are checked literally.
        if "\\" in c or "/" in c:
            if Path(c).is_file():
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def has_ffprobe() -> bool:
    return _find(_FFPROBE_CANDIDATES) is not None


def has_exiftool() -> bool:
    return _find(_EXIFTOOL_CANDIDATES) is not None


def run_ffprobe(path: Path) -> dict[str, Any]:
    """Return a structured dict from `ffprobe -show_format -show_streams`.
    On failure returns {}."""
    exe = _find(_FFPROBE_CANDIDATES)
    if not exe:
        return {"_error": "ffprobe not found"}
    cmd = [
        exe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-show_programs",
        "-show_error",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECS,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"_error": f"ffprobe timed out after {PROBE_TIMEOUT_SECS}s"}
    except (OSError, ValueError) as e:
        return {"_error": f"ffprobe spawn failed: {e}"}

    if proc.returncode != 0:
        return {"_error": f"ffprobe exit {proc.returncode}: {proc.stderr.strip()[:300]}"}
    try:
        return json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError as e:
        return {"_error": f"ffprobe JSON parse failed: {e}"}


def run_exiftool(path: Path) -> dict[str, Any]:
    """Return a single dict of all EXIF tags. exiftool -j returns a list of
    one dict per file; we unwrap to that dict."""
    exe = _find(_EXIFTOOL_CANDIDATES)
    if not exe:
        return {"_error": "exiftool not installed"}
    cmd = [
        exe,
        "-json",
        "-G",                # group prefix on tag names (e.g. 'Sony:LensModel')
        "-n",                # numerical (not human-formatted) — keeps GPS as decimal
        "-a",                # allow duplicate tags
        "--ext", "-",        # don't follow extension restrictions
        "-fast2",            # don't read past metadata blocks
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECS,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"_error": f"exiftool timed out after {PROBE_TIMEOUT_SECS}s"}
    except (OSError, ValueError) as e:
        return {"_error": f"exiftool spawn failed: {e}"}

    if proc.returncode != 0 and not proc.stdout:
        return {"_error": f"exiftool exit {proc.returncode}: {proc.stderr.strip()[:300]}"}
    try:
        data = json.loads(proc.stdout) if proc.stdout else []
    except json.JSONDecodeError as e:
        return {"_error": f"exiftool JSON parse failed: {e}"}

    if not data:
        return {}
    # Format is `[{"SourceFile": ..., "EXIF:Make": ..., ...}]`.
    return data[0] if isinstance(data, list) else dict(data)


def collect(path: Path) -> dict[str, Any]:
    """Run both probes and return the combined result. Tools missing or
    failing are reported in the corresponding sub-dict's `_error` key."""
    return {
        "path": str(path),
        "ffprobe": run_ffprobe(path),
        "exiftool": run_exiftool(path),
    }
