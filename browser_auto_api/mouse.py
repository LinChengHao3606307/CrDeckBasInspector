"""鼠标动作：向 QWebEngineView 投递真实 Qt 鼠标事件，移动轨迹用贝塞尔曲线模拟人手。

不向页面注入合成 JS 事件，因此链接跳转、表单提交、输入框聚焦、悬停、拖动等
默认行为都由 Chromium 正常处理，和真浏览器一致。
"""

import ctypes
import math
import random
import time
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QPoint, QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

HOVER_KEEP_S = 1.2  # 投递移动后这段时间内吞掉 WM_MOUSELEAVE

WM_MOUSEMOVE = 0x0200
WM_MOUSELEAVE = 0x02A3
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

_MK = {"left": 0x0001, "right": 0x0002, "middle": 0x0010}
_MSG_DOWN = {"left": WM_LBUTTONDOWN, "right": WM_RBUTTONDOWN, "middle": WM_MBUTTONDOWN}
_MSG_UP = {"left": WM_LBUTTONUP, "right": WM_RBUTTONUP, "middle": WM_MBUTTONUP}

def _stamp(event):
    """打上事件的真实时间戳；全 0 会让 Chromium 认为所有输入同时发生。"""
    event.setTimestamp(int(time.monotonic() * 1000))
    return event


class _HoverKeeper(QAbstractNativeEventFilter):
    """真光标不在视口时，Qt 每处理一条投递的移动就立刻收到 WM_MOUSELEAVE，
    视口状态在「离开/进入」之间反复跳动，页面看到一路 mouseleave/mouseenter。
    自动化移动期间把这条消息吞掉，让悬停状态像真鼠标连续停留一样稳定。"""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):
        try:
            if b"windows" not in bytes(event_type):
                return False, 0
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_MOUSELEAVE and \
                    time.monotonic() - self._owner.last_move_post() < HOVER_KEEP_S:
                return True, 0
        except Exception:
            pass
        return False, 0


_NO_BUTTON = Qt.MouseButton.NoButton
_LEFT = Qt.MouseButton.LeftButton
_NO_MOD = Qt.KeyboardModifier.NoModifier

_BUTTONS = {
    "left": Qt.MouseButton.LeftButton,
    "right": Qt.MouseButton.RightButton,
    "middle": Qt.MouseButton.MiddleButton,
}


