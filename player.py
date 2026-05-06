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
from PySide6.QtGui import QAction, QActionGroup, QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStatusBar,
    QStyle,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    import mpv  # type: ignore
except OSError as e:
    _bail(f"Failed to load libmpv-2.dll:\n\n{e}")
except ImportError as e:
    _bail(f"python-mpv not installed:\n\n{e}\n\nRun: pip install python-mpv")

# ─── Optional companion modules (LUT mgmt, Clip Inspector) ───────────────────
# These live in playerlib/ so the main file stays manageable as features grow.
try:
    from playerlib import luts as _luts_mod
    from playerlib.inspector import ClipInspectorDialog
except ImportError as e:
    sys.stderr.write(f"[warn] playerlib not importable: {e}\n")
    _luts_mod = None
    ClipInspectorDialog = None  # type: ignore


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


def _fmt_size(num_bytes: int | float | None) -> str:
    if not num_bytes:
        return "?"
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _decode_pixfmt(pixfmt: str) -> str:
    """Turn a libav pixel format like 'yuv422p10le' into a human label."""
    if not pixfmt:
        return ""
    p = pixfmt.lower()
    bits = "8-bit"
    if "10" in p:
        bits = "10-bit"
    elif "12" in p:
        bits = "12-bit"
    elif "16" in p:
        bits = "16-bit"
    chroma = ""
    if "444" in p:
        chroma = "4:4:4"
    elif "422" in p:
        chroma = "4:2:2"
    elif "420" in p:
        chroma = "4:2:0"
    parts = [bits]
    if chroma:
        parts.append(chroma)
    parts.append(f"({pixfmt})")
    return " ".join(parts)


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


# ─── Transport bar ───────────────────────────────────────────────────────────
TRANSPORT_QSS = """
QWidget#transportBar {
    background: #181818;
    color: #e0e0e0;
    border-top: 1px solid #2a2a2a;
}
QWidget#transportBar QToolButton {
    background: transparent;
    border: none;
    padding: 6px;
    color: #e0e0e0;
}
QWidget#transportBar QToolButton:hover {
    background: #2a2a2a;
    border-radius: 4px;
}
QWidget#transportBar QToolButton:pressed {
    background: #333;
}
QWidget#transportBar QLabel {
    color: #b0b0b0;
    font-family: 'Consolas', 'Cascadia Mono', monospace;
    font-size: 9pt;
    min-width: 56px;
}
QWidget#transportBar QSlider::groove:horizontal {
    height: 4px;
    background: #333;
    border-radius: 2px;
}
QWidget#transportBar QSlider::sub-page:horizontal {
    background: #4a90e2;
    border-radius: 2px;
}
QWidget#transportBar QSlider::add-page:horizontal {
    background: #333;
    border-radius: 2px;
}
QWidget#transportBar QSlider::handle:horizontal {
    background: #f0f0f0;
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
}
QWidget#transportBar QSlider::handle:horizontal:hover {
    background: #fff;
}
QWidget#transportBar QSlider#volumeSlider {
    max-width: 90px;
}
"""


