"""RoyaleAPI 查询：按玩家名搜索，返回该玩家最近的卡组。

站点没有公开 API，搜索页和卡组页都是 Cloudflare 保护下的 HTML；这里借用
内置浏览器先走 /player/search/results?q=...，再从玩家卡组页读取卡片列表。
搜索结果不含部落，而卡组页头部有部落徽章和部落名，所以 clan 参数是逐候选
打开卡组页后按页头部落筛人。
"""

import json
import random
import time
from urllib.parse import quote

from . import cloudflare

SEARCH_URL = "https://royaleapi.com/player/search/results?q=%s"
PLAYER_URL = "https://royaleapi.com/player/%s/decks"

CHALLENGE_TITLES = ("just a moment", "请稍候", "attention required",
                    "checking your browser", "正在进行安全验证")

SEARCH_RESULT_SELECTOR = ".player_search_results__result_container a.header"
URL_JS = "JSON.stringify({url: location.href, title: document.title})"

SEARCH_JS = """
(function(){
  var out = [];
  var nodes = document.querySelectorAll('.player_search_results__result_container');
  nodes.forEach(function(el){
    var a = el.querySelector('a.header');
    var t = el.querySelector('.player_tag');
    var href = a ? a.getAttribute('href') : '';
    var tag = t ? t.textContent.trim() : '';
    out.push({
      name: a ? a.textContent.replace(/\\s+/g, ' ').trim() : '',
      href: href,
      tag: tag
    });
  });
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    count: out.length,
    results: out.slice(0, 20),
    text: (document.body ? document.body.innerText : '').slice(0, 400)
  });
})()
"""

DECKS_JS = """
(function(){
  var decks = [];
  document.querySelectorAll('div.deck_segment').forEach(function(seg){
    var nameEl = seg.querySelector('.deck_name_link a');
    var cards = [];
    seg.querySelectorAll('img.deck_card').forEach(function(img){
      cards.push({
        key: img.getAttribute('data-card-key') || '',
        name: img.getAttribute('alt') || ''
      });
    });
    if (cards.length || nameEl) {
      decks.push({
        name: nameEl ? nameEl.textContent.replace(/\\s+/g, ' ').trim() : '',
        url: nameEl ? (nameEl.getAttribute('href') || '') : '',
        cards: cards
      });
    }
  });
  var badge = document.querySelector('.clan_badge a[href^="/clan/"]');
  var clanTag = badge ? badge.getAttribute('href').replace('/clan/', '').trim() : '';
  var clanName = '';
  if (clanTag) {
    document.querySelectorAll('a[href="/clan/' + clanTag + '"]').forEach(function(a){
      var t = a.textContent.replace(/\\s+/g, ' ').trim();
      if (t && !clanName) clanName = t;
    });
  }
  var bodyText = document.body ? document.body.innerText : '';
  var empty = bodyText.indexOf('No decks to display') >= 0;
  var notFound = /error 404|page not found/i.test(document.title || '');
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    count: decks.length,
    empty: empty,
    notFound: notFound,
    decks: decks,
    clan: {name: clanName, tag: clanTag},
    text: bodyText.slice(0, 400)
  });
})()
"""


def _challenge(title, text=""):
    low = (str(title) + " " + str(text)).lower()
    return any(mark in low for mark in CHALLENGE_TITLES)


def _title_notfound(title):
    low = str(title or "").lower()
    return "error 404" in low or "page not found" in low


def _title_split(title):
    """'Mohamed Light #G9YV9GR8R | Decks | ...' -> (name, '#G9YV9GR8R')。"""
    head = str(title or "").split("|", 1)[0].strip()
    if "#" in head:
        name, tag = head.rsplit("#", 1)
        return name.strip(), "#" + tag.strip()
    return head, ""


def _norm(value):
    return " ".join(str(value or "").split()).casefold()


def _clan_match(want, got_name, got_tag):
    w = _norm(want)
    if not w:
        return True
    if w.startswith("#"):
        return _norm(got_tag) == w.lstrip("#")
    g = _norm(got_name)
    return bool(g) and (w == g or w in g or g in w)


