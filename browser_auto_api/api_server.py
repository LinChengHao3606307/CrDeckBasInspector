"""本地 HTTP API：线程内运行，所有页面操作调度回 Qt 主线程执行。"""

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QObject, QTimer

from . import config

PENDING = object()


class Job:
    def __init__(self, timeout):
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.timeout = timeout

    def done(self):
        self.event.set()


class JobDispatcher(QObject):
    """HTTP 线程提交任务，Qt 主线程逐个执行。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = queue.Queue()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(8)

    def submit(self, fn, timeout=30):
        job = Job(timeout)
        self._queue.put((fn, job))
        job.event.wait(timeout)
        if not job.event.is_set():
            raise TimeoutError("页面操作超时（%.0f 秒）" % timeout)
        if job.error:
            raise RuntimeError(job.error)
        return job.result

    def resolve(self, job, result=None, error=None):
        if job.event.is_set():
            return
        job.result = result
        job.error = error
        job.event.set()

    def _drain(self):
        while True:
            try:
                fn, job = self._queue.get_nowait()
            except queue.Empty:
                return
            if job.event.is_set():
                continue
            try:
                value = fn(job)
            except Exception as exc:
                self.resolve(job, error=repr(exc))
                continue
            if value is not PENDING:
                self.resolve(job, value)


class ApiServer:
    def __init__(self, host, port):
        self.host = host
        self.port = int(port)
        self.backend = None  # 由窗口注入，提供 get_current / get_elements / mouse
        self.recorder = None  # 由窗口注入，记录浏览器区域输入事件
        self._httpd = None
        self._thread = None

    @property
    def running(self):
        return self._httpd is not None

    def start(self):
        if self._httpd is not None:
            return
        api = self

        class Handler(ApiHandler):
            server_api = api

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.daemon_threads = True
        httpd.allow_reuse_address = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="api-server", daemon=True)
        self._thread.start()

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)


class ApiHandler(BaseHTTPRequestHandler):
    server_api: ApiServer = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- 响应工具 ----------

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Access-Control-Request-Private-Network")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("body is not valid UTF-8 JSON")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _route(self):
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or ""
        query = parse_qs(parts.query)
        return path, query

    # ---------- 路由 ----------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path, query = self._route()
        try:
            if path in ("", "/help", "/docs"):
                self._send_json(200, {
                    "name": "Browser Auto API",
                    "endpoints": {
                        "GET /nav/?url=<url>": "跳转到指定页面，等待加载完成后返回",
                    "GET /cloudflare/": "检测并尝试通过 Cloudflare 人机校验（Turnstile）",
                    "GET /current/": "返回当前页面 URL / 标题 / 完整 HTML",
                        "GET /element/?selector=<css>": "返回该 CSS 选择器命中的所有元素坐标（视口 CSS 像素）",
                        "GET /royaleapi/deck/?name=<player>": "RoyaleAPI 按玩家名查最近卡组（human 拟人点击、无 clan 只取 index 那一条、clan 可选筛部落并最多核对 max 人、wait 秒、solve 是否自动过 CF）",
                        "POST /mouse/": "发送鼠标事件，移动轨迹为贝塞尔曲线手滑",
                        "POST /recording/": "开始/结束记录浏览器区域的输入事件（action: start / end）",
                        "POST /cookies/": "清除 cookie 重新触发校验页（action: clear，domain/name 可选；clear_all 清全部）",
                        "GET /pointer/": "虚拟指针状态（位置 CSS 坐标、可见性、轨迹、是否跟随真实鼠标）",
                        "POST /pointer/": "显示/隐藏虚拟指针覆盖层（action: show / hide / status，show 可带 trail）",
                        "GET /health": "健康检查",
                    },
                    "mouse_actions": {
                        "move": {"x": 0, "y": 0},
                        "click": {"x": 0, "y": 0, "button": "left"},
                        "dblclick": {"x": 0, "y": 0},
                        "down": {"x": 0, "y": 0},
                        "up": {"x": 0, "y": 0},
                        "drag": {"fromX": 0, "fromY": 0, "toX": 100, "toY": 100},
                        "wheel": {"x": 0, "y": 0, "deltaY": 120},
                        "selector 可选": "动作里不给 x/y 时可用 selector（及 index）先定位元素中心",
                        "native": "为 true 时投递原生窗口消息，页面收到 trusted 输入，真实光标不动",
                    },
                })
                return
            if path == "/health":
                self._send_json(200, self.server_api.backend.status())
                return
            if path == "/startpage":
                page = os.path.join(config.APP_DIR, "startpage.html")
                if not os.path.exists(page):
                    self._send_json(404, {"ok": False, "error": "startpage not found"})
                    return
                with open(page, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/shot":
                def fn(job):
                    view = self.server_api.backend._view
                    img = view.grab().toImage()
                    name = "shot_%d.png" % int(__import__("time").time() * 1000)
                    path = os.path.join(config.APP_DIR, "recordings", name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    img.save(path)
                    return {"path": path, "width": img.width(), "height": img.height()}
                self._send_json(200, {"ok": True,
                                      **self.server_api.backend._dispatcher.submit(fn, timeout=15)})
                return
            if path == "/widgets":
                from . import cloudflare
                self._send_json(200, {"ok": True,
                                      "boxes": cloudflare.pixel_boxes(self.server_api.backend)})
                return
            if path == "/native":
                try:
                    nx = float((query.get("x") or ["0"])[0])
                    ny = float((query.get("y") or ["0"])[0])
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "x/y 必须是数字"})
                    return
                self._send_json(200, {"ok": True,
                                      **self.server_api.backend.native_math(nx, ny)})
                return
            if path == "/env":
                from . import cloudflare
                self._send_json(200, {"ok": True,
                                      "env": cloudflare._env(self.server_api.backend)})
                return
            if path == "/cloudflare":
                try:
                    attempts = int((query.get("attempts") or ["3"])[0])
                    find_timeout = float((query.get("find_timeout") or ["25"])[0])
                    click_timeout = float((query.get("click_timeout") or ["20"])[0])
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": "参数必须是数字: %s" % exc})
                    return
                self._send_json(200, self.server_api.backend.solve_cloudflare(
                    attempts, find_timeout, click_timeout))
                return
            if path == "/nav":
                url = (query.get("url") or [""])[0]
                try:
                    self._send_json(200, self.server_api.backend.navigate(url))
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return
            if path == "/current":
                self._send_json(200, self.server_api.backend.get_current())
                return
            if path == "/pointer":
                ptr = self.server_api.backend.pointer
                if ptr is None:
                    self._send_json(503, {"ok": False, "error": "虚拟指针未接入"})
                    return
                self._send_json(200, {"ok": True, **ptr.status()})
                return
            if path == "/royaleapi/deck":
                from . import royaleapi
                name = (query.get("name") or [""])[0].strip()
                tag = (query.get("tag") or [""])[0].strip()
                clan = (query.get("clan") or [""])[0].strip()
                if not name and not tag:
                    self._send_json(400, {"ok": False, "error": "缺少 name 参数"})
                    return
                try:
                    index = int((query.get("index") or ["0"])[0])
                    max_candidates = int((query.get("max") or ["10"])[0])
                    wait = float((query.get("wait") or ["12"])[0])
                    attempts = int((query.get("attempts") or ["2"])[0])
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "index/max/wait/attempts 必须是数字"})
                    return
                solve = (query.get("solve") or ["1"])[0].strip().lower() \
                    not in ("0", "false", "no", "off")
                human = (query.get("human") or ["1"])[0].strip().lower() \
                    not in ("0", "false", "no", "off")
                direct = (query.get("direct") or ["0"])[0].strip().lower() \
                    not in ("0", "false", "no", "off")
                try:
                    result = royaleapi.deck_by_name(
                        self.server_api.backend,
                        name or "#" + tag.lstrip("#"),
                        clan=clan, index=index, max_candidates=max_candidates,
                        wait=wait, solve=solve, attempts=attempts, human=human,
                        direct=direct)
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                self._send_json(200, result)
                return
            if path == "/element":
                selector = (query.get("selector") or [""])[0]
                if not selector.strip():
                    self._send_json(400, {"ok": False, "error": "缺少 selector 参数"})
                    return
                self._send_json(200, self.server_api.backend.get_elements(selector))
                return
            self._send_json(404, {"ok": False, "error": "unknown path: %s" % (path or "/")})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        path, _ = self._route()
        if path not in ("/mouse", "/recording", "/cookies", "/pointer"):
            self._send_json(404, {"ok": False, "error": "unknown path: %s" % path})
            return
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        if not str(body.get("action") or "").strip():
            self._send_json(400, {"ok": False, "error": "缺少 action 字段"})
            return
        try:
            if path == "/recording":
                self._send_json(200, self._recording(body.get("action")))
                return
            if path == "/pointer":
                ptr = self.server_api.backend.pointer
                if ptr is None:
                    self._send_json(503, {"ok": False, "error": "虚拟指针未接入"})
                    return
                action = str(body.get("action") or "").strip().lower()
                if action == "status":
                    self._send_json(200, {"ok": True, **ptr.status()})
                    return
                if action not in ("show", "hide"):
                    self._send_json(400, {"ok": False, "error": "未知 action: %s（只支持 show / hide / status）" % action})
                    return
                pos = body.get("pos")
                trail = body.get("trail")
                self._send_json(200, {"ok": True, **self.server_api.backend.pointer_action(
                    action,
                    None if pos is None else bool(pos),
                    None if trail is None else bool(trail))})
                return
            if path == "/cookies":
                action = str(body.get("action") or "").strip().lower()
                if action == "clear_all":
                    self._send_json(200, self.server_api.backend.clear_cookies("*"))
                    return
                if action != "clear":
                    self._send_json(400, {"ok": False, "error": "未知 action: %s（只支持 clear / clear_all）" % action})
                    return
                self._send_json(200, self.server_api.backend.clear_cookies(
                    body.get("domain") or "", body.get("name") or ""))
                return
            self._send_json(200, self.server_api.backend.mouse(body))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _recording(self, action):
        recorder = self.server_api.recorder
        if recorder is None:
            raise ValueError("录制功能未接入")
        action = str(action).strip().lower()
        if action == "start":
            return recorder.start()
        if action in ("end", "stop"):
            return recorder.stop()
        raise ValueError("未知 action: %s（只支持 start / end）" % action)
