from datetime import datetime

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenuBar,
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
from ..core import udev
from ..core.usb import DeviceMode, detect_mode, usb_id


POLL_INTERVAL_MS = 250  # need to be fast enough to catch ~4s BROM windows


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
    """Runs a Recipe in a worker thread, emitting log lines back to the UI.

    Spawning mtkclient eagerly (before the phone is connected) is fine and useful:
    mtkclient has its own retry loop that grabs the device the instant it enumerates,
    which is faster than us reacting to the USB event after the fact. This is how we
    fit inside the ~4s BROM watchdog window.
    """

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
        self.resize(1200, 760)

        self._thread: QThread | None = None
        self._worker: RecipeWorker | None = None
        self._last_mode: DeviceMode | None = None

        self._build_menubar()
        self._build_ui()
        self._refresh_usb_status()

        self._usb_timer = QTimer(self)
        self._usb_timer.setInterval(POLL_INTERVAL_MS)
        self._usb_timer.timeout.connect(self._refresh_usb_status)
        self._usb_timer.start()

    # ---- UI construction ---------------------------------------------------

    def _build_menubar(self) -> None:
        bar = QMenuBar(self)
        setup_menu = bar.addMenu("&Setup")

        install_act = QAction("Install &udev rule", self)
        install_act.triggered.connect(self._install_udev_rule)
        setup_menu.addAction(install_act)

        check_act = QAction("&Check udev status", self)
        check_act.triggered.connect(self._check_udev_status)
        setup_menu.addAction(check_act)

        self.setMenuBar(bar)

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        # Status banner
        self._status_label = QLabel("Detecting device…")
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        self._status_label.setFont(font)
        self._status_label.setStyleSheet("padding: 8px; border-radius: 4px;")
        outer.addWidget(self._status_label)

        splitter = QSplitter()

        # ---- left: findings ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Findings"))
        self._probe_button = QPushButton("Probe device (read-only)")
        self._probe_button.clicked.connect(self._run_probe)
        left_layout.addWidget(self._probe_button)
        self._findings_list = QListWidget()
        left_layout.addWidget(self._findings_list, 1)

        left_layout.addWidget(QLabel("Connection events"))
        self._events_list = QListWidget()
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._events_list.setFont(mono)
        left_layout.addWidget(self._events_list, 1)

        # ---- right: recipes + log ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("Recipes"))
        self._recipes_list = QListWidget()
        for recipe in RECIPES.values():
            mark = " (writes!)" if recipe.writes_device else ""
            item = QListWidgetItem(f"{recipe.title}{mark}")
            item.setData(0x0100, recipe.id)
            self._recipes_list.addItem(item)
        if self._recipes_list.count():
            self._recipes_list.setCurrentRow(0)
        right_layout.addWidget(self._recipes_list)

        button_row = QHBoxLayout()
        self._run_button = QPushButton("Run / Arm recipe")
        self._run_button.setToolTip(
            "If the phone is connected in BROM, the recipe runs now.\n"
            "If the phone is OFFLINE, mtkclient is spawned and waits — enter BROM "
            "(Vol Up + Vol Down + USB) and it will grab the device automatically."
        )
        self._run_button.clicked.connect(self._run_selected_recipe)
        button_row.addWidget(self._run_button)
        self._stop_button = QPushButton("Stop")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._stop_running)
        button_row.addWidget(self._stop_button)
        right_layout.addLayout(button_row)

        right_layout.addWidget(QLabel("mtkclient log"))
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(mono)
        right_layout.addWidget(self._log, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)

    # ---- USB status polling ------------------------------------------------

    def _refresh_usb_status(self) -> None:
        mode = detect_mode()
        if mode != self._last_mode:
            self._on_mode_changed(self._last_mode, mode)
            self._last_mode = mode

        text = f"USB: {usb_id(mode)}   |   Mode: {mode.value.upper()}"
        color = {
            DeviceMode.BROM: "#2e7d32",
            DeviceMode.PRELOADER: "#ef6c00",
            DeviceMode.OFFLINE: "#c62828",
        }[mode]
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"padding: 8px; border-radius: 4px; color: white; background: {color};"
        )

    def _on_mode_changed(self, prev: DeviceMode | None, new: DeviceMode) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prev_str = prev.value.upper() if prev is not None else "—"
        self._add_event(f"{ts}  {prev_str} → {new.value.upper()}")

        # Best-effort: detach cdc_acm/option whenever a device appears so it doesn't
        # steal the interface from mtkclient. Idempotent and silent on failure.
        if new in (DeviceMode.BROM, DeviceMode.PRELOADER):
            detached = udev.detach_kernel_drivers()
            if detached:
                self._add_event(f"{ts}  detached: {', '.join(detached)}")

    def _add_event(self, text: str) -> None:
        self._events_list.addItem(QListWidgetItem(text))
        self._events_list.scrollToBottom()
        # Trim to last 200 lines
        while self._events_list.count() > 200:
            self._events_list.takeItem(0)

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
        if self._worker is not None:
            QMessageBox.information(self, "Busy", "A recipe is already running.")
            return

        item = self._recipes_list.currentItem()
        if item is None:
            QMessageBox.information(self, "No recipe", "Select a recipe.")
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

        ts = datetime.now().strftime("%H:%M:%S")
        mode = detect_mode()
        if mode == DeviceMode.OFFLINE:
            self._log.append(
                f"\n[{ts}] >>> {recipe.title}\n"
                f"[{ts}] Device is OFFLINE. mtkclient is starting and will wait.\n"
                f"[{ts}] Enter BROM now: power off phone, hold Vol Up + Vol Down, plug USB.\n"
            )
        else:
            self._log.append(f"\n[{ts}] >>> {recipe.title} (device is {mode.value.upper()})\n")

        self._run_button.setEnabled(False)
        self._stop_button.setEnabled(True)

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
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{ts}] <<< {message}\n")
        self._run_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._worker = None

    def _stop_running(self) -> None:
        # Not implemented in MVP — mtkclient subprocess can be terminated by
        # killing it manually; we'll wire SIGTERM in the next iteration.
        QMessageBox.information(
            self,
            "Not implemented yet",
            "Use the terminal where mtk-rescue was launched and Ctrl-C, or kill the "
            "mtkclient subprocess. Graceful cancel coming in the next iteration.",
        )

    # ---- Setup menu actions ------------------------------------------------

    def _install_udev_rule(self) -> None:
        ok, msg = udev.install_rule()
        icon = QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning
        box = QMessageBox(icon, "Install udev rule", msg, parent=self)
        box.exec()

    def _check_udev_status(self) -> None:
        installed = udev.rule_installed()
        if installed:
            msg = (
                f"udev rule is installed at {udev.UDEV_RULE_PATH}.\n\n"
                "Kernel will grant userspace access to MTK devices and unbind "
                "cdc_acm/option automatically."
            )
        else:
            msg = (
                f"udev rule is NOT installed.\n\n"
                "Without it, the kernel may grab MTK devices as cdc_acm modems, "
                "blocking mtkclient. Use Setup → Install udev rule."
            )
        QMessageBox.information(self, "udev rule status", msg)
