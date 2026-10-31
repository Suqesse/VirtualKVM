"""Desktop controls and bounded, focused video/input presentation for VirtualKVM.

Controllers are imported only when a user starts a host or connects, allowing
the GUI to be inspected without starting network or capture services.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
import socket
from pathlib import Path
import re
import secrets
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
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QFrame, QScrollArea, QSlider, QSpinBox, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget,
)


from .display_gui import DisplayMap, MonitorPanel, local_screens
from .display_layout import MAX_STREAMS, Placement, arrange, display_inventory, fit_windows
from .discovery import DiscoveryService, TrustStore, device_identifier
from .lan_gui import AccessApprovalDialog, CodeConfirmationDialog, ComputerPicker
from .pairing import AccessRequest


class _Bridge(QObject):
    status = Signal(str)
    clipboard = Signal(str)
    finished = Signal(str, int, object, object)
    input_failed = Signal(str)
    shutdown_done = Signal()
    media = Signal(int, int, str, int, int)
    devices = Signal(object)
    access_request = Signal(object)
    pairing_code = Signal(str)
    verify_pairing = Signal(object)


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
QMainWindow, QWidget#workspace, QDialog { background: #f5f6f8; color: #252c36; }
QWidget { font-size: 13px; color: #252c36; }
QLabel#brandTitle { font-size: 23px; font-weight: 600; }
QLabel#brandMark { background: #66778e; color: #ffffff; border-radius: 8px; font-size: 15px; font-weight: 600; }
QLabel#muted { color: #697380; font-size: 12px; }
QLabel#validationError { color: #9a4145; font-size: 12px; }
QLabel#connectionBadge { background: #e9edf2; color: #526174; border: 1px solid #dce1e8; border-radius: 7px; padding: 7px 12px; }
QTabWidget::pane { background: #f5f6f8; border: 1px solid #dce1e8; border-radius: 10px; top: -1px; }
QTabBar::tab { background: #edf0f3; color: #697380; padding: 11px 22px; border: 1px solid #dce1e8; border-bottom: 0; border-top-left-radius: 7px; border-top-right-radius: 7px; margin-right: 4px; }
QTabBar::tab:selected { background: #ffffff; color: #303b4c; }
QGroupBox { background: #ffffff; border: 1px solid #dce1e8; border-radius: 9px; margin-top: 17px; padding: 20px 15px 13px; }
QGroupBox::title { color: #404c5d; subcontrol-origin: margin; subcontrol-position: top left; left: 14px; padding: 0 5px; font-weight: 600; }
QLineEdit, QSpinBox, QComboBox { min-height: 32px; background: #ffffff; border: 1px solid #cbd1da; border-radius: 6px; padding: 0 8px; selection-background-color: #d6dfec; selection-color: #252c36; }
QAbstractSpinBox QLineEdit { min-height: 0; border: none; border-radius: 0; background: transparent; padding: 0 8px; }
QSpinBox::up-button, QSpinBox::down-button { width: 0; height: 0; border: none; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #667e9d; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #89919b; border-color: #e1e4e9; background: #f2f4f6; }
QComboBox::drop-down { width: 21px; border: 0; }
QComboBox QAbstractItemView { background: #ffffff; color: #252c36; selection-background-color: #e4eaf2; }
QPushButton { min-height: 33px; padding: 0 13px; border: 1px solid #cbd1da; border-radius: 6px; background: #ffffff; font-weight: 500; }
QPushButton:hover { background: #eef1f5; border-color: #aab5c4; }
QPushButton:pressed { background: #e1e6ee; }
QPushButton:disabled { color: #929aa5; background: #f1f3f6; border-color: #e0e4ea; }
QPushButton#primaryButton { background: #586e8b; color: #ffffff; border-color: #586e8b; min-width: 138px; font-weight: 600; }
QPushButton#primaryButton:hover { background: #485e7b; }
QPushButton#primaryButton:disabled { background: #c4ccd7; color: #ffffff; border-color: #c4ccd7; }
QPushButton#dangerButton { color: #8c454a; border-color: #dfc9cb; background: #fcf7f7; }
QCheckBox { spacing: 8px; padding: 4px 0; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #a6b0bf; border-radius: 4px; background: #ffffff; }
QCheckBox::indicator:checked { background: #697f9d; border-color: #697f9d; image: url("__CHECK_ICON__"); }
QCheckBox:disabled { color: #89919b; }
QPlainTextEdit { background: #ffffff; border: 1px solid #dce1e8; border-radius: 6px; padding: 8px; font-family: monospace; font-size: 11px; }
QToolButton { background: transparent; border: none; padding: 5px; color: #58697e; }
QToolBar { background: #f3f5f8; border: none; spacing: 8px; padding: 7px; }
QStatusBar { background: #f3f5f8; color: #697380; }
QTableWidget, QTreeWidget, QListWidget { background: #ffffff; alternate-background-color: #f7f8fa; border: 1px solid #dce1e8; gridline-color: #e6e9ed; }
QListWidget::item { padding: 7px; border-radius: 4px; margin: 2px; }
QListWidget::item:selected { background: #e1e7ef; color: #253349; }
QHeaderView::section { background: #eef1f5; color: #536073; padding: 7px; border: none; }
QScrollArea { background: #f5f6f8; }
QScrollBar:vertical { width: 10px; background: #f1f3f6; }
QScrollBar::handle:vertical { background: #c0c8d3; border-radius: 4px; min-height: 25px; }
""".replace("__CHECK_ICON__", (Path(__file__).parent / "assets/check.svg").as_posix())


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
        self.setMinimumSize(160, 90)

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
        self._keep_media_alive = False
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
            # Empty source means stop AND unload. stop() alone retains a live
            # HTTP media engine, which can retry reads after relay shutdown.
            # PySide explicitly releases the GIL for setSource as well.
            self._player.setSource(QUrl())
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
        if (self._player is not None or self._keep_media_alive) and not self._session_closing:
            # Keep reading the AV relay if the other monitor remains open.
            # Closing the HTTP reader prematurely breaks the host media stream.
            self.canvas.release_pressed()
            if self._audio_output is not None:
                self._audio_output.setMuted(True)
            self.hide()
            self.closed.emit(self.display)
            event.ignore()
            return
        self._closing_media = True
        if self._player:
            self._player.setSource(QUrl())
        self.canvas.release_pressed()
        self.closed.emit(self.display)
        super().closeEvent(event)

    def force_close(self) -> None:
        self._session_closing = True
        self.close()