def _pick_result(candidates, query, clan, index, max_candidates):
    """选择要打开的搜索结果下标。

    给了 clan 时后面会逐条核对，从头扫即可；未给 clan 时优先找名字精确
    匹配（忽略大小写）：RoyaleAPI 按奖杯数排序，短名 "Utto" 会排在
    "UTTORIKKU" 这类长名后面，盲取第一条容易选错人。
    """
    if clan:
        return 0
    wanted = _norm(query)
    for pos, cand in enumerate(candidates[:max_candidates]):
        if _norm(cand.get("name")) == wanted:
            return pos
    return min(index, len(candidates) - 1)


def _search_ready(d):
    return (bool(d.get("results")) or
            _challenge(d.get("title", ""), d.get("text", "")) or
            "no player" in str(d.get("text", "")).lower() or
            "没有" in str(d.get("text", "")))


def _decks_ready(d):
    return (bool(d.get("decks")) or bool(d.get("empty")) or
            bool(d.get("notFound")) or
            _challenge(d.get("title", ""), d.get("text", "")))


def _poll(backend, expr, ready, deadline, interval=0.35):
    """反复求值，直到 ready(data) 为真或超时；返回最后一次结果。"""
    last = None
    while time.time() < deadline:
        try:
            data = backend.eval_js(expr)
        except Exception as exc:
            last = {"error": repr(exc)}
            time.sleep(interval)
            continue
        last = data
        if isinstance(data, dict) and not data.get("error") and ready(data):
            return data
        time.sleep(interval)
    return last


def _expected_path(url):
    return url.split("royaleapi.com", 1)[-1].split("?", 1)[0]


def _ready_on(url, ready):
    expect = _expected_path(url)
    return lambda d: expect in str(d.get("url", "")) and ready(d)


def _pause(lo=0.5, hi=1.2):
    """停一下，模拟人在看页面。"""
    time.sleep(random.uniform(lo, hi))


def _open(backend, url, expr, ready, wait, solve, attempts):
    """跳转并轮询 DOM；遇到校验页则尝试过一次校验后重开页面。

    轮询结果必须来自目标页面（URL 包含目标路径），否则会读到跳转前旧页面
    仍满足 ready 的残留 DOM。
    """
    expect = _expected_path(url)
    ready_on_target = _ready_on(url, ready)

    backend.navigate_async(url)
    data = _poll(backend, expr, ready_on_target, time.time() + wait)
    if isinstance(data, dict) and not data.get("error") and \
            expect not in str(data.get("url", "")):
        data = {"error": "页面未加载到目标地址", "url": data.get("url", "")}
    solved = None
    if _challenge((data or {}).get("title", ""), (data or {}).get("text", "")) and solve:
        solved = cloudflare.solve(backend, attempts=attempts)
        if solved.get("solved") or not solved.get("challenge"):
            backend.navigate_async(url)
            data = _poll(backend, expr, ready_on_target, time.time() + wait)
            if isinstance(data, dict) and not data.get("error") and \
                    expect not in str(data.get("url", "")):
                data = {"error": "页面未加载到目标地址", "url": data.get("url", "")}
    return data, solved


SEARCH_FETCH_START = """
(function(){
  window.__crSearchHTML = 'PENDING';
  fetch(%s, {credentials: 'include'})
    .then(function(r){ return r.text(); })
    .then(function(t){ window.__crSearchHTML = String(t); })
    .catch(function(e){ window.__crSearchHTML = 'FETCH_ERROR:' + e; });
  return JSON.stringify({started: true});
})()
"""

