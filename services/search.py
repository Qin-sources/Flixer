"""
showpaw.xyz 搜索适配层
- SQLite 缓存（把 17 秒的等待变成第二次毫秒级）
- 多关键词并搜 + 去重（把 38 条召回拉到 80~150）
- 与 matcher / quality 联动：纠错 TMDB + 结构化质量 + 排序
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict

import requests

from .matcher import (rank_candidates, needs_confirmation,
                      title_matches_resource, norm)
from .quality import parse_quality, explain

BASE = "https://www.showpaw.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "cache.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_lock = threading.Lock()
TTL_SEARCH = 60 * 60 * 12      # 搜索结果缓存 12 小时
TTL_NEGATIVE = 60 * 10         # 负缓存 10 分钟


# ---------------------------------------------------------------- cache
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS kv (
        k TEXT PRIMARY KEY, v TEXT, ts REAL)""")
    return conn


def cache_get(key, ttl):
    with _lock:
        try:
            conn = _db()
            row = conn.execute("SELECT v, ts FROM kv WHERE k=?", (key,)).fetchone()
            conn.close()
        except Exception:
            return None
    if not row:
        return None
    v, ts = row
    if time.time() - ts > ttl:
        return None
    try:
        return json.loads(v)
    except Exception:
        return None


def cache_set(key, value):
    with _lock:
        try:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)",
                         (key, json.dumps(value, ensure_ascii=False), time.time()))
            conn.commit()
            conn.close()
        except Exception:
            pass


def cache_stats():
    with _lock:
        try:
            conn = _db()
            n = conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
            conn.close()
            return {"entries": n}
        except Exception:
            return {"entries": 0}


# ---------------------------------------------------------------- showpaw
def _sp_search_raw(query: str, timeout=60):
    """单次 showpaw 搜索（原始返回）"""
    try:
        r = requests.post(BASE + "/api/search",
                          json={"query": query},
                          headers={"User-Agent": UA,
                                   "Content-Type": "application/json"},
                          timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _variant_queries(query: str, tv: bool) -> list:
    """多关键词并搜，扩大召回"""
    q = query.strip()
    out = [q]
    # 剧集：按季拆
    if tv:
        for i in ("第二季", "第三季", "第一季", "第四季"):
            out.append(f"{q} {i}")
    # 去空格变体
    if " " in q:
        out.append(q.replace(" ", ""))
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:5]


def _normalize_url(u: str) -> str:
    """用于去重：只留分享 id"""
    m = re.search(r"pan\.quark\.cn/s/([0-9a-fA-F]+)", u or "")
    if m:
        return "quark:" + m.group(1).lower()
    m = re.search(r"aliyundrive\.com/s/([\w]+)", u or "")
    if m:
        return "aliyun:" + m.group(1).lower()
    m = re.search(r"pan\.baidu\.com/s/([\w\-]+)", u or "")
    if m:
        return "baidu:" + m.group(1).lower()
    m = re.search(r"pan\.xunlei\.com/s/([\w]+)", u or "")
    if m:
        return "xunlei:" + m.group(1).lower()
    return "url:" + (u or "")


@dataclass
class Resource:
    title: str
    pan_type: str
    pan_url: str
    passcode: str = ""
    raw_quality: str = ""
    is_alive: object = None
    publish_time: str = ""
    subtitle_type: str = ""
    file_size: str = ""
    quality: dict = field(default_factory=dict)
    quality_text: str = ""
    source_variant: str = ""
    score: float = 0.0

    def to_dict(self):
        return asdict(self)


