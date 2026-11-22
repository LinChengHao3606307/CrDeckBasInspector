"""流量捕获：请求拦截器 + 页面 console 上报（fetch/XHR/WebSocket 响应）。"""

import html
import json

from PySide6.QtCore import QByteArray
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineUrlRequestInterceptor

PREFIX = "__AUTOAPI__"


def _text(value):
    try:
        if isinstance(value, QByteArray):
            return value.data().decode("utf-8", "replace")
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)
    except Exception:
        return "?"


class RequestInterceptor(QWebEngineUrlRequestInterceptor):
    """记录所有外发请求（文档、脚本、图片、XHR、WebSocket 握手等）。"""

    def __init__(self, sink, parent=None):
        super().__init__(parent)
        self._sink = sink  # 可调用对象，接收 dict；跨线程由 Qt 排队

    def interceptRequest(self, info):
        try:
            headers = {}
            try:
                raw = info.httpHeaders()
                for key, value in (raw.items() if hasattr(raw, "items") else raw):
                    headers[_text(key)] = _text(value)
                    if len(headers) >= 40:
                        break
            except Exception:
                pass
            entry = {
                "type": "request",
                "method": _text(info.requestMethod()),
                "url": info.requestUrl().toString(),
                "resource": _text(info.resourceType().name),
                "headers": headers,
            }
            self._sink(entry)
        except Exception:
            pass


class BrowserPage(QWebEnginePage):
    """页面 console：转发 __AUTOAPI__ 上报，并把页面自身的 warn/error 记入日志。"""

    def __init__(self, profile, sink, parent=None):
        super().__init__(profile, parent)
        self._sink = sink

    def createWindow(self, _window_type):
        # target=_blank 等新窗口请求在当前视图打开
        return self

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        if message.startswith(PREFIX):
            try:
                self._sink(json.loads(message[len(PREFIX):]))
            except (TypeError, ValueError):
                pass
            return
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            label = "error"
        elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel:
            label = "warn"
        else:
            return
        self._sink({
            "type": "console",
            "level": label,
            "message": message,
            "source": source_id,
            "line": line_number,
        })


def _esc(text):
    return html.escape(str(text), quote=False)


def _origin(url, limit=140):
    url = str(url)
    return _esc(url if len(url) <= limit else url[:limit] + "…")


def _human_size(size):
    try:
        n = int(size)
    except (TypeError, ValueError):
        return None
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def format_entry(entry):
    """把一个流量/控制台条目格式化成一行（或两行）带颜色 HTML。"""
    kind = entry.get("type")
    if kind == "request":
        method = entry.get("method", "GET")
        resource = entry.get("resource", "")
        line = ('<span style="color:#4fa8ff;">&#8594;</span> <b>%s</b> %s '
                '<span style="color:#7d8fa6;">[%s]</span>') % (
            _esc(method), _origin(entry.get("url", "")), _esc(resource))
        headers = entry.get("headers") or {}
        if headers:
            summary = ", ".join("%s: %s" % (k, v) for k, v in list(headers.items())[:8])
            if len(summary) > 260:
                summary = summary[:260] + "…"
            line += ('<br><span style="color:#5c6b7d; font-size:11px;">&nbsp;&nbsp;%s</span>'
                     % _esc(summary))
        return line
    if kind == "response":
        source = entry.get("source", "")
        if entry.get("error"):
            return ('<span style="color:#ff6b6b;">&#8592;</span> <b>ERR</b> %s '
                    '<span style="color:#ff8c8c;">%s</span> <span style="color:#7d8fa6;">[%s]</span>') % (
                _origin(entry.get("url", "")), _esc(entry.get("error")), _esc(source))
        status = entry.get("status", 0)
        color = "#4cd17a;" if 200 <= int(status or 0) < 400 else "#ffb454;"
        bits = []
        size = _human_size(entry.get("size"))
        if size:
            bits.append(size)
        if entry.get("ms") is not None:
            bits.append("%s ms" % entry.get("ms"))
        if entry.get("contentType"):
            bits.append(_esc(str(entry["contentType"]).split(";")[0]))
        bits.append(source)
        line = ('<span style="color:%s">&#8592; %s</span> %s '
                '<span style="color:#7d8fa6;">(%s)</span>') % (
            color, _esc(status), _origin(entry.get("url", "")), _esc(" · ".join(bits)))
        body = entry.get("body")
        if body:
            body = " ".join(str(body).split())
            if len(body) > 220:
                body = body[:220] + "…"
            line += ('<br><span style="color:#6b7c8f; font-size:11px;">&nbsp;&nbsp;%s</span>'
                     % _esc(body))
        return line
    if kind == "ws-open":
        return ('<span style="color:#b48cff;">&#8646; WS OPEN</span> %s' % _origin(entry.get("url", "")))
    if kind == "ws-message":
        data = " ".join(str(entry.get("data", "")).split())
        if len(data) > 220:
            data = data[:220] + "…"
        return ('<span style="color:#b48cff;">&#8646; WS MSG</span> %s '
                '<span style="color:#6b7c8f;">%s</span>') % (_origin(entry.get("url", "")), _esc(data))
    if kind == "ws-close":
        return ('<span style="color:#b48cff;">&#8646; WS CLOSE</span> %s '
                '<span style="color:#6b7c8f;">%s %s</span>') % (
            _origin(entry.get("url", "")), _esc(entry.get("code", "")), _esc(entry.get("reason", "")))
    if kind == "console":
        color = "#ffb454" if entry.get("level") == "warn" else "#ff6b6b"
        return ('<span style="color:%s;">[console %s]</span> %s '
                '<span style="color:#6b7c8f;">%s:%s</span>') % (
            color, _esc(entry.get("level", "")), _esc(entry.get("message", "")),
            _origin(entry.get("source", "")), _esc(entry.get("line", "")))
    if kind == "nav":
        return ('<span style="color:#9aa7b4;">[页面]</span> %s %s') % (
            _origin(entry.get("url", "")), _esc(entry.get("note", "")))
    if kind == "api":
        color = "#ff6b6b" if entry.get("status", 200) >= 400 else "#4fa8ff"
        return ('<span style="color:%s;">[API]</span> <b>%s</b> %s '
                '<span style="color:#7d8fa6;">%s</span>') % (
            color, _esc(entry.get("method", "")), _origin(entry.get("path", "")),
            _esc(entry.get("note", "")))
    if kind == "info":
        return '<span style="color:#9aa7b4;">[信息] %s</span>' % _esc(entry.get("message", ""))
    return _esc(json.dumps(entry, ensure_ascii=False)[:300])
