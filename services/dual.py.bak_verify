"""
双源搜索：showpaw（电影/剧集强）+ PanSou（纪录片/动漫/综艺强）
统一归一化 → 按分享ID去重 → 同一套质量评分排序

重要架构约束：
- 搜索链路允许走代理
- 播放链路必须直连（本文件不涉及播放，播放端见 player.py）
"""
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .quality import parse_quality, explain
from .search import _normalize_url, cache_get, cache_set

PANSOU = "http://127.0.0.1:8888"
PANSOU_PUBLIC = "https://so.252035.xyz"      # 公共实例，本地不可用时兜底
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 搜索端会话：允许跟随环境代理（用户已批准：搜索可走代理）
_search_session = requests.Session()
_search_session.headers.update({"User-Agent": UA})

# 播放端会话：强制直连，绝不吃系统代理（硬性要求）
_play_session = None


def play_session():
    """播放专用会话——强制直连，不受任何代理环境影响"""
    global _play_session
    if _play_session is None:
        s = requests.Session()
        s.trust_env = False           # ★ 忽略 HTTP_PROXY / HTTPS_PROXY / 系统代理
        s.proxies = {"http": None, "https": None}   # ★ 显式清空
        s.headers.update({"User-Agent": UA})
        _play_session = s
    return _play_session


