from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.checks import CHECKS
from ..core.findings import Finding, Severity
from ..core.mtk import MtkClient, MtkClientNotFoundError
from ..core.recipes import RECIPES, Recipe
from ..core.usb import DeviceMode, detect_mode, usb_id


SEVERITY_COLORS = {
    Severity.OK: QColor("#2e7d32"),
    Severity.INFO: QColor("#1565c0"),
    Severity.WARN: QColor("#ef6c00"),
    Severity.CRITICAL: QColor("#c62828"),
    Severity.UNKNOWN: QColor("#616161"),
}

SEVERITY_ICONS = {
    Severity.OK: "[OK]",
    Severity.INFO: "[i] ",
    Severity.WARN: "[!] ",
    Severity.CRITICAL: "[X] ",
    Severity.UNKNOWN: "[?] ",
}


class RecipeWorker(QObject):
    """Runs a Recipe in a worker thread, emitting log lines back to the UI."""

    line = Signal(str)
    finished = Signal(bool, str)  # success, message

    def __init__(self, recipe: Recipe, mtk: MtkClient) -> None:
        super().__init__()
        self._recipe = recipe
        self._mtk = mtk

    @Slot()
    def run(self) -> None:
        try:
            for line in self._recipe.runner(self._mtk):
                self.line.emit(line)
            self.finished.emit(True, "Recipe completed successfully.")
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(False, f"Recipe failed: {exc}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("mtk-rescue")
        self.resize(1100, 720)

        self._thread: QThread | None = None
        self._worker: RecipeWorker | None = None

        self._build_ui()
        self._refresh_usb_status()

        self._usb_timer = QTimer(self)
        self._usb_timer.setInterval(2000)
        self._usb_timer.timeout.connect(self._refresh_usb_status)
        self._usb_timer.start()

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        # Top: USB status banner
        self._status_label = QLabel("Detecting device…")
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        self._status_label.setFont(font)
        self._status_label.setStyleSheet("padding: 8px; border-radius: 4px;")
        outer.addWidget(self._status_label)

        # Middle: split between findings (left) and recipes+log (right)
        splitter = QSplitter()

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Findings"))
        self._probe_button = QPushButton("Probe device")
        self._probe_button.clicked.connect(self._run_probe)
        left_layout.addWidget(self._probe_button)
        self._findings_list = QListWidget()
        left_layout.addWidget(self._findings_list, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("Recipes"))
        self._recipes_list = QListWidget()
        for recipe in RECIPES.values():
            mark = " (writes!)" if recipe.writes_device else ""
            item = QListWidgetItem(f"{recipe.title}{mark}")
            item.setData(0x0100, recipe.id)  # Qt::UserRole = 0x0100
            self._recipes_list.addItem(item)
        right_layout.addWidget(self._recipes_list)

        self._run_button = QPushButton("Run selected recipe")
        self._run_button.clicked.connect(self._run_selected_recipe)
        right_layout.addWidget(self._run_button)

        right_layout.addWidget(QLabel("Log"))
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._log.setFont(mono)
        right_layout.addWidget(self._log, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)

    # ---- USB status banner -------------------------------------------------

    def _refresh_usb_status(self) -> None:
        mode = detect_mode()
        text = f"USB: {usb_id(mode)}   |   Mode: {mode.value.upper()}"
        if mode == DeviceMode.BROM:
            color = "#2e7d32"
        elif mode == DeviceMode.PRELOADER:
            color = "#ef6c00"
        else:
            color = "#c62828"
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"padding: 8px; border-radius: 4px; color: white; background: {color};"
        )

    # ---- Probe -------------------------------------------------------------

    def _run_probe(self) -> None:
        self._findings_list.clear()
        for check in CHECKS:
            finding = check()
            self._add_finding(finding)

    def _add_finding(self, finding: Finding) -> None:
        icon = SEVERITY_ICONS.get(finding.severity, "")
        text = f"{icon} {finding.title}\n    {finding.summary}"
        if finding.evidence:
            text += f"\n    ({finding.evidence})"
        item = QListWidgetItem(text)
        item.setForeground(SEVERITY_COLORS.get(finding.severity, QColor("#000000")))
        self._findings_list.addItem(item)

    # ---- Recipe execution --------------------------------------------------

    def _run_selected_recipe(self) -> None:
        item = self._recipes_list.currentItem()
        if item is None:
            QMessageBox.information(self, "No recipe", "Select a recipe to run.")
            return
        recipe_id = item.data(0x0100)
        recipe = RECIPES.get(recipe_id)
        if recipe is None:
            return

        if recipe.writes_device:
            confirm = QMessageBox.warning(
                self,
                "Confirm destructive operation",
                f"'{recipe.title}' WRITES to the device.\n\n{recipe.description}\n\nProceed?",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Ok:
                return

        try:
            mtk = MtkClient()
        except MtkClientNotFoundError as exc:
            QMessageBox.critical(self, "mtkclient not found", str(exc))
            return

        self._log.append(f"\n>>> {recipe.title}\n")
        self._run_button.setEnabled(False)

        self._thread = QThread(self)
        self._worker = RecipeWorker(recipe, mtk)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.line.connect(self._log.append)
        self._worker.finished.connect(self._on_recipe_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot(bool, str)
    def _on_recipe_finished(self, success: bool, message: str) -> None:
        self._log.append(f"<<< {message}\n")
        self._run_button.setEnabled(True)
        self._worker = None