class MainWindow(QMainWindow):
    def __init__(self, state_dir: Path | None = None, *, networking=False):
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
        self._bridge.devices.connect(self._show_computers)
        self._bridge.access_request.connect(self._approve_access)
        self._bridge.pairing_code.connect(lambda code: self.lan_panel.show_code(code))
        self._bridge.verify_pairing.connect(self._verify_pairing)
        self._networking = networking
        self._discovery = None
        self._trust_store = TrustStore(self.state_dir)
        self._quick_device = None
        self._quick_connecting = False
        self._approval_dialogs = []
        self._awaiting_video_at = None
        self._disconnect_reason = None
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
        self._scanning_displays = False
        self._known_peer = None
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
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        heading = QHBoxLayout()
        mark = QLabel("VK")
        mark.setObjectName("brandMark")
        mark.setFixedSize(38, 38)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel("VirtualKVM")
        title.setObjectName("brandTitle")
        brand.addWidget(title)
        heading.addLayout(brand)
        heading.addStretch()
        self.connection_badge = QLabel("●  Готов к подключению")
        self.connection_badge.setObjectName("connectionBadge")
        self.connection_badge.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.connection_badge)
        self.incoming_stop = QPushButton("Прекратить сеанс")
        self.incoming_stop.setObjectName("dangerButton")
        self.incoming_stop.clicked.connect(self._release_all_control)
        self.incoming_stop.hide()
        heading.addWidget(self.incoming_stop)
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
        release.clicked.connect(self._release_all_control)
        shared.addWidget(release)
        layout.addLayout(shared)
        self.status_label = QLabel("Выберите компьютер в списке. При первом подключении сверьте короткий код на двух экранах.")
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
        emergency.triggered.connect(self._release_all_control)
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
        self.monitor_layout.changed.connect(self._save_preferences)
        QTimer.singleShot(0, self._refresh_host_displays)
        if networking:
            QTimer.singleShot(100, self._start_host)

    @staticmethod
    def _spin(low: int, high: int, value: int, step: int = 1) -> QSpinBox:
        widget = QSpinBox()
        widget.lineEdit().setTextMargins(8, 0, 8, 0)
        widget.setToolTip("Введите значение или используйте клавиши ↑ и ↓.")
        widget.setRange(low, high)
        widget.setSingleStep(step)
        widget.setValue(value)
        return widget

    def _host_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(22, 24, 22, 22)
        columns = QHBoxLayout()
        columns.setSpacing(22)
        access = QGroupBox("Подключение")
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
        # Enumerate local interfaces directly. Resolving gethostname() through
        # DNS can block for minutes on unregistered native CI / LAN hostnames.
        addresses = {address.toString() for address in QNetworkInterface.allAddresses()
                     if address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol
                     and not address.isLoopback() and not address.isNull()
                     and address.toString() != "0.0.0.0"}
        lan = QLabel("Адрес в локальной сети: " + (", ".join(sorted(addresses)) or "посмотрите в настройках сети"))
        lan.setWordWrap(True)
        lan.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(lan)
        form.addRow("Интерфейс", self.host_bind)
        form.addRow("Порт", self.host_port)
        form.addRow("PIN", self.host_pin)
        self.host_pin_hint = QLabel()
        self.host_pin_hint.setWordWrap(True)
        self.host_pin_hint.setObjectName("validationError")
        form.addRow(self.host_pin_hint)
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
        extras = QGroupBox("Звук и файлы")
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
        self.host_pin.textChanged.connect(self._validate_host_pin)
        buttons.addWidget(self.host_start)
        buttons.addWidget(self.host_stop)
        self.host_disconnect = QPushButton("Прекратить сеанс")
        self.host_disconnect.setEnabled(False)
        self.host_disconnect.clicked.connect(lambda: self._host.server.disconnect_viewer() if self._host else None)
        buttons.addWidget(self.host_disconnect)
        self.host_session_label = QLabel("Нет активного подключения")
        self.host_session_label.setTextFormat(Qt.TextFormat.PlainText)
        self.host_session_label.setWordWrap(True)
        layout.addWidget(self.host_session_label)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.display_label = QLabel("Можно передавать от 1 до 8 экранов, которые доступны в ОС.")
        self.display_label.setWordWrap(True)
        layout.addWidget(self.display_label)
        self.host_map = DisplayMap()
        self.host_map.setFixedHeight(160)
        layout.addWidget(self.host_map)
        refresh_displays = QPushButton("Обновить список экранов")
        refresh_displays.clicked.connect(self._refresh_host_displays)
        self.host_display_refresh = refresh_displays
        layout.addWidget(refresh_displays)
        screen_help = QLabel("Расположение и поворот экранов источника задаются в системных настройках Windows или macOS. "
                             "После изменения конфигурации перезапустите хост.")
        screen_help.setWordWrap(True)
        screen_help.setObjectName("muted")
        layout.addWidget(screen_help)
        note = QLabel("Новый клиент подтверждается коротким кодом сеанса. Полученные отдельные файлы: Downloads/VirtualKVM.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        return widget

    def _viewer_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(22, 24, 22, 22)
        columns = QHBoxLayout()
        columns.setSpacing(22)
        connection = QGroupBox("Ручное подключение")
        form = QFormLayout(connection)
        form.setSpacing(14)
        self.peer_host = QLineEdit()
        self.peer_host.setPlaceholderText("192.168.1.10")
        self.peer_port = self._spin(1024, 65535, 24800)
        self.peer_pin = QLineEdit()
        self.peer_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.peer_pin.setPlaceholderText("Только для старого режима подключения")
        self.peer_fingerprint = QLineEdit()
        self.peer_fingerprint.setPlaceholderText("Необязательно: пустое поле — подтверждение кодом на хосте")
        form.addRow("Адрес хоста", self.peer_host)
        form.addRow("Порт", self.peer_port)
        form.addRow("PIN", self.peer_pin)
        form.addRow("Ключ старой версии", self.peer_fingerprint)
        self.audio_enabled = QCheckBox("Передавать звук")
        audio_note = QLabel("Звук синхронизирован с видео первой строки схемы экранов.")
        audio_note.setWordWrap(True)
        audio_note.setObjectName("muted")
        computers = QGroupBox("Компьютеры в сети")
        computer_layout = QVBoxLayout(computers)
        self.lan_panel = ComputerPicker()
        self.lan_panel.connect_requested.connect(self._connect_quick)
        self.lan_panel.refresh_requested.connect(self._refresh_computers)
        self.lan_panel.visibility_changed.connect(self._lan_visibility_changed)
        computer_layout.addWidget(self.lan_panel)
        columns.addWidget(computers, 1)
        quality = QGroupBox("Изображение")
        quality_form = QFormLayout(quality)
        quality_form.setSpacing(12)
        self.profile = QComboBox()
        self.profile.addItems(["Игры · 240 FPS", "Работа · 60 FPS", "Качество · 4K / 60 FPS", "Свой профиль"])
        self.native_resolution = QCheckBox("Родное разрешение каждого экрана")
        self.native_resolution.toggled.connect(self._resolution_mode)
        self.preserve_aspect = QCheckBox("Сохранять пропорции экранов")
        self.preserve_aspect.setChecked(True)
        self.preserve_aspect.setToolTip("Горизонтальные и вертикальные экраны вписываются в выбранное разрешение без растяжения.")
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
        quality_form.addRow(self.preserve_aspect)
        quality_form.addRow("Разрешение", self.resolution_preset)
        quality_form.addRow("Свой размер", resolution)
        quality_form.addRow("Частота · FPS", self.frame_rate)
        quality_form.addRow(self.audio_enabled)
        quality_form.addRow(audio_note)
        advanced = QGroupBox("Параметры кодирования")
        advanced_form = QFormLayout(advanced)
        advanced_form.addRow("Кодек", self.codec)
        advanced_form.addRow("Видеокодер", self.encoder)
        advanced_form.addRow("Мбит/с на экран", self.bitrate)
        advanced_form.addRow("Буфер · мс", self.latency)
        self.video_transport = QComboBox()
        self.video_transport.addItem("Защищённый TCP · без входящих портов на клиенте", "tcp")
        self.video_transport.addItem("UDP / SRT · требуется доступ в брандмауэре", "srt")
        advanced_form.addRow("Передача видео", self.video_transport)
        advanced.setVisible(False)
        advanced_toggle = QToolButton()
        advanced_toggle.setText("Дополнительные параметры")
        advanced_toggle.setCheckable(True)
        advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        advanced_toggle.toggled.connect(lambda checked: (advanced.setVisible(checked), advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)))
        quality_form.addRow(advanced_toggle)
        quality_form.addRow(advanced)
        self.profile.currentIndexChanged.connect(self._apply_profile)
        self.resolution_preset.currentIndexChanged.connect(self._apply_resolution)
        self.resolution_preset.setCurrentIndex(self._resolution_index(1920, 1080))
        columns.addWidget(quality, 1)
        layout.addLayout(columns, 1)
        self.manual_toggle = QToolButton()
        self.manual_toggle.setText("Подключиться по адресу…")
        self.manual_toggle.setCheckable(True)
        self.manual_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.manual_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.manual_toggle.toggled.connect(connection.setVisible)
        connection.hide()
        layout.addWidget(self.manual_toggle)
        layout.addWidget(connection)
        monitors = QGroupBox("Экраны")
        monitor_form = QVBoxLayout(monitors)
        self.monitor_layout = MonitorPanel()
        self.monitor_layout.discover_requested.connect(self._discover_displays)
        monitor_form.addWidget(self.monitor_layout)
        layout.addWidget(monitors)
        target = QLabel("Фактический FPS зависит от захвата, кодера, сети и частоты обновления экрана.")
        target.setWordWrap(True)
        target.setObjectName("muted")
        layout.addWidget(target)
        buttons = QHBoxLayout()
        self.connect_button = QPushButton("Подключиться")
        self.connect_button.setObjectName("primaryButton")
        self.disconnect_button = QPushButton("Отключиться")
        self.disconnect_button.setEnabled(False)
        self.connect_button.clicked.connect(lambda: self._connect_viewer())
        self.connect_button.hide()
        self.manual_toggle.toggled.connect(self.connect_button.setVisible)
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
            self.monitor_layout, self.video_width,
            self.video_height, self.frame_rate, self.bitrate, self.latency, self.encoder,
            self.profile, self.native_resolution, self.preserve_aspect, self.resolution_preset, self.audio_enabled, self.codec,
            self.lan_panel, self.manual_toggle, self.video_transport)
        for field in (self.frame_rate, self.bitrate, self.latency):
            field.valueChanged.connect(self._custom_profile)
        self.video_width.valueChanged.connect(self._dimensions_changed)
        self.video_height.valueChanged.connect(self._dimensions_changed)
        self.encoder.currentIndexChanged.connect(self._custom_profile)
        self.peer_host.textChanged.connect(self._peer_changed)
        self.peer_port.valueChanged.connect(self._peer_changed)
        self.peer_fingerprint.textChanged.connect(self._peer_changed)
        return widget

    def _peer_signature(self):
        return (self.peer_host.text().strip(), self.peer_port.value(),
                re.sub(r"[:\s]", "", self.peer_fingerprint.text()).lower(), self.peer_pin.text())

    def _show_computers(self, devices):
        if not self._closing:
            self.lan_panel.set_devices(devices, self._trust_store)

    def _start_discovery(self):
        if not self._networking or self._host is None or not self._host_running:
            return
        port = self._host.server.address[1] if self.lan_panel.visible_on_lan.isChecked() else 0
        if self._discovery is not None:
            service = self._discovery
            self._task("discovery-port", 0, service, lambda: service.set_port(port))
            return
        addresses = [address.toString() for address in QNetworkInterface.allAddresses()
                     if address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol
                     and not address.isLoopback() and not address.isNull()]
        try:
            service = DiscoveryService(device_identifier(self.state_dir), socket.gethostname(), addresses or ["127.0.0.1"],
                port, self._host.fingerprint,
                lambda devices: _emit(self._bridge.devices, devices))
            self._discovery = service
            self.lan_panel.refresh.setEnabled(False)
            self._task("discovery-start", 0, service, service.start)
        except Exception as error:
            self._status(f"Поиск компьютеров недоступен: {error}. Можно подключиться по адресу.")

    def _refresh_computers(self):
        if self._discovery:
            self.lan_panel.refresh.setEnabled(False)
            self._task("discovery-refresh", 0, self._discovery, self._discovery.refresh)
        elif self._networking:
            self._start_host() if self._host is None else self._start_discovery()
        else:
            self._status("Поиск включается при обычном запуске приложения.")

    def _lan_visibility_changed(self, enabled):
        if self._closing:
            return
        if self._host:
            self._host.server.on_access_request = (lambda request: _emit(self._bridge.access_request, request)) if enabled else None
        if enabled:
            self._networking = True
            self._refresh_computers()
        else:
            for dialog in self._approval_dialogs:
                dialog.reject()
            service = self._discovery
            if service:
                self._task("discovery-port", 0, service, lambda: service.set_port(0))

    def _approve_access(self, request):
        if self._closing or not self.lan_panel.visible_on_lan.isChecked():
            request.resolve()
            return
        dialog = AccessApprovalDialog(request, self)
        self._approval_dialogs.append(dialog)
        dialog.finished.connect(lambda _: self._approval_dialogs.remove(dialog) if dialog in self._approval_dialogs else None)
        dialog.accepted.connect(lambda: self.clipboard_enabled.setChecked(True) if dialog.clipboard.isChecked() else None)
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _connect_quick(self, device):
        if self._viewer is not None or self._closing:
            return
        if self._trust_store.state(device) == "changed":
            choice = QMessageBox.warning(self, "Ключ компьютера изменился",
                "У этого компьютера изменился ключ. Это бывает после переустановки, но может означать подмену. "
                "Продолжайте только после сверки нового кода с владельцем.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return
        self._connect_viewer(device)

    def _verify_pairing(self, request):
        if self._closing or self._disconnecting:
            request.resolve()
            return
        dialog = CodeConfirmationDialog(request, self)
        self._approval_dialogs.append(dialog)
        dialog.finished.connect(lambda _: self._approval_dialogs.remove(dialog) if dialog in self._approval_dialogs else None)
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _peer_changed(self, *args):
        self._known_peer = None
        self.monitor_layout.inventory = []
        self.monitor_layout._refresh()

    def _discover_displays(self):
        if not self.manual_toggle.isChecked() and self.lan_panel.selected() is not None:
            self._connect_quick(self.lan_panel.selected())
            return
        if self._scanning_displays or self._viewer is not None or self._closing:
            return
        host, port, fingerprint, pin = self._peer_signature()
        if not host or any(c.isspace() for c in host) or not pin:
            self._status("Для получения экранов введите адрес хоста и PIN.")
            return
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            self._status("Вставьте и сверьте полный SHA-256 отпечаток хоста.")
            return
        from .app import ViewerController
        self._scanning_displays = True
        self.monitor_layout.discover.setEnabled(False)
        self.connect_button.setEnabled(False)
        result = {"peer": self._peer_signature()}
        self._status("Получаем список экранов по защищённому соединению…")
        def discover():
            result["displays"] = ViewerController.discover_displays(self.state_dir, host, port, pin, fingerprint)
        self._task("display-discovery", self._viewer_epoch, result, discover)

    def _show_host_displays(self, values):
        inventory = display_inventory(values)
        entries = [Placement(d.index, d.x, d.y, d.width, d.height) for d in inventory]
        self.host_map.set_entries(entries)
        self.display_label.setText(f"Экранов в ОС: {len(entries)}. Номера на схеме используются в VirtualKVM.")

    def _refresh_host_displays(self):
        if self._host is not None or self._closing:
            return
        synthetic = self.synthetic.isChecked()
        result = []
        self.host_display_refresh.setEnabled(False)
        def detect():
            if synthetic:
                result.extend(dict(index=i, name=f"Тестовый экран {i + 1}", x=i * 1920,
                                   y=0, width=1920, height=1080) for i in range(2))
            else:
                from dataclasses import asdict
                from .native import list_displays
                result.extend(asdict(display) for display in list_displays())
        self._task("local-display-inventory", 0, result, detect)

    def _window_placement(self, index):
        screens = local_screens()
        primary = QApplication.primaryScreen()
        area = primary.availableGeometry() if primary else None
        fallback = (area.x(), area.y(), area.width(), area.height()) if area else (0, 0, 1280, 720)
        entry = next((e for e in self.monitor_layout.entries if e.source == index), Placement(index))
        if self.monitor_layout.presentation == "monitors":
            target = next((screen for key, _, screen in screens if key == entry.target), primary)
            if target:
                rectangle = target.geometry() if self.monitor_layout.fullscreen else target.availableGeometry()
                return ((rectangle.x(), rectangle.y(), rectangle.width(), rectangle.height()),
                        target, self.monitor_layout.fullscreen)
        rectangles = fit_windows(self.monitor_layout.entries, fallback)
        return rectangles.get(index, fit_windows([entry], fallback)[index]), primary, False

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
        self.preserve_aspect.setEnabled(enabled)
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
        self.incoming_stop.setVisible(bool(self._host and self._host.server.sessions))
        if self._host is not None:
            sessions = self._host.server.sessions
            self.host_disconnect.setEnabled(bool(sessions))
            if sessions:
                session = sessions[0]
                mode = "управление разрешено" if session["permissions"]["control"] else "только просмотр"
                self.host_session_label.setText(f"Подключён: {session['name'] or session['host']} · {mode}")
                if self._viewer is None:
                    self.connection_badge.setText(f"●  Подключён: {(session['name'] or session['host'])[:40]}")
            else:
                self.host_session_label.setText("Нет активного подключения")
                if self._viewer is None and self._host_running:
                    self.connection_badge.setText("●  Хост доступен")
        if self._awaiting_video_at is not None and self._viewer is not None and not self._disconnecting:
            ready = all(index in self.video_windows and
                        (self.video_windows[index].av_frames or self.video_windows[index].canvas.painted_frames)
                        for index in self._requested_displays if index not in self._suppressed_displays)
            if ready:
                self._awaiting_video_at = None
                self.connection_badge.setText("●  Изображение получено")
                self._status("Экран другого компьютера открыт. Ctrl+Alt+Shift+Escape — вернуть управление.")
            elif time.monotonic() - self._awaiting_video_at >= 20:
                self._awaiting_video_at = None
                reason = ("Управление подключилось, но изображение не пришло за 20 секунд. "
                          "Проверьте разрешение записи экрана на хосте и выбранный кодер. "
                          "Для UDP/SRT проверьте брандмауэр или выберите защищённый TCP.")
                self._status(reason)
                self._disconnect_viewer(reason)
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
        if (self._closing or epoch != self._viewer_epoch or self._disconnecting or
                index in self._suppressed_displays and index not in self._hidden_video_windows):
            return
        try:
            window = self._video_window(index)
            window.start_media(url, width, height)
            if index in self._hidden_video_windows:
                window._audio_output.setMuted(True)
        except Exception as error:
            self._status(f"Не удалось запустить звук и видео: {error}")
            self._disconnect_viewer()

    def _preference_fields(self) -> dict:
        return {"host_bind": self.host_bind, "host_port": self.host_port,
            "peer_host": self.peer_host, "peer_port": self.peer_port,
            "width": self.video_width, "height": self.video_height,
            "fps": self.frame_rate, "bitrate": self.bitrate, "latency": self.latency,
            "shared_path": self.shared_path}

    def _save_preferences(self) -> None:
        data = {name: field.value() if isinstance(field, QSpinBox) else field.text()
                for name, field in self._preference_fields().items()}
        data.update(profile=self.profile.currentIndex(), encoder=self.encoder.currentText(),
                    codec=self.codec.currentData(),
                    native_resolution=self.native_resolution.isChecked(),
                    preserve_aspect=self.preserve_aspect.isChecked(),
                    audio=self.audio_enabled.isChecked(), audio_device=self._audio_device_value(),
                    video_transport=self.video_transport.currentData(),
                    lan_visible=self.lan_panel.visible_on_lan.isChecked(),
                    monitor_layout=self.monitor_layout.encode())
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
        if type(data.get("lan_visible")) is bool:
            self.lan_panel.visible_on_lan.setChecked(data["lan_visible"])
        self.video_transport.setCurrentIndex(max(0, self.video_transport.findData(data.get("video_transport", "tcp"))))
        for name, field in self._preference_fields().items():
            value = data.get(name)
            if isinstance(field, QSpinBox) and type(value) is int:
                field.setValue(value)
            elif isinstance(field, QLineEdit) and isinstance(value, str):
                field.setText(value[:4096])
        try:
            if "monitor_layout" in data:
                self.monitor_layout.restore(data["monitor_layout"])
            else:
                # Migrate the original one/two-screen preference format.
                first = data.get("first_display", 0)
                second = data.get("second_display", 1)
                entries = [Placement(first)]
                if data.get("second_display_enabled") is True:
                    entries.append(Placement(second, x=2000))
                self.monitor_layout.set_entries(entries)
        except (TypeError, ValueError):
            pass
        for name, field in (("native_resolution", self.native_resolution), ("audio", self.audio_enabled),
                            ("preserve_aspect", self.preserve_aspect)):
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
        message = {"Host listening": "Хост принимает подключения", "Host stopped": "Хост остановлен",
                   "Viewer connected": "К хосту подключился клиент", "Viewer disconnected": "Клиент отключился",
                   "Connected": "Защищённое подключение установлено", "Disconnected": "Соединение закрыто",
                   "Video streams started": "Захват экранов запущен"}.get(message, message)
        if self._viewer is not None and message.startswith(("Video failed:", "Control connection failed:",
                                                           "Защищённый видеопоток прерван:")):
            self._disconnect_reason = message
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
                shared_writable=self.shared_writable.isChecked() if self.share_enabled.isChecked() else False,
                allow_port_fallback=True)
            if self.lan_panel.visible_on_lan.isChecked():
                self._host.server.on_access_request = lambda request: _emit(self._bridge.access_request, request)
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
                      self.shared_writable, self.host_display_refresh):
            field.setEnabled(enabled)

    def _validate_host_pin(self, *args):
        valid = len(self.host_pin.text()) >= 8
        self.host_pin_hint.setText("" if valid else "Минимум 8 символов. Кнопка «Новый PIN» создаёт подходящий код.")
        if self._host is None and not self._closing:
            self.host_start.setEnabled(valid)

    def _stop_host(self) -> None:
        if self._host is None:
            return
        self.host_stop.setEnabled(False)
        self._host_epoch += 1
        self._task("host-stop", self._host_epoch, self._host, self._host.stop)

    def _connect_viewer(self, device=None) -> None:
        if self._viewer is not None or self._closing or self._scanning_displays:
            return
        quick = device is not None or not self.peer_fingerprint.text().strip()
        fingerprint = device.fingerprint if device is not None else re.sub(r"[:\s]", "", self.peer_fingerprint.text()).lower()
        if device is None and quick:
            fingerprint = None
        if fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            self._status("Вставьте и сверьте полный SHA-256 отпечаток: 64 шестнадцатеричных символа.")
            return
        host = device.address if device is not None else self.peer_host.text().strip()
        if not host or any(character.isspace() for character in host) or not quick and not self.peer_pin.text():
            self._status("Введите адрес второго компьютера и его PIN.")
            return
        try:
            self.monitor_layout.validate()
        except ValueError as error:
            self._status(str(error))
            return
        displays = [entry.source for entry in self.monitor_layout.entries]
        if quick:
            self.monitor_layout.inventory = []
        config = {"width": self.video_width.value(), "height": self.video_height.value(),
                  "fps": self.frame_rate.value(), "bitrate_mbps": self.bitrate.value(),
                  "codec": self.codec.currentData(), "encoder": self.encoder.currentText(),
                  "latency_ms": self.latency.value(), "source_resolution": self.native_resolution.isChecked(),
                  "preserve_aspect": self.preserve_aspect.isChecked(),
                  "audio": self.audio_enabled.isChecked(), "transport": self.video_transport.currentData()}
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
        self._disconnect_reason = None
        self._quick_device, self._quick_connecting = device, quick
        self.lan_panel.code.clear()
        self._set_viewer_fields(False)
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        controller = self._viewer
        # Read widget values before crossing the Qt thread boundary.
        port, pin = (device.port, None) if device is not None else (self.peer_port.value(), self.peer_pin.text())
        control = self.lan_panel.control.isChecked()
        approval_options = dict(approval=True, name=socket.gethostname()[:80], control=control,
            on_pairing_code=lambda code: _emit(self._bridge.pairing_code, code)) if quick else {}
        if quick and (device is None or self._trust_store.state(device) != "known"):
            target_name = device.name if device is not None else host
            def confirm(code):
                request = AccessRequest(target_name, code, False)
                _emit(self._bridge.verify_pairing, request)
                return request.wait(controller._cancelled) is not None
            approval_options["confirm_pairing"] = confirm
        self.connection_badge.setText("●  Подключаемся…")
        self._status("Проверяем отпечаток и устанавливаем безопасное соединение…")
        def connect():
            identity = fingerprint
            if identity is None:
                from .security import probe_fingerprint
                identity = probe_fingerprint(host, port)
            controller.connect(host, port, pin, identity, displays, config, **approval_options)
        self._task("viewer-connect", epoch, controller, connect)

    def _disconnect_viewer(self, reason=None) -> None:
        if self._viewer is None or self._disconnecting:
            return
        self._disconnecting = True
        if isinstance(reason, str):
            self._disconnect_reason = reason
        if self._connecting:
            self._viewer.cancel_connect()
        self._awaiting_video_at = None
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
                self.host_port.setValue(owner.server.address[1])
                self.host_fingerprint.setText(str(owner.fingerprint))
                self._show_host_displays(owner.displays)
                self.connection_badge.setText("●  Хост доступен")
                self._status("Компьютер готов к запросам. Выберите его имя в списке на другом ПК." if
                             self.lan_panel.visible_on_lan.isChecked() else
                             "Показ этого компьютера в сети выключен. Можно искать другие компьютеры или включить показ.")
                self._start_discovery()
            else:
                if kind == "host-start":
                    self._task("host-stale", -1, owner, owner.stop)
                self._host = None
                self._host_running = False
                self.host_start.setEnabled(True)
                self.host_stop.setEnabled(False)
                self._host_fields(True)
                if self._discovery:
                    service = self._discovery
                    self._task("discovery-port", 0, service, lambda: service.set_port(0))
                self.connection_badge.setText("●  Готов к подключению")
                self._status(f"Ошибка хоста: {error}" if error else "Хост остановлен.")
        elif kind.startswith("viewer"):
            if epoch != self._viewer_epoch:
                if kind == "viewer-connect":
                    self._task("viewer-stale", -1, owner, owner.disconnect)
                return
            if kind == "viewer-connect" and error is None:
                try:
                    self._requested_displays = owner.selected_displays or self._requested_displays
                    if self._quick_connecting:
                        entries = {entry.source: entry for entry in self.monitor_layout.entries}
                        self.monitor_layout.set_entries([entries.get(index, Placement(index)) for index in self._requested_displays])
                    self.monitor_layout.set_inventory(owner.displays)
                except ValueError as failure:
                    self._status(f"Некорректный список экранов: {failure}")
                    self._disconnect_viewer()
                    return
                # A media callback may open a window before negotiation finishes.
                # Apply final source dimensions even when discovery was skipped.
                for index, window in self.video_windows.items():
                    self._place_video_window(index, window)
                self._connecting = False
                self._pump = _InputPump(owner.send_event,
                    lambda text: _emit(self._bridge.input_failed, text))
                permissions = owner.client.permissions
                self.control_enabled.setEnabled(permissions.control)
                self.send_file_button.setEnabled(permissions.files)
                self.shared_browser_button.setEnabled(permissions.files and owner.shared_info.get("enabled", False))
                self.clipboard_enabled.setEnabled(permissions.clipboard)
                self.connection_badge.setText("●  Ожидаем изображение")
                if self._quick_device is not None:
                    self._trust_store.remember(self._quick_device)
                for index in self._requested_displays:
                    self._video_window(index)
                if self._quick_connecting and permissions.control and self.lan_panel.control.isChecked():
                    self.control_enabled.setChecked(True)
                self._awaiting_video_at = time.monotonic()
                self._status("Подключение подтверждено. Окна экранов открыты, ожидаем изображение…")
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
                self.clipboard_enabled.setEnabled(True)
                self._awaiting_video_at = None
                self.lan_panel.code.clear()
                self._quick_device = None
                self._quick_connecting = False
                self.connection_badge.setText("●  Хост доступен" if self._host_running else "●  Готов к подключению")
                self._status(f"Не удалось подключиться: {error}" if error else self._disconnect_reason or
                             "Отключено. Клавиатура и мышь освобождены.")
        elif kind.startswith("discovery-"):
            self.lan_panel.refresh.setEnabled(True)
            if kind == "discovery-start" and error is None and not self.lan_panel.visible_on_lan.isChecked():
                self._task("discovery-port", 0, owner, lambda: owner.set_port(0))
            if error:
                self._status(f"Поиск компьютеров недоступен: {error}. Используйте подключение по адресу.")
                if kind == "discovery-start" and self._discovery is owner:
                    self._discovery = None
        elif kind == "display-discovery":
            self._scanning_displays = False
            self.monitor_layout.discover.setEnabled(self._viewer is None)
            self.connect_button.setEnabled(self._viewer is None)
            if owner["peer"] != self._peer_signature():
                self._status("Данные подключения изменились. Получите список экранов заново.")
            elif error:
                self._status(f"Не удалось получить экраны: {error}")
            else:
                try:
                    self.monitor_layout.set_inventory(owner["displays"])
                    self._known_peer = owner["peer"][:3]
                    self._status(f"Доступно экранов: {len(self.monitor_layout.inventory)}. Выберите количество и расположение в настройке экранов.")
                except ValueError as failure:
                    self._status(str(failure))
        elif kind == "local-display-inventory":
            self.host_display_refresh.setEnabled(self._host is None)
            if error:
                self._status(f"Не удалось получить экраны этого компьютера: {error}")
            elif self._host is None:
                try:
                    self._show_host_displays(owner)
                except ValueError as failure:
                    self._status(str(failure))
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
        if (self._closing or epoch != self._viewer_epoch or type(index) is not int
                or index < 0 or index > 63 or width <= 0 or height <= 0
                or width > 16384 or height > 16384 or width * height > 67108864
                or len(rgb) != width * height * 3 or index not in self._requested_displays):
            return
        with self._frames_lock:
            # At most one pending frame per selected source, never a Qt event per frame.
            if index in self._frames or len(self._frames) < MAX_STREAMS:
                self._frames[index] = (rgb, width, height)

    def _poll_frames(self) -> None:
        if self._closing:
            return
        if (self._viewer is not None and not self._connecting and not self._disconnecting
                and not self._viewer.connected):
            reason = self._disconnect_reason or "Соединение потеряно. Возвращаем управление."
            self._status(reason)
            self._disconnect_viewer(reason)
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
        window = self.video_windows.get(index) or self._hidden_video_windows.get(index)
        if window is None:
            window = VideoWindow(index, self._send_input, self._release_all_control,
                                 self.control_enabled.setChecked, self.control_enabled.isChecked())
            window.setStyleSheet(_STYLE)
            window.closed.connect(self._video_closed)
            window.media_error.connect(self._media_failed)
            self.video_windows[index] = window
            window._keep_media_alive = self._viewer is not None and index in getattr(self._viewer, "audio_displays", set())
            self._place_video_window(index, window)
        return window

    def _place_video_window(self, index, window):
        rectangle, screen, fullscreen = self._window_placement(index)
        if screen is not None:
            window.winId()
            window.windowHandle().setScreen(screen)
        window.setGeometry(*rectangle)
        if fullscreen:
            window.showFullScreen()
        else:
            window.show()
        window.raise_()

    def _media_failed(self, message: str) -> None:
        self._status(message)
        self._disconnect_viewer(message)

    def _video_closed(self, index: int) -> None:
        window = self.video_windows.pop(index, None)
        if window is not None and (window._player is not None or window._keep_media_alive) and not window._session_closing:
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

    def _release_all_control(self):
        self.emergency_release()
        if self._host is not None and self._host.server.sessions:
            self._host.server.disconnect_viewer()

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
        for dialog in list(self._approval_dialogs):
            dialog.reject()
        if self._viewer is not None and self._connecting:
            self._viewer.cancel_connect()
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
        discovery, self._discovery = self._discovery, None

        def shutdown():
            # Wait for pending startup before teardown, so it cannot spawn a
            # capture child after the controller was stopped.
            for thread in list(self._threads):
                thread.join(timeout=8)
            if discovery:
                discovery.stop()
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
    window = MainWindow(networking=True)
    window.show()
    return app.exec()