class MouseController(QObject):
    """一次执行一个鼠标动作；多个动作由调用方串行排队。"""

    finished = Signal(str, dict)

    def __init__(self, view, pointer=None, parent=None):
        super().__init__(parent)
        self._view = view
        self._pointer = pointer
        self._native = False
        self._pos = None
        self._token = None
        self._ops = []
        self._op_index = 0
        self._result = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._run_next)
        self._last_move_post = 0.0
        app = QApplication.instance()
        self._hover_keeper = None
        if app is not None:
            self._hover_keeper = _HoverKeeper(self)
            app.installNativeEventFilter(self._hover_keeper)

    def last_move_post(self):
        return self._last_move_post

    # ---------- 对外 ----------

    def start(self, action: dict, token: str) -> None:
        self._view.setFocus()
        self._token = token
        self._native = bool(action.get("native", False))
        self._result = {"token": token, "action": action.get("action")}
        x = float(action.get("x", self._center()[0]))
        y = float(action.get("y", self._center()[1]))
        name = str(action.get("action", "move")).lower()
        button = _BUTTONS.get(str(action.get("button", "left")).lower(), _LEFT)
        jitter = action.get("human_jitter", True)

        # 显式轨迹：按给定点位和固定间隔连续移动，用于复刻真人高频鼠标流
        pts = action.get("points")
        if isinstance(pts, list) and pts:
            step = max(1, int(action.get("step_ms", 4)))
            for p in pts:
                px, py = float(p[0]), float(p[1])
                self._ops.append((step, lambda a=px, b=py: self._emit_move(a, b, _NO_BUTTON)))
            self._result.update({"x": float(pts[-1][0]), "y": float(pts[-1][1])})
            self._op_index = 0
            self._timer.start(1)
            self._run_next()
            return

        if name == "move":
            self._plan_path(x, y, _NO_BUTTON, jitter)
        elif name in ("click", "dblclick"):
            self._plan_path(x, y, _NO_BUTTON, jitter, approach_pause=True)
            clicks = 2 if name == "dblclick" else 1
            for _ in range(clicks):
                self._ops.append((int(random.uniform(30, 70)), lambda xx=x, yy=y, b=button: self._emit_press(xx, yy, b)))
                self._ops.append((int(random.uniform(45, 110)), lambda xx=x, yy=y, b=button: self._emit_release(xx, yy, b)))
                if name == "dblclick":
                    self._ops.append((int(random.uniform(10, 30)), lambda xx=x, yy=y, b=button: self._emit_dblclick(xx, yy, b)))
                self._ops.append((int(random.uniform(70, 140)), lambda: None))
        elif name == "down":
            self._plan_path(x, y, _NO_BUTTON, jitter, approach_pause=True)
            self._ops.append((int(random.uniform(20, 50)), lambda xx=x, yy=y, b=button: self._emit_press(xx, yy, b)))
        elif name == "up":
            self._plan_path(x, y, button, jitter, approach_pause=True)
            self._ops.append((int(random.uniform(20, 50)), lambda xx=x, yy=y, b=button: self._emit_release(xx, yy, b)))
        elif name == "drag":
            fx, fy = float(action.get("fromX", x)), float(action.get("fromY", y))
            tx, ty = float(action.get("toX", x)), float(action.get("toY", y))
            self._plan_path(fx, fy, _NO_BUTTON, jitter, approach_pause=True)
            self._ops.append((int(random.uniform(30, 60)), lambda a=fx, b=fy, c=button: self._emit_press(a, b, c)))
            self._ops.append((int(random.uniform(80, 160)), lambda: None))
            self._plan_path(tx, ty, button, jitter, start_from=(fx, fy))
            self._ops.append((int(random.uniform(40, 90)), lambda a=tx, b=ty, c=button: self._emit_release(a, b, c)))
        elif name == "wheel":
            dy = float(action.get("deltaY", 0))
            dx = float(action.get("deltaX", 0))
            self._plan_path(x, y, _NO_BUTTON, jitter, approach_pause=True)
            self._ops.append((int(random.uniform(60, 120)), lambda xx=x, yy=y, a=dx, b=dy: self._emit_wheel(xx, yy, a, b)))
        else:
            self._result["error"] = "unknown action: %s" % name

        self._op_index = 0
        self._finish_after_ops = True
        self._result.update({"x": x, "y": y})
        self._timer.start(10)
        self._run_next()

    # ---------- 内部 ----------

    def _center(self):
        return (self._view.width() / 2.0, self._view.height() / 2.0)

    def _zoom(self):
        try:
            z = float(self._view.zoomFactor())
            return z if z > 0 else 1.0
        except Exception:
            return 1.0

    def _target(self):
        return self._view.focusProxy() or self._view

    def _widget_pos(self, x, y):
        z = self._zoom()
        return QPointF(x * z, y * z)

    def _global_pos(self, x, y):
        p = self._widget_pos(x, y)
        return QPointF(self._view.mapToGlobal(QPoint(int(p.x()), int(p.y()))))

    # ---------- 原生窗口消息模式 ----------
    # Cloudflare Turnstile 会拒绝 Qt 合成事件（QApplication.postEvent），
    # 只认经窗口消息进入 Chromium 的输入；native=True 时投 WM_ 消息。
    #
    # Qt 把消息 client 坐标按物理像素解析（再除以 dpr 得逻辑位置），所以 lParam
    # 取「目标逻辑全局坐标 × dpr − 客户区物理原点」。指针停在别处时，页面收到的
    # client/screen 与真鼠标移到该点完全一致，因此不必再 SetCursorPos，真实光标
    # 不会被抢走。

    def _native_coords(self, x, y):
        view = self._view
        z = self._zoom()
        dpr = float(view.devicePixelRatioF() or 1.0)
        hwnd = int(view.window().winId())
        u = ctypes.windll.user32
        view_pt = wintypes.POINT(0, 0)
        window_pt = wintypes.POINT(0, 0)
        u.ClientToScreen(wintypes.HWND(int(view.winId())), ctypes.byref(view_pt))
        u.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(window_pt))
        cx = view_pt.x + int(round(x * z * dpr)) - window_pt.x
        cy = view_pt.y + int(round(y * z * dpr)) - window_pt.y
        return hwnd, cx, cy

    def native_math(self, x, y):
        """报告原生投递前的全部坐标换算值，用于诊断 DPI/窗口偏移。"""
        view = self._view
        hwnd, cx, cy = self._native_coords(x, y)
        u = ctypes.windll.user32
        frame = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(frame))
        gp = view.mapToGlobal(QPoint(int(round(x * self._zoom())),
                                     int(round(y * self._zoom()))))
        return {
            "zoom": self._zoom(),
            "dpr": float(view.devicePixelRatioF() or 1.0),
            "hwnd": hwnd,
            "gp": [gp.x(), gp.y()],
            "frame": [frame.left, frame.top, frame.right, frame.bottom],
            "L": [cx, cy],
            "viewPos": [view.mapToGlobal(QPoint(0, 0)).x(),
                        view.mapToGlobal(QPoint(0, 0)).y()],
        }

    def _native_post(self, msg, x, y, wparam=0):
        hwnd, cx, cy = self._native_coords(x, y)
        if msg == WM_MOUSEMOVE:
            self._last_move_post = time.monotonic()
        ctypes.windll.user32.PostMessageW(hwnd, msg, wparam,
                                          ((cy & 0xFFFF) << 16) | (cx & 0xFFFF))
        self._sync_pointer(x, y)

    @staticmethod
    def _name(button):
        for name, qt in _BUTTONS.items():
            if qt == button:
                return name
        return "left"

    def _sync_pointer(self, x, y):
        if self._pointer is not None:
            self._pointer.move_to(x, y)

    def _emit_move(self, x, y, buttons):
        if self._native:
            mk = 0
            for name, qt in _BUTTONS.items():
                if buttons & qt:
                    mk |= _MK[name]
            # wParam 保持真实的按键状态（悬停为 0）；真光标在窗外时
            # _HoverKeeper 会吞掉 WM_MOUSELEAVE，hover 状态不会被反复清掉。
            self._native_post(WM_MOUSEMOVE, x, y, mk)
            self._pos = (x, y)
            return
        ev = _stamp(QMouseEvent(QMouseEvent.Type.MouseMove, self._widget_pos(x, y), self._global_pos(x, y),
                         _NO_BUTTON, buttons, _NO_MOD))
        QApplication.postEvent(self._target(), ev)
        self._pos = (x, y)
        self._sync_pointer(x, y)

    def _emit_press(self, x, y, button):
        if self._native:
            name = self._name(button)
            self._native_post(WM_MOUSEMOVE, x, y, _MK[name])
            self._native_post(_MSG_DOWN[name], x, y, _MK[name])
            self._pos = (x, y)
            return
        self._emit_move(x, y, _NO_BUTTON)
        ev = _stamp(QMouseEvent(QMouseEvent.Type.MouseButtonPress, self._widget_pos(x, y), self._global_pos(x, y),
                         button, button, _NO_MOD))
        QApplication.postEvent(self._target(), ev)
        self._pos = (x, y)
        self._sync_pointer(x, y)
        if self._pointer is not None:
            self._pointer.click_flash()

    def _emit_release(self, x, y, button):
        if self._native:
            name = self._name(button)
            self._native_post(_MSG_UP[name], x, y, 0)
            self._pos = (x, y)
            return
        ev = _stamp(QMouseEvent(QMouseEvent.Type.MouseButtonRelease, self._widget_pos(x, y), self._global_pos(x, y),
                         button, _NO_BUTTON, _NO_MOD))
        QApplication.postEvent(self._target(), ev)
        self._pos = (x, y)
        self._sync_pointer(x, y)

    def _emit_dblclick(self, x, y, button):
        if self._native and self._name(button) == "left":
            self._native_post(WM_LBUTTONDBLCLK, x, y, _MK["left"])
            self._pos = (x, y)
            return
        ev = _stamp(QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, self._widget_pos(x, y), self._global_pos(x, y),
                         button, button, _NO_MOD))
        QApplication.postEvent(self._target(), ev)
        self._pos = (x, y)

    def _emit_wheel(self, x, y, dx, dy):
        self._emit_move(x, y, _NO_BUTTON)
        ang = QPoint(int(round(dx)), int(round(dy)))
        ev = _stamp(QWheelEvent(self._widget_pos(x, y), self._global_pos(x, y), QPoint(), ang,
                         _NO_BUTTON, _NO_MOD, Qt.ScrollPhase.NoScrollPhase, False))
        QApplication.postEvent(self._target(), ev)
        self._pos = (x, y)

    def _start_point(self):
        if self._pos is not None:
            return self._pos
        cx, cy = self._center()
        return (cx, cy)

    def _plan_path(self, tx, ty, buttons, jitter=True, approach_pause=False, start_from=None):
        sx, sy = start_from if start_from else self._start_point()
        points = human_path(sx, sy, tx, ty) if jitter else [(tx, ty)]
        # 真人鼠标事件间隔中位约 16ms（60~90Hz）：原生和非原生都按这个节拍，
        # 偶尔插入一次几十毫秒的停顿，模拟手部换段的间隙。
        lo, hi = (12, 20)
        for i, (px, py) in enumerate(points):
            delay = int(random.uniform(lo, hi))
            if i and random.random() < 0.06:
                delay += int(random.uniform(20, 70))
            if i and random.random() < 0.015:
                delay += int(random.uniform(90, 220))
            self._ops.append((delay, lambda a=px, b=py, c=buttons: self._emit_move(a, b, c)))
        if approach_pause:
            pause = (130, 260) if self._native else (110, 230)
            self._ops.append((int(random.uniform(*pause)), lambda: None))
        # 终点精确定位
        self._ops.append((int(random.uniform(lo, hi)), lambda a=tx, b=ty, c=buttons: self._emit_move(a, b, c)))

    def _run_next(self):
        if self._op_index >= len(self._ops):
            self._finish()
            return
        delay, fn = self._ops[self._op_index]
        self._op_index += 1
        try:
            fn()
        except Exception as exc:
            self._result["error"] = repr(exc)
        self._timer.start(max(1, delay))

    def _finish(self):
        token, result = self._token, dict(self._result)
        self._token = None
        self._ops = []
        self._op_index = 0
        self._timer.stop()
        if token is not None:
            self.finished.emit(token, result)


