"""注入到每个页面的 JS 源码。

只负责两件事：
1. 流量上报（fetch / XHR / WebSocket 的收发），通过 console.log 带前缀回传给 Python。
2. 元素定位（按 CSS 选择器返回坐标），由 Python 按需执行。

鼠标动作不使用 JS 合成事件，而是向 QWebEngineView 投递真实的 Qt 鼠标事件，
这样默认行为（链接跳转、表单提交、焦点、悬停、拖动）与真浏览器一致。
"""

TRAFFIC_SCRIPT = r"""
(function () {
  if (window.__AUTOAPI_TRAFFIC__) return;
  window.__AUTOAPI_TRAFFIC__ = true;

  var LIMIT = 2000;
  function now() { return performance.now ? performance.now() : Date.now(); }
  function snip(v, limit) {
    if (v === null || v === undefined) return null;
    var s = String(v);
    if (s.length > (limit || LIMIT)) return s.slice(0, (limit || LIMIT)) + ' ...[truncated ' + (s.length - (limit || LIMIT)) + ' chars]';
    return s;
  }
  function report(obj) {
    try { console.log('__AUTOAPI__' + JSON.stringify(obj)); } catch (e) {}
  }
  function urlOf(input) {
    try {
      if (typeof input === 'string') return input;
      if (input && typeof input.url === 'string') return input.url;
      return String(input);
    } catch (e) { return '[unknown]'; }
  }

  // ---- fetch ----
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var url = urlOf(input);
      var method = (init && init.method) || (input && input.method) || 'GET';
      var t0 = now();
      var p = origFetch.apply(this, arguments);
      p.then(function (res) {
        try {
          var ct = (res.headers && res.headers.get('content-type')) || '';
          var info = {
            type: 'response', source: 'fetch', url: url, method: method,
            status: res.status, statusText: res.statusText, contentType: ct,
            ms: Math.round(now() - t0)
          };
          if (/json|text|xml|javascript|x-www-form-urlencoded/i.test(ct)) {
            res.clone().text().then(function (t) {
              info.body = snip(t);
              info.size = t.length;
              report(info);
            }, function () { report(info); });
          } else {
            var len = res.headers && res.headers.get('content-length');
            info.size = len ? parseInt(len, 10) : null;
            report(info);
          }
        } catch (e) {}
        return res;
      }, function (err) {
        report({ type: 'response', source: 'fetch', url: url, method: method,
                 error: String(err), ms: Math.round(now() - t0) });
        throw err;
      });
      return p;
    };
  }

  // ---- XMLHttpRequest ----
  var XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    var origOpen = XHR.prototype.open;
    var origSend = XHR.prototype.send;
    XHR.prototype.open = function (method, url) {
      try { this.__aapi = { method: String(method), url: String(url), t0: now() }; } catch (e) {}
      return origOpen.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      var meta = this.__aapi || {};
      var self = this;
      try {
        this.addEventListener('loadend', function () {
          try {
            var ct = self.getResponseHeader('content-type') || '';
            var info = {
              type: 'response', source: 'xhr', url: meta.url || '',
              method: meta.method || 'GET', status: self.status,
              statusText: self.statusText, contentType: ct,
              ms: Math.round(now() - (meta.t0 || now()))
            };
            var rt = self.responseType || '';
            if (rt === '' || rt === 'text') {
              info.body = snip(self.responseText);
              info.size = (self.responseText || '').length;
            } else if (rt === 'json') {
              info.body = snip(JSON.stringify(self.response));
              info.size = info.body ? info.body.length : 0;
            } else {
              var cl = self.getResponseHeader('content-length');
              info.size = cl ? parseInt(cl, 10) : null;
            }
            report(info);
          } catch (e) {}
        });
      } catch (e) {}
      return origSend.apply(this, arguments);
    };
  }

  // ---- WebSocket ----
  var OrigWS = window.WebSocket;
  if (OrigWS) {
    var WSHook = function (url, protocols) {
      var ws = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      var u = String(url);
      try {
        report({ type: 'ws-open', url: u });
        ws.addEventListener('message', function (e) {
          var d = typeof e.data === 'string' ? snip(e.data) : '[binary]';
          report({ type: 'ws-message', url: u, data: d, size: typeof e.data === 'string' ? e.data.length : null });
        });
        ws.addEventListener('close', function (e) {
          report({ type: 'ws-close', url: u, code: e.code, reason: e.reason });
        });
      } catch (e) {}
      return ws;
    };
    WSHook.prototype = OrigWS.prototype;
    ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) {
      try { WSHook[k] = OrigWS[k]; } catch (e) {}
    });
    window.WebSocket = WSHook;
  }
})();
"""