# Token-direct search: the query is sent as a same-origin fetch (it carries the
# Cloudflare cookies), the HTML fragment is parsed offscreen with DOMParser and
# only the candidate list is returned. The visible page is never navigated.
SEARCH_FETCH_POLL = """
(function(){
  var html = window.__crSearchHTML;
  if (typeof html === 'undefined' || html === null || html === 'PENDING') {
    return JSON.stringify({state: 'pending'});
  }
  if (String(html).indexOf('FETCH_ERROR:') === 0) {
    return JSON.stringify({state: 'error', error: String(html)});
  }
  var doc = new DOMParser().parseFromString(String(html), 'text/html');
  var out = [];
  doc.querySelectorAll('.player_search_results__result_container').forEach(function(el){
    var a = el.querySelector('a.header');
    var t = el.querySelector('.player_tag');
    var href = a ? a.getAttribute('href') : '';
    var tag = t ? t.textContent.trim() : '';
    out.push({
      name: a ? a.textContent.replace(/\\s+/g, ' ').trim() : '',
      href: href,
      tag: tag
    });
  });
  var raw = String(html);
  return JSON.stringify({
    state: 'done',
    title: doc.title || '',
    count: out.length,
    results: out.slice(0, 20),
    text: (doc.body ? doc.body.innerText : '').slice(0, 400),
    markers: {
      raw_len: raw.length,
      container_hits: (raw.match(/result_container/g) || []).length,
      next_data: raw.indexOf('__NEXT_DATA__') >= 0,
      preloaded: raw.indexOf('__PRELOADED') >= 0,
      cloudflare: raw.indexOf('cf-chl') >= 0 || raw.indexOf('Just a moment') >= 0
    }
  });
})()
"""


def _fetch_ready(d):
    state = d.get("state")
    if state == "pending":
        return False
    if state in ("error", "done"):
        if state == "done" and not d.get("results"):
            # Terminal only when the fragment clearly says no players;
            # otherwise keep polling in case the fragment is still parsing.
            return ("no player" in str(d.get("text", "")).lower()
                    or "没有" in str(d.get("text", "")))
        return True
    return False


def _ensure_origin(backend, wait, solve, attempts):
    """Direct fetch needs a same-origin page; open the site once if needed."""
    data = _poll(backend, URL_JS,
                 lambda d: "royaleapi.com" in str(d.get("url", "")),
                 time.time() + 2.0)
    if isinstance(data, dict) and not data.get("error") and \
            "royaleapi.com" in str(data.get("url", "")):
        return True
    _open(backend, "https://royaleapi.com/", URL_JS,
          lambda d: bool(str(d.get("title", "")).strip()), wait, solve, attempts)
    data = _poll(backend, URL_JS,
                 lambda d: "royaleapi.com" in str(d.get("url", "")),
                 time.time() + 3.0)
    return isinstance(data, dict) and "royaleapi.com" in str(data.get("url", ""))


def _fetch_search(backend, query, wait, solve, attempts):
    """Search without touching the UI: fetch + DOMParser inside the page."""
    start_expr = SEARCH_FETCH_START % json.dumps(SEARCH_URL % quote(query))
    backend.eval_js(start_expr)
    data = _poll(backend, SEARCH_FETCH_POLL, _fetch_ready,
                 time.time() + wait, interval=0.3)
    if not isinstance(data, dict) or data.get("state") == "pending":
        return {"error": "搜索请求超时"}, None
    if data.get("state") == "error":
        return {"error": str(data.get("error"))}, None
    solved = None
    if _challenge(data.get("title", ""), data.get("text", "")) and solve:
        solved = cloudflare.solve(backend, attempts=attempts)
        if solved.get("solved") or not solved.get("challenge"):
            backend.eval_js(start_expr)
            data = _poll(backend, SEARCH_FETCH_POLL, _fetch_ready,
                         time.time() + wait, interval=0.3)
            if not isinstance(data, dict) or data.get("state") == "pending":
                return {"error": "搜索请求超时"}, solved
    return data, solved


def _click(backend, selector, index=0):
    return backend.mouse({"action": "click", "selector": selector,
                          "index": index, "native": True})