# ---------------------------------------------------------------- PanSou
def pansou_available(timeout=3):
    try:
        r = requests.get(PANSOU + "/api/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


# 只保留夸克和磁力：其他网盘（迅雷/百度/阿里/UC/115…）用户不看
KEEP_PAN_TYPES = {"quark", "uc", "115", "123", "aliyun", "magnet", "ed2k"}
# 明确不要：baidu（需客户端）、xunlei（需客户端）


def _pansou_once(base, kw, timeout, nocache=False):
    """打一次 PanSou，返回原始 merged_by_type"""
    try:
        params = {"kw": kw, "res": "merge"}
        if nocache:
            params["cache"] = "false"
        r = _search_session.get(base + "/api/search",
                                params=params,
                                timeout=timeout)
        if r.status_code != 200:
            return None
        d = r.json()
        merged = (d.get("data") or {}).get("merged_by_type") or {}
        return merged if isinstance(merged, dict) else None
    except Exception:
        return None


def pansou_search(kw, timeout=90):
    """PanSou 搜索：本地优先，失败自动切公共实例"""
    merged = _pansou_once(PANSOU, kw, min(timeout, 20), nocache=True)
    # 插件异步：第一段常常只有零星结果，等几秒补第二段
    try:
        def _cnt(m):
            if not isinstance(m, dict):
                return 0
            return sum(len(v) for v in m.values() if isinstance(v, list))
        if _cnt(merged) < 3:
            time.sleep(4)
            m2 = _pansou_once(PANSOU, kw, min(timeout, 20))
            if isinstance(m2, dict):
                if not isinstance(merged, dict):
                    merged = m2
                else:
                    for k, v in m2.items():
                        if not isinstance(v, list):
                            continue
                        cur = merged.setdefault(k, [])
                        have = {str(x.get("url") or x.get("link") or "") for x in cur}
                        for it in v:
                            u = str(it.get("url") or it.get("link") or "")
                            if u and u not in have:
                                cur.append(it)
                                have.add(u)
    except Exception:
        pass
    if not merged:
        # 本地不可用（没代理 / 没插件 / 没启动）-> 走公共实例
        merged = _pansou_once(PANSOU_PUBLIC, kw, timeout)
    if not merged:
        return []
    d = {"data": {"merged_by_type": merged}}

    data = d.get("data") or {}
    merged = data.get("merged_by_type") or {}

    TYPE_MAP = {"quark": "quark", "aliyun": "aliyun", "baidu": "baidu",
                "xunlei": "xunlei", "uc": "uc", "115": "115", "123": "123",
                "tianyi": "tianyi", "magnet": "magnet", "ed2k": "ed2k",
                "pikachu": "pikachu", "mobile": "mobile", "guangya": "guangya"}

    out = []
    for ptype, items in merged.items():
        if not isinstance(items, list):
            continue
        pt = TYPE_MAP.get(ptype, ptype)
        if pt not in KEEP_PAN_TYPES:
            continue
        for it in items:
            url = it.get("url") or it.get("link") or ""
            if not url:
                continue
            note = it.get("note") or ""
            title = it.get("title") or ""
            if note:
                title = (title + "  " + note).strip()
            out.append({
                "title": title,
                "pan_type": pt,
                "pan_url": url,
                "passcode": it.get("password") or it.get("pwd") or "",
                "raw_quality": "",
                "file_size": "",       # PanSou 不给体积，靠标题解析
                "publish_time": it.get("datetime") or "",
                "subtitle_type": "",
                "is_alive": None,
                "source": "pansou",
            })
    return out


# ---------------------------------------------------------------- showpaw
def showpaw_search_portal(kw, timeout=60):
    return None  # 由 search.py 提供，这里不重复


# ---------------------------------------------------------------- 合并
_NOISE_WORDS = ("小说", "作者：", "作者:", "有声", "广播剧", "漫画",
                "txt", "epub", "pdf", "全集下载", "同人", "书源", "听书")
_YEAR_RE = __import__("re").compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")



_LEAD_TAG = re.compile(r"^\s*(?:[\[【(（][^\]】)）]{0,12}[\]】)）]\s*)+")
_PUNCT = re.compile(r"[「」『』“”‘’《》〈〉\s\.\-_·:：,，、!！?？'\u2018\u2019\u201c\u201d]")


def _strip_lead_tag(title):
    t = title or ""
    for _ in range(3):
        t2 = _LEAD_TAG.sub("", t)
        if t2 == t:
            break
        t = t2
    return t.strip()


_QUALIFIER = re.compile(
    r"^(?:(?:19|20)\d{2}|\d{3,4}[pi]|[48]k|hd|bd|web|webdl|web\-dl|bluray|blu\-ray|remux|"
    r"hdr|hdr10|dv|dolby|atmos|hevc|h265|h264|x265|x264|avc|aac|ddp|ac3|dts|truehd|"
    r"国语|粤语|中字|中英|双语|内封|内嵌|外挂|字幕|高清|超清|蓝光|完整版|未删减|导演剪辑|"
    r"国英|英语|原声|无删减|修复版|重制版|合集|全集|\d+集|\d+话|全\d+集)[\s\.\-\]）)]*"
)


def _qualifier_only(rest):
    """查询词后面剩下的部分是否只是发行标记（年份/清晰度/语言/字幕…）"""
    r = (rest or "").strip(" \t.-_()[]{}【】（）")
    if not r:
        return True
    for _ in range(6):
        m = _QUALIFIER.match(r)
        if not m or m.end() == 0:
            break
        r = r[m.end():].strip(" \t.-_()[]{}【】（）")
        if not r:
            return True
    return not r


def _strict_title_ok(title, q):
    """
    严格片名：去掉前缀标签后必须以查询词开头。
    「祝你生日快乐」「生日快乐之无限循环」这类不会混进来。
    """
    t = _PUNCT.sub("", _strip_lead_tag(title)).lower()
    qq = _PUNCT.sub("", q or "").lower()
    if not qq or not t:
        return True
    if not t.startswith(qq):
        return False
    rest = t[len(qq):]
    if not rest:
        return True
    # 冒号/破折号/括号开头 = 副标题或标签，属于同一部片（登峰传奇：丹增诺盖）
    if rest[0] in ":：-—–([【（《\"":
        return True
    # 其余只允许发行标记，否则是别有用心的同名（美人鱼的夏天 / 美人鱼带崽找上门）
    return _qualifier_only(rest)


_PWD_PATTERNS = [
    __import__("re").compile(r"[?&]pwd=([0-9a-zA-Z]{4,8})"),
    __import__("re").compile(r"[?&]password=([0-9a-zA-Z]{4,8})"),
    __import__("re").compile(r"提取码[\s:：]*([0-9a-zA-Z]{4,8})"),
    __import__("re").compile(r"密码[\s:：]*([0-9a-zA-Z]{4,8})"),
    __import__("re").compile(r"访问码[\s:：]*([0-9a-zA-Z]{4,8})"),
]


def _extract_pwd(url, title=""):
    """从链接或标题里抠提取码（省得用户手动输）"""
    for text in (url or "", title or ""):
        for pat in _PWD_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1)
    return ""


def _is_noise(title):
    """明显不是影视资源的条目（小说/广播剧/有声书…）"""
    t = (title or "").lower()
    return any(w in t for w in _NOISE_WORDS)


def _title_years(title):
    return [int(y) for y in _YEAR_RE.findall(title or "")]


