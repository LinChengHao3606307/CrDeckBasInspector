"""虚拟指针：应用内独立的鼠标指针，带可选轨迹显示，不碰真实光标。

指针位置用页面 CSS 坐标保存。覆盖层是一块悬在浏览器视口正上方的无边框透明
窗口，鼠标事件穿透到下层页面，只负责画位置环和轨迹。覆盖层挂在主窗口名下、
不置顶，因此其他应用盖住浏览器时它也跟着被盖住。真实鼠标落在视口里、且该点
最上层窗口属于本应用时，指针才跟随真实位置；其余时间完全由自动化驱动。
"""

import ctypes
import os
import time
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

TRAIL_MAX = 4000        # 轨迹点上限，防止长时间运行后无限增长
FRAME_MS = 16           # 覆盖层重绘节拍（约 60fps）

_User32 = ctypes.windll.user32
_User32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_User32.GetWindowThreadProcessId.restype = wintypes.DWORD
_User32.WindowFromPoint.argtypes = [wintypes.POINT]
_User32.WindowFromPoint.restype = wintypes.HWND
_User32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_User32.ClientToScreen.restype = wintypes.BOOL
_User32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_User32.GetCursorPos.restype = wintypes.BOOL

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def _client_origin(hwnd):
    pt = wintypes.POINT(0, 0)
    if not _User32.ClientToScreen(wintypes.HWND(int(hwnd)), ctypes.byref(pt)):
        return 0, 0
    return int(pt.x), int(pt.y)


def _hwnd_pid(hwnd):
    try:
        hwnd = int(hwnd or 0)
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        if not _User32.GetWindowThreadProcessId(wintypes.HWND(hwnd),
                                                ctypes.byref(pid)):
            return 0
        return int(pid.value)
    except Exception:
        return 0


def _window_at(x, y):
    try:
        pt = wintypes.POINT(int(round(x)), int(round(y)))
        hwnd = _User32.WindowFromPoint(pt)
        return int(hwnd) if hwnd else 0
    except Exception:
        return 0


class PointerOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._owner = None
        self.hide()

    def paintEvent(self, event):
        owner = self._owner
        if owner is None or owner.pos is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if owner.trail_visible:
            prev = None
            for x, y, _ts in owner.trail:
                p = owner.local_point(x, y)
                if prev is not None:
                    painter.setPen(QPen(QColor(90, 190, 255, 150), 1.6,
                                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    painter.drawLine(prev, p)
                prev = p

        p = owner.local_point(owner.pos[0], owner.pos[1])
        if owner.pos_visible:
            painter.setPen(QPen(QColor(90, 190, 255, 225), 1.6))
            painter.setBrush(QColor(90, 190, 255, 38))
            painter.drawEllipse(p, 9.0, 9.0)

        if owner.ring_until > time.monotonic() * 1000.0:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(90, 190, 255, 190), 1.8))
            painter.drawEllipse(QRectF(p.x() - 13, p.y() - 13, 26, 26))
        painter.end()


