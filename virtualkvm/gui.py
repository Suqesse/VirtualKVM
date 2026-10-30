"""Desktop controls and bounded, focused video/input presentation for VirtualKVM.

Controllers are imported only when a user starts a host or connects, allowing
the GUI to be inspected without starting network or capture services.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from typing import Callable

# Qt's default PipeWire plugin needs a desktop session/configuration. Use the
# supported PulseAudio backend only for explicitly headless Linux rendering.
if sys.platform.startswith("linux") and os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")

from PySide6.QtCore import QCoreApplication, QObject, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QImage, QKeyEvent, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QFrame, QScrollArea, QSlider, QSpinBox, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget,
)


class _Bridge(QObject):
    status = Signal(str)
    clipboard = Signal(str)
    finished = Signal(str, int, object, object)
    input_failed = Signal(str)
    shutdown_done = Signal()
    media = Signal(int, int, str, int, int)


def _emit(signal, *args) -> None:
    # A worker may finish just after the main window has been destroyed.
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


class _InputPump:
    """One ordered writer; coalesce movement without dropping key releases."""

    def __init__(self, send: Callable[[dict], None], failed: Callable[[str], None]):
        self._send, self._failed = send, failed
        self._queue: deque[dict] = deque()
        self._condition = threading.Condition()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="kvm-gui-input", daemon=True)
        self._thread.start()

    def submit(self, event: dict) -> bool:
        with self._condition:
            if self._stopped:
                return False
            if event.get("type") == "release_all":
                # No queued press may arrive after emergency release.
                self._queue.clear()
            if (self._queue and event.get("type") == "pointer"
                    and self._queue[-1].get("type") == "pointer"
                    and self._queue[-1].get("display") == event.get("display")):
                self._queue[-1] = event
            else:
                if len(self._queue) >= 512:
                    for index, queued in enumerate(self._queue):
                        if queued.get("type") == "pointer":
                            del self._queue[index]
                            break
                    else:
                        self._queue.clear()
                        self._stopped = True
                        self._condition.notify_all()
                        self._failed("Input queue overflow; disconnected to release held keys.")
                        return False
                self._queue.append(event)
            self._condition.notify()
        return True

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or bool(self._queue))
                if self._stopped:
                    return
                event = self._queue.popleft()
            try:
                self._send(event)
            except Exception as error:
                self.stop()
                self._failed(f"Input connection failed: {error}")
                return

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._queue.clear()
            self._condition.notify_all()


_KEY_NAMES = {
    Qt.Key.Key_Escape: "escape", Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Pause: "pause", Qt.Key.Key_Print: "printscreen",
    Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Up: "up", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Down: "down", Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Shift: "shift", Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Alt: "alt", Qt.Key.Key_Meta: "meta",
    Qt.Key.Key_CapsLock: "capslock", Qt.Key.Key_NumLock: "numlock",
    Qt.Key.Key_ScrollLock: "scrolllock", Qt.Key.Key_Space: "space",
}
_BUTTONS = {
    Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right",
    Qt.MouseButton.MiddleButton: "middle",
}
_SHIFTED_BASE = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
    "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",",
    ">": ".", "?": "/", "~": "`",
}

_STYLE = """
QMainWindow, QWidget#workspace { background: #0b1220; color: #e7edf7; }
QWidget { font-family: "Segoe UI", "Helvetica Neue", sans-serif; font-size: 13px; color: #e7edf7; }
QLabel#brandTitle { font-size: 29px; font-weight: 700; letter-spacing: 1px; }
QLabel#brandMark { background: #32d8bb; color: #062b2b; border-radius: 17px; font-size: 23px; font-weight: 800; }
QLabel#muted { color: #95a7bf; font-size: 12px; }
QLabel#connectionBadge { background: #152c38; color: #60e4ca; border: 1px solid #234b50; border-radius: 15px; padding: 9px 15px; }
QTabWidget::pane { background: #101c2e; border: 1px solid #223149; border-radius: 16px; top: -1px; }
QTabBar::tab { background: #101c2e; color: #9fb1ca; padding: 13px 26px; border: 1px solid #223149; border-bottom: 0; border-top-left-radius: 9px; border-top-right-radius: 9px; margin-right: 5px; }
QTabBar::tab:selected { background: #1b3348; color: #6ce8d0; border-color: #315169; }
QGroupBox { background: #132238; border: 1px solid #273951; border-radius: 12px; margin-top: 19px; padding: 22px 16px 15px; }
QGroupBox::title { color: #7ee7d2; subcontrol-origin: margin; subcontrol-position: top left; left: 16px; padding: 0 6px; font-weight: 600; }
QLineEdit, QSpinBox, QComboBox { min-height: 33px; background: #0c1829; border: 1px solid #32445e; border-radius: 7px; padding: 0 9px; selection-background-color: #247d76; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #45c9b5; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #6f829b; border-color: #223047; background: #111c2d; }
QComboBox::drop-down { width: 21px; border: 0; }
QComboBox QAbstractItemView { background: #14253b; color: #e7edf7; selection-background-color: #26685f; }
QPushButton { min-height: 35px; padding: 0 15px; border: 1px solid #3a506a; border-radius: 7px; background: #1c314b; font-weight: 500; }
QPushButton:hover { background: #29445f; border-color: #627f9d; }
QPushButton:pressed { background: #132b40; }
QPushButton:disabled { color: #63758b; background: #142137; border-color: #24344a; }
QPushButton#primaryButton { background: #40d6bb; color: #052e2e; border: 1px solid #40d6bb; min-width: 150px; font-weight: 700; }
QPushButton#primaryButton:hover { background: #6ee8d0; }
QPushButton#primaryButton:disabled { background: #25554f; color: #789c96; border-color: #25554f; }
QPushButton#dangerButton { color: #f5b1b8; border-color: #7b4654; background: #382332; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 17px; height: 17px; background: #0c1829; border: 1px solid #536c87; border-radius: 4px; }
QCheckBox::indicator:checked { background: #3dd7b8; border: 1px solid #3dd7b8; image: none; }
QCheckBox:disabled { color: #7b8ca3; }
QPlainTextEdit { background: #07101c; border: 1px solid #2a3c55; border-radius: 8px; padding: 9px; font-family: monospace; font-size: 11px; }
QToolButton { background: transparent; border: none; padding: 5px; color: #9fb1ca; }
QToolBar { background: #101c2e; border: none; spacing: 8px; padding: 7px; }
QStatusBar { background: #101c2e; color: #9fb1ca; }
QTableWidget, QTreeWidget { background: #101c2e; alternate-background-color: #14253b; border: 1px solid #2a3c55; gridline-color: #273951; }
QHeaderView::section { background: #1d324b; color: #b9cce2; padding: 8px; border: none; }
QDialog { background: #101c2e; }
QScrollBar:vertical { width: 10px; background: #101c2e; }
QScrollBar::handle:vertical { background: #3d536e; border-radius: 4px; min-height: 25px; }
"""


def normalized_key(event: QKeyEvent) -> str | None:
    key = event.key()
    if key in _KEY_NAMES:
        return _KEY_NAMES[key]
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        return f"f{key - Qt.Key.Key_F1 + 1}"
    if 33 <= key <= 126:
        character = chr(key).lower()
        return _SHIFTED_BASE.get(character, character)
    if sys.platform in ("win32", "darwin") and (event.nativeVirtualKey() or event.text()):
        # Use physical keys for layouts such as Russian rather than trying to
        # inject Unicode text as a key name. Quartz keycode zero is a valid A.
        from .native import native_key_name
        return native_key_name(sys.platform, event.nativeVirtualKey())
    return None


class VideoCanvas(QWidget):
    """Paint one latest frame, preserving aspect ratio and pointer geometry."""

    def __init__(self, display: int, send: Callable[[dict], None],
                 emergency: Callable[[], None], parent=None):
        super().__init__(parent)
        self.display, self._send, self._emergency = display, send, emergency
        self._image = QImage()
        self._media_size = None
        self._video_surface = None
        self._frame_serial = self._painted_serial = self.painted_frames = 0
        self._input_enabled = False
        self._pressed_keys: set[str] = set()
        self._pressed_buttons: set[str] = set()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)

    def set_frame(self, rgb: bytes, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
            return
        self._image = QImage(rgb, width, height, width * 3,
                             QImage.Format.Format_RGB888).copy()
        self._frame_serial += 1
        self.update()

    def image_rect(self) -> QRectF:
        if (self._image.isNull() and self._media_size is None) or not self.width() or not self.height():
            return QRectF()
        source_width, source_height = self._media_size or (self._image.width(), self._image.height())
        scale = min(self.width() / source_width, self.height() / source_height)
        width, height = source_width * scale, source_height * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._video_surface is not None:
            return
        if self._image.isNull():
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Ожидаем видеопоток…")
        else:
            painter.drawImage(self.image_rect(), self._image)
            if self._painted_serial != self._frame_serial:
                self.painted_frames += 1
                self._painted_serial = self._frame_serial

    def set_video_surface(self, surface: QWidget, width: int, height: int) -> None:
        self._media_size = (width, height)
        self._video_surface = surface
        surface.setParent(self)
        surface.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        surface.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        surface.setGeometry(self.image_rect().toRect())
        surface.show()
        self.update()

    def resizeEvent(self, event) -> None:
        if self._video_surface is not None:
            self._video_surface.setGeometry(self.image_rect().toRect())
        super().resizeEvent(event)

    def set_input_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.release_pressed()
        self._input_enabled = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

    def release_pressed(self) -> None:
        if self._pressed_keys or self._pressed_buttons:
            self._send({"type": "release_all"})
        self._pressed_keys.clear()
        self._pressed_buttons.clear()

    def focusOutEvent(self, event) -> None:
        self.release_pressed()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        modifiers = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                     | Qt.KeyboardModifier.ShiftModifier)
        if event.key() == Qt.Key.Key_Escape and event.modifiers() & modifiers == modifiers:
            self._emergency()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.window().isFullScreen():
            self.window().showNormal()
            event.accept()
            return
        key = normalized_key(event)
        if self._input_enabled and key:
            self._pressed_keys.add(key)
            self._send({"type": "key", "key": key, "down": True})
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            event.accept()
            return
        key = normalized_key(event)
        if key in self._pressed_keys:
            self._pressed_keys.discard(key)
            self._send({"type": "key", "key": key, "down": False})
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def _pointer(self, event, clamp: bool = False) -> bool:
        rect = self.image_rect()
        position = event.position()
        if rect.isEmpty() or (not clamp and not rect.contains(position)):
            return False
        self._send({"type": "pointer", "display": self.display,
                    "x": max(0.0, min(1.0, (position.x() - rect.left()) / rect.width())),
                    "y": max(0.0, min(1.0, (position.y() - rect.top()) / rect.height()))})
        return True

    def mouseMoveEvent(self, event) -> None:
        if self._input_enabled:
            self._pointer(event, clamp=bool(self._pressed_buttons))

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        button = _BUTTONS.get(event.button())
        if self._input_enabled and button and self._pointer(event):
            self._pressed_buttons.add(button)
            self._send({"type": "button", "button": button, "down": True})
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        button = _BUTTONS.get(event.button())
        if button in self._pressed_buttons:
            self._pointer(event, clamp=True)
            self._pressed_buttons.discard(button)
            self._send({"type": "button", "button": button, "down": False})
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        # Qt replaces the second press with MouseButtonDblClick. Forward it
        # as an ordinary down so the host can recognize a complete double click.
        self.mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        if not self._input_enabled or not self._pointer(event):
            return
        delta = event.angleDelta()
        dx, dy = delta.x() / 120.0, delta.y() / 120.0
        if not dx and not dy:
            delta = event.pixelDelta()
            dx, dy = delta.x() / 40.0, delta.y() / 40.0
        if dx or dy:
            self._send({"type": "scroll", "dx": dx, "dy": dy})
        event.accept()


class VideoWindow(QMainWindow):
    closed = Signal(int)
    media_error = Signal(str)

    def __init__(self, display: int, send, emergency, toggle_control, enabled: bool):
        super().__init__()
        self.display = display
        self._player = self._audio_output = self._video_widget = None
        self.av_frames = 0
        self._closing_media = False
        self._session_closing = False
        self.setWindowTitle(f"VirtualKVM · экран {display + 1}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.canvas = VideoCanvas(display, send, emergency, self)
        self.setCentralWidget(self.canvas)
        toolbar = QToolBar("Video controls", self)
        self.addToolBar(toolbar)
        self.control_action = QAction("Управление", self, checkable=True)
        self.control_action.setChecked(enabled)
        self.control_action.toggled.connect(toggle_control)
        toolbar.addAction(self.control_action)
        fullscreen = QAction("На весь экран · F11", self)
        fullscreen.setShortcut(QKeySequence("F11"))
        fullscreen.triggered.connect(lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen())
        toolbar.addAction(fullscreen)
        release = QAction("Вернуть управление", self)
        release.triggered.connect(emergency)
        toolbar.addAction(release)
        self.canvas.set_input_enabled(enabled)
        toolbar.addSeparator()
        self.mute_action = QAction("Без звука", self, checkable=True)
        self.mute_action.setEnabled(False)
        self.mute_action.toggled.connect(lambda muted: self._audio_output.setMuted(muted) if self._audio_output else None)
        toolbar.addAction(self.mute_action)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(100)
        self.volume.setEnabled(False)
        self.volume.setToolTip("Громкость")
        self.volume.valueChanged.connect(lambda value: self._audio_output.setVolume(value / 100) if self._audio_output else None)
        toolbar.addWidget(self.volume)
        self.statusBar().showMessage("Управление в активном окне · Ctrl+Alt+Shift+Esc — вернуть управление")
        self.resize(960, 600)

    def start_media(self, url: str, width: int, height: int) -> None:
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        source = QUrl(url)
        if source.scheme() != "http" or source.host() not in ("127.0.0.1", "localhost"):
            raise ValueError("AV-плеер принимает только внутренний локальный видеопоток")
        if self._player:
            self._player.stop()
            self._player.deleteLater()
            self._audio_output.deleteLater()
            self._video_widget.deleteLater()
        self._video_widget = QVideoWidget(self.canvas)
        self._video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.canvas.set_video_surface(self._video_widget, width, height)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self.volume.value() / 100)
        self._audio_output.setMuted(self.mute_action.isChecked())
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(self._video_widget)
        try:
            from PySide6.QtMultimedia import QPlaybackOptions
            options = QPlaybackOptions()
            options.setPlaybackIntent(QPlaybackOptions.PlaybackIntent.LowLatencyStreaming)
            self._player.setPlaybackOptions(options)
        except (ImportError, AttributeError):
            pass
        self.av_frames = 0
        self._video_widget.videoSink().videoFrameChanged.connect(self._av_frame)
        self._player.errorOccurred.connect(self._av_error)
        self._player.setSource(source)
        self._player.play()
        self.mute_action.setEnabled(True)
        self.volume.setEnabled(True)

    def _av_frame(self, frame) -> None:
        if frame.isValid():
            self.av_frames += 1

    def _av_error(self, error, text) -> None:
        if not self._closing_media:
            self.media_error.emit(f"Ошибка звука/видео: {text}")

    def set_input_enabled(self, enabled: bool) -> None:
        self.control_action.blockSignals(True)
        self.control_action.setChecked(enabled)
        self.control_action.blockSignals(False)
        self.canvas.set_input_enabled(enabled)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._player is not None and not self._session_closing:
            # Keep reading the AV relay if the other monitor remains open.
            # Closing the HTTP reader prematurely breaks the host media stream.
            self.canvas.release_pressed()
            self._audio_output.setMuted(True)
            self.hide()
            self.closed.emit(self.display)
            event.ignore()
            return
        self._closing_media = True
        if self._player:
            self._player.stop()
        self.canvas.release_pressed()
        self.closed.emit(self.display)
        super().closeEvent(event)

    def force_close(self) -> None:
        self._session_closing = True
        self.close()


class MainWindow(QMainWindow):
    def __init__(self, state_dir: Path | None = None):
        super().__init__()
        default_state = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VirtualKVM"
                         if sys.platform == "win32" else Path.home() / ".virtualkvm")
        self.state_dir = Path(state_dir) if state_dir else default_state
        self._bridge = _Bridge(self)
        self._bridge.status.connect(self._status)
        self._bridge.clipboard.connect(self._remote_clipboard)
        self._bridge.finished.connect(self._finished)
        self._bridge.input_failed.connect(self._input_failed)
        self._bridge.shutdown_done.connect(self._shutdown_finished)
        self._bridge.media.connect(self._receive_media)
        self._host = self._viewer = self._pump = None
        self._host_epoch = self._viewer_epoch = 0
        self._host_running = self._connecting = self._disconnecting = False
        self._closing = self._shutdown_complete = False
        self._threads: list[threading.Thread] = []
        self._lifecycle_locks: dict[int, threading.Lock] = {}
        self._frames_lock = threading.Lock()
        self._frames: dict[int, tuple[bytes, int, int]] = {}
        self.video_windows: dict[int, VideoWindow] = {}
        self._hidden_video_windows: dict[int, VideoWindow] = {}
        self._suppressed_displays: set[int] = set()
        self._last_clipboard_hash = ""
        self._clipboard_pending: str | None = None
        self._clipboard_busy = False
        self._requested_displays = []
        self._stats_at = time.monotonic()
        self._stats_samples = {}
        self._applying_profile = False
        self.setWindowTitle("VirtualKVM")
        available = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        self.resize(min(1140, max(760, available.width() - 40)) if available else 1140,
                    min(940, max(560, available.height() - 60)) if available else 900)
        self.setMinimumSize(760, 560)
        self.setStyleSheet(_STYLE)
        central = QWidget(self)
        central.setObjectName("workspace")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(30, 24, 30, 20)
        layout.setSpacing(18)
        heading = QHBoxLayout()
        mark = QLabel("VK")
        mark.setObjectName("brandMark")
        mark.setFixedSize(58, 58)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel("VirtualKVM")
        title.setObjectName("brandTitle")
        subtitle = QLabel("Два компьютера. Одно рабочее место.")
        subtitle.setObjectName("muted")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        heading.addLayout(brand)
        heading.addStretch()
        self.connection_badge = QLabel("●  Готов к подключению")
        self.connection_badge.setObjectName("connectionBadge")
        heading.addWidget(self.connection_badge)
        layout.addLayout(heading)
        tabs = QTabWidget()
        tabs.addTab(self._viewer_tab(), "Подключиться")
        tabs.addTab(self._host_tab(), "Этот компьютер")
        layout.addWidget(tabs)
        shared = QHBoxLayout()
        self.clipboard_enabled = QCheckBox("Общий буфер обмена")
        self.clipboard_enabled.setToolTip("Включите, чтобы передавать новые изменения текста в буфере обмена. До 64 КиБ.")
        self.clipboard_enabled.toggled.connect(self._clipboard_toggled)
        shared.addWidget(self.clipboard_enabled)
        self.shared_browser_button = QPushButton("Общая папка…")
        self.shared_browser_button.setEnabled(False)
        self.shared_browser_button.clicked.connect(self._browse_shared)
        shared.addWidget(self.shared_browser_button)
        shared.addStretch()
        release = QPushButton("Вернуть управление")
        release.setObjectName("dangerButton")
        release.clicked.connect(self.emergency_release)
        shared.addWidget(release)
        layout.addLayout(shared)
        self.status_label = QLabel("Начните с запуска хоста на втором компьютере. Затем введите его адрес, PIN и отпечаток.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(160)
        self.log.setVisible(False)
        diagnostics = QToolButton()
        diagnostics.setText("Диагностика")
        diagnostics.setCheckable(True)
        diagnostics.setArrowType(Qt.ArrowType.RightArrow)
        diagnostics.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        diagnostics.toggled.connect(lambda checked: (self.log.setVisible(checked), diagnostics.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)))
        layout.addWidget(diagnostics)
        layout.addWidget(self.log)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidget(central)
        self.setCentralWidget(self._scroll)
        emergency = QAction("Emergency release", self)
        emergency.setShortcut(QKeySequence("Ctrl+Alt+Shift+Esc"))
        emergency.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        emergency.triggered.connect(self.emergency_release)
        self.addAction(emergency)
        QApplication.clipboard().dataChanged.connect(self._local_clipboard)
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setSingleShot(True)
        self._clipboard_timer.setInterval(150)
        self._clipboard_timer.timeout.connect(self._flush_clipboard)
        self._frame_timer = QTimer(self)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(4)
        self._frame_timer.timeout.connect(self._poll_frames)
        self._frame_timer.start()
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start()
        self._load_preferences()

    @staticmethod
    def _spin(low: int, high: int, value: int, step: int = 1) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(low, high)
        widget.setSingleStep(step)
        widget.setValue(value)
        return widget

    def _host_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(22, 24, 22, 22)
        intro = QLabel("<b>Сделайте этот компьютер доступным</b><br>Запустите хост и передайте данные подключения на второй компьютер.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        columns = QHBoxLayout()
        columns.setSpacing(22)
        access = QGroupBox("01  ·  Подключение и доверие")
        form = QFormLayout(access)
        form.setSpacing(14)
        self.host_bind = QLineEdit("0.0.0.0")
        self.host_port = self._spin(1024, 65535, 24800)
        self.host_pin = QLineEdit(secrets.token_urlsafe(9))
        self.host_fingerprint = QLineEdit()
        self.host_fingerprint.setReadOnly(True)
        self.host_fingerprint.setPlaceholderText("Появится после запуска хоста")
        self.synthetic = QCheckBox("Тестовый экран вместо захвата")
        self.synthetic.setChecked(sys.platform not in ("win32", "darwin"))
        addresses = set()
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                         if not item[4][0].startswith("127.")}
        except OSError:
            pass
        lan = QLabel("Адрес в локальной сети: " + (", ".join(sorted(addresses)) or "посмотрите в настройках сети"))
        lan.setWordWrap(True)
        lan.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(lan)
        form.addRow("Интерфейс", self.host_bind)
        form.addRow("Порт", self.host_port)
        form.addRow("PIN", self.host_pin)
        self.new_pin = QPushButton("Новый PIN")
        self.new_pin.clicked.connect(lambda: self.host_pin.setText(secrets.token_urlsafe(9)))
        form.addRow("", self.new_pin)
        form.addRow("Отпечаток", self.host_fingerprint)
        copy_fingerprint = QPushButton("Скопировать отпечаток")
        copy_fingerprint.clicked.connect(lambda: QApplication.clipboard().setText(self.host_fingerprint.text())
                                         if self.host_fingerprint.text() else None)
        form.addRow("", copy_fingerprint)
        form.addRow(self.synthetic)
        columns.addWidget(access, 1)
        extras = QGroupBox("02  ·  Звук и общая папка")
        extra_form = QFormLayout(extras)
        extra_form.setSpacing(14)
        self.host_audio_device = QComboBox()
        self.host_audio_device.setEditable(True)
        self.host_audio_device.setPlaceholderText("Выберите источник системного звука")
        audio_refresh = QPushButton("Найти источники звука")
        audio_refresh.clicked.connect(self._audio_devices)
        self.audio_refresh = audio_refresh
        extra_form.addRow("Источник звука", self.host_audio_device)
        extra_form.addRow("", audio_refresh)
        audio_help = QLabel("Для системного звука нужен loopback-источник: Stereo Mix, BlackHole или аналог. "
                            "В тестовом режиме используется звуковой сигнал.")
        audio_help.setWordWrap(True)
        audio_help.setObjectName("muted")
        extra_form.addRow(audio_help)
        self.share_enabled = QCheckBox("Открыть общую папку")
        self.shared_path = QLineEdit()
        self.shared_path.setReadOnly(True)
        self.shared_path.setPlaceholderText("Выберите только ту папку, которой хотите поделиться")
        self.shared_choose = QPushButton("Выбрать папку…")
        self.shared_choose.clicked.connect(self._choose_shared_directory)
        self.shared_writable = QCheckBox("Разрешить загрузку в папку")
        self.shared_writable.setToolTip("По умолчанию доступно только чтение. Существующие файлы не заменяются.")
        extra_form.addRow(self.share_enabled)
        extra_form.addRow(self.shared_path)
        extra_form.addRow(self.shared_choose)
        extra_form.addRow(self.shared_writable)
        shared_help = QLabel("Партнёр увидит только выбранную папку. По умолчанию — только чтение, без удаления файлов.")
        shared_help.setWordWrap(True)
        shared_help.setObjectName("muted")
        extra_form.addRow(shared_help)
        columns.addWidget(extras, 1)
        layout.addLayout(columns, 1)
        buttons = QHBoxLayout()
        self.host_start = QPushButton("Запустить хост")
        self.host_start.setObjectName("primaryButton")
        self.host_stop = QPushButton("Остановить")
        self.host_stop.setEnabled(False)
        self.host_start.clicked.connect(self._start_host)
        self.host_stop.clicked.connect(self._stop_host)
        buttons.addWidget(self.host_start)
        buttons.addWidget(self.host_stop)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.display_label = QLabel("После подключения партнёр сможет выбрать один или два экрана.")
        self.display_label.setWordWrap(True)
        layout.addWidget(self.display_label)
        note = QLabel("Сверьте полный отпечаток перед подключением. Полученные отдельные файлы: Downloads/VirtualKVM.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        return widget

    def _viewer_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(22, 24, 22, 22)
        intro = QLabel("<b>Ваше рабочее место — на другом компьютере</b><br>Безопасное подключение, отдельное окно для каждого экрана.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        columns = QHBoxLayout()
        columns.setSpacing(22)
        connection = QGroupBox("01  ·  Второй компьютер")
        form = QFormLayout(connection)
        form.setSpacing(14)
        self.peer_host = QLineEdit()
        self.peer_host.setPlaceholderText("192.168.1.10")
        self.peer_port = self._spin(1024, 65535, 24800)
        self.peer_pin = QLineEdit()
        self.peer_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.peer_fingerprint = QLineEdit()
        self.peer_fingerprint.setPlaceholderText("Вставьте полный SHA-256 отпечаток хоста")
        form.addRow("Адрес хоста", self.peer_host)
        form.addRow("Порт", self.peer_port)
        form.addRow("PIN", self.peer_pin)
        form.addRow("Отпечаток", self.peer_fingerprint)
        sources = QHBoxLayout()
        self.display_first = QCheckBox("Первый экран")
        self.display_first.setChecked(True)
        self.display_first.setEnabled(False)
        self.display_first_index = self._spin(0, 63, 0)
        self.display_second = QCheckBox("Второй экран")
        self.display_second_index = self._spin(0, 63, 1)
        sources.addWidget(self.display_first)
        sources.addWidget(self.display_first_index)
        sources.addWidget(self.display_second)
        sources.addWidget(self.display_second_index)
        form.addRow("Экраны хоста", sources)
        self.audio_enabled = QCheckBox("Передавать звук")
        form.addRow(self.audio_enabled)
        audio_note = QLabel("Звук и видео используют общий тактовый источник. Звук передаётся с первого выбранного экрана.")
        audio_note.setWordWrap(True)
        audio_note.setObjectName("muted")
        form.addRow(audio_note)
        columns.addWidget(connection, 1)
        quality = QGroupBox("02  ·  Качество и скорость")
        quality_form = QFormLayout(quality)
        quality_form.setSpacing(12)
        self.profile = QComboBox()
        self.profile.addItems(["Игры · 240 FPS", "Работа · 60 FPS", "Качество · 4K / 60 FPS", "Свой профиль"])
        self.native_resolution = QCheckBox("Родное разрешение источника")
        self.native_resolution.toggled.connect(self._resolution_mode)
        self.resolution_preset = QComboBox()
        resolutions = {(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160), (7680, 4320)}
        for screen in QApplication.screens():
            size = screen.size() * screen.devicePixelRatio()
            resolutions.add((size.width(), size.height()))
        for width, height in sorted(resolutions):
            self.resolution_preset.addItem(f"{width} × {height}", (width, height))
        self.resolution_preset.addItem("Свой размер…", None)
        self.video_width = self._spin(1, 16384, 1920)
        self.video_height = self._spin(1, 16384, 1080)
        resolution = QHBoxLayout()
        resolution.addWidget(self.video_width)
        resolution.addWidget(QLabel("×"))
        resolution.addWidget(self.video_height)
        self.frame_rate = self._spin(1, 240, 240)
        self.bitrate = self._spin(1, 500, 45)
        self.latency = self._spin(10, 2000, 30)
        self.codec = QComboBox()
        self.codec.addItem("H.264 · совместимость", "h264")
        self.codec.addItem("HEVC · H.265", "hevc")
        self.encoder = QComboBox()
        self.encoder.addItems(["auto", "libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"])
        self.codec.currentIndexChanged.connect(self._codec_changed)
        quality_form.addRow("Профиль", self.profile)
        quality_form.addRow(self.native_resolution)
        quality_form.addRow("Разрешение", self.resolution_preset)
        quality_form.addRow("Свой размер", resolution)
        quality_form.addRow("Частота · FPS", self.frame_rate)
        quality_form.addRow("Кодек", self.codec)
        quality_form.addRow("Видеокодер", self.encoder)
        quality_form.addRow("Мбит/с на экран", self.bitrate)
        quality_form.addRow("Буфер · мс", self.latency)
        self.profile.currentIndexChanged.connect(self._apply_profile)
        self.resolution_preset.currentIndexChanged.connect(self._apply_resolution)
        self.resolution_preset.setCurrentIndex(self._resolution_index(1920, 1080))
        columns.addWidget(quality, 1)
        layout.addLayout(columns, 1)
        target = QLabel("240 FPS — ваша цель. Фактическая частота зависит от разрешения, видеокарты, сети и мониторов. "
                        "Счётчики приёма и вывода кадров доступны в окне каждого экрана.")
        target.setWordWrap(True)
        target.setObjectName("muted")
        layout.addWidget(target)
        buttons = QHBoxLayout()
        self.connect_button = QPushButton("Подключиться")
        self.connect_button.setObjectName("primaryButton")
        self.disconnect_button = QPushButton("Отключиться")
        self.disconnect_button.setEnabled(False)
        self.connect_button.clicked.connect(self._connect_viewer)
        self.disconnect_button.clicked.connect(self._disconnect_viewer)
        self.send_file_button = QPushButton("Отправить файл…")
        self.send_file_button.setEnabled(False)
        self.send_file_button.clicked.connect(self._choose_file)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.send_file_button)
        layout.addLayout(buttons)
        self.control_enabled = QCheckBox("Управлять клавиатурой и мышью в активном окне экрана")
        self.control_enabled.setEnabled(False)
        self.control_enabled.toggled.connect(self._control_toggled)
        layout.addWidget(self.control_enabled)
        self._viewer_fields = (self.peer_host, self.peer_port, self.peer_pin, self.peer_fingerprint,
            self.display_first_index, self.display_second, self.display_second_index, self.video_width,
            self.video_height, self.frame_rate, self.bitrate, self.latency, self.encoder,
            self.profile, self.native_resolution, self.resolution_preset, self.audio_enabled, self.codec)
        for field in (self.frame_rate, self.bitrate, self.latency):
            field.valueChanged.connect(self._custom_profile)
        self.video_width.valueChanged.connect(self._dimensions_changed)
        self.video_height.valueChanged.connect(self._dimensions_changed)
        self.encoder.currentIndexChanged.connect(self._custom_profile)
        return widget

    def _apply_profile(self, index: int) -> None:
        values = ((1920, 1080, 240, 45, 30), (1920, 1080, 60, 16, 60),
                  (3840, 2160, 60, 60, 60))
        if index >= len(values):
            return
        self._applying_profile = True
        width, height, fps, bitrate, latency = values[index]
        self.video_width.setValue(width)
        self.video_height.setValue(height)
        self.frame_rate.setValue(fps)
        self.bitrate.setValue(bitrate)
        self.latency.setValue(latency)
        self.encoder.setCurrentIndex(0)
        self.resolution_preset.setCurrentIndex(self._resolution_index(width, height))
        self._applying_profile = False

    def _custom_profile(self, *args) -> None:
        if not self._applying_profile:
            self.profile.setCurrentIndex(3)

    def _codec_changed(self, index: int) -> None:
        self.encoder.clear()
        codec = self.codec.currentData()
        self.encoder.addItems(["auto", "libx265" if codec == "hevc" else "libx264"] +
            [f"{codec}_{backend}" for backend in ("nvenc", "qsv", "amf", "videotoolbox")])
        self._custom_profile()

    def _apply_resolution(self, index: int) -> None:
        value = self.resolution_preset.itemData(index)
        if value:
            self.video_width.setValue(value[0])
            self.video_height.setValue(value[1])

    def _dimensions_changed(self, *args) -> None:
        self.resolution_preset.blockSignals(True)
        self.resolution_preset.setCurrentIndex(self._resolution_index(self.video_width.value(), self.video_height.value()))
        self.resolution_preset.blockSignals(False)
        self._custom_profile()

    def _resolution_index(self, width: int, height: int) -> int:
        for index in range(self.resolution_preset.count()):
            value = self.resolution_preset.itemData(index)
            if value is not None and tuple(value) == (width, height):
                return index
        return self.resolution_preset.count() - 1

    def _resolution_mode(self, native: bool) -> None:
        enabled = not native and self._viewer is None
        for field in (self.video_width, self.video_height, self.resolution_preset):
            field.setEnabled(enabled)

    def _set_viewer_fields(self, enabled: bool) -> None:
        for field in self._viewer_fields:
            field.setEnabled(enabled)
        if enabled:
            self._resolution_mode(self.native_resolution.isChecked())

    def _choose_shared_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Выберите общую папку", self.shared_path.text())
        if selected:
            self.shared_path.setText(selected)
            self.share_enabled.setChecked(True)

    def _browse_shared(self) -> None:
        if self._viewer is None or self._connecting or self._disconnecting:
            return
        from .shared_gui import SharedFolderDialog
        dialog = SharedFolderDialog(self._viewer, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _audio_devices(self) -> None:
        from .media import FFmpegLocator
        self.audio_refresh.setEnabled(False)
        result = []
        self._task("audio-devices", 0, result, lambda: result.extend(FFmpegLocator().audio_devices()))

    def _audio_device_value(self) -> str:
        text = self.host_audio_device.currentText()
        if text == self.host_audio_device.itemText(self.host_audio_device.currentIndex()):
            return str(self.host_audio_device.currentData() or text)
        return text

    def _update_stats(self) -> None:
        now = time.monotonic()
        elapsed = max(now - self._stats_at, 0.001)
        self._stats_at = now
        receivers = dict(zip(self._requested_displays, list(self._viewer.receivers))) if self._viewer else {}
        for index, window in list(self.video_windows.items()):
            receiver = receivers.get(index)
            presented = window.av_frames if window._player else window.canvas.painted_frames
            decoded = getattr(receiver, "frames_received", None)
            previous_painted, previous_decoded = self._stats_samples.get(index, (presented, decoded or 0))
            paint_rate = max(0, presented - previous_painted) / elapsed
            if decoded is not None:
                decode_rate = max(0, decoded - previous_decoded) / elapsed
                drops = getattr(receiver, "frames_dropped", 0)
                detail = f"Декодировано {decode_rate:.0f} FPS · выведено {paint_rate:.0f} FPS · пропущено при выводе {drops}"
            else:
                detail = f"Qt AV: {paint_rate:.0f} кадров/с · синхронное воспроизведение звука и видео"
            window.statusBar().showMessage(f"{detail} · цель {self.frame_rate.value()} FPS · Ctrl+Alt+Shift+Esc — вернуть управление")
            self._stats_samples[index] = (presented, decoded or 0)

    def _receive_media(self, epoch: int, index: int, url: str, width: int, height: int) -> None:
        if self._closing or epoch != self._viewer_epoch or self._disconnecting or index in self._suppressed_displays:
            return
        try:
            self._video_window(index).start_media(url, width, height)
        except Exception as error:
            self._status(f"Не удалось запустить звук и видео: {error}")
            self._disconnect_viewer()

    def _preference_fields(self) -> dict:
        return {"host_bind": self.host_bind, "host_port": self.host_port,
            "peer_host": self.peer_host, "peer_port": self.peer_port,
            "width": self.video_width, "height": self.video_height,
            "fps": self.frame_rate, "bitrate": self.bitrate, "latency": self.latency,
            "first_display": self.display_first_index, "second_display": self.display_second_index,
            "shared_path": self.shared_path}

    def _save_preferences(self) -> None:
        data = {name: field.value() if isinstance(field, QSpinBox) else field.text()
                for name, field in self._preference_fields().items()}
        data.update(profile=self.profile.currentIndex(), encoder=self.encoder.currentText(),
                    codec=self.codec.currentData(),
                    native_resolution=self.native_resolution.isChecked(),
                    audio=self.audio_enabled.isChecked(), audio_device=self._audio_device_value(),
                    second_display_enabled=self.display_second.isChecked())
        # Deliberately exclude PINs, certificate fingerprints and sharing/write
        # consent. A stored address must never silently authorize another peer.
        temporary = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state_dir,
                                             prefix="preferences-", suffix=".tmp", delete=False) as output:
                temporary = output.name
                json.dump(data, output, ensure_ascii=False, indent=2)
            os.replace(temporary, self.state_dir / "preferences.json")
        except OSError:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def _load_preferences(self) -> None:
        try:
            path = self.state_dir / "preferences.json"
            if path.stat().st_size > 65536:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
        except (OSError, ValueError):
            return
        self._applying_profile = True
        for name, field in self._preference_fields().items():
            value = data.get(name)
            if isinstance(field, QSpinBox) and type(value) is int:
                field.setValue(value)
            elif isinstance(field, QLineEdit) and isinstance(value, str):
                field.setText(value[:4096])
        for name, field in (("native_resolution", self.native_resolution), ("audio", self.audio_enabled),
                            ("second_display_enabled", self.display_second)):
            if type(data.get(name)) is bool:
                field.setChecked(data[name])
        self.codec.setCurrentIndex(max(0, self.codec.findData(data.get("codec", "h264"))))
        encoder_index = self.encoder.findText(str(data.get("encoder", "auto")))
        if encoder_index >= 0:
            self.encoder.setCurrentIndex(encoder_index)
        self.host_audio_device.setEditText(str(data.get("audio_device", ""))[:4096])
        self.resolution_preset.setCurrentIndex(self._resolution_index(self.video_width.value(), self.video_height.value()))
        if type(data.get("profile")) is int and 0 <= data["profile"] <= 3:
            self.profile.blockSignals(True)
            self.profile.setCurrentIndex(data["profile"])
            self.profile.blockSignals(False)
        self._applying_profile = False
        self._resolution_mode(self.native_resolution.isChecked())

    def _status(self, message: str) -> None:
        if self._closing:
            return
        message = str(message)[:4096]
        for pin in (self.host_pin.text(), self.peer_pin.text()):
            if len(pin) >= 4:
                message = message.replace(pin, "[redacted]")
        message = re.sub(r"(?i)(passphrase=)[^&\s]+", r"\1[redacted]", message)
        self.status_label.setText(message)
        self.log.appendPlainText(message)

    def _task(self, kind: str, epoch: int, owner, operation: Callable[[], None]) -> None:
        lifecycle = kind.startswith(("host-", "viewer-"))
        lock = self._lifecycle_locks.setdefault(id(owner), threading.Lock()) if lifecycle else None

        def work():
            error = None
            try:
                if lock is None:
                    operation()
                else:
                    # Serializing startup and teardown prevents a canceled
                    # connect from starting fresh decoder processes after stop.
                    with lock:
                        operation()
            except Exception as failure:
                error = str(failure)
            _emit(self._bridge.finished, kind, epoch, owner, error)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        thread = threading.Thread(target=work, name=f"kvm-gui-{kind}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _start_host(self) -> None:
        if self._host is not None or self._closing:
            return
        if len(self.host_pin.text()) < 8:
            self._status("PIN хоста должен содержать не менее восьми символов.")
            return
        if self.share_enabled.isChecked() and (not self.shared_path.text().strip()
                                              or not Path(self.shared_path.text()).is_dir()):
            self._status("Выберите существующую общую папку перед запуском хоста.")
            return
        try:
            from .app import HostController
            self._host_epoch += 1
            self._host = HostController(self.state_dir, self.host_bind.text().strip(), self.host_port.value(),
                self.host_pin.text(), synthetic=self.synthetic.isChecked(),
                on_status=lambda text: _emit(self._bridge.status, str(text)),
                on_clipboard=lambda text: _emit(self._bridge.clipboard, str(text)),
                audio_device=self._audio_device_value(),
                shared_dir=Path(self.shared_path.text()) if self.share_enabled.isChecked() else None,
                shared_writable=self.shared_writable.isChecked() if self.share_enabled.isChecked() else False)
        except Exception as error:
            self._status(f"Не удалось создать хост: {error}")
            return
        self._host_fields(False)
        self.host_start.setEnabled(False)
        self.connection_badge.setText("●  Запускаем хост…")
        self._status("Запускаем хост…")
        self._task("host-start", self._host_epoch, self._host, self._host.start)

    def _host_fields(self, enabled: bool) -> None:
        for field in (self.host_bind, self.host_port, self.host_pin, self.new_pin, self.synthetic,
                      self.host_audio_device, self.audio_refresh, self.share_enabled, self.shared_choose,
                      self.shared_writable):
            field.setEnabled(enabled)

    def _stop_host(self) -> None:
        if self._host is None:
            return
        self.host_stop.setEnabled(False)
        self._host_epoch += 1
        self._task("host-stop", self._host_epoch, self._host, self._host.stop)

    def _connect_viewer(self) -> None:
        if self._viewer is not None or self._closing:
            return
        fingerprint = re.sub(r"[:\s]", "", self.peer_fingerprint.text()).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            self._status("Вставьте и сверьте полный SHA-256 отпечаток: 64 шестнадцатеричных символа.")
            return
        host = self.peer_host.text().strip()
        if not host or any(character.isspace() for character in host) or not self.peer_pin.text():
            self._status("Введите адрес второго компьютера и его PIN.")
            return
        displays = [self.display_first_index.value()]
        if self.display_second.isChecked():
            displays.append(self.display_second_index.value())
        if len(set(displays)) != len(displays):
            self._status("Для двух окон выберите разные номера экранов хоста.")
            return
        config = {"width": self.video_width.value(), "height": self.video_height.value(),
                  "fps": self.frame_rate.value(), "bitrate_mbps": self.bitrate.value(),
                  "codec": self.codec.currentData(), "encoder": self.encoder.currentText(),
                  "latency_ms": self.latency.value(), "source_resolution": self.native_resolution.isChecked(),
                  "audio": self.audio_enabled.isChecked()}
        if config["width"] * config["height"] > 67108864 and not config["source_resolution"]:
            self._status("Выберите разрешение до 64 мегапикселей на экран.")
            return
        self._viewer_epoch += 1
        epoch = self._viewer_epoch
        try:
            from .app import ViewerController
            self._viewer = ViewerController(self.state_dir,
                on_status=lambda text: _emit(self._bridge.status, str(text)),
                on_frame=lambda index, rgb, width, height: self._receive_frame(epoch, index, rgb, width, height),
                on_media=lambda index, url, width, height: _emit(self._bridge.media, epoch, index, url, width, height),
                on_clipboard=lambda text: _emit(self._bridge.clipboard, str(text)))
        except Exception as error:
            self._status(f"Не удалось создать клиент: {error}")
            return
        self._suppressed_displays.clear()
        self._requested_displays = displays
        self._stats_samples.clear()
        self._connecting = True
        self._set_viewer_fields(False)
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        controller = self._viewer
        # Read widget values before crossing the Qt thread boundary.
        port, pin = self.peer_port.value(), self.peer_pin.text()
        self.connection_badge.setText("●  Подключаемся…")
        self._status("Проверяем отпечаток и устанавливаем безопасное соединение…")
        self._task("viewer-connect", epoch, controller,
                   lambda: controller.connect(host, port, pin, fingerprint, displays, config))

    def _disconnect_viewer(self) -> None:
        if self._viewer is None or self._disconnecting:
            return
        self._disconnecting = True
        self._viewer_epoch += 1
        self.emergency_release()
        self.control_enabled.setEnabled(False)
        self.send_file_button.setEnabled(False)
        self.shared_browser_button.setEnabled(False)
        self.disconnect_button.setEnabled(False)
        if self._pump:
            self._pump.stop()
            self._pump = None
        self._quiesce_video_windows()
        self._task("viewer-disconnect", self._viewer_epoch, self._viewer, self._viewer.disconnect)

    def _finished(self, kind: str, epoch: int, owner, error) -> None:
        if self._closing:
            # The shutdown worker owns controller teardown and takes the same
            # lifecycle lock after any in-flight startup finishes.
            return
        if kind.startswith("host"):
            if epoch != self._host_epoch:
                if kind == "host-start":
                    self._task("host-stale", -1, owner, owner.stop)
                return
            if kind == "host-start" and error is None:
                self._host_running = True
                self.host_stop.setEnabled(True)
                self.host_fingerprint.setText(str(owner.fingerprint))
                self.display_label.setText("Доступные экраны: " + "; ".join(
                    f"{display.get('index', '?')}: {display.get('name', 'Display')} "
                    f"({display.get('width', '?')}×{display.get('height', '?')})"
                    for display in owner.displays))
                self.connection_badge.setText("●  Хост доступен")
                self._status("Хост запущен. Передайте адрес, PIN и отпечаток на второй компьютер.")
            else:
                if kind == "host-start":
                    self._task("host-stale", -1, owner, owner.stop)
                self._host = None
                self._host_running = False
                self.host_start.setEnabled(True)
                self.host_stop.setEnabled(False)
                self._host_fields(True)
                self.connection_badge.setText("●  Готов к подключению")
                self._status(f"Ошибка хоста: {error}" if error else "Хост остановлен.")
        elif kind.startswith("viewer"):
            if epoch != self._viewer_epoch:
                if kind == "viewer-connect":
                    self._task("viewer-stale", -1, owner, owner.disconnect)
                return
            if kind == "viewer-connect" and error is None:
                self._connecting = False
                self._pump = _InputPump(owner.send_event,
                    lambda text: _emit(self._bridge.input_failed, text))
                self.control_enabled.setEnabled(True)
                self.send_file_button.setEnabled(True)
                self.shared_browser_button.setEnabled(True)
                self.connection_badge.setText("●  Соединение защищено")
                self._status("Вы подключены. Видеопоток откроется в отдельных окнах. Включите управление, когда будете готовы.")
            else:
                if kind == "viewer-connect":
                    self._task("viewer-stale", -1, owner, owner.disconnect)
                self._viewer = None
                self._connecting = self._disconnecting = False
                self._close_video_windows()
                self.connect_button.setEnabled(True)
                self._set_viewer_fields(True)
                self.disconnect_button.setEnabled(False)
                self.send_file_button.setEnabled(False)
                self.shared_browser_button.setEnabled(False)
                self.control_enabled.setChecked(False)
                self.control_enabled.setEnabled(False)
                self.connection_badge.setText("●  Хост доступен" if self._host_running else "●  Готов к подключению")
                self._status(f"Не удалось подключиться: {error}" if error else "Отключено. Клавиатура и мышь освобождены.")
        elif kind == "file" and epoch == self._viewer_epoch:
            self.send_file_button.setEnabled(self._viewer is not None and not self._disconnecting)
            self._status(f"Не удалось передать файл: {error}" if error else f"Файл передан: {owner.name}")
        elif kind == "audio-devices":
            self.audio_refresh.setEnabled(self._host is None)
            if error:
                self._status(f"Не удалось найти источники звука: {error}")
            else:
                self.host_audio_device.clear()
                for device in owner:
                    self.host_audio_device.addItem(device.get("name", "Источник"), str(device.get("device", device.get("index", ""))))
                self._status(f"Найдено источников звука: {len(owner)}. Выберите loopback-источник для системного звука.")
        elif kind == "clipboard":
            self._clipboard_busy = False
            if error:
                self._status(f"Clipboard synchronization failed: {error}")
            if self._clipboard_pending is not None:
                self._clipboard_timer.start()

    def _receive_frame(self, epoch: int, index: int, rgb: bytes, width: int, height: int) -> None:
        if (self._closing or epoch != self._viewer_epoch or not isinstance(index, int)
                or index < 0 or index > 63 or width <= 0 or height <= 0
                or width > 16384 or height > 16384 or width * height > 67108864
                or len(rgb) != width * height * 3):
            return
        with self._frames_lock:
            # At most one pending frame per selected source, never a Qt event per frame.
            if index in self._frames or len(self._frames) < 2:
                self._frames[index] = (rgb, width, height)

    def _poll_frames(self) -> None:
        if self._closing:
            return
        if (self._viewer is not None and not self._connecting and not self._disconnecting
                and not self._viewer.connected):
            self._status("Соединение потеряно. Возвращаем управление.")
            self._disconnect_viewer()
            return
        with self._frames_lock:
            frames, self._frames = self._frames, {}
        if self._viewer is None or self._disconnecting:
            return
        for index, (rgb, width, height) in frames.items():
            if index in self._suppressed_displays:
                continue
            self._video_window(index).canvas.set_frame(rgb, width, height)

    def _video_window(self, index: int) -> VideoWindow:
        window = self.video_windows.get(index)
        if window is None:
            window = VideoWindow(index, self._send_input, self.emergency_release,
                                 self.control_enabled.setChecked, self.control_enabled.isChecked())
            window.setStyleSheet(_STYLE)
            window.closed.connect(self._video_closed)
            window.media_error.connect(self._media_failed)
            self.video_windows[index] = window
            window.move(self.x() + 40 + len(self.video_windows) * 35,
                        self.y() + 40 + len(self.video_windows) * 35)
            window.show()
        return window

    def _media_failed(self, message: str) -> None:
        self._status(message)
        self._disconnect_viewer()

    def _video_closed(self, index: int) -> None:
        window = self.video_windows.pop(index, None)
        if window is not None and window._player is not None and not window._session_closing:
            self._hidden_video_windows[index] = window
        self._suppressed_displays.add(index)
        if not self.video_windows and not self._closing and not self._disconnecting and self._viewer:
            self._disconnect_viewer()

    def _close_video_windows(self) -> None:
        windows = list(self.video_windows.values()) + list(self._hidden_video_windows.values())
        self.video_windows.clear()
        self._hidden_video_windows.clear()
        with self._frames_lock:
            self._frames.clear()
        for window in windows:
            window.force_close()

    def _quiesce_video_windows(self) -> None:
        # A Qt player may wait for its HTTP demuxer when stop() is called.
        # Keep its reader alive until the background controller has stopped
        # the AV relay, then close the Qt objects on their owning GUI thread.
        for window in list(self.video_windows.values()) + list(self._hidden_video_windows.values()):
            window.canvas.set_input_enabled(False)
            window.setEnabled(False)
            window._closing_media = True
            if window._audio_output is not None:
                window._audio_output.setMuted(True)

    def _send_input(self, event: dict) -> None:
        release = event.get("type") == "release_all" or (
            event.get("type") in ("key", "button") and event.get("down") is False)
        if self._pump and (release or self.control_enabled.isChecked()):
            self._pump.submit(event)

    def _control_toggled(self, enabled: bool) -> None:
        if not enabled:
            self._send_input({"type": "release_all"})
        for window in list(self.video_windows.values()):
            window.set_input_enabled(enabled)

    def emergency_release(self) -> None:
        self._send_input({"type": "release_all"})
        self.control_enabled.setChecked(False)
        for window in list(self.video_windows.values()):
            window.canvas.release_pressed()

    def _input_failed(self, message: str) -> None:
        self._status(message)
        self._disconnect_viewer()

    def _clipboard_toggled(self, enabled: bool) -> None:
        self._clipboard_pending = None
        self._last_clipboard_hash = hashlib.sha256(QApplication.clipboard().text().encode("utf-8")).hexdigest()
        self._status("Новые изменения текста в буфере обмена будут передаваться партнёру." if enabled
                     else "Общий буфер обмена выключен.")

    def _local_clipboard(self) -> None:
        if self._closing or not self.clipboard_enabled.isChecked():
            return
        text = QApplication.clipboard().text()
        encoded = text.encode("utf-8")
        if len(encoded) > 65536:
            self._status("Текст в буфере обмена больше 64 КиБ; передача пропущена.")
            return
        digest = hashlib.sha256(encoded).hexdigest()
        if digest == self._last_clipboard_hash:
            return
        self._last_clipboard_hash = digest
        self._clipboard_pending = text
        self._clipboard_timer.start()

    def _flush_clipboard(self) -> None:
        if self._closing or not self.clipboard_enabled.isChecked():
            return
        if self._clipboard_busy:
            self._clipboard_timer.start()
            return
        text, self._clipboard_pending = self._clipboard_pending, None
        if text is None:
            return
        host, viewer = self._host if self._host_running else None, self._viewer
        viewer = viewer if not self._connecting and not self._disconnecting else None
        if host is None and viewer is None:
            return
        self._clipboard_busy = True

        def send():
            if host:
                host.broadcast_clipboard(text)
            if viewer:
                viewer.send_clipboard(text)
        self._task("clipboard", self._viewer_epoch, None, send)

    def _remote_clipboard(self, text: str) -> None:
        if self._closing or not self.clipboard_enabled.isChecked() or len(text.encode("utf-8")) > 65536:
            return
        self._clipboard_pending = None
        self._last_clipboard_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        QApplication.clipboard().setText(text)

    def _choose_file(self) -> None:
        if self._viewer is None or self._disconnecting:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Передать файл на хост")
        if not filename:
            return
        path = Path(filename)
        owner = self._viewer
        self.send_file_button.setEnabled(False)
        self._status(f"Передаём {path.name}…")
        self._task("file", self._viewer_epoch, path, lambda: owner.send_file(path))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_complete:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._save_preferences()
        self.emergency_release()
        self._closing = True
        self.setEnabled(False)
        self.status_label.setText("Завершаем захват, воспроизведение и соединения…")
        self._frame_timer.stop()
        self._stats_timer.stop()
        self._clipboard_timer.stop()
        self._quiesce_video_windows()
        if self._pump:
            self._pump.stop()
        host, viewer = self._host, self._viewer

        def shutdown():
            # Wait for pending startup before teardown, so it cannot spawn a
            # capture child after the controller was stopped.
            for thread in list(self._threads):
                thread.join(timeout=8)
            for owner, operation in ((viewer, "disconnect"), (host, "stop")):
                if owner:
                    try:
                        lock = self._lifecycle_locks.get(id(owner))
                        if lock is None:
                            getattr(owner, operation)()
                        else:
                            with lock:
                                getattr(owner, operation)()
                    except Exception:
                        pass
            _emit(self._bridge.shutdown_done)
        threading.Thread(target=shutdown, name="kvm-gui-shutdown", daemon=True).start()

    def _shutdown_finished(self) -> None:
        self._close_video_windows()
        self._shutdown_complete = True
        self.close()


def run_gui() -> int:
    if QApplication.instance() is None and sys.platform == "darwin":
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_MacDontSwapCtrlAndMeta, True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("VirtualKVM")
    app.setOrganizationName("VirtualKVM")
    window = MainWindow()
    window.show()
    return app.exec()
