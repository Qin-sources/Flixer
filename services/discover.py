# -*- coding: utf-8 -*-
"""
TMDB discover：精确筛选（地区 + 门类 + 类型 + 排序 + 分页）

为什么不用豆瓣：
  豆瓣没有「地区 + 类型」的组合接口，只能挑一个 tag 抓，另一个条件形同虚设
  → 表现为"点美国+喜剧却出来国产片"，或者干脆筛空

TMDB /discover 支持标准参数，天然满足：
  with_genres           类型 ID
  with_origin_country   地区代码
  sort_by / page        排序与分页
  language=zh-CN        中文标题与简介
"""
import os
import time

import requests

from services.tmdb import _read_key, _get_json

BASE_LIST = [
    "https://api.tmdb.org/3",
    "https://api.themoviedb.org/3",
]

# 门类：中文 → (TMDB 媒体类型, 附加 genre id)
MEDIA = [
    ("电影", "movie", None),
    ("电视剧", "tv", None),
    ("纪录片", "movie", 99),
    ("综艺", "tv", 10764),
    ("动漫", "tv", 16),
]

REGIONS = {
    "华语": "CN|HK|TW",
    "大陆": "CN",
    "香港": "HK",
    "台湾": "TW",
    "美国": "US",
    "日本": "JP",
    "韩国": "KR",
    "英国": "GB",
    "法国": "FR",
    "德国": "DE",
    "印度": "IN",
    "泰国": "TH",
    "西班牙": "ES",
    "意大利": "IT",
    "加拿大": "CA",
    "澳大利亚": "AU",
    "俄罗斯": "RU",
}

GENRES = {
    "喜剧": 35, "动作": 28, "爱情": 10749, "科幻": 878, "恐怖": 27,
    "悬疑": 9648, "犯罪": 80, "动画": 16, "冒险": 12, "奇幻": 14,
    "剧情": 18, "战争": 10752, "惊悚": 53, "家庭": 10751, "音乐": 10402,
    "历史": 36, "西部": 37, "灾难": 10770, "武侠": 10752, "传记": 18,
}

SORTS = {
    "热度": "popularity.desc",
    "评分": "vote_average.desc",
    "最新": "primary_release_date.desc",
    "最多评价": "vote_count.desc",
}

_cache = {}
_CACHE_TTL = 600


def _cache_get(k):
    v = _cache.get(k)
    if v and time.time() < v[0]:
        return v[1]
    return None


def _cache_set(k, v):
    if len(_cache) > 400:
        _cache.clear()
    _cache[k] = (time.time() + _CACHE_TTL, v)


def options():
    return {
        "media": ["全部"] + [m[0] for m in MEDIA],
        "region": ["全部"] + list(REGIONS.keys()),
        "genre": ["全部"] + list(GENRES.keys()),
        "sort": list(SORTS.keys()),
    }


def _norm_media(name):
    for cn, kind, gid in MEDIA:
        if cn == name:
            return kind, gid
    return "movie", None


def discover(media="电影", region="全部", genre="全部", sort="热度",
             page=1, year_from=None, year_to=None, limit=24):
    kind, forced_gid = _norm_media(media)
    gids = []
    if forced_gid:
        gids.append(forced_gid)
    if genre and genre != "全部" and genre in GENRES and GENRES[genre] not in gids:
        gids.append(GENRES[genre])

    key = _read_key()
    if not key:
        return {"items": [], "page": page, "total_pages": 0, "error": "缺少 TMDB key"}

    ck = (kind, region, genre, sort, page, year_from, year_to)
    hit = _cache_get(ck)
    if hit:
        return hit

    params = {
        "api_key": key,
        "language": "zh-CN",
        "page": page,
        "sort_by": SORTS.get(sort, "popularity.desc"),
        "include_adult": "false",
    }
    if gids:
        params["with_genres"] = ",".join(str(g) for g in gids)
    if region and region != "全部" and region in REGIONS:
        params["with_origin_country"] = REGIONS[region]
    if kind == "movie":
        if year_from:
            params["primary_release_date.gte"] = "%d-01-01" % int(year_from)
        if year_to:
            params["primary_release_date.lte"] = "%d-12-31" % int(year_to)
        if sort == "评分":
            params["vote_count.gte"] = 200
    else:
        if year_from:
            params["first_air_date.gte"] = "%d-01-01" % int(year_from)
        if year_to:
            params["first_air_date.lte"] = "%d-12-31" % int(year_to)
        params["include_null_first_air_dates"] = "false"
        if sort == "评分":
            params["vote_count.gte"] = 50

    from urllib.parse import urlencode
    qs = urlencode(params)
    last_err = ""
    j = None
    for host in BASE_LIST:
        try:
            j = _get_json(host + "/discover/" + kind + "?" + qs, timeout=15)
            if j and isinstance(j, dict):
                break
        except Exception as e:
            last_err = str(e)[:80]
            j = None
    if not j:
        return {"items": [], "page": page, "total_pages": 0, "error": last_err or "TMDB 无响应"}

    out = []
    for it in (j.get("results") or []):
        if it.get("adult"):
            continue
        if kind == "movie" and forced_gid == 99:      # 纪录片
            if 99 not in (it.get("genre_ids") or []):
                continue
        if kind == "tv" and forced_gid == 16:         # 动漫
            if 16 not in (it.get("genre_ids") or []):
                continue
        date = it.get("release_date") or it.get("first_air_date") or ""
        out.append({
            "tmdb_id": it.get("id"),
            "media": "tv" if kind == "tv" else "movie",
            "title": it.get("title") or it.get("name") or "",
            "original": it.get("original_title") or it.get("original_name") or "",
            "year": date[:4],
            "rating": round(float(it.get("vote_average") or 0), 1),
            "overview": it.get("overview") or "",
            "poster": ("https://image.tmdb.org/t/p/w500" + it["poster_path"]) if it.get("poster_path") else "",
            "backdrop": ("https://image.tmdb.org/t/p/w780" + it["backdrop_path"]) if it.get("backdrop_path") else "",
            "country": "|".join(it.get("origin_country") or []),
            "genres": it.get("genre_ids") or [],
        })

    data = {
        "items": out[:limit],
        "page": int(j.get("page") or page),
        "total_pages": int(j.get("total_pages") or 0),
        "total": int(j.get("total_results") or 0),
        "filters": {"media": media, "region": region, "genre": genre, "sort": sort},
    }
    _cache_set(ck, data)
    return data
