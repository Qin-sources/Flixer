"""
推荐数据

similar(): 相似影片 —— 用当前片的类型/地区去豆瓣找同类
daily():   每日推荐 —— 以日期为种子，当天固定，次日自动换
"""
import hashlib
import random
import threading
import time

from . import browse as BW
from .filter import search as db_search

_CACHE = {}
_TTL = 60 * 60 * 6

_LOCK = threading.Lock()
_LAST = [0.0]
_GAP = 0.32


def _gate():
    with _LOCK:
        wait = _LAST[0] + _GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()


def _cached(k, ttl=_TTL):
    v = _CACHE.get(k)
    if v and time.time() - v[0] < ttl:
        return v[1]
    return None


def _save(k, v):
    _CACHE[k] = (time.time(), v)


def similar(title, genres=None, year=None, media="movie", limit=14):
    """
    相似影片：优先用类型，其次用地区，都没有就退回热门
    """
    genres = [g for g in (genres or []) if g]
    key = "sim:%s:%s:%s" % (title, ",".join(genres[:2]), media)
    hit = _cached(key)
    if hit is not None:
        return hit

    items = []
    tried = set()

    # 依次尝试：主类型 -> 次类型 -> 热门
    candidates = []
    for g in genres[:2]:
        candidates.append(("genres", [g]))
    candidates.append(("hot", None))

    for kind, gs in candidates:
        if kind == "hot":
            rows = (BW.browse(media=media) or {}).get("rows") or []
            for r in rows[:2]:
                for it in r.get("items") or []:
                    if it.get("title") and it["title"] != title:
                        items.append(it)
        else:
            _gate()
            r = db_search(genres=gs, media=media, limit=20, sort="U")
            for it in (r.get("items") or []):
                if it.get("title"):
                    items.append(it)
        # 去重
        seen, uniq = set(), []
        for it in items:
            k = it.get("douban_id") or it.get("title")
            if not k or k in seen or it.get("title") == title:
                continue
            seen.add(k)
            uniq.append(it)
        items = uniq
        if len(items) >= limit:
            break

    out = items[:limit]
    if out:
        _save(key, out)
    return out


_LAST_DAILY = []


def daily(limit=14, media="mixed"):
    """
    每日推荐：用 YYYY-MM-DD 当种子，同一天结果固定，第二天自动变
    """
    today = time.strftime("%Y-%m-%d")
    key = "daily:%s:%s" % (today, media)
    hit = _cached(key)
    if hit is not None:
        return hit

    pool = []
    if media in ("mixed", "movie"):
        for r in ((BW.browse(media="movie") or {}).get("rows") or [])[:3]:
            pool.extend(r.get("items") or [])
    if media in ("mixed", "tv"):
        for r in ((BW.browse(media="tv") or {}).get("rows") or [])[:3]:
            pool.extend(r.get("items") or [])

    # 去重
    seen, uniq = set(), []
    for it in pool:
        k = it.get("douban_id") or it.get("title")
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(it)

    if not uniq:
        return _LAST_DAILY[:limit]

    # 日期做种子，保证当天稳定
    seed = int(hashlib.md5(today.encode()).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    picked = uniq[:]
    rnd.shuffle(picked)
    out = picked[:limit]
    if out:
        _save(key, out)
        try:
            _LAST_DAILY[:] = out
        except Exception:
            pass
    return out


def daily_date():
    return time.strftime("%Y-%m-%d")
