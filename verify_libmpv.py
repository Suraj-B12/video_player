"""
Phase 0 verification: confirm libmpv-2.dll can be loaded by python-mpv.

Prints versions, then shuts down cleanly. If this works, the toolchain is ready
for Phase 1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LIBMPV_DIR = PROJECT_ROOT / "vendor" / "libmpv"


def main() -> int:
    if not LIBMPV_DIR.is_dir():
        print(f"libmpv directory not found: {LIBMPV_DIR}", file=sys.stderr)
        print("Run: py -3.13 setup_libmpv.py", file=sys.stderr)
        return 1

    dll = LIBMPV_DIR / "libmpv-2.dll"
    if not dll.is_file():
        print(f"libmpv-2.dll not found at {dll}", file=sys.stderr)
        return 1

    # python-mpv's ctypes loader scans %PATH% — prepend our vendor dir so it
    # finds libmpv-2.dll. add_dll_directory alone isn't sufficient for this lib.
    os.environ["PATH"] = str(LIBMPV_DIR) + os.pathsep + os.environ.get("PATH", "")
    os.add_dll_directory(str(LIBMPV_DIR))  # belt-and-suspenders for any deps

    # Import after DLL path is set.
    try:
        import mpv  # type: ignore
    except OSError as e:
        print(f"Failed to load libmpv: {e}", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"python-mpv not installed: {e}", file=sys.stderr)
        return 1

    # Create a headless mpv instance — no window, no audio, no video output.
    # Just want to confirm libmpv initializes and we can read properties.
    try:
        player = mpv.MPV(vo="null", ao="null", video=False, audio=False)
    except Exception as e:
        print(f"Failed to create MPV instance: {e}", file=sys.stderr)
        return 1

    try:
        print(f"python-mpv:    {getattr(mpv, '__version__', 'unknown')}")
        print(f"libmpv:        {player.mpv_version}")
        print(f"ffmpeg:        {player.ffmpeg_version}")
        print(f"libass:        {getattr(player, 'libass_version', 'n/a')}")
    finally:
        player.terminate()

    print("\nOK: libmpv loaded, initialized, and shut down cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
