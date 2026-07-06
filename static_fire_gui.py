"""
DJS Impulse — Static Fire Ground Station GUI
=============================================
PyQt6 + pyqtgraph GUI replacing ground_station.py CLI.

Usage:
    pip install PyQt6 pyqtgraph numpy
    python static_fire_gui.py
"""

import sys
import os
import csv
import math
import socket
import threading
import datetime

from PyQt6.QtCore import (
    Qt, QObject, QThread, pyqtSignal, pyqtSlot, QTimer, QRectF, QPointF
)
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QFontMetrics,
    QPainterPath, QRadialGradient, QConicalGradient, QLinearGradient,
    QTextCursor, QTextCharFormat
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QTextEdit, QPushButton, QLabel, QLineEdit,
    QMessageBox, QFrame, QSizePolicy, QStatusBar, QSpacerItem
)

import pyqtgraph as pg
import numpy as np

# =============================================================================
#  CONSTANTS
# =============================================================================
PICO_IP_DEFAULT = "192.168.1.60"
PICO_PORT_DEFAULT = 8080
CSV_FILE = "Ethernet_data.csv"
TIMEOUT = 10

# Colours
BG_DARK       = "#0f0f0f"
PANEL_BG      = "#1a1a1a"
PANEL_BORDER  = "#2a2a2a"
ACCENT_PURPLE = "#7c3aed"
ACCENT_PURPLE_DIM = "#5b21b6"
TEXT_PRIMARY   = "#e2e8f0"
TEXT_SECONDARY = "#94a3b8"
TEXT_DIM       = "#64748b"
GREEN_OK       = "#22c55e"
RED_ERR        = "#ef4444"
YELLOW_WARN    = "#f59e0b"
BRIGHT_GREEN   = "#4ade80"
FIRE_RED       = "#dc2626"
FIRE_RED_HOVER = "#b91c1c"

# State machine order
STATE_ORDER = [
    "DISCONNECTED", "IDLE", "INIT", "TARE", "ARMED",
    "LOG_READY", "IGNITE", "LOGGING", "COMPLETE"
]


# =============================================================================
#  TCP RECEIVER WORKER
# =============================================================================
class TcpWorker(QObject):
    """Runs in a background thread, reads from TCP socket line by line."""
    line_received = pyqtSignal(str)
    data_received = pyqtSignal(int, float, float)
    state_changed = pyqtSignal(str)
    countdown_tick = pyqtSignal(str)
    connection_lost = pyqtSignal()
    complete_received = pyqtSignal()
    error_received = pyqtSignal(str)

    def __init__(self, sock):
        super().__init__()
        self._sock = sock
        self._running = True

    def stop(self):
        self._running = False

    @pyqtSlot()
    def run(self):
        buf = ""
        while self._running:
            try:
                chunk = self._sock.recv(4096).decode(errors="replace")
                if not chunk:
                    self.connection_lost.emit()
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    self.connection_lost.emit()
                break
            except Exception:
                if self._running:
                    self.connection_lost.emit()
                break

            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                self.line_received.emit(line)

                if line.startswith("DATA:"):
                    parts = line[5:].split(",")
                    if len(parts) == 3:
                        try:
                            t_ms = int(parts[0].strip())
                            raw_n = float(parts[1].strip())
                            filt_n = float(parts[2].strip())
                            self.data_received.emit(t_ms, raw_n, filt_n)
                        except ValueError:
                            pass

                elif line.startswith("STATE:"):
                    state = line[6:].strip()
                    self.state_changed.emit(state)

                elif line.startswith("T-"):
                    self.countdown_tick.emit(line)

                elif line == "COMPLETE":
                    self.state_changed.emit("COMPLETE")
                    self.complete_received.emit()

                elif line.startswith("ERR:"):
                    self.error_received.emit(line)


