"""
豆瓣筛选：类型 / 地区 / 年份 组合检索

接口：https://movie.douban.com/j/new_search_subjects
  tags=华语,动作     逗号分隔，可任意组合
  type=电影|电视剧
返回 data[]: {id,title,rate,cover,url,directors,casts}

这个接口是首页「筛选」板块和华语电影行的共同数据源。
"""
import concurrent.futures as cf
import random
import re
import threading
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# UA 轮换：同一 IP 换指纹能降低被判定为爬虫的概率
UA_POOL = [
    UA,
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"),
]


def _ua():
    return random.choice(UA_POOL)
API = "https://movie.douban.com/j/new_search_subjects"

_CACHE = {}
_TTL = 60 * 30
_STALE = {}                 # 过期缓存兜底（保留更久）
_STALE_TTL = 60 * 60 * 12

# ---- 豆瓣请求节流：避免并发打爆导致返回空数组 ----
_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_MIN_GAP = 0.35

# new_search_subjects 连续失败后的冷却（秒）：期间直接走替代通道
_DEAD_UNTIL = [0.0]
_DEAD_FAILS = [0]            # 两次请求最小间隔（秒）


def _gate():
    with _LOCK:
        now = time.time()
        wait = _LAST_CALL[0] + _MIN_GAP - now
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        _LAST_CALL[0] = time.time()


# 筛选项（前端直接用这些值拼 tags）
GENRES = ["动作", "喜剧", "爱情", "科幻", "悬疑", "惊悚", "恐怖",
          "剧情", "动画", "犯罪", "冒险", "奇幻", "战争", "传记", "历史"]
REGIONS = ["华语", "美国", "日本", "韩国", "英国", "法国", "印度", "泰国"]
YEARS = ["2026", "2025", "2024", "2023", "2022", "2021", "2020",
         "2019", "2018", "2015", "2010", "2000"]


def _cache_get(k):
    v = _CACHE.get(k)
    if v and time.time() - v[0] < _TTL:
        return v[1]
    return None


def _cache_set(k, v):
    _CACHE[k] = (time.time(), v)
    _STALE[k] = (time.time(), v)


def _stale_get(k):
    v = _STALE.get(k)
    if v and time.time() - v[0] < _STALE_TTL:
        return v[1]
    return None


def _norm(s):
    cover = (s.get("cover") or "").strip()
    if cover:
        from urllib.parse import quote
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
        "year": "",                       # 该接口不给年份，靠标题/后续补
        "has_meta": False,
        "poster_from": "douban" if cover else "none",
    }



# ---------------------------------------------------------------- 替代通道

SUBJ_API = "https://movie.douban.com/j/search_subjects"


def _via_search_subjects(tag, media, start=0, limit=40, timeout=15):
    """
    单 tag 通道（new_search_subjects 被限流时用）。

    返回 [{title, rate, cover, url, id}, ...]，字段与 new_search_subjects 对齐。
    """
    typ = "tv" if media == "tv" else "movie"
    try:
        _gate()
        from . import stealth as _ST
        d = _ST.douban_json(SUBJ_API, {
            "type": typ, "tag": tag,
            "page_limit": max(20, min(limit, 100)),
            "page_start": start,
        }, timeout=timeout) or {}
        arr = d.get("subjects") if isinstance(d, dict) else d
        # 剧集对题材 tag 不生效（返回空），自动退回热门
        if (not arr) and typ == "tv" and tag != "热门":
            try:
                _gate()
                d = _ST.douban_json(SUBJ_API, {
                    "type": typ, "tag": "热门",
                    "page_limit": max(20, min(limit, 100)),
                    "page_start": start,
                }, timeout=timeout) or {}
                arr = d.get("subjects") if isinstance(d, dict) else d
            except Exception:
                pass
        if not isinstance(arr, list):
            return []
        out = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            out.append({
                "title": (it.get("title") or "").strip(),
                "rate": it.get("rate"),
                "cover": it.get("cover") or "",
                "url": it.get("url") or "",
                "id": it.get("id") or "",
            })
        return out
    except Exception:
        return []


def _pick_tag(genres, regions, year):
    """降级时用哪个 tag：优先题材，其次地区，再次年份，最后热门"""
    for x in (genres or []):
        if x:
            return x
    for x in (regions or []):
        if x:
            return x
    if year:
        return str(year)
    return "热门"


