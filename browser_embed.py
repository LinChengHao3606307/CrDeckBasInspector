"""Entry point for the embedded browser process.

Launched by app.py with --parent-hwnd <Tk frame HWND>; the Qt window then
reparents itself into that frame and reports events/logs on stdout.
"""

import argparse
import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-hwnd", type=int, required=True)
    args = parser.parse_args()

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("CrDeckBasInspector Browser")

    from browser_auto_api.window import MainWindow

    MainWindow(embed_parent=args.parent_hwnd)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