class VirtualPointer(QObject):
    """独立指针状态：位置（页面 CSS 像素）、可见性、轨迹和覆盖层。"""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view
        self.pos = None
        self.trail = []
        self.pos_visible = False   # 位置显示：指针周围一圈蓝环
        self.trail_visible = False  # 轨迹显示：走过的路径一直保留，关掉开关才清空
        self.ring_until = 0.0
        self.following = False
        self._visible = False
        self._margin = 24  # padding so the ring stays whole at page edges
        self._overlay = PointerOverlay(parent if isinstance(parent, QWidget) else None)
        self._overlay._owner = self
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        view.installEventFilter(self)

    # ---------- 状态 ----------

    def status(self):
        rect = self._view_rect()
        native = wintypes.RECT()
        _User32.GetWindowRect(wintypes.HWND(int(self._overlay.winId())),
                              ctypes.byref(native))
        return {
            "visible": self._visible,
            "pos": self.pos_visible,
            "trail": self.trail_visible,
            "x": None if self.pos is None else round(self.pos[0], 1),
            "y": None if self.pos is None else round(self.pos[1], 1),
            "points": len(self.trail),
            "following": self.following,
            "viewRect": None if rect is None else [rect.x(), rect.y(), rect.width(), rect.height()],
            "overlayWidget": [self._overlay.x(), self._overlay.y(),
                              self._overlay.width(), self._overlay.height()],
            "overlayNative": [native.left, native.top,
                              native.right - native.left, native.bottom - native.top],
        }

    def show(self, pos=None, trail=None):
        """API 主动显示：两个开关都为关时至少显示位置环。"""
        if pos is not None:
            self.pos_visible = bool(pos)
        if trail is not None:
            self._set_trail(trail)
        if not (self.pos_visible or self.trail_visible):
            self.pos_visible = True
        self._open()

    def configure(self, pos=None, trail=None):
        """右侧开关驱动：两个开关都关就整块隐藏。"""
        if pos is not None:
            self.pos_visible = bool(pos)
        if trail is not None:
            self._set_trail(trail)
        if self.pos_visible or self.trail_visible:
            self._open()
        else:
            self.hide()

    def _set_trail(self, trail):
        self.trail_visible = bool(trail)
        if not self.trail_visible:
            self.trail = []

    def _open(self):
        self._visible = True
        self._sync_geometry()
        self._overlay.show()
        self._overlay.raise_()
        self._timer.start()

    def hide(self):
        self._visible = False
        self.trail = []
        self._timer.stop()
        self._overlay.hide()

    # ---------- 位置 ----------

    def move_to(self, x, y):
        if self.following:  # 真鼠标在视图内时，指针只听真鼠标的
            return
        self._set(x, y)

    def _set(self, x, y):
        x = min(1920.0, max(0.0, float(x)))
        y = min(1080.0, max(0.0, float(y)))
        if self._visible and self.trail_visible:
            if not self.trail or abs(x - self.trail[-1][0]) > 0.5 or abs(y - self.trail[-1][1]) > 0.5:
                now = time.monotonic() * 1000.0
                self.trail.append((x, y, now))
                if len(self.trail) > TRAIL_MAX:
                    del self.trail[:len(self.trail) - TRAIL_MAX]
        self.pos = (x, y)

    def click_flash(self):
        self.ring_until = time.monotonic() * 1000.0 + 260.0

    # ---------- 几何 ----------

    def _zoom(self):
        try:
            z = float(self._view.zoomFactor())
            return z if z > 0 else 1.0
        except Exception:
            return 1.0

    def local_point(self, x, y):
        z = self._zoom()
        return QPointF(x * z + self._margin, y * z + self._margin)

    def _view_origin(self):
        """Return the view's native client origin in screen device pixels."""
        return _client_origin(self._view.winId())

    def _sync_geometry(self):
        view = self._view
        if view is None or not view.isVisible():
            return
        dpr = float(view.devicePixelRatioF() or 1.0)
        x, y = self._view_origin()
        m = int(round(self._margin * dpr))
        w = int(round(view.width() * dpr)) + 2 * m
        h = int(round(view.height() * dpr)) + 2 * m
        if w < 1 or h < 1:
            return
        overlay = self._overlay
        hwnd = wintypes.HWND(int(overlay.winId()))
        native = wintypes.RECT()
        _User32.GetWindowRect(hwnd, ctypes.byref(native))
        if (native.left == x - m and native.top == y - m and
                native.right - native.left == w and native.bottom - native.top == h):
            return
        _User32.SetWindowPos(hwnd, wintypes.HWND(0), int(x - m), int(y - m), w, h,
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _tick(self):
        self.following = self._apply_cursor()
        if self._visible and self._view is not None and self._view.isVisible():
            self._sync_geometry()
        self._overlay.update()

    # ---------- 跟随真实鼠标 ----------
    # 用真实光标的系统位置判断，而不是事件来源：无论窗口消息来自真人还是
    # 自动化，只有真光标落在视口矩形内时指针才跟随。
    def _view_rect(self):
        view = self._view
        if view is None or not view.isVisible():
            return None
        x, y = self._view_origin()
        dpr = float(view.devicePixelRatioF() or 1.0)
        return QRect(x, y, int(round(view.width() * dpr)),
                     int(round(view.height() * dpr)))

    def _over_own_window(self, p):
        """光标处最上层的窗口属于本进程时才算压在浏览器上。

        只看几何矩形会把被其他应用盖住的视口也当成操作区，真实鼠标在别的
        应用窗口里划过那块屏幕区域时指针就会跟着跳。
        """
        try:
            own = _hwnd_pid(int(self._view.window().winId())) or os.getpid()
        except Exception:
            return True
        under = _hwnd_pid(_window_at(p.x(), p.y()))
        if not under:
            return True
        return under == own

    def _apply_cursor(self):
        rect = self._view_rect()
        if rect is None:
            return False
        pt = wintypes.POINT()
        if not _User32.GetCursorPos(ctypes.byref(pt)):
            return False
        p = QPoint(pt.x, pt.y)
        if not rect.contains(p) or not self._over_own_window(p):
            return False
        z = self._zoom()
        dpr = float(self._view.devicePixelRatioF() or 1.0)
        self._set((p.x() - rect.x()) / (z * dpr),
                  (p.y() - rect.y()) / (z * dpr))
        return True

    _MOUSE = (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
              QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick,
              QEvent.Type.Enter, QEvent.Type.Leave)

    def eventFilter(self, obj, event):
        et = event.type()
        if et not in self._MOUSE:
            return False
        self.following = self._apply_cursor()
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            self.click_flash()
        return False
