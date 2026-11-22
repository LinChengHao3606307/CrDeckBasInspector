"""Cloudflare 人机校验：定位 Turnstile 组件，用真实鼠标点击并等待通过。

Turnstile 小组件在封闭 shadow DOM 里，从文档 DOM 查不到内部 iframe，
因此定位分两步：先用主文档里带 cf 标记的元素矩形；没有可用矩形时，
抓取渲染视图的像素图，找宽度约 220~400 CSS 像素的亮色横线（组件边框）。
"""

import random
import time

from .api_server import PENDING

JS_PROBE = """
(function(){
  var challenge = !!document.querySelector(
    '#challenge-form, #cf-challenge-running, [id^="cf-chl-"], [name="cf-turnstile-response"]');
  return JSON.stringify({
    title: document.title || '',
    url: location.href,
    challenge: challenge,
    text: (document.body ? document.body.innerText : '').slice(0, 300)
  });
})()
"""

# 校验页上组件在两种状态间切换：等待点击 / 正在验证。正在验证时再点击无效。
# 中英文页面共用这段文案判断；判断不了就按超时处理。
VERIFYING_MARKS = (
    "verifying you are human",
    "正在验证您是否是真人",
    "这可能需要几秒钟时间",
)

CHALLENGE_TITLES = (
    "just a moment",
    "just a moment...",
    "checking your browser",
    "attention required",
    "one moment, please",
    "verify you are human",
    "请稍候",
    "正在进行安全验证",
    "安全检查",
)


def _is_challenge(probe):
    title = str(probe.get("title") or "").strip().lower()
    if any(mark in title for mark in CHALLENGE_TITLES):
        return True
    return bool(probe.get("challenge"))


def _probe(backend):
    def fn(job):
        backend._page.runJavaScript(JS_PROBE, lambda res: backend._js_result(job, res))
        return PENDING

    return backend._dispatcher.submit(fn, timeout=15)


JS_ENV = """
(function(){
  return JSON.stringify({
    focus: document.hasFocus(),
    vis: document.visibilityState,
    webdriver: !!navigator.webdriver,
    ua: navigator.userAgent,
    plugins: navigator.plugins ? navigator.plugins.length : -1,
    pluginNames: navigator.plugins ? Array.prototype.map.call(navigator.plugins, function(p){return p.name}).join('|') : '',
    languages: navigator.languages ? navigator.languages.join(',') : '',
    inner: [innerWidth, innerHeight].join('x'),
    outer: [outerWidth, outerHeight].join('x'),
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight].join('x'),
    dpr: devicePixelRatio,
    cores: navigator.hardwareConcurrency,
    mem: navigator.deviceMemory || -1,
    tz: (new Date()).getTimezoneOffset() + '/' + (Intl.DateTimeFormat().resolvedOptions().timeZone || ''),
    chrome: !!window.chrome,
    webgl: (function(){try{var c=document.createElement('canvas');var g=c.getContext('webgl');return g?(g.getParameter(g.VENDOR)+' / '+g.getParameter(g.RENDERER)):'none'}catch(e){return 'err'}})()
  });
})()
"""


def _env(backend):
    def fn(job):
        backend._page.runJavaScript(JS_ENV, lambda res: backend._js_result(job, res))
        return PENDING

    try:
        return backend._dispatcher.submit(fn, timeout=15)
    except Exception as exc:
        return {"error": repr(exc)}


def _front_window(backend):
    """把浏览器窗口提到最前并激活：被其它窗口遮挡时 visibilityState 会变成
    hidden，Turnstile 判定校验不通过。"""
    def fn(job):
        view = backend._view
        win = view.window()
        if win is None:
            return {"active": False, "error": "no window"}
        win.raise_()
        win.activateWindow()
        view.setFocus()
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = int(win.winId())
            u.ShowWindow(hwnd, 9)
            u.SetForegroundWindow(hwnd)
            u.BringWindowToTop(hwnd)
        except Exception:
            pass
        return {"active": bool(win.isActiveWindow())}

    try:
        return backend._dispatcher.submit(fn, timeout=15)
    except Exception as exc:
        return {"active": False, "error": repr(exc)}


def _grab_widget(backend):
    """在 Qt 主线程抓取视图，按像素找 Turnstile 组件外框，返回 CSS 坐标。"""
    def fn(job):
        view = backend._view
        if view is None:
            return None
        img = view.grab().toImage().convertToFormat(img_format())
        if img.width() <= 0 or img.height() <= 0:
            return None
        return _boxes_from_image(img)

    return backend._dispatcher.submit(fn, timeout=15)