class TransportBar(QWidget):
    """Bottom playback controls — play/pause, seek, time, volume, fullscreen."""

    play_pause_clicked = Signal()
    seek_to = Signal(float)              # absolute seconds
    volume_set = Signal(int)             # 0-100
    mute_toggled = Signal()
    fullscreen_toggled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("transportBar")
        self.setStyleSheet(TRANSPORT_QSS)
        self.setFixedHeight(52)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._duration: float = 0.0
        self._user_seeking: bool = False

        style = self.style()
        ic_play = style.standardIcon(QStyle.SP_MediaPlay)
        ic_volume = style.standardIcon(QStyle.SP_MediaVolume)
        ic_full = style.standardIcon(QStyle.SP_TitleBarMaxButton)

        self.btn_play = QToolButton()
        self.btn_play.setIcon(ic_play)
        self.btn_play.setIconSize(self.btn_play.iconSize() * 1.2)
        self.btn_play.setToolTip("Play / Pause (Space)")
        self.btn_play.clicked.connect(self.play_pause_clicked.emit)
        self._icon_play = ic_play
        self._icon_pause = style.standardIcon(QStyle.SP_MediaPause)

        self.lbl_pos = QLabel("0:00")
        self.lbl_pos.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(50)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self._slider_pressed)
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.sliderMoved.connect(self._slider_moved)

        self.lbl_dur = QLabel("0:00")
        self.lbl_dur.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.btn_mute = QToolButton()
        self.btn_mute.setIcon(ic_volume)
        self.btn_mute.setToolTip("Mute (M)")
        self.btn_mute.clicked.connect(self.mute_toggled.emit)
        self._icon_volume = ic_volume
        self._icon_muted = style.standardIcon(QStyle.SP_MediaVolumeMuted)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setObjectName("volumeSlider")
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.valueChanged.connect(self.volume_set.emit)

        self.btn_full = QToolButton()
        self.btn_full.setIcon(ic_full)
        self.btn_full.setToolTip("Fullscreen (F11)")
        self.btn_full.clicked.connect(self.fullscreen_toggled.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        layout.addWidget(self.btn_play)
        layout.addWidget(self.lbl_pos)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.lbl_dur)
        layout.addSpacing(6)
        layout.addWidget(self.btn_mute)
        layout.addWidget(self.vol_slider)
        layout.addSpacing(2)
        layout.addWidget(self.btn_full)

    # ── slot updates from main window ────────────────────────────────────────
    def set_position(self, seconds: float) -> None:
        if self._user_seeking or self._duration <= 0:
            self.lbl_pos.setText(_fmt_seconds(seconds))
            return
        self.slider.blockSignals(True)
        self.slider.setValue(int(seconds / self._duration * 1000))
        self.slider.blockSignals(False)
        self.lbl_pos.setText(_fmt_seconds(seconds))

    def set_duration(self, seconds: float) -> None:
        self._duration = max(seconds, 0.0)
        self.slider.setEnabled(self._duration > 0)
        self.lbl_dur.setText(_fmt_seconds(seconds))

    def set_paused(self, paused: bool) -> None:
        self.btn_play.setIcon(self._icon_play if paused else self._icon_pause)

    def set_volume(self, vol: int) -> None:
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(max(0, min(100, vol)))
        self.vol_slider.blockSignals(False)

    def set_muted(self, muted: bool) -> None:
        self.btn_mute.setIcon(self._icon_muted if muted else self._icon_volume)

    # ── slider drag handling ─────────────────────────────────────────────────
    def _slider_pressed(self) -> None:
        self._user_seeking = True

    def _slider_moved(self, value: int) -> None:
        # Live-update the position label while dragging so the user sees the
        # target time before committing the seek.
        if self._duration > 0:
            self.lbl_pos.setText(_fmt_seconds(self._duration * value / 1000))

    def _slider_released(self) -> None:
        if self._duration > 0:
            target = self._duration * self.slider.value() / 1000
            self.seek_to.emit(target)
        self._user_seeking = False


# ─── Properties dialog ───────────────────────────────────────────────────────
PROPERTIES_QSS = """
QDialog { background: #1a1a1a; }
QTextBrowser {
    background: #111;
    color: #e0e0e0;
    border: 1px solid #2a2a2a;
    border-radius: 4px;
    padding: 8px;
    font-size: 9.5pt;
}
"""


