"""把 HTTP 请求翻译成页面操作：取 HTML、查元素坐标、驱动鼠标。"""

import json
import threading
import uuid

from PySide6.QtCore import QObject

from .api_server import PENDING
from .js_scripts import ELEMENT_QUERY_TEMPLATE, ELEMENT_SCROLL_TEMPLATE

JS_CURRENT = (
    "JSON.stringify({url: location.href, title: document.title, "
    "html: (document.documentElement ? document.documentElement.outerHTML : '')})"
)


class Backend(QObject):
    def __init__(self, page, dispatcher, mouse_controller, parent=None, view=None):
        super().__init__(parent)
        self._page = page
        self._dispatcher = dispatcher
        self._mouse = mouse_controller
        self._view = view
        self.pointer = None  # 由窗口注入，虚拟指针
        self._mouse_lock = threading.Lock()
        self._pending = {}
        mouse_controller.finished.connect(self._on_mouse_finished)

    # ---------- 只读状态 ----------

    def status(self):
        def fn(job):
            out = {
                "ok": True,
                "url": self._page.url().toString(),
                "title": self._page.title(),
                "loading": self._page.isLoading(),
            }
            view = self._view
            if view is not None:
                pos = view.mapToGlobal(view.rect().topLeft())
                out["view"] = {
                    "width": view.width(),
                    "height": view.height(),
                    "zoom": view.zoomFactor(),
                    "screenX": pos.x(),
                    "screenY": pos.y(),
                    "dpr": view.devicePixelRatioF(),
                }
            try:
                prof = self._page.profile()
                out["profile"] = {
                    "offTheRecord": bool(prof.isOffTheRecord()),
                    "storage": prof.persistentStoragePath(),
                    "cache": prof.cachePath(),
                    "cookiePolicy": str(prof.persistentCookiesPolicy()),
                }
            except Exception as exc:
                out["profile"] = {"error": repr(exc)}
            return out

        return self._dispatcher.submit(fn, timeout=10)

    def viewport_size(self):
        """返回视口 CSS 尺寸（页面始终是 1920x1080 的坐标系）。"""
        def fn(job):
            view = self._view
            z = float(view.zoomFactor()) or 1.0
            return (view.width() / z, view.height() / z)

        try:
            return self._dispatcher.submit(fn, timeout=10)
        except Exception:
            return (1920.0, 1080.0)

    # ---------- GET cloudflare ----------

    def solve_cloudflare(self, attempts=3, find_timeout=25.0, click_timeout=20.0):
        from . import cloudflare
        return cloudflare.solve(self, attempts=attempts,
                                find_timeout=find_timeout, click_timeout=click_timeout)

    # ---------- POST pointer ----------

    def pointer_action(self, action, pos=None, trail=None):
        ptr = self.pointer
        if ptr is None:
            raise ValueError("虚拟指针未接入")

        def fn(job):
            if action == "show":
                ptr.show(pos=pos, trail=trail)
            elif action == "hide":
                ptr.hide()
            return ptr.status()

        return self._dispatcher.submit(fn, timeout=10)

    # ---------- GET native（坐标换算诊断） ----------

    def native_math(self, x, y):
        def fn(job):
            return self._mouse.native_math(x, y)

        return self._dispatcher.submit(fn, timeout=10)

    # ---------- 硬件级 SendInput 点击 ----------

    def hardware_click(self, css_x, css_y, css_away=None, timeout=25.0):
        """用独立 DPI-aware 子进程投递真实 OS 鼠标事件（SendInput）。

        PostMessage 合成的 WM_ 消息会被 Turnstile 服务端判为非可信输入；
        真实硬件事件走这里。Qt 主线程只负责读 hwnd/zoom，子进程在调用
        线程上跑，避免把事件循环阻塞两秒以上。失败时抛异常，由调用方
        回退到 PostMessage 路径。
        """
        import os
        import subprocess
        import sys

        from . import config

        if sys.platform != "win32":
            raise RuntimeError("硬件输入仅支持 Windows")

        def gather(job):
            import ctypes
            from PySide6.QtCore import QPoint

            view = self._view
            zoom = float(view.zoomFactor()) or 1.0
            # 宿主 Qt 进程是 DPI 不感知的，mapToGlobal/width 都在系统虚拟化
            # 坐标系里；GetScaleFactorForDevice 不受感知级别影响，能拿到真实
            # 缩放（本机 150%），乘回去即渲染表面的物理屏幕矩形。
            try:
                pct = ctypes.windll.shcore.GetScaleFactorForDevice(0)
                scale = (pct / 100.0) if pct and 50 <= pct <= 500 else 1.0
            except Exception:
                scale = 1.0
            origin = view.mapToGlobal(QPoint(0, 0))
            left = int(round(origin.x() * scale))
            top = int(round(origin.y() * scale))
            right = left + int(round(view.width() * scale))
            bottom = top + int(round(view.height() * scale))
            return {"view_hwnd": int(view.winId()),
                    "css_w": view.width() / zoom,
                    "css_h": view.height() / zoom,
                    "rect": (left, top, right, bottom)}

        info = self._dispatcher.submit(gather, timeout=10)
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "hardware_worker.py")
        args = [sys.executable, worker, "click",
                "--view-hwnd", str(info["view_hwnd"]),
                "--css", str(float(css_x)), str(float(css_y)),
                "--view-size", str(info["css_w"]), str(info["css_h"]),
                "--expect-rect", *[str(v) for v in info["rect"]],
                "--debug"]
        if css_away is not None:
            args += ["--away", str(float(css_away[0])), str(float(css_away[1]))]

        # 独立进程组：venv 的 python.exe 是再启动器，超时时必须 /T 连整个
        # 进程树一起杀，否则基础解释器可能成为孤儿继续移动鼠标。
        # stdin 必须显式 DEVNULL：浏览器进程的 stdin 是 Tk 宿主的命令管道，
        # 子进程继承该管道句柄会在解释器启动阶段挂死（实测无任何输出直到
        # 超时），而 worker 本来就不需要 stdin。
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags,
            encoding="utf-8", errors="replace")
        t_start = __import__("time").perf_counter()

        def _log_worker(rc, stdout, stderr, note=""):
            # 诊断落盘：每次 worker 调用的现场（含点击前后窗口归属）都进
            # .work/cfdiag/worker_runs.log，不依赖调用方是否抛异常。
            try:
                import os as _os
                import time as _time
                log_dir = _os.path.join(config.APP_DIR, ".work", "cfdiag")
                _os.makedirs(log_dir, exist_ok=True)
                lines = (stdout or "").strip().splitlines()
                payload = {
                    "at": _time.strftime("%H:%M:%S"),
                    "elapsed": round(__import__("time").perf_counter() - t_start, 3),
                    "css": [css_x, css_y],
                    "away": css_away,
                    "rc": rc,
                    "note": note,
                    "stdout": lines[-1] if lines else "",
                    "stderr": (stderr or "").strip()[-1200:],
                }
                with open(_os.path.join(log_dir, "worker_runs.log"),
                          "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception:
                pass

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=5)
            except Exception:
                proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            _log_worker(-1, stdout, stderr, note="timeout")
            raise RuntimeError("硬件输入子进程超时(>%.0fs)，已杀进程树；阶段日志: %s"
                               % (timeout, (stderr or stdout or "(无输出)").strip()[-500:]))
        out = (stdout or "").strip().splitlines()
        if proc.returncode != 0 or not out:
            detail = (stderr or stdout or "no output").strip()[-500:]
            _log_worker(proc.returncode, stdout, stderr, note="bad-rc")
            raise RuntimeError("硬件输入子进程失败(rc=%d): %s" % (proc.returncode, detail))
        data = json.loads(out[-1])
        _log_worker(proc.returncode, stdout, stderr)
        if not data.get("ok"):
            raise RuntimeError("硬件输入失败: %s | %s"
                               % (data.get("error", "unknown"),
                                  (stderr or "").strip()[-300:]))

        # 虚拟指针覆盖层同步到离开点（真实光标已经在那里）。
        try:
            px, py = css_away if css_away is not None else (css_x, css_y)

            def sync(job):
                if self.pointer is not None:
                    self.pointer.move_to(float(px), float(py))

            self._dispatcher.submit(sync, timeout=5)
        except Exception:
            pass
        return data

    # ---------- POST cookies ----------

    def clear_cookies(self, domain="", name=""):
        """删除某个域名（或全部）的 cookie，用于重新触发 Cloudflare 校验页。

        name 非空时只删指定名字的 cookie（例如只删 cf_clearance，保留 __cf_bm
        等信任 cookie，模拟「被要求校验但浏览器本身已可信」的常见情形）。
        """
        from PySide6.QtCore import QTimer

        dom = str(domain or "").strip().lower()
        cname = str(name or "").strip()

        def fn(job):
            store = self._page.profile().cookieStore()
            if dom in ("*", "all", "-"):
                store.deleteAllCookies()
                self._dispatcher.resolve(job, {"ok": True, "cleared": -1, "domain": "*"})
                return PENDING
            grabbed = []
            hosts = set()

            def on_cookie(cookie):
                try:
                    host = str(cookie.domain() or "").lower()
                    hosts.add(host)
                    if (not dom or dom in host) and \
                            (not cname or cname == str(cookie.name())):
                        grabbed.append(cookie)
                except Exception:
                    pass

            store.cookieAdded.connect(on_cookie)

            def finish():
                try:
                    store.cookieAdded.disconnect(on_cookie)
                except (TypeError, RuntimeError):
                    pass
                for cookie in grabbed:
                    store.deleteCookie(cookie)
                self._dispatcher.resolve(job, {"ok": True, "cleared": len(grabbed),
                                               "domain": dom, "name": cname,
                                               "hosts": sorted(hosts)})

            store.loadAllCookies()
            QTimer.singleShot(2500, finish)
            return PENDING

        return self._dispatcher.submit(fn, timeout=20)

    # ---------- GET nav ----------

    def navigate(self, url):
        target = _normalize_url(url)
        from PySide6.QtCore import QUrl

        def fn(job):
            page = self._page

            def on_loaded(ok):
                try:
                    page.loadFinished.disconnect(on_loaded)
                except (TypeError, RuntimeError):
                    pass
                self._dispatcher.resolve(job, {
                    "ok": True,
                    "url": page.url().toString(),
                    "title": page.title(),
                    "loaded": bool(ok),
                })

            page.loadFinished.connect(on_loaded)
            page.load(QUrl(target))
            return PENDING

        return self._dispatcher.submit(fn, timeout=30)

    def navigate_async(self, url):
        """发起跳转后立即返回：有些站点（长连接/广告）的 loadFinished 迟迟
        不来，调用方改为轮询 DOM 判断是否就绪。"""
        target = _normalize_url(url)
        from PySide6.QtCore import QUrl

        def fn(job):
            self._page.load(QUrl(target))
            return {"ok": True, "url": target, "started": True}

        return self._dispatcher.submit(fn, timeout=10)

    # ---------- 通用 JS 求值 ----------

    def eval_js(self, expr, timeout=20):
        def fn(job):
            self._page.runJavaScript(expr, lambda res: self._js_result(job, res))
            return PENDING

        return self._dispatcher.submit(fn, timeout=timeout)

    # ---------- GET current ----------

    def get_current(self):
        def fn(job):
            self._page.runJavaScript(JS_CURRENT, lambda res: self._js_result(job, res))
            return PENDING

        data = self._dispatcher.submit(fn, timeout=20)
        out = {"ok": True}
        out.update(data)
        return out

    # ---------- GET element ----------

    def get_elements(self, selector):
        expr = ELEMENT_QUERY_TEMPLATE % {"selector": json.dumps(selector)}

        def fn(job):
            self._page.runJavaScript(expr, lambda res: self._js_result(job, res))
            return PENDING

        data = self._dispatcher.submit(fn, timeout=20)
        data["ok"] = "error" not in data
        return data

    # ---------- POST mouse ----------

    def mouse(self, action):
        action = normalize_action(action)
        with self._mouse_lock:
            def fn(job):
                if action.get("selector"):
                    idx = int(action.get("index", 0))
                    expr = ELEMENT_SCROLL_TEMPLATE % {"selector": json.dumps(action["selector"]), "index": idx}

                    def resolve(res):
                        try:
                            info = json.loads(str(res))
                        except (TypeError, ValueError):
                            self._dispatcher.resolve(job, error="元素定位结果无法解析")
                            return
                        if info.get("error"):
                            self._dispatcher.resolve(job, error="元素定位失败: %s" % info["error"])
                            return
                        act = dict(action)
                        act.pop("selector", None)
                        act["x"] = info["x"]
                        act["y"] = info["y"]
                        act["_selectorCount"] = info.get("count")
                        self._start_mouse(act, job)
                    self._page.runJavaScript(expr, resolve)
                else:
                    self._start_mouse(action, job)
                return PENDING

            return self._dispatcher.submit(fn, timeout=90)

    def _start_mouse(self, action, job):
        token = uuid.uuid4().hex
        self._pending[token] = job
        self._mouse.start(action, token)

    def _on_mouse_finished(self, token, result):
        job = self._pending.pop(token, None)
        if job is None:
            return
        data = {k: v for k, v in result.items() if not k.startswith("_") and k != "token"}
        if data.get("error"):
            self._dispatcher.resolve(job, error=data["error"])
        else:
            data["ok"] = True
            self._dispatcher.resolve(job, data)

    # ---------- 工具 ----------

    def _js_result(self, job, res):
        if res is None:
            self._dispatcher.resolve(job, error="页面不可用或尚未加载完成")
            return
        try:
            data = json.loads(str(res))
        except (TypeError, ValueError):
            self._dispatcher.resolve(job, error="页面返回的不是 JSON")
            return
        self._dispatcher.resolve(job, data)