def merge_and_rank(lists, query, original_title, year, is_tv,
                   prefer_quark=True):
    """
    lists: [(资源dict...), (资源dict...)]
    去重 → 质量解析 → 打分 → 排序
    """
    merged = {}

    for res_list in lists:
        for r in res_list:
            url = r.get("pan_url") or ""
            if not url:
                continue
            key = _normalize_url(url)
            if not key or key.startswith("url:"):
                # 磁力/ed2k 用 hash
                m = re.search(r"btih:([0-9a-fA-F]{40})", url)
                key = ("magnet:" + m.group(1).lower()) if m else ("raw:" + url[:80])
            if key in merged:
                # 已有的合并来源标记
                merged[key]["sources"] = list(set(merged[key].get("sources", []) +
                                                 [r.get("source", "?")]))
                # 补齐缺失字段
                for f in ("passcode", "file_size", "publish_time", "subtitle_type"):
                    if not merged[key].get(f) and r.get(f):
                        merged[key][f] = r[f]
                continue
            item = dict(r)
            item["sources"] = [r.get("source", "?")]
            merged[key] = item

    out = []
    for key, r in merged.items():
        q = parse_quality(r.get("title", ""), r.get("file_size", ""),
                          r.get("raw_quality", ""), is_tv)
        r["quality"] = q.to_dict()
        r["quality_text"] = explain(q)
        score = q.score

        # 源加权：夸克有会员、体验最好
        if r.get("pan_type") == "quark":
            score += 8

        # 双源命中 = 可信度更高
        if len(r.get("sources", [])) >= 2:
            score += 5

        # 磁力/ed2k：不能直接播（要离线下载），降权但不丢弃
        if r.get("pan_type") in ("magnet", "ed2k"):
            score -= 25
            r["needs_offline"] = True

        # 网盘直链类（能直接进分享目录看），优先
        if r.get("pan_type") in ("quark", "aliyun", "baidu", "uc", "115", "123"):
            score += 4

        # 存活标记
        if r.get("is_alive") is True:
            score += 8
        elif r.get("is_alive") is False:
            score -= 25

        # 标题里带「合集/全季/全集」的加权
        if re.search(r"合集|全\s*\d+\s*季|全集|季全|三季全", r.get("title", "")):
            score += 6

        # 无关标题过滤
        if not _title_relevant(r.get("title", ""), query, original_title):
            score -= 60
            r["irrelevant"] = True

        r["score"] = round(score, 1)
        out.append(r)

    # ── 去重策略 ──
    # 网盘类：按标题去重（同一部片的不同分享只留分最高的）
    # 磁力/ed2k：按 hash 去重（不同 hash = 不同种子，可能有不同季/质量，全部保留）
    by_key = {}
    for r in out:
        pt = r.get("pan_type") or ""
        if pt in ("magnet", "ed2k"):
            m = re.search(r"btih:([0-9a-fA-F]{40})", r.get("pan_url") or "")
            key = (m.group(1).lower() if m else (r.get("pan_url") or "")[:80])
        else:
            # 按分享链接去重：同一部片的不同分享要全部保留
            u = (r.get("pan_url") or "").strip().split("#")[0].rstrip("/")
            key = "nt|" + pt + "|" + u.lower()
        if key not in by_key or r["score"] > by_key[key]["score"]:
            by_key[key] = r
    out = list(by_key.values())

    # 分档：
    #   strong  —— 明确 ≥1080P
    #   unknown —— 标题没写分辨率，进分享目录核实
    #   weak    —— 明确低于 1080P
    strong, unknown, weak = [], [], []
    for r in out:
        if r.get("irrelevant"):
            continue
        h = r["quality"].get("height", 0)
        if h >= 1080:
            strong.append(r)
        elif 0 < h < 1080:
            weak.append(r)
        else:
            r["resolution_unknown"] = True
            unknown.append(r)

    strong.sort(key=lambda x: x["score"], reverse=True)
    unknown.sort(key=lambda x: x["score"], reverse=True)
    weak.sort(key=lambda x: x["score"], reverse=True)
    return strong, unknown, weak


_STRIP = re.compile(r"[\s·・\-—_.,:：!！?？\"'“”‘’()（）\[\]【】《》<>/\\]+")


def _norm(s):
    return _STRIP.sub("", (s or "").lower())


# 明显不是影视的扩展名 → 直接判无关
_NON_VIDEO_EXT = (".mobi", ".epub", ".pdf", ".txt", ".zip", ".rar", ".7z",
                  ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".mp3", ".flac", ".ape", ".wav", ".jpg", ".png", ".gif")

# 标题里出现这些词但查询里没有 → 大概率是另一部片 / 无关内容
_BAD_WORDS = ("海贼王女", "西门庆", "庆余生", "番外", "预告", "花絮",
              "从零开始", "程序员", "攻略", "教程", "课程", "电子书")


