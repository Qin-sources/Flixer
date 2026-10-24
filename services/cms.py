"""
苹果CMS 采集源（LibreTV 同款方案）

和网盘方案的区别：
  - 不需要登录任何账号
  - 不需要转存
  - 接口直接返回 m3u8 播放地址，前端用 hls.js 就能播
  - 公共源普遍是 1080p（LibreTV 实测结论：没有 2K/4K）

接口约定（苹果CMS v10）：
  ?ac=videolist&wd=关键词     搜索
  ?ac=videolist&ids=vod_id    详情（用于补剧集）
返回 JSON 里 vod_play_url 形如：
  第01集$https://x/index.m3u8#第02集$https://y/index.m3u8
"""
import concurrent.futures as cf
import json
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 来自 LibreTV .env 的实测可用源（量子资源 SSL 异常已剔除）
SOURCES = [
    {"id": "ffzy",  "name": "非凡资源", "url": "https://cj.ffzyapi.com/api.php/provide/vod"},
    {"id": "bfzy",  "name": "暴风资源", "url": "https://bfzyapi.com/api.php/provide/vod"},
    {"id": "360zy", "name": "360资源",  "url": "https://360zy.com/api.php/provide/vod"},
    {"id": "mdzy",  "name": "魔都资源", "url": "https://www.mdzyapi.com/api.php/provide/vod"},
    {"id": "gszy",  "name": "光速资源", "url": "https://api.guangsuapi.com/api.php/provide/vod"},
    {"id": "bdzy",  "name": "豆瓣资源", "url": "https://api.apibdzy.com/api.php/provide/vod"},
]

SOURCE_MAP = {s["id"]: s for s in SOURCES}

_CACHE = {}
_TTL = 60 * 20


def _cache_get(k):
    v = _CACHE.get(k)
    if v and time.time() - v[0] < _TTL:
        return v[1]
    return None


def _cache_set(k, v):
    _CACHE[k] = (time.time(), v)


def _http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    txt = raw.decode("utf-8", "ignore").strip()
    # 有些源会在 JSON 前后夹垃圾字符
    if not txt.startswith("{"):
        i = txt.find("{")
        j = txt.rfind("}")
        if i >= 0 and j > i:
            txt = txt[i:j + 1]
    return json.loads(txt)


def _parse_group(grp):
    out = []
    for seg in (grp or "").split("#"):
        seg = seg.strip()
        if not seg:
            continue
        if "$" in seg:
            name, _, url = seg.partition("$")
        else:
            name, url = "", seg
        url = url.strip()
        if url.startswith("http"):
            out.append({"name": name.strip() or ("第%d集" % (len(out) + 1)),
                        "url": url})
    return out


def parse_play(raw):
    """
    苹果CMS 的 vod_play_url 可能是多线路，用 $$$ 分隔：
        正片$https://a.m3u8$$$正片$https://b.m3u8
    优先返回「含 m3u8 且集数最多」的那一组。
    """
    groups = [_parse_group(g) for g in (raw or "").split("$$$")]
    groups = [g for g in groups if g]
    if not groups:
        return []

    def gscore(g):
        m3u8 = sum(1 for e in g if ".m3u8" in e["url"].lower())
        return (m3u8, len(g))

    groups.sort(key=gscore, reverse=True)
    return groups[0]


def _normalize(it, src):
    eps = parse_play(it.get("vod_play_url"))
    pics = (it.get("vod_pic") or "").strip()
    return {
        "source": "cms",
        "site": src["name"],
        "site_id": src["id"],
        "vod_id": it.get("vod_id"),
        "title": (it.get("vod_name") or "").strip(),
        "poster": pics,
        "year": (it.get("vod_year") or "").strip(),
        "type": (it.get("type_name") or "").strip(),
        "area": (it.get("vod_area") or "").strip(),
        "remarks": (it.get("vod_remarks") or "").strip(),
        "actor": (it.get("vod_actor") or "")[:60],
        "director": (it.get("vod_director") or "")[:40],
        "lang": (it.get("vod_lang") or "").strip(),
        "episodes": eps,
        "ep_count": len(eps),
        "playable": len(eps) > 0,
    }


def search_one(src, kw, timeout=12):
    url = src["url"] + "?ac=videolist&wd=" + urllib.parse.quote(kw)
    try:
        d = _http_json(url, timeout=timeout)
    except Exception:
        return []
    lst = d.get("list") or []
    out = []
    for it in lst:
        try:
            out.append(_normalize(it, src))
        except Exception:
            continue
    return out


def search(kw, timeout=14, max_workers=6):
    """并发搜所有源，按「剧集数 + 标题匹配」排序"""
    kw = (kw or "").strip()
    if not kw:
        return []
    ck = "cms:search:%s" % kw
    hit = _cache_get(ck)
    if hit is not None:
        return hit

    results = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(search_one, s, kw, timeout) for s in SOURCES]
        for f in cf.as_completed(futs, timeout=timeout + 6):
            try:
                results.extend(f.result() or [])
            except Exception:
                pass

    key = re.sub(r"[^\w\u4e00-\u9fa5]", "", kw).lower()

    def score(r):
        s = 0
        t = re.sub(r"[^\w\u4e00-\u9fa5]", "", r.get("title") or "").lower()
        if t == key:
            s += 100
        elif key and key in t:
            s += 60
        elif t and t in key:
            s += 30
        # 有剧集可播更重要
        s += min(r.get("ep_count") or 0, 40)
        # 高清标记
        rm = (r.get("remarks") or "").lower()
        if any(k in rm for k in ("1080", "4k", "2160", "蓝光", "hd")):
            s += 20
        if re.search(r"(预告|花絮|解说|影评)", r.get("title") or ""):
            s -= 80
        return s

    results.sort(key=score, reverse=True)
    _cache_set(ck, results)
    return results


def detail(site_id, vod_id, timeout=14):
    """按 id 补全详情（拿完整剧集）"""
    src = SOURCE_MAP.get(site_id)
    if not src:
        return None
    ck = "cms:detail:%s:%s" % (site_id, vod_id)
    hit = _cache_get(ck)
    if hit is not None:
        return hit
    url = src["url"] + "?ac=videolist&ids=" + urllib.parse.quote(str(vod_id))
    try:
        d = _http_json(url, timeout=timeout)
    except Exception:
        return None
    lst = d.get("list") or []
    if not lst:
        return None
    r = _normalize(lst[0], src)
    _cache_set(ck, r)
    return r


def status():
    """各源的存活状态（轻量探测）"""
    out = []

    def one(src):
        t0 = time.time()
        try:
            d = _http_json(src["url"] + "?ac=videolist&wd=" + urllib.parse.quote("测试"),
                           timeout=8)
            ok = isinstance(d.get("list"), list)
            return {"id": src["id"], "name": src["name"], "ok": ok,
                    "ms": int((time.time() - t0) * 1000),
                    "count": len(d.get("list") or [])}
        except Exception as e:
            return {"id": src["id"], "name": src["name"], "ok": False,
                    "ms": int((time.time() - t0) * 1000), "error": str(e)[:60]}

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(one, SOURCES):
            out.append(r)
    return out