# 视口固定为 1920x1080 CSS，但真实屏幕只有 1280x720；若如实暴露，
# innerWidth 会大于屏幕和 outerWidth，属于物理上不存在的组合。
# 这里把屏幕/窗口指标补齐成「1920x1080 全屏」自洽的数值，
# 同时补上 Chrome 默认的 PDF 插件列表（QtWebEngine 里是空的）。
VIEWPORT_SCRIPT = r"""
(function () {
  try {
    function define(obj, prop, getter) {
      try { Object.defineProperty(obj, prop, { get: getter, configurable: true, enumerable: true }); }
      catch (e) {}
    }
    var SW = 1920, SH = 1080, AH = 1030;
    define(Screen.prototype, 'width', function () { return SW; });
    define(Screen.prototype, 'height', function () { return SH; });
    define(Screen.prototype, 'availWidth', function () { return SW; });
    define(Screen.prototype, 'availHeight', function () { return AH; });
    define(Screen.prototype, 'colorDepth', function () { return 24; });
    define(Screen.prototype, 'pixelDepth', function () { return 24; });
    define(window, 'outerWidth', function () { return SW; });
    define(window, 'outerHeight', function () { return SH; });
    define(window, 'screenX', function () { return 0; });
    define(window, 'screenY', function () { return 0; });
    define(window, 'devicePixelRatio', function () { return 1; });

    if (!navigator.plugins || navigator.plugins.length === 0) {
      var mimes = [
        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
        { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
      ];
      var mimeObjs = mimes.map(function (m) {
        return { type: m.type, suffixes: m.suffixes, description: m.description,
                 enabledPlugin: null, item: function () { return this; },
                 namedItem: function () { return null; } };
      });
      var NAMES = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                   'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
      var plugins = NAMES.map(function (name) {
        var p = { name: name, filename: 'internal-pdf-viewer',
                  description: 'Portable Document Format', length: mimeObjs.length,
                  item: function (i) { return this[i] || null; },
                  namedItem: function (n) {
                    for (var i = 0; i < this.length; i++) if (this[i].type === n) return this[i];
                    return null;
                  } };
        for (var i = 0; i < mimeObjs.length; i++) p[i] = mimeObjs[i];
        return p;
      });
      for (var i = 0; i < mimeObjs.length; i++) mimeObjs[i].enabledPlugin = plugins[0];
      var pluginArr = {
        length: plugins.length,
        item: function (i) { return this[i] || null; },
        namedItem: function (n) {
          for (var i = 0; i < this.length; i++) {
            if (this[i].name === n) return this[i];
            for (var j = 0; j < this[i].length; j++) if (this[i][j].type === n) return this[i];
          }
          return null;
        }
      };
      for (var i = 0; i < plugins.length; i++) pluginArr[i] = plugins[i];
      var mimeArr = {
        length: mimeObjs.length,
        item: function (i) { return this[i] || null; },
        namedItem: function (n) {
          for (var i = 0; i < this.length; i++) if (this[i].type === n) return this[i];
          return null;
        }
      };
      for (var i = 0; i < mimeObjs.length; i++) mimeArr[i] = mimeObjs[i];
      define(Navigator.prototype, 'plugins', function () { return pluginArr; });
      define(Navigator.prototype, 'mimeTypes', function () { return mimeArr; });
    }
  } catch (e) {}
})();
"""


ELEMENT_QUERY_TEMPLATE = r"""
(function () {
  var sel = %(selector)s;
  var out = { url: location.href, title: document.title,
              viewport: { width: window.innerWidth, height: window.innerHeight },
              scroll: { x: window.scrollX, y: window.scrollY } };
  var nodes;
  try { nodes = Array.prototype.slice.call(document.querySelectorAll(sel)); }
  catch (err) { out.error = 'invalid selector: ' + String(err && err.message || err); out.count = 0; out.elements = []; return JSON.stringify(out); }
  out.count = nodes.length;
  out.elements = nodes.map(function (el, i) {
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    var visible = cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0 && (r.width > 0 || r.height > 0);
    var text = '';
    try { text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200); } catch (e) {}
    return {
      index: i, tag: el.tagName.toLowerCase(), id: el.id || null,
      classes: (typeof el.className === 'string' && el.className) ? el.className.split(/\s+/).filter(Boolean) : [],
      name: el.getAttribute ? el.getAttribute('name') : null,
      type: el.getAttribute ? el.getAttribute('type') : null,
      text: text,
      x: Math.round(r.left * 100) / 100, y: Math.round(r.top * 100) / 100,
      width: Math.round(r.width * 100) / 100, height: Math.round(r.height * 100) / 100,
      centerX: Math.round((r.left + r.width / 2) * 100) / 100,
      centerY: Math.round((r.top + r.height / 2) * 100) / 100,
      docX: Math.round((r.left + window.scrollX) * 100) / 100,
      docY: Math.round((r.top + window.scrollY) * 100) / 100,
      inViewport: r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth,
      visible: visible
    };
  });
  return JSON.stringify(out);
})()
"""


# 把指定选择器命中的第 index 个元素滚到视口中央，返回它的视口坐标。
ELEMENT_SCROLL_TEMPLATE = r"""
(function () {
  var sel = %(selector)s, idx = %(index)d;
  var nodes;
  try { nodes = document.querySelectorAll(sel); } catch (e) { return JSON.stringify({ error: 'invalid selector' }); }
  if (!nodes.length) return JSON.stringify({ error: 'no match' });
  var el = nodes[Math.min(idx, nodes.length - 1)];
  try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
  var r = el.getBoundingClientRect();
  return JSON.stringify({ count: nodes.length, x: r.left + r.width / 2, y: r.top + r.height / 2, width: r.width, height: r.height });
})()
"""
