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

# 隐身补丁：把 QtWebEngine 与真 Chrome 之间会被 Cloudflare Turnstile 检出的
# 指纹差异补齐。所有 frame（含 challenges.cloudflare.com 跨源 iframe）都注入，
# 保证父页面与挑战 frame 看到一致的值。
#
# 关键约束：
# - 不修改 Function.prototype.toString（跨源 iframe 可用自己 realm 的原生
#   toString 拆穿补丁）。需要伪装的函数一律用 fn.bind(null) —— V8 对绑定函数
#   原生输出 "function () { [native code] }"，跨 realm 调用同样成立。
# - 方法内部不依赖 this（绑定后 this 为 null），全部走闭包引用。
# - 嵌入进程靠 DPI 环境变量原生报出 1920x1080@1，几何指标只在原生值不符时
#   才覆盖，让绝大多数 getter 保持引擎原生。
STEALTH_SCRIPT_TEMPLATE = r"""
(function () {
  if (window.__aapiStealth) return;
  Object.defineProperty(window, '__aapiStealth', {
    value: true, writable: false, enumerable: false, configurable: false
  });

  function asNative(fn) { try { return fn.bind(null); } catch (e) { return fn; } }
  function defineGetter(obj, name, value) {
    try {
      Object.defineProperty(obj, name, {
        get: asNative(function () { return value; }),
        configurable: true, enumerable: true
      });
    } catch (e) {}
  }

  // ---- 屏幕色深：真 Chrome/Edge 为 24，QtWebEngine 原生为 32 ----
  defineGetter(Screen.prototype, 'colorDepth', 24);
  defineGetter(Screen.prototype, 'pixelDepth', 24);

  // ---- deviceMemory：本机真 Chrome 报 16 ----
  try {
    if ('deviceMemory' in Navigator.prototype &&
        (!navigator.deviceMemory || navigator.deviceMemory < 16)) {
      defineGetter(Navigator.prototype, 'deviceMemory', 16);
    }
  } catch (e) {}

  // ---- 内置 PDF 查看器：真 Chrome 恒为 true ----
  defineGetter(Navigator.prototype, 'pdfViewerEnabled', true);

  // ---- PluginArray / MimeTypeArray：QtWebEngine 编译时不带 PDF 插件，原生为空 ----
  // 原型上的 IDL 属性是只带原生 getter 的访问器：普通赋值在 sloppy 模式下静默
  // 失败，读操作落到原生 getter 又会因接收者不是 C++ 原生对象而抛 Illegal
  // invocation。正确做法是 Object.create(原生原型) 后用 defineProperty 安装自有
  // 数据属性遮蔽它们；这样 instanceof、constructor、Symbol.toStringTag 全部自然
  // 成立，未遮蔽的方法（如 refresh）再补一个 no-op 绑定函数。
  try {
    if (navigator.plugins && navigator.plugins.length === 0 &&
        typeof PluginArray === 'function' && typeof Plugin === 'function' &&
        typeof MimeTypeArray === 'function' && typeof MimeType === 'function') {

      function fakeNativeObject(proto, props) {
        var obj = Object.create(proto);
        Object.keys(props).forEach(function (k) {
          Object.defineProperty(obj, k, {
            value: props[k], writable: true, enumerable: true, configurable: true
          });
        });
        return obj;
      }

      var PLUGIN_NAMES = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                          'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
      var MIME_DEFS = [
        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
        { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
      ];

      var mimeBase = MIME_DEFS.map(function (def) {
        return fakeNativeObject(MimeType.prototype, {
          type: def.type,
          suffixes: def.suffixes,
          description: def.description,
          enabledPlugin: null
        });
      });

      var plugins = PLUGIN_NAMES.map(function (name) {
        var props = {
          name: name,
          filename: 'internal-pdf-viewer',
          description: 'Portable Document Format',
          length: mimeBase.length
        };
        for (var i = 0; i < mimeBase.length; i++) props[i] = mimeBase[i];
        var self = null;
        props.item = asNative(function (idx) {
          var n = parseInt(idx, 10);
          return (n >= 0 && n < self.length) ? self[n] : null;
        });
        props.namedItem = asNative(function (n) {
          for (var i = 0; i < self.length; i++) if (self[i].type === n) return self[i];
          return null;
        });
        self = fakeNativeObject(Plugin.prototype, props);
        return self;
      });

      var paProps = { length: plugins.length };
      plugins.forEach(function (p, i) { paProps[i] = p; });
      var pluginArr = null;
      paProps.item = asNative(function (idx) {
        var n = parseInt(idx, 10);
        return (n >= 0 && n < pluginArr.length) ? pluginArr[n] : null;
      });
      paProps.namedItem = asNative(function (n) {
        for (var i = 0; i < pluginArr.length; i++) {
          if (pluginArr[i].name === n) return pluginArr[i];
          for (var j = 0; j < pluginArr[i].length; j++) {
            if (pluginArr[i][j].type === n) return pluginArr[i];
          }
        }
        return null;
      });
      paProps.refresh = asNative(function () {});
      pluginArr = fakeNativeObject(PluginArray.prototype, paProps);

      var maProps = { length: mimeBase.length };
      mimeBase.forEach(function (m, i) { maProps[i] = m; });
      var mimeArr = null;
      maProps.item = asNative(function (idx) {
        var n = parseInt(idx, 10);
        return (n >= 0 && n < mimeArr.length) ? mimeArr[n] : null;
      });
      maProps.namedItem = asNative(function (n) {
        for (var i = 0; i < mimeArr.length; i++) if (mimeArr[i].type === n) return mimeArr[i];
        return null;
      });
      mimeArr = fakeNativeObject(MimeTypeArray.prototype, maProps);

      // 真 Chrome 里每个 MimeType 的 enabledPlugin 指向第一个插件。
      // target 是普通对象，直接改代理属性即可写回 target。
      mimeBase.forEach(function (m) { m.enabledPlugin = plugins[0]; });

      defineGetter(Navigator.prototype, 'plugins', pluginArr);
      defineGetter(Navigator.prototype, 'mimeTypes', mimeArr);
    }
  } catch (e) {}

  // ---- window.chrome：QtWebEngine 只有空对象，补齐真 Chrome 的三件套 ----
  try {
    if (!window.chrome || Object.keys(window.chrome).length === 0) {
      var loadTimes = asNative(function () {
        var t = Date.now();
        return {
          requestTime: t / 1000 - 5,
          startLoadTime: t - 5000,
          commitLoadTime: t - 4800,
          domContentLoadedEventEnd: t - 1500,
          domInteractive: t - 1600,
          finishDocumentLoadTime: t - 1200,
          finishLoadTime: t - 1000,
          firstPaintAfterLoadTime: 0,
          firstPaintTime: t - 4200,
          loadEventEnd: t - 1000,
          navigationType: 'Other',
          wasFetchedViaSpdy: true,
          wasNpnNegotiated: true,
          npnNegotiatedProtocol: 'h2',
          wasAlternateProtocolAvailable: false,
          connectionInfo: 'h2'
        };
      });
      var csi = asNative(function () {
        var t = Date.now();
        return { startE: t - 5000, onloadT: t - 1000, pageT: 4000, tran: 15 };
      });
      var app = {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
      };
      Object.defineProperty(window, 'chrome', {
        value: { loadTimes: loadTimes, csi: csi, app: app },
        writable: true, configurable: true, enumerable: true
      });
    }
  } catch (e) {}

  // ---- chrome.runtime：不要伪造。本机真 Chrome 157 在普通网页上实测
  // window.chrome 只有 loadTimes/csi/app 三个键，chrome.runtime 为 undefined
  // （该 API 早已从非扩展页面移除）。此前版本在这里补了一个带 connect/
  // getURL/onMessage 的假 runtime 对象，反而成为"真实浏览器不存在"的指纹。
  // 保持缺失，与真 Chrome 一致。

  // ---- navigator.userAgentData：无品牌 Chromium 的 brands 只有 GREASE 占位
  // （Not=A?Brand）和 Chromium，缺 "Google Chrome"，和 UA 字符串、navigator.vendor
  // 矛盾。按真实引擎 140（构建号 140.0.7339.225）补齐，不冒充更高版本。对象挂在
  // 原生 NavigatorUAData 原型链上（instanceof / Symbol.toStringTag 自然成立），
  // 自有数据属性 brands/mobile/platform 可枚举，方法放下层原型且用绑定函数，
  // toString 仍为 [native code]。每个 frame 各自构造，Turnstile 跨源 iframe 同样生效。
  try {
    var nativeUAD = navigator.userAgentData;
    if (nativeUAD) {
      // 顺序与 sec-ch-ua 头一致：Chromium → GREASE → Google Chrome。
      // 由 Chromium user_agent_utils.cc 的确定性洗牌推出（seed=140 →
      // orders[2]={1,0,2}），真 Chrome 140 只会是这个排列。
      // 这三个值由 stealth_source() 注入；真实 Chrome 过检后会被替换成它的实测值。
      var UAD_BRANDS = __UAD_BRANDS__;
      var UAD_FULL = __UAD_FULL__;
      // 标量高熵值取本机实测基线；platform/mobile 直接沿用原生对象避免漂移。
      // 注意标准提示名是 uaFullVersion（返回键同名），不是 fullVersion。
      var UAD_SCALAR = __UAD_SCALAR__;
      var uadPlatform = nativeUAD.platform || 'Windows';
      var uadMobile = !!nativeUAD.mobile;
      var uadMethods = Object.create(Object.getPrototypeOf(nativeUAD));
      Object.defineProperty(uadMethods, 'getHighEntropyValues', {
        value: asNative(function (hints) {
          var result = { brands: UAD_BRANDS, mobile: uadMobile, platform: uadPlatform };
          if (hints && typeof hints.length === 'number') {
            for (var i = 0; i < hints.length; i++) {
              var h = String(hints[i]);
              if (h === 'fullVersionList') result.fullVersionList = UAD_FULL;
              else if (Object.prototype.hasOwnProperty.call(UAD_SCALAR, h)) {
                result[h] = UAD_SCALAR[h];
              }
            }
          }
          return Promise.resolve(result);
        }), writable: true, enumerable: false, configurable: true
      });
      Object.defineProperty(uadMethods, 'toJSON', {
        value: asNative(function () {
          return { brands: UAD_BRANDS, mobile: uadMobile, platform: uadPlatform };
        }), writable: true, enumerable: false, configurable: true
      });
      var fakeUAD = Object.create(uadMethods);
      Object.defineProperty(fakeUAD, 'brands', {
        value: UAD_BRANDS, writable: true, enumerable: true, configurable: true
      });
      Object.defineProperty(fakeUAD, 'mobile', {
        value: uadMobile, writable: true, enumerable: true, configurable: true
      });
      Object.defineProperty(fakeUAD, 'platform', {
        value: uadPlatform, writable: true, enumerable: true, configurable: true
      });
      defineGetter(Navigator.prototype, 'userAgentData', fakeUAD);
    }
  } catch (e) {}

  // ---- NetworkInformation.rtt：QtWebEngine 没有网络质量评估，恒报 0ms；
  // 真 Chrome 的 rtt 按 25ms 量化且不可能为 0（本机基线 150ms）。0 是
  // headless/嵌入式环境的经典信号。downlink=10 是 API 规定的上限桶，保留不动。
  try {
    var conn = navigator.connection;
    if (conn && conn.rtt === 0 && typeof NetworkInformation !== 'undefined') {
      Object.defineProperty(NetworkInformation.prototype, 'rtt', {
        get: asNative(function () { return 100; }),
        configurable: true, enumerable: true
      });
    }
  } catch (e) {}

  // ---- navigator.usb：真 Chrome 桌面端恒暴露 WebUSB（USB 继承 EventTarget），
  // QtWebEngine 编译时裁掉了该特性（hid/serial/bluetooth 都在，唯独 usb 缺失），
  // 功能探测 navigator.usb 会直接露馅。补一个无设备的标准形态对象：
  // getDevices 空数组、requestDevice 按规范 reject、事件接口走 EventTarget。
  try {
    if (!navigator.usb && typeof EventTarget !== 'undefined') {
      var usb = Object.create(EventTarget.prototype);
      Object.defineProperty(usb, 'getDevices', {
        value: asNative(function () { return Promise.resolve([]); }),
        writable: true, enumerable: true, configurable: true
      });
      Object.defineProperty(usb, 'requestDevice', {
        value: asNative(function () {
          return Promise.reject(new DOMException('No device selected.', 'NotFoundError'));
        }),
        writable: true, enumerable: true, configurable: true
      });
      ['onconnect', 'ondisconnect'].forEach(function (name) {
        var handler = null;
        Object.defineProperty(usb, name, {
          get: asNative(function () { return handler; }),
          set: asNative(function (v) { handler = v; }),
          configurable: true, enumerable: true
        });
      });
      defineGetter(Navigator.prototype, 'usb', usb);
    }
  } catch (e) {}

  // ---- devicePixelRatio：嵌入宿主 BrowserHost 用 zoomFactor（当前 0.25）把
  // 1920x1080 的 CSS 视口缩放进小窗格，而 Chromium 会把页面缩放乘进
  // window.devicePixelRatio，于是页面读到 0.25 —— 真实桌面浏览器只会报
  // >=1 的值（1/1.25/1.5/2），0.25 本身就是自动化标记。这里无条件安装归一
  //  getter（不能按注入时的瞬时值判断：脚本在文档创建期运行，此刻宿主可能还
  // 没把 zoom 从默认 1.0 压到 0.25，等它之后改 zoom 就没有 getter 兜底了），
  // 与 1920x1080@100% 的屏幕自述保持自洽。getter 一旦安装即可存活后续 zoom
  // 变化。Python 侧坐标换算走 Qt 自己的 devicePixelRatioF()，不读这个 JS
  // 属性，所以不影响鼠标映射。----
  try {
    defineGetter(window, 'devicePixelRatio', 1);
  } catch (e) {}

  // ---- 几何自洽（嵌入式宿主专用，所有 frame 统一生效）----
  // BrowserHost 把页面固定成 1920x1080 视口再整体缩放，且 QtWebEngine 没有
  // 浏览器外壳，于是 outer==inner==1920x1080，innerHeight(1080) 甚至超过屏幕
  // 可用高度（availHeight 1030）。真实浏览器窗口必然有边框/工具栏（outer>inner）、
  // 视口也不可能高过工作区——这是 headless/嵌入式的强信号，且历史上每次失败的
  // 运行都带着它。这里把"屏幕"叙述成更大的 2560x1440@100% 显示器、当前是一个
  // 普通窗口化 Chrome：inner 视口保持真实的 1920x1080 不动（避免与布局、元素
  // getBoundingClientRect 冲突），让 inner < outer < avail < screen 逐层成立。
  // 必须无条件、逐 frame 统一安装：若用顶层特有的 inner 尺寸做判断，跨源
  // Turnstile iframe（自身 inner 很小）会漏装，反而造成父子屏幕值不一致。
  // iframe 的 outer* 本就应等于顶层窗口值，inner 保持各自真实尺寸，语义与真浏览器一致。
  try {
    var SCR_W = 2560, SCR_H = 1440, SCR_AH = 1392;   // 48px 任务栏，与本机实测一致
    // outer 比 inner（真实 1920x1080）多出 Win11 窗口边框（横向各 8px）与
    // 标题栏/工具栏（纵向约 110px）；x=312 使窗口在 2560 屏上水平居中。
    var OUT_W = 1936, OUT_H = 1190, POS_X = 312, POS_Y = 100;
    defineGetter(Screen.prototype, 'width', SCR_W);
    defineGetter(Screen.prototype, 'height', SCR_H);
    defineGetter(Screen.prototype, 'availWidth', SCR_W);
    defineGetter(Screen.prototype, 'availHeight', SCR_AH);
    defineGetter(window, 'outerWidth', OUT_W);
    defineGetter(window, 'outerHeight', OUT_H);
    defineGetter(window, 'screenX', POS_X);
    defineGetter(window, 'screenY', POS_Y);
    defineGetter(window, 'screenLeft', POS_X);
    defineGetter(window, 'screenTop', POS_Y);
  } catch (e) {}
})();
"""