def _enrich_meta_bg(query, want_tv=None):
    """后台补元数据：慢就慢，反正不在搜索主链路上"""
    try:
        raw = _sp_search_raw(query, 25)
        tm = (raw or {}).get("tmdb") or {}
        poster = tm.get("posterUrl") or ""
        if not poster:
            return
        # 存成 meta 模块能吃到的格式，下次搜索直接命中
        try:
            from . import store
            from . import meta as META
            payload = {
                "tmdbId": tm.get("tmdbId"),
                "title": tm.get("title") or query,
                "originalTitle": tm.get("originalTitle") or "",
                "firstAirDate": tm.get("firstAirDate") or "",
                "year": int(str(tm.get("firstAirDate") or "0")[:4] or 0),
                "posterUrl": poster,
                "backdropUrl": tm.get("backdropUrl") or "",
                "overview": tm.get("overview") or "",
                "voteAverage": tm.get("voteAverage") or 0,
                "voteCount": tm.get("voteCount") or 0,
                "genres": tm.get("genres") or [],
                "runtime": tm.get("runtime") or 0,
                "cast": (tm.get("cast") or [])[:6],
                "director": tm.get("director") or "",
                "type": tm.get("type") or ("tv" if want_tv is True else "movie"),
                "_source": "showpaw-bg",
            }
            ck = "meta:v1:%s:%s" % (
                (query or "").strip(),
                "tv" if want_tv is True else ("movie" if want_tv is False else "any"))
            store.put_search("meta:" + ck, query, payload)
            try:
                META._CACHE[ck] = (time.time(), payload)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass


