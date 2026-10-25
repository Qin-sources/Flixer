# -*- coding: utf-8 -*-
"""
元数据平替：豆瓣 rexxar 移动接口（主）+ OMDb / TVmaze（兜底）

背景：showpaw 只覆盖 85/112，很多片没简介；豆瓣详情页被反爬拦，但
      m.douban.com/rexxar 移动接口能直接返回 JSON（简介/评分/类型/海报）。

链路：
  subject_suggest（拿 id，按年份/类型挑）
    → rexxar /api/v2/{movie|tv}/{id}   ← 中文简介，命中率最高
    → OMDb（英文原名）                  ← 外国片兜底
    → TVmaze（英文原名，仅剧集）        ← 兜底

结果缓存 30 天，避免反复打豆瓣。
"""
import json
import logging
import time

import re
import requests

CJK_RE = re.compile(r'[\u4e00-\u9fff]')

from . import stealth as ST
from .search import cache_get, cache_set

log = logging.getLogger("meta2")

TTL = 60 * 60 * 24 * 30
NEG_TTL = 60 * 10                 # 失败结果只记 10 分钟，别反复拖
_GATE_LOCK = __import__("threading").Lock()
_GATE_LAST = [0.0]
_MIN_GAP = 0.35                   # 豆瓣接口最小间隔
_FAILS = [0]                      # 连续失败次数
_COOLDOWN_UNTIL = [0.0]           # 冷却截止时间
_COOLDOWN_SEC = 60                # 冷却 60 秒（别封太久）
_FAIL_BACKOFF = [0.0]             # 当前退避

_SESS = None


def _in_cooldown():
    return time.time() < _COOLDOWN_UNTIL[0]


def _mark_fail():
    _FAILS[0] += 1
    _FAIL_BACKOFF[0] = min(8.0, _FAIL_BACKOFF[0] + 1.0)
    if _FAILS[0] >= 8:
        _COOLDOWN_UNTIL[0] = time.time() + _COOLDOWN_SEC
        log.warning("豆瓣限流，冷却 %d 秒", _COOLDOWN_SEC)


def _mark_ok():
    _FAILS[0] = 0
    _FAIL_BACKOFF[0] = 0.0


def _gate():
    with _GATE_LOCK:
        gap = _MIN_GAP + _FAIL_BACKOFF[0]
        wait = _GATE_LAST[0] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _GATE_LAST[0] = time.time()


def _sess():
    global _SESS
    if _SESS is None:
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        _SESS = s
    return _SESS


def _suggest_direct(q, tries=2):
    """直连豆瓣 suggest（实测比 Scrapling 通道稳定）"""
    for i in range(tries):
        try:
            _gate()
            r = _sess().get("https://movie.douban.com/j/subject_suggest",
                            params={"q": q},
                            headers={"Referer": "https://movie.douban.com/"},
                            timeout=12)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, list) and j:
                    return j
                return []          # 200 但空：多半被限流，别重试（由调用方计失败）
        except Exception as e:
            log.info("suggest 直连失败 %s", str(e)[:60])
        time.sleep(0.8 * (i + 1))
    return []


def _suggest(q):
    """直连 → Scrapling 兜底（冷却期内直接放弃，不浪费时间）"""
    if _in_cooldown():
        return []
    items = _suggest_direct(q)
    if items:
        _mark_ok()
        return items
    _mark_fail()
    try:
        return ST.douban_json("https://movie.douban.com/j/subject_suggest",
                              {"q": q}, timeout=12) or []
    except Exception:
        return []


def _norm_title(x):
    import re as _re
    return _re.sub(r"[\s\.\-_·:：,，、!！?？'\"“”‘’()（）\[\]【】]", "", str(x or "")).lower()


def _pick_candidate(items, year, is_tv, query=""):
    """
    挑最匹配的候选：
      · 片名必须和查询词一致（否则宁可不补，也不能张冠李戴）
      · 类型要和栏目一致（电影栏不能拿剧集的简介）
      · 有年份线索时年份越近越好
    """
    if not items:
        return None
    qn = _norm_title(query)
    best, best_s = None, -999
    for it in items:
        s = 0
        t = str(it.get("type", "")).lower()
        title = it.get("title") or ""

        # 片名一致性（最重要）
        tn = _norm_title(title)
        if qn:
            if tn == qn:
                s += 10
            elif qn in tn or tn in qn:
                s += 2
            else:
                s -= 8

        # 类型一致性
        is_tv_item = t in ("tv", "tvshow")
        if is_tv is True and not is_tv_item:
            s -= 8
        if is_tv is False and is_tv_item:
            s -= 8

        # 年份
        y = it.get("year")
        if year and y:
            try:
                d = abs(int(str(y)[:4]) - int(year))
                s += 8 if d == 0 else (3 if d <= 1 else -6)
            except Exception:
                pass

        if s > best_s:
            best, best_s = it, s

    # 匹配太弱就别用，交给后面的兜底
    return best if best_s >= 4 else None


def _rexxar(sid, typ):
    url = "https://m.douban.com/rexxar/api/v2/%s/%s" % (typ, sid)
    try:
        _gate()
        r = _sess().get(url, headers={"Referer": "https://m.douban.com/%s/%s/" % (typ, sid)},
                        timeout=15)
        if r.status_code != 200:
            return None
        j = r.json() or {}
        intro = str(j.get("intro") or "").strip()
        if not intro:
            return None
        pic = j.get("pic")
        poster = pic if isinstance(pic, str) else ((pic or {}).get("large") or "")
        rt = j.get("rating")
        rating = rt if not isinstance(rt, dict) else rt.get("value")
        gs = j.get("genres") or []
        genres = [g if isinstance(g, str) else (g or {}).get("name", "") for g in gs]
        _mark_ok()
        return {"overview": intro.replace("\n", " ").strip(),
                "poster": poster, "rating": rating,
                "genres": [g for g in genres if g],
                "douban_id": str(sid), "source": "douban-rexxar"}
    except Exception as e:
        log.info("rexxar 失败 %s", str(e)[:80])
        return None


