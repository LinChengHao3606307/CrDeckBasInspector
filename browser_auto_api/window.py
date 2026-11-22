"""主窗口：左侧嵌入式浏览器，右侧服务配置与流量日志。"""

import json
import os
import sys
import threading
from datetime import datetime
from urllib.parse import quote

from PySide6.QtCore import (QAbstractNativeEventFilter, QSettings, QTimer, QUrl,
                           QUrlQuery, Qt, Signal)
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QProgressBar, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from . import config
from .api_server import ApiServer, JobDispatcher
from .backend import Backend
from .js_scripts import TRAFFIC_SCRIPT, VIEWPORT_SCRIPT
from .recording import Recorder
from .mouse import MouseController
from .pointer import VirtualPointer
from .traffic import BrowserPage, RequestInterceptor, format_entry
from .winembed import (embed_child, fit_child, focus_child, press_inside,
                       press_message)


EVENT_PREFIX = "@@EV@@"
STATE_PREFIX = "@@STATE@@"


def _write_line(line):
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _emit_event(entry):
    try:
        _write_line(EVENT_PREFIX + json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass


def _emit_state(payload):
    try:
        _write_line(STATE_PREFIX + json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


VIEW_WIDTH = 1920
VIEW_HEIGHT = 1080
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


class BrowserHost(QWidget):
    """固定 1920x1080 页面视口，按可用空间整体缩放。

    view 的物理尺寸设为 1920*s × 1080*s，zoomFactor 设为 s，
    于是页面内 window.innerWidth/innerHeight 恒为 1920/1080，
    页面不会因为窗口变小而改变布局。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("browserHost")
        self.setStyleSheet("background:#07090c;")
        self._view = None
        self._applying = False

    def attach(self, view):
        self._view = view
        view.setParent(self)
        view.show()
        try:
            view.zoomFactorChanged.connect(self._enforce_zoom)
        except AttributeError:
            pass
        self._fit()

    def _fit(self):
        view = self._view
        if view is None:
            return
        w, h = self.width(), self.height()
        if w < 16 or h < 9:
            return
        k = self._fitted_height(w, h)
        vw = k * VIEW_WIDTH // VIEW_HEIGHT
        view.setGeometry((w - vw) // 2, (h - k) // 2, vw, k)
        self._set_zoom(k / float(VIEW_HEIGHT))

    @staticmethod
    def _fitted_height(w, h):
        # 缩放后高度取 9 的倍数，宽度恰为 16k/9，zoom=k/1080，
        # 三者严格成比例，Chromium 算出的 CSS 视口才不会差 1 像素。
        k = min(VIEW_HEIGHT, h, (w * VIEW_HEIGHT) // VIEW_WIDTH)
        k = (k // 9) * 9
        return max(9, k)

    def _set_zoom(self, scale):
        if self._view is None:
            return
        self._applying = True
        self._view.setZoomFactor(scale)
        self._applying = False

    def _enforce_zoom(self, factor):
        if self._applying or self._view is None:
            return
        expected = self._expected_zoom()
        if expected is not None and abs(factor - expected) > 0.02:
            self._set_zoom(expected)

    def _expected_zoom(self):
        if self._view is None:
            return None
        w, h = self.width(), self.height()
        if w < 16 or h < 9:
            return None
        return self._fitted_height(w, h) / float(VIEW_HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


class _PressFocusFilter(QAbstractNativeEventFilter):
    """Forward real mouse presses to the window that owns the keyboard focus."""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):
        try:
            if b"windows" not in bytes(event_type):
                return False, 0
            hwnd, lparam, is_press = press_message(int(message))
            if is_press:
                self._owner._on_press(hwnd, lparam)
        except Exception:
            pass
        return False, 0


class MainWindow(QMainWindow):
    traffic = Signal(dict)
    command = Signal(str)

    def __init__(self, embed_parent=None):
        super().__init__()
        self._embed_parent = embed_parent
        self._cfg = config.load_config()
        self._server = None
        self._port = int(self._cfg.get("port", 8765))
        self._settings = QSettings("BrowserAutoApi", "BrowserAutoApi")

        self.setWindowTitle("Browser Auto API")
        screen = QGuiApplication.primaryScreen().availableGeometry()
        w = min(1400, screen.width() - 60) if screen else 1400
        h = min(880, screen.height() - 60) if screen else 880
        self.resize(max(900, w), max(600, h))

        self._build_ui()
        self._build_browser()
        self._build_backend()

        self.traffic.connect(self._on_traffic)
        self.setStyleSheet(_STYLE)
        self._restore_geometry()
        self._start_server(silent=True)

        if self._embed_parent is not None:
            self._start_stdin_reader()
            self._embed_into(self._embed_parent)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        # ---- 左：浏览器 ----
        left = QWidget()
        left.setMinimumWidth(480)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("toolbar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 6, 8, 6)
        bar_layout.setSpacing(6)

        self.btn_back = QPushButton("后退")
        self.btn_forward = QPushButton("前进")
        self.btn_reload = QPushButton("刷新")
        for btn in (self.btn_back, self.btn_forward, self.btn_reload):
            btn.setObjectName("navBtn")
            btn.setFixedHeight(28)
        self.btn_back.clicked.connect(lambda: self.view.back())
        self.btn_forward.clicked.connect(lambda: self.view.forward())
        self.btn_reload.clicked.connect(lambda: self.view.reload())

        self.address = QLineEdit()
        self.address.setPlaceholderText("输入网址或搜索内容，回车访问")
        self.address.setClearButtonEnabled(True)
        self.address.returnPressed.connect(self._navigate_from_address)

        self.btn_go = QPushButton("前往")
        self.btn_go.setObjectName("goBtn")
        self.btn_go.setFixedHeight(28)
        self.btn_go.clicked.connect(self._navigate_from_address)

        bar_layout.addWidget(self.btn_back)
        bar_layout.addWidget(self.btn_forward)
        bar_layout.addWidget(self.btn_reload)
        bar_layout.addWidget(self.address, 1)
        bar_layout.addWidget(self.btn_go)

        self.progress = QProgressBar()
        self.progress.setObjectName("loadBar")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()

        self.browser_host = BrowserHost()
        left_layout.addWidget(bar)
        left_layout.addWidget(self.progress)
        left_layout.addWidget(self.browser_host, 1)
        self._left_layout = left_layout

        # ---- 右：配置 + 日志 ----
        right = QWidget()
        right.setObjectName("sidePanel")
        right.setMinimumWidth(390)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(10)

        cfg_label = QLabel("服务配置")
        cfg_label.setObjectName("sectionTitle")
        right_layout.addWidget(cfg_label)

        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(8)
        port_label = QLabel("端口")
        self.port_edit = QLineEdit(str(self._cfg["port"]))
        self.port_edit.setFixedWidth(86)
        self.port_edit.setMaxLength(5)
        self.port_edit.setPlaceholderText("1-65535")
        self.btn_server = QPushButton("启动服务")
        self.btn_server.setObjectName("primaryBtn")
        self.btn_server.clicked.connect(self._toggle_server)
        cfg_row.addWidget(port_label)
        cfg_row.addWidget(self.port_edit)
        cfg_row.addWidget(self.btn_server)
        cfg_row.addStretch(1)
        right_layout.addLayout(cfg_row)

        self.server_status = QLabel("未启动")
        self.server_status.setObjectName("serverStatus")
        right_layout.addWidget(self.server_status)

        self.api_hint = QLabel("")
        self.api_hint.setObjectName("apiHint")
        self.api_hint.setWordWrap(True)
        self.api_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.api_hint)

        ptr_row = QHBoxLayout()
        ptr_row.setSpacing(8)
        ptr_label = QLabel("指针显示")
        ptr_label.setObjectName("sectionTitle")
        self.chk_pos = QCheckBox("位置")
        self.chk_pos.setToolTip("在虚拟指针周围画一个圈")
        self.chk_trail = QCheckBox("轨迹")
        self.chk_trail.setToolTip("累计画出走过的路径，关掉开关时清空")
        self.chk_pos.setChecked(bool(self._cfg.get("pointer_pos", True)))
        self.chk_trail.setChecked(bool(self._cfg.get("pointer_trail", False)))
        ptr_row.addWidget(ptr_label)
        ptr_row.addWidget(self.chk_pos)
        ptr_row.addWidget(self.chk_trail)
        ptr_row.addStretch(1)
        right_layout.addLayout(ptr_row)

        log_header = QHBoxLayout()
        log_header.setSpacing(8)
        log_title = QLabel("流量日志")
        log_title.setObjectName("sectionTitle")
        self.chk_pause = QCheckBox("暂停")
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("navBtn")
        self.btn_clear.clicked.connect(lambda: self.log.clear())
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        log_header.addWidget(self.chk_pause)
        log_header.addWidget(self.chk_autoscroll)
        log_header.addWidget(self.btn_clear)
        right_layout.addLayout(log_header)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("logView")
        self.log.document().setMaximumBlockCount(6000)
        self.log.setAcceptRichText(True)
        right_layout.addWidget(self.log, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        total = max(900, self.width())
        splitter.setSizes([int(total * 0.68), int(total * 0.32)])

        if self._embed_parent is not None:
            # The Tk host owns the toolbar and the side panel in embedded mode.
            bar.hide()
            self.progress.hide()
            self.setCentralWidget(left)
            self.setWindowTitle("CrDeckBasInspector Browser")

    # ------------------------------------------------------------- browser

    def _build_browser(self):
        # 默认 profile 是 off-the-record（内存态），重启即丢 cookie；
        # 这里用显式命名 profile，并把存储落在程序目录，cookie 才能跨重启保留。
        profile = QWebEngineProfile("browser-auto-api", self)
        base = os.path.join(config.APP_DIR, "profile")
        os.makedirs(os.path.join(base, "storage"), exist_ok=True)
        profile.setPersistentStoragePath(os.path.join(base, "storage"))
        profile.setCachePath(os.path.join(base, "cache"))
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        profile.setHttpCacheMaximumSize(512 * 1024 * 1024)
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self._profile = profile  # 保持引用，防止被回收
        if hasattr(profile, "setDownloadPath"):
            profile.setDownloadPath(os.path.expanduser("~/Downloads"))
        # 默认 UA 带 QtWebEngine 标记，会被 Cloudflare 判成机器人；换成标准 Chrome UA
        profile.setHttpUserAgent(USER_AGENT)
        profile.setHttpAcceptLanguage("zh-CN,zh,en-US,en")

        self._interceptor = RequestInterceptor(lambda entry: self.traffic.emit(entry), self)
        profile.setUrlRequestInterceptor(self._interceptor)

        script = QWebEngineScript()
        script.setName("autoapi-traffic")
        script.setSourceCode(TRAFFIC_SCRIPT)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        profile.scripts().insert(script)

        # 视口是虚拟的 1920x1080，屏幕指标要与之自洽（见 VIEWPORT_SCRIPT）
        script = QWebEngineScript()
        script.setName("autoapi-viewport")
        script.setSourceCode(VIEWPORT_SCRIPT)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        profile.scripts().insert(script)

        self.view = QWebEngineView()
        self.page = BrowserPage(profile, lambda entry: self.traffic.emit(entry), self.view)
        self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.browser_host.attach(self.view)

        profile.downloadRequested.connect(self._on_download)
        self.view.urlChanged.connect(self._on_url_changed)
        self.view.loadProgress.connect(self._on_progress)
        self.view.loadFinished.connect(self._on_load_finished)
        self.view.titleChanged.connect(self._on_title)

        self._shortcut("Ctrl+L", lambda: self.address.setFocus())
        self._shortcut("Ctrl+R", lambda: self.view.reload())
        self._shortcut("F5", lambda: self.view.reload())
        self._shortcut("Alt+Left", lambda: self.view.back())
        self._shortcut("Alt+Right", lambda: self.view.forward())

        self.view.setUrl(QUrl("about:blank"))

        # A click into the page must also move the Win32 input focus: the Tk
        # host owns it, so the view would never receive real key presses.
        # Chromium eats real clicks on its own native child window, so watch the
        # press messages; Qt widget events are not generated for that window.
        self._press_filter = _PressFocusFilter(self)
        QApplication.instance().installNativeEventFilter(self._press_filter)

    def _shortcut(self, keys, handler):
        sc = QShortcut(QKeySequence(keys), self)
        sc.activated.connect(handler)

    def _navigate_from_address(self):
        self._navigate(self.address.text())

    def _navigate(self, text):
        text = (text or "").strip()
        if not text:
            return
        low = text.lower()
        if low.startswith(("http://", "https://", "file://", "about:", "data:")):
            url = text
        elif " " in text or "." not in text:
            url = "https://www.bing.com/search?q=" + quote(text)
        else:
            url = "https://" + text
        self.view.setUrl(QUrl(url))

    def _on_url_changed(self, url):
        if not self.address.hasFocus():
            self.address.setText(url.toString())
        self._update_nav_buttons()
        if self._embed_parent is not None:
            _emit_state({"type": "url", "url": url.toString()})

    def _on_progress(self, value):
        if 0 < value < 100:
            self.progress.show()
            self.progress.setValue(value)
        else:
            self.progress.hide()
            self.progress.setValue(0)

    def _on_load_finished(self, ok):
        self.progress.hide()
        self._update_nav_buttons()
        if not ok:
            self.traffic.emit({"type": "info", "message": "页面加载失败"})

    def _on_title(self, title):
        if title:
            self.setWindowTitle(title if "Browser Auto API" in title else "Browser Auto API · " + title)
        else:
            self.setWindowTitle("Browser Auto API")

    def _update_nav_buttons(self):
        hist = self.view.page().history()
        self.btn_back.setEnabled(hist.canGoBack())
        self.btn_forward.setEnabled(hist.canGoForward())

    def _on_download(self, item):
        try:
            suggested = item.suggestedFileName() or "download.bin"
            folder = os.path.expanduser("~/Downloads")
            os.makedirs(folder, exist_ok=True)
            name, ext = os.path.splitext(suggested)
            target, n = suggested, 1
            while os.path.exists(os.path.join(folder, target)):
                target = "%s (%d)%s" % (name, n, ext)
                n += 1
            item.setDownloadDirectory(folder)
            item.setDownloadFileName(target)
            item.accept()
            self.traffic.emit({"type": "info", "message": "开始下载: " + target})
        except Exception as exc:
            self.traffic.emit({"type": "info", "message": "下载处理失败: %r" % (exc,)})

    # ------------------------------------------------------ keyboard focus

    def _focus_view(self):
        """Give the web view both Qt focus and the Win32 keyboard focus."""
        try:
            self.view.setFocus(Qt.FocusReason.MouseFocusReason)
        except Exception:
            pass
        try:
            focus_child(int(self.winId()))
        except Exception:
            pass

    def _on_press(self, msg_hwnd, lparam):
        try:
            if not press_inside(msg_hwnd, lparam, int(self.winId())):
                return
        except Exception:
            return
        # Deferred so Qt's input routing is not re-entered from inside it.
        QTimer.singleShot(0, self._focus_view)

    # ------------------------------------------------------------- backend

    def _build_backend(self):
        self._dispatcher = JobDispatcher(self)
        self._pointer = VirtualPointer(self.view, self)
        self._ptr_pos = bool(self._cfg.get("pointer_pos", True))
        self._ptr_trail = bool(self._cfg.get("pointer_trail", False))
        self._pointer.configure(pos=self._ptr_pos, trail=self._ptr_trail)
        if self._embed_parent is not None:
            _emit_state({"type": "pointer", "pos": self._ptr_pos,
                         "trail": self._ptr_trail})
        if self._embed_parent is None:
            self.chk_pos.setChecked(self._ptr_pos)
            self.chk_trail.setChecked(self._ptr_trail)
            self.chk_pos.toggled.connect(self._apply_pointer_flags)
            self.chk_trail.toggled.connect(self._apply_pointer_flags)
        self._mouse = MouseController(self.view, self._pointer, self)
        self._backend = Backend(self.page, self._dispatcher, self._mouse, self, self.view)
        self._backend.pointer = self._pointer
        self._recorder = Recorder(self.view, os.path.join(config.APP_DIR, "recordings"),
                                  self._dispatcher, self)

    def _apply_pointer_flags(self):
        # Embedded mode drops the Qt toolbar, so the flags are driven over stdin.
        if self._embed_parent is not None:
            return
        pos = self.chk_pos.isChecked()
        trail = self.chk_trail.isChecked()
        self._ptr_pos = pos
        self._ptr_trail = trail
        self._pointer.configure(pos=pos, trail=trail)
        self._cfg["pointer_pos"] = pos
        self._cfg["pointer_trail"] = trail
        config.save_config(self._cfg)

    def _toggle_server(self):
        if self._server is not None and self._server.running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self, silent=False):
        if self._server is not None and self._server.running:
            self._stop_server()
        try:
            text = self.port_edit.text() if self._embed_parent is None else str(self._port)
            port = int(text.strip())
            if not (1 <= port <= 65535):
                raise ValueError("端口需在 1-65535 之间")
        except ValueError as exc:
            self._set_status(False, "端口无效：%s" % exc)
            return
        server = ApiServer("127.0.0.1", port)
        server.backend = self._backend
        server.recorder = self._recorder
        server.sink = self.traffic.emit
        try:
            server.start()
        except OSError as exc:
            self._set_status(False, "启动失败：%s" % exc)
            self._load_start_page(port)
            if not silent:
                self.traffic.emit({"type": "info", "message": "API 启动失败: %s" % exc})
            return
        self._server = server
        self._port = port
        self._cfg["port"] = port
        config.save_config(self._cfg)
        self._set_status(True, "服务已启动")
        if self._embed_parent is None:
            self.btn_server.setText("停止服务")
        self._refresh_api_hint(port)
        self._load_start_page(port)
        if not silent:
            self.traffic.emit({"type": "info", "message": "API 已监听 http://127.0.0.1:%d" % port})

    def _stop_server(self):
        if self._server is not None:
            port = self._server.port
            self._server.stop()
            self._server = None
            self._set_status(False, "服务已停止")
            if self._embed_parent is None:
                self.btn_server.setText("启动服务")
            self._refresh_api_hint(port, running=False)
            self.traffic.emit({"type": "info", "message": "API 已停止"})


    def _load_start_page(self, port):
        page = os.path.join(config.APP_DIR, "startpage.html")
        if self._server is not None and self._server.running:
            url = QUrl("http://127.0.0.1:%d/startpage" % port)
        elif os.path.exists(page):
            url = QUrl.fromLocalFile(page)
        else:
            self.view.setUrl(QUrl("about:blank"))
            return
        query = QUrlQuery()
        query.addQueryItem("port", str(port))
        url.setQuery(query)
        self.view.setUrl(url)

    def _set_status(self, running, text):
        if self._embed_parent is not None:
            _emit_state({
                "type": "server",
                "running": bool(running),
                "text": text,
                "port": getattr(self._server, "port", None),
            })
            return
        color = "#4cd17a" if running else "#8a97a5"
        self.server_status.setText('<span style="color:%s;">&#9679;</span> %s' % (color, text))

    def _refresh_api_hint(self, port, running=True):
        if self._embed_parent is not None:
            return
        base = "http://127.0.0.1:%d" % port
        state = "示例请求：" if running else "服务未运行，示例请求："
        self.api_hint.setText(
            '<span style="color:#8fa1b4;">%s</span><br>'
            '<span style="color:#6b7c8f;">GET&nbsp; %s/current/ &nbsp;&middot;&nbsp; '
            'GET&nbsp; %s/element/?selector=... &nbsp;&middot;&nbsp; '
            'POST&nbsp; %s/mouse/</span>' % (state, base, base, base))

    # --------------------------------------------------------------- traffic

    def _on_traffic(self, entry):
        if self._embed_parent is not None:
            # The Tk host renders the log; the Qt side widgets are already gone.
            _emit_event(entry)
            return
        if self.chk_pause.isChecked():
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log.append('<span style="color:#4a5764;">[%s]</span> %s' % (ts, format_entry(entry)))
        if self.chk_autoscroll.isChecked():
            sb = self.log.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ---------------------------------------------------------------- embed

    def _embed_into(self, parent_hwnd):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.show()
        hwnd = int(self.winId())
        embed_child(hwnd, parent_hwnd)
        fit_child(hwnd, parent_hwnd)
        self._fit_timer = QTimer(self)
        self._fit_timer.timeout.connect(lambda: fit_child(hwnd, parent_hwnd))
        self._fit_timer.start(120)
        _write_line("@@READY@@")

    def _start_stdin_reader(self):
        self.command.connect(self._on_command, Qt.ConnectionType.QueuedConnection)

        def run():
            try:
                for line in sys.stdin:
                    line = line.strip()
                    if line:
                        self.command.emit(line)
            except Exception:
                pass

        threading.Thread(target=run, name="stdin", daemon=True).start()

    def _on_command(self, line):
        cmd, _, arg = line.partition(" ")
        cmd = cmd.upper()
        arg = arg.strip()
        if cmd == "NAV":
            self._navigate(arg)
        elif cmd == "BACK":
            self.view.back()
        elif cmd == "FORWARD":
            self.view.forward()
        elif cmd == "RELOAD":
            self.view.reload()
        elif cmd == "SERVER":
            sub, _, port_text = arg.partition(" ")
            if sub.upper() == "START":
                if port_text.strip().isdigit():
                    self._port = int(port_text.strip())
                    if self._embed_parent is None:
                        self.port_edit.setText(port_text.strip())
                self._start_server()
            elif sub.upper() == "STOP":
                self._stop_server()
        elif cmd == "POINTER":
            sub, _, val = arg.partition(" ")
            on = val.strip().lower() in ("1", "on", "true", "yes")
            sub = sub.upper()
            if sub == "POS":
                self._ptr_pos = on
            elif sub == "TRAIL":
                self._ptr_trail = on
            else:
                return
            self._pointer.configure(pos=self._ptr_pos, trail=self._ptr_trail)
            self._cfg["pointer_pos"] = self._ptr_pos
            self._cfg["pointer_trail"] = self._ptr_trail
            config.save_config(self._cfg)
            _emit_state({"type": "pointer", "pos": self._ptr_pos,
                         "trail": self._ptr_trail})
        elif cmd == "QUIT":
            QApplication.instance().quit()

    # ----------------------------------------------------------- lifecycle

    def _restore_geometry(self):
        geo = self._settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        if self._server is not None:
            self._server.stop()
        self._settings.setValue("geometry", self.saveGeometry())
        try:
            self._cfg["port"] = int(self.port_edit.text())
            config.save_config(self._cfg)
        except ValueError:
            pass
        super().closeEvent(event)


_STYLE = """
QMainWindow { background: #0f141a; }
QWidget { color: #c9d3de; font-size: 13px; }
#toolbar { background: #161c24; border-bottom: 1px solid #232b35; }
#sidePanel { background: #121820; border-left: 1px solid #232b35; }
QLineEdit {
    background: #0b1016; border: 1px solid #2a3440; border-radius: 6px;
    padding: 5px 9px; color: #dbe4ee; selection-background-color: #2a4a6a;
}
QLineEdit:focus { border-color: #3f7bbd; }
QPushButton {
    background: #1d2732; border: 1px solid #2c3845; border-radius: 6px;
    padding: 4px 12px; color: #cfd8e3;
}
QPushButton:hover { background: #263340; }
QPushButton:pressed { background: #18212b; }
QPushButton:disabled { color: #5b6773; background: #161c24; }
#primaryBtn { background: #245a91; border-color: #2f6fae; color: #eaf3ff; font-weight: 600; }
#primaryBtn:hover { background: #2b6aa8; }
#sectionTitle { color: #8fa1b4; font-weight: 600; letter-spacing: 1px; }
#serverStatus { color: #9fb0c2; font-size: 12px; }
#apiHint { color: #6b7c8f; font-size: 11px; }
QCheckBox { color: #96a5b6; font-size: 12px; spacing: 5px; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #3a4653; border-radius: 3px; background: #0b1016; }
QCheckBox::indicator:checked { background: #2f6fae; border-color: #3f7bbd; }
#logView {
    background: #0b1016; border: 1px solid #232b35; border-radius: 6px;
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px;
    color: #c3d0dd; padding: 4px;
}
#loadBar { background: #0b1016; border: none; }
#loadBar::chunk { background: #3f7bbd; }
QProgressBar { border: none; }
QSplitter::handle { background: #232b35; width: 1px; }
"""
