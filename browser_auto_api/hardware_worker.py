"""独立硬件输入进程：用真实 SendInput 事件点击嵌入式浏览器。

为什么必须是独立进程：宿主 Qt 进程在 150% 缩放的桌面上是 DPI 不感知的
（GetSystemMetrics 被系统虚拟化到 1280x720），进程内既不能可靠地切换
DPI 感知级别，绝对坐标 SendInput 也会被虚拟化偏 1.5 倍。本进程启动即
声明 per-monitor v2 感知，在物理像素空间完成全部换算：

    物理目标 = ClientToScreen(视图 hwnd, 0,0) + CSS * zoom * (dpi / 96)

只依赖标准库（不导入 PySide6），避免在同一 user-data-dir 上再起一套
Qt/Chromium 进程。结果以单行 JSON 打到 stdout，非零退出码表示失败。

用法：
    python hardware_worker.py click --view-hwnd <hwnd> --css <x> <y>
                                    --zoom <z> [--away <x> <y>]
"""

import argparse
import ctypes
import json
import os
import random
import sys
import time
from ctypes import wintypes

_T0 = time.perf_counter()


def _dbg(phase, debug):
    if debug:
        sys.stderr.write("[hwdbg] +%.3f pid=%d %s\n"
                         % (time.perf_counter() - _T0, os.getpid(), phase))
        sys.stderr.flush()

# ---- DPI 感知：必须在任何窗口/坐标调用之前声明 ----
DPI_STATUS = "unset"
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    DPI_STATUS = "per-monitor-v2"
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        DPI_STATUS = "shcore-per-monitor"
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            DPI_STATUS = "system-dpi-aware"
        except Exception:
            DPI_STATUS = "failed"

u32 = ctypes.windll.user32

# 64 位下 HWND 是指针宽度，默认 restype=c_int 会截断句柄，必须显式声明。
u32.GetForegroundWindow.restype = wintypes.HWND
u32.GetAncestor.restype = wintypes.HWND
u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
u32.ClientToScreen.restype = wintypes.BOOL
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetWindowRect.restype = wintypes.BOOL
u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
u32.GetCursorPos.restype = wintypes.BOOL
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
u32.AttachThreadInput.restype = wintypes.BOOL
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SetForegroundWindow.restype = wintypes.BOOL
u32.BringWindowToTop.argtypes = [wintypes.HWND]
u32.BringWindowToTop.restype = wintypes.BOOL
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.ShowWindow.restype = wintypes.BOOL
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetSystemMetrics.restype = ctypes.c_int
u32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
u32.SendInput.restype = wintypes.UINT
u32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
u32.EnumChildWindows.restype = wintypes.BOOL
u32.WindowFromPoint.argtypes = [wintypes.POINT]
u32.WindowFromPoint.restype = wintypes.HWND
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetClassNameW.restype = ctypes.c_int

k32 = ctypes.windll.kernel32
k32.GetCurrentThreadId.restype = wintypes.DWORD


def _hwnd_int(hwnd):
    """HWND 归一化为 Python int。

    restype=c_void_p 的调用直接返回 int/None；显式构造的 wintypes.HWND
    是 c_void_p 实例，而 Python 3.12 下 int(c_void_p 实例) 会尝试解析其
    原始缓冲区字节并抛 ValueError，必须走 .value。
    """
    if hwnd is None:
        return 0
    value = getattr(hwnd, "value", hwnd)
    return int(value) if value is not None else 0

GA_ROOT = 2
SW_RESTORE = 9
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _abs_xy(x, y):
    """归一化到整个虚拟桌面；单显示器时与主屏幕归一化完全等价。"""
    vx = u32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = u32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return (int((x - vx) * 65535 / max(1, vw - 1)),
            int((y - vy) * 65535 / max(1, vh - 1)))


def _send_move(x, y):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dx, inp.mi.dy = _abs_xy(x, y)
    inp.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    return int(u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))) == 1


def _send_button(flag):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = flag
    return int(u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))) == 1