# =============================================================================
#  ARC GAUGE WIDGET
# =============================================================================
class ArcGauge(QWidget):
    """Custom arc-style gauge dial with scale markings."""

    def __init__(self, label="Value", unit="N", max_val=1000.0, parent=None):
        super().__init__(parent)
        self._label = label
        self._unit = unit
        self._max_val = max_val
        self._value = 0.0
        self.setMinimumSize(200, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_value(self, v):
        self._value = max(0.0, v)
        self.update()

    def set_max(self, m):
        self._max_val = max(1.0, m)
        self.update()

    @property
    def value(self):
        return self._value

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()
        h = self.height()

        # Arc geometry — 180° arc (semi-circle) from 180° to 0° (Qt degrees)
        arc_margin = 30
        arc_diameter = min(w - arc_margin * 2, (h - 45) * 2)
        if arc_diameter < 60:
            arc_diameter = 60
        arc_radius = arc_diameter / 2
        cx = w / 2
        cy = h * 0.52

        arc_rect = QRectF(cx - arc_radius, cy - arc_radius,
                          arc_diameter, arc_diameter)

        arc_thickness = max(12, arc_diameter * 0.09)

        # Draw background arc (grey track)
        pen_bg = QPen(QColor("#2a2a2a"), arc_thickness, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(arc_rect, 180 * 16, -180 * 16)

        # Draw filled arc (purple)
        fraction = min(self._value / self._max_val, 1.0) if self._max_val > 0 else 0
        sweep_deg = fraction * 180.0

        if sweep_deg > 0.5:
            # Gradient along arc
            grad = QConicalGradient(cx, cy, 180)
            grad.setColorAt(0.0, QColor("#a78bfa"))
            grad.setColorAt(0.5, QColor(ACCENT_PURPLE))
            grad.setColorAt(1.0, QColor(ACCENT_PURPLE_DIM))

            pen_fill = QPen(QBrush(grad), arc_thickness, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap)
            painter.setPen(pen_fill)
            painter.drawArc(arc_rect, 180 * 16, int(-sweep_deg * 16))

        # Glow at needle tip
        if fraction > 0.01:
            angle_rad = math.radians(180 - sweep_deg)
            tip_x = cx + arc_radius * math.cos(angle_rad)
            tip_y = cy - arc_radius * math.sin(angle_rad)
            glow_grad = QRadialGradient(tip_x, tip_y, arc_thickness * 1.5)
            glow_grad.setColorAt(0, QColor(167, 139, 250, 120))
            glow_grad.setColorAt(1, QColor(167, 139, 250, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(glow_grad))
            painter.drawEllipse(QPointF(tip_x, tip_y),
                                arc_thickness * 1.5, arc_thickness * 1.5)

        # Scale ticks and labels
        num_major = 5
        num_minor_per = 4
        tick_outer_r = arc_radius + arc_thickness / 2 + 2
        tick_inner_major = tick_outer_r + 8
        tick_inner_minor = tick_outer_r + 4
        label_r = tick_inner_major + 12

        painter.setPen(QPen(QColor(TEXT_DIM), 1))
        font_tick = QFont("Inter", 7)
        painter.setFont(font_tick)
        fm = QFontMetrics(font_tick)

        for i in range(num_major + 1):
            frac = i / num_major
            angle_deg = 180 - frac * 180
            angle_rad = math.radians(angle_deg)
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)

            x0 = cx + tick_outer_r * cos_a
            y0 = cy - tick_outer_r * sin_a
            x1 = cx + tick_inner_major * cos_a
            y1 = cy - tick_inner_major * sin_a

            painter.setPen(QPen(QColor(TEXT_SECONDARY), 1.5))
            painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))

            # Label
            val_at_tick = frac * self._max_val
            if val_at_tick >= 100:
                txt = f"{val_at_tick:.0f}"
            elif val_at_tick >= 10:
                txt = f"{val_at_tick:.0f}"
            else:
                txt = f"{val_at_tick:.1f}"
            tx = cx + label_r * cos_a - fm.horizontalAdvance(txt) / 2
            ty = cy - label_r * sin_a + fm.height() / 4
            painter.setPen(QPen(QColor(TEXT_DIM), 1))
            painter.drawText(QPointF(tx, ty), txt)

            # Minor ticks
            if i < num_major:
                for j in range(1, num_minor_per + 1):
                    mfrac = frac + j / (num_major * (num_minor_per + 1))
                    ma_deg = 180 - mfrac * 180
                    ma_rad = math.radians(ma_deg)
                    mx0 = cx + tick_outer_r * math.cos(ma_rad)
                    my0 = cy - tick_outer_r * math.sin(ma_rad)
                    mx1 = cx + tick_inner_minor * math.cos(ma_rad)
                    my1 = cy - tick_inner_minor * math.sin(ma_rad)
                    painter.setPen(QPen(QColor("#3a3a3a"), 0.8))
                    painter.drawLine(QPointF(mx0, my0), QPointF(mx1, my1))

        # Value text below arc
        painter.setPen(QPen(QColor(TEXT_PRIMARY)))
        val_font = QFont("Inter", 13, QFont.Weight.DemiBold)
        painter.setFont(val_font)
        if self._value >= 100:
            val_text = f"{self._label} : {self._value:.0f} {self._unit}"
        elif self._value >= 10:
            val_text = f"{self._label} : {self._value:.1f} {self._unit}"
        else:
            val_text = f"{self._label} : {self._value:.2f} {self._unit}"
        vm = QFontMetrics(val_font)
        vw = vm.horizontalAdvance(val_text)
        painter.drawText(QPointF(cx - vw / 2, cy + 18), val_text)

        painter.end()