def search(genres=None, regions=None, year=None, media="movie",
           start=0, limit=40, sort="U", timeout=18):
    """
    genres/regions: 列表或 None
    year: 字符串或 None
    media: "movie" | "tv"
    返回 {ok, tags, items}
    """
    tags = []
    for x in (regions or []):
        if x:
            tags.append(x)
    for x in (genres or []):
        if x:
            tags.append(x)
    if year:
        tags.append(str(year))
    tag_str = ",".join(tags)

    type_str = "电视剧" if media == "tv" else "电影"
    ck = "filter:%s:%s:%d:%s" % (tag_str, type_str, start, sort)
    hit = _cache_get(ck)
    if hit is not None:
        return {"ok": True, "tags": tag_str, "items": hit, "cached": True}

    # 磁盘层（重启后仍有效，豆瓣限流时也能顶上）
    try:
        from . import store
        disk = store.get_search("flt:" + ck, 60 * 60 * 12)
        if disk:
            items = (disk or {}).get("items") or []
            if items:
                _cache_set(ck, items)
                return {"ok": True, "tags": tag_str, "items": items,
                        "cached": True, "from_disk": True}
    except Exception:
        pass

    # 主接口冷却期内：优先走替代通道（但冷却结束后会自动回归主接口）
    if time.time() < _DEAD_UNTIL[0]:
        tag = _pick_tag(genres, regions, year)
        alt = _via_search_subjects(tag, media, start, limit)
        if alt:
            items = []
            for s2 in alt[:limit]:
                try:
                    it = _norm(s2)
                    if it["title"]:
                        items.append(it)
                except Exception:
                    continue
            if items:
                return {"ok": True, "tags": tag, "items": items,
                        "degraded": True, "cooled": True,
                        "note": "组合筛选暂不可用，已按「%s」筛选" % tag}

    h = {"User-Agent": _ua(), "Referer": "https://movie.douban.com/",
         "Accept": "application/json, text/plain, */*"}
    params = {
        "sort": sort, "tags": tag_str, "start": start,
        "genres": "", "countries": "", "year": "",
        "type": type_str,
    }
    subs = None
    last_err = ""
    for attempt in range(3):
        _gate()
        try:
            # 走 Scrapling（TLS 指纹伪装），失败自动回退 requests
            from . import stealth as _ST
            d = _ST.douban_json(API, params, timeout=timeout) or {}
            got = d.get("data") if isinstance(d, dict) else None
            if isinstance(got, list) and got:
                subs = got
                _DEAD_FAILS[0] = 0
                break
            last_err = "豆瓣返回空结果"        # 通常是临时限流
        except Exception as e:
            last_err = str(e)[:120]
        # 退避重试
        if attempt < 2:
            time.sleep(0.8 * (attempt + 1))

    if not subs:
        # 兜底 1：换真实浏览器（能过 JS 挑战）
        try:
            from . import stealth
            if stealth.available():
                from urllib.parse import urlencode
                url = API + "?" + urlencode(params)
                d = stealth.fetch_json(url, timeout=40)
                got = (d or {}).get("data") if isinstance(d, dict) else None
                if isinstance(got, list) and got:
                    subs = got
        except Exception:
            pass

    if not subs:
        _DEAD_FAILS[0] += 1
        if _DEAD_FAILS[0] >= 1:
            _DEAD_UNTIL[0] = time.time() + 60      # 冷却 1 分钟后再试主接口
        # 兜底 2：降级到 search_subjects（单 tag，实测未受限流影响）
        tag = _pick_tag(genres, regions, year)
        alt = _via_search_subjects(tag, media, start, limit)
        if alt:
            items = []
            for s2 in alt[:limit]:
                try:
                    it = _norm(s2)
                    if it["title"]:
                        items.append(it)
                except Exception:
                    continue
            if items:
                _cache_set(ck, items)
                try:
                    from . import store
                    store.put_search("flt:" + ck, tag_str, {"items": items})
                except Exception:
                    pass
                return {"ok": True, "tags": tag, "items": items,
                        "degraded": True, "note": "组合筛选暂不可用，已按「%s」筛选" % tag}

    if not subs:
        # 兜底 3：磁盘旧数据（豆瓣限流/重启后仍然可用）
        try:
            from . import store
            disk = store.get_search("flt:" + ck, 60 * 60 * 24 * 7)
            items = (disk or {}).get("items") or []
            if items:
                return {"ok": True, "tags": tag_str, "items": items,
                        "cached": True, "from_disk": True, "stale": True}
        except Exception:
            pass
        # 兜底 3：内存旧数据，宁可给旧内容也不给空页
        old = _stale_get(ck)
        if old:
            return {"ok": True, "tags": tag_str, "items": old,
                    "cached": True, "stale": True}
        return {"ok": False, "error": last_err or "获取失败",
                "tags": tag_str, "items": []}
    items = []
    for s in subs[:limit]:
        try:
            it = _norm(s)
            if it["title"]:
                items.append(it)
        except Exception:
            continue

    _cache_set(ck, items)
    try:
        from . import store
        store.put_search("flt:" + ck, tag_str, {"items": items})
    except Exception:
        pass
    return {"ok": True, "tags": tag_str, "items": items}


def chinese_movies(limit=20):
    """华语电影行（首页用）"""
    r = search(regions=["华语"], media="movie", limit=limit)
    return r.get("items") or []


def options():
    return {"genres": GENRES, "regions": REGIONS, "years": YEARS}
