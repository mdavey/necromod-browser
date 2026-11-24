"""A simple browser for WSP1 packages (``.wsp`` / ``.necromod``)."""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QFontDatabase,
    QKeySequence,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .package import Entry, Package, PackageError, read_package, safe_destination

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".ico",
    ".svg",
    ".tif",
    ".tiff",
}
AUDIO_EXTENSIONS = {".ogg", ".wav", ".mp3", ".flac", ".opus"}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".lua",
    ".h",
    ".hpp",
    ".hxx",
    ".c",
    ".cc",
    ".cpp",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".htm",
    ".css",
    ".sh",
    ".bat",
    ".ps1",
    ".log",
    ".id",
    ".glsl",
    ".frag",
    ".vert",
    ".map",
    ".editorconfig",
}
SPECIFIC_TEXT_LABELS = {
    ".json": "JSON",
    ".lua": "Lua",
    ".xml": "XML",
    ".md": "Markdown",
    ".h": "C header",
    ".hpp": "C++ header",
    ".cpp": "C++",
    ".c": "C",
    ".csv": "CSV",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".sh": "Shell",
    ".py": "Python",
}
TEXT_PREVIEW_LIMIT = 4 * 1024 * 1024  # bytes shown in the text pane


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for b in sample if b >= 0x20 or b in (0x09, 0x0A, 0x0D))
    return printable / len(sample) > 0.95


def classify(name: str, data: bytes) -> str:
    """Return a short human readable file type.

    Content is checked before the extension: the game ships LuaJIT bytecode in
    ``.lua`` files, which must not be shown as source.
    """
    suffix = _suffix(name)
    if suffix in IMAGE_EXTENSIONS:
        return "Image"
    if suffix in AUDIO_EXTENSIONS:
        return "Audio"
    if data.startswith(b"\x1bLJ"):
        return "Lua bytecode"
    if _looks_like_text(data):
        if suffix in TEXT_EXTENSIONS:
            return SPECIFIC_TEXT_LABELS.get(suffix, "Text")
        return "Text"
    return "Binary"


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- model


class FileTableModel(QAbstractTableModel):
    COLUMNS = ("Name", "Type", "Size")
    _NO_PARENT = QModelIndex()

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[Entry] = []
        self._kinds: dict[str, str] = {}

    def set_entries(self, entries: list[Entry], kinds: dict[str, str]) -> None:
        self.beginResetModel()
        self._entries = entries
        self._kinds = kinds
        self.endResetModel()

    def entry_at(self, row: int) -> Entry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def rowCount(self, parent: QModelIndex = _NO_PARENT) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex = _NO_PARENT) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = (
                Qt.AlignmentFlag.AlignRight
                if section == 2
                else Qt.AlignmentFlag.AlignLeft
            )
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return PurePosixPath(entry.name).name
            if column == 1:
                return self._kinds.get(entry.name, "Binary")
            return human_size(len(entry.data))
        if role == Qt.ItemDataRole.UserRole:  # also the sort key
            if column == 0:
                return entry.name
            if column == 1:
                return self._kinds.get(entry.name, "Binary")
            return len(entry.data)
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 2:
                return f"{len(entry.data):,} bytes"
            return entry.name
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


# ------------------------------------------------------------------------- preview


