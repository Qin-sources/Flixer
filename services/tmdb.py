"""
TMDB 元数据补全
锚点表只有 id/名字，覆盖后会把海报/简介/背景图清空。
这里在没有 showpaw 数据时，按 tmdb_id 去 TMDB 取完整元数据。
多源兜底：官方 API（有 key 时）→ 公开镜像 → 降级用资源标题里的信息。
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_KEY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "tmdb_key.txt")


def _read_key():
    """key 优先级：环境变量 → data/tmdb_key.txt"""
    k = (os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_KEY") or "").strip()
    if k:
        return k
    try:
        if os.path.exists(_KEY_PATH):
            for line in open(_KEY_PATH, encoding="utf-8"):
                line = line.strip()
                # 只认 v3 key（32 位十六进制），不认 eyJ 开头的 v4 令牌
                if line and not line.startswith("eyJ"):
                    return line
    except Exception:
        pass
    return ""
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 免费代理端点（showpaw 自己就在用类似的 CF Worker 代理）
_ENDPOINTS = [
    "https://api.themoviedb.org/3/{kind}/{id}?api_key={key}&language=zh-CN",
    "https://api.tmdb.org/3/{kind}/{id}?api_key={key}&language=zh-CN",
]

IMG = "https://image.tmdb.org/t/p/"


def _get_json(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept": "application/json"})
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _year(s):
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else 0


def enrich(tmdb_id: int, media_type: str = "tv", key: str = None) -> dict:
    """
    按 tmdb_id 取元数据。
    返回 {ok, poster, backdrop, overview, year, vote_average, vote_count,
          original_title, genres, runtime, cast, director}
    """
    if not tmdb_id:
        return {"ok": False}

    key = key or _read_key()
    kind = "tv" if media_type == "tv" else "movie"

    if key:
        for tpl in _ENDPOINTS:
            d = _get_json(tpl.format(kind=kind, id=tmdb_id, key=key))
            if d and d.get("id"):
                return _parse(d, kind)
        return {"ok": False, "reason": "tmdb api failed"}

    # 无 key：用 TMDB 公开的 v3 无鉴权镜像（部分社区代理提供）
    for host in (
        "https://tmdb.shenzjd.com",
        "https://api.tmdb.org",
    ):
        url = f"{host}/3/{kind}/{tmdb_id}?language=zh-CN"
        d = _get_json(url)
        if d and d.get("id"):
            return _parse(d, kind)

    # 再试试图片元数据的公开路径
    return {"ok": False, "reason": "no tmdb access"}


def _parse(d: dict, kind: str) -> dict:
    poster = (IMG + "w500" + d["poster_path"]) if d.get("poster_path") else ""
    backdrop = (IMG + "w1280" + d["backdrop_path"]) if d.get("backdrop_path") else ""
    genres = [g.get("name") for g in (d.get("genres") or [])]
    return {
        "ok": True,
        "tmdb_id": d.get("id"),
        "title": d.get("name") or d.get("title") or "",
        "original_title": d.get("original_name") or d.get("original_title") or "",
        "poster": poster,
        "backdrop": backdrop,
        "overview": d.get("overview") or "",
        "year": _year(d.get("first_air_date") or d.get("release_date")),
        "vote_average": d.get("vote_average") or 0,
        "vote_count": d.get("vote_count") or 0,
        "genres": genres,
        "runtime": d.get("runtime") or (d.get("episode_run_time") or [0])[0],
        "status": d.get("status") or "",
    }


def from_showpaw(title: str, original_title: str = "",
                 want_tv: bool = None, timeout: int = 60) -> dict:
    """
    拿不到 TMDB key 时的最优解：用片名去问 showpaw 自己。
    它每次都返回完整元数据（海报/背景图/简介/评分），我们只借用这一部分，
    它的 TMDB 判断结果不用（那正是有 bug 的地方）。
    """
    import requests

    BASE = "https://www.showpaw.xyz/api/search"
    probes = []
    if original_title and original_title != title:
        probes.append(original_title)
    probes.append(title)

    best = None
    for q in probes:
        if not q:
            continue
        try:
            r = requests.post(BASE, json={"query": q},
                              headers={"User-Agent": _UA,
                                       "Content-Type": "application/json"},
                              timeout=timeout)
            if r.status_code != 200:
                continue
            tm = (r.json() or {}).get("tmdb") or {}
        except Exception:
            continue

        poster = tm.get("posterUrl") or ""
        backdrop = tm.get("backdropUrl") or ""
        if not (poster or backdrop):
            continue

        cand = {
            "ok": True,
            "tmdb_id": tm.get("tmdbId"),
            "title": tm.get("title") or "",
            "original_title": tm.get("originalTitle") or "",
            "poster": poster,
            "backdrop": backdrop,
            "overview": tm.get("overview") or "",
            "year": _year(tm.get("firstAirDate")),
            "vote_average": tm.get("voteAverage") or 0,
            "vote_count": tm.get("voteCount") or 0,
            "genres": tm.get("genres") or [],
            "runtime": tm.get("runtime") or 0,
            "cast": (tm.get("cast") or [])[:6],
            "director": tm.get("director") or "",
            "media_type": tm.get("type") or "",
            "source": "showpaw:" + q,
        }
        # 类型对得上就采用
        if want_tv is None or (want_tv is True and cand["media_type"] == "tv") \
                or (want_tv is False and cand["media_type"] == "movie"):
            return cand
        if best is None:
            best = cand

    return best or {"ok": False, "reason": "showpaw 也没给元数据"}


def fill_missing(chosen: dict, query: str, want_tv: bool = None) -> dict:
    """
    把缺失的海报/背景图/简介补上。
    chosen: 已经是 Candidate.to_dict() 的字典
    顺序：TMDB API(有key) → showpaw 回查 → 占位
    """
    need = not (chosen.get("poster") or chosen.get("backdrop"))
    if not need:
        return chosen

    tid = chosen.get("tmdb_id")
    mt = chosen.get("media_type") or "movie"

    # 1) 直接按 id 查 TMDB
    r = enrich(tid, mt) if tid else {"ok": False}
    if not r.get("ok"):
        # 2) 回问 showpaw
        r = from_showpaw(chosen.get("title") or query,
                         chosen.get("original_title") or "", want_tv)

    if r.get("ok"):
        for k, v in r.items():
            if k in ("ok", "source", "reason"):
                continue
            # 只补空的，不覆盖已有值
            if v and not chosen.get(k):
                chosen[k] = v
    return chosen



import re as _re

_PUNCT = _re.compile(r"[\s\.\-_·:：,，、!！?？'\u2018\u2019\u201c\u201d()（）\[\]【】《》]")


def _norm(t):
    return _PUNCT.sub("", str(t or "")).lower()


def _score(item, kind, title, year):
    """
    候选打分：片名完全一致最重要，其次年份，最后热度。
    中文片名必须精确匹配，否则容易匹配到「骇客交锋」这种带前后缀的同名片。
    """
    s = 0.0
    name = item.get("name") or item.get("title") or ""
    orig = item.get("original_name") or item.get("original_title") or ""
    q = _norm(title)
    n = _norm(name)
    o = _norm(orig)

    if q and n:
        if n == q:
            s += 100
        elif n.startswith(q) and len(n) - len(q) <= 2:
            s += 60
        elif q in n:
            s += 25
        else:
            s -= 40
    if q and o and o == q:
        s += 20

    # 年份
    date = item.get("first_air_date") or item.get("release_date") or ""
    iy = 0
    try:
        iy = int(date[:4])
    except Exception:
        pass
    if year and iy:
        d = abs(iy - int(year))
        s += 30 if d == 0 else (12 if d <= 1 else (0 if d <= 3 else -25))

    # 热度（对数，避免大热片压过精确匹配）
    pop = item.get("popularity") or 0
    try:
        s += min(20.0, (pop ** 0.5))
    except Exception:
        pass

    # 有简介的优先
    if (item.get("overview") or "").strip():
        s += 5
    return s


def search(title, is_tv=None, year=None, key=None):
    """
    按片名搜 TMDB（需要 key）。返回 {ok, overview, poster, backdrop, rating,
    genres, year, tmdb_id, source}

    改进：
      · 类型没确定时（is_tv=None）同时查 tv 和 movie，按分数挑
      · 片名精确匹配权重最高，避免「交锋」→「骇客交锋」
    """
    k = key or _read_key()
    if not k or not title:
        return {"ok": False}

    kinds = ["tv", "movie"] if is_tv is None else (["tv"] if is_tv else ["movie"])
    pool = []
    for kind in kinds:
        base = "https://api.tmdb.org/3/search/%s" % kind
        params = {"api_key": k, "language": "zh-CN", "query": title}
        if year:
            params["first_air_date_year" if kind == "tv" else "year"] = str(year)
        url = base + "?" + urllib.parse.urlencode(params)
        d = _get_json(url)
        for it in ((d or {}).get("results") or [])[:12]:
            pool.append((kind, it))

    if not pool:
        # 年份过滤太严会漏结果，放宽再试一次
        if year:
            return search(title, is_tv=is_tv, year=None, key=key)
        return {"ok": False}

    scored = [(_score(it, kind, title, year), kind, it) for kind, it in pool]
    scored.sort(key=lambda x: -x[0])
    top = scored[0]
    if top[0] < 0:
        return {"ok": False}

    _, kind, best = top
    detail_url = ("https://api.tmdb.org/3/%s/%s?api_key=%s&language=zh-CN"
                  % (kind, best.get("id"), k))
    detail = _get_json(detail_url) or best
    parsed = _parse(detail, kind)
    if not parsed.get("overview"):
        # 取候选里下一个能用的
        for sc, kd, it in scored[1:]:
            if sc < 0:
                break
            du = ("https://api.tmdb.org/3/%s/%s?api_key=%s&language=zh-CN"
                  % (kd, it.get("id"), k))
            p2 = _parse(_get_json(du) or it, kd)
            if p2.get("overview"):
                p2["rating"] = p2.get("vote_average") or None
                p2["source"] = "tmdb"
                return p2
        return {"ok": False}
    parsed["rating"] = parsed.get("vote_average") or None
    parsed["source"] = "tmdb"
    return parsed
