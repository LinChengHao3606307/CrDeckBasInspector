"""Win32 helpers that reparent a Qt window into a Tk host window.

The Tk app owns the layout; the Qt browser runs in a separate process and is
attached as a child window of a Tk frame, then kept in sync with its client area.
"""

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GWL_STYLE = -16
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_VISIBLE = 0x10000000

_GetWindowLong = getattr(_user32, "GetWindowLongPtrW", None) or _user32.GetWindowLongW
_SetWindowLong = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW

_GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
_GetWindowLong.restype = ctypes.c_ssize_t
_SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_SetWindowLong.restype = ctypes.c_ssize_t

_user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
_user32.SetParent.restype = wintypes.HWND
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.BOOL]
_user32.MoveWindow.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.restype = wintypes.BOOL

_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.SetFocus.argtypes = [wintypes.HWND]
_user32.SetFocus.restype = wintypes.HWND
_user32.GetFocus.argtypes = []
_user32.GetFocus.restype = wintypes.HWND
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def embed_child(child_hwnd: int, parent_hwnd: int) -> bool:
    """Turn the Qt top-level window into a child of the Tk frame."""
    if not child_hwnd or not parent_hwnd:
        return False
    child = wintypes.HWND(child_hwnd)
    style = _GetWindowLong(child, _GWL_STYLE)
    style = (style | _WS_CHILD | _WS_VISIBLE) & ~(_WS_POPUP | _WS_CAPTION | _WS_THICKFRAME)
    _SetWindowLong(child, _GWL_STYLE, style)
    return bool(_user32.SetParent(child, wintypes.HWND(parent_hwnd)))


def fit_child(child_hwnd: int, parent_hwnd: int) -> None:
    """Resize the embedded window to fill the parent's client area."""
    if not child_hwnd or not parent_hwnd:
        return
    rect = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(parent_hwnd), ctypes.byref(rect)):
        return
    _user32.MoveWindow(wintypes.HWND(child_hwnd), 0, 0,
                       max(1, rect.right - rect.left), max(1, rect.bottom - rect.top), True)


_shared_thread = 0


def focus_child(child_hwnd: int) -> bool:
    """Hand Win32 keyboard focus to the embedded child window.

    Keyboard input goes to the focus window of the *foreground* input queue,
    which belongs to the Tk host, so a plain SetFocus is ignored. The queues
    have to be joined and stay joined: detaching straight after SetFocus hands
    the keyboard back to Tk, which is exactly the bug being fixed. Tk takes the
    focus back on its own by calling SetFocus when one of its widgets is
    clicked. Best effort only, never raises.
    """
    global _shared_thread
    try:
        child = int(child_hwnd or 0)
        if not child:
            return False
        foreground = _user32.GetForegroundWindow()
        if not foreground:
            return False
        pid = wintypes.DWORD(0)
        fg_thread = _user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        current = _kernel32.GetCurrentThreadId()
        if fg_thread and fg_thread != current and fg_thread != _shared_thread:
            if _shared_thread:
                _user32.AttachThreadInput(current, _shared_thread, False)
            _user32.AttachThreadInput(current, fg_thread, True)
            _shared_thread = fg_thread
        _user32.SetFocus(wintypes.HWND(child))
        return int(_user32.GetFocus() or 0) == child
    except Exception:
        return False


def press_inside(msg_hwnd: int, lparam: int, target_hwnd: int) -> bool:
    """True when a WM_*BUTTONDOWN message points inside the target window."""
    try:
        if not msg_hwnd or not target_hwnd:
            return True
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        point = wintypes.POINT(x, y)
        if not _user32.ClientToScreen(wintypes.HWND(int(msg_hwnd)), ctypes.byref(point)):
            return True
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(wintypes.HWND(int(target_hwnd)), ctypes.byref(rect)):
            return True
        return rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom
    except Exception:
        return True


class _WinMsg(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


_PRESS_MESSAGES = (0x0201, 0x0203, 0x0204)  # LBUTTONDOWN / DBLCLK / RBUTTONDOWN


def press_message(address: int):
    """Decode a native MSG pointer into (hwnd, lparam, is_button_press)."""
    try:
        address = int(address or 0)
        if address <= 0:
            return 0, 0, False
        msg = _WinMsg.from_address(address)
        is_press = int(msg.message) in _PRESS_MESSAGES
        return int(msg.hwnd or 0), int(msg.lParam or 0), is_press
    except Exception:
        return 0, 0, False