def _title_relevant(title, query, original_title):
    t = title or ""
    tl = t.lower().strip()

    # 1) 非影视文件直接否
    if tl.endswith(_NON_VIDEO_EXT):
        return False

    nt = _norm(t)
    if not nt:
        return True
    names = [n for n in (_norm(query), _norm(original_title)) if n]
    hit = any(n and n in nt for n in names)
    if not hit:
        return False

    # 2) 查询本身不含的低质词
    nq = _norm(query)
    for bad in _BAD_WORDS:
        nb = _norm(bad)
        if nb and nb in nt and nb not in nq:
            return False

    # 3) 标题首段就是长句（>28 字且含逗号）→ 多半是杂项合集
    head = re.split(r"[，,。;；]", t)[0]
    if len(head) > 30:
        return False

    return True


# ---------------------------------------------------------------- 主入口
def _annotate_live(payload):
    """用 SQLite 里已有的校验结果给每条打标（零网络请求）"""
    try:
        from . import store as _ST
        for key in ("resources", "unknown_resolution", "weak"):
            for r in (payload.get(key) or []):
                if (r.get("pan_type") or "") != "quark":
                    continue
                st = _ST.get_link(r.get("pan_url") or "")
                if not st:
                    continue
                if st["alive"]:
                    r["live_state"] = "pwd" if st.get("code") == "NEED_PASSCODE" else "ok"
                else:
                    r["live_state"] = "dead"
                    r["dead_reason"] = st.get("error") or "分享已失效"
    except Exception:
        pass
    return payload


