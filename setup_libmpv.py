"""
One-time setup: download libmpv-dev archive from shinchiro's SourceForge build
and extract the runtime DLL into vendor/libmpv/.

Run once: `py -3.13 setup_libmpv.py`
Idempotent: skips download if libmpv DLL already extracted.

Notes:
  - Uses curl.exe (ships with Win10+) for the download. SourceForge's /download
    URL serves a JS interstitial; we use the downloads.sourceforge.net direct
    pattern which redirects to a real mirror.
  - Uses 7-Zip's 7z.exe for extraction. The shinchiro build uses BCJ2 filter
    which py7zr doesn't support; 7z.exe handles it. py7zr is kept as a fallback.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

LIBMPV_FILENAME = "mpv-dev-x86_64-20260419-git-06f4ce7.7z"
LIBMPV_URL = (
    "https://downloads.sourceforge.net/project/mpv-player-windows/libmpv/"
    + LIBMPV_FILENAME
)

PROJECT_ROOT = Path(__file__).resolve().parent
VENDOR_DIR = PROJECT_ROOT / "vendor" / "libmpv"
ARCHIVE_PATH = PROJECT_ROOT / LIBMPV_FILENAME

DLL_CANDIDATES = ("libmpv-2.dll", "mpv-2.dll", "libmpv.dll")
SEVENZIP_PATHS = (
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
)


def find_existing_dll() -> Path | None:
    if not VENDOR_DIR.is_dir():
        return None
    for name in DLL_CANDIDATES:
        for p in VENDOR_DIR.rglob(name):
            if p.is_file():
                return p
    return None


def download(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    print(f"  -> {dest}")
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        sys.exit("curl not found. On Windows 10+ it should be in C:\\Windows\\System32.")
    cmd = [curl, "-L", "--fail", "--progress-bar", "-o", str(dest), url]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        sys.exit(f"curl download failed (exit {e.returncode})")


def is_valid_7z(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(6) == b"\x37\x7A\xBC\xAF\x27\x1C"
    except OSError:
        return False


def find_7z_exe() -> str | None:
    found = shutil.which("7z.exe") or shutil.which("7z")
    if found:
        return found
    for p in SEVENZIP_PATHS:
        if Path(p).is_file():
            return p
    return None


def extract_with_7zip(archive: Path, dest: Path, sevenz: str) -> bool:
    print(f"Extracting (7-Zip) {archive.name} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [sevenz, "x", "-y", f"-o{dest}", str(archive)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stdout.decode("utf-8", errors="replace"))
        sys.stderr.write(e.stderr.decode("utf-8", errors="replace"))
        return False


def extract_with_py7zr(archive: Path, dest: Path) -> bool:
    try:
        import py7zr
    except ImportError:
        return False
    print(f"Extracting (py7zr) {archive.name} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with py7zr.SevenZipFile(archive, mode="r") as z:
            z.extractall(path=dest)
        return True
    except Exception as e:
        print(f"  py7zr failed: {e}")
        return False


def extract(archive: Path, dest: Path) -> None:
    sevenz = find_7z_exe()
    if sevenz and extract_with_7zip(archive, dest, sevenz):
        return
    if extract_with_py7zr(archive, dest):
        return
    sys.exit(
        "Extraction failed. Install 7-Zip (winget install 7zip.7zip) "
        "or extract the archive manually into vendor/libmpv/."
    )


def main() -> int:
    existing = find_existing_dll()
    if existing:
        print(f"libmpv already present: {existing}")
        return 0

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    if not is_valid_7z(ARCHIVE_PATH):
        if ARCHIVE_PATH.exists():
            print(f"Removing invalid archive: {ARCHIVE_PATH}")
            ARCHIVE_PATH.unlink()
        download(LIBMPV_URL, ARCHIVE_PATH)
        if not is_valid_7z(ARCHIVE_PATH):
            sys.exit("Downloaded file is not a valid 7z archive.")
    else:
        print(f"Archive already downloaded and valid: {ARCHIVE_PATH}")

    extract(ARCHIVE_PATH, VENDOR_DIR)

    found = find_existing_dll()
    if not found:
        print("ERROR: extracted archive but no libmpv DLL found.")
        print("Contents of vendor/libmpv/:")
        for p in sorted(VENDOR_DIR.rglob("*")):
            print(f"  {p.relative_to(VENDOR_DIR)}")
        return 1

    try:
        ARCHIVE_PATH.unlink()
    except OSError:
        pass

    print(f"OK: libmpv extracted to {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