def _omdb(name, year=None):
    if not name:
        return None
    try:
        params = {"apikey": "trilogy", "t": name, "plot": "short"}
        if year:
            params["y"] = str(year)
        r = _sess().get("https://www.omdbapi.com/", params=params, timeout=15)
        j = r.json() or {}
        plot = (j.get("Plot") or "").strip()
        if j.get("Response") != "True" or not plot or plot == "N/A":
            return None
        # 中文片名却返回纯西文片名 → 视为错配，拒绝（避免外国片海报顶替）
        _nm0 = params.get('t') or ''
        _ti0 = j.get('Title') or ''
        if CJK_RE.search(_nm0) and not CJK_RE.search(_ti0):
            return None

        return {"overview": plot, "poster": j.get("Poster") if j.get("Poster") != "N/A" else "",
                "rating": None, "genres": [g.strip() for g in (j.get("Genre") or "").split(",") if g.strip()],
                "source": "omdb", "title": j.get("Title")}
    except Exception:
        return None


def _tvmaze(name):
    if not name:
        return None
    try:
        r = _sess().get("https://api.tvmaze.com/search/shows", params={"q": name}, timeout=15)
        arr = r.json() or []
        if not arr:
            return None
        show = (arr[0] or {}).get("show") or {}
        summary = str(show.get("summary") or "")
        # 去 HTML 标签
        import re as _re
        summary = _re.sub(r"<[^>]+>", "", summary).strip()
        if not summary:
            return None
        return {"overview": summary, "poster": ((show.get("image") or {}).get("original") or ""),
                "rating": ((show.get("rating") or {}).get("average")),
                "genres": show.get("genres") or [], "source": "tvmaze",
                "title": show.get("name")}
    except Exception:
        return None


def get(title, year=None, is_tv=None, orig_title=None, douban_id=None):
    """
    取元数据（简介优先）。返回 {ok, overview, poster, rating, genres, source}
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False}

    ck = "m2:v2:%s:%s:%s" % (title, year or "", "tv" if is_tv else "movie")
    hit = cache_get(ck, TTL)
    if hit:
        return hit
    neg = cache_get(ck + ":neg", NEG_TTL)
    if neg:
        return {"ok": False}

    out = {"ok": False}

    # ⓿ TMDB（配了 key 就用它当主源：多语言 + 结构最全）
    try:
        from . import tmdb as _TMD
        for nm in (title, orig_title):
            if not nm:
                continue
            d = _TMD.search(nm, is_tv=is_tv, year=year)
            if d.get("ok"):
                out = {
                    "ok": True,
                    "overview": d.get("overview") or "",
                    "poster": d.get("poster") or "",
                    "backdrop": d.get("backdrop") or "",
                    "rating": d.get("rating"),
                    "genres": d.get("genres") or [],
                    "year": d.get("year") or "",
                    "source": "tmdb",
                }
                cache_set(ck, out)
                return out
    except Exception:
        pass

    # ① 有 douban_id 就直接取（最快、最准）
    if douban_id and not _in_cooldown():
        try:
            typ = "tv" if is_tv else "movie"
            d = _rexxar(douban_id, typ)
            if not d and typ == "movie":
                d = _rexxar(douban_id, "tv")
            if d:
                out = dict(d, ok=True)
                if year:
                    d["year"] = str(year)[:4]
                cache_set(ck, out)
                return out
        except Exception:
            pass

    # ② 豆瓣：suggest 挑候选 → rexxar
    try:
        sug = _suggest(title)
        cand = _pick_candidate(sug, year, is_tv, title)
        if cand:
            sid = cand.get("id")
            typ = "tv" if str(cand.get("type", "")).lower() in ("tv", "tvshow") else "movie"
            d = _rexxar(sid, typ)
            if d:
                if cand.get("year"):
                    d["year"] = str(cand.get("year"))[:4]
                out = dict(d, ok=True)
    except Exception as e:
        log.info("豆瓣元数据失败 %s", str(e)[:80])

    # ② OMDb（用原名/英文名）
    if not out.get("ok"):
        for nm in (orig_title, title):
            d = _omdb(nm, year)
            if d:
                out = dict(d, ok=True)
                break

    # ③ TVmaze（剧集兜底）
    if not out.get("ok") and is_tv:
        for nm in (orig_title, title):
            d = _tvmaze(nm)
            if d:
                out = dict(d, ok=True)
                break

    if out.get("ok"):
        cache_set(ck, out)
    else:
        cache_set(ck + ":neg", {"t": int(time.time())})
    return out


# ── OMDb 语言一致性校验（包装，不侵入原函数）────────────────
import re as _re_c

_CJK = _re_c.compile(r"[\u4e00-\u9fff]")
_omdb_guarded = _omdb


def _omdb(name, year=None):
    d = _omdb_guarded(name, year)
    if not d:
        return None
    req_cn = bool(_CJK.search(str(name or "")))
    got_cn = bool(_CJK.search(str(d.get("title") or "")))
    if req_cn and not got_cn:
        log.info("OMDb 结果与中文片名不符，已丢弃: %s -> %s", name, d.get("title"))
        return None
    return d
