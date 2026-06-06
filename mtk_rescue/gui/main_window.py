import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.checks import CHECKS
from ..core.findings import Finding, Severity
from ..core.log_parser import EventKind, LogEvent, LogParser
from ..core.mtk import MtkClient, MtkClientNotFoundError
from ..core.preloader import identify_preloader
from ..core.recipes import RECIPES, Recipe
from ..core import udev
from ..core.usb import DeviceMode, detect_mode, usb_id


POLL_INTERVAL_MS = 250  # need to be fast enough to catch ~4s BROM windows


# Friendly display labels for parsed device-info keys.
DEVICE_INFO_LABELS = {
    "cpu": "CPU",
    "hw_code": "HW code",
    "target_config": "Target config",
    "sbc_enabled": "SBC enabled",
    "sla_enabled": "SLA enabled",
    "me_id": "ME ID",
    "soc_id": "SOC ID",
    "ufs_blocksize": "UFS block size",
    "ufs_id": "UFS ID",
    "ufs_mid": "UFS MID",
    "ufs_fwver": "UFS FW ver",
    "ufs_serial": "UFS serial",
    "ufs_lu0_size": "UFS LU0 size",
    "ufs_lu1_size": "UFS LU1 size",
    "ufs_lu2_size": "UFS LU2 size",
}

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
    """Runs a Recipe in a worker thread, emitting raw lines AND parsed events.

    Spawning mtkclient eagerly (before the phone is connected) is fine and useful:
    mtkclient has its own retry loop that grabs the device the instant it enumerates,
    which is faster than us reacting to the USB event after the fact. This is how we
    fit inside the ~4s BROM watchdog window.
    """

    line = Signal(str)
    event = Signal(LogEvent)
    finished = Signal(bool, str)  # success, message

    def __init__(self, recipe: Recipe, mtk: MtkClient, parser: LogParser) -> None:
        super().__init__()
        self._recipe = recipe
        self._mtk = mtk
        self._parser = parser

    @Slot()
    def run(self) -> None:
        try:
            for line in self._recipe.runner(self._mtk):
                self.line.emit(line)
                for evt in self._parser.feed(line):
                    self.event.emit(evt)
            self.finished.emit(True, "Recipe completed successfully.")
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(False, f"Recipe failed: {exc}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("mtk-rescue")
        self.resize(1280, 820)

        self._thread: QThread | None = None
        self._worker: RecipeWorker | None = None
        self._last_mode: DeviceMode | None = None
        self._parser = LogParser()
        self._device_info: dict[str, str] = {}
        self._handled_fixes: set[str] = set()

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
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)

        # ---- left: findings + device info + connection events ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Findings"))
        self._probe_button = QPushButton("Probe device (read-only)")
        self._probe_button.clicked.connect(self._run_probe)
        left_layout.addWidget(self._probe_button)
        self._findings_list = QListWidget()
        self._findings_list.itemDoubleClicked.connect(self._on_finding_double_clicked)
        left_layout.addWidget(self._findings_list, 2)

        left_layout.addWidget(QLabel("Device info (parsed live from mtkclient)"))
        self._device_info_table = QTableWidget(0, 2)
        self._device_info_table.setHorizontalHeaderLabels(["Field", "Value"])
        self._device_info_table.horizontalHeader().setStretchLastSection(True)
        self._device_info_table.verticalHeader().setVisible(False)
        self._device_info_table.setFont(mono)
        self._device_info_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        left_layout.addWidget(self._device_info_table, 2)

        left_layout.addWidget(QLabel("Connection events"))
        self._events_list = QListWidget()
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

        right_layout.addWidget(QLabel("mtkclient log (raw)"))
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

        # Fresh parser + findings state per run. The previous run's findings (e.g. a
        # handshake_failed from an earlier probe attempt, or an auth_required that
        # Kamakiri ended up bypassing) would otherwise stay on screen and mislead.
        self._parser.reset()
        self._handled_fixes.clear()
        self._findings_list.clear()

        self._thread = QThread(self)
        self._worker = RecipeWorker(recipe, mtk, self._parser)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.line.connect(self._log.append)
        self._worker.event.connect(self._on_log_event)
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

    # ---- Parsed-event handling --------------------------------------------

    @Slot(LogEvent)
    def _on_log_event(self, evt: LogEvent) -> None:
        if evt.kind == EventKind.DEVICE_INFO:
            self._set_device_info(evt.key, evt.value)
        elif evt.kind == EventKind.STATE:
            ts = datetime.now().strftime("%H:%M:%S")
            self._add_event(f"{ts}  STATE  {evt.key}: {evt.message}")
            # When Kamakiri bypasses security, the auth_required error from a few
            # lines earlier becomes a non-issue. Drop it so the user isn't misled.
            if evt.key == "security_bypassed":
                self._dismiss_finding_by_key("auth_required")
        elif evt.kind == EventKind.ERROR:
            self._add_error_finding(evt)

    def _dismiss_finding_by_key(self, error_key: str) -> None:
        title_match = f"mtkclient error: {error_key}"
        for row in range(self._findings_list.count()):
            item = self._findings_list.item(row)
            if item and title_match in item.text():
                self._findings_list.takeItem(row)
                self._handled_fixes.discard(error_key)
                return

    def _set_device_info(self, key: str, value: str) -> None:
        label = DEVICE_INFO_LABELS.get(key, key)
        self._device_info[key] = value
        # Refresh table (sorted by friendly label for stable display)
        rows = sorted(self._device_info.items(), key=lambda kv: DEVICE_INFO_LABELS.get(kv[0], kv[0]))
        self._device_info_table.setRowCount(len(rows))
        for row, (k, v) in enumerate(rows):
            label_item = QTableWidgetItem(DEVICE_INFO_LABELS.get(k, k))
            value_item = QTableWidgetItem(v)
            self._device_info_table.setItem(row, 0, label_item)
            self._device_info_table.setItem(row, 1, value_item)
        self._device_info_table.resizeColumnsToContents()

    def _add_error_finding(self, evt: LogEvent) -> None:
        # Avoid duplicate findings for the same error key within one run.
        if evt.key in self._handled_fixes:
            return
        self._handled_fixes.add(evt.key)

        title = f"mtkclient error: {evt.key}"
        summary = evt.message
        suggested = ""
        if evt.suggested_fix:
            suggested = f"\n    → Double-click to apply fix ({evt.suggested_fix})"

        text = f"{SEVERITY_ICONS[Severity.CRITICAL]} {title}\n    {summary}{suggested}"
        item = QListWidgetItem(text)
        item.setForeground(SEVERITY_COLORS[Severity.CRITICAL])
        item.setData(0x0100, evt.suggested_fix)  # store fix code for double-click handler
        self._findings_list.addItem(item)
        self._findings_list.scrollToBottom()

    def _on_finding_double_clicked(self, item: QListWidgetItem) -> None:
        fix = item.data(0x0100)
        if not fix:
            return
        if fix == "set_preloader":
            self._action_set_preloader()
        elif fix == "set_auth":
            self._action_set_auth()
        else:
            QMessageBox.information(
                self, "No automatic fix yet", f"Fix '{fix}' is recognised but not wired up yet."
            )

    def _action_set_preloader(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select stock preloader (preloader_<device>.bin)",
            os.environ.get("HOME", ""),
            "Preloader (preloader_*.bin);;All files (*)",
        )
        if not path:
            return
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "Not found", f"{p} does not exist.")
            return
        kind = identify_preloader(p)
        if kind is None:
            res = QMessageBox.question(
                self,
                "Unrecognised header",
                "File doesn't start with any known preloader magic "
                "(GFH MMM\\x01 / UFS_BOOT / EMMC_BOOT / COMBO_BOOT). Use anyway?",
            )
            if res != QMessageBox.StandardButton.Yes:
                return
            kind_note = "unknown magic"
        else:
            kind_note = f"detected: {kind}"

        os.environ["MTK_RESCUE_PRELOADER"] = str(p)
        QMessageBox.information(
            self,
            "Preloader set",
            f"MTK_RESCUE_PRELOADER set to:\n{p}\n  ({kind_note})\n\n"
            "The next 'Run / Arm recipe' will pass this to mtkclient. Re-run the recipe now.",
        )

    def _action_set_auth(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select auth file (auth_sv5.auth or similar)",
            os.environ.get("HOME", ""),
            "Auth (*.auth);;All files (*)",
        )
        if not path:
            return
        os.environ["MTK_RESCUE_AUTH"] = path
        QMessageBox.information(
            self,
            "Auth set",
            f"MTK_RESCUE_AUTH set to:\n{path}\n\n"
            "Note: mtkclient invocation needs --auth wiring; coming in the next iteration.",
        )

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
