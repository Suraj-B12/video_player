"""
Catalyst-style Clip Inspector dialog.

Spawns ffprobe + exiftool in a worker thread and presents the results across
five tabs:

  Summary        — at-a-glance camera / lens / picture-profile / codec
  Camera & Lens  — Sony / EXIF tags grouped (body, lens, exposure, settings)
  Streams        — every stream from ffprobe (video, audio, data, timecode)
  Color          — color science details (matrix, primaries, transfer, HDR)
  All Metadata   — searchable tree of every tag from both tools

Worker pattern keeps the UI alive — exiftool can take 1-2s on Sony XAVC
files. The dialog opens immediately with a Loading… banner and swaps in
real content when the worker finishes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import metadata as md_mod

# ─── Styling ─────────────────────────────────────────────────────────────────
INSPECTOR_QSS = """
QDialog { background: #1a1a1a; }
QTabWidget::pane { border: 1px solid #2a2a2a; background: #111; }
QTabBar::tab {
    background: #1a1a1a; color: #aaa;
    padding: 6px 14px; border: 1px solid #2a2a2a;
    border-bottom: none; min-width: 80px;
}
QTabBar::tab:selected { background: #111; color: #fff; }
QTabBar::tab:hover:!selected { background: #232323; color: #ddd; }
QTextBrowser, QTreeWidget {
    background: #111; color: #e0e0e0;
    border: none; padding: 8px;
    font-size: 9.5pt;
}
QTreeWidget {
    selection-background-color: #2c4f7c;
    alternate-background-color: #161616;
}
QHeaderView::section {
    background: #1a1a1a; color: #aaa;
    padding: 4px; border: none; border-bottom: 1px solid #2a2a2a;
}
QLineEdit {
    background: #181818; color: #e0e0e0;
    border: 1px solid #2a2a2a; border-radius: 3px;
    padding: 4px 8px;
}
QLineEdit:focus { border-color: #4a90e2; }
QLabel#path {
    color: #aaa; font-family: Consolas, monospace; font-size: 9pt;
}
QPushButton {
    background: #2a2a2a; color: #e0e0e0;
    border: 1px solid #3a3a3a; border-radius: 3px;
    padding: 4px 12px;
}
QPushButton:hover { background: #333; }
"""

ROW_STYLE = (
    "<style>"
    "h3 { color:#4a90e2; margin:14px 0 4px 0; font-size:11pt; "
    "border-bottom:1px solid #2a2a2a; padding-bottom:2px; }"
    "table { border-collapse:collapse; width:100%; margin:0; }"
    "td { padding:3px 8px; vertical-align:top; }"
    "td.k { color:#888; width:38%; }"
    "td.v { color:#e8e8e8; font-family:Consolas,monospace; word-break:break-all; }"
    ".muted { color:#666; font-style:italic; }"
    "</style>"
)


# ─── Worker (runs probes off the main thread) ────────────────────────────────
class _MetadataWorker(QObject):
    finished = Signal(dict)

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            data = md_mod.collect(self._path)
        except Exception as e:
            data = {"_error": f"{type(e).__name__}: {e}"}
        self.finished.emit(data)


# ─── HTML helpers ────────────────────────────────────────────────────────────
def _html_table(rows: list[tuple[str, Any]], skip_empty: bool = True) -> str:
    """Build a key/value HTML table. Rows with empty values are dropped."""
    out = ['<table>']
    any_row = False
    for k, v in rows:
        if skip_empty and (v is None or v == "" or v == "?"):
            continue
        any_row = True
        out.append(f'<tr><td class="k">{k}</td><td class="v">{v}</td></tr>')
    out.append('</table>')
    if not any_row:
        return '<p class="muted">No data available.</p>'
    return "".join(out)


def _e(d: dict, *keys: str, default=None):
    """First non-empty value found across the listed keys."""
    for k in keys:
        if k in d:
            v = d[k]
            if v not in (None, "", "?"):
                return v
    return default


def _video_stream(ffp: dict) -> dict:
    for s in ffp.get("streams", []) or []:
        if isinstance(s, dict) and s.get("codec_type") == "video":
            return s
    return {}


def _audio_stream(ffp: dict) -> dict:
    for s in ffp.get("streams", []) or []:
        if isinstance(s, dict) and s.get("codec_type") == "audio":
            return s
    return {}


def _fmt_size(num_bytes) -> str:
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_duration(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    h = int(s // 3600); m = int((s % 3600) // 60); rem = s - h * 3600 - m * 60
    return f"{h}:{m:02d}:{rem:06.3f}" if h else f"{m}:{rem:06.3f}"


def _fmt_fnumber(v) -> str:
    try:
        f = float(v)
        return f"f/{f:.1f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_focal(v) -> str:
    try:
        return f"{float(v):.0f} mm"
    except (TypeError, ValueError):
        return str(v)


def _fmt_shutter(v) -> str:
    try:
        s = float(v)
        if s and s < 1:
            return f"1/{int(round(1/s))} s"
        return f"{s} s"
    except (TypeError, ValueError):
        return str(v)


# ─── Tab builders ────────────────────────────────────────────────────────────
def _build_summary_html(data: dict) -> str:
    ffp = data.get("ffprobe", {}) or {}
    et = data.get("exiftool", {}) or {}
    fmt = ffp.get("format", {}) or {}
    v = _video_stream(ffp)
    a = _audio_stream(ffp)

    rows: list[tuple[str, Any]] = []

    # File
    rows.append(("File", os.path.basename(data.get("path", ""))))
    rows.append(("Container", _e(fmt, "format_long_name", "format_name")))
    rows.append(("Size", _fmt_size(fmt.get("size"))))
    rows.append(("Duration", _fmt_duration(fmt.get("duration"))))
    rows.append(("Bit rate", f"{int(fmt['bit_rate'])//1000} kbps" if fmt.get("bit_rate") else None))

    # Camera
    rows.append(("", "<h3>Camera</h3>"))
    rows.append(("Make", _e(et, "EXIF:Make", "QuickTime:Make", "Make")))
    rows.append(("Model", _e(et, "EXIF:Model", "QuickTime:Model", "Model")))
    rows.append(("Software", _e(et, "EXIF:Software", "QuickTime:Software", "Software")))
    rows.append(("Lens", _e(et, "Sony:LensModel", "Composite:LensSpec", "EXIF:LensModel", "LensModel")))
    fl = _e(et, "EXIF:FocalLength", "Composite:FocalLength35efl", "FocalLength")
    if fl:
        rows.append(("Focal length", _fmt_focal(fl)))
    fn = _e(et, "EXIF:FNumber", "Composite:Aperture", "FNumber")
    if fn:
        rows.append(("Aperture", _fmt_fnumber(fn)))
    iso = _e(et, "EXIF:ISO", "Composite:ISO", "ISO")
    if iso:
        rows.append(("ISO", iso))
    sh = _e(et, "EXIF:ExposureTime", "Composite:ShutterSpeed", "ExposureTime")
    if sh:
        rows.append(("Shutter", _fmt_shutter(sh)))
    wb = _e(et, "EXIF:WhiteBalance", "WhiteBalance")
    if wb:
        rows.append(("White balance", wb))
    pp = _e(et, "Sony:PictureProfile", "Sony:CreativeStyle", "MakerNotes:PictureProfile")
    if pp:
        rows.append(("Picture profile", pp))

    # Video
    rows.append(("", "<h3>Video</h3>"))
    rows.append(("Codec", _e(v, "codec_long_name", "codec_name")))
    if v.get("width") and v.get("height"):
        rows.append(("Resolution", f"{v['width']}x{v['height']}"))
    rows.append(("Pixel format", v.get("pix_fmt")))
    if v.get("r_frame_rate"):
        rows.append(("Frame rate", v["r_frame_rate"] + " fps"))
    rows.append(("Bit rate", f"{int(v['bit_rate'])//1000} kbps" if v.get("bit_rate") else None))
    rows.append(("Profile", v.get("profile")))
    rows.append(("Color matrix", v.get("color_space")))
    rows.append(("Color primaries", v.get("color_primaries")))
    rows.append(("Color transfer", v.get("color_transfer")))
    rows.append(("Color range", v.get("color_range")))

    # Audio
    if a:
        rows.append(("", "<h3>Audio</h3>"))
        rows.append(("Codec", _e(a, "codec_long_name", "codec_name")))
        rows.append(("Sample rate", f"{a['sample_rate']} Hz" if a.get("sample_rate") else None))
        rows.append(("Channels", a.get("channels")))
        rows.append(("Channel layout", a.get("channel_layout")))
        rows.append(("Bit rate", f"{int(a['bit_rate'])//1000} kbps" if a.get("bit_rate") else None))

    # Recording
    tc = _e(et, "QuickTime:TimeCode", "Sony:TimecodeStart", "TimeCode")
    if tc:
        rows.append(("", "<h3>Timecode</h3>"))
        rows.append(("Start TC", tc))
        rows.append(("Drop frame", _e(et, "QuickTime:TimecodeDropFrame", "Sony:TimecodeDropFrame")))

    # Renders rows[] but the (key=='', value='<h3>…</h3>') entries are subtitles.
    parts = [ROW_STYLE]
    in_table = False
    for k, val in rows:
        if k == "" and isinstance(val, str) and val.startswith("<h3>"):
            if in_table:
                parts.append("</table>")
                in_table = False
            parts.append(val)
            continue
        if val in (None, "", "?"):
            continue
        if not in_table:
            parts.append("<table>")
            in_table = True
        parts.append(f'<tr><td class="k">{k}</td><td class="v">{val}</td></tr>')
    if in_table:
        parts.append("</table>")
    return "".join(parts)


def _build_camera_html(data: dict) -> str:
    et = data.get("exiftool", {}) or {}
    if et.get("_error"):
        return f'{ROW_STYLE}<p class="muted">{et["_error"]}</p>'

    # Group by exiftool group prefix.
    groups: dict[str, list[tuple[str, Any]]] = {}
    for k, v in et.items():
        if k.startswith("_"):
            continue
        if ":" in k:
            group, name = k.split(":", 1)
        else:
            group, name = "Other", k
        groups.setdefault(group, []).append((name, v))

    # Order groups so the relevant ones come first.
    order = ["EXIF", "Sony", "MakerNotes", "QuickTime", "Composite", "XMP",
             "ICC_Profile", "File", "Other"]
    parts = [ROW_STYLE]
    for group in order:
        rows = groups.pop(group, None)
        if not rows:
            continue
        rows.sort(key=lambda kv: kv[0])
        parts.append(f"<h3>{group}</h3>")
        parts.append(_html_table([(k, _truncate(v)) for k, v in rows]))
    # Anything else.
    for group, rows in sorted(groups.items()):
        rows.sort(key=lambda kv: kv[0])
        parts.append(f"<h3>{group}</h3>")
        parts.append(_html_table([(k, _truncate(v)) for k, v in rows]))
    return "".join(parts)


def _build_streams_html(data: dict) -> str:
    ffp = data.get("ffprobe", {}) or {}
    if ffp.get("_error"):
        return f'{ROW_STYLE}<p class="muted">{ffp["_error"]}</p>'

    parts = [ROW_STYLE]
    streams = ffp.get("streams", []) or []
    if not streams:
        return f'{ROW_STYLE}<p class="muted">No streams reported.</p>'
    for s in streams:
        if not isinstance(s, dict):
            continue
        idx = s.get("index", "?")
        ctype = s.get("codec_type", "?")
        codec = s.get("codec_long_name") or s.get("codec_name") or "?"
        parts.append(f"<h3>Stream #{idx} — {ctype} — {codec}</h3>")
        rows = [(k, _truncate(v)) for k, v in s.items() if k != "disposition"]
        rows.sort(key=lambda kv: kv[0])
        parts.append(_html_table(rows))
    return "".join(parts)


def _build_color_html(data: dict) -> str:
    ffp = data.get("ffprobe", {}) or {}
    et = data.get("exiftool", {}) or {}
    v = _video_stream(ffp)
    side = v.get("side_data_list", []) or []
    rows = [
        ("Pixel format", v.get("pix_fmt")),
        ("Color space (matrix)", v.get("color_space")),
        ("Color primaries", v.get("color_primaries")),
        ("Color transfer", v.get("color_transfer")),
        ("Color range", v.get("color_range")),
        ("Chroma location", v.get("chroma_location")),
        ("Field order", v.get("field_order")),
        ("Profile", v.get("profile")),
        ("Bit depth (probed)", v.get("bits_per_raw_sample")),
        ("Sony picture profile", _e(et, "Sony:PictureProfile", "MakerNotes:PictureProfile")),
        ("Sony gamma", _e(et, "Sony:Gamma", "MakerNotes:Gamma")),
        ("Sony color mode", _e(et, "Sony:ColorMode", "MakerNotes:ColorMode")),
    ]
    parts = [ROW_STYLE, "<h3>Color attributes</h3>", _html_table(rows)]
    if side:
        parts.append("<h3>Side data (HDR / mastering display)</h3>")
        for sd in side:
            if not isinstance(sd, dict):
                continue
            label = sd.get("side_data_type", "side_data")
            sub = [(k, _truncate(v)) for k, v in sd.items() if k != "side_data_type"]
            parts.append(f'<p><b style="color:#aaa">{label}</b></p>')
            parts.append(_html_table(sub))
    return "".join(parts)


def _truncate(v: Any, limit: int = 220) -> str:
    s = str(v)
    if len(s) > limit:
        return s[:limit] + "…"
    return s


# ─── All-metadata tree (searchable) ─────────────────────────────────────────
class _MetadataTreeWidget(QWidget):
    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter (name, value, group)")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Tag", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 320)
        layout.addWidget(self.tree, 1)

        self._populate(data)

    def _populate(self, data: dict) -> None:
        self.tree.clear()
        # exiftool branch
        et = data.get("exiftool") or {}
        et_root = QTreeWidgetItem(self.tree, ["exiftool", ""])
        if et.get("_error"):
            QTreeWidgetItem(et_root, ["_error", str(et["_error"])])
        else:
            # Group by prefix.
            groups: dict[str, list[tuple[str, Any]]] = {}
            for k, v in sorted(et.items()):
                if k.startswith("_"):
                    continue
                grp, _, name = k.partition(":")
                if not name:
                    grp, name = "Other", grp
                groups.setdefault(grp, []).append((name, v))
            for grp in sorted(groups):
                gnode = QTreeWidgetItem(et_root, [grp, ""])
                for name, val in groups[grp]:
                    QTreeWidgetItem(gnode, [name, _truncate(val, 600)])
        et_root.setExpanded(True)

        # ffprobe branch
        ffp = data.get("ffprobe") or {}
        ffp_root = QTreeWidgetItem(self.tree, ["ffprobe", ""])
        self._add_dict(ffp_root, ffp)
        ffp_root.setExpanded(True)

    def _add_dict(self, parent: QTreeWidgetItem, d: Any) -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                child = QTreeWidgetItem(parent, [str(k), ""])
                if isinstance(v, (dict, list)):
                    self._add_dict(child, v)
                else:
                    child.setText(1, _truncate(v, 600))
        elif isinstance(d, list):
            for i, v in enumerate(d):
                child = QTreeWidgetItem(parent, [f"[{i}]", ""])
                if isinstance(v, (dict, list)):
                    self._add_dict(child, v)
                else:
                    child.setText(1, _truncate(v, 600))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()

        def _walk(item: QTreeWidgetItem) -> bool:
            # Returns True if item or any descendant matches.
            self_match = (
                not needle
                or needle in item.text(0).lower()
                or needle in item.text(1).lower()
            )
            child_match = False
            for i in range(item.childCount()):
                c = item.child(i)
                if _walk(c):
                    child_match = True
            visible = self_match or child_match
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            _walk(self.tree.topLevelItem(i))


# ─── Dialog ──────────────────────────────────────────────────────────────────
class ClipInspectorDialog(QDialog):
    def __init__(self, parent: QWidget, path: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Clip Inspector — {path.name}")
        self.resize(880, 740)
        self.setStyleSheet(INSPECTOR_QSS)
        self._path = path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QLabel(str(path))
        header.setObjectName("path")
        header.setWordWrap(True)
        layout.addWidget(header)

        self.tabs = QTabWidget()
        loading = QTextBrowser()
        loading.setHtml(f'{ROW_STYLE}<p class="muted">Reading metadata…</p>')
        self.tabs.addTab(loading, "Loading")
        layout.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        if not md_mod.has_ffprobe():
            bottom.addWidget(QLabel("ffprobe missing"))
        if not md_mod.has_exiftool():
            bottom.addWidget(QLabel("exiftool missing"))
        bottom.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

        # Spawn the worker.
        self._thread = QThread(self)
        self._worker = _MetadataWorker(path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_worker_finished(self, data: dict) -> None:
        self.tabs.clear()

        sum_view = QTextBrowser()
        sum_view.setHtml(_build_summary_html(data))
        self.tabs.addTab(sum_view, "Summary")

        cam_view = QTextBrowser()
        cam_view.setHtml(_build_camera_html(data))
        self.tabs.addTab(cam_view, "Camera & Lens")

        st_view = QTextBrowser()
        st_view.setHtml(_build_streams_html(data))
        self.tabs.addTab(st_view, "Streams")

        cl_view = QTextBrowser()
        cl_view.setHtml(_build_color_html(data))
        self.tabs.addTab(cl_view, "Color")

        all_view = _MetadataTreeWidget(data, self)
        self.tabs.addTab(all_view, "All Metadata")

    def closeEvent(self, event):
        # Make sure the worker thread is reaped if the dialog is dismissed
        # before metadata extraction finishes.
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)