# 默认 UA-CH 配置：与内嵌 Chromium 140.0.7339.225 对应的真实 Chrome 140 形态
# （品牌顺序/GREASE 由 Chromium user_agent_utils.cc 的确定性算法推出）。
UAD_DEFAULT = {
    "brands": [
        {"brand": "Chromium", "version": "140"},
        {"brand": "Not=A?Brand", "version": "24"},
        {"brand": "Google Chrome", "version": "140"},
    ],
    "fullVersionList": [
        {"brand": "Chromium", "version": "140.0.7339.225"},
        {"brand": "Not=A?Brand", "version": "24.0.0.0"},
        {"brand": "Google Chrome", "version": "140.0.7339.225"},
    ],
    "scalar": {
        "architecture": "x86", "bitness": "64", "model": "",
        "platformVersion": "19.0.0", "wow64": False,
        "uaFullVersion": "140.0.7339.225",
    },
}


def stealth_source(uad=None):
    """渲染隐身脚本；uad 可传入真实 Chrome 实测的 brands/fullVersionList/scalar。"""
    import json
    cfg = uad or UAD_DEFAULT
    return (STEALTH_SCRIPT_TEMPLATE
            .replace("__UAD_BRANDS__", json.dumps(cfg.get("brands") or []))
            .replace("__UAD_FULL__", json.dumps(cfg.get("fullVersionList") or []))
            .replace("__UAD_SCALAR__", json.dumps(cfg.get("scalar") or {})))


STEALTH_SCRIPT = stealth_source()


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