class PropertiesDialog(QDialog):
    """Read-only file/codec/metadata view. Reads fresh values from libmpv,
    falling back to the parent window's observer cache when a fresh read
    returns nothing (which can happen briefly right after a file loads)."""

    def __init__(self, parent: "PlayerWindow", player: "mpv.MPV") -> None:
        super().__init__(parent)
        self.setWindowTitle("File Properties")
        self.resize(620, 560)
        self.setStyleSheet(PROPERTIES_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.text = QTextBrowser(self)
        self.text.setOpenExternalLinks(False)
        layout.addWidget(self.text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.text.setHtml(self._build_html(parent, player))

    @staticmethod
    def _safe_get(player: "mpv.MPV", prop: str, default=None):
        try:
            v = player[prop]
            return v if v is not None else default
        except Exception:
            return default

    def _build_html(self, owner: "PlayerWindow", p: "mpv.MPV") -> str:
        path = self._safe_get(p, "path") or (str(owner._current_file) if owner._current_file else "(no file)")
        fmt = self._safe_get(p, "file-format", "?")
        size = self._safe_get(p, "file-size")
        dur = self._safe_get(p, "duration") or owner._duration
        hwdec = self._safe_get(p, "hwdec-current", "no") or "no"
        fps = self._safe_get(p, "container-fps") or self._safe_get(p, "estimated-vf-fps")

        # track-list is the most reliable source of per-stream codec info —
        # populated at file-load time and structured. The bare video-codec /
        # audio-codec properties sometimes return empty in main-thread reads.
        tracks = self._safe_get(p, "track-list", []) or []
        if not isinstance(tracks, (list, tuple)):
            tracks = []
        v_track = next((t for t in tracks if isinstance(t, dict)
                        and t.get("type") == "video" and t.get("selected")), {})
        a_track = next((t for t in tracks if isinstance(t, dict)
                        and t.get("type") == "audio" and t.get("selected")), {})

        v_codec_id = v_track.get("codec") or self._safe_get(p, "video-format", "")
        v_codec_friendly = v_track.get("codec-desc") or self._safe_get(p, "video-codec", "") or v_codec_id
        a_codec_id = a_track.get("codec") or self._safe_get(p, "audio-codec-name", "")
        a_codec_friendly = a_track.get("codec-desc") or self._safe_get(p, "audio-codec", "") or a_codec_id

        # Resolution / fps fallbacks from track-list when video-params is empty.
        v_track_w = v_track.get("demux-w") or v_track.get("w")
        v_track_h = v_track.get("demux-h") or v_track.get("h")
        v_track_fps = v_track.get("demux-fps")
        if not fps and v_track_fps:
            fps = v_track_fps

        # Prefer cached observer state; fall back to fresh read.
        vp = owner._video_params or _to_plain_dict(self._safe_get(p, "video-params"))
        ap = owner._audio_params or _to_plain_dict(self._safe_get(p, "audio-params"))
        md = owner._metadata or _to_plain_dict(self._safe_get(p, "metadata"))

        # Debug: see what mpv gave us if the dialog still looks empty.
        sys.stderr.write(
            f"[props] tracks={len(tracks)} v_codec={v_codec_friendly!r}/{v_codec_id!r} "
            f"a_codec={a_codec_friendly!r}/{a_codec_id!r} fmt={fmt!r} fps={fps!r} hwdec={hwdec!r}\n"
        )

        rows: list[str] = []
        rows.append(
            "<style>"
            "h3 { color:#4a90e2; margin:14px 0 4px 0; font-size:11pt; "
            "border-bottom:1px solid #2a2a2a; padding-bottom:2px; }"
            "table { border-collapse:collapse; width:100%; margin:0; }"
            "td { padding:3px 8px; vertical-align:top; }"
            "td.k { color:#888; width:35%; }"
            "td.v { color:#e8e8e8; font-family:Consolas,monospace; }"
            "</style>"
        )

        def section(title: str) -> None:
            rows.append(f"<h3>{title}</h3><table>")

        def row(k: str, v) -> None:
            if v in (None, "", "?"):
                return
            rows.append(f'<tr><td class="k">{k}</td><td class="v">{v}</td></tr>')

        def end_section() -> None:
            rows.append("</table>")

        # File
        section("File")
        row("Path", path)
        row("Container", fmt)
        row("Size", _fmt_size(size) if size else None)
        row("Duration", _fmt_seconds(dur) if dur else None)
        end_section()

        # Video
        section("Video")
        if v_codec_friendly and v_codec_id and v_codec_friendly != v_codec_id:
            row("Codec", f"{v_codec_friendly}  ({v_codec_id})")
        else:
            row("Codec", v_codec_friendly or v_codec_id)
        # Resolution: prefer video-params (renderer's truth) then track demux-w/h.
        w = (vp.get("w") or vp.get("dw") or v_track_w) if vp or v_track_w else None
        h = (vp.get("h") or vp.get("dh") or v_track_h) if vp or v_track_h else None
        if w and h:
            row("Resolution", f"{w}x{h}")
        if vp:
            pixfmt = vp.get("pixelformat") or vp.get("hw-pixelformat")
            if pixfmt:
                row("Pixel format", _decode_pixfmt(pixfmt))
            row("Color matrix", vp.get("colormatrix"))
            row("Primaries", vp.get("primaries"))
            row("Transfer (gamma)", vp.get("gamma"))
            row("Range", vp.get("colorlevels"))
            row("Chroma siting", vp.get("chroma-location"))
            sig_peak = vp.get("sig-peak")
            if sig_peak:
                row("Signal peak", f"{sig_peak}")
        if fps:
            try:
                row("Frame rate", f"{float(fps):.3f} fps")
            except (TypeError, ValueError):
                row("Frame rate", str(fps))
        row("Hardware decode", hwdec)
        end_section()

        # Audio
        if a_codec_friendly or a_codec_id:
            section("Audio")
            if a_codec_friendly and a_codec_id and a_codec_friendly != a_codec_id:
                row("Codec", f"{a_codec_friendly}  ({a_codec_id})")
            else:
                row("Codec", a_codec_friendly or a_codec_id)
            if ap:
                if ap.get("samplerate"):
                    row("Sample rate", f"{ap['samplerate']} Hz")
                ch = ap.get("channel-count") or ap.get("channels")
                if ch:
                    row("Channels", ch)
                if ap.get("format"):
                    row("Sample format", ap["format"])
            end_section()

        # Container metadata (Sony cameras embed model, lens, ISO, etc.)
        if md:
            section("Metadata")
            # Surface common camera tags first.
            preferred = ("make", "model", "encoder", "creation_time",
                        "com.android.version", "com.apple.quicktime.make",
                        "com.apple.quicktime.model", "com.apple.quicktime.creationdate")
            seen: set[str] = set()
            for key in preferred:
                if key in md:
                    row(key, md[key])
                    seen.add(key)
            for k in sorted(md):
                if k in seen:
                    continue
                v = md[k]
                if v is None or v == "":
                    continue
                s = str(v)
                if len(s) > 200:
                    s = s[:200] + "…"
                row(k, s)
            end_section()

        return "".join(rows)


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
        self._current_lut: Path | None = None        # legacy single-slot
        self._cst_lut: Path | None = None            # color-space transform
        self._look_lut: Path | None = None           # creative look
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
        # Central widget = video on top, transport bar below.
        central = QWidget(self)
        central.setStyleSheet("background-color: #000;")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # mpv renders into this frame's native HWND (set via wid).
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet("background-color: #000;")
        self.video_frame.setAttribute(Qt.WA_NativeWindow, True)
        self.video_frame.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.video_frame.setFocusPolicy(Qt.StrongFocus)
        self.video_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self.video_frame, 1)

        self.transport = TransportBar()
        outer.addWidget(self.transport, 0)

        # Wire transport bar -> player methods.
        self.transport.play_pause_clicked.connect(self.toggle_pause)
        self.transport.seek_to.connect(self.seek_absolute)
        self.transport.volume_set.connect(self.set_volume)
        self.transport.mute_toggled.connect(self.toggle_mute)
        self.transport.fullscreen_toggled.connect(self._fullscreen_button_pressed)

        self.setCentralWidget(central)

    def _build_status_bar(self) -> None:
        self.status: QStatusBar = self.statusBar()
        # Permanent label keeps file info visible even when a transient toast
        # message is shown via showMessage().
        self._info_label = QLabel("Ready  -  open a video (Ctrl+O)")
        self._info_label.setStyleSheet("padding-left: 6px;")
        self.status.addWidget(self._info_label, 1)

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
                # WASAPI is the native Windows audio backend. We list explicit
                # fallbacks too because `auto` was failing silently in this
                # embedded setup. wasapi,openal,sdl gives us redundancy.
                ao="wasapi,openal,sdl",
                audio_fallback_to_null=False,
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
        # No Space shortcut here — mpv's input handler covers Space natively
        # via the focused video frame, and a Qt accelerator would steal it.
        act_play = QAction("Play / Pause", self)
        act_play.triggered.connect(self.toggle_pause)
        playback_menu.addAction(act_play)
        act_stop = QAction("Stop", self)
        act_stop.triggered.connect(self.stop_playback)
        playback_menu.addAction(act_stop)
        playback_menu.addSeparator()
        # Shortcuts (Left/Right, comma/period) are registered globally in
        # _install_global_shortcuts so they survive fullscreen. We just label
        # them here for menu discoverability.
        act_back10 = QAction("Step Back 10s\tLeft", self)
        act_back10.triggered.connect(lambda: self.seek_relative(-10))
        playback_menu.addAction(act_back10)
        act_fwd10 = QAction("Step Forward 10s\tRight", self)
        act_fwd10.triggered.connect(lambda: self.seek_relative(10))
        playback_menu.addAction(act_fwd10)
        act_frame_back = QAction("Previous Frame\t,", self)
        act_frame_back.triggered.connect(self._frame_step_back)
        playback_menu.addAction(act_frame_back)
        act_frame_fwd = QAction("Next Frame\t.", self)
        act_frame_fwd.triggered.connect(self._frame_step_forward)
        playback_menu.addAction(act_frame_fwd)

        view_menu = bar.addMenu("&View")
        # F11 is a global QShortcut so it works in fullscreen too; we keep
        # the label hint here so the menu is self-documenting.
        act_full = QAction("Fullscreen\tF11", self, checkable=True)
        act_full.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(act_full)
        self.act_full = act_full
        act_props = QAction("Properties...", self, shortcut="Ctrl+I")
        act_props.triggered.connect(self.show_properties)
        view_menu.addAction(act_props)
        act_inspector = QAction("Clip Inspector...", self, shortcut="Ctrl+M")
        act_inspector.triggered.connect(self.show_clip_inspector)
        view_menu.addAction(act_inspector)

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

        # — Color Space Transform (S-Log3 → Rec.709 etc.) —
        self._cst_menu = settings_menu.addMenu("Color Space Transform")
        self._cst_action_group = QActionGroup(self)
        self._cst_action_group.setExclusive(True)
        # — Look LUT (cinematic film emulations + user .cube files) —
        self._look_menu = settings_menu.addMenu("Look LUT")
        self._look_action_group = QActionGroup(self)
        self._look_action_group.setExclusive(True)
        self._populate_lut_menus()

        settings_menu.addSeparator()
        act_load_custom = QAction("Load Custom .cube as Look...", self)
        act_load_custom.triggered.connect(self.load_lut_dialog)
        settings_menu.addAction(act_load_custom)
        act_clear_all_luts = QAction("Clear All LUTs", self)
        act_clear_all_luts.triggered.connect(self.clear_all_luts)
        settings_menu.addAction(act_clear_all_luts)

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
        self._install_global_shortcuts()

    def _install_global_shortcuts(self) -> None:
        """Application-context shortcuts so keys work regardless of which
        widget has focus (transport bar, menu, video frame all coexist)."""
        def add(seq: str, slot) -> None:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(slot)

        # Keys that have to work in fullscreen (when the menu is hidden) and
        # regardless of which child widget holds focus. Menu-based Ctrl+O,
        # Ctrl+I, Ctrl+Q work fine while the menu is visible.
        add("Space", self.toggle_pause)
        add("F11", self._fullscreen_button_pressed)
        add("Escape", self._exit_fullscreen)
        add("M", self.toggle_mute)
        add("Left", lambda: self.seek_relative(-10))
        add("Right", lambda: self.seek_relative(10))
        add("Shift+Left", lambda: self.seek_relative(-1))
        add("Shift+Right", lambda: self.seek_relative(1))
        add("Up", lambda: self._nudge_volume(+5))
        add("Down", lambda: self._nudge_volume(-5))
        add(",", self._frame_step_back)
        add(".", self._frame_step_forward)

    def _exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.toggle_fullscreen(False)

    def _nudge_volume(self, delta: int) -> None:
        new = max(0, min(100, int(self._volume) + delta))
        self.set_volume(new)

    def _frame_step_forward(self) -> None:
        if not self._current_file:
            return
        try:
            self.player.command("frame-step")
            self._show_osd("frame +1")
        except Exception:
            pass

    def _frame_step_back(self) -> None:
        if not self._current_file:
            return
        try:
            self.player.command("frame-back-step")
            self._show_osd("frame -1")
        except Exception:
            pass

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
            # Cached pause state lags one tick — read what we just toggled to.
            now_paused = not self._paused
            self._show_osd("Paused" if now_paused else "Playing")
        except Exception:
            pass

    def stop_playback(self) -> None:
        try:
            self.player.command("stop")
            self._current_file = None
            self.setWindowTitle(self.APP_TITLE)
            self.status.showMessage("Stopped")
            self.transport.set_position(0)
            self.transport.set_duration(0)
        except Exception:
            pass

    def seek_relative(self, seconds: float) -> None:
        if not self._current_file:
            return
        try:
            self.player.seek(seconds, reference="relative", precision="exact")
            sign = "+" if seconds >= 0 else ""
            self._show_osd(f"{sign}{int(seconds)}s")
        except Exception:
            pass

    def seek_absolute(self, seconds: float) -> None:
        if not self._current_file:
            return
        try:
            self.player.seek(seconds, reference="absolute", precision="exact")
        except Exception:
            pass

    def set_volume(self, value: int) -> None:
        try:
            self.player["volume"] = int(value)
            self._show_osd(f"Volume {int(value)}%")
        except Exception:
            pass

    def toggle_mute(self) -> None:
        try:
            self.player.cycle("mute")
            now_muted = not self._muted
            self._show_osd("Muted" if now_muted else "Unmuted")
        except Exception:
            pass

    def toggle_fullscreen(self, checked: bool | None = None) -> None:
        if checked is None:
            checked = not self.isFullScreen()
        if checked:
            self.showFullScreen()
            self.transport.hide()
            self.menuBar().hide()
            self.statusBar().hide()
        else:
            self.showNormal()
            self.transport.show()
            self.menuBar().show()
            self.statusBar().show()
        if hasattr(self, "act_full"):
            self.act_full.setChecked(checked)

    def _fullscreen_button_pressed(self) -> None:
        self.toggle_fullscreen(not self.isFullScreen())

    def _show_osd(self, text: str, duration_ms: int = 1200) -> None:
        # mpv's show-text uses libass; works without the bundled OSC script.
        try:
            self.player.command("show-text", text, duration_ms)
        except Exception:
            pass

    def set_target_prim(self, value: str) -> None:
        self._target_prim = value
        try:
            self.player["target-prim"] = value
            self.status.showMessage(f"Color output: {value}", 3000)
        except Exception as e:
            self._show_error(f"Failed to set target primaries: {e}")

    def _populate_lut_menus(self) -> None:
        """Discover LUT files in luts/{conversions,cinematic,user} and wire
        them into the two action groups. Called at construction; should be
        called again if LUTs are added at runtime."""
        if _luts_mod is None:
            return

        # Helper to add a "None" radio item.
        def add_none(menu: QMenu, group: QActionGroup, on_select) -> None:
            act = QAction("None", self, checkable=True)
            act.setChecked(True)
            act.triggered.connect(lambda _checked, p=None: on_select(p))
            group.addAction(act)
            menu.addAction(act)

        # Helper to add a LUT radio item.
        def add_lut(menu: QMenu, group: QActionGroup, lut, on_select) -> None:
            act = QAction(lut.name, self, checkable=True)
            act.triggered.connect(lambda _checked, p=lut.path: on_select(p))
            group.addAction(act)
            menu.addAction(act)

        catalog = _luts_mod.discover()

        # ── CST menu ──
        self._cst_menu.clear()
        for a in self._cst_action_group.actions():
            self._cst_action_group.removeAction(a)
        add_none(self._cst_menu, self._cst_action_group, self.apply_cst)
        if catalog["conversions"]:
            self._cst_menu.addSeparator()
            for lut in catalog["conversions"]:
                add_lut(self._cst_menu, self._cst_action_group, lut, self.apply_cst)
        else:
            no_data = QAction("(no conversion LUTs found — run lut_generator)", self)
            no_data.setEnabled(False)
            self._cst_menu.addAction(no_data)

        # ── Look menu ──
        self._look_menu.clear()
        for a in self._look_action_group.actions():
            self._look_action_group.removeAction(a)
        add_none(self._look_menu, self._look_action_group, self.apply_look)
        if catalog["cinematic"]:
            self._look_menu.addSeparator()
            cinematic_label = QAction("Film Emulation", self)
            cinematic_label.setEnabled(False)
            self._look_menu.addAction(cinematic_label)
            for lut in catalog["cinematic"]:
                add_lut(self._look_menu, self._look_action_group, lut, self.apply_look)
        if catalog["user"]:
            self._look_menu.addSeparator()
            user_label = QAction("User LUTs", self)
            user_label.setEnabled(False)
            self._look_menu.addAction(user_label)
            for lut in catalog["user"]:
                add_lut(self._look_menu, self._look_action_group, lut, self.apply_look)

    def load_lut_dialog(self) -> None:
        """Open a file dialog to load any .cube — applied as a Look LUT."""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Load Look LUT",
            str(Path.home()),
            f"LUT files ({' '.join('*' + e for e in sorted(LUT_EXTS))});;All files (*.*)",
        )
        if path_str:
            self.apply_look(Path(path_str))

    def apply_cst(self, path: Path | None) -> None:
        """Set the Color Space Transform LUT. Mutually exclusive with Look —
        picking one clears the other (single-slot model uses mpv's `lut`
        property, which is reliable but only holds one LUT at a time)."""
        if path is not None and not path.is_file():
            self._show_error(f"LUT not found: {path}")
            return
        self._cst_lut = path
        if path is not None:
            self._look_lut = None
            self._tick_none(self._look_action_group)
        self._refresh_lut_chain()
        self._update_status()
        if path:
            self._show_osd(f"CST: {path.stem}")
        else:
            self._show_osd("CST cleared")
        # Force a re-render so the new LUT shows immediately even when paused.
        try:
            self.player.command("seek", "0", "relative-percent", "exact")
        except Exception:
            pass

    def apply_look(self, path: Path | None) -> None:
        """Set the Look LUT. Mutually exclusive with CST."""
        if path is not None and not path.is_file():
            self._show_error(f"LUT not found: {path}")
            return
        self._look_lut = path
        if path is not None:
            self._cst_lut = None
            self._tick_none(self._cst_action_group)
        self._refresh_lut_chain()
        self._update_status()
        if path:
            self._show_osd(f"Look: {path.stem}")
        else:
            self._show_osd("Look cleared")
        try:
            self.player.command("seek", "0", "relative-percent", "exact")
        except Exception:
            pass

    @staticmethod
    def _tick_none(group: QActionGroup) -> None:
        for act in group.actions():
            if act.text() == "None":
                act.setChecked(True)
                return

    def clear_all_luts(self) -> None:
        self._cst_lut = None
        self._look_lut = None
        self._refresh_lut_chain()
        # Re-tick the "None" radio in each group.
        for grp in (self._cst_action_group, self._look_action_group):
            for act in grp.actions():
                if act.text() == "None":
                    act.setChecked(True)
                    break
        self._show_osd("All LUTs cleared")
        self.status.showMessage("All LUTs cleared", 3000)

    def _refresh_lut_chain(self) -> None:
        if _luts_mod is None:
            return
        try:
            _luts_mod.apply_to(self.player, cst=self._cst_lut, look=self._look_lut)
        except Exception as e:
            self._show_error(f"Failed to apply LUT chain: {e}")

    def show_clip_inspector(self) -> None:
        if not self._current_file:
            self.status.showMessage("No file loaded.", 3000)
            return
        if ClipInspectorDialog is None:
            QMessageBox.warning(self, "Clip Inspector",
                                "playerlib.inspector module is not available.")
            return
        dlg = ClipInspectorDialog(self, self._current_file)
        dlg.exec()

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

    def show_properties(self) -> None:
        if not self._current_file:
            self.status.showMessage("No file loaded.", 3000)
            return
        dlg = PropertiesDialog(self, self.player)
        dlg.exec()

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
        # Runs on mpv thread. Forward warnings + errors — info/debug would flood.
        if level in ("warn", "error", "fatal"):
            self.bridge.error_message.emit(f"[{component}] {message.strip()}")

    # ── Qt main-thread handlers ──────────────────────────────────────────────
    def _handle_file_loaded(self) -> None:
        self._update_status()
        title = self._current_file.name if self._current_file else self.APP_TITLE
        self.setWindowTitle(f"{title} - {self.APP_TITLE}")
        # One-shot diagnostic to console: confirm audio backend actually started.
        QTimer.singleShot(800, self._log_audio_state)
        # Properties dialog observers may not have fired yet — give them time
        # by re-running update_status once more after a short delay.
        QTimer.singleShot(800, self._update_status)

    def _log_audio_state(self) -> None:
        try:
            ao = self.player.current_ao
        except Exception:
            ao = "?"
        try:
            vol = self.player.volume
        except Exception:
            vol = "?"
        try:
            muted = self.player.mute
        except Exception:
            muted = "?"
        try:
            ac = self.player.audio_codec
        except Exception:
            ac = "?"
        sys.stderr.write(
            f"[audio] ao={ao!r} volume={vol} muted={muted} audio_codec={ac!r}\n"
        )

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
        self.transport.set_position(value)

    def _set_duration(self, value: float) -> None:
        self._duration = value
        self.transport.set_duration(value)
        self._update_status()

    def _set_paused(self, value: bool) -> None:
        self._paused = value
        self.transport.set_paused(value)

    def _set_volume(self, value: int) -> None:
        self._volume = value
        self.transport.set_volume(value)

    def _set_muted(self, value: bool) -> None:
        self._muted = value
        self.transport.set_muted(value)

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
        active_lut = self._cst_lut or self._look_lut
        lut_part = f"   |   LUT: {active_lut.stem}" if active_lut else ""
        self._info_label.setText(
            f"{self._current_file.name}   |   {res}   |   {codec}   |   "
            f"hwdec: {hwdec}   |   {dur_str}{lut_part}"
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