def _human_open(backend, result_index, tag, wait, solve, attempts):
    """真人走法：结果页停一下 → 点该条结果进玩家页 → 稍等 → 点 Decks 标签。

    任一步点不到就返回 None，调用方回退成直接打开卡组页。
    """
    _pause(0.8, 2.0)
    try:
        _click(backend, SEARCH_RESULT_SELECTOR, result_index)
    except Exception:
        return None
    profile_url = "https://royaleapi.com/player/%s" % tag
    ready = _ready_on(profile_url, lambda d: bool(str(d.get("title", "")).strip()))
    data = _poll(backend, URL_JS, ready, time.time() + wait)
    if not isinstance(data, dict) or data.get("error"):
        return None
    if _challenge(data.get("title", "")):
        if not solve:
            return None
        cloudflare.solve(backend, attempts=attempts)
        data = _poll(backend, URL_JS, ready, time.time() + wait)
        if not isinstance(data, dict) or data.get("error"):
            return None
    if _title_notfound(data.get("title")):
        # 封号/无效玩家页没有 Decks 标签，页面上的导航链接会把人带去站点
        # 自己的卡组列表（best deck），所以直接判定 404，不再点。
        return ({"url": data.get("url", ""),
                 "title": data.get("title", ""),
                 "decks": [], "count": 0, "empty": False,
                 "notFound": True,
                 "clan": {"name": "", "tag": ""}}, None)
    _pause(0.7, 1.8)
    decks_path = "/player/%s/decks" % tag
    try:
        _click(backend, 'a[href="%s"]' % decks_path)
    except Exception:
        return None
    decks_url = "https://royaleapi.com" + decks_path
    data = _poll(backend, DECKS_JS, _ready_on(decks_url, _decks_ready),
                 time.time() + wait)
    solved = None
    if _challenge((data or {}).get("title", ""), (data or {}).get("text", "")) and solve:
        solved = cloudflare.solve(backend, attempts=attempts)
        if solved.get("solved") or not solved.get("challenge"):
            data = _poll(backend, DECKS_JS, _ready_on(decks_url, _decks_ready),
                         time.time() + wait)
    if not isinstance(data, dict) or data.get("error") or \
            decks_path not in str(data.get("url", "")):
        return None
    return data, solved


def _back_to_search(backend, search_url, wait):
    """候选不对时退回结果页（浏览器后退），失败再直接打开结果页。"""
    _pause(0.4, 1.1)
    try:
        backend.eval_js(
            "(function(){history.go(-2);return JSON.stringify({ok:true});})()")
    except Exception:
        pass
    ready = _ready_on(search_url, _search_ready)
    data = _poll(backend, SEARCH_JS, ready, time.time() + min(wait, 8.0))
    if isinstance(data, dict) and not data.get("error") and \
            "/player/search/results" in str(data.get("url", "")):
        _pause(0.5, 1.3)
        return True
    backend.navigate_async(search_url)
    _poll(backend, SEARCH_JS, ready, time.time() + wait)
    return False