def _normalize_url(url):
    target = str(url or "").strip()
    if not target:
        raise ValueError("缺少 url 参数")
    if "://" not in target:
        target = "https://" + target.lstrip("/")
    scheme = target.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 地址")
    return target


def normalize_action(action):
    name = str(action.get("action") or "").strip().lower()
    act = dict(action)
    act["action"] = name
    if not act.get("selector"):
        for key in ("x", "y"):
            if key in act:
                try:
                    act[key] = float(act[key])
                except (TypeError, ValueError):
                    raise ValueError("字段 %s 必须是数字" % key)
        if name in ("drag",):
            for key in ("fromX", "fromY", "toX", "toY"):
                if key in act:
                    try:
                        act[key] = float(act[key])
                    except (TypeError, ValueError):
                        raise ValueError("字段 %s 必须是数字" % key)
        if name not in ("drag",):
            if "x" not in act or "y" not in act:
                raise ValueError("缺少 x/y 坐标（或提供 selector）")
    if "deltaY" in act:
        act["deltaY"] = float(act["deltaY"])
    if "deltaX" in act:
        act["deltaX"] = float(act["deltaX"])
    if name not in ("move", "click", "dblclick", "down", "up", "drag", "wheel"):
        raise ValueError("未知 action: %s" % name)
    return act
