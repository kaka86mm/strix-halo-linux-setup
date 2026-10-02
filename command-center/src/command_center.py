#!/usr/bin/env python3
"""
Strix Halo Command Center — Strix Halo Edition (v6.8.0)
Unified Dashboard and System Tray Controller.
Inspired by G-Helper and Strix-Halo-Control.
"""
import sys
import os
import signal
import shutil
import subprocess
import re
from pathlib import Path
from threading import Thread
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QFrame, QGridLayout,
    QColorDialog, QSlider, QProgressBar, QLineEdit, QSizePolicy,
    QDialog, QFormLayout
)
from PyQt6.QtGui import QIcon, QAction, QActionGroup, QColor, QFont, QPainter, QPixmap, QCursor
from PyQt6.QtCore import QTimer, Qt, QPoint, QRect, QSize, QObject, pyqtSignal

try:
    from PyQt6.QtSvg import QSvgRenderer
except ImportError:
    QSvgRenderer = None

try:
    import psutil
except ImportError:
    psutil = None

# Import modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from modules.config import ConfigManager
from modules.notifications import NotificationManager
from modules.rgb_controller import RGBController
from modules.power_controller import PowerController
from modules.llm_controller import LLMController
from modules.display_controller import DisplayController

TRAY_ICON_SIZE = 24
VERSION = "6.14.0"

DASHBOARD_WINDOW_TITLE = "Strix Halo Dashboard"
DASHBOARD_WINDOW_ROLE = "strix-halo-dashboard"
KWIN_DASHBOARD_SCRIPT_NAME = "strix_halo_dashboard_anchor"
RGB_COLOR_PRESETS = [
    ("Ice", "7FDBFF"),
    ("Mint", "2ECC71"),
    ("Lemon", "F1C40F"),
    ("Amber", "F39C12"),
    ("Coral", "FF6B6B"),
    ("Rose", "FF4D8D"),
    ("Violet", "9B59B6"),
    ("White", "FFFFFF"),
]


class _MetricsRelay(QObject):
    """Carries worker-thread metric snapshots into the Qt main thread."""

    got = pyqtSignal(dict)


def _fmt_duration(secs):
    if secs is None:
        return "--"
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


