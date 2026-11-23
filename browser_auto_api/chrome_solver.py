"""用真实 Chrome 通过 Cloudflare 人机校验，把通关凭证带回内嵌浏览器。

动机（本机对照实验结论）：
  * 真 Chrome（无论是否开 CDP）+ 硬件 SendInput 点击，royaleapi 的 Turnstile
    2 秒内通过；同一台机器、同一出口 IP、同一种硬件点击，QtWebEngine 内嵌
    引擎永远 verifying→reset（token 被服务端拒收）。
  * 因此 CDP、点击方式、IP 信誉都不是决定性因素，瓶颈在引擎本身。
与其继续逐项猜引擎指纹，不如让真实 Chrome 去过检：cf_clearance 只绑定
User-Agent 与出口 IP，拿到后回灌内嵌 profile（UA、UA-CH 品牌列表、cookie
全部照搬真实 Chrome 的实测值），内嵌浏览器即可直接访问目标站。

实现要点：
  * 默认走 CDP Input.dispatchMouseEvent + 渲染器截图：不调用 SendInput，
    不前移窗口，不移动 Windows 系统光标。2026-11-23 实测 18.9 秒过检，
    点击前后光标坐标完全不变。
  * hardware 模式仅作显式可选的诊断后备，使用独立 hardware_worker；
    生产默认绝不从 CDP 静默回退到硬件输入。
  * 截图只取网页视口，CDP 图像坐标按 cssLayoutViewport 比例换算成 CSS 坐标。
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from ctypes import wintypes

CHALLENGE_TITLE_MARKS = ("just a moment", "请稍候", "安全验证", "checking your browser",
                         "attention required", "one moment")


# --------------------------------------------------------------------------
# 真实 Chrome 定位
# --------------------------------------------------------------------------

def find_chrome():
    """返回本机真实 Chrome 路径；找不到返回 None。"""
    candidates = []
    env = os.environ.get("CHROME_PATH")
    if env:
        candidates.append(env)
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    which = shutil.which("chrome") or shutil.which("chrome.exe")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


# --------------------------------------------------------------------------
# Win32（DPI 感知必须最先设置，坐标一律物理像素）
# --------------------------------------------------------------------------

try:
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

u32 = ctypes.windll.user32 if sys.platform == "win32" else None

if u32 is not None:
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetAncestor.restype = wintypes.HWND
    u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u32.ClientToScreen.restype = wintypes.BOOL
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.GetWindowRect.restype = wintypes.BOOL
    u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.GetClientRect.restype = wintypes.BOOL
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    u32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u32.EnumWindows.restype = wintypes.BOOL
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.IsWindowVisible.restype = wintypes.BOOL
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.ShowWindow.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) \
    if sys.platform == "win32" else None


def _main_hwnd(pid):
    hits = []

    @WNDENUMPROC
    def cb(hwnd, _):
        wpid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not u32.IsWindowVisible(hwnd):
            return True
        if u32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        rect = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        hits.append((area, int(hwnd), title.value))
        return True

    u32.EnumWindows(cb, 0)
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0]


def _client_rect(hwnd):
    """客户区（网页内容）的屏幕物理矩形。"""
    pt = wintypes.POINT(0, 0)
    u32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
    rc = wintypes.RECT()
    u32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc))
    return (pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom)


def _title(pid):
    info = _main_hwnd(pid)
    return info[2] if info else ""


def _is_challenge_title(title):
    low = (title or "").lower()
    if not title:
        return True
    return any(mark in low or mark in title for mark in CHALLENGE_TITLE_MARKS)


# --------------------------------------------------------------------------
# 最小 CDP 客户端（stdlib WebSocket，只读 DOM/cookie，不投递输入）
# --------------------------------------------------------------------------

class _WS:
    def __init__(self, url):
        host_port, path = url[5:].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host_port, key)
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.buf = b""
        self._id = 0

    def send(self, obj):
        payload = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        while True:
            frame = self._read_frame()
            if frame is None:
                continue
            return json.loads(frame.decode("utf-8", "replace"))

    def _read_frame(self):
        def need(k):
            while len(self.buf) < k:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise RuntimeError("CDP socket closed")
                self.buf += chunk
        need(2)
        b1, b2 = self.buf[0], self.buf[1]
        opcode = b1 & 0x0F
        ln = b2 & 0x7F
        idx = 2
        if ln == 126:
            need(4)
            ln = struct.unpack(">H", self.buf[2:4])[0]
            idx = 4
        elif ln == 127:
            need(10)
            ln = struct.unpack(">Q", self.buf[2:10])[0]
            idx = 10
        need(idx + ln)
        payload = self.buf[idx:idx + ln]
        self.buf = self.buf[idx + ln:]
        if opcode == 0x9:
            self.sock.sendall(bytes([0x8A, len(payload)]) + payload)
            return None
        if opcode == 0x8:
            raise RuntimeError("CDP closed")
        return payload

    def cmd(self, method, params=None):
        self._id += 1
        mid = self._id
        self.send({"id": mid, "method": method, "params": params or {}})
        while True:
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _http_json(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
        return json.loads(r.read().decode())


# --------------------------------------------------------------------------
# 像素定位勾选框（深色挑战页，灰色描边；不依赖任何 DOM）
# --------------------------------------------------------------------------

def _find_checkbox(img):
    w, h = img.size
    px = img.load()
    xs, ys = [], []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y][:3]
            if abs(r - 153) <= 16 and abs(g - 153) <= 16 and abs(b - 153) <= 16 \
                    and abs(r - g) <= 6 and abs(g - b) <= 6:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    ys.sort()
    bands = []
    start = prev = ys[0]
    for y in ys[1:]:
        if y - prev > 6:
            bands.append((start, prev))
            start = y
        prev = y
    bands.append((start, prev))
    bands = [bd for bd in bands if 20 <= bd[1] - bd[0] <= 70]
    if not bands:
        return None
    y0, y1 = max(bands, key=lambda bd: bd[1] - bd[0])
    xs2 = sorted(x for x, y in zip(xs, ys) if y0 <= y <= y1)
    x0 = xs2[0]
    xs2 = [x for x in xs2 if x <= x0 + 60]
    return (x0 + xs2[-1]) // 2, (y0 + y1) // 2


def _cdp_screenshot(ws):
    """CDP 截图；渲染器/表面两种模式交替重试，适应后台窗口的瞬时停帧。"""
    from io import BytesIO
    from PIL import Image
    last_error = None
    for from_surface in (False, True, False):
        ws.sock.settimeout(12)
        try:
            shot = ws.cmd("Page.captureScreenshot", {
                "format": "png", "fromSurface": from_surface,
                "captureBeyondViewport": False,
            })
            return Image.open(BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
        except TimeoutError as exc:
            # 超时的旧响应若随后到达，_WS.cmd 会按请求 id 跳过它，不会
            # 错配给下一次调用；换另一种截图路径继续试。
            last_error = exc
        finally:
            ws.sock.settimeout(10)
    raise last_error or TimeoutError("CDP 截图超时")


def _cdp_click(ws, img, point):
    """把截图坐标换算成 CSS 视口坐标，投递页面输入，不动系统光标。"""
    metrics = ws.cmd("Page.getLayoutMetrics")
    viewport = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
    css_w = float(viewport.get("clientWidth") or img.width)
    css_h = float(viewport.get("clientHeight") or img.height)
    x = point[0] * css_w / img.width
    y = point[1] * css_h / img.height
    for i in range(1, 5):
        ws.cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x - (5 - i) * 3,
            "y": y - (5 - i) * 2, "button": "none", "buttons": 0,
            "pointerType": "mouse",
        })
        time.sleep(0.04)
    ws.cmd("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y,
        "button": "none", "buttons": 0, "pointerType": "mouse",
    })
    ws.cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": "left", "buttons": 1, "clickCount": 1,
        "pointerType": "mouse",
    })
    time.sleep(0.09)
    ws.cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": "left", "buttons": 0, "clickCount": 1,
        "pointerType": "mouse",
    })
    return {"imagePoint": list(point), "imageSize": list(img.size),
            "cssViewport": [css_w, css_h], "cssPoint": [x, y]}


def _page_tab(port):
    tabs = _http_json(port, "/json/list")
    tab = next((t for t in tabs if t.get("type") == "page"
                and "royaleapi" in (t.get("url") or "")), None)
    return tab or next((t for t in tabs if t.get("type") == "page"), None)


def _hardware_click(hwnd, rect, point):
    """复用生产 hardware_worker：CSS=客户区像素坐标，矩形=客户区屏幕矩形。"""
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hardware_worker.py")
    cmd = [sys.executable, "-I", worker, "click",
           "--view-hwnd", str(int(hwnd)),
           "--css", str(point[0]), str(point[1]),
           "--view-size", str(rect[2] - rect[0]), str(rect[3] - rect[1]),
           "--expect-rect", *(str(int(v)) for v in rect)]
    # stdin 显式 DEVNULL：经生产链调用时祖父进程的 stdin 是 Tk 宿主命令管道，
    # 继承该句柄会让子解释器在启动阶段挂死（与 backend.hardware_click 同因）。
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=creationflags,
                            encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        except Exception:
            proc.kill()
        out, err = proc.communicate()
        raise RuntimeError("hardware worker 超时(30s): %s" % ((err or out or "")[-400:]))
    line = (out or "").strip().splitlines()
    if not line:
        raise RuntimeError("hardware worker 无输出(rc=%s): %s"
                           % (proc.returncode, (err or "")[-300:]))
    result = json.loads(line[-1])
    if not result.get("ok"):
        raise RuntimeError("hardware worker 失败: %s" % result)
    return result


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def solve(url, timeout=100.0, profile_dir=None, keep=False, log=None,
          debug_dir=None, input_mode="cdp"):
    """在真实 Chrome 里通过 url 上的 CF 校验，返回 UA/UA-CH/cookie。

    input_mode="cdp"（默认）全程不移动系统光标；"hardware" 仅用于显式
    诊断，会前置窗口并通过 SendInput 移动真实鼠标。
    返回 dict: {ua, brands, fullVersionList, scalar, cookies, title, url, input}
    抛异常表示失败（未安装 Chrome、勾选框未出现、超时未通过等）。
    """
    if input_mode not in ("cdp", "hardware"):
        raise ValueError("input_mode 必须是 cdp 或 hardware")
    if sys.platform != "win32":
        raise RuntimeError("真实 Chrome 求解仅支持 Windows")
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("未找到真实 Chrome（可设 CHROME_PATH 指定）")

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    own_profile = profile_dir is None
    if own_profile:
        profile_dir = tempfile.mkdtemp(prefix="cf_chrome_")
    os.makedirs(profile_dir, exist_ok=True)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proc = subprocess.Popen([
        chrome, "--no-first-run", "--no-default-browser-check",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile_dir,
        "--window-position=60,60", "--window-size=1280,900",
        "--new-window", url,
    ], stdin=subprocess.DEVNULL)
    ws = None
    try:
        hwnd = None
        for _ in range(60):
            info = _main_hwnd(proc.pid)
            if info:
                hwnd = info[1]
                break
            time.sleep(0.5)
        if not hwnd:
            raise RuntimeError("真实 Chrome 窗口未出现")

        if input_mode == "cdp":
            tab = None
            for _ in range(40):
                try:
                    tab = _page_tab(port)
                    if tab:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            if not tab:
                raise RuntimeError("CDP 找不到页面标签")
            ws = _WS(tab["webSocketDebuggerUrl"])
            ws.cmd("Page.enable")
            ws.cmd("Network.enable")

        started = time.time()
        clicks = 0
        last_click = 0.0
        saw_challenge = False
        deadline = started + timeout
        last_title = None
        while time.time() < deadline:
            title = _title(proc.pid)
            if title != last_title:
                _log("chrome title: %r" % title)
                last_title = title
            if _is_challenge_title(title):
                saw_challenge = True
            elif saw_challenge:
                # 只有真的从挑战页走出来才算通过；导航初期的临时标题
                # （域名/空白）不能当作通过，否则会在挑战出现前就返回空 cookie。
                _log("chrome 已过检")
                break
            elif time.time() - started > 30:
                _log("chrome 30s 内未出现挑战页，按无需校验处理")
                break
            # 每次点击后给 12s 验证窗口；仍停留挑战页则重新识别勾选框再点，最多 3 次
            if saw_challenge and clicks < 3 and time.time() - started > 3 \
                    and time.time() - last_click > 12:
                if input_mode == "cdp":
                    # 不前移窗口、不读屏幕、不发 Windows 全局输入。
                    try:
                        img = _cdp_screenshot(ws)
                    except TimeoutError:
                        _log("chrome CDP 截图暂时超时，等待下一轮回试")
                        time.sleep(1.0)
                        continue
                    rect = None
                else:
                    u32.ShowWindow(wintypes.HWND(hwnd), 9)
                    u32.SetForegroundWindow(wintypes.HWND(hwnd))
                    time.sleep(0.4)
                    rect = _client_rect(hwnd)
                    from PIL import ImageGrab
                    img = ImageGrab.grab(bbox=rect)
                if debug_dir:
                    img.save(os.path.join(debug_dir, "chrome_before_click%d.png" % clicks))
                pt = _find_checkbox(img)
                if not pt:
                    _log("chrome 未识别到勾选框 mode=%s rect=%s" % (input_mode, rect))
                if pt:
                    _log("chrome 勾选框=%s mode=%s rect=%s" % (pt, input_mode, rect))
                    if input_mode == "cdp":
                        res = _cdp_click(ws, img, pt)
                        _log("chrome CDP 点击（系统光标不动）: %s" % json.dumps(res)[:400])
                    else:
                        res = _hardware_click(hwnd, rect, pt)
                        _log("chrome 硬件点击: %s" % json.dumps(res)[:400])
                    clicks += 1
                    last_click = time.time()
                    time.sleep(1.0)
            time.sleep(1.0)
        else:
            if debug_dir:
                try:
                    if input_mode == "cdp":
                        final_img = _cdp_screenshot(ws)
                    else:
                        from PIL import ImageGrab
                        final_img = ImageGrab.grab(bbox=_client_rect(hwnd))
                    final_img.save(os.path.join(debug_dir, "chrome_final.png"))
                except Exception:
                    pass
            raise RuntimeError("真实 Chrome 在 %.0fs 内未通过校验" % timeout)
        if _is_challenge_title(_title(proc.pid)):
            raise RuntimeError("真实 Chrome 仍停留在校验页")

        time.sleep(1.5)  # 等 cf_clearance 落盘/内存就绪

        # 读 UA / UA-CH / cookie；CDP 模式复用点击期间的同一页面连接。
        if ws is None:
            tab = _page_tab(port)
            if tab is None:
                raise RuntimeError("CDP 找不到页面标签")
            ws = _WS(tab["webSocketDebuggerUrl"])
        try:
            ws.cmd("Network.enable")
            cookies = ws.cmd("Network.getAllCookies").get("cookies", [])
            meta = ws.cmd("Runtime.evaluate", {
                "expression": """
