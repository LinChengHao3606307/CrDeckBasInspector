"""用 Windows 低层鼠标钩子记录真实鼠标输入。

不走 Qt 事件过滤：实测在 Cloudflare 校验页跳转时，Python 事件过滤器会让
Qt6Core 原生崩溃（c0000005）。低层钩子只读系统输入，不介入 Qt 事件链。

记录的是屏幕物理像素坐标；文件首行 meta 带视图几何，分析时换算成 CSS。
"""

import ctypes
import json
import os
import time
from ctypes import wintypes

from PySide6.QtCore import QObject

WH_MOUSE_LL = 14

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_HWHEEL = 0x020E

MOUSE_ACTIONS = {
    WM_MOUSEMOVE: "move",
    WM_LBUTTONDOWN: "down",
    WM_LBUTTONUP: "up",
    WM_RBUTTONDOWN: "rdown",
    WM_RBUTTONUP: "rup",
    WM_MBUTTONDOWN: "mdown",
    WM_MBUTTONUP: "mup",
    WM_MOUSEWHEEL: "wheel",
    WM_HWHEEL: "hwheel",
    WM_XBUTTONDOWN: "xdown",
    WM_XBUTTONUP: "xup",
}

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                              ctypes.POINTER(wintypes.LPARAM))


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class Recorder(QObject):
    def __init__(self, view, base_dir, dispatcher, parent=None):
        super().__init__(parent)
        self._view = view
        self._base = base_dir
        self._submit = dispatcher.submit
        self._active = False
        self._path = None
        self._events = []
        self._meta = {}
        self._hook = None
        self._proc = None

    @property
    def active(self):
        return self._active

    # ---------- 对外：接口线程调用，内部调度到 Qt 主线程 ----------

    def start(self):
        return self._submit(self._start, timeout=15)

    def stop(self):
        return self._submit(self._stop, timeout=15)

    # ---------- Qt 主线程 ----------

    def _start(self, job):
        if self._active:
            return {"ok": True, "recording": True, "file": self._path}
        user32 = ctypes.windll.user32
        os.makedirs(self._base, exist_ok=True)
        self._path = os.path.join(self._base, time.strftime("rec_%Y%m%d_%H%M%S.jsonl"))
        self._events = []
        view = self._view
        origin = view.mapToGlobal(view.rect().topLeft())
        self._meta = {
            "type": "meta",
            "startedAt": time.time(),
            "view": {
                "screenX": origin.x(), "screenY": origin.y(),
                "width": view.width(), "height": view.height(),
                "zoom": float(view.zoomFactor() or 1.0),
                "dpr": float(view.devicePixelRatioF() or 1.0),
            },
        }
        self._proc = HOOKPROC(self._on_mouse)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        self._active = bool(self._hook)
        return {"ok": self._active, "recording": self._active, "file": self._path,
                "error": None if self._hook else "SetWindowsHookExW failed"}

    def _stop(self, job):
        if not self._active:
            return {"ok": False, "error": "当前没有在录制"}
        ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._proc = None
        self._active = False
        events = self._events
        self._events = []
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._meta, ensure_ascii=False) + "\n")
            prev_t = None
            for name, tap_ms, x, y, flags, data in events:
                entry = {"type": name, "t": tap_ms, "x": x, "y": y,
                         "flags": flags, "data": data}
                entry["dtMs"] = None if prev_t is None else tap_ms - prev_t
                prev_t = tap_ms
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        kinds = {}
        for name, *_ in events:
            kinds[name] = kinds.get(name, 0) + 1
        return {"ok": True, "recording": False, "file": self._path,
                "events": len(events), "debug": kinds}

    def _on_mouse(self, n_code, w_param, l_param):
        try:
            if n_code >= 0 and self._active:
                data = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                name = MOUSE_ACTIONS.get(int(w_param))
                if name:
                    self._events.append((name, int(data.time), int(data.pt.x),
                                         int(data.pt.y), int(data.flags),
                                         int(data.mouseData)))
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)
