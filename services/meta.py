"""
快速元数据（豆瓣 suggest）

为什么不用 showpaw 拿元数据：
  实测 showpaw /api/search 单次 6~13 秒，且对 4 个片名全部返回 0 条资源，
  只是顺带给 TMDB 元数据 —— 为了一份海报让整次搜索等十几秒不划算。

豆瓣 subject_suggest 实测 0.11~0.34 秒，够用（标题/年份/海报/豆瓣ID）。

返回结构与 showpaw 的 tmdb 字段对齐，方便 search.py 直接替换。
"""
import logging
import time

import requests

log = logging.getLogger("meta")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SUGGEST = "https://movie.douban.com/j/subject_suggest"

_SESS = requests.Session()
_SESS.trust_env = False
_SESS.proxies = {"http": None, "https": None}
_SESS.headers.update({"User-Agent": UA, "Referer": "https://movie.douban.com/",
                      "Accept": "application/json, text/javascript, */*; q=0.01"})


def _fresh_session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update({"User-Agent": UA, "Referer": "https://movie.douban.com/",
                      "Accept": "application/json, text/javascript, */*; q=0.01"})
    return s

# 内存缓存（够用，磁盘缓存由 store 负责）
_CACHE = {}
_TTL = 60 * 60 * 6


def _norm_year(y):
    try:
        return int(str(y)[:4])
    except Exception:
        return 0


def quick(query, want_tv=None, timeout=1.5, year_hint=None, douban_id=None):
    """
    按片名取元数据。返回 showpaw 形状的 dict，取不到返回 None。

    只依赖豆瓣，0.1~0.3 秒，失败也不阻塞搜索。
    """
    q = (query or "").strip()
    if not q:
        return None

    ck = "meta:v3:%s:%s:%s:%s" % (q, "tv" if want_tv is True else ("movie" if want_tv is False else "any"),
                          year_hint or "", douban_id or "")

    # ---- 直接按 id 出元数据（最可靠，不经过 suggest）----
    if douban_id:
        try:
            from . import meta2 as _M2
            _d = _M2.get(q, year=(int(year_hint) if str(year_hint).isdigit() else None),
                         is_tv=want_tv, douban_id=douban_id)
            if _d.get("ok"):
                out = {
                    "tmdbId": None,
                    "title": q,
                    "originalTitle": "",
                    "firstAirDate": str(_d.get("year") or ""),
                    "year": int(str(_d.get("year") or 0)[:4] or 0),
                    "posterUrl": _d.get("poster") or "",
                    "backdropUrl": _d.get("backdrop") or "",
                    "overview": _d.get("overview") or "",
                    "voteAverage": _d.get("rating") or 0,
                    "voteCount": 0,
                    "genres": _d.get("genres") or [],
                    "runtime": 0,
                    "cast": [],
                    "director": "",
                    "type": "tv" if want_tv else "movie",
                    "douban_id": str(douban_id),
                }
                _CACHE[ck] = (time.time(), out)
                return out
        except Exception:
            pass
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    arr = None
    # 1) 豆瓣 suggest：Scrapling（TLS 伪装）优先，失败再回退 requests，最多两轮
    for attempt in range(2):
        try:
            from . import stealth as _ST
            got = _ST.douban_json(SUGGEST, {"q": q},
                                  timeout=max(2.5, timeout), headers=None)
            if isinstance(got, list) and got:
                arr = got
                break
        except Exception as e:
            log.info("豆瓣 suggest 失败(%d): %s", attempt + 1, str(e)[:100])
        if attempt == 0:
            time.sleep(0.15)

    # 2) 豆瓣不可用时退回持久层（也许是之前 showpaw 存过的）
    if not arr:
        try:
            from . import store
            old = store.get_search("meta:" + ck)
            if old:
                return old
        except Exception:
            pass
        return None

    if not isinstance(arr, list) or not arr:
        return None

    # 挑一个最像的：优先同名、类型匹配、年份最近的
    def score(it):
        s = 0
        if (it.get("title") or "").strip() == q:
            s += 3
        t = (it.get("type") or "")
        if want_tv is True and "tv" in t:
            s += 2
        if want_tv is False and "movie" in t:
            s += 2
        y = _norm_year(it.get("year"))
        if y:
            s += 1
        # 有年份线索时，年份接近的优先（同名作品太多）
        if year_hint and y:
            try:
                d = abs(y - int(year_hint))
                s += 6 if d == 0 else (3 if d <= 1 else -4)
            except Exception:
                pass
        return s

    best = max(arr, key=score)
    title = (best.get("title") or "").strip()
    if not title:
        return None

    img = best.get("img") or ""
    # ---- meta2 补简介（豆瓣移动接口；suggest 拿不到简介）----
    _ov = {}
    try:
        from . import meta2 as _M2
        _d = _M2.get(best.get("title") or q, year=_norm_year(best.get("year")),
                     is_tv=(str(best.get("type") or "").lower() in ("tv", "tvshow")),
                     orig_title=(best.get("sub_title") or "").strip(),
                     douban_id=douban_id or str(best.get("id") or ""))
        if _d.get("ok"):
            _ov = _d
    except Exception:
        _ov = {}

    out = {
        "tmdbId": None,
        "title": title,
        "originalTitle": (best.get("sub_title") or "").strip(),
        "firstAirDate": str(best.get("year") or ""),
        "year": _norm_year(best.get("year")),
        "posterUrl": (_ov.get("poster") or img),
        "backdropUrl": "",
        "overview": (_ov.get("overview") or ""),
        "voteAverage": (_ov.get("rating") or 0),
        "voteCount": 0,
        "genres": (_ov.get("genres") or []),
        "runtime": 0,
        "cast": [],
        "director": "",
        "type": "tv" if "tv" in (best.get("type") or "") else "movie",
        "douban_id": str(best.get("id") or ""),
        "_source": "douban",
    }

    _CACHE[ck] = (time.time(), out)
    try:
        from . import store
        store.put_search("meta:" + ck, q, out)
    except Exception:
        pass
    return out
