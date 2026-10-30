"""Display selection and receiving-window arrangement; no virtual-display driver."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QScrollArea, QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

from .display_layout import (
    MAX_COORDINATE, MAX_STREAMS, Placement, arrange, decode_layout,
    display_inventory, validate_layout,
)


def local_screens():
    screens = QApplication.screens()
    names = Counter(screen.name() for screen in screens)
    result = []
    for index, screen in enumerate(screens):
        geometry = screen.geometry()
        key = screen.name() or f"screen-{index}"
        if screen.serialNumber():
            key += "|" + screen.serialNumber()
        elif names[screen.name()] > 1:
            key += f"|{geometry.x()},{geometry.y()}"
        label = f"{index + 1} · {screen.name() or 'Монитор'} · {geometry.width()} × {geometry.height()}"
        if screen == QApplication.primaryScreen():
            label += " · основной"
        result.append((key, label, screen))
    return result


class DisplayMap(QWidget):
    moved = Signal(int, int, int)
    selected = Signal(int)

    def __init__(self, parent=None, interactive=False):
        super().__init__(parent)
        self.entries = []
        self.interactive = interactive
        self.selected_row = 0
        self._transform = None
        self._drag = None
        self.setMinimumSize(280, 150)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Схема расположения экранов")

    def set_entries(self, entries, refit=True):
        self.entries = list(entries)
        self.selected_row = max(0, min(self.selected_row, len(self.entries) - 1))
        if refit:
            self._transform = None
        self.update()

    def resizeEvent(self, event):
        self._transform = None
        super().resizeEvent(event)

    def _geometry(self):
        if not self.entries:
            return []
        if self._transform is None:
            left = min(e.x for e in self.entries)
            top = min(e.y for e in self.entries)
            width = max(e.x + e.width for e in self.entries) - left
            height = max(e.y + e.height for e in self.entries) - top
            scale = min(max(1, self.width() - 48) / width, max(1, self.height() - 40) / height)
            self._transform = (scale, (self.width() - width * scale) / 2 - left * scale,
                               (self.height() - height * scale) / 2 - top * scale)
        scale, dx, dy = self._transform
        return [QRectF(e.x * scale + dx, e.y * scale + dy,
                       max(2, e.width * scale), max(2, e.height * scale)) for e in self.entries]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f4f5f7"))
        if not self.entries:
            painter.setPen(QColor("#747c88"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Список экранов пока не получен")
            return
        for row, (entry, rect) in enumerate(zip(self.entries, self._geometry())):
            selected = row == self.selected_row and self.interactive
            painter.setBrush(QColor("#dce3ec" if selected else "#e7eaee"))
            painter.setPen(QPen(QColor("#60738e" if selected else "#bcc3cd"), 2 if selected else 1))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 6, 6)
            painter.setPen(QColor("#303a49"))
            font = QFont(self.font())
            font.setPointSize(18 if rect.width() > 70 and rect.height() > 70 else 10)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.drawText(rect.adjusted(5, 5, -5, -20 if rect.height() > 70 else -5),
                             Qt.AlignmentFlag.AlignCenter, str(entry.source + 1))
            if rect.width() > 100 and rect.height() > 70:
                font.setPointSize(9)
                font.setWeight(QFont.Weight.Normal)
                painter.setFont(font)
                painter.setPen(QColor("#657080"))
                painter.drawText(rect.adjusted(5, rect.height() - 30, -5, -5),
                                 Qt.AlignmentFlag.AlignCenter, f"{entry.width} × {entry.height}")

    def mousePressEvent(self, event):
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        for row, rect in reversed(list(enumerate(self._geometry()))):
            if rect.contains(event.position()):
                self.selected_row = row
                self._drag = (row, event.position(), self.entries[row])
                self.selected.emit(row)
                self.setFocus()
                self.update()
                event.accept()
                return

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        row, start, entry = self._drag
        scale = self._transform[0]
        delta = event.position() - start
        x = max(-MAX_COORDINATE, min(MAX_COORDINATE, entry.x + round(delta.x() / scale)))
        y = max(-MAX_COORDINATE, min(MAX_COORDINATE, entry.y + round(delta.y() / scale)))
        self.moved.emit(row, x, y)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self._transform = None
            self.update()
            event.accept()

    def keyPressEvent(self, event):
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                  Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if self.interactive and self.entries and event.key() in arrows:
            entry = self.entries[self.selected_row]
            dx, dy = arrows[event.key()]
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self.moved.emit(self.selected_row,
                max(-MAX_COORDINATE, min(MAX_COORDINATE, entry.x + dx * step)),
                max(-MAX_COORDINATE, min(MAX_COORDINATE, entry.y + dy * step)))
            event.accept()
        else:
            super().keyPressEvent(event)


class LayoutDialog(QDialog):
    def __init__(self, entries, inventory, presentation, fullscreen, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Экраны и расположение")
        available = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        self.resize(min(900, max(600, available.width() - 40)) if available else 900,
                    min(700, max(400, available.height() - 60)) if available else 660)
        self.setMinimumSize(600, 400)
        self.entries = list(entries)[:min(MAX_STREAMS, len(inventory)) if inventory else MAX_STREAMS]
        self.inventory = list(inventory)
        self.screens = local_screens()
        self._updating = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 12)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(QLabel("Количество экранов"))
        self.count = QSpinBox()
        self.count.setMinimumWidth(64)
        self.count.lineEdit().setTextMargins(8, 0, 8, 0)
        self.count.setToolTip("От 1 до 8. Введите число или используйте клавиши ↑ и ↓.")
        self.count.setRange(1, min(MAX_STREAMS, len(inventory)) if inventory else MAX_STREAMS)
        self.count.setValue(len(self.entries))
        self.count.setAccessibleName("Количество передаваемых экранов")
        top.addWidget(self.count)
        top.addSpacing(14)
        self.presentation = QComboBox()
        self.presentation.addItem("Окна по схеме", "windows")
        self.presentation.addItem("На локальных мониторах", "monitors")
        self.presentation.setCurrentIndex(max(0, self.presentation.findData(presentation)))
        top.addWidget(self.presentation, 1)
        self.fullscreen = QCheckBox("Полный экран")
        self.fullscreen.setChecked(fullscreen and presentation == "monitors")
        self._last_presentation = presentation
        top.addWidget(self.fullscreen)
        layout.addLayout(top)
        self.map = DisplayMap(interactive=True)
        self.map.setMinimumHeight(200)
        layout.addWidget(self.map, 1)
        presets = QHBoxLayout()
        self.preset_buttons = []
        for title, mode in (("Как у хоста", "host"), ("В ряд", "horizontal"),
                            ("Столбцом", "vertical"), ("Сетка", "grid")):
            button = QPushButton(title)
            button.clicked.connect(lambda _, m=mode: self._arrange(m))
            presets.addWidget(button)
            self.preset_buttons.append(button)
        layout.addLayout(presets)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Экран хоста", "Локальный монитор", "X", "Y"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setMinimumHeight(160)
        layout.addWidget(self.table)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setObjectName("muted")
        layout.addWidget(self.help)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setObjectName("validationError")
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(self.buttons)
        self.count.valueChanged.connect(self._count_changed)
        self.presentation.currentIndexChanged.connect(self._render)
        self.fullscreen.toggled.connect(self._validate)
        self.map.moved.connect(self._moved)
        self.map.selected.connect(self.table.selectRow)
        self.table.cellClicked.connect(self._selected)
        self._render()

    def _count_changed(self, count):
        self.entries = self.entries[:count]
        available = [d.index for d in self.inventory] if self.inventory else list(range(64))
        keys = [key for key, _, _ in self.screens]
        while len(self.entries) < count:
            source = next(i for i in available if i not in {e.source for e in self.entries})
            x = max((e.x + e.width for e in self.entries), default=-80) + 80
            target = next((key for key in keys if key not in {e.target for e in self.entries}), "")
            display = next((d for d in self.inventory if d.index == source), None)
            self.entries.append(Placement(source, x, 0, display.width if display else 1920,
                                          display.height if display else 1080, target))
        self._render()

    def _render(self, *args):
        self._updating = True
        mapped = self.presentation.currentData() == "monitors"
        if self.presentation.currentData() != self._last_presentation:
            self.fullscreen.setChecked(mapped)
            self._last_presentation = self.presentation.currentData()
        if mapped:
            keys = [key for key, _, _ in self.screens]
            for row, entry in enumerate(self.entries):
                if not entry.target:
                    key = next((key for key in keys if key not in {e.target for e in self.entries}), "")
                    self.entries[row] = replace(entry, target=key)
        self.fullscreen.setEnabled(mapped)
        for index, button in enumerate(self.preset_buttons):
            button.setEnabled(not mapped and (index != 0 or
                bool(self.inventory) and all(e.source in {d.index for d in self.inventory} for e in self.entries)))
        self.help.setText(
            "Расположение локальных мониторов задаётся в настройках ОС. Назначьте каждому экрану отдельный монитор."
            if mapped else
            "Перетаскивайте экраны или задайте X и Y. Стрелки — сдвиг на 1, Shift + стрелки — на 10. "
            "Схема определяет расположение окон на этом компьютере; координаты хоста не меняются.")
        self.map.interactive = not mapped
        self.table.setRowCount(len(self.entries))
        self.table.setColumnHidden(1, not mapped)
        self.table.setColumnHidden(2, mapped)
        self.table.setColumnHidden(3, mapped)
        for row, entry in enumerate(self.entries):
            source = QComboBox()
            choices = [(d.index, f"{d.index + 1} · {d.name} · {d.pixel_width} × {d.pixel_height}")
                       for d in self.inventory] if self.inventory else [(i, f"Экран {i + 1}") for i in range(64)]
            if entry.source not in {key for key, _ in choices}:
                choices.append((entry.source, f"Экран {entry.source + 1} · недоступен"))
            for index, name in choices:
                source.addItem(name, index)
            source.setCurrentIndex(source.findData(entry.source))
            source.setAccessibleName(f"Источник для окна {row + 1}")
            source.currentIndexChanged.connect(lambda _, r=row, field=source: self._source_changed(r, field.currentData()))
            self.table.setCellWidget(row, 0, source)
            target = QComboBox()
            target.addItem("Выберите монитор", "")
            for key, name, _ in self.screens:
                target.addItem(name, key)
            if entry.target and target.findData(entry.target) < 0:
                target.addItem("Монитор недоступен", entry.target)
            target.setCurrentIndex(max(0, target.findData(entry.target)))
            target.setEnabled(mapped)
            target.setAccessibleName(f"Локальный монитор для окна {row + 1}")
            target.currentIndexChanged.connect(lambda _, r=row, field=target: self._target_changed(r, field.currentData()))
            self.table.setCellWidget(row, 1, target)
            for column, axis in ((2, "x"), (3, "y")):
                field = QSpinBox()
                field.lineEdit().setTextMargins(8, 0, 8, 0)
                field.setRange(-MAX_COORDINATE, MAX_COORDINATE)
                field.setValue(getattr(entry, axis))
                field.setEnabled(not mapped)
                field.setAccessibleName(f"{axis.upper()} окна {row + 1}")
                field.valueChanged.connect(lambda value, r=row, a=axis: self._coordinate_changed(r, a, value))
                self.table.setCellWidget(row, column, field)
        self.table.resizeRowsToContents()
        self._updating = False
        self._preview()
        self._validate()

    def _source_changed(self, row, source):
        self.entries[row] = replace(self.entries[row], source=source)
        display = next((d for d in self.inventory if d.index == source), None)
        if display:
            self.entries[row] = replace(self.entries[row], width=display.width, height=display.height)
        self._preview()
        self._validate()

    def _target_changed(self, row, target):
        self.entries[row] = replace(self.entries[row], target=target)
        self._preview()
        self._validate()

    def _coordinate_changed(self, row, axis, value):
        if not self._updating:
            self.entries[row] = replace(self.entries[row], **{axis: value})
            self._preview()

    def _moved(self, row, x, y):
        self.entries[row] = replace(self.entries[row], x=x, y=y)
        for column, value in ((2, x), (3, y)):
            field = self.table.cellWidget(row, column)
            field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(False)
        self.map.set_entries(self.entries, refit=False)

    def _selected(self, row, column):
        self.map.selected_row = row
        self.map.update()

    def _arrange(self, mode):
        self.entries = arrange(self.entries, mode, self.inventory)
        self._render()

    def _preview(self):
        entries = self.entries
        if self.presentation.currentData() == "monitors":
            screens = {key: screen.geometry() for key, _, screen in self.screens}
            entries = [replace(e, x=screens[e.target].x(), y=screens[e.target].y(),
                       width=screens[e.target].width(), height=screens[e.target].height())
                       if e.target in screens else e for e in entries]
        self.map.set_entries(entries)

    def _validate(self, *args):
        try:
            validate_layout(self.entries, self.fullscreen.isChecked(), self.presentation.currentData())
            if self.inventory and any(e.source not in {d.index for d in self.inventory} for e in self.entries):
                raise ValueError("Один из выбранных экранов отсутствует на хосте. Обновите список или выберите другой экран.")
            if self.presentation.currentData() == "monitors":
                if any(e.target not in {key for key, _, _ in self.screens} for e in self.entries):
                    raise ValueError("Выбранный локальный монитор недоступен. Назначьте подключённый монитор.")
            error = ""
        except ValueError as failure:
            error = str(failure)
        self.error.setText(error)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not error)


class MonitorPanel(QWidget):
    discover_requested = Signal()
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = [Placement(0)]
        self.inventory = []
        self.presentation = "windows"
        self.fullscreen = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        top = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        top.addWidget(self.summary, 1)
        self.discover = QPushButton("Получить экраны хоста")
        self.discover.clicked.connect(self.discover_requested.emit)
        top.addWidget(self.discover)
        self.configure = QPushButton("Настроить…")
        self.configure.clicked.connect(self._configure)
        top.addWidget(self.configure)
        layout.addLayout(top)
        self.map = DisplayMap()
        self.map.setFixedHeight(155)
        layout.addWidget(self.map)
        hint = QLabel("Передаются существующие экраны хоста. Новые дисплеи в Windows или macOS не создаются.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        self._refresh()

    def _refresh(self):
        count = len(self.entries)
        kind = "окна по схеме" if self.presentation == "windows" else "локальные мониторы"
        known = f" · доступно на хосте: {len(self.inventory)}" if self.inventory else " · номера экранов заданы вручную"
        self.summary.setText(f"Экранов: {count} · {kind}{known}")
        entries = self.entries
        if self.presentation == "monitors":
            screens = {key: screen.geometry() for key, _, screen in local_screens()}
            entries = [replace(e, x=screens[e.target].x(), y=screens[e.target].y(),
                       width=screens[e.target].width(), height=screens[e.target].height())
                       if e.target in screens else e for e in entries]
        self.map.set_entries(entries)

    def set_entries(self, entries, presentation=None, fullscreen=None):
        mode = presentation if presentation is not None else self.presentation
        full = fullscreen if fullscreen is not None else self.fullscreen
        validate_layout(entries, full, mode)
        self.entries = list(entries)
        self.presentation, self.fullscreen = mode, full
        self._refresh()
        self.changed.emit()

    def set_count(self, count):
        dialog = LayoutDialog(self.entries, self.inventory, self.presentation, self.fullscreen, self)
        try:
            dialog.count.setValue(count)
            self.set_entries(dialog.entries, "windows")
        finally:
            dialog.deleteLater()

    def set_inventory(self, values, follow_host=False):
        inventory = display_inventory(values)
        self.inventory = inventory
        known = {display.index: display for display in inventory}
        self.entries = [replace(entry, width=known[entry.source].width, height=known[entry.source].height)
                        if entry.source in known else entry for entry in self.entries]
        if follow_host:
            self.entries = arrange(self.entries, "host", inventory)
        self._refresh()

    def _configure(self):
        dialog = LayoutDialog(self.entries, self.inventory, self.presentation, self.fullscreen, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_entries(dialog.entries, dialog.presentation.currentData(), dialog.fullscreen.isChecked())
        dialog.deleteLater()

    def encode(self):
        return {"version": 1, "presentation": self.presentation, "fullscreen": self.fullscreen,
                "entries": [asdict(entry) for entry in self.entries]}

    def restore(self, value):
        entries, fullscreen, presentation = decode_layout(value)
        self.set_entries(entries, presentation, fullscreen)

    def validate(self):
        validate_layout(self.entries, self.fullscreen, self.presentation)
        if self.inventory and any(e.source not in {d.index for d in self.inventory} for e in self.entries):
            raise ValueError("Один из выбранных экранов отсутствует на хосте. Обновите список и настройку экранов.")
        if self.presentation == "monitors":
            available = {key for key, _, _ in local_screens()}
            if any(entry.target not in available for entry in self.entries):
                raise ValueError("Локальный монитор недоступен. Откройте настройку экранов и обновите назначение.")
