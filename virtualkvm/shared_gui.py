"""Asynchronous, opt-in shared-folder browsing for the desktop interface."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QPushButton, QScrollArea,
    QStackedWidget, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)


class _WorkerBridge(QObject):
    finished = Signal(int, str, object, object)


def _size_text(size: int) -> str:
    amount = max(0, size)
    for suffix in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if amount < 1024 or suffix == "ТБ":
            return f"{amount:.0f} {suffix}" if suffix == "Б" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return ""


def _join_remote(directory: str, name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Введите имя без разделителей пути.")
    return str(PurePosixPath(directory) / name) if directory else name


class SharedFolderDialog(QDialog):
    """Expose only the folder and permissions explicitly granted by the host.

    The viewer object provides shared_list, shared_download, shared_upload, and
    shared_mkdir. All protocol operations run away from the Qt GUI thread.
    """

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self._path = ""
        self._writable = False
        self._busy = False
        self._closed = False
        self._generation = 0
        self._notice = ""
        self._bridge = _WorkerBridge(self)
        self._bridge.finished.connect(self._finished)
        self.setWindowTitle("Общая папка · VirtualKVM")
        self.setObjectName("sharedFolderDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(850, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        heading = QHBoxLayout()
        title_column = QVBoxLayout()
        title_column.setSpacing(5)
        self.title = QLabel("Общая папка")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setObjectName("pageTitle")
        self.subtitle = QLabel("Файлы на подключённом компьютере")
        self.subtitle.setObjectName("muted")
        title_column.addWidget(self.title)
        title_column.addWidget(self.subtitle)
        heading.addLayout(title_column, 1)
        self.permission_label = QLabel("Только чтение")
        self.permission_label.setObjectName("permissionBadge")
        heading.addWidget(self.permission_label)
        layout.addLayout(heading)

        navigation = QHBoxLayout()
        navigation.setSpacing(8)
        self.up_button = QPushButton("↑ Назад")
        self.up_button.setToolTip("На один уровень выше")
        self.up_button.clicked.connect(self._go_up)
        navigation.addWidget(self.up_button)
        self.breadcrumb_scroll = QScrollArea()
        self.breadcrumb_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.breadcrumb_scroll.setWidgetResizable(True)
        self.breadcrumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.breadcrumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.breadcrumb_scroll.setFixedHeight(44)
        self.breadcrumb_widget = QWidget()
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_widget)
        self.breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb_layout.setSpacing(3)
        self.breadcrumb_scroll.setWidget(self.breadcrumb_widget)
        navigation.addWidget(self.breadcrumb_scroll, 1)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.clicked.connect(lambda: self._load(self._path))
        navigation.addWidget(self.refresh_button)
        layout.addLayout(navigation)

        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setObjectName("sharedFiles")
        self.tree.setHeaderLabels(["Имя", "Тип", "Размер", "Изменён"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.itemActivated.connect(self._open_item)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        self.stack.addWidget(self.tree)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        self.empty_title = QLabel("Папка пуста")
        self.empty_title.setObjectName("sectionTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_description = QLabel("Здесь пока нет файлов.")
        self.empty_description.setObjectName("muted")
        self.empty_description.setWordWrap(True)
        self.empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_description.setTextFormat(Qt.TextFormat.PlainText)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_description)
        empty_layout.addStretch()
        self.stack.addWidget(empty)
        layout.addWidget(self.stack, 1)

        self.status_label = QLabel("Подключение к общей папке…")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.mkdir_button = QPushButton("Новая папка")
        self.mkdir_button.clicked.connect(self._make_directory)
        self.upload_button = QPushButton("Загрузить на другой компьютер…")
        self.upload_button.clicked.connect(self._upload)
        self.download_button = QPushButton("Скачать…")
        self.download_button.setObjectName("primaryButton")
        self.download_button.clicked.connect(self._download)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.close)
        actions.addWidget(self.mkdir_button)
        actions.addWidget(self.upload_button)
        actions.addStretch()
        actions.addWidget(self.download_button)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._breadcrumbs()
        info = getattr(viewer, "shared_info", None)
        if not isinstance(info, dict):
            info = getattr(getattr(viewer, "client", None), "shared_info", None)
        if isinstance(info, dict) and info.get("enabled") is False:
            self._show_empty("Общая папка не подключена", "На другом компьютере выберите папку "
                             "в настройках доступа, затем подключитесь заново.")
            self.status_label.setText("Доступ к папкам предоставляется владельцем другого компьютера.")
            self.permission_label.setText("Нет доступа")
            self._update_actions()
        else:
            self._load("")

    @property
    def current_path(self) -> str:
        return self._path

    def _selected(self) -> dict | None:
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    def _update_actions(self) -> None:
        available = not self._busy and not self._closed
        self.up_button.setEnabled(available and bool(self._path))
        self.refresh_button.setEnabled(available)
        self.tree.setEnabled(available)
        self.breadcrumb_widget.setEnabled(available)
        selected = self._selected()
        self.download_button.setEnabled(available and bool(selected) and selected.get("type") == "file")
        self.upload_button.setEnabled(available and self._writable)
        self.mkdir_button.setEnabled(available and self._writable)
        for button in (self.upload_button, self.mkdir_button):
            button.setToolTip("Владелец разрешил только чтение." if not self._writable else "")

    def _request(self, kind: str, operation, status: str) -> None:
        if self._busy or self._closed:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self.status_label.setText(status)
        self._update_actions()

        def work():
            result = error = None
            try:
                result = operation()
            except Exception as failure:
                error = str(failure)
            if not self._closed:
                try:
                    self._bridge.finished.emit(generation, kind, result, error)
                except RuntimeError:
                    pass
        threading.Thread(target=work, name=f"virtualkvm-shared-{kind}", daemon=True).start()

    def _load(self, path: str) -> None:
        self._request("list", lambda: self.viewer.shared_list(path), "Читаем содержимое папки…")

    def _finished(self, generation: int, kind: str, result, error) -> None:
        if self._closed or generation != self._generation:
            return
        self._busy = False
        if error:
            message = str(error)[:2048]
            self.status_label.setText(f"Не удалось выполнить действие: {message}")
            if kind == "list":
                self._writable = False
                self.permission_label.setText("Нет доступа")
                self.tree.clear()
                self._show_empty("Папка недоступна", message)
            self._update_actions()
            return
        if kind == "list":
            try:
                self._apply_listing(result)
            except (TypeError, ValueError, KeyError) as failure:
                self._writable = False
                self.tree.clear()
                self._show_empty("Не удалось прочитать папку", str(failure))
                self.status_label.setText("Другой компьютер вернул некорректный список файлов.")
            self._update_actions()
        else:
            self._notice = {"download": "Файл сохранён на этом компьютере.",
                            "upload": "Файл загружен на другой компьютер.",
                            "mkdir": "Папка создана."}.get(kind, "Готово.")
            self._load(self._path)

    def _apply_listing(self, listing: dict) -> None:
        if not isinstance(listing, dict) or not isinstance(listing.get("entries"), list):
            raise ValueError("Некорректный список файлов.")
        path = listing.get("path", "")
        if not isinstance(path, str) or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise ValueError("Некорректный путь общей папки.")
        self._path = path
        self._writable = listing.get("writable") is True
        self.permission_label.setText("Чтение и запись" if self._writable else "Только чтение")
        share_name = listing.get("name")
        if isinstance(share_name, str) and share_name:
            self.title.setText(f"Общая папка · {share_name[:120]}")
        entries = []
        for entry in listing["entries"]:
            if not isinstance(entry, dict) or entry.get("type") not in ("file", "directory"):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            # Never construct a local/remote operation from an unvalidated row.
            try:
                _join_remote(path, name)
            except ValueError:
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: (entry["type"] != "directory", entry["name"].casefold()))
        self.tree.clear()
        for entry in entries:
            directory = entry["type"] == "directory"
            size = entry.get("size", 0)
            size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
            try:
                modified = datetime.fromtimestamp(float(entry.get("mtime", 0))).strftime("%d.%m.%Y %H:%M")
            except (ValueError, TypeError, OSError, OverflowError):
                modified = "—"
            row = QTreeWidgetItem([entry["name"], "Папка" if directory else "Файл",
                                  "—" if directory else _size_text(size), modified])
            row.setData(0, Qt.ItemDataRole.UserRole, dict(entry))
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon if directory
                                                    else QStyle.StandardPixmap.SP_FileIcon))
            self.tree.addTopLevelItem(row)
        self._breadcrumbs()
        if entries:
            self.stack.setCurrentIndex(0)
        else:
            self._show_empty("Папка пуста", "Загрузите сюда файл или создайте папку." if self._writable
                             else "Владелец пока не добавил сюда файлы.")
        self.status_label.setText(self._notice or f"Объектов: {len(entries)}. " +
                                 ("Можно скачивать и загружать файлы." if self._writable
                                  else "Можно просматривать и скачивать файлы."))
        self._notice = ""

    def _breadcrumbs(self) -> None:
        while self.breadcrumb_layout.count():
            item = self.breadcrumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        components = [("Общая папка", "")]
        path = ""
        for name in PurePosixPath(self._path).parts if self._path else ():
            path = _join_remote(path, name)
            components.append((name, path))
        for index, (name, destination) in enumerate(components):
            if index:
                separator = QLabel("›")
                separator.setObjectName("muted")
                self.breadcrumb_layout.addWidget(separator)
            button = QToolButton()
            button.setText(name)
            button.setObjectName("breadcrumbButton")
            button.setToolTip("/" + destination if destination else "/")
            button.clicked.connect(lambda checked=False, target=destination: self._load(target))
            self.breadcrumb_layout.addWidget(button)
        self.breadcrumb_layout.addStretch()

    def _show_empty(self, title: str, description: str) -> None:
        self.empty_title.setText(title)
        self.empty_description.setText(description)
        self.stack.setCurrentIndex(1)

    def _go_up(self) -> None:
        if self._path:
            parent = PurePosixPath(self._path).parent
            self._load("" if str(parent) == "." else str(parent))

    def _open_item(self, item, column=0) -> None:
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        if entry.get("type") == "directory":
            self._load(_join_remote(self._path, entry["name"]))
        elif entry.get("type") == "file":
            self._download()

    def _download(self) -> None:
        entry = self._selected()
        if self._busy or not entry or entry.get("type") != "file":
            return
        remote = _join_remote(self._path, entry["name"])
        destination, _ = QFileDialog.getSaveFileName(self, "Сохранить файл на этом компьютере",
            str(Path.home() / "Downloads" / entry["name"]))
        if destination:
            self._request("download", lambda: self.viewer.shared_download(remote, Path(destination)),
                          f"Скачиваем «{entry['name']}»…")

    def _upload(self) -> None:
        if self._busy or not self._writable:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Загрузить файл в общую папку")
        if filename:
            local = Path(filename)
            remote = _join_remote(self._path, local.name)
            self._request("upload", lambda: self.viewer.shared_upload(local, remote),
                          f"Загружаем «{local.name}»…")

    def _make_directory(self) -> None:
        if self._busy or not self._writable:
            return
        name, accepted = QInputDialog.getText(self, "Новая папка", "Имя папки:")
        if not accepted:
            return
        try:
            remote = _join_remote(self._path, name.strip())
        except ValueError as failure:
            self.status_label.setText(str(failure))
            return
        self._request("mkdir", lambda: self.viewer.shared_mkdir(remote), f"Создаём «{name.strip()}»…")

    def closeEvent(self, event) -> None:
        self._closed = True
        self._generation += 1
        super().closeEvent(event)