class LLMMetricsDialog(QDialog):
    """Live runtime metrics for the gufo engine, auto-refreshed every 2s."""

    def __init__(self, llm_ctrl, parent=None):
        super().__init__(parent)
        self.llm = llm_ctrl
        self.setWindowTitle("AI Engine Metrics")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint
        )
        self._fetching = False
        self.setMinimumWidth(560)

        self._relay = _MetricsRelay()
        self._relay.got.connect(self._apply)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_cards())
        root.addWidget(self._build_detail("ENGINE", [
            ("requests", "Requests in-flight"),
            ("deferred", "Deferred"),
            ("kv_ratio", "KV cache usage"),
            ("context_length", "Context window"),
        ]))
        root.addWidget(self._build_detail("TOKENS", [
            ("predict_total", "Generated"),
            ("prompt_total", "Prefilled"),
            ("predict_tps_last", "Decode speed (last)"),
            ("prompt_tps_last", "Prefill speed (last)"),
        ]))
        root.addWidget(self._build_footer())

        self.apply_styles()
        self.adjustSize()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)
        self._refresh()

    # -- construction ------------------------------------------------
    def _build_header(self):
        header = QFrame()
        header.setObjectName("llm_header")
        hbox = QHBoxLayout(header)
        hbox.setContentsMargins(16, 12, 12, 12)
        hbox.setSpacing(10)

        self._status_dot = QLabel("●")
        self._status_dot.setToolTip("engine /health status")
        self._status_dot.setObjectName("llm_dot")
        self._status_dot.setProperty("state", "loading")
        hbox.addWidget(self._status_dot)

        self._status_txt = QLabel("…")
        self._status_txt.setObjectName("llm_status_txt")
        hbox.addWidget(self._status_txt)
        hbox.addStretch()

        self._model_txt = QLabel("gufo")
        self._model_txt.setObjectName("llm_model_txt")
        hbox.addWidget(self._model_txt)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("llm_close_btn")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.accept)
        hbox.addWidget(close_btn)
        return header

    def _metric_card(self, label):
        card = QFrame()
        card.setObjectName("llm_card")
        card.setMinimumHeight(66)
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(10, 8, 10, 8)
        vbox.setSpacing(2)
        lbl = QLabel(label)
        lbl.setObjectName("llm_card_label")
        val = QLabel("--")
        val.setObjectName("llm_card_value")
        sub = QLabel(" ")
        sub.setObjectName("llm_card_sub"
        )
        vbox.addWidget(lbl)
        vbox.addWidget(val)
        vbox.addWidget(sub)
        card._value = val
        card._sub = sub
        return card

    def _build_cards(self):
        wrap = QFrame()
        wrap.setObjectName("llm_cards_wrap")
        hbox = QHBoxLayout(wrap)
        hbox.setContentsMargins(14, 12, 14, 4)
        hbox.setSpacing(8)
        self._cards = {}
        for key, label in [
            ("gpu", "GPU MEMORY"),
            ("predict_tps", "DECODE"),
            ("cpu_pct", "CPU"),
            ("uptime", "UPTIME"),
        ]:
            card = self._metric_card(label)
            self._cards[key] = card
            hbox.addWidget(card)
        return wrap

    def _build_detail(self, title, rows):
        section = QFrame()
        section.setObjectName("llm_detail")
        vbox = QVBoxLayout(section)
        vbox.setContentsMargins(14, 8, 14, 8)
        vbox.setSpacing(6)

        ttl = QLabel(title)
        ttl.setObjectName("section_title")
        vbox.addWidget(ttl)

        grid = QGridLayout()
        grid.setSpacing(4)
        self._rows = getattr(self, "_rows", {})
        for i, (key, label) in enumerate(rows):
            lbl = QLabel(label)
            lbl.setObjectName("llm_row_label")
            val = QLabel("--")
            val.setObjectName("llm_row_value")
            grid.addWidget(lbl, i // 2, (i % 2) * 2)
            grid.addWidget(val, i // 2, (i % 2) * 2 + 1)
            self._rows[key] = val
        grid.setColumnMinimumWidth(0, 130)
        grid.setColumnMinimumWidth(2, 130)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        vbox.addLayout(grid)
        return section

    def _build_footer(self):
        footer = QFrame()
        footer.setObjectName("llm_footer")
        hbox = QHBoxLayout(footer)
        hbox.setContentsMargins(14, 8, 14, 10)
        note = QLabel("auto-refresh · 2s")
        note.setObjectName("llm_note")
        hbox.addWidget(note)
        hbox.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setObjectName("llm_btn_accent")
        refresh_btn.setFixedHeight(30)
        refresh_btn.clicked.connect(self._refresh)
        hbox.addWidget(refresh_btn)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("llm_btn")
        close_btn.setFixedHeight(30)
        close_btn.clicked.connect(self.accept)
        hbox.addWidget(close_btn)
        return footer

    def apply_styles(self):
        self.setStyleSheet("""
            QDialog { background-color: #111; color: #ddd; }
            #llm_header { background-color: #1a1a1a; }
            #llm_dot { font-size: 14px; }
            #llm_dot[state="serving"] { color: #2ecc71; }
            #llm_dot[state="loading"] { color: #f1c40f; }
            #llm_dot[state="stopped"] { color: #555; }
            #llm_status_txt {
                font-size: 13px; font-weight: bold; color: #eee;
            }
            #llm_model_txt { font-size: 11px; color: #999; }
            #llm_close_btn {
                background: transparent; border: none; color: #555;
                font-size: 13px; padding: 0;
            }
            #llm_close_btn:hover { color: #ff4655; }
            #llm_cards_wrap { background-color: #111; }
            #llm_card {
                background-color: #1c1c1c;
                border: 1px solid #232323;
                border-radius: 8px;
            }
            #llm_card_label {
                font-size: 9px; color: #777;
                letter-spacing: 1px;
            }
            #llm_card_value {
                font-size: 16px; font-weight: bold; color: #eee;
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
            }
            #llm_card_sub {
                font-size: 10px; color: #666;
            }
            #llm_detail { background-color: #111; }
            .QLabel#section_title { font-size: 9px; }
            #llm_row_label { font-size: 11px; color: #999; }
            #llm_row_value {
                font-size: 11px; font-weight: bold; color: #ddd;
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
            }
            #llm_footer { background-color: #0d0d0d; }
            #llm_note { font-size: 10px; color: #444; }
            QPushButton#llm_btn {
                background-color: #1c1c1c;
                border: 1px solid #2a2a2a;
                border-radius: 5px;
                color: #aaa;
                padding: 0 14px;
            }
            QPushButton#llm_btn:hover {
                background-color: #252525;
                border-color: #444;
                color: #fff;
            }
            QPushButton#llm_btn_accent {
                background-color: #12251b;
                border: 1px solid #2ecc71;
                border-radius: 5px;
                color: #2ecc71;
                padding: 0 14px;
            }
            QPushButton#llm_btn_accent:hover {
                background-color: #1a3325;
                color: #4be28a;
            }
        """)

    # -- data --------------------------------------------------------
    def _refresh(self):
        if self._fetching or self.llm is None:
            return
        self._fetching = True
        def work():
            data = self.llm.get_metrics() or {}
            self._relay.got.emit(data)
        Thread(target=work, daemon=True).start()

    def _apply(self, data):
        self._fetching = False
        if not data:
            return
        status_map = {
            "serving": "Serving",
            "loading": "Loading",
            "stopped": "Stopped",
        }
        st = data.get("status")
        self._status_txt.setText(status_map.get(st, st or "--"))
        for w in (self._status_dot,):
            w.setProperty("state", st or "stopped")
            w.style().unpolish(w)
            w.style().polish(w)
        self._model_txt.setText(data.get("model") or "--")

        self._cards["gpu"]._value.setText(
            f"{(data.get('gtt_used_gib') or 0):.1f} GiB"
            if data.get("gtt_used_gib") is not None else "--"
        )
        self._cards["gpu"]._sub.setText(
            f"of {data['gtt_total_gib']:.0f} GiB"
            if data.get("gtt_total_gib") else " "
        )
        self._cards["predict_tps"]._value.setText(
            f"{data['predict_tps']:.1f}" if data.get("predict_tps") is not None else "--"
        )
        self._cards["predict_tps"]._sub.setText("tok/s · last request")
        self._cards["cpu_pct"]._value.setText(data.get("cpu_pct") or "--")
        self._cards["cpu_pct"]._sub.setText("docker container")
        self._cards["uptime"]._value.setText(_fmt_duration(data.get("uptime_secs")))
        self._cards["uptime"]._sub.setText(
            "restarts: %s" % data.get("restarts", "--")
        )
        self._cards["uptime"]._sub.setToolTip("container restart count")

        def setv(key, text):
            self._rows[key].setText(str(text))
        setv("requests", "--" if data.get("processing") is None else data["processing"])
        setv("deferred", "--" if data.get("deferred") is None else data["deferred"])
        setv(
            "kv_ratio",
            "--" if data.get("kv_ratio") is None else f"{data['kv_ratio'] * 100:.1f}%",
        )
        setv(
            "context_length",
            "--" if data.get("context_length") is None else f"{data['context_length']:,}",
        )
        setv(
            "predict_total",
            "--" if data.get("predict_total") is None else f"{data['predict_total']:,}",
        )
        setv(
            "prompt_total",
            "--" if data.get("prompt_total") is None else f"{data['prompt_total']:,}",
        )
        setv(
            "predict_tps_last",
            "--" if data.get("predict_tps") is None else f"{data['predict_tps']:.1f} tok/s",
        )
        setv(
            "prompt_tps_last",
            "--" if data.get("prompt_tps") is None else f"{data['prompt_tps']:.0f} tok/s",
        )


class DashboardWindow(QWidget):
    """G-Helper-style compact popup panel."""

    # All 8 profiles: (display label, z13ctl code, accent color)
    PROFILES = [
        ("Emergency\n10W",  "emergency", "#555"),
        ("Battery\n18W",    "battery",   "#4a9"),
        ("Efficient\n30W",  "efficient", "#4ae"),
        ("Silent",          "quiet",     "#59c"),
        ("Balanced\n40W",   "balanced",  "#88c"),
        ("Turbo\n55W",      "performance","#c84"),
        ("Gaming\n70W",     "gaming",    "#e63"),
        ("Maximum\n90W",    "maximum",   "#e33"),
    ]

    def __init__(self, power_ctrl, rgb_controller, config, notifier, llm_ctrl=None):
        super().__init__()
        self.power = power_ctrl
        self.rgb = rgb_controller
        self.config = config
        self.notifier = notifier
        self.llm = llm_ctrl
        self._profile_btns = {}
        self._rgb_buttons = []
        self._fan_curve_placeholder = "48:2,53:22,57:30,60:43,63:56,65:68,70:89,76:102"

        self.setWindowTitle(DASHBOARD_WINDOW_TITLE)
        self.setWindowRole(DASHBOARD_WINDOW_ROLE)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.setup_ui()
        self.apply_styles()
        self.apply_backend_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_stats_bar())
        root.addWidget(self._build_divider())
        root.addWidget(self._build_profiles_section())
        root.addWidget(self._build_divider())
        root.addWidget(self._build_battery_section())
        root.addWidget(self._build_divider())
        root.addWidget(self._build_rgb_section())
        root.addWidget(self._build_divider())
        root.addWidget(self._build_fan_section())
        if self.llm is not None:
            root.addWidget(self._build_divider())
            root.addWidget(self._build_llm_section())
        root.addWidget(self._build_footer())

    def _build_header(self):
        header = QFrame()
        header.setObjectName("header")
        hbox = QHBoxLayout(header)
        hbox.setContentsMargins(14, 10, 14, 10)

        title = QLabel(self.config.get_device_label())
        title.setObjectName("title_label")
        self._title_label = title
        hbox.addWidget(title)

        hbox.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setObjectName("close_btn")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(self.hide)
        hbox.addWidget(close_btn)
        return header

    def _build_stats_bar(self):
        bar = QFrame()
        bar.setObjectName("stats_bar")
        hbox = QHBoxLayout(bar)
        hbox.setContentsMargins(14, 8, 14, 8)
        hbox.setSpacing(16)

        self.stat_temp  = self._stat_widget("APU", "--°C")
        self.stat_fans  = self._stat_widget("FANS", "-- RPM")
        self.stat_pwr   = self._stat_widget("MODE", "Balanced")
        self.stat_bat   = self._stat_widget("BATTERY", "--%")
        self.stat_cpu   = self._stat_widget("CPU", "0%")

        for w in (self.stat_temp, self.stat_fans, self.stat_pwr, self.stat_bat, self.stat_cpu):
            hbox.addWidget(w)
        return bar

    def _stat_widget(self, label, value):
        frame = QFrame()
        frame.setObjectName("stat_card")
        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(8, 6, 8, 6)
        vbox.setSpacing(1)
        lbl = QLabel(label)
        lbl.setObjectName("stat_label")
        val = QLabel(value)
        val.setObjectName("stat_value")
        vbox.addWidget(lbl)
        vbox.addWidget(val)
        # store the value label as attribute on the frame for easy update
        frame._value_lbl = val
        return frame

    def _build_profiles_section(self):
        section = QFrame()
        section.setObjectName("section")
        vbox = QVBoxLayout(section)
        vbox.setContentsMargins(14, 10, 14, 10)
        vbox.setSpacing(6)

        vbox.addWidget(self._section_title("PERFORMANCE"))

        grid = QGridLayout()
        grid.setSpacing(5)
        for i, (label, code, color) in enumerate(self.PROFILES):
            btn = QPushButton(label)
            btn.setObjectName("profile_btn")
            btn.setProperty("profile_color", color)
            btn.setCheckable(True)
            btn.setFixedHeight(52)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _, c=code: self._set_profile(c))
            self._profile_btns[code] = btn
            grid.addWidget(btn, i // 4, i % 4)
        vbox.addLayout(grid)
        return section

    def _build_battery_section(self):
        section = QFrame()
        section.setObjectName("section")
        vbox = QVBoxLayout(section)
        vbox.setContentsMargins(14, 10, 14, 10)
        vbox.setSpacing(6)
        vbox.addWidget(self._section_title("BATTERY LIMIT"))

        hbox = QHBoxLayout()
        hbox.setSpacing(6)
        self._bat_btns = {}
        for lim in [60, 80, 100]:
            btn = QPushButton(f"{lim}%")
            btn.setObjectName("bat_btn")
            btn.setCheckable(True)
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda _, l=lim: self._set_charge_limit(l))
            self._bat_btns[lim] = btn
            hbox.addWidget(btn)
        vbox.addLayout(hbox)
        return section

    def _build_rgb_section(self):
        section = QFrame()
        section.setObjectName("section")
        vbox = QVBoxLayout(section)
        vbox.setContentsMargins(14, 10, 14, 10)
        vbox.setSpacing(6)
        vbox.addWidget(self._section_title("RGB LIGHTING"))

        vbox.addWidget(
            self._build_color_row(
                "Keyboard", self.rgb.set_keyboard_color, self.rgb.turn_off_keyboard
            )
        )
        vbox.addWidget(
            self._build_color_row(
                "Backlight", self.rgb.set_lightbar_color, self.rgb.turn_off_lightbar
            )
        )

        hbox = QHBoxLayout()
        hbox.setSpacing(6)
        hbox.addWidget(self._rgb_row_label("Keyboard"))
        for label, val in [("Off", 0), ("Low", 1), ("Med", 2), ("High", 3)]:
            btn = QPushButton(label)
            btn.setObjectName("rgb_btn")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _, v=val: self.rgb.set_keyboard_brightness(v))
            self._rgb_buttons.append(btn)
            hbox.addWidget(btn)
        hbox.addStretch()
        vbox.addLayout(hbox)

        hbox = QHBoxLayout()
        hbox.setSpacing(6)
        hbox.addWidget(self._rgb_row_label("Keyboard FX"))
        for label, fx in [("Rainbow", "rainbow"), ("Breathing", "breathing"), ("Off", None)]:
            btn = QPushButton(label)
            btn.setObjectName("rgb_btn")
            btn.setFixedHeight(28)
            if fx:
                btn.clicked.connect(lambda _, e=fx: self.rgb.set_keyboard_animation(e))
            else:
                btn.clicked.connect(self.rgb.turn_off_keyboard)
            self._rgb_buttons.append(btn)
            hbox.addWidget(btn)
        hbox.addStretch()
        vbox.addLayout(hbox)
        return section

    def _build_color_row(self, zone_label, apply_color, turn_off):
        row = QFrame()
        row.setObjectName("rgb_zone_row")
        hbox = QHBoxLayout(row)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(6)
        hbox.addWidget(self._rgb_row_label(zone_label))

        for color_name, hex_color in RGB_COLOR_PRESETS:
            hbox.addWidget(
                self._build_color_swatch(zone_label, color_name, hex_color, apply_color)
            )

        custom_btn = QPushButton("Custom")
        custom_btn.setObjectName("rgb_minor_btn")
        custom_btn.setFixedHeight(24)
        custom_btn.clicked.connect(
            lambda _, zone=zone_label, callback=apply_color: self._pick_custom_color(zone, callback)
        )
        self._rgb_buttons.append(custom_btn)
        hbox.addWidget(custom_btn)

        off_btn = QPushButton("Off")
        off_btn.setObjectName("rgb_minor_btn")
        off_btn.setFixedHeight(24)
        off_btn.clicked.connect(turn_off)
        self._rgb_buttons.append(off_btn)
        hbox.addWidget(off_btn)
        hbox.addStretch()
        return row

    def _build_color_swatch(self, zone_label, color_name, hex_color, apply_color):
        btn = QPushButton()
        btn.setObjectName("rgb_swatch_btn")
        btn.setToolTip(f"{zone_label}: {color_name}")
        btn.setFixedSize(22, 22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _, value=hex_color: apply_color(value))
        btn.setStyleSheet(
            f"QPushButton {{"
            f"background-color: #{hex_color};"
            "border: 1px solid #2a2a2a;"
            "border-radius: 11px;"
            "padding: 0;"
            "}"
            "QPushButton:hover { border: 2px solid #f5f5f5; }"
            "QPushButton:pressed { border: 2px solid #ff4655; }"
        )
        self._rgb_buttons.append(btn)
        return btn

    def _pick_custom_color(self, zone_label, apply_color):
        color = QColorDialog.getColor(QColor("#FFFFFF"), self, f"{zone_label} Color")
        if color.isValid():
            apply_color(color.name().lstrip("#").upper())

    def _rgb_row_label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("rgb_zone_label")
        lbl.setFixedWidth(68)
        return lbl

    def _build_fan_section(self):
        section = QFrame()
        section.setObjectName("section")
        vbox = QVBoxLayout(section)
        vbox.setContentsMargins(14, 10, 14, 10)
        vbox.setSpacing(6)
        vbox.addWidget(self._section_title("CUSTOM FAN CURVE"))

        hbox = QHBoxLayout()
        hbox.setSpacing(6)
        self.curve_input = QLineEdit()
        self.curve_input.setPlaceholderText(self._fan_curve_placeholder)
        self.curve_input.setObjectName("curve_input")
        hbox.addWidget(self.curve_input)
        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("apply_btn")
        apply_btn.setFixedHeight(30)
        apply_btn.clicked.connect(self._apply_fan_curve)
        self._fan_apply_btn = apply_btn
        hbox.addWidget(apply_btn)
        vbox.addLayout(hbox)
        return section

    def _build_llm_section(self):
        section = QFrame()
        section.setObjectName("section")
        vbox = QVBoxLayout(section)
        vbox.setContentsMargins(14, 10, 14, 10)
        vbox.setSpacing(6)
        vbox.addWidget(self._section_title("AI ENGINE (GUFO)"))

        hbox = QHBoxLayout()
        hbox.setSpacing(6)

        from modules.llm_controller import LLM_MODEL_ID

        self.llm_status_lbl = QLabel(f"{LLM_MODEL_ID} · {self.llm.get_status_text()}")
        self.llm_status_lbl.setObjectName("llm_status")
        self.llm_status_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.llm_status_lbl.setToolTip("Click for live runtime metrics")
        self.llm_status_lbl.mousePressEvent = lambda _e: self._llm_show_metrics()
        hbox.addWidget(self.llm_status_lbl)
        hbox.addStretch()

        metrics_btn = QPushButton("📊 Metrics")
        metrics_btn.setObjectName("llm_btn")
        metrics_btn.setFixedHeight(28)
        metrics_btn.clicked.connect(lambda: self._llm_show_metrics())
        hbox.addWidget(metrics_btn)

        start_btn = QPushButton("Start")
        start_btn.setObjectName("llm_btn")
        start_btn.setFixedHeight(28)
        start_btn.clicked.connect(lambda: self._llm_action("start"))

        stop_btn = QPushButton("Stop")
        stop_btn.setObjectName("llm_btn")
        stop_btn.setFixedHeight(28)
        stop_btn.clicked.connect(lambda: self._llm_action("stop"))

        restart_btn = QPushButton("Restart")
        restart_btn.setObjectName("llm_btn")
        restart_btn.setFixedHeight(28)
        restart_btn.clicked.connect(lambda: self._llm_action("restart"))

        self._llm_btns = (metrics_btn, start_btn, stop_btn, restart_btn)
        for btn in self._llm_btns:
            hbox.addWidget(btn)
        vbox.addLayout(hbox)
        return section

    def _llm_show_metrics(self):
        if self.llm is None or not self.llm.available:
            self.notifier.notify(
                "AI Engine",
                "gufo container not found on this device.",
                "warning",
                4000,
            )
            return
        dlg = LLMMetricsDialog(self.llm, self)
        dlg.exec()

    def _llm_action(self, action):
        if self.llm is None:
            return
        getattr(self.llm, action)()
        QTimer.singleShot(400, self._refresh_llm_state)

    def _refresh_llm_state(self):
        if self.llm is None or not hasattr(self, "llm_status_lbl"):
            return
        from modules.llm_controller import LLM_MODEL_ID
        running = self.llm.is_running() if self.llm.available else False
        ok, _ = self.llm.get_health() if running else (False, "")
        self.llm_status_lbl.setText(f"{LLM_MODEL_ID} · {self.llm.get_status_text()}")
        self.llm_status_lbl.setProperty("state", "serving" if ok else ("loading" if running else "stopped"))
        # re-apply stylesheet so property-based selectors refresh
        self.llm_status_lbl.style().unpolish(self.llm_status_lbl)
        self.llm_status_lbl.style().polish(self.llm_status_lbl)

    def _build_footer(self):
        footer = QFrame()
        footer.setObjectName("footer")
        hbox = QHBoxLayout(footer)
        hbox.setContentsMargins(14, 6, 14, 8)

        auto_btn = QPushButton("⚡ Auto Switch")
        auto_btn.setObjectName("footer_btn")
        auto_btn.setCheckable(True)
        auto_btn.setChecked(self.power.is_auto_enabled())
        auto_btn.toggled.connect(lambda checked: self.power.set_auto(checked))
        self._auto_btn = auto_btn
        hbox.addWidget(auto_btn)

        hbox.addStretch()

        ver_lbl = QLabel(f"v{VERSION}")
        ver_lbl.setObjectName("ver_label")
        hbox.addWidget(ver_lbl)
        return footer

    def _build_divider(self):
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    def refresh_device_label(self):
        self._title_label.setText(self.config.get_device_label())

    def apply_backend_state(self):
        power_available = self.power.available
        rgb_available = self.rgb.is_available()

        for btn in self._profile_btns.values():
            btn.setEnabled(power_available)

        for btn in self._bat_btns.values():
            btn.setEnabled(power_available)

        self.curve_input.setEnabled(power_available)
        self._fan_apply_btn.setEnabled(power_available)
        self._auto_btn.setEnabled(power_available)
        self.curve_input.setPlaceholderText(
            self._fan_curve_placeholder if power_available else "Hardware control backend unavailable on this device"
        )

        for btn in self._rgb_buttons:
            btn.setEnabled(rgb_available)

        if self.llm is not None and hasattr(self, "_llm_btns"):
            for btn in self._llm_btns:
                btn.setEnabled(self.llm.available)
            self._refresh_llm_state()

    def _section_title(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section_title")
        return lbl

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------
    def apply_styles(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #111;
                color: #ddd;
                font-family: "Segoe UI", "Noto Sans", sans-serif;
                font-size: 12px;
            }
            #header {
                background-color: #1a1a1a;
            }
            #title_label {
                font-size: 13px;
                font-weight: bold;
                color: #ff4655;
                text-transform: uppercase;
            }
            #close_btn {
                background: transparent;
                border: none;
                color: #555;
                font-size: 13px;
                padding: 0;
            }
            #close_btn:hover { color: #ff4655; }

            #stats_bar {
                background-color: #151515;
            }
            #stat_card {
                background-color: #1c1c1c;
                border-radius: 6px;
                min-width: 70px;
            }
            #stat_label {
                font-size: 9px;
                color: #555;
                text-transform: uppercase;
            }
            #stat_value {
                font-size: 13px;
                font-weight: bold;
                color: #eee;
            }

            #divider { background-color: #222; }

            #section { background-color: #111; }

            #section_title {
                font-size: 9px;
                font-weight: bold;
                color: #444;
                text-transform: uppercase;
                letter-spacing: 1px;
            }

            QPushButton#profile_btn {
                background-color: #1c1c1c;
                border: 1px solid #2a2a2a;
                border-radius: 6px;
                color: #bbb;
                font-size: 11px;
                padding: 4px;
            }
            QPushButton#profile_btn:hover {
                background-color: #252525;
                border-color: #444;
                color: #fff;
            }
            QPushButton#profile_btn:checked {
                background-color: #1e1e1e;
                border-color: #ff4655;
                color: #ff4655;
                font-weight: bold;
            }

            QPushButton#bat_btn, QPushButton#rgb_btn {
                background-color: #1c1c1c;
                border: 1px solid #2a2a2a;
                border-radius: 5px;
                color: #aaa;
            }
            QPushButton#bat_btn:hover, QPushButton#rgb_btn:hover {
                background-color: #252525;
                color: #fff;
            }
            QPushButton#bat_btn:checked {
                border-color: #ff4655;
                color: #ff4655;
            }

            QLineEdit#curve_input {
                background-color: #1c1c1c;
                border: 1px solid #2a2a2a;
                border-radius: 4px;
                color: #aaa;
                padding: 4px 8px;
                font-size: 11px;
            }
            QPushButton#apply_btn {
                background-color: #1c1c1c;
                border: 1px solid #333;
                border-radius: 4px;
                color: #aaa;
                padding: 0 10px;
            }
            QPushButton#apply_btn:hover {
                background-color: #ff4655;
                border-color: #ff4655;
                color: #fff;
            }

            QLabel#llm_status {
                font-size: 11px;
                color: #888;
            }
            QLabel#llm_status[state="serving"] { color: #2e8b57; }
            QLabel#llm_status[state="loading"] { color: #d9a441; }
            QLabel#llm_status[state="stopped"] { color: #666; }
            QPushButton#llm_btn {
                background-color: #1c1c1c;
                border: 1px solid #2a2a2a;
                border-radius: 5px;
                color: #aaa;
                padding: 0 12px;
            }
            QPushButton#llm_btn:hover {
                background-color: #252525;
                border-color: #444;
                color: #fff;
            }

            #footer { background-color: #0d0d0d; }
            QPushButton#footer_btn {
                background-color: transparent;
                border: 1px solid #2a2a2a;
                border-radius: 4px;
                color: #555;
                padding: 2px 10px;
                font-size: 11px;
            }
            QPushButton#footer_btn:hover { color: #aaa; border-color: #444; }
            QPushButton#footer_btn:checked { color: #ff4655; border-color: #ff4655; }
            #ver_label { font-size: 10px; color: #333; }
            #rgb_zone_label {
                font-size: 10px;
                font-weight: bold;
                color: #666;
                text-transform: uppercase;
            }
            QPushButton#rgb_minor_btn {
                background-color: #171717;
                border: 1px solid #2a2a2a;
                border-radius: 4px;
                color: #aaa;
                padding: 0 8px;
            }
            QPushButton#rgb_minor_btn:hover {
                background-color: #252525;
                color: #fff;
            }
        """)
        self.adjustSize()

    # ------------------------------------------------------------------
    # Popup positioning - anchored to the bottom-right corner
    # ------------------------------------------------------------------
    def popup_near_tray(self, tray_icon):
        """Position the window in the bottom-right corner of the screen."""
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        screen_geom = screen.availableGeometry()
        self.adjustSize()
        x = screen_geom.right() - self.width() - 8
        y = screen_geom.bottom() - self.height() - 8
        self.move(x, y)
        if self.windowHandle() is not None:
            self.windowHandle().setPosition(x, y)

    # ------------------------------------------------------------------
    # Focus loss -> close (like a popup)
    # ------------------------------------------------------------------
    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        QTimer.singleShot(150, self._check_hide)

    def _check_hide(self):
        if not self.isActiveWindow():
            self.hide()

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _set_profile(self, code):
        self.power.set_profile(code)
        self._update_profile_buttons()

    def _update_profile_buttons(self):
        active = self.power.current_profile
        for code, btn in self._profile_btns.items():
            btn.setChecked(code == active)

    def _set_charge_limit(self, lim):
        self.power.set_charge_limit(lim)
        for l, btn in self._bat_btns.items():
            btn.setChecked(l == lim)

    def _apply_fan_curve(self):
        curve = self.curve_input.text().strip()
        if curve:
            self.power.set_fan_curve(curve)

    # ------------------------------------------------------------------
    # Live stat updates (called by poll_status timer)
    # ------------------------------------------------------------------
    def update_ui_states(self):
        try:
            status = self.power.get_status()
            temp, fans = "--°C", "-- RPM"
            for line in status.splitlines():
                if "APU:" in line:
                    temp = line.split(":", 1)[1].strip()
                if "Fans:" in line:
                    fans = re.sub(r",\s*mode:.*", "", line.split(":", 1)[1].strip())

            self.stat_temp._value_lbl.setText(temp)
            self.stat_fans._value_lbl.setText(fans)
            if self.power.available:
                self.stat_pwr._value_lbl.setText(self.power.current_profile.title())
            else:
                self.stat_pwr._value_lbl.setText("Monitor")

            bat_info = self.power.get_battery_info()
            pct = bat_info.get("percent")
            if pct is not None:
                self.stat_bat._value_lbl.setText(f"{int(pct)}%")

            if psutil:
                self.stat_cpu._value_lbl.setText(f"{int(psutil.cpu_percent())}%")
        except Exception:
            pass

        self._update_profile_buttons()
        self._auto_btn.setChecked(self.power.is_auto_enabled())
        self.apply_backend_state()

class CommandCenterApp(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.config = ConfigManager()
        self.notifier = NotificationManager(self)
        self.rgb = RGBController(self.notifier)
        self.display = DisplayController(self.notifier)
        self.power = PowerController(self.notifier, display_ctrl=self.display, rgb_ctrl=self.rgb)
        self.llm = LLMController(self.notifier)

        self.dashboard = DashboardWindow(self.power, self.rgb, self.config, self.notifier, llm_ctrl=self.llm)
        self._kwin_script_loaded = False
        self._setup_kwin_dashboard_positioner()
        
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.setup_menu)
        self.setup_menu()
        # Keep the native context menu attached so Plasma exposes right-click
        # actions consistently through the status notifier integration.
        self.setContextMenu(self.menu)

        self.activated.connect(self._on_activated)
        self.setToolTip(self.config.get_app_name())
        self.update_icon()
        self.show()
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll_status)
        self.timer.start(3000)
        
        self.notifier.notify("Dashboard Ready", self.config.get_device_label(), "success", 2000)

    def _build_color_icon(self, hex_color):
        pixmap = QPixmap(14, 14)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(f"#{hex_color}"))
        painter.setPen(QColor("#2a2a2a"))
        painter.drawEllipse(1, 1, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def _open_custom_color_dialog(self, zone_label, apply_color):
        color = QColorDialog.getColor(QColor("#FFFFFF"), self.dashboard, f"{zone_label} Color")
        if color.isValid():
            apply_color(color.name().lstrip("#").upper())

    def _populate_static_color_menu(self, menu, zone_label, apply_color, turn_off):
        for color_name, hex_color in RGB_COLOR_PRESETS:
            action = QAction(color_name, self)
            action.setIcon(self._build_color_icon(hex_color))
            action.triggered.connect(lambda _, value=hex_color: apply_color(value))
            menu.addAction(action)

        menu.addSeparator()
        menu.addAction("Custom...").triggered.connect(
            lambda _=False, zone=zone_label, callback=apply_color: self._open_custom_color_dialog(zone, callback)
        )
        menu.addAction("Off").triggered.connect(turn_off)

    def setup_menu(self):
        self.menu.clear()
        power_available = self.power.available
        rgb_available = self.rgb.is_available()
        self.menu.addAction("🖥️ Open Dashboard").triggered.connect(
            lambda _=False: QTimer.singleShot(0, self._show_dashboard)
        )
        self.menu.addSeparator()

        # --- Power Profiles ---
        profiles_menu = self.menu.addMenu("⚡ Profiles")
        profile_group = QActionGroup(profiles_menu)
        for n, c in [
            ("Emergency (10W)", "emergency"),
            ("Battery (18W)", "battery"),
            ("Efficient (30W)", "efficient"),
            ("Silent (Quiet)", "quiet"),
            ("Balanced (40W)", "balanced"),
            ("Turbo (55W)", "performance"),
            ("Gaming (70W)", "gaming"),
            ("Maximum (90W)", "maximum")
        ]:
            a = QAction(n, self)
            a.setCheckable(True)
            a.setChecked(self.power.current_profile == c)
            a.triggered.connect(lambda _, code=c: self.power.set_profile(code))
            profiles_menu.addAction(a)
            profile_group.addAction(a)
        profiles_menu.setEnabled(power_available)

        # --- Battery Limit ---
        limit_menu = self.menu.addMenu("🔋 Battery Limit")
        for lim in [60, 80, 100]:
            a = QAction(f"Limit to {lim}%", self)
            a.triggered.connect(lambda _, l=lim: self.power.set_charge_limit(l))
            limit_menu.addAction(a)
        limit_menu.setEnabled(power_available)

        self.menu.addSeparator()

        # --- RGB Lighting ---
        rgb_menu = self.menu.addMenu("🌈 RGB Lighting")
        static_menu = rgb_menu.addMenu("🎨 Static Colors")
        self._populate_static_color_menu(
            static_menu.addMenu("⌨️ Keyboard"),
            "Keyboard",
            self.rgb.set_keyboard_color,
            self.rgb.turn_off_keyboard,
        )
        self._populate_static_color_menu(
            static_menu.addMenu("💡 Backlight"),
            "Backlight",
            self.rgb.set_lightbar_color,
            self.rgb.turn_off_lightbar,
        )
        
        # Brightness Submenu
        bright_menu = rgb_menu.addMenu("⌨️ Keyboard Brightness")
        for label, val in [("Off", 0), ("Low", 1), ("Medium", 2), ("High", 3)]:
            a = QAction(label, self)
            a.triggered.connect(lambda _, v=val: self.rgb.set_keyboard_brightness(v))
            bright_menu.addAction(a)

        lightbar_menu = rgb_menu.addMenu("💡 Backlight Brightness")
        for label, val in [("Off", 0), ("Low", 1), ("Medium", 2), ("High", 3)]:
            a = QAction(label, self)
            a.triggered.connect(lambda _, v=val: self.rgb.set_window_backlight(v))
            lightbar_menu.addAction(a)
            
        # Effects Submenu
        effects_menu = rgb_menu.addMenu("✨ Keyboard Effects")
        for label, effect in [("Rainbow", "rainbow"), ("Color Cycle", "colorcycle"), ("Breathing", "breathing")]:
            a = QAction(label, self)
            a.triggered.connect(lambda _, e=effect: self.rgb.set_keyboard_animation(e))
            effects_menu.addAction(a)

        lightbar_fx_menu = rgb_menu.addMenu("✨ Backlight Effects")
        for label, effect in [("Rainbow", "rainbow"), ("Breathing", "breathing")]:
            a = QAction(label, self)
            a.triggered.connect(lambda _, e=effect: self.rgb.start_window_animation(e))
            lightbar_fx_menu.addAction(a)
        lightbar_fx_menu.addAction("Off").triggered.connect(self.rgb.turn_off_lightbar)
            
        rgb_menu.addAction("❌ Turn Off All").triggered.connect(self.rgb.turn_off)
        rgb_menu.setEnabled(rgb_available)

        self.menu.addSeparator()

        # --- AI Engine (gufo) ---
        llm_menu = self.menu.addMenu("🧠 AI Engine")
        llm_status = QAction(self.llm.get_status_text(), self)
        llm_status.setEnabled(False)
        llm_menu.addAction(llm_status)
        llm_menu.addSeparator()
        llm_menu.addAction("📊 Metrics").triggered.connect(
            lambda _=False: self._show_llm_metrics()
        )
        llm_menu.addAction("▶️ Start").triggered.connect(
            lambda _=False: self.llm.start()
        )
        llm_menu.addAction("⏹️ Stop").triggered.connect(
            lambda _=False: self.llm.stop()
        )
        llm_menu.addAction("🔄 Restart").triggered.connect(
            lambda _=False: self.llm.restart()
        )
        llm_menu.setEnabled(self.llm.available)

        self.menu.addSeparator()

        # --- Auto Settings ---
        auto_action = QAction("🔄 Auto Settings Adjust", self)
        auto_action.setCheckable(True)
        auto_action.setChecked(self.power.is_auto_enabled())
        auto_action.triggered.connect(lambda checked: self.power.set_auto(checked))
        auto_action.setEnabled(power_available)
        self.menu.addAction(auto_action)

        self.menu.addSeparator()
        self.menu.addAction("❌ Quit").triggered.connect(self.app.quit)

    def _run_kwin_script_command(self, method, *args):
        try:
            return subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Scripting", method, *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _setup_kwin_dashboard_positioner(self):
        if os.environ.get("XDG_SESSION_TYPE") != "wayland":
            return
        if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", ""):
            return
        if not shutil.which("qdbus6"):
            return

        script_path = Path(__file__).resolve().with_name("kwin_dashboard_positioner.js")
        if not script_path.exists():
            return

        self._run_kwin_script_command(
            "org.kde.kwin.Scripting.unloadScript",
            KWIN_DASHBOARD_SCRIPT_NAME,
        )
        load_result = self._run_kwin_script_command(
            "org.kde.kwin.Scripting.loadScript",
            str(script_path),
            KWIN_DASHBOARD_SCRIPT_NAME,
        )
        if load_result is None or load_result.returncode != 0:
            return

        start_result = self._run_kwin_script_command("org.kde.kwin.Scripting.start")
        self._kwin_script_loaded = start_result is not None and start_result.returncode == 0

    def _show_dashboard(self):
        """Show the dashboard and let KWin/Qt place it."""
        self.dashboard.update_ui_states()
        self.dashboard.show()
        QTimer.singleShot(0, self._finalize_dashboard_show)

    def _show_llm_metrics(self):
        if not self.llm.available:
            self.notifier.notify(
                "AI Engine",
                "gufo container not found on this device.",
                "warning",
                4000,
            )
            return
        dlg = LLMMetricsDialog(self.llm)
        dlg.exec()

    def _finalize_dashboard_show(self):
        self.dashboard.popup_near_tray(self)
        self.dashboard.raise_()
        self.dashboard.activateWindow()

    def reload_config(self):
        self.config.load_config()
        self.setToolTip(self.config.get_app_name())
        self.dashboard.refresh_device_label()
        self.dashboard.update_ui_states()

    def _on_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Unknown,
        ):
            if self.dashboard.isVisible():
                self.dashboard.hide()
            else:
                QTimer.singleShot(50, self._show_dashboard)

    def update_icon(self):
        assets = Path(__file__).resolve().parent.parent / "assets"
        if not self.power.available:
            icon_name = "battery" if self.power.get_battery_info().get("plugged") is False else "ac"
        else:
            icon_name = "battery" if self.power.is_auto_enabled() and not self.power.get_battery_info().get("plugged") else "ac"
        if self.power.available and not self.power.is_auto_enabled():
            icon_name = {"quiet": "profile-b", "balanced": "profile-b", "performance": "profile-p", "gaming": "profile-g"}.get(self.power.current_profile, "profile-b")

        icon_path = assets / f"{icon_name}.svg"
        if QSvgRenderer is not None and icon_path.exists():
            renderer = QSvgRenderer(str(icon_path))
            if renderer.isValid():
                pixmap = QPixmap(TRAY_ICON_SIZE, TRAY_ICON_SIZE)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                renderer.render(painter)
                painter.end()
                self.setIcon(QIcon(pixmap))
                return

        # Fallback: paint a simple letter-based icon so the tray is never blank
        label = {"battery": "B", "ac": "A", "profile-b": "B", "profile-p": "P",
                 "profile-g": "G", "profile-e": "E", "profile-f": "F", "profile-m": "M"}.get(icon_name, "R")
        color = {"profile-p": "#e44", "profile-g": "#e84", "battery": "#4ae", "ac": "#8e4"}.get(icon_name, "#aaa")
        pixmap = QPixmap(TRAY_ICON_SIZE, TRAY_ICON_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, TRAY_ICON_SIZE - 2, TRAY_ICON_SIZE - 2)
        painter.setPen(QColor("#fff"))
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(TRAY_ICON_SIZE - 8)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()
        self.setIcon(QIcon(pixmap))

    def poll_status(self):
        try:
            self.power.refresh_availability()
            self.rgb.refresh_availability()
            self.llm.refresh_availability()
            self.power.check_auto_switch()
            self.update_icon()
            if self.dashboard.isVisible(): self.dashboard.update_ui_states()
        except Exception: pass

def main():
    app = QApplication(sys.argv)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app.setQuitOnLastWindowClosed(False)
    
    if not QSystemTrayIcon.isSystemTrayAvailable():
        for _ in range(10):
            import time
            time.sleep(1)
            if QSystemTrayIcon.isSystemTrayAvailable(): break
            
    tray = CommandCenterApp(app)
    app.setApplicationName(tray.config.get_app_name())
    signal.signal(signal.SIGUSR1, lambda *_: tray.reload_config())
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
