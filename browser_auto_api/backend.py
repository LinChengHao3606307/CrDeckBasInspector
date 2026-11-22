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
