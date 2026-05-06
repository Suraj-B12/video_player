"""
Build a real Windows executable for Deli Player using PyInstaller. Once
built, run register_app.py and Open With will show "Deli Player" (instead
of "Python") because the .exe carries its own embedded FileDescription.

    py -3.13 build_exe.py

Output:
    dist/DeliPlayer/DeliPlayer.exe       (the launcher)
    dist/DeliPlayer/libmpv-2.dll          (sibling — ctypes finds it via PATH)
    dist/DeliPlayer/luts/                 (LUT library)
    dist/DeliPlayer/assets/icon.ico       (icon, also embedded in the .exe)
    dist/DeliPlayer/_internal/            (Python + Qt runtime — ignore)

This is a `--onedir` build: a folder you can move/copy as a unit. Startup
is faster than `--onefile` (no temp extraction) and the bundle is easier
to debug.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PLAYER_PY = PROJECT_ROOT / "player.py"
ICON = PROJECT_ROOT / "assets" / "icon.ico"
LIBMPV_DLL = PROJECT_ROOT / "vendor" / "libmpv" / "libmpv-2.dll"
LUTS_DIR = PROJECT_ROOT / "luts"
ASSETS_DIR = PROJECT_ROOT / "assets"
PLAYERLIB_DIR = PROJECT_ROOT / "playerlib"

DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
SPEC_FILE = PROJECT_ROOT / "DeliPlayer.spec"


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    print("PyInstaller not installed; installing...")
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--quiet", "pyinstaller"]
    subprocess.run(cmd, check=True)


def check_inputs() -> None:
    missing = [str(p) for p in (PLAYER_PY, ICON, LIBMPV_DLL, LUTS_DIR, PLAYERLIB_DIR) if not p.exists()]
    if missing:
        sys.exit("Missing required files:\n  " + "\n  ".join(missing) +
                 "\n\nMake sure setup_libmpv.py and make_icon.py have run.")


def clean() -> None:
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            print(f"Removing {d}")
            shutil.rmtree(d, ignore_errors=True)
    if SPEC_FILE.exists():
        SPEC_FILE.unlink()


def build() -> None:
    # PyInstaller's --add-data uses ';' as src/dest separator on Windows.
    sep = ";"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",                            # no console window
        "--onedir",                              # folder mode (faster startup)
        "--name", "DeliPlayer",
        "--icon", str(ICON),
        "--add-binary", f"{LIBMPV_DLL}{sep}.",    # libmpv-2.dll next to exe
        "--add-data", f"{LUTS_DIR}{sep}luts",    # luts folder
        "--add-data", f"{ASSETS_DIR}{sep}assets",
        "--add-data", f"{PLAYERLIB_DIR}{sep}playerlib",
        # Hidden imports — PyInstaller can miss runtime ctypes loads.
        "--hidden-import", "mpv",
        "--collect-submodules", "playerlib",
        str(PLAYER_PY),
    ]
    print("Running:")
    print("  " + " ".join(f'"{a}"' if " " in a else a for a in cmd))
    print()
    subprocess.run(cmd, check=True)


def post_build_sanity() -> None:
    bundle = DIST_DIR / "DeliPlayer"
    exe = bundle / "DeliPlayer.exe"
    if not exe.is_file():
        sys.exit(f"Build finished but {exe} not found.")
    print(f"\nOK: {exe}  ({exe.stat().st_size // 1024} KB)")

    # PyInstaller drops --add-data and --add-binary content into _internal/.
    # The runtime code (PROJECT_ROOT = exe.parent) expects libmpv-2.dll, luts/
    # and assets/ at the bundle root, so lift them up.
    deep_internal = bundle / "_internal"
    for name in ("libmpv-2.dll", "luts", "assets"):
        src = deep_internal / name
        dst = bundle / name
        if src.exists() and not dst.exists():
            print(f"Lifting {name} from _internal/ to bundle root")
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    print()
    print("Next: register the .exe with Windows so Open With shows 'Deli Player':")
    print("  py -3.13 register_app.py")


def main() -> int:
    check_inputs()
    ensure_pyinstaller()
    clean()
    build()
    post_build_sanity()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
