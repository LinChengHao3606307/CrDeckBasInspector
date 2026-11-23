"""只读内网卡组速览：监听所有网卡，页面仅显示玩家、部落和 8 张卡。"""

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit


class QuickViewServer:
    host = "0.0.0.0"

    def __init__(self, port):
        self.port = int(port)
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()
        self._data = {"name": "", "clan": "", "cards": []}

    @property
    def running(self):
        return self._httpd is not None

    def start(self):
        if self._httpd is not None:
            return
        quick_view = self

        class Handler(QuickViewHandler):
            server_view = quick_view

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.daemon_threads = True
        self.port = int(httpd.server_address[1])
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="deck-quick-view", daemon=True)
        self._thread.start()

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

    def update(self, payload):
        """接收 /royaleapi/deck 的原始成功响应，只保留速览需要的字段。"""
        matched = payload.get("matched") or {}
        clan = matched.get("clan") or {}
        deck = payload.get("deck") or {}
        raw_cards = deck.get("cards") if isinstance(deck, dict) else deck
        cards = []
        for card in (raw_cards or [])[:8]:
            if isinstance(card, dict):
                name = str(card.get("name") or "")
                image = str(card.get("src") or "")
            else:
                name = str(card)
                image = ""
            if image:
                image = urljoin("https://royaleapi.com", image)
                if urlsplit(image).scheme not in ("http", "https"):
                    image = ""
            cards.append({"name": name, "src": image})
        data = {
            "name": str(matched.get("name") or payload.get("query") or ""),
            "clan": str(clan.get("name") or "") if isinstance(clan, dict) else str(clan),
            "cards": cards,
        }
        with self._lock:
            self._data = data

    def snapshot(self):
        with self._lock:
            data = self._data
            return {"name": data["name"], "clan": data["clan"],
                    "cards": [dict(card) for card in data["cards"]]}

    def render(self):
        data = self.snapshot()
        cards = []
        for card in data["cards"][:8]:
            name = html.escape(card["name"])
            image = ""
            if card["src"]:
                image = '<img src="%s" alt="%s" loading="eager">' % (
                    html.escape(card["src"], quote=True), name)
            cards.append('<figure class="card">%s<figcaption>%s</figcaption></figure>'
                         % (image, name))
        return PAGE_TEMPLATE % {
            "name": html.escape(data["name"]),
            "clan": html.escape(data["clan"]),
            "cards": "".join(cards),
        }


class QuickViewHandler(BaseHTTPRequestHandler):
    server_view = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        body = self.server_view.render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>Deck</title>
<style>
*{box-sizing:border-box}html,body{margin:0;min-height:100%%;background:#090d12;color:#f5f7fa}
body{font-family:Arial,"Microsoft YaHei",sans-serif}main{max-width:900px;margin:auto;padding:20px}
h1{margin:0;text-align:center;font-size:clamp(24px,4vw,42px);line-height:1.2}
.clan{min-height:1.4em;margin:8px 0 20px;text-align:center;color:#aebdce;font-size:clamp(16px,2.5vw,25px)}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.card{min-width:0;margin:0}.card img{display:block;width:100%%;aspect-ratio:5/6;object-fit:contain}
figcaption{overflow:hidden;text-align:center;color:#e6edf3;font-size:clamp(10px,1.8vw,16px);line-height:1.25}
@media(max-width:480px){main{padding:12px}.clan{margin-bottom:12px}.cards{gap:6px}}
</style>
</head>
<body><main>
<h1>%(name)s</h1>
<div class="clan">%(clan)s</div>
<div class="cards">%(cards)s</div>
</main></body>
</html>
"""
