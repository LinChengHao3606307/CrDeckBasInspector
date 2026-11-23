"""Entry point for the embedded browser process.

Launched by app.py with --parent-hwnd <Tk frame HWND>; the Qt window then
reparents itself into that frame and reports events/logs on stdout.
"""

import argparse
import os
import sys

# QtWebEngine 不向 Blink 上报指针设备，页面里 matchMedia('(any-pointer: fine)')
# 恒为 none——一个能产生鼠标事件的浏览器却自称没有指针，是 Turnstile 的强信号。
# 在 QApplication / QtWebEngine 初始化前用 blink-settings 固定为 fine 指针+悬停
# （枚举值 4=fine 指针，2=hover，本机实测与真实桌面 Chrome 读数一致）。
_chromium_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
if "primaryPointerType" not in _chromium_flags:
    _pointer_fix = ("--blink-settings=primaryPointerType=4,"
                    "availablePointerTypes=4,primaryHoverType=2,"
                    "availableHoverTypes=2")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        _chromium_flags + " " + _pointer_fix).strip()

# Intl 默认 locale 由 Chromium 应用语言决定，QtWebEngine 默认只给 "en"（无地区），
# 真 Chrome 恒为完整标签（本机基线 en-US）。用原生开关固定，避免再加 JS 补丁。
if "--lang=" not in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] + " --lang=en-US").strip()

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
