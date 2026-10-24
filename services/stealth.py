"""
反爬兜底抓取层（基于 Scrapling）

背景
----
部分站点对普通 requests 返回 403 + 「Checking your browser」JS 挑战页，
例如 libvio.io / libku.com。实测对比：

    libvio 首页   requests → 403，44 字节纯文本
                  Scrapling Fetcher → 403，19329 字节（拿到了挑战页）
                  持久会话第 2 次 → 200，42952 字节（挑战通过）

结论：普通 HTTP 客户端过不了 JS 挑战，必须真实浏览器 + 保持会话。

本模块职责
----------
1. fetch_text()    轻量抓取（TLS 指纹伪装），够用就别开浏览器
2. fetch_stealth() 真实浏览器抓取，自动处理 JS 挑战，会话复用
3. 单例浏览器会话   避免每次请求都冷启动（冷启约 5-10 秒）

依赖
----
pip install "scrapling[fetchers]"
python -m patchright install chromium      # 浏览器内核

未安装时所有函数安全返回 None，不影响主流程。
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("stealth")

# 浏览器内核放在 F 盘（C 盘空间紧张），这里兜底指定路径
_BROWSER_DIR = r"F:\ms-playwright"
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.isdir(_BROWSER_DIR):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _BROWSER_DIR

_AVAILABLE = None          # None=未检测 True/False
_CHECK_LOCK = threading.Lock()

# 浏览器会话（全局复用）
_SESSION = None
_SESSION_LOCK = threading.Lock()
_SESSION_LAST = [0.0]
_IDLE_CLOSE = 180          # 空闲多久关掉浏览器（秒）


def available():
    """Scrapling 是否可用（含浏览器内核）"""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    with _CHECK_LOCK:
        if _AVAILABLE is not None:
            return _AVAILABLE
        try:
            from scrapling.fetchers import Fetcher  # noqa: F401
            _AVAILABLE = True
        except Exception as e:
            log.warning("Scrapling 不可用: %s", str(e)[:120])
            _AVAILABLE = False
    return _AVAILABLE


# ------------------------------------------------------------------ 轻量抓取

def fetch_text(url, timeout=25, headers=None, params=None):
    """
    TLS 指纹伪装抓取，返回响应文本；失败返回 None。

    适合无 JS 挑战的接口（豆瓣 JSON、普通 HTML）。
    比 requests 更不容易被判为爬虫。
    """
    if not available():
        return None
    try:
        from scrapling.fetchers import Fetcher
        kw = {"timeout": timeout}
        if headers:
            kw["headers"] = headers
        page = Fetcher.get(url, **kw)
        status = getattr(page, "status", 0)
        body = getattr(page, "body", None)
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body).decode("utf-8", "ignore")
        else:
            text = getattr(page, "html_content", "") or ""
        if status and status != 200:
            log.info("fetch_text %s -> HTTP %s", url[:70], status)
        return text or None
    except Exception as e:
        log.warning("fetch_text 失败 %s: %s", url[:70], str(e)[:120])
        return None


# ------------------------------------------------------------------ 浏览器抓取

def _get_session():
    """拿（或创建）复用的浏览器会话"""
    global _SESSION
    from scrapling.fetchers import StealthySession
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION_LAST[0] = time.time()
            return _SESSION
        _SESSION = StealthySession(
            headless=True,
            timeout=90000,
            network_idle=True,
            disable_resources=False,     # 挑战页要靠 JS，别禁资源
        )
        _SESSION.__enter__()
        _SESSION_LAST[0] = time.time()
        log.info("浏览器会话已启动")
        return _SESSION


def _drop_session():
    global _SESSION
    with _SESSION_LOCK:
        s, _SESSION = _SESSION, None
    if s is not None:
        try:
            s.__exit__(None, None, None)
        except Exception:
            pass


def close():
    """主动关闭浏览器会话"""
    _drop_session()


def fetch_stealth(url, timeout=90, attempts=2, wait=3.0):
    """
    真实浏览器抓取，处理 JS 挑战。

    挑战页通常第一次返回 403，JS 执行后种下 cookie，再请求就 200，
    所以这里会重试同一地址。

    返回 (text, status)；失败返回 (None, 0)。
    """
    if not available():
        return None, 0
    sess = _get_session()
    last_status = 0
    for i in range(max(1, attempts)):
        try:
            page = sess.fetch(url)
            status = getattr(page, "status", 0) or 0
            body = getattr(page, "body", None)
            if isinstance(body, (bytes, bytearray)):
                text = bytes(body).decode("utf-8", "ignore")
            else:
                text = getattr(page, "html_content", "") or ""
            last_status = status
            if status == 200 and text:
                _SESSION_LAST[0] = time.time()
                # 可能是挑战页伪装成 200，做个粗筛
                if "Checking your browser" not in text:
                    return text, status
        except Exception as e:
            log.warning("fetch_stealth 异常 %s: %s", url[:70], str(e)[:120])
            _drop_session()
            sess = _get_session()
        if i < attempts - 1:
            time.sleep(wait)
    return None, last_status


# ------------------------------------------------------------------ 工具

def fetch_json(url, timeout=25):
    """抓 JSON 接口（自动脱掉浏览器包装）"""
    txt = fetch_text(url, timeout=timeout)
    if txt:
        val = _parse_json(txt)
        if val is not None:
            return val
    txt, _ = fetch_stealth(url, attempts=2)
    if txt:
        return _parse_json(txt)
    return None


def _parse_json(txt):
    """从可能被 HTML 包装的文本里抠 JSON"""
    if not txt:
        return None
    s = txt.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # 浏览器抓回来常是 <html><body><p>{...}</p></body></html>
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return None


def cleanup_idle():
    """空闲太久就关掉浏览器，省内存"""
    if _SESSION is not None and time.time() - _SESSION_LAST[0] > _IDLE_CLOSE:
        log.info("浏览器会话空闲，关闭")
        close()


# ------------------------------------------------------------------ 豆瓣专用

_DB_HEADERS = {
    "Referer": "https://movie.douban.com/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}


def douban_json(url, params=None, timeout=15, headers=None):
    """
    豆瓣 JSON 接口：TLS 伪装优先，失败回退 requests。

    返回解析后的 dict/list；彻底失败返回 None。
    豆瓣限流时通常返回 200 + 空数据，这里把"空"也当成失败，交给上层重试。
    """
    from urllib.parse import urlencode

    full = url
    if params:
        sep = "&" if ("?" in url) else "?"
        full = url + sep + urlencode(params, doseq=True)

    h = dict(_DB_HEADERS)
    if headers:
        h.update(headers)

    # ① Scrapling（TLS 指纹伪装）
    try:
        txt = fetch_text(full, timeout=timeout, headers=h)
        if txt:
            d = json.loads(txt)
            if d:
                return d
    except Exception as e:
        log.info("douban_json scrapling 失败: %s", str(e)[:100])

    # ② 回退 requests
    try:
        import requests as _rq
        sess = _rq.Session()
        sess.trust_env = False
        sess.proxies = {"http": None, "https": None}
        r = sess.get(url, params=params, headers=h, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            if d:
                return d
    except Exception as e:
        log.info("douban_json requests 失败: %s", str(e)[:100])

    return None