def img_format():
    from PySide6.QtGui import QImage
    return QImage.Format.Format_RGB32


def _boxes_from_image(img):
    w, h = img.width(), img.height()
    stride = img.bytesPerLine()
    mv = img.constBits()
    sx = max(1, w // 960)
    sy = max(1, h // 540)
    css_w = 1920.0 / w
    css_h = 1080.0 / h
    rows = []
    for y in range(0, h, sy):
        base = y * stride
        run_start = None
        last_x = 0
        for x in range(0, w, sx):
            off = base + x * 4
            b, g, r = mv[off], mv[off + 1], mv[off + 2]
            bright = b > 180 and g > 180 and r > 180
            if bright:
                if run_start is None:
                    run_start = x
                last_x = x
            elif run_start is not None:
                _collect(rows, run_start, last_x, sx, y, css_w, css_h)
                run_start = None
        if run_start is not None:
            _collect(rows, run_start, last_x, sx, y, css_w, css_h)

    groups = {}
    for row in rows:
        key = (int(round(row["x0"] / 15.0)), int(round(row["x1"] / 15.0)))
        group = groups.get(key)
        if group is None:
            groups[key] = {"y0": row["y"], "y1": row["y"], "x0_min": row["x0"],
                           "x1_max": row["x1"], "rows": 1}
        else:
            group["y0"] = min(group["y0"], row["y"])
            group["y1"] = max(group["y1"], row["y"])
            group["x0_min"] = min(group["x0_min"], row["x0"])
            group["x1_max"] = max(group["x1_max"], row["x1"])
            group["rows"] += 1

    boxes = []
    for group in groups.values():
        box_w = group["x1_max"] - group["x0_min"]
        box_h = group["y1"] - group["y0"]
        if 200 <= box_w <= 420 and 36 <= box_h <= 100:
            boxes.append({"x": group["x0_min"], "y": group["y0"],
                          "w": box_w, "h": box_h, "rows": group["rows"]})
    boxes.sort(key=lambda b: (b["w"] * b["h"]), reverse=True)
    return boxes


def _collect(rows, run_start, last_x, sx, y, css_w, css_h):
    css_len = (last_x - run_start + sx) * css_w
    if 200 <= css_len <= 430:
        rows.append({"y": y * css_h, "x0": run_start * css_w,
                     "x1": (last_x + sx) * css_w})


def _verifying(probe):
    text = str(probe.get("text") or "").lower()
    return any(mark in text for mark in VERIFYING_MARKS)


def _checkbox_ready(img, box):
    """卡片左侧是否画出了待勾选的方框。

    加载期组件只有一个转圈动画（绿色小点），没有任何方框；只有灰色的
    方框描边出现，才说明勾选框已经就位、可以点了。只看卡片最左边
    30 CSS 像素，避开右侧的文字。
    """
    w = img.width()
    if w <= 0:
        return False
    scale = float(w) / 1920.0
    bx0 = int(round(box["x"] * scale))
    bx1 = int(round((box["x"] + box["w"]) * scale))
    by0 = int(round(box["y"] * scale))
    by1 = int(round((box["y"] + box["h"]) * scale))
    x_lo = bx0 + 3
    x_hi = min(bx1, bx0 + max(24, int(40 * scale)))
    rows = 0
    for y in range(by0 + 4, by1 - 2):
        gray = []
        for x in range(x_lo, x_hi):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            if abs(r - g) <= 6 and abs(g - b) <= 6 and 120 <= r <= 205:
                gray.append(x)
        if len(gray) >= 2 and (gray[-1] - gray[0]) >= max(4, int(5 * scale)):
            rows += 1
    return rows >= max(3, int(4 * scale))


def _grab_card(backend):
    """抓一张视图，返回卡片外框及勾选框是否就位。"""
    def fn(job):
        view = backend._view
        if view is None:
            return None
        img = view.grab().toImage().convertToFormat(img_format())
        if img.width() <= 0 or img.height() <= 0:
            return None
        boxes = _boxes_from_image(img)
        for box in boxes:
            if _checkbox_ready(img, box):
                return {"x": box["x"], "y": box["y"], "w": box["w"],
                        "h": box["h"], "ready": True}
        if not boxes:
            return None
        box = boxes[0]
        return {"x": box["x"], "y": box["y"], "w": box["w"],
                "h": box["h"], "ready": False}

    return backend._dispatcher.submit(fn, timeout=15)


def pixel_boxes(backend):
    """返回当前视图里识别到的 Turnstile 组件框（CSS 坐标），供调试定位。"""
    return _grab_widget(backend) or []



def _move(backend, x, y):
    backend.mouse({"action": "move", "x": round(x, 1), "y": round(y, 1), "native": True})


def _approach(backend, fx, fy, tx, ty, seconds=1.2):
    """较快地接近目标，步子大一些但仍有抖动。"""
    start = time.time()
    deadline = start + seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        frac = min(1.0, (time.time() - start) / seconds)
        frac = frac * frac * (3 - 2 * frac)
        x = fx + (tx - fx) * frac
        y = fy + (ty - fy) * frac
        _move(backend, x, y)
        time.sleep(max(0.012, min(0.03, left / 8)))
    _move(backend, tx, ty)


def _wander(backend, start, seconds):
    """到达页面后像人一样在页面上游走几段，给校验组件留下指针历史。"""
    x, y = start
    deadline = time.time() + seconds
    vx, vy = backend.viewport_size()
    while time.time() < deadline - 0.5:
        tx = min(vx - 60, max(60, x + random.uniform(-260, 260)))
        ty = min(vy - 60, max(60, y + random.uniform(-180, 180)))
        _approach(backend, x, y, tx, ty, seconds=random.uniform(0.6, 1.1))
        x, y = tx, ty
        time.sleep(random.uniform(0.15, 0.5))
    return x, y


def _press(backend, x, y, hold=0.09, jitter=0.0):
    backend.mouse({"action": "down", "x": round(x, 1), "y": round(y, 1), "native": True})
    time.sleep(hold)
    backend.mouse({"action": "up", "x": round(x + random.uniform(-jitter, jitter), 1),
                   "y": round(y + random.uniform(-jitter, jitter), 1), "native": True})


def _glide(backend, fx, fy, tx, ty, seconds):
    """滑向目标：轨迹与节拍都按真人录像的特征生成，而不是笔画式的
    匀速贝塞尔加高频投递。"""
    from .mouse import human_path
    pts = human_path(fx, fy, tx, ty)
    step_ms = int(round(seconds * 1000.0 / len(pts)))
    step_ms = max(10, min(22, step_ms))
    pts = [[round(p[0], 2), round(p[1], 2)] for p in pts]
    backend.mouse({"action": "move", "points": pts, "step_ms": step_ms,
                   "native": True, "x": round(tx, 1), "y": round(ty, 1)})


def _await_clickable(backend, deadline):
    """等到勾选框真正出现且位置稳定。

    卡片一出现时还处在加载期（页面文案「正在验证您是否是真人」），这时
    没有勾选框，点了只会白费一轮。所以必须同时满足：页面不在验证中、
    像素识别到 200 多像素宽的亮框、位置两次一致。
    """
    prev = None
    while time.time() < deadline:
        try:
            card = _grab_card(backend)
        except Exception:
            card = None
        if card is None or not card.get("ready"):
            prev = None
            time.sleep(0.4)
            continue
        try:
            probe = _probe(backend)
        except Exception:
            probe = None
        if probe is not None and _verifying(probe):
            prev = None
            time.sleep(0.4)
            continue
        if prev is not None and abs(card["x"] - prev["x"]) <= 8 \
                and abs(card["y"] - prev["y"]) <= 8 \
                and abs(card["w"] - prev["w"]) <= 12:
            return {"x": card["x"], "y": card["y"], "w": card["w"],
                    "h": card["h"], "source": "pixels"}
        prev = card
        time.sleep(0.7)
    return None


def _stable_clear(backend, seconds=3.5):
    """校验标记消失后要稳定一段时间，避免 CF 校验通过后又循环回校验页。"""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        time.sleep(0.7)
        try:
            last = _probe(backend)
        except Exception:
            continue
        if _is_challenge(last):
            return False, last
    return True, last


def _run_log(record):
    """把每轮尝试的轨迹与结果追加到 recordings/cloudflare_runs.jsonl。"""
    import json
    import os
    from . import config
    path = os.path.join(config.APP_DIR, "recordings", "cloudflare_runs.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def solve(backend, attempts=3, find_timeout=25.0, click_timeout=20.0):
    started = time.time()
    try:
        probe = _probe(backend)
    except Exception as exc:
        return {"ok": False, "challenge": False, "solved": False, "error": repr(exc)}
    if not _is_challenge(probe):
        return {"ok": True, "challenge": False, "solved": False,
                "title": probe.get("title", ""), "url": probe.get("url", ""),
                "note": "当前页面不是 Cloudflare 校验页"}

    attempts = max(1, min(int(attempts), 5))
    tried = []
    warm = _probe(backend)
    env = _env(backend)
    front = _front_window(backend)
    time.sleep(0.4)
    env_after = _env(backend)
    vp = backend.viewport_size()
    cur = (vp[0] * random.uniform(0.62, 0.85), vp[1] * random.uniform(0.62, 0.88))
    # 真人到校验页后停留一两秒、指针活动几段，再去点组件
    cur = _wander(backend, cur, random.uniform(2.0, 4.0))
    for attempt_no in range(1, attempts + 1):
        widget = _await_clickable(backend, time.time() + find_timeout)
        if widget is None:
            tried.append({"attempt": attempt_no, "result": "widget-not-ready"})
            break
        h = widget["h"]
        # 落点在勾选框内，纵向在中线附近。
        lx = widget["x"] + random.uniform(11.0, 28.0)
        ly = widget["y"] + h * 0.5 + random.uniform(-3.0, 4.0)
        # 直接从当前位置挪到勾选框上点一下，点完立刻挪开；勾选框再次出现
        # 说明这一轮没过，下一轮再挪回来点。
        try:
            time.sleep(random.uniform(0.2, 0.6))
            _glide(backend, cur[0], cur[1], lx, ly, random.uniform(0.55, 0.9))
            time.sleep(random.uniform(0.10, 0.24))
            # 接近过程中 CF 可能自己切换到验证中，或勾选框还没画出来；
            # 这时按下只会把它打断，等于白费一轮
            try:
                card = _grab_card(backend)
            except Exception:
                card = None
            busy = card is None or not card.get("ready")
            try:
                ready = _probe(backend)
            except Exception:
                ready = None
            if busy or (ready is not None and _verifying(ready)):
                tried.append({"attempt": attempt_no, "result": "widget-busy"})
                continue
            _press(backend, lx, ly, random.uniform(0.047, 0.093), jitter=0)
            cur = (lx, ly)
            time.sleep(random.uniform(0.12, 0.30))
            vx, vy = backend.viewport_size()
            ax = min(vx - 40.0, max(40.0, lx + random.choice((-1.0, 1.0)) * random.uniform(40.0, 140.0)))
            ay = min(vy - 40.0, max(40.0, ly + random.uniform(-60.0, 90.0)))
            _approach(backend, lx, ly, ax, ay, seconds=random.uniform(0.3, 0.55))
            cur = (ax, ay)
        except Exception as exc:
            tried.append({"attempt": attempt_no, "result": "click-error", "error": repr(exc)})
            continue

        deadline = time.time() + click_timeout
        saw_verifying = False
        now = None
        states = []
        while time.time() < deadline:
            # 真人点完后指针完全静止，这里不再游走，只轮询页面状态。
            time.sleep(0.3)
            try:
                now = _probe(backend)
            except Exception:
                continue
            text = str(now.get("text") or "")[:120].replace("\n", " / ")
            if not states or states[-1] != text:
                states.append(text)
            if not _is_challenge(now):
                stable, check = _stable_clear(backend)
                if stable:
                    check = check or now
                    result = {"ok": True, "challenge": True, "solved": True,
                              "method": "click", "attempts": attempt_no,
                              "elapsedMs": int((time.time() - started) * 1000),
                              "widget": widget, "title": check.get("title", ""),
                              "url": check.get("url", ""), "tried": tried}
                    _run_log({"solved": True, "attempt": attempt_no, "tried": tried,
                              "elapsedMs": result["elapsedMs"],
                              "warmTitle": warm.get("title", ""),
                              "env": env, "envAfter": env_after, "front": front})
                    return result
                break
            if _verifying(now):
                saw_verifying = True
            elif saw_verifying:
                # 验证失败后组件复位，重新回到等待点击状态；这一轮结束重试
                break
        tried.append({"attempt": attempt_no, "result": "not-solved",
                      "point": [round(lx, 1), round(ly, 1)],
                      "states": states[:4]})
        if attempt_no < attempts:
            # 失败后 CF 自己会把页面刷回校验页；等勾选框重新出现再挪回去点
            time.sleep(random.uniform(0.8, 1.6))

    try:
        final = _probe(backend)
    except Exception:
        final = {}
    _run_log({"solved": bool(not _is_challenge(final)), "attempt": attempts,
              "tried": tried, "warmTitle": warm.get("title", ""),
              "env": env, "envAfter": env_after, "front": front})
    return {"ok": False, "challenge": True, "solved": bool(not _is_challenge(final)),
            "attempts": attempts, "tried": tried,
            "elapsedMs": int((time.time() - started) * 1000),
            "title": final.get("title", ""), "url": final.get("url", "")}