def dual_search(query, tmdb_meta, is_tv, max_wait=60, year_hint=None):
    """
    tmdb_meta: 已裁决好的影视元数据（含 title/original_title）
    返回 {resources, weak, stats}
    """
    q = (query or "").strip()
    orig = (tmdb_meta or {}).get("original_title") or ""
    year = int((tmdb_meta or {}).get("year") or 0)
    if year_hint:
        # 调用方明确给了年份，以它为准（同名中外片靠这个区分）
        try:
            year = int(year_hint)
        except Exception:
            pass

    ck = "dual:v3:%s:%s:%s" % (q, "tv" if is_tv else "movie", year or 0)
    hit = cache_get(ck, 60 * 60 * 6)
    if hit:
        hit = _annotate_live(dict(hit))
        hit["cached"] = True
        return hit

    # 内存没命中 → 查 SQLite（重启后依然有效，秒回）
    try:
        from . import store
        disk = store.get_search(ck)
        if disk:
            disk = _annotate_live(dict(disk))
            cache_set(ck, disk)          # 回填内存
            disk["cached"] = True
            disk["from_disk"] = True
            return disk
    except Exception:
        pass

    t0 = time.time()

    # 关键词变体：中文名 + 英文原名 + 年份
    # 只搜中文名 + 英文原名；「片名+年份」实测多花 6.5 秒只多 1 条，砍掉
    variants = [q]
    if orig and orig != q:
        variants.append(orig)
    variants = list(dict.fromkeys(variants))[:2]

    pansou_results = []
    showpaw_results = []

    with ThreadPoolExecutor(max_workers=6) as ex:
        pfuts = {ex.submit(pansou_search, v, 15): ("pansou", v) for v in variants}

        # showpaw 复用 search.py 的搜索（已带缓存）
        def _showpaw():
            from .search import search as sp_search
            d = sp_search(q, want_tv=is_tv, verify=False)
            res = d.get("resources") or []
            for r in res:
                r["source"] = "showpaw"
            return res

        sfut = ex.submit(_showpaw)

        # 超时不能让整次搜索失败：拿到多少算多少
        used = set()
        try:
            for f in as_completed(pfuts, timeout=max_wait):
                used.add(f)
                try:
                    pansou_results.extend(f.result() or [])
                except Exception:
                    pass
        except Exception:
            pass
        for f in pfuts:
            if f in used:
                continue
            if f.done():
                try:
                    pansou_results.extend(f.result() or [])
                except Exception:
                    pass
            else:
                f.cancel()

        try:
            showpaw_results = sfut.result(timeout=max(3, max_wait // 3)) or []
        except Exception:
            showpaw_results = []

    # 统一过滤：只保留夸克 + 磁力/电驴，并剔除小说/广播剧这类噪声
    def _keep(lst):
        return [r for r in lst
                if (r.get("pan_type") or "") in KEEP_PAN_TYPES
                and not _is_noise(r.get("title"))]

    showpaw_results = _keep(showpaw_results)
    pansou_results = _keep(pansou_results)

    strong, unknown, weak = merge_and_rank([showpaw_results, pansou_results],
                                           q, orig, year, is_tv)

    # 提取码 + 音轨标记（都在本地算，零网络）
    try:
        from . import audio as _AU
        for r in (strong + unknown):
            if not r.get("passcode"):
                pwd = _extract_pwd(r.get("pan_url"), r.get("title"))
                if pwd:
                    r["passcode"] = pwd
        _AU.annotate_resources(strong + unknown)
    except Exception:
        pass

    # 严格片名：丢掉「祝你生日快乐」这类前缀不同的凑数条目
    if q:
        _s0, _u0 = list(strong), list(unknown)
        strong = [r for r in strong if _strict_title_ok(r.get("title"), q)]
        unknown = [r for r in unknown if _strict_title_ok(r.get("title"), q)]
        weak = [r for r in weak if _strict_title_ok(r.get("title"), q)]

        # 结果太少时放宽：包含片名或相似度 ≥ 0.62 也收进来
        if len(strong) + len(unknown) < 3:
            import difflib as _dl

            def _loose(t):
                tt = _PUNCT.sub("", _strip_lead_tag(t or "")).lower()
                qq = _PUNCT.sub("", q).lower()
                if not tt or not qq:
                    return False
                return (qq in tt) or _dl.SequenceMatcher(None, tt, qq).ratio() >= 0.62

            _kept = strong + unknown + weak
            _extra = []
            for _r in (_s0 + _u0):
                if _r in _kept or _r in _extra:
                    continue
                if _loose(_r.get("title")):
                    _extra.append(_r)
            if _extra:
                unknown = unknown + _extra

    # 年份绑定：片名里写了年份且明显冲突的，直接丢掉（同名片太多）
    if year:
        def _year_ok(r):
            ys = _title_years(r.get("title"))
            if not ys:
                return True
            return any(abs(y - year) <= 1 for y in ys)
        strong = [r for r in strong if _year_ok(r)]
        unknown = [r for r in unknown if _year_ok(r)]
        weak = [r for r in weak if _year_ok(r)]

        # 有年份线索：写得去年份的优先，没写年份的往后排
        for lst in (strong, unknown):
            for r in lst:
                ys = _title_years(r.get("title"))
                if any(abs(y - year) <= 1 for y in ys):
                    r["score"] = r.get("score", 0) + 8
                elif not ys:
                    r["score"] = r.get("score", 0) - 5

    # 先用本地缓存（SQLite）给每条打标：不发网络请求，零等待
    _annotate_live({"resources": strong, "unknown_resolution": unknown})

    # 并发验证夸克链接，失效的标出来（不删，用户能看到为什么不可用）
    try:
        from .play import verify_resources
        # 搜索阶段只验前几条、给 2 秒预算：前端打开详情时还会逐条打角标，
        # 这里做全量校验会把冷搜拖到 6 秒以上，不值当
        verify_resources((strong + unknown)[:6], max_workers=6, deadline=2.0)
    except Exception:
        pass

    # 来源统计
    def cnt(lst, src):
        return sum(1 for r in lst if src in (r.get("sources") or []))

    stats = {
        "showpaw_raw": len(showpaw_results),
        "pansou_raw": len(pansou_results),
        "merged_total": len(strong) + len(unknown) + len(weak),
        "strong_1080p_plus": len(strong),
        "resolution_unknown": len(unknown),
        "under_1080p": len(weak),
        "from_showpaw": cnt(strong, "showpaw"),
        "from_pansou": cnt(strong, "pansou"),
        "both_sources": sum(1 for r in strong if len(r.get("sources") or []) >= 2),
        "variants": variants,
        # 分类统计，前端用来分组
        "by_type": {},
        "magnet_count": 0,
        "pan_count": 0,
    }
    allres = strong + unknown
    for r in allres:
        pt = r.get("pan_type") or "other"
        stats["by_type"][pt] = stats["by_type"].get(pt, 0) + 1
        if pt in ("magnet", "ed2k"):
            stats["magnet_count"] += 1
        else:
            stats["pan_count"] += 1

    payload = {"resources": strong, "unknown_resolution": unknown[:24],
               "weak": weak[:10], "stats": stats,
               "elapsed_ms": int((time.time() - t0) * 1000), "cached": False}
    # 只有真的搜到东西才缓存；空结果不落盘，避免抖动被锁死一整天
    total_found = len(strong) + len(unknown) + len(weak)
    if total_found > 0:
        cache_set(ck, payload)
        try:
            from . import store
            store.put_search(ck, q, payload)
        except Exception:
            pass
    return payload