def deck_by_name(backend, name, clan="", index=0, max_candidates=10,
                 wait=12.0, solve=True, attempts=2, human=True,
                 direct=False):
    """按玩家名（或 #tag）查最近卡组。

    clan 可选：部落名或 #部落标签，用来在同名玩家之间筛人（按卡组页头部核对）；
    未给 clan 时优先取名字精确匹配的结果，没有才退回第 index 个。human=True 时按真人方式点结果、
    点 Decks 标签逐条查看，human=False 直接按 URL 打开。direct=True 时搜索改为页面内
    fetch 静默完成（不导航、不点击），UI 只在最后打开目标玩家的卡组页。
    """
    query = str(name or "").strip()
    if not query:
        raise ValueError("缺少 name 参数")
    wait = max(2.0, min(float(wait), 60.0))
    index = max(0, int(index))
    max_candidates = max(1, min(int(max_candidates), 20))
    effective_human = human and not direct

    tag = ""
    if query.startswith("#"):
        tag = query[1:].strip().upper()
    elif query.startswith("Y") and len(query) == 9 and query.upper() == query:
        # 直接给了不带 # 的 tag
        tag = query.upper()

    candidates = []
    searched = False
    solved = None
    start = 0
    if not tag:
        searched = True
        url = SEARCH_URL % quote(query)
        if direct:
            if not _ensure_origin(backend, wait, solve, attempts):
                return {"ok": False,
                        "error": "浏览器不在 royaleapi.com 页面上，无法静默请求搜索接口",
                        "stage": "origin"}
            data, solved = _fetch_search(backend, query, wait, solve, attempts)
        else:
            data, solved = _open(backend, url, SEARCH_JS, _search_ready,
                                 wait, solve, attempts)
        if isinstance(data, dict) and data.get("error"):
            return {"ok": False, "error": data["error"], "stage": "search"}
        candidates = (data or {}).get("results") or []
        if not candidates:
            return {"ok": False, "error": "没有搜到玩家",
                    "query": query, "search_url": url,
                    "title": (data or {}).get("title", ""),
                    "text": (data or {}).get("text", ""),
                    "markers": (data or {}).get("markers")}
        start = _pick_result(candidates, query, clan, index, max_candidates)
        # 未给 clan 时只打开选中的这一条：其余同名玩家是另一个人，返回
        # 他们的卡组就是答非所问。clan 才有逐条核对的理由。
        scan = candidates[start:start + (max_candidates if clan else 1)]
    else:
        chosen = {"name": query.lstrip("#"), "tag": "#" + tag,
                  "href": "/player/" + tag}
        candidates = [chosen]
        scan = [chosen]

    checked = []
    data = None
    chosen = None
    cand_tag = ""
    search_url = url if searched else ""
    for pos, cand in enumerate(scan):
        cand_tag = cand.get("tag", "").lstrip("#") or \
            cand.get("href", "").split("/player/")[-1]
        if not cand_tag:
            continue
        player_url = PLAYER_URL % cand_tag
        cand_wait = wait if pos == 0 else min(wait, 6.0)
        cand_solved = None
        mode = "direct"
        if effective_human and searched:
            got = _human_open(backend, start + pos, cand_tag,
                              cand_wait, solve, attempts)
            if got is not None:
                data, cand_solved = got
                mode = "click"
            else:
                data, cand_solved = _open(backend, player_url, DECKS_JS,
                                          _decks_ready, cand_wait,
                                          solve, attempts)
        else:
            data, cand_solved = _open(backend, player_url, DECKS_JS,
                                      _decks_ready, cand_wait,
                                      solve, attempts)
        if cand_solved is not None:
            solved = cand_solved
        if isinstance(data, dict) and data.get("error"):
            if effective_human and searched and pos < len(scan) - 1:
                _back_to_search(backend, search_url, wait)
            continue
        info = {
            "name": cand.get("name", ""),
            "tag": cand.get("tag") or "#" + cand_tag,
            "player_url": "https://royaleapi.com/player/%s" % cand_tag,
            "clan": data.get("clan") or {},
            "decks": len(data.get("decks") or []),
            "empty": bool(data.get("empty")),
            "notFound": bool(data.get("notFound")),
            "mode": mode,
        }
        checked.append(info)
        more = pos < len(scan) - 1
        if data.get("notFound"):
            if effective_human and searched and more:
                _back_to_search(backend, search_url, wait)
            continue
        if not _clan_match(clan, (data.get("clan") or {}).get("name", ""),
                           (data.get("clan") or {}).get("tag", "")):
            if effective_human and searched and more:
                _back_to_search(backend, search_url, wait)
            continue
        chosen = cand
        break

    if chosen is None:
        if clan:
            error = "没找到部落匹配的玩家"
        elif searched:
            error = "搜索结果第 %d 条打不开或没有卡组" % (start + 1)
        else:
            error = "该玩家页打不开或没有卡组"
        return {"ok": False,
                "error": error,
                "query": query, "clan": clan,
                "checked": checked,
                "title": (data or {}).get("title", "")}

    decks = data.get("decks") or []
    if not decks:
        return {"ok": False, "error": "该玩家最近没有可显示的卡组",
                "query": query, "tag": "#" + cand_tag, "clan": clan,
                "player_url": "https://royaleapi.com/player/%s/decks" % cand_tag,
                "mode": mode, "title": data.get("title", ""),
                "checked": checked}

    page_name, page_tag = _title_split(data.get("title"))
    out = {
        "ok": True,
        "query": query,
        "clan": clan,
        "matched": {
            "name": page_name or chosen.get("name", ""),
            "tag": page_tag or chosen.get("tag") or "#" + cand_tag,
            "clan": data.get("clan") or {},
            "player_url": "https://royaleapi.com/player/%s" % cand_tag,
            "mode": mode,
        },
        "candidates": candidates,
        "deck": decks[0],
        "decks": decks,
    }
    if len(checked) > 1:
        out["checked"] = checked
    if solved is not None:
        out["cloudflare"] = solved
    return out