def _foreground(root):
    """把宿主顶层窗口提到前台。

    优先直接 SetForegroundWindow（实证成功路径）。只有失败时才用
    AttachThreadInput 挂靠前台线程输入队列后重试——绝不注入 ALT 键事件：
    Turnstile 采集键盘遥测，没有配对 keydown 的孤立 Alt keyup 是典型的
    机器人信号，会让本可通过的真实鼠标事件被服务端拒绝。
    """
    u32.ShowWindow(root, SW_RESTORE)
    u32.SetForegroundWindow(root)
    u32.BringWindowToTop(root)
    time.sleep(0.3)
    if _hwnd_int(u32.GetForegroundWindow()) == _hwnd_int(root):
        return True

    fg = u32.GetForegroundWindow()
    attached = wintypes.DWORD(0)
    if fg and _hwnd_int(fg) != _hwnd_int(root):
        pid = wintypes.DWORD(0)
        fg_thread = u32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        cur_thread = k32.GetCurrentThreadId()
        if fg_thread and fg_thread != cur_thread:
            ok = u32.AttachThreadInput(cur_thread, fg_thread, True)
            attached = fg_thread if ok else wintypes.DWORD(0)
    u32.ShowWindow(root, SW_RESTORE)
    u32.SetForegroundWindow(root)
    u32.BringWindowToTop(root)
    if attached:
        u32.AttachThreadInput(k32.GetCurrentThreadId(), attached, False)
    time.sleep(0.5)
    return _hwnd_int(u32.GetForegroundWindow()) == _hwnd_int(root)


def _glide(tx, ty, steps=None, settle_range=(0.35, 0.6)):
    """从屏幕物理中心，带缓动和轻微弧度滑向目标。

    起点固定为屏幕中心而非当前光标位置——这是两次实证成功（人工实验与
    A/B 复刻）共同采用的轨迹；首帧 SendInput 绝对移动本身就会把光标带到
    轨迹起点，页面看到的是一条完整的弧形人手轨迹。
    """
    sx, sy = u32.GetSystemMetrics(0) // 2, u32.GetSystemMetrics(1) // 2
    n = steps or random.randint(12, 16)
    for i in range(1, n + 1):
        p = i / float(n)
        ease = p * p * (3 - 2 * p)
        ix = sx + (tx - sx) * ease
        iy = sy + (ty - sy) * ease - 30 * (1 - p) * p
        if not _send_move(int(ix), int(iy)):
            raise RuntimeError("SendInput move failed at step %d" % i)
        time.sleep(random.uniform(0.018, 0.034))
    _send_move(tx, ty)
    if settle_range:
        time.sleep(random.uniform(*settle_range))


def _click():
    if not _send_button(MOUSEEVENTF_LEFTDOWN):
        raise RuntimeError("SendInput LEFTDOWN failed")
    time.sleep(random.uniform(0.05, 0.09))
    if not _send_button(MOUSEEVENTF_LEFTUP):
        raise RuntimeError("SendInput LEFTUP failed")


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _window_rect(hwnd):
    rect = wintypes.RECT()
    if not u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect


def _resolve_render_rect(view_hwnd, expected):
    """在视图的全部后代窗口里找与预期物理矩形一致的渲染表面。

    QtWebEngine 的渲染窗口类名与普通 Qt 窗口相同（都是 Qt6120QWindowIcon），
    无法按类名识别；它的唯一可靠特征是矩形恰好等于页面绘制区
    （view.mapToGlobal(0,0) 与 view 尺寸乘以系统 DPI 缩放）。
    没有足够接近的后代时直接采用预期矩形——它本就来自 Qt 自己的几何数据。
    """
    exp = tuple(int(v) for v in expected)
    best = None
    best_score = None

    @WNDENUMPROC
    def cb(hwnd, _):
        nonlocal best, best_score
        rect = _window_rect(hwnd)
        if rect is None:
            return True
        got = (rect.left, rect.top, rect.right, rect.bottom)
        score = sum(abs(a - b) for a, b in zip(got, exp))
        if best_score is None or score < best_score:
            best_score, best = score, got
        return True

    u32.EnumChildWindows(wintypes.HWND(view_hwnd), cb, 0)
    if best is not None and best_score <= 8:
        return best, True, best_score
    return exp, False, best_score


def _physical_target(rect, view_w, view_h, css_x, css_y):
    """CSS 坐标按视口比例映射到渲染表面的物理矩形。

    渲染矩形宽 = view_w * zoom * 系统 DPI 缩放，比例映射与该乘积等价，
    因此完全不需要调用任何 DPI API（GetDpiForWindow 对 DPI 不感知属主
    的窗口只会返回 96，反而是陷阱）。
    """
    px = rect[0] + int(round(css_x / view_w * (rect[2] - rect[0])))
    py = rect[1] + int(round(css_y / view_h * (rect[3] - rect[1])))
    return px, py


