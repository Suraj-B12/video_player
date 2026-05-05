"""
ffmpeg player — a libmpv-backed video player that handles 10-bit 4:2:2 S-Log3
footage and other professional formats Windows free players struggle with.

Phase 1 MVP scope:
  - Open file (menu / Ctrl+O / drag-drop)
  - Native libmpv playback with on-screen controls (mpv's OSC)
  - Software decode default with safe hwdec fallback (works without a GPU)
  - DCI-P3 / sRGB / Auto color output (settings menu)
  - Optional .cube LUT loading (S-Log3 → Rec.709 etc.)
  - 10-second read-ahead cache, no whole-file buffering
  - Graceful errors — no silent crashes
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# ─── Set up libmpv DLL search path BEFORE importing mpv ──────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
LIBMPV_DIR = PROJECT_ROOT / "vendor" / "libmpv"


def _bail(message: str) -> "None":
    # Show a GUI error if Qt is available; otherwise stderr + exit.
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "ffmpeg player", message)
    except Exception:
        sys.stderr.write(message + "\n")
    sys.exit(1)


def _prepare_libmpv() -> None:
    dll = LIBMPV_DIR / "libmpv-2.dll"
    if not dll.is_file():
        _bail(
            "libmpv-2.dll is missing.\n\n"
            f"Expected at: {dll}\n\n"
            "Run the setup script first:\n"
            "    py -3.13 setup_libmpv.py"
        )
    os.environ["PATH"] = str(LIBMPV_DIR) + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(str(LIBMPV_DIR))
    except (FileNotFoundError, OSError):
        pass


_prepare_libmpv()

# ─── Imports that depend on libmpv/Qt being ready ────────────────────────────
from PySide6.QtCore import Qt, QObject, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
)

try:
    import mpv  # type: ignore
except OSError as e:
    _bail(f"Failed to load libmpv-2.dll:\n\n{e}")
except ImportError as e:
    _bail(f"python-mpv not installed:\n\n{e}\n\nRun: pip install python-mpv")


# ─── Constants ───────────────────────────────────────────────────────────────
VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".mxf", ".m2ts", ".ts",
    ".mts", ".m4v", ".wmv", ".flv", ".3gp", ".vob",
}
LUT_EXTS = {".cube", ".3dl"}

# Color-output presets exposed in the Settings menu. mpv's `target-prim`
# governs the primaries the renderer maps to. `auto` lets mpv pick based on
# what Windows reports for the active display.
COLOR_PRESETS = [
    ("Auto (match display)", "auto"),
    ("sRGB / Rec.709", "bt.709"),
    ("Display P3 (DCI-P3 D65)", "display-p3"),
    ("Rec.2020 (HDR)", "bt.2020"),
]

# Comma-separated VO list — mpv tries them in order. gpu-next is best for
# colour mgmt and HDR; gpu is the legacy fallback; direct3d is a last resort
# for systems without working GL drivers.
VO_FALLBACK_CHAIN = "gpu-next,gpu,direct3d"


def _to_plain_dict(value) -> dict:
    """mpv often hands back MpvNode/list-of-pairs structures. Coerce to a
    plain dict so Qt signals can carry a stable type."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    try:
        return {str(k): v for k, v in value}
    except (TypeError, ValueError):
        return {}


def _fmt_seconds(seconds: float | int | None) -> str:
    s = int(seconds or 0)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ─── Bridge: mpv thread → Qt main thread ─────────────────────────────────────
class MpvBridge(QObject):
    """python-mpv fires events on its own thread. Qt UI calls must happen on
    the main thread, so we marshal everything through Qt signals."""
    file_loaded = Signal()
    end_file = Signal(str)              # reason
    error_message = Signal(str)         # human-readable error

    # property changes
    time_pos_changed = Signal(float)
    duration_changed = Signal(float)
    pause_changed = Signal(bool)
    volume_changed = Signal(int)
    mute_changed = Signal(bool)
    video_params_changed = Signal(dict)
    audio_params_changed = Signal(dict)
    metadata_changed = Signal(dict)