class ImagePreview(QScrollArea):
    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pixmap = QPixmap()
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self._label)

    def set_image(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._update_pixmap()

    def clear_image(self) -> None:
        self._pixmap = QPixmap()
        self._label.clear()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_pixmap()

    def _update_pixmap(self) -> None:
        if self._pixmap.isNull():
            self._label.clear()
            return
        area = self.viewport().size() - QSize(12, 12)
        if area.width() <= 0 or area.height() <= 0:
            return
        width, height = self._pixmap.width(), self._pixmap.height()
        if width <= area.width() and height <= area.height():
            # Pixel art: enlarge by an integer factor so small sprites are
            # visible without blurring.
            factor = max(
                1,
                min(area.width() // max(width, 1), area.height() // max(height, 1), 8),
            )
            scaled = self._pixmap.scaled(
                width * factor,
                height * factor,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        else:
            scaled = self._pixmap.scaled(
                area,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._label.setPixmap(scaled)


class PreviewPane(QWidget):
    def __init__(self, window: PackageViewer) -> None:
        super().__init__()
        self._window = window
        self._entry: Entry | None = None

        self._title = QLabel("No file selected")
        self._title.setStyleSheet("font-weight: 600;")
        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet("color: palette(placeholderText);")
        self._subtitle.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self._open_button = QPushButton("Open")
        self._save_button = QPushButton("Save as\u2026")
        self._open_button.setEnabled(False)
        self._save_button.setEnabled(False)
        self._open_button.clicked.connect(self._open_externally)
        self._save_button.clicked.connect(self._save_as)

        header = QHBoxLayout()
        header.addWidget(self._title, 1)
        header.addWidget(self._open_button)
        header.addWidget(self._save_button)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed.setPointSize(max(fixed.pointSize() - 1, 8))
        self._text.setFont(fixed)
        self._text.setTabStopDistance(
            4 * self._text.fontMetrics().horizontalAdvance(" ")
        )

        self._image = ImagePreview()

        self._placeholder = QLabel("Select a file to preview")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color: palette(placeholderText);")

        self._stack = QStackedWidget()
        self._stack.addWidget(self._placeholder)  # 0
        self._stack.addWidget(self._text)  # 1
        self._stack.addWidget(self._image)  # 2

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self._subtitle)
        layout.addWidget(self._stack, 1)

    # -- public API ------------------------------------------------------

    def clear(self) -> None:
        self._entry = None
        self._title.setText("No file selected")
        self._subtitle.setText("")
        self._open_button.setEnabled(False)
        self._save_button.setEnabled(False)
        self._text.clear()
        self._image.clear_image()
        self._placeholder.setText("Select a file to preview")
        self._stack.setCurrentIndex(0)

    def show_entry(self, entry: Entry, kind: str) -> None:
        self._entry = entry
        self._text.clear()
        self._image.clear_image()
        self._open_button.setEnabled(True)
        self._save_button.setEnabled(True)
        self._title.setText(PurePosixPath(entry.name).name)
        self._title.setToolTip(entry.name)
        self._subtitle.setText(f"{kind} \u00b7 {human_size(len(entry.data))}")

        if kind == "Image":
            pixmap = QPixmap()
            if pixmap.loadFromData(entry.data):
                self._image.set_image(pixmap)
                self._subtitle.setText(
                    f"Image \u00b7 {pixmap.width()}\u00d7{pixmap.height()} \u00b7 "
                    f"{human_size(len(entry.data))}"
                )
                self._stack.setCurrentIndex(2)
                return
            self._placeholder.setText("This image could not be decoded")
        elif _looks_like_text(entry.data):
            text = entry.data
            note = ""
            if len(text) > TEXT_PREVIEW_LIMIT:
                text = text[:TEXT_PREVIEW_LIMIT]
                note = "\n\n\u2026 preview truncated; use Open or Save as\u2026 for the whole file."
            body = text.decode("utf-8", errors="replace")
            self._text.setPlainText(body + note)
            self._stack.setCurrentIndex(1)
            return
        else:
            self._placeholder.setText(
                f"No preview available for {kind.lower()} files.\n"
                "Use Open or Save as\u2026 to work with this file."
            )

        self._stack.setCurrentIndex(0)

    # -- actions ---------------------------------------------------------

    def _open_externally(self) -> None:
        if self._entry is None:
            return
        directory = Path(tempfile.gettempdir()) / "necromod-preview"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / PurePosixPath(self._entry.name).name
        try:
            target.write_bytes(self._entry.data)
        except OSError as exc:
            QMessageBox.warning(self, "Open failed", str(exc))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            QMessageBox.warning(
                self,
                "Open failed",
                f"No application is registered to open {target.name}.\n"
                f"The file was written to {target}.",
            )

    def _save_as(self) -> None:
        if self._entry is None:
            return
        suggested = str(Path.home() / PurePosixPath(self._entry.name).name)
        filename, _ = QFileDialog.getSaveFileName(self, "Save file", suggested)
        if not filename:
            return
        try:
            Path(filename).write_bytes(self._entry.data)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self._window.statusBar().showMessage(f"Saved {filename}", 5000)


# --------------------------------------------------------------------------- window


class PackageViewer(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("necromod-browser")
        self.resize(1280, 800)
        self.setAcceptDrops(True)

        self._package: Package | None = None
        self._kinds: dict[str, str] = {}
        self._by_dir: dict[str, list[Entry]] = {}
        self._dir_counts: Counter[str] = Counter()

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Directory", "Files"])
        self._tree.setUniformRowHeights(True)
        self._tree.setIndentation(12)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.header().resizeSection(1, 54)
        self._tree.currentItemChanged.connect(self._on_directory_changed)

        self._model = FileTableModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(int(Qt.ItemDataRole.UserRole))
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(0)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.doubleClicked.connect(
            lambda _index: self._preview._open_externally()
        )

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter this directory\u2026")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._proxy.setFilterFixedString)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self._filter)

        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color: palette(placeholderText);")

        self._middle_stack = QStackedWidget()
        self._middle_stack.addWidget(self._table)
        self._middle_stack.addWidget(self._empty)
        middle_layout.addWidget(self._middle_stack, 1)

        self._preview = PreviewPane(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._tree)
        splitter.addWidget(middle)
        splitter.addWidget(self._preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([260, 420, 600])
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        self._build_menu()
        self.statusBar().showMessage("Open a .wsp or .necromod file to begin")

        self._table.selectionModel().currentChanged.connect(self._on_current_changed)

    # -- menu ------------------------------------------------------------

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open\u2026", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_dialog)
        file_menu.addAction(open_action)

        self._close_action = QAction("&Close", self)
        self._close_action.setShortcut(QKeySequence.StandardKey.Close)
        self._close_action.setEnabled(False)
        self._close_action.triggered.connect(self.close_package)
        file_menu.addAction(self._close_action)

        file_menu.addSeparator()

        self._extract_action = QAction("&Extract all\u2026", self)
        self._extract_action.setEnabled(False)
        self._extract_action.triggered.connect(self.extract_all)
        file_menu.addAction(self._extract_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # -- loading ---------------------------------------------------------

    def open_dialog(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open package",
            "",
            "NecroDancer packages (*.necromod *.wsp);;All files (*)",
        )
        if filename:
            self.open_package(filename)

    def open_package(self, filename: str) -> None:
        try:
            package = read_package(filename)
        except (PackageError, OSError) as exc:
            QMessageBox.critical(self, "Could not open package", str(exc))
            return

        self._package = package
        self._kinds = {e.name: classify(e.name, e.data) for e in package.entries}
        self._by_dir = {}
        for entry in package.entries:
            parent = str(PurePosixPath(entry.name).parent)
            self._by_dir.setdefault("" if parent == "." else parent, []).append(entry)

        self._populate_tree()
        self._close_action.setEnabled(True)
        self._extract_action.setEnabled(True)
        self.setWindowTitle(Path(filename).name)
        stored = human_size(package.stored_size)
        self.statusBar().showMessage(
            f"{Path(filename).name}: {len(package.entries):,} files, "
            f"{stored} stored, {human_size(package.total_size)} uncompressed"
        )

    def close_package(self) -> None:
        self._package = None
        self._kinds = {}
        self._by_dir = {}
        self._dir_counts = Counter()
        self._tree.clear()
        self._model.set_entries([], {})
        self._empty.setText("")
        self._middle_stack.setCurrentWidget(self._table)
        self._preview.clear()
        self._close_action.setEnabled(False)
        self._extract_action.setEnabled(False)
        self.setWindowTitle("necromod-browser")
        self.statusBar().showMessage("Open a .wsp or .necromod file to begin")

    # -- tree / table ----------------------------------------------------

    def _populate_tree(self) -> None:
        assert self._package is not None
        self._tree.clear()
        self._filter.clear()

        counts: Counter[str] = Counter()
        for entry in self._package.entries:
            parent = str(PurePosixPath(entry.name).parent)
            parent = "" if parent == "." else parent
            while parent:
                counts[parent] += 1
                parent = parent.rpartition("/")[0]
        self._dir_counts = counts

        root = QTreeWidgetItem(["/", f"{len(self._package.entries):,}"])
        root.setData(0, Qt.ItemDataRole.UserRole, "")
        root.setToolTip(0, "/")
        root.setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._tree.addTopLevelItem(root)

        dirs: set[str] = set()
        for path in self._by_dir:
            if not path:
                continue
            parts = path.split("/")
            for index in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:index]))

        items: dict[str, QTreeWidgetItem] = {"": root}
        for path in sorted(dirs):
            parent_path, _, name = path.rpartition("/")
            parent_item = items.get(parent_path, root)
            item = QTreeWidgetItem([name, f"{counts.get(path, 0):,}"])
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            item.setToolTip(0, path)
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            parent_item.addChild(item)
            items[path] = item

        root.setExpanded(True)
        target = self._first_populated_item() or root
        self._reveal(target)
        self._tree.setCurrentItem(target)
        target.setExpanded(True)

    def _first_populated_item(self) -> QTreeWidgetItem | None:
        """Shallowest directory that actually contains files."""
        queue = [self._tree.topLevelItem(0)]
        while queue:
            item = queue.pop(0)
            if item is None:
                continue
            path = item.data(0, Qt.ItemDataRole.UserRole) or ""
            if self._by_dir.get(path):
                return item
            queue.extend(item.child(index) for index in range(item.childCount()))
        return None

    def _reveal(self, item: QTreeWidgetItem) -> None:
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()

    def _on_directory_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        self._preview.clear()
        if current is None:
            self._model.set_entries([], self._kinds)
            return
        path = current.data(0, Qt.ItemDataRole.UserRole) or ""
        entries = sorted(
            self._by_dir.get(path, []),
            key=lambda e: PurePosixPath(e.name).name.lower(),
        )
        self._model.set_entries(entries, self._kinds)
        if not entries:
            self._show_empty_directory(path)
            return
        self._middle_stack.setCurrentWidget(self._table)
        self._table.scrollToTop()
        self._table.selectRow(0)

    def _show_empty_directory(self, path: str) -> None:
        total = self._dir_counts.get(path, 0)
        label = (
            "\u201cthe package root\u201d"
            if not path
            else f"\u201c{path.rpartition('/')[2]}\u201d"
        )
        if total:
            text = (
                f"No files directly in {label}.\n{total:,} files in its subdirectories."
            )
        else:
            text = f"No files in {label}."
        self._empty.setText(text)
        self._middle_stack.setCurrentWidget(self._empty)

    def _current_entry(self) -> Entry | None:
        index = self._table.currentIndex()
        if not index.isValid():
            return None
        source = self._proxy.mapToSource(index)
        return self._model.entry_at(source.row())

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            self._preview.clear()
            return
        entry = self._current_entry()
        if entry is None:
            self._preview.clear()
            return
        self._preview.show_entry(entry, self._kinds.get(entry.name, "Binary"))
        self.statusBar().showMessage(f"{entry.name} \u2014 {len(entry.data):,} bytes")

    # -- extraction ------------------------------------------------------

    def extract_all(self) -> None:
        if self._package is None:
            return
        target = QFileDialog.getExistingDirectory(self, "Extract all to\u2026")
        if not target:
            return
        root = Path(target)
        written = failed = 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for entry in self._package.entries:
                try:
                    destination = safe_destination(root, entry.name)
                except PackageError:
                    failed += 1
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(entry.data)
                written += 1
        finally:
            QApplication.restoreOverrideCursor()
        message = f"Extracted {written:,} files to {root}"
        if failed:
            message += f" ({failed} refused)"
        self.statusBar().showMessage(message, 10000)

    # -- drag and drop ---------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            filename = url.toLocalFile()
            if filename:
                self.open_package(filename)
                event.acceptProposedAction()
                break


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    app = QApplication(args)
    app.setApplicationName("necromod-browser")
    app.setApplicationDisplayName("necromod-browser")

    window = PackageViewer()
    for candidate in args[1:]:
        if Path(candidate).is_file():
            window.open_package(candidate)
            break
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