def search(query: str, want_tv: bool = None, max_wait: int = 45,
           verify: bool = True, max_verify: int = 12) -> dict:
    """
    主入口。返回:
    {query, tmdb, candidates, need_confirm, resources, stats, elapsed_ms, cached}
    verify=True 时对前 max_verify 条夸克资源做存活验证（并发，约 +2~5 秒）
    """
    t0 = time.time()
    q = (query or "").strip()
    if not q:
        return {"error": "empty query"}

    ck = "search:v3:%s:%s" % (q, "tv" if want_tv is True else ("movie" if want_tv is False else "any"))
    hit = cache_get(ck, TTL_SEARCH)
    if hit:
        hit = dict(hit)
        hit["cached"] = True
        hit["elapsed_ms"] = int((time.time() - t0) * 1000)
        return hit

    # ---- 1. 元数据：豆瓣 suggest 优先（0.1-0.3 秒）----
    # showpaw 单次 6-13 秒且资源恒为 0，只在豆瓣失败时兜底
    primary = None
    try:
        from . import meta as META
        m = META.quick(q, want_tv, timeout=1.5)
        if m:
            primary = {"tmdb": m, "resources": []}
    except Exception:
        primary = None

    if not primary:
        # 不阻塞：先用片名占位，后台补海报/简介（下次搜索就带着了）
        primary = {
            "tmdb": {
                "title": q, "originalTitle": "", "firstAirDate": "",
                "posterUrl": "", "backdropUrl": "", "overview": "",
                "voteAverage": 0, "voteCount": 0, "genres": [], "cast": [],
                "type": "tv" if want_tv is True else "movie",
            },
            "resources": [],
        }
        try:
            import threading as _th
            _th.Thread(target=_enrich_meta_bg, args=(q, want_tv), daemon=True).start()
        except Exception:
            pass

    # ---- 2. TMDB 裁决 ----
    tmdb_raw = primary.get("tmdb") or {}
    # showpaw 只给一个结果；把它和「资源标题里出现的高频片名」一起当候选
    cand_pool = []
    if tmdb_raw.get("tmdbId"):
        cand_pool.append({
            "tmdb_id": tmdb_raw.get("tmdbId"),
            "media_type": tmdb_raw.get("type") or "movie",
            "title": tmdb_raw.get("title") or "",
            "original_title": tmdb_raw.get("originalTitle") or "",
            "year": int((tmdb_raw.get("firstAirDate") or "0")[:4] or 0),
            "vote_average": tmdb_raw.get("voteAverage") or 0,
            "vote_count": tmdb_raw.get("voteCount") or 0,
            "popularity": 100,
            "poster": tmdb_raw.get("posterUrl") or "",
            "backdrop": tmdb_raw.get("backdropUrl") or "",
            "overview": tmdb_raw.get("overview") or "",
        })
    ranked = rank_candidates(q, cand_pool, prefer_tv=want_tv)
    chosen = ranked[0] if ranked else None

    # ★ 锚点补齐的候选没有海报/简介，这里补上
    chosen_dict = chosen.to_dict() if chosen else None
    if chosen_dict:
        try:
            from .tmdb import fill_missing
            chosen_dict = fill_missing(chosen_dict, q, want_tv)
        except Exception:
            pass
    is_tv = bool(chosen and chosen.media_type == "tv") or (want_tv is True)

    # ---- 3. 合并所有变体的资源（showpaw 已非主链路，这里通常为空）----
    merged = {}
    per_variant = {}
    _raws = {}
    if not _raws and primary:
        _raws = {q: primary}
    for v, payload in _raws.items():
        if not payload:
            per_variant[v] = 0
            continue
        cnt = 0
        for r in (payload.get("resources") or []):
            key = _normalize_url(r.get("panUrl"))
            if not key or key == "url:":
                continue
            # 资源侧二次过滤
            if chosen and not title_matches_resource(
                    q, chosen.original_title, chosen.year, r.get("title") or ""):
                continue
            if key not in merged:
                cnt += 1
                merged[key] = {
                    "title": r.get("title") or "",
                    "pan_type": r.get("panType") or "",
                    "pan_url": r.get("panUrl") or "",
                    "passcode": r.get("passcode") or "",
                    "raw_quality": r.get("quality") or "",
                    "is_alive": r.get("isAlive"),
                    "publish_time": r.get("publishTime") or "",
                    "subtitle_type": r.get("subtitleType") or "",
                    "file_size": r.get("fileSize") or "",
                    "source_variant": v,
                }
        per_variant[v] = cnt

    # ---- 4. 质量解析 + 排序 ----
    out_res = []
    for key, r in merged.items():
        qq = parse_quality(r["title"], r["file_size"], r["raw_quality"], is_tv)
        res = Resource(**r)
        res.quality = qq.to_dict()
        res.quality_text = explain(qq)
        res.score = qq.score
        # 存活优先
        if r["is_alive"] is True:
            res.score += 8
        elif r["is_alive"] is False:
            res.score -= 25
        # 夸克优先（有会员、体验最好）
        if r["pan_type"] == "quark":
            res.score += 6
        out_res.append(res)

    # 只保留 >=1080P（硬约束），但允许少量 720 兜底并标记
    strong = [r for r in out_res if r.quality.get("height", 0) >= 1080]
    weak = [r for r in out_res if 0 < r.quality.get("height", 0) < 1080]
    strong.sort(key=lambda x: x.score, reverse=True)
    weak.sort(key=lambda x: x.score, reverse=True)

    # 元数据兜底：showpaw 裁判没结果时，用豆瓣 suggest 的数据补上（标题/海报/年份）
    if not chosen_dict:
        _tm = (primary or {}).get("tmdb") or {}
        if _tm.get("title"):
            chosen_dict = {
                "title": _tm.get("title") or q,
                "original_title": _tm.get("originalTitle") or "",
                "year": _tm.get("year") or 0,
                "poster": _tm.get("posterUrl") or "",
                "backdrop": _tm.get("backdropUrl") or "",
                "overview": _tm.get("overview") or "",
                "vote_average": _tm.get("voteAverage") or 0,
                "genres": _tm.get("genres") or [],
                "douban_id": _tm.get("douban_id") or "",
            }

    payload = {
        "query": q,
        "variant_queries": [q],
        "tmdb": chosen_dict,
        "candidates": [c.to_dict() for c in ranked[:5]],
        "need_confirm": needs_confirmation(ranked) if len(ranked) > 1 else False,
        "is_tv": is_tv,
        "resources": [r.to_dict() for r in strong],
        "weak_resources": [r.to_dict() for r in weak[:8]],
        "stats": {
            "variants": per_variant,
            "raw_total": sum((len((p or {}).get("resources") or []))
                             for p in (_raws or {}).values()),
            "merged": len(merged),
            "strong_1080p_plus": len(strong),
            "under_1080p": len(weak),
            "showpaw_total_found": primary.get("totalFound"),
        },
        "cached": False,
    }

    # ---- 5. 存活验证（只验夸克，并发，只验前 N 条）----
    if verify:
        try:
            from .probe import verify_resources
            dicts = payload["resources"]
            verify_resources(dicts, max_check=max_verify, workers=6)
            live = sum(1 for r in dicts if r.get("live_state") == "ok")
            dead = sum(1 for r in dicts if r.get("live_state") == "dead")
            payload["stats"]["verified_live"] = live
            payload["stats"]["verified_dead"] = dead
        except Exception:
            pass

    cache_set(ck, payload)
    payload["elapsed_ms"] = int((time.time() - t0) * 1000)
    return payload