def human_path(x0, y0, x1, y1):
    """人手移动轨迹：先冲后刹的速度曲线、弧形路径、随速度变化的手抖。

    参数取自真人录像（事件间隔中位约 16ms，平均速度 0.1~0.4 px/ms，
    一次移动 0.1~1.5 秒，轨迹偏离首尾连线可达数十像素）。返回逐点位置，
    调用方按 ~16ms 的节拍投递。
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 3:
        return [(x1, y1)]

    # 平均速度：短距离犹豫，长距离甩得更快
    if dist < 120:
        v_avg = random.uniform(0.08, 0.18)
    else:
        v_avg = random.uniform(0.16, 0.42)
    duration_ms = min(2200.0, max(100.0, dist / v_avg))
    n = max(4, int(round(duration_ms / 16.0)))

    # 弧线：垂直方向随机弓起；长距离偶尔走 S 形
    nx, ny = -dy / dist, dx / dist
    bow = max(-0.22 * dist, min(0.22 * dist, random.gauss(0.0, 0.09 * dist)))
    bow2 = 0.0
    if dist > 220 and random.random() < 0.25:
        bow2 = max(-0.12 * dist, min(0.12 * dist, random.gauss(0.0, 0.05 * dist)))

    # 速度曲线：峰值落在约 35% 处（先冲后刹车），末段几乎停下
    a, b = 1.15, 2.1
    weights = []
    for i in range(1, n + 1):
        t = i / float(n)
        weights.append((t ** a) * (max(0.0, 1.0 - t) ** b) + 1e-6)
    total = sum(weights)
    peak = max(weights)

    points = []
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        p = min(1.0, acc / total)
        off = bow * math.sin(math.pi * p) + bow2 * math.sin(2.0 * math.pi * p)
        bx = x0 + dx * p + nx * off
        by = y0 + dy * p + ny * off
        # 手抖：速度越快抖得越明显；偶尔一次较大的抖动
        f = w / peak
        amp = 0.22 + 0.95 * f
        if random.random() < 0.05:
            amp *= random.uniform(2.0, 3.6)
        bx += random.gauss(0.0, amp)
        by += random.gauss(0.0, amp)
        if i < n - 1:
            points.append((bx, by))
    # 收尾：悬停微调一下再精确落到目标
    if dist > 20:
        points.append((x1 + random.uniform(-0.9, 0.9),
                       y1 + random.uniform(-0.9, 0.9)))
    points.append((x1, y1))
    return points
