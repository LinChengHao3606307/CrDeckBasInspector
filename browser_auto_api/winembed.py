"""Win32 helpers that reparent a Qt window into a Tk host window.

The Tk app owns the layout; the Qt browser runs in a separate process and is
attached as a child window of a Tk frame, then kept in sync with its client area.
"""

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)

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
