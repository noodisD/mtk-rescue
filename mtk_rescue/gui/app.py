import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("mtk-rescue")
    app.setApplicationDisplayName("mtk-rescue")
    win = MainWindow()
    win.show()
    return app.exec()