# =============================================================================
#  STYLED BUTTON
# =============================================================================
def make_button(text, object_name="", is_fire=False):
    btn = QPushButton(text)
    btn.setObjectName(object_name or text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(42)
    btn.setMinimumWidth(120)
    if is_fire:
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {FIRE_RED};
                color: #ffffff;
                border: 1px solid #991b1b;
                border-radius: 8px;
                font-family: 'Inter', sans-serif;
                font-size: 13px;
                font-weight: 600;
                padding: 6px 18px;
            }}
            QPushButton:hover {{
                background-color: {FIRE_RED_HOVER};
                border-color: #7f1d1d;
            }}
            QPushButton:pressed {{
                background-color: #991b1b;
            }}
            QPushButton:disabled {{
                background-color: #292524;
                color: #57534e;
                border-color: #292524;
            }}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #1e1e2e;
                color: {TEXT_PRIMARY};
                border: 1px solid {PANEL_BORDER};
                border-radius: 8px;
                font-family: 'Inter', sans-serif;
                font-size: 13px;
                font-weight: 500;
                padding: 6px 18px;
            }}
            QPushButton:hover {{
                background-color: {ACCENT_PURPLE};
                border-color: {ACCENT_PURPLE};
            }}
            QPushButton:pressed {{
                background-color: {ACCENT_PURPLE_DIM};
            }}
            QPushButton:disabled {{
                background-color: #161622;
                color: #3f3f5c;
                border-color: #1e1e2e;
            }}
        """)
    return btn


# =============================================================================
#  MAIN WINDOW
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DJS Impulse — Static Fire Ground Station")
        self.setMinimumSize(1280, 800)

        # Connection state
        self._sock = None
        self._worker = None
        self._worker_thread = None
        self._current_state = "DISCONNECTED"
        self._connected = False

        # Data arrays
        self._data_t = []
        self._data_raw = []
        self._data_filt = []
        self._max_thrust = 0.0
        self._max_weight = 0.0

        # CSV
        self._csv_file = None
        self._csv_writer = None

        # Auto-scroll flag
        self._terminal_auto_scroll = True

        # Error flash timer
        self._error_flash_timer = QTimer(self)
        self._error_flash_timer.setSingleShot(True)
        self._error_flash_timer.timeout.connect(self._reset_terminal_border)

        self._build_ui()
        self._apply_global_style()
        self._update_button_states()
        self._update_connection_indicator()

    # ─────────────────────────────────────────────────────────────────
    #  BUILD UI
    # ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Connection bar ──
        conn_bar = QFrame()
        conn_bar.setObjectName("connBar")
        conn_bar.setFixedHeight(52)
        conn_bar_layout = QHBoxLayout(conn_bar)
        conn_bar_layout.setContentsMargins(16, 6, 16, 6)
        conn_bar_layout.setSpacing(12)

        title_lbl = QLabel("DJS Impulse")
        title_lbl.setStyleSheet(f"""
            color: {ACCENT_PURPLE};
            font-family: 'Inter', sans-serif;
            font-size: 16px;
            font-weight: 700;
            letter-spacing: 1px;
        """)
        conn_bar_layout.addWidget(title_lbl)

        conn_bar_layout.addSpacerItem(
            QSpacerItem(20, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        ip_label = QLabel("IP:")
        ip_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        conn_bar_layout.addWidget(ip_label)

        self.ip_input = QLineEdit(PICO_IP_DEFAULT)
        self.ip_input.setFixedWidth(140)
        self.ip_input.setObjectName("ipInput")
        conn_bar_layout.addWidget(self.ip_input)

        port_label = QLabel("Port:")
        port_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        conn_bar_layout.addWidget(port_label)

        self.port_input = QLineEdit(str(PICO_PORT_DEFAULT))
        self.port_input.setFixedWidth(60)
        self.port_input.setObjectName("portInput")
        conn_bar_layout.addWidget(self.port_input)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_btn.setFixedHeight(32)
        self.connect_btn.clicked.connect(self._toggle_connection)
        conn_bar_layout.addWidget(self.connect_btn)

        # Connection dot
        self.conn_dot = QLabel("●")
        self.conn_dot.setFixedWidth(20)
        conn_bar_layout.addWidget(self.conn_dot)

        # State badge
        self.state_badge = QLabel("DISCONNECTED")
        self.state_badge.setObjectName("stateBadge")
        conn_bar_layout.addWidget(self.state_badge)

        root_layout.addWidget(conn_bar)

        # ── Separator ──
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {PANEL_BORDER};")
        root_layout.addWidget(sep)

        # ── Main content area ──
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(10)

        # == LEFT: Terminal ==
        terminal_frame = QFrame()
        terminal_frame.setObjectName("terminalFrame")
        terminal_layout = QVBoxLayout(terminal_frame)
        terminal_layout.setContentsMargins(12, 10, 12, 10)
        terminal_layout.setSpacing(6)

        term_header = QLabel("Terminal")
        term_header.setStyleSheet(f"""
            color: {TEXT_SECONDARY};
            font-family: 'Inter', sans-serif;
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.5px;
        """)
        terminal_layout.addWidget(term_header)

        self.terminal = QTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setObjectName("terminalText")
        self.terminal.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.terminal.verticalScrollBar().rangeChanged.connect(self._on_terminal_range_changed)
        self.terminal.verticalScrollBar().valueChanged.connect(self._on_terminal_scroll)
        terminal_layout.addWidget(self.terminal)

        content_layout.addWidget(terminal_frame, stretch=2)

        # == CENTER: Gauges + Stats ==
        center_frame = QFrame()
        center_frame.setObjectName("centerFrame")
        center_layout = QVBoxLayout(center_frame)
        center_layout.setContentsMargins(10, 10, 10, 10)
        center_layout.setSpacing(4)

        self.thrust_gauge = ArcGauge("Thrust", "N", 1000.0)
        center_layout.addWidget(self.thrust_gauge, stretch=4)

        self.weight_gauge = ArcGauge("Weight", "kg", 200.0)
        center_layout.addWidget(self.weight_gauge, stretch=4)

        # Stats
        self.stats_label = QLabel("Max T: 0.0 N\nMax W: 0.0 kg")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stats_label.setStyleSheet(f"""
            color: {TEXT_SECONDARY};
            font-family: 'JetBrains Mono', 'Consolas', monospace;
            font-size: 14px;
            padding: 8px;
        """)
        center_layout.addWidget(self.stats_label, stretch=1)

        content_layout.addWidget(center_frame, stretch=2)

        # == RIGHT: Plot (top) + Buttons (bottom) ==
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        # Plot
        plot_frame = QFrame()
        plot_frame.setObjectName("plotFrame")
        plot_inner = QVBoxLayout(plot_frame)
        plot_inner.setContentsMargins(10, 10, 10, 10)

        pg.setConfigOptions(antialias=True, background=PANEL_BG, foreground=TEXT_PRIMARY)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel("bottom", "Time", units="s")
        self.plot_widget.setLabel("left", "Thrust", units="N")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.15)
        self.plot_widget.setBackground(PANEL_BG)
        self.plot_widget.getAxis("bottom").setPen(pg.mkPen(TEXT_DIM))
        self.plot_widget.getAxis("left").setPen(pg.mkPen(TEXT_DIM))
        self.plot_widget.getAxis("bottom").setTextPen(pg.mkPen(TEXT_SECONDARY))
        self.plot_widget.getAxis("left").setTextPen(pg.mkPen(TEXT_SECONDARY))

        self.raw_curve = self.plot_widget.plot(
            pen=pg.mkPen(color="#64748b", width=1), name="Raw Thrust")
        self.filt_curve = self.plot_widget.plot(
            pen=pg.mkPen(color=ACCENT_PURPLE, width=2.5), name="Filtered Thrust")

        legend = self.plot_widget.addLegend(offset=(10, 10))
        legend.setLabelTextColor(TEXT_SECONDARY)

        plot_inner.addWidget(self.plot_widget)
        right_layout.addWidget(plot_frame, stretch=5)

        # Buttons
        btn_frame = QFrame()
        btn_frame.setObjectName("btnFrame")
        btn_grid = QGridLayout(btn_frame)
        btn_grid.setContentsMargins(16, 14, 16, 14)
        btn_grid.setSpacing(10)

        self.btn_init      = make_button("Init",       "btnInit")
        self.btn_tare      = make_button("Tare",       "btnTare")
        self.btn_arm       = make_button("Arm",        "btnArm")
        self.btn_log_start = make_button("Log_Start",  "btnLogStart")
        self.btn_fire      = make_button("Fire",       "btnFire", is_fire=True)
        self.btn_log_stop  = make_button("Log_Stop",   "btnLogStop")
        self.btn_disarm    = make_button("Disarm",     "btnDisarm")
        self.btn_status    = make_button("Status?",    "btnStatus")

        btn_grid.addWidget(self.btn_init,      0, 0)
        btn_grid.addWidget(self.btn_log_stop,  0, 1)
        btn_grid.addWidget(self.btn_tare,      1, 0)
        btn_grid.addWidget(self.btn_disarm,    1, 1)
        btn_grid.addWidget(self.btn_arm,       2, 0)
        btn_grid.addWidget(self.btn_status,    2, 1)
        btn_grid.addWidget(self.btn_log_start, 3, 0, 1, 1)
        btn_grid.addWidget(self.btn_fire,      4, 0, 1, 1)

        # Brand label bottom-right
        brand = QLabel("DJS Impulse\nStatic Fire GUI")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setStyleSheet(f"""
            color: {TEXT_DIM};
            font-family: 'Inter', sans-serif;
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.5px;
            padding: 8px;
        """)
        btn_grid.addWidget(brand, 3, 1, 2, 1)

        right_layout.addWidget(btn_frame, stretch=3)
        content_layout.addWidget(right_widget, stretch=4)

        root_layout.addWidget(content, stretch=1)

        # ── Status bar ──
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet(f"""
            QStatusBar {{
                background-color: #111111;
                color: {TEXT_DIM};
                font-family: 'JetBrains Mono', 'Consolas', monospace;
                font-size: 11px;
                padding: 2px 12px;
                border-top: 1px solid {PANEL_BORDER};
            }}
        """)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(f"CSV: {CSV_FILE}  |  Ready")

        # ── Connect button signals ──
        self.btn_init.clicked.connect(lambda: self._send("INIT"))
        self.btn_tare.clicked.connect(lambda: self._send("TARE"))
        self.btn_arm.clicked.connect(lambda: self._send("ARM"))
        self.btn_log_start.clicked.connect(lambda: self._send("LOG_START"))
        self.btn_log_stop.clicked.connect(lambda: self._send("LOG_STOP"))
        self.btn_disarm.clicked.connect(lambda: self._send("DISARM"))
        self.btn_status.clicked.connect(lambda: self._send("STATUS"))
        self.btn_fire.clicked.connect(self._fire_confirm)

    # ─────────────────────────────────────────────────────────────────
    #  GLOBAL STYLESHEET
    # ─────────────────────────────────────────────────────────────────
    def _apply_global_style(self):
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {BG_DARK};
            }}
            QWidget {{
                background-color: {BG_DARK};
                color: {TEXT_PRIMARY};
                font-family: 'Inter', 'Segoe UI', sans-serif;
            }}
            #connBar {{
                background-color: #111111;
            }}
            #connectBtn {{
                background-color: {ACCENT_PURPLE};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 600;
                padding: 4px 16px;
            }}
            #connectBtn:hover {{
                background-color: #6d28d9;
            }}
            #stateBadge {{
                background-color: #1e1b4b;
                color: {ACCENT_PURPLE};
                border: 1px solid {ACCENT_PURPLE_DIM};
                border-radius: 10px;
                font-size: 11px;
                font-weight: 700;
                padding: 3px 12px;
                letter-spacing: 1px;
            }}
            QLineEdit {{
                background-color: #1a1a2e;
                color: {TEXT_PRIMARY};
                border: 1px solid {PANEL_BORDER};
                border-radius: 6px;
                padding: 4px 8px;
                font-family: 'JetBrains Mono', 'Consolas', monospace;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT_PURPLE};
            }}
            #terminalFrame {{
                background-color: {PANEL_BG};
                border: 1px solid {PANEL_BORDER};
                border-radius: 12px;
            }}
            #terminalText {{
                background-color: transparent;
                border: none;
                font-family: 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
                font-size: 11px;
                color: {TEXT_SECONDARY};
                selection-background-color: {ACCENT_PURPLE_DIM};
            }}
            #centerFrame {{
                background-color: {PANEL_BG};
                border: 1px solid {PANEL_BORDER};
                border-radius: 12px;
            }}
            #plotFrame {{
                background-color: {PANEL_BG};
                border: 1px solid {PANEL_BORDER};
                border-radius: 12px;
            }}
            #btnFrame {{
                background-color: {PANEL_BG};
                border: 1px solid {PANEL_BORDER};
                border-radius: 12px;
            }}
            QScrollBar:vertical {{
                background-color: {PANEL_BG};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background-color: #3a3a4a;
                border-radius: 4px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: {ACCENT_PURPLE_DIM};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QMessageBox {{
                background-color: #1a1a2e;
            }}
            QMessageBox QLabel {{
                color: {TEXT_PRIMARY};
                font-size: 13px;
            }}
            QMessageBox QPushButton {{
                background-color: {ACCENT_PURPLE};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 20px;
                font-weight: 600;
                min-width: 80px;
            }}
            QMessageBox QPushButton:hover {{
                background-color: #6d28d9;
            }}
        """)

    # ─────────────────────────────────────────────────────────────────
    #  CONNECTION
    # ─────────────────────────────────────────────────────────────────
    def _toggle_connection(self):
        if self._connected:
            self._disconnect()
        else:
            self._do_connect()

    def _do_connect(self):
        ip = self.ip_input.text().strip()
        try:
            port = int(self.port_input.text().strip())
        except ValueError:
            self._append_terminal("ERR: Invalid port number", "err")
            return

        self._append_terminal(f"Connecting to {ip}:{port} ...", "info")
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(TIMEOUT)
            self._sock.connect((ip, port))
            self._sock.settimeout(1.0)
        except Exception as e:
            self._append_terminal(f"ERR: Connection failed — {e}", "err")
            self._sock = None
            return

        self._connected = True
        self._current_state = "IDLE"
        self._append_terminal("Connected.", "ok")

        # Open CSV
        self._open_csv()

        # Start worker thread
        self._worker_thread = QThread()
        self._worker = TcpWorker(self._sock)
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.line_received.connect(self._on_line_received)
        self._worker.data_received.connect(self._on_data_received)
        self._worker.state_changed.connect(self._on_state_changed)
        self._worker.connection_lost.connect(self._on_connection_lost)
        self._worker.complete_received.connect(self._on_complete)
        self._worker.error_received.connect(self._on_error)

        self._worker_thread.start()

        self.connect_btn.setText("Disconnect")
        self._update_connection_indicator()
        self._update_button_states()

    def _disconnect(self):
        if self._worker:
            self._worker.stop()
        if self._worker_thread:
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._close_csv()
        self._connected = False
        self._current_state = "DISCONNECTED"
        self.connect_btn.setText("Connect")
        self._update_connection_indicator()
        self._update_button_states()
        self._append_terminal("Disconnected.", "info")

    def _on_connection_lost(self):
        self._connected = False
        self._current_state = "DISCONNECTED"
        self.connect_btn.setText("Connect")
        self._update_connection_indicator()
        self._update_button_states()
        self._close_csv()
        self._append_terminal("Connection lost.", "err")
        QMessageBox.warning(
            self, "Connection Lost",
            "TCP connection to Pico was lost.\n"
            "Plot data is preserved. Reconnect when ready.")

    # ─────────────────────────────────────────────────────────────────
    #  CSV
    # ─────────────────────────────────────────────────────────────────
    def _open_csv(self):
        exists = os.path.exists(CSV_FILE)
        self._csv_file = open(CSV_FILE, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        if not exists or os.path.getsize(CSV_FILE) == 0:
            self._csv_writer.writerow(["t_ms", "thrust_raw_N", "thrust_filt_N"])
            self._csv_file.flush()
        self.status_bar.showMessage(f"CSV: {CSV_FILE}  |  Connected")

    def _close_csv(self):
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    # ─────────────────────────────────────────────────────────────────
    #  SEND COMMAND
    # ─────────────────────────────────────────────────────────────────
    def _send(self, cmd):
        if not self._connected or not self._sock:
            self._append_terminal("ERR: Not connected", "err")
            return
        try:
            self._sock.sendall((cmd.strip() + "\n").encode())
            self._append_terminal(f">>> {cmd.strip()}", "sent")
        except Exception as e:
            self._append_terminal(f"ERR: Send failed — {e}", "err")

    def _fire_confirm(self):
        reply = QMessageBox.warning(
            self, "CONFIRM IGNITION",
            "Are you sure? This will fire the pyro charges.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._send("IGNITE")

    # ─────────────────────────────────────────────────────────────────
    #  SLOT HANDLERS
    # ─────────────────────────────────────────────────────────────────
    @pyqtSlot(str)
    def _on_line_received(self, line):
        if line.startswith("DATA:"):
            self._append_terminal(line, "data")
        elif line.startswith("OK:"):
            self._append_terminal(line, "ok")
        elif line.startswith("ERR:"):
            self._append_terminal(line, "err")
        elif line.startswith("STATE:"):
            self._append_terminal(line, "state")
        elif line.startswith("T-"):
            self._append_terminal(line, "countdown")
        elif line == "COMPLETE":
            self._append_terminal("*** LOGGING COMPLETE ***", "complete")
        else:
            self._append_terminal(line, "info")

    @pyqtSlot(int, float, float)
    def _on_data_received(self, t_ms, raw_n, filt_n):
        self._data_t.append(t_ms / 1000.0)
        self._data_raw.append(raw_n)
        self._data_filt.append(filt_n)

        # CSV
        if self._csv_writer:
            self._csv_writer.writerow([t_ms, raw_n, filt_n])
            try:
                self._csv_file.flush()
            except Exception:
                pass

        # Gauges
        weight = filt_n / 9.80665
        self.thrust_gauge.set_value(filt_n)
        self.weight_gauge.set_value(weight)

        # Max tracking
        if filt_n > self._max_thrust:
            self._max_thrust = filt_n
        if weight > self._max_weight:
            self._max_weight = weight

        # Auto-scale gauge ranges
        if self._max_thrust > self.thrust_gauge._max_val * 0.8:
            self.thrust_gauge.set_max(self._max_thrust * 1.2)
        if self._max_weight > self.weight_gauge._max_val * 0.8:
            self.weight_gauge.set_max(self._max_weight * 1.2)

        self.stats_label.setText(
            f"Max T: {self._max_thrust:.1f} N\nMax W: {self._max_weight:.1f} kg")

        # Update plot (throttled — every sample is fine for pyqtgraph)
        t_arr = np.array(self._data_t)
        self.raw_curve.setData(t_arr, np.array(self._data_raw))
        self.filt_curve.setData(t_arr, np.array(self._data_filt))

    @pyqtSlot(str)
    def _on_state_changed(self, state):
        self._current_state = state
        self._update_button_states()
        self._update_connection_indicator()

    @pyqtSlot()
    def _on_complete(self):
        self.status_bar.showMessage(
            f"Session complete — data saved to {CSV_FILE}")

    @pyqtSlot(str)
    def _on_error(self, msg):
        # Flash terminal border red
        self.findChild(QFrame, "terminalFrame").setStyleSheet(f"""
            #terminalFrame {{
                background-color: {PANEL_BG};
                border: 2px solid {RED_ERR};
                border-radius: 12px;
            }}
        """)
        self._error_flash_timer.start(1500)

    def _reset_terminal_border(self):
        self.findChild(QFrame, "terminalFrame").setStyleSheet(f"""
            #terminalFrame {{
                background-color: {PANEL_BG};
                border: 1px solid {PANEL_BORDER};
                border-radius: 12px;
            }}
        """)

    # ─────────────────────────────────────────────────────────────────
    #  TERMINAL
    # ─────────────────────────────────────────────────────────────────
    def _append_terminal(self, text, kind="info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S.") + \
             f"{datetime.datetime.now().microsecond // 1000:03d}"

        colour_map = {
            "data":      TEXT_DIM,
            "ok":        GREEN_OK,
            "err":       RED_ERR,
            "state":     ACCENT_PURPLE,
            "countdown": YELLOW_WARN,
            "complete":  BRIGHT_GREEN,
            "sent":      TEXT_PRIMARY,
            "info":      TEXT_SECONDARY,
        }
        colour = colour_map.get(kind, TEXT_SECONDARY)
        weight = "bold" if kind in ("state", "complete", "err") else "normal"

        html = (f'<span style="color:{TEXT_DIM};font-size:10px;">[{ts}]</span> '
                f'<span style="color:{colour};font-weight:{weight};">{text}</span>')

        self.terminal.append(html)

        if self._terminal_auto_scroll:
            sb = self.terminal.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_terminal_range_changed(self, min_val, max_val):
        if self._terminal_auto_scroll:
            self.terminal.verticalScrollBar().setValue(max_val)

    def _on_terminal_scroll(self, value):
        sb = self.terminal.verticalScrollBar()
        self._terminal_auto_scroll = (value >= sb.maximum() - 5)

    # ─────────────────────────────────────────────────────────────────
    #  BUTTON STATE MANAGEMENT
    # ─────────────────────────────────────────────────────────────────
    def _update_button_states(self):
        s = self._current_state
        connected = self._connected

        # Default: all disabled
        self.btn_init.setEnabled(False)
        self.btn_tare.setEnabled(False)
        self.btn_arm.setEnabled(False)
        self.btn_log_start.setEnabled(False)
        self.btn_fire.setEnabled(False)
        self.btn_log_stop.setEnabled(False)
        self.btn_disarm.setEnabled(False)
        self.btn_status.setEnabled(False)

        if not connected:
            return

        # STATUS and DISARM always available once connected
        self.btn_status.setEnabled(True)
        self.btn_disarm.setEnabled(True)

        if s == "IDLE":
            self.btn_init.setEnabled(True)
        elif s == "INIT":
            self.btn_tare.setEnabled(True)
        elif s == "TARE":
            self.btn_tare.setEnabled(True)   # allow re-tare
            self.btn_arm.setEnabled(True)
        elif s == "ARMED":
            self.btn_log_start.setEnabled(True)
        elif s == "LOG_READY":
            self.btn_fire.setEnabled(True)
        elif s == "IGNITE":
            pass  # countdown in progress
        elif s == "LOGGING":
            self.btn_log_stop.setEnabled(True)
        elif s == "COMPLETE":
            self.btn_init.setEnabled(True)  # allow restart

    # ─────────────────────────────────────────────────────────────────
    #  CONNECTION INDICATOR
    # ─────────────────────────────────────────────────────────────────
    def _update_connection_indicator(self):
        if self._connected:
            self.conn_dot.setStyleSheet(
                f"color: {GREEN_OK}; font-size: 18px; background: transparent;")
        else:
            self.conn_dot.setStyleSheet(
                f"color: {RED_ERR}; font-size: 18px; background: transparent;")

        self.state_badge.setText(self._current_state)

    # ─────────────────────────────────────────────────────────────────
    #  CLOSE
    # ─────────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._disconnect()
        event.accept()


# =============================================================================
#  ENTRY POINT
# =============================================================================
def main():
    app = QApplication(sys.argv)

    # Load Inter font if available (system or Google Fonts)
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
