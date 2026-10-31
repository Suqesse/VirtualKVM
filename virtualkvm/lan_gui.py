"""Local computer picker and owner-approved, short-code pairing."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                              QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from .pairing import Permissions


class ComputerPicker(QWidget):
    connect_requested = Signal(object)
    refresh_requested = Signal()
    visibility_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMinimumHeight(115)
        self.list.setAccessibleName('Компьютеры с открытым VirtualKVM в локальной сети')
        self.list.itemDoubleClicked.connect(lambda _: self.connect_selected())
        self.list.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.list)
        actions = QHBoxLayout()
        self.connect_button = QPushButton('Подключиться')
        self.connect_button.setObjectName('primaryButton')
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self.connect_selected)
        actions.addWidget(self.connect_button)
        self.refresh = QPushButton('Обновить')
        self.refresh.clicked.connect(self.refresh_requested.emit)
        actions.addWidget(self.refresh)
        layout.addLayout(actions)
        self.control = QCheckBox('Клавиатура и мышь')
        self.control.setChecked(True)
        layout.addWidget(self.control)
        self.visible_on_lan = QCheckBox('Показывать этот компьютер в сети')
        self.visible_on_lan.setChecked(True)
        self.visible_on_lan.toggled.connect(self.visibility_changed.emit)
        layout.addWidget(self.visible_on_lan)
        self.hint = QLabel('Откройте VirtualKVM на другом компьютере той же сети. Для подключения потребуется подтверждение владельца.')
        self.hint.setWordWrap(True)
        self.hint.setObjectName('muted')
        layout.addWidget(self.hint)
        self.code = QLabel()
        self.code.setTextFormat(Qt.TextFormat.PlainText)
        self.code.setWordWrap(True)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.code)

    def set_devices(self, devices, trust_store=None):
        selected = self.selected()
        self._devices = list(devices)
        self.list.clear()
        for device in self._devices:
            trust = trust_store.state(device) if trust_store else 'new'
            state = {'new': 'новый компьютер', 'known': 'знакомый компьютер', 'changed': 'ключ изменился — проверьте владельца'}[trust]
            if not device.quick_pairing:
                state = 'старая версия — требуется обновление'
            item = QListWidgetItem(f'{device.name}\n{device.address} · {state}')
            self.list.addItem(item)
            if selected and selected.identifier == device.identifier:
                self.list.setCurrentItem(item)
        if not self._devices:
            self.hint.setText('Пока никого нет. Откройте VirtualKVM на другом ПК и разрешите приложению доступ к частной сети в брандмауэре.')
        else:
            self.hint.setText('Выберите компьютер. Владелец подтвердит подключение; длинный отпечаток и PIN вводить не нужно.')
        self._selection_changed()

    def selected(self):
        row = self.list.currentRow()
        return self._devices[row] if 0 <= row < len(self._devices) else None

    def _selection_changed(self):
        device = self.selected()
        self.connect_button.setEnabled(device is not None and device.quick_pairing)

    def connect_selected(self):
        device = self.selected()
        if device is not None and device.quick_pairing:
            self.connect_requested.emit(device)

    def show_code(self, code):
        self.code.setText(f'Код сеанса: {code}\nСверьте его на другом компьютере и дождитесь подтверждения владельца.')


class AccessApprovalDialog(QDialog):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle('Запрос подключения')
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        name = QLabel(f'Компьютер «{request.name}» запрашивает доступ к вашему экрану.')
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setWordWrap(True)
        layout.addWidget(name)
        code = QLabel(request.code)
        code.setTextFormat(Qt.TextFormat.PlainText)
        code.setStyleSheet('font-size: 25px; font-weight: 600; padding: 12px;')
        code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(code)
        note = QLabel('Разрешайте подключение, только если на другом компьютере показан тот же код. Он относится к этому сеансу.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.control = QCheckBox('Клавиатура и мышь')
        self.control.setChecked(request.control)
        self.clipboard = QCheckBox('Текстовый буфер обмена')
        self.files = QCheckBox('Передача файлов и общая папка')
        self.audio = QCheckBox('Передавать выбранный источник звука')
        for widget in (self.control, self.clipboard, self.files, self.audio):
            layout.addWidget(widget)
        self.remaining = QLabel()
        self.remaining.setObjectName('muted')
        layout.addWidget(self.remaining)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Разрешить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отклонить')
        buttons.accepted.connect(self._allow)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(lambda _: request.resolve())
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(250)
        self._tick()

    def _allow(self):
        self.request.resolve(Permissions(self.control.isChecked(), self.clipboard.isChecked(), self.files.isChecked(), self.audio.isChecked()))
        self.accept()

    def _tick(self):
        seconds = max(0, math.ceil(self.request.expires - time.monotonic()))
        self.remaining.setText(f'Время на подтверждение: {seconds} с')
        if self.request.done.is_set() or not seconds:
            self.reject()


class CodeConfirmationDialog(QDialog):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request=request
        self.setWindowTitle('Сверьте код подключения')
        self.setMinimumWidth(440)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(24,24,24,24)
        layout.setSpacing(14)
        title=QLabel(f'Первое подключение к «{request.name}»')
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        code=QLabel(request.code)
        code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code.setStyleSheet('font-size:25px;font-weight:600;padding:12px;')
        layout.addWidget(code)
        help=QLabel('На компьютере владельца должен отображаться точно такой же код. Если код отличается или запрос там не появился, отмените подключение.')
        help.setWordWrap(True)
        layout.addWidget(help)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Код совпал')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(self._allow)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(lambda _:request.resolve())
        self._timer=QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(250)

    def _allow(self):
        self.request.resolve(Permissions(False,False,False,False))
        self.accept()

    def _tick(self):
        if self.request.done.is_set() or time.monotonic()>=self.request.expires:
            self.reject()
