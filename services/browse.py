"""
电影 / 剧集 分类页

数据源选择（实测结论）：
  - 豆瓣 `/j/search_subjects` 的 type 参数是真的生效的（movie / tv 结果不同）
    而且 tag 覆盖较全：热门、豆瓣高分、美剧、韩剧、日剧、国产剧、英剧、
    日本动画、综艺、动画 …大多能出 20 条
  - `/j/new_search_subjects` 虽然支持多标签组合，但 type 参数不生效
    （type=电视剧 返回的还是电影），所以只用于「筛选」页

因此：分类页走 search_subjects，筛选页走 new_search_subjects，各用所长。
"""
import concurrent.futures as cf
import threading
import time
from urllib.parse import quote

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
API = "https://movie.douban.com/j/search_subjects"

_ROWS_MOVIE = [
    ("热门电影", "热门"),
    ("豆瓣高分", "豆瓣高分"),
    ("华语电影", "华语"),
    ("欧美电影", "欧美"),
    ("动画电影", "动画"),
    ("经典老片", "经典"),
]

_ROWS_TV = [
    ("热门剧集", "热门"),
    ("国产剧",   "国产剧"),
    ("美剧",     "美剧"),
    ("韩剧",     "韩剧"),
    ("日剧",     "日剧"),
    ("英剧",     "英剧"),
    ("日本动画", "日本动画"),
    ("综艺",     "综艺"),
]

_CACHE = {}
_TTL = 60 * 40
_STALE = {}                 # 最近一次成功结果，过期也留着先给用户看
_BUILDING = set()
_BUILD_LOCK = __import__("threading").Lock()

_LOCK = threading.Lock()
_LAST = [0.0]
_GAP = 0.32


def _gate():
    with _LOCK:
        wait = _LAST[0] + _GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()


def _cache_get(k):
    v = _CACHE.get(k)
    if v and time.time() - v[0] < _TTL:
        return v[1]
    return None


def _cache_set(k, v):
    _CACHE[k] = (time.time(), v)


def _norm(s):
    cover = (s.get("cover") or "").strip()
    if cover:
        cover = "/api/img?u=" + quote(cover, safe="")
    rate = s.get("rate")
    try:
        rate = float(rate) if rate not in (None, "") else None
    except Exception:
        rate = None
    return {
        "title": (s.get("title") or "").strip(),
        "poster": cover,
        "cover": cover,
        "douban_id": str(s.get("id") or ""),
        "rating": rate,
        "year": "",
        "has_meta": False,
        "poster_from": "douban" if cover else "none",
    }


def _one(typ, tag, limit=20):
    for attempt in range(3):
        _gate()
        try:
            from . import stealth as _ST
            d = _ST.douban_json(API, {
                "type": typ, "tag": tag, "sort": "recommend",
                "page_limit": limit, "page_start": 0,
            }, timeout=18) or {}
            subs = d.get("subjects") or []
            if subs:
                return [_norm(x) for x in subs if x.get("title")]
        except Exception:
            pass
        if attempt < 2:
            time.sleep(0.7 * (attempt + 1))
    return []


def _kick_rebuild(media, limit):
    """后台重建（同一媒体只跑一个）"""
    key = "%s:%d" % (media, limit)
    with _BUILD_LOCK:
        if key in _BUILDING:
            return
        _BUILDING.add(key)

    def work():
        try:
            browse(media=media, limit=limit, force=True)
        except Exception:
            pass
        finally:
            with _BUILD_LOCK:
                _BUILDING.discard(key)

    __import__("threading").Thread(target=work, daemon=True).start()


def browse(media="movie", limit=20, force=False):
    media = "tv" if media == "tv" else "movie"
    ck = "browse2:%s:%d" % (media, limit)
    if not force:
        hit = _cache_get(ck)
        if hit is not None:
            return hit
        # 过期也先给，后台更新
        stale = _STALE.get(ck)
        if stale is not None:
            _kick_rebuild(media, limit)
            return stale

    defs = _ROWS_TV if media == "tv" else _ROWS_MOVIE
    typ = "tv" if media == "tv" else "movie"

    got = {}
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_one, typ, tag, limit): label for label, tag in defs}
        for f in cf.as_completed(futs, timeout=120):
            label = futs[f]
            try:
                got[label] = f.result() or []
            except Exception:
                got[label] = []

    rows = []
    for label, tag in defs:
        items = got.get(label) or []
        if len(items) >= 4:
            rows.append({"label": label, "items": items, "tag": tag})

    out = {"media": media, "rows": rows}
    if rows:
        _cache_set(ck, out)
        _STALE[ck] = out
    return out