def _class_of(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    n = u32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n else ""


def _point_diag(x, y, root):
    """点击瞬间的现场取证：光标位置、前台窗口、目标点下最顶层窗口及其
    祖先链。若目标点下的窗口不属于宿主根，说明点击被别的窗口截走了。"""
    cur = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(cur))
    fg = u32.GetForegroundWindow()
    under = u32.WindowFromPoint(wintypes.POINT(x, y))
    under_root = u32.GetAncestor(under, GA_ROOT) if under else None
    return {
        "cursor": [cur.x, cur.y],
        "foreground": _hwnd_int(fg),
        "foregroundClass": _class_of(fg),
        "root": _hwnd_int(root),
        "windowUnderPoint": _hwnd_int(under),
        "underClass": _class_of(under),
        "underRoot": _hwnd_int(under_root),
        "underIsHost": _hwnd_int(under_root) == _hwnd_int(root),
    }


def click(view_hwnd, css, view_size, expected_rect, away, debug=False):
    _dbg("enter view_hwnd=%r" % (view_hwnd,), debug)
    root = u32.GetAncestor(wintypes.HWND(view_hwnd), GA_ROOT) or wintypes.HWND(view_hwnd)
    _dbg("ancestor root=%r" % (_hwnd_int(root),), debug)
    foreground = _foreground(root)
    _dbg("foreground done ok=%s" % foreground, debug)

    rect, matched, match_score = _resolve_render_rect(view_hwnd, expected_rect)
    _dbg("rect resolved matched=%s score=%r rect=%s" % (matched, match_score, rect), debug)
    view_w, view_h = view_size
    tx, ty = _physical_target(rect, view_w, view_h, css[0], css[1])
    _dbg("target=(%d,%d)" % (tx, ty), debug)

    # 安全护栏：目标必须落在宿主顶层窗口内，防止坐标换算错误时点到别的程序。
    # 取宿主矩形失败时按失败处理（fail-closed），绝不带疑问发送输入。
    host = wintypes.RECT()
    if not u32.GetWindowRect(root, ctypes.byref(host)):
        raise RuntimeError("GetWindowRect failed for root hwnd %r" % (_hwnd_int(root),))
    inside = host.left <= tx < host.right and host.top <= ty < host.bottom
    if not inside:
        raise RuntimeError(
            "target (%d,%d) outside host window %s" % (tx, ty, (host.left, host.top,
                                                                 host.right, host.bottom)))
    _dbg("guard ok, gliding to target", debug)
    _glide(tx, ty)
    _dbg("glide done, pressing", debug)
    diag_before = _point_diag(tx, ty, root)
    _click()
    _dbg("click done, holding still", debug)
    time.sleep(0.25)
    diag_after = _point_diag(tx, ty, root)

    # 实证成功路径在 mouseup 后让光标完全静止、立刻挪开反而会打断验证。
    # --away 参数保留兼容（后端仍用于同步虚拟指针覆盖层），但不再驱动真实
    # 光标产生任何移动事件。
    away_phys = None
    _dbg("return", debug)

    return {
        "ok": True,
        "renderRect": list(rect),
        "rectMatched": matched,
        "rectMatchScore": match_score,
        "target": [tx, ty],
        "root": _hwnd_int(root),
        "foreground": foreground,
        "away": away_phys,
        "diagBefore": diag_before,
        "diagAfter": diag_after,
    }


def _selftest():
    """不触碰任何窗口/输入：只证明解释器启动、模块导入（含 DPI 声明）成功，
    并上报本进程看到的屏幕尺寸与缩放，用于子进程启动类故障诊断。"""
    try:
        pct = ctypes.windll.shcore.GetScaleFactorForDevice(0)
    except Exception:
        pct = None
    return {
        "ok": True,
        "pid": os.getpid(),
        "dpiStatus": DPI_STATUS,
        "scalePercent": pct,
        "screenW": u32.GetSystemMetrics(0),
        "screenH": u32.GetSystemMetrics(1),
        "stdinIsatty": sys.stdin.isatty() if sys.stdin is not None else None,
        "cwd": os.getcwd(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hardware SendInput worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    p = sub.add_parser("click")
    p.add_argument("--view-hwnd", type=int, required=True)
    p.add_argument("--css", nargs=2, type=float, required=True, metavar=("X", "Y"))
    p.add_argument("--view-size", nargs=2, type=float, required=True,
                   metavar=("CSS_W", "CSS_H"))
    p.add_argument("--expect-rect", nargs=4, type=int, required=True,
                   metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    p.add_argument("--away", nargs=2, type=float, metavar=("X", "Y"))
    p.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        print(json.dumps(_selftest(), ensure_ascii=False))
        return 0
    if args.cmd != "click":  # pragma: no cover - argparse 已保证
        parser.error("unknown command")
    _dbg("process start, argv parsed", args.debug)
    try:
        result = click(args.view_hwnd, args.css, args.view_size,
                       args.expect_rect, args.away, debug=args.debug)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