(async function(){
  var d = navigator.userAgentData;
  var he = d ? await d.getHighEntropyValues(['fullVersionList','uaFullVersion',
      'platformVersion','architecture','bitness','model','wow64']) : {};
  return JSON.stringify({ua: navigator.userAgent,
      brands: d ? d.brands : [], he: he, url: location.href, title: document.title});
})()""",
                "awaitPromise": True, "returnByValue": True,
            }).get("result", {}).get("value")
        finally:
            ws.close()
        info = json.loads(meta or "{}")
        he = info.get("he") or {}
        scalar = {k: he[k] for k in ("architecture", "bitness", "model",
                                     "platformVersion", "wow64", "uaFullVersion")
                  if k in he}
        result = {
            "ua": info.get("ua") or "",
            "brands": info.get("brands") or [],
            "fullVersionList": he.get("fullVersionList") or [],
            "scalar": scalar,
            "cookies": cookies,
            "title": info.get("title") or _title(proc.pid),
            "url": info.get("url") or url,
            "clicked": clicks > 0,
            "clicks": clicks,
            "input": input_mode,
            "elapsedMs": int((time.time() - started) * 1000),
        }
        _log("chrome 取回 cookie %d 个，UA=%s" % (len(cookies), result["ua"]))
        return result
    finally:
        if ws is not None:
            ws.close()
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        except Exception:
            pass
        if own_profile and not keep:
            # 临时 profile 是本次求解的副产物；异步清理，避免文件句柄未释放报错
            try:
                def _rm():
                    for _ in range(10):
                        try:
                            shutil.rmtree(profile_dir, ignore_errors=True)
                            return
                        except Exception:
                            time.sleep(1)
                import threading
                threading.Thread(target=_rm, daemon=True).start()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 独立进程封装（生产调用入口）
# --------------------------------------------------------------------------

def solve_in_subprocess(url, timeout=170.0, solve_timeout=100.0, debug_dir=None,
                        input_mode="cdp"):
    """在独立干净的 Python 进程里运行 solve()，返回其结果 dict。

    为什么不能直接在生产进程里调用 solve()：生产调用方是 Qt 浏览器子进程，
    它 DPI 不感知，GetClientRect/ClientToScreen 返回的是系统虚拟化坐标
    （实测客户区被报成 1265x730 而非物理 1898x1095），截图与点击坐标随之
    错位；且它的 stdin 是 Tk 宿主的命令管道，子孙进程继承后会挂死。
    独立进程与已验证成功的独立运行环境一致，从根上避开这两类问题。

    结果 dict 附带 "solverLog"（求解过程日志列表）；失败抛 RuntimeError，
    消息里带求解日志与 stderr 尾部，便于诊断。
    """
    cmd = [sys.executable, "-I", os.path.abspath(__file__), url,
           "--timeout", str(float(solve_timeout)), "--input", input_mode]
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        cmd += ["--debug-dir", debug_dir]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=creationflags,
                            encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # venv 的 python.exe 是再启动器，必须 /T 连整棵树（含真实 Chrome）一起杀
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        except Exception:
            proc.kill()
        out, err = proc.communicate()
        raise RuntimeError("chrome solver 子进程超时(>%.0fs)，已杀进程树: %s"
                           % (timeout, (err or out or "(无输出)")[-600:]))
    lines = (out or "").strip().splitlines()
    if not lines:
        raise RuntimeError("chrome solver 子进程无输出(rc=%s): %s"
                           % (proc.returncode, (err or "(无 stderr)")[-800:]))
    try:
        envelope = json.loads(lines[-1])
    except ValueError:
        raise RuntimeError("chrome solver 输出非 JSON(rc=%s): %s | %s"
                           % (proc.returncode, lines[-1][-300:], (err or "")[-400:]))
    if not envelope.get("ok"):
        raise RuntimeError("真实 Chrome 求解失败: %s | 日志: %s"
                           % (envelope.get("error", "unknown"),
                              " ; ".join(envelope.get("log") or [])[-600:]))
    result = envelope.get("result") or {}
    result["solverLog"] = envelope.get("log") or []
    return result


def main(argv=None):
    """CLI：python -I chrome_solver.py <url> [--timeout S] [--debug-dir DIR]

    最后一行 stdout 恒为 JSON 信封：
      {"ok": true,  "result": {...}, "log": [...]}
      {"ok": false, "error": "...",  "log": [...]}
    """
    import argparse
    parser = argparse.ArgumentParser(description="真实 Chrome 过 CF 校验并回传凭证")
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=100.0)
    parser.add_argument("--input", dest="input_mode", choices=("cdp", "hardware"),
                        default="cdp", help="默认 cdp，不移动系统光标")
    parser.add_argument("--debug-dir", default=None)
    parser.add_argument("--keep", action="store_true", help="保留临时 Chrome profile")
    args = parser.parse_args(argv)

    logs = []
    try:
        result = solve(args.url, timeout=args.timeout, keep=args.keep,
                       log=logs.append, debug_dir=args.debug_dir,
                       input_mode=args.input_mode)
        envelope = {"ok": True, "result": result, "log": logs}
        code = 0
    except Exception as exc:
        envelope = {"ok": False, "error": repr(exc), "log": logs}
        code = 1
    # -I 模式忽略 PYTHONIOENCODING，stdout 可能是 cp1252；cookie/标题里含
    # 非 ASCII 字符时 ensure_ascii=False 会直接 UnicodeEncodeError。
    # 一律输出纯 ASCII JSON，与父进程/终端编码完全解耦。
    sys.stdout.write(json.dumps(envelope, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
