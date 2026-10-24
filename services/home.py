"""
首页数据层（方案 C：豆瓣出片单 + showpaw 补元数据）
- 豆瓣周榜经 60s API（免 key）
- 豆瓣封面有防盗链，统一走 /api/img 代理
- 每部片用 showpaw 补 TMDB 元数据（背景图/简介/评分），并落盘缓存
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import threading as _threading

from concurrent.futures import ThreadPoolExecutor, as_completed

_REFRESHING = set()
_REFRESH_LOCK = _threading.Lock()
STALE_TTL = 60 * 60 * 24 * 7      # 旧缓存最长留 7 天（先给用户看）

import requests

from . import tmdb as T
from .search import cache_get, cache_set, DB_PATH, _lock, _db

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SIXTY = ["https://60s.crystelf.top", "https://60s.viki.moe"]
DOUBAN_SUBJECTS = "https://movie.douban.com/j/search_subjects"

TTL_LISTS = 60 * 60 * 6      # 榜单 6 小时
TTL_ENRICH = 60 * 60 * 24    # 单片元数据 24 小时

# 首页分栏（顺序即展示顺序）
ROWS = [
    ("douban_movie_weekly", "豆瓣电影周榜",   "movie"),
    ("douban_movie_chinese", "华语电影",      "movie"),
    ("douban_tv_chinese",   "热度剧集 · 华语", "tv"),
    ("douban_tv_global",    "热度剧集 · 欧美", "tv"),
    ("douban_hot_movie",    "豆瓣热门电影",    "movie"),
    ("douban_hot_tv",       "豆瓣热门剧集",    "tv"),
    ("douban_show_chinese", "综艺 · 华语",     "tv"),
    ("douban_show_global",  "综艺 · 欧美",     "tv"),
    ("douban_show_more",    "热门综艺",        "tv"),
]

CAT_MAP = {
    "douban_movie_weekly": "movie",
    "douban_tv_chinese":   "tv_chinese",
    "douban_tv_global":    "tv_global",
    "douban_show_chinese": "show_chinese",
    "douban_show_global":  "show_global",
}


# ------------------------------------------------------------ 60s API
def _sixty(path, timeout=15):
    for base in SIXTY:
        try:
            r = requests.get(base + path, timeout=timeout,
                             headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


def _weekly(cat):
    d = _sixty("/v2/douban/weekly/%s" % cat)
    if not d:
        return []
    arr = d.get("data") or []
    out = []
    for a in arr:
        if not a.get("title"):
            continue
        cover = a.get("cover") or a.get("cover_proxy") or ""
        out.append({
            "title": (a.get("title") or "").strip(),
            "cover": cover,
            "rating": a.get("rating") or 0,
            "douban_id": str(a.get("id") or ""),
        })
    return out


def _douban_subjects(typ, tag, limit=20):
    try:
        from . import stealth as _ST
        d = _ST.douban_json(DOUBAN_SUBJECTS, {
            "type": typ, "tag": tag, "sort": "recommend",
            "page_limit": limit, "page_start": 0,
        }, timeout=18) or {}
        arr = d.get("subjects") or []
        out = []
        for s in arr:
            if not s.get("title"):
                continue
            out.append({
                "title": s.get("title", "").strip(),
                "cover": s.get("cover") or "",
                "rating": float(s.get("rate") or 0),
                "douban_id": str(s.get("id") or ""),
            })
        return out
    except Exception:
        return []


# ------------------------------------------------------------ 封面代理
# img9 对冷门条目会返回反爬页，统一换成 img1，从源头避免失败
_DB_SAFE_HOST = "img1.doubanio.com"


def normalize_douban_host(u: str) -> str:
    if not u or "doubanio.com" not in u:
        return u
    for h in ("img2.doubanio.com", "img3.doubanio.com", "img9.doubanio.com"):
        if h in u:
            return u.replace(h, _DB_SAFE_HOST)
    return u


_PROXY_HOSTS = ("doubanio.com", "douban.com", "tmdb.org", "image.tmdb.org")


def proxy_cover_url(u: str) -> str:
    """
    只有豆瓣/TMDB 需要服务端转发（防盗链/被浏览器 ORB 拦）；
    其他图直连，避免 /api/img 白名单拒绝（403）。
    """
    if not u:
        return ""
    u = u.strip()
    if not u.startswith("http"):
        return u
    if "/api/img" in u:
        return u
    try:
        host = (urllib.parse.urlparse(u).hostname or "").lower()
    except Exception:
        return u
    if any(host.endswith(h) for h in _PROXY_HOSTS):
        return "/api/img?u=" + urllib.parse.quote(u, safe="")
    return u


# 豆瓣 CDN 有多个节点，部分节点会对冷门条目返回反爬页（418 / text/html）
# 实测：img9 被拦时 img1 / img2 / img3 仍可正常出图
_DB_HOSTS = ["img1.doubanio.com", "img2.doubanio.com",
             "img3.doubanio.com", "img9.doubanio.com"]


def _is_image_response(r) -> bool:
    ct = (r.headers.get("Content-Type") or "").lower()
    return r.status_code == 200 and ct.startswith("image") and len(r.content) > 512


def _get_image_once(u: str, headers: dict):
    try:
        r = requests.get(u, headers=headers, timeout=20)
    except Exception:
        return None
    return (r.content, r.headers.get("Content-Type", "image/jpeg")) if _is_image_response(r) else None


def fetch_image(u: str):
    """服务端拉图片，带 Referer 伪装。返回 (bytes, content_type)"""
    from urllib.parse import urlparse, urlunparse

    if "doubanio.com" in u:
        headers = {"User-Agent": UA, "Referer": "https://movie.douban.com/",
                   "Accept": "image/*"}
    else:
        headers = {"User-Agent": UA, "Accept": "image/*"}

    # 1) 先按原 URL 取
    got = _get_image_once(u, headers)
    if got:
        return got

    # 2) 是豆瓣图就换 CDN 节点重试
    p = urlparse(u)
    if p.hostname and p.hostname.endswith("doubanio.com"):
        for h in _DB_HOSTS:
            if h == p.hostname:
                continue
            alt = urlunparse(p._replace(netloc=h))
            got = _get_image_once(alt, headers)
            if got:
                return got

    # 3) 仍失败才抛错（不返回 HTML，避免前端当成图）
    raise RuntimeError("图片不可用（豆瓣反爬）")


# ------------------------------------------------------------ 单片元数据
def _enrich_one(title, is_tv, year=None, douban_id=None):
    ck = "enrich:v3:%s:%s" % (title, "tv" if is_tv else "movie")
    hit = cache_get(ck, TTL_ENRICH)
    if hit and hit.get("ok"):
        return hit

    # ① 豆瓣移动接口（简介/评分/类型/海报，中文最全）
    meta = {}
    try:
        from . import meta2 as M2
        d = M2.get(title, year=year, is_tv=is_tv, douban_id=douban_id)
        if d.get("ok"):
            meta = {
                "ok": True,
                "overview": d.get("overview") or "",
                "poster": d.get("poster") or "",
                "backdrop": "",
                "year": d.get("year") or "",
                "vote_average": d.get("rating") or 0,
                "genres": d.get("genres") or [],
            }
    except Exception:
        meta = {}

    # ② 兜底：showpaw
    if not meta.get("ok"):
        m = T.from_showpaw(title, "", is_tv, timeout=12)
        meta = m if m.get("ok") else {"ok": False}

    cache_set(ck, meta)
    return meta


# ------------------------------------------------------------ 组装首页
def _merge_prev(ck, payload):
    """把上一版里已补好的元数据合并进来，避免越补越少"""
    try:
        prev = cache_get(ck, STALE_TTL)
        if not prev:
            return payload
        old = {}
        for row in prev.get("rows") or []:
            for it in row.get("items") or []:
                t = it.get("title")
                if t and (it.get("overview") or it.get("has_meta")):
                    old[t] = it
        for row in payload.get("rows") or []:
            for it in row.get("items") or []:
                src = old.get(it.get("title"))
                if not src:
                    continue
                for k in ("overview", "poster", "backdrop", "year", "tmdb_rating",
                          "genres", "has_meta", "poster_from"):
                    if not it.get(k) and src.get(k):
                        it[k] = src.get(k)
    except Exception:
        pass
    return payload


def _kick_refresh(max_per_row, enrich):
    """后台重建首页（同一时刻只跑一个）"""
    key = "%d:%s" % (max_per_row, enrich)
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def work():
        try:
            build_home(max_per_row=max_per_row, enrich=enrich, force=True)
        except Exception:
            pass
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(key)

    _threading.Thread(target=work, daemon=True).start()


def build_home(max_per_row=20, enrich=True, enrich_workers=3, force=False) -> dict:
    ck = "home:v1:%d:%s" % (max_per_row, enrich)

    if not force:
        hit = cache_get(ck, TTL_LISTS)
        if hit:
            hit["cached"] = True
            return hit
        # 过期了也先给用户看，后台悄悄更新
        stale = cache_get(ck, STALE_TTL)
        if stale:
            stale["cached"] = True
            stale["stale"] = True
            _kick_refresh(max_per_row, enrich)
            return stale

    t0 = time.time()
    rows = []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        for key, label, typ in ROWS:
            if key in CAT_MAP:
                futs[ex.submit(_weekly, CAT_MAP[key])] = (key, label, typ)
            elif key == "douban_hot_movie":
                futs[ex.submit(_douban_subjects, "movie", "热门", max_per_row)] = (key, label, typ)
            elif key == "douban_hot_tv":
                futs[ex.submit(_douban_subjects, "tv", "热门", max_per_row)] = (key, label, typ)
            elif key == "douban_show_more":
                # 豆瓣「综艺」标签能出 20 部；周榜综艺只有 6 部，用它补足
                futs[ex.submit(_douban_subjects, "tv", "综艺", max_per_row)] = (key, label, typ)
            elif key == "douban_movie_chinese":
                # 周榜没有华语电影分类，用豆瓣「华语」标签取
                futs[ex.submit(_douban_subjects, "movie", "华语", max_per_row)] = (key, label, typ)

        got = {}
        for f in as_completed(futs, timeout=60):
            k, label, typ = futs[f]
            try:
                got[k] = (f.result(), label, typ)
            except Exception:
                got[k] = ([], label, typ)

    order = [r[0] for r in ROWS]
    seen_titles = set()
    for key in order:
        items, label, typ = got.get(key, ([], "", ""))
        if not items:
            continue
        # 去重：同一部片不在多栏重复出现（保留首次出现）
        uniq = []
        for it in items:
            t = it.get("title")
            if t in seen_titles:
                continue
            seen_titles.add(t)
            uniq.append(it)
        items = uniq[:max_per_row]
        if len(items) < 4:          # 太短的栏不出，避免视觉上"缺一块"
            continue
        for it in items:
            it["raw_cover"] = it.get("cover") or ""
            it["cover"] = proxy_cover_url(it.get("cover") or "")
            it["is_tv"] = (typ == "tv")
        rows.append({"key": key, "label": label, "items": items})

    # ---- 用 showpaw 补元数据（后台或强制刷新时才做）----
    do_enrich = enrich and (force or cache_get(ck, STALE_TTL) is not None)
    if enrich and not do_enrich:
        # 首次冷启动：先把榜单返回，元数据后台补
        payload = {
            "rows": rows,
            "built_at": int(time.time()),
            "elapsed_ms": int((time.time() - t0) * 1000),
            "total": sum(len(r["items"]) for r in rows),
            "warm": True,
        }
        payload = _merge_prev(ck, payload)
        cache_set(ck, payload)
        _kick_refresh(max_per_row, enrich)
        payload["cached"] = False
        return payload

    if do_enrich:
        targets = []
        for row in rows:
            for it in row["items"]:
                targets.append(it)

        def work(it):
            m = _enrich_one(it["title"], it.get("is_tv"), it.get("year"), it.get("douban_id"))
            return it, m

        with ThreadPoolExecutor(max_workers=enrich_workers) as ex:
            futs = [ex.submit(work, it) for it in targets]
            try:
                for f in as_completed(futs, timeout=180):
                    try:
                        it, m = f.result()
                    except Exception:
                        continue
                    try:
                        if m.get("ok"):
                            for k_src, k_dst in (("poster", "poster"), ("backdrop", "backdrop"),
                                                 ("overview", "overview"), ("year", "year"),
                                                 ("tmdb_id", "tmdb_id"), ("media_type", "media_type")):
                                v = m.get(k_src)
                                if v:
                                    it[k_dst] = v
                            if m.get("vote_average"):
                                it["tmdb_rating"] = round(float(m["vote_average"]), 1)
                            it["genres"] = m.get("genres") or []
                            it["has_meta"] = True
                        else:
                            it["has_meta"] = False
                    except Exception:
                        continue
            except Exception:
                # 超时：没补完的就算了，先把页面给用户
                pass

    # ---- 封面兜底：TMDB 没海报时用豆瓣自己的封面，绝不空着 ----
    for row in rows:
        for it in row["items"]:
            p = (it.get("poster") or "").strip()
            c = (it.get("cover") or "").strip()
            if not p and c:
                it["poster"] = c
            # 豆瓣图统一走 img1，避开 img9 的反爬
            if "doubanio.com" in (it.get("poster") or ""):
                it["poster"] = normalize_douban_host(it["poster"])
            if "doubanio.com" in (it.get("cover") or ""):
                it["cover"] = normalize_douban_host(it["cover"])
            if not (it.get("backdrop") or "").strip():
                it["backdrop"] = it.get("poster") or c
            # 让前端知道这张是豆瓣图（比例可能是竖版，布局可据此微调）
            it["poster_from"] = "tmdb" if (p and p != c) else ("douban" if c else "none")

    payload = {
        "rows": rows,
        "built_at": int(time.time()),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "total": sum(len(r["items"]) for r in rows),
    }
    payload = _merge_prev(ck, payload)
    cache_set(ck, payload)
    payload["cached"] = False
    return payload


def hero_pick(home: dict) -> list:
    """从首页数据里挑 5 部做 Hero 轮播（优先有背景图的）"""
    pool = []
    for row in home.get("rows") or []:
        for it in row.get("items") or []:
            if it.get("backdrop"):
                pool.append(it)
    # 去重（同名）
    seen, out = set(), []
    for it in pool:
        k = it.get("title")
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= 5:
            break
    return out