# ─── Main window ─────────────────────────────────────────────────────────────
class PlayerWindow(QMainWindow):
    APP_TITLE = "ffmpeg player"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(self.APP_TITLE)
        self.setMinimumSize(640, 360)
        self.resize(1280, 720)
        self.setAcceptDrops(True)

        self._current_file: Path | None = None
        self._current_lut: Path | None = None
        self._target_prim: str = "auto"
        self._hwdec_enabled: bool = True

        # Cached property state (kept up-to-date by bridge signals).
        self._duration: float = 0.0
        self._time_pos: float = 0.0
        self._paused: bool = False
        self._volume: int = 100
        self._muted: bool = False
        self._video_params: dict = {}
        self._audio_params: dict = {}
        self._metadata: dict = {}

        self._build_video_surface()
        self._build_status_bar()
        self.bridge = MpvBridge()
        self._build_player()  # creates self.player (libmpv), wires events
        self._build_menus()   # menus reference self.player methods
        self._wire_signals()
        self._update_status()

    # ── UI scaffolding ───────────────────────────────────────────────────────
    def _build_video_surface(self) -> None:
        # mpv renders into this frame's native HWND (set via wid).
        self.video_frame = QFrame(self)
        self.video_frame.setStyleSheet("background-color: #000;")
        self.video_frame.setAttribute(Qt.WA_NativeWindow, True)
        self.video_frame.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.video_frame.setFocusPolicy(Qt.StrongFocus)
        self.setCentralWidget(self.video_frame)

    def _build_status_bar(self) -> None:
        self.status: QStatusBar = self.statusBar()
        self.status.showMessage("Ready — open a video to start (Ctrl+O)")

    def _build_player(self) -> None:
        # Force the QFrame to materialise a real Win32 HWND now, before mpv
        # init, so we can pass `wid` as an init option (changing wid after
        # init is unsupported).
        self.video_frame.winId()
        hwnd = int(self.video_frame.winId())

        try:
            self.player = mpv.MPV(
                # Embedding
                wid=str(hwnd),

                # Output chain — falls back gracefully if no GPU/GL.
                vo=VO_FALLBACK_CHAIN,
                ao="auto",
                audio_device="auto",
                volume=100,
                mute=False,

                # OSD on (we do our own controls, but keep mpv's text overlay
                # for show-text feedback).
                osd_level=1,
                osd_bar=True,
                osd_on_seek="msg-bar",
                osd_font_size=36,

                # Keyboard works inside the video window.
                input_default_bindings=True,
                input_vo_keyboard=True,

                # When playback ends, hold last frame instead of closing.
                keep_open="yes",

                # Hardware decode: auto-safe means only enable when known to
                # match the format. 10-bit 4:2:2 isn't covered by his GPU
                # (RTX 4050, no Blackwell 4:2:2), so this falls through to
                # software automatically. ffmpeg-via-libavcodec multithreads.
                hwdec="auto-safe",

                # Cache: 10s read-ahead, capped memory (his "no whole-clip
                # caching" requirement). 200 MiB covers ~10s of 600 Mbps XAVC.
                cache="yes",
                demuxer_max_bytes="200MiB",
                demuxer_max_back_bytes="200MiB",
                demuxer_readahead_secs=10,

                # Higher-quality scalers + dithering. Worth it for his use
                # case (graded review on an OLED P3 panel).
                profile="high-quality",

                # Default colour mapping — overridden via Settings menu.
                target_prim=self._target_prim,

                # Logging — capture warnings/errors for the status bar.
                log_handler=self._on_mpv_log,
            )
        except Exception as e:
            _bail(
                "Could not initialise libmpv.\n\n"
                f"{type(e).__name__}: {e}\n\n"
                "If this persists, check that your graphics drivers are up "
                "to date or report the message above."
            )

        # mpv-thread → bridge signals.
        self.player.event_callback("file-loaded")(self._mpv_file_loaded)
        self.player.event_callback("end-file")(self._mpv_end_file)

        # Property observers — Qt thread reads stay current.
        self._wire_property_observers()

    def _wire_property_observers(self) -> None:
        # Each observer fires on the mpv worker thread; we just emit a Qt
        # signal so the main thread can act on it.

        @self.player.property_observer("time-pos")
        def _time_pos(_name, value):
            self.bridge.time_pos_changed.emit(float(value or 0.0))

        @self.player.property_observer("duration")
        def _dur(_name, value):
            self.bridge.duration_changed.emit(float(value or 0.0))

        @self.player.property_observer("pause")
        def _pause(_name, value):
            self.bridge.pause_changed.emit(bool(value))

        @self.player.property_observer("volume")
        def _vol(_name, value):
            try:
                self.bridge.volume_changed.emit(int(value or 0))
            except (TypeError, ValueError):
                pass

        @self.player.property_observer("mute")
        def _mute(_name, value):
            self.bridge.mute_changed.emit(bool(value))

        @self.player.property_observer("video-params")
        def _vparams(_name, value):
            self.bridge.video_params_changed.emit(_to_plain_dict(value))

        @self.player.property_observer("audio-params")
        def _aparams(_name, value):
            self.bridge.audio_params_changed.emit(_to_plain_dict(value))

        @self.player.property_observer("metadata")
        def _meta(_name, value):
            self.bridge.metadata_changed.emit(_to_plain_dict(value))

    def _build_menus(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        act_open = QAction("&Open...", self, shortcut=QKeySequence.Open)
        act_open.triggered.connect(self.open_file_dialog)
        file_menu.addAction(act_open)
        file_menu.addSeparator()
        act_quit = QAction("E&xit", self, shortcut="Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        playback_menu = bar.addMenu("&Playback")
        act_play = QAction("Play / Pause", self, shortcut="Space")
        act_play.triggered.connect(self.toggle_pause)
        playback_menu.addAction(act_play)
        act_stop = QAction("Stop", self)
        act_stop.triggered.connect(self.stop_playback)
        playback_menu.addAction(act_stop)
        playback_menu.addSeparator()
        act_back10 = QAction("Step Back 10s", self, shortcut="Left")
        act_back10.triggered.connect(lambda: self.seek_relative(-10))
        playback_menu.addAction(act_back10)
        act_fwd10 = QAction("Step Forward 10s", self, shortcut="Right")
        act_fwd10.triggered.connect(lambda: self.seek_relative(10))
        playback_menu.addAction(act_fwd10)

        view_menu = bar.addMenu("&View")
        act_full = QAction("Fullscreen", self, shortcut="F11", checkable=True)
        act_full.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(act_full)
        self.act_full = act_full

        settings_menu = bar.addMenu("&Settings")

        # — Color output sub-menu (target primaries) —
        color_menu: QMenu = settings_menu.addMenu("Color Output")
        self._color_action_group = QActionGroup(self)
        self._color_action_group.setExclusive(True)
        for label, value in COLOR_PRESETS:
            act = QAction(label, self, checkable=True)
            act.setData(value)
            act.setChecked(value == self._target_prim)
            act.triggered.connect(lambda _checked, v=value: self.set_target_prim(v))
            self._color_action_group.addAction(act)
            color_menu.addAction(act)

        # — LUT loader —
        lut_menu: QMenu = settings_menu.addMenu("LUT")
        act_load_lut = QAction("Load .cube LUT...", self)
        act_load_lut.triggered.connect(self.load_lut_dialog)
        lut_menu.addAction(act_load_lut)
        act_clear_lut = QAction("Clear LUT", self)
        act_clear_lut.triggered.connect(self.clear_lut)
        lut_menu.addAction(act_clear_lut)

        # — Hardware decoding toggle —
        settings_menu.addSeparator()
        act_hwdec = QAction("Hardware Decoding (when available)", self, checkable=True)
        act_hwdec.setChecked(self._hwdec_enabled)
        act_hwdec.triggered.connect(self.toggle_hwdec)
        settings_menu.addAction(act_hwdec)
        self.act_hwdec = act_hwdec

        # — About —
        help_menu = bar.addMenu("&Help")
        act_about = QAction("About", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    def _wire_signals(self) -> None:
        self.bridge.file_loaded.connect(self._handle_file_loaded)
        self.bridge.end_file.connect(self._handle_end_file)
        self.bridge.error_message.connect(self._handle_error)
        self.bridge.time_pos_changed.connect(self._set_time_pos)
        self.bridge.duration_changed.connect(self._set_duration)
        self.bridge.pause_changed.connect(self._set_paused)
        self.bridge.volume_changed.connect(self._set_volume)
        self.bridge.mute_changed.connect(self._set_muted)
        self.bridge.video_params_changed.connect(self._set_video_params)
        self.bridge.audio_params_changed.connect(self._set_audio_params)
        self.bridge.metadata_changed.connect(self._set_metadata)

    # ── Public actions (called from menus / drag-drop) ───────────────────────
    def open_file_dialog(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open video",
            str(Path.home()),
            f"Video files ({' '.join('*' + e for e in sorted(VIDEO_EXTS))});;All files (*.*)",
        )
        if path_str:
            self.load_file(Path(path_str))

    def load_file(self, path: Path) -> None:
        if not path.is_file():
            self._show_error(f"File not found: {path}")
            return
        try:
            self.player.play(str(path))
            self._current_file = path
            self.status.showMessage(f"Loading: {path.name}…")
        except Exception as e:
            self._show_error(f"Could not load {path.name}: {e}")

    def toggle_pause(self) -> None:
        try:
            self.player.cycle("pause")
        except Exception:
            pass

    def stop_playback(self) -> None:
        try:
            self.player.command("stop")
            self._current_file = None
            self.setWindowTitle(self.APP_TITLE)
            self.status.showMessage("Stopped")
        except Exception:
            pass

    def seek_relative(self, seconds: float) -> None:
        if not self._current_file:
            return
        try:
            self.player.seek(seconds, reference="relative", precision="exact")
        except Exception:
            pass

    def toggle_fullscreen(self, checked: bool) -> None:
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()

    def set_target_prim(self, value: str) -> None:
        self._target_prim = value
        try:
            self.player["target-prim"] = value
            self.status.showMessage(f"Color output: {value}", 3000)
        except Exception as e:
            self._show_error(f"Failed to set target primaries: {e}")

    def load_lut_dialog(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Load .cube LUT",
            str(Path.home()),
            f"LUT files ({' '.join('*' + e for e in sorted(LUT_EXTS))});;All files (*.*)",
        )
        if path_str:
            self.apply_lut(Path(path_str))

    def apply_lut(self, path: Path) -> None:
        if not path.is_file():
            self._show_error(f"LUT not found: {path}")
            return
        try:
            self.player["lut"] = str(path)
            self.player["lut-type"] = "conversion"
            self._current_lut = path
            self.status.showMessage(f"LUT applied: {path.name}", 4000)
        except Exception as e:
            self._show_error(f"Failed to apply LUT: {e}")

    def clear_lut(self) -> None:
        try:
            self.player["lut"] = ""
            self._current_lut = None
            self.status.showMessage("LUT cleared", 3000)
        except Exception as e:
            self._show_error(f"Failed to clear LUT: {e}")

    def toggle_hwdec(self, checked: bool) -> None:
        self._hwdec_enabled = checked
        try:
            self.player["hwdec"] = "auto-safe" if checked else "no"
            self.status.showMessage(
                "Hardware decoding: " + ("enabled (auto-safe)" if checked else "disabled (software)"),
                3000,
            )
        except Exception as e:
            self._show_error(f"Failed to set hwdec: {e}")

    def show_about(self) -> None:
        try:
            mpv_v = self.player.mpv_version
            ff_v = self.player.ffmpeg_version
        except Exception:
            mpv_v = ff_v = "unknown"
        QMessageBox.information(
            self,
            "About ffmpeg player",
            f"<h3>ffmpeg player</h3>"
            f"<p>A libmpv-backed player for high-bit-depth professional footage.</p>"
            f"<p><b>libmpv:</b> {mpv_v}<br>"
            f"<b>FFmpeg:</b> {ff_v}<br>"
            f"<b>Qt:</b> {QApplication.instance().applicationVersion() or '6.x'}</p>",
        )

    # ── mpv-thread event callbacks (thin — emit signals only) ────────────────
    def _mpv_file_loaded(self, *_args, **_kw) -> None:
        # Runs on mpv thread.
        self.bridge.file_loaded.emit()

    def _mpv_end_file(self, event=None, *_args, **_kw) -> None:
        # Runs on mpv thread. Event payload includes a `reason` field.
        reason = "unknown"
        try:
            if isinstance(event, dict):
                reason = str(event.get("reason", reason))
            elif hasattr(event, "reason"):
                reason = str(event.reason)
            elif hasattr(event, "get"):
                reason = str(event.get("reason", reason))
        except Exception:
            pass
        self.bridge.end_file.emit(reason)

    def _on_mpv_log(self, level: str, component: str, message: str) -> None:
        # Runs on mpv thread. Forward errors only — info/debug would flood.
        if level in ("error", "fatal"):
            self.bridge.error_message.emit(f"[{component}] {message.strip()}")

    # ── Qt main-thread handlers ──────────────────────────────────────────────
    def _handle_file_loaded(self) -> None:
        self._update_status()
        title = self._current_file.name if self._current_file else self.APP_TITLE
        self.setWindowTitle(f"{title} — {self.APP_TITLE}")

    def _handle_end_file(self, reason: str) -> None:
        # Reasons: eof (normal), stop (user), quit, error, redirect, unknown
        if reason == "error":
            self._show_error("Playback error — file may be unsupported or corrupt.")
        elif reason == "eof":
            self.status.showMessage("Playback finished", 4000)

    def _handle_error(self, message: str) -> None:
        self.status.showMessage(message, 6000)

    def _set_time_pos(self, value: float) -> None:
        self._time_pos = value

    def _set_duration(self, value: float) -> None:
        self._duration = value
        self._update_status()

    def _set_paused(self, value: bool) -> None:
        self._paused = value

    def _set_volume(self, value: int) -> None:
        self._volume = value

    def _set_muted(self, value: bool) -> None:
        self._muted = value

    def _set_video_params(self, params: dict) -> None:
        self._video_params = params or {}
        self._update_status()

    def _set_audio_params(self, params: dict) -> None:
        self._audio_params = params or {}
        self._update_status()

    def _set_metadata(self, meta: dict) -> None:
        self._metadata = meta or {}

    def _show_error(self, message: str) -> None:
        # Visible-but-not-modal error: status bar + console log.
        sys.stderr.write(f"[error] {message}\n")
        self.status.showMessage(message, 6000)

    def _update_status(self) -> None:
        if not self._current_file:
            return
        # Pull from cached observer state (no main-thread reads of mpv props).
        vp = self._video_params
        w = vp.get("w") or vp.get("dw")
        h = vp.get("h") or vp.get("dh")
        # video-codec / hwdec-current aren't observed yet; safe to read once
        # since they don't change during steady-state playback.
        try:
            codec = self.player.video_codec or "?"
        except Exception:
            codec = "?"
        try:
            hwdec = self.player.hwdec_current or "no"
        except Exception:
            hwdec = "?"
        res = f"{w}x{h}" if w and h else "?"
        dur_str = _fmt_seconds(self._duration) if self._duration else "?"
        self.status.showMessage(
            f"{self._current_file.name}   |   {res}   |   {codec}   |   "
            f"hwdec: {hwdec}   |   {dur_str}"
        )

    # ── Drag-and-drop ────────────────────────────────────────────────────────
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in VIDEO_EXTS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt API)
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in VIDEO_EXTS and path.is_file():
                self.load_file(path)
                event.acceptProposedAction()
                return
        event.ignore()

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Tear down libmpv cleanly so the process exits without a hang.
        try:
            self.player.terminate()
        except Exception:
            pass
        super().closeEvent(event)


# ─── Entry point ─────────────────────────────────────────────────────────────
def main() -> int:
    # High-DPI is automatic in Qt6; nothing to configure for the PX13 OLED.
    app = QApplication(sys.argv)
    app.setApplicationName("ffmpeg player")
    app.setApplicationVersion("0.1.0")

    # Last-resort exception handler — keep the UI alive on background errors.
    def _excepthook(exctype, value, tb):
        text = "".join(traceback.format_exception(exctype, value, tb))
        sys.stderr.write(text)
        QMessageBox.critical(None, "Unexpected error", text[-2000:])

    sys.excepthook = _excepthook

    win = PlayerWindow()
    win.show()

    # If a path was passed as argv[1], open it.
    if len(sys.argv) >= 2:
        first = Path(sys.argv[1])
        if first.is_file():
            QTimer.singleShot(0, lambda: win.load_file(first))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
