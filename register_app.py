"""
Register this player with Windows so video files show "FFmpeg Player" in
the right-click "Open with" menu, and so you can set it as the default
handler for video extensions.

    py -3.13 register_app.py             # install
    py -3.13 register_app.py --uninstall # remove

Notes:
  - Writes to HKEY_CURRENT_USER (no admin needed). Per-user only.
  - Uses `pyw.exe` (Windows Python Launcher, no console window) so opening
    a video doesn't flash a black terminal.
  - Once registered, right-click any video → Open with → choose 'FFmpeg
    Player'. Tick "Always use this app" to make it your default for that
    extension. (Windows protects the actual default with a hash; programmatic
    default-setting is unreliable, so the user must do that one click.)
  - If you move the project folder, run this script again to update paths.
"""

from __future__ import annotations

import shutil
import sys
import winreg
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PLAYER_PY = PROJECT_ROOT / "player.py"
ICON_PATH = PROJECT_ROOT / "assets" / "icon.ico"

APP_PROGID = "Suraj.FFmpegPlayer"        # internal id Windows uses
APP_FRIENDLY = "FFmpeg Player"           # what users see in menus
APP_EXE_NAME = "ffmpegplayer.exe"        # virtual exe name under Applications\

VIDEO_EXTS = (
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".m2ts", ".ts", ".mts", ".mxf", ".wmv", ".flv", ".3gp",
)


def _resolve_pyw() -> str:
    """Find pyw.exe (windowless Python launcher). Prefer the Windows Python
    Launcher in System32; fall back to pythonw.exe sibling of sys.executable."""
    found = shutil.which("pyw.exe")
    if found:
        return found
    found = shutil.which("pyw")
    if found:
        return found
    # Fall back to pythonw.exe in the same dir as the active python.exe.
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    if pyw.is_file():
        return str(pyw)
    raise SystemExit("Couldn't find pyw.exe or pythonw.exe. Install Python from python.org.")


def _build_command(pyw: str) -> str:
    """The shell command Windows runs when opening a video with us."""
    # %1 is the file path Windows substitutes. Quote everything to handle
    # paths with spaces.
    if "py" in Path(pyw).name.lower() and Path(pyw).name.lower().startswith("pyw"):
        # Windows Python Launcher — pin to 3.13 so we use the right interpreter.
        return f'"{pyw}" -3.13 "{PLAYER_PY}" "%1"'
    return f'"{pyw}" "{PLAYER_PY}" "%1"'


# ─── Install ─────────────────────────────────────────────────────────────────
def install() -> int:
    if not PLAYER_PY.is_file():
        return _err(f"player.py not found at {PLAYER_PY}")
    if not ICON_PATH.is_file():
        print(f"Warning: icon not found at {ICON_PATH}. Run make_icon.py first for a real icon.")

    pyw = _resolve_pyw()
    cmd = _build_command(pyw)
    icon = str(ICON_PATH) if ICON_PATH.is_file() else ""

    print(f"Registering '{APP_FRIENDLY}' under HKEY_CURRENT_USER")
    print(f"  python launcher: {pyw}")
    print(f"  command:         {cmd}")
    print(f"  icon:            {icon or '(none)'}")

    # 1) Application entry under Software\Classes\Applications\<exe>
    app_root = rf"Software\Classes\Applications\{APP_EXE_NAME}"
    _set(winreg.HKEY_CURRENT_USER, app_root, "FriendlyAppName", APP_FRIENDLY)
    _set(winreg.HKEY_CURRENT_USER, rf"{app_root}\shell\open\command", "", cmd)
    if icon:
        _set(winreg.HKEY_CURRENT_USER, rf"{app_root}\DefaultIcon", "", icon)
    # SupportedTypes lets Windows show the app in "Open with" for these:
    for ext in VIDEO_EXTS:
        _set(winreg.HKEY_CURRENT_USER, rf"{app_root}\SupportedTypes", ext, "")

    # 2) ProgID entry — separate from the Application key; this is the
    # identity Windows attaches to file extensions when set as default.
    _set(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{APP_PROGID}", "", APP_FRIENDLY)
    _set(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{APP_PROGID}", "FriendlyTypeName", APP_FRIENDLY)
    _set(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{APP_PROGID}\shell\open\command", "", cmd)
    if icon:
        _set(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{APP_PROGID}\DefaultIcon", "", icon)

    # 3) Add ProgID to OpenWithProgids for each extension so it appears in
    # the Open With dropdown.
    for ext in VIDEO_EXTS:
        _set(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids",
             APP_PROGID, "", value_type=winreg.REG_NONE, value_bytes=b"")

    print()
    print("Done.")
    print()
    print("Right-click any video file -> Open with -> 'FFmpeg Player'.")
    print("Tick 'Always use this app' to make it the default for that extension.")
    return 0


# ─── Uninstall ───────────────────────────────────────────────────────────────
def uninstall() -> int:
    print(f"Removing '{APP_FRIENDLY}' from HKEY_CURRENT_USER")
    # Top-level keys to nuke.
    for key in (
        rf"Software\Classes\Applications\{APP_EXE_NAME}",
        rf"Software\Classes\{APP_PROGID}",
    ):
        _delete_key_tree(winreg.HKEY_CURRENT_USER, key)

    # Per-extension OpenWithProgids cleanup.
    for ext in VIDEO_EXTS:
        sub = rf"Software\Classes\{ext}\OpenWithProgids"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, APP_PROGID)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    print("Done.")
    return 0


# ─── Registry helpers ────────────────────────────────────────────────────────
def _set(root, subkey: str, name: str, value: str, *,
         value_type=winreg.REG_SZ, value_bytes: bytes | None = None) -> None:
    with winreg.CreateKey(root, subkey) as k:
        if value_bytes is not None:
            winreg.SetValueEx(k, name, 0, value_type, value_bytes)
        else:
            winreg.SetValueEx(k, name, 0, value_type, value)


def _delete_key_tree(root, subkey: str) -> None:
    """Recursively delete a registry key and all its children."""
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_key_tree(root, subkey + "\\" + sub)
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  warn: failed to delete {subkey}: {e}")


def _err(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


# ─── Entry point ─────────────────────────────────────────────────────────────
def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        return uninstall()
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
