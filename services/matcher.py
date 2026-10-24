"""
TMDB 精确匹配
修 showpaw 的硬伤：它直接取 TMDB 搜索第一条，导致
  海贼王 → 海贼王女 / Fena: Pirate Princess (2021)
这类严重误配。这里自己拿候选，再按多维打分裁决。
"""
import re
import time
import difflib
import requests
from dataclasses import dataclass, field, asdict
from typing import Optional

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# TMDB 官方 API 需要 key。没有 key 时走公开的镜像/代理。
# 先用 tmdb 的公开只读端点（无 key 的合规替代：用 showpaw 已带回的候选做二次裁决）
TMDB_SEARCH = "https://api.themoviedb.org/3/search/multi"

# 常见的中文名 → 确认的 tmdbId 锚点（人工校准，命中即高置信）
ANCHORS = {
    "海贼王":  {"id": 37854, "type": "tv", "title": "海贼王",
                "original_title": "One Piece", "year": 1999},
    "航海王":  {"id": 37854, "type": "tv", "title": "海贼王",
                "original_title": "One Piece", "year": 1999},
    "奥本海默": {"id": 872585, "type": "movie", "title": "奥本海默",
                "original_title": "Oppenheimer", "year": 2023},
    "流浪地球2": {"id": 842675, "type": "movie", "title": "流浪地球2",
                 "original_title": "流浪地球2", "year": 2023},
    "甄嬛传":  {"id": 50878, "type": "tv", "title": "甄嬛传",
                "original_title": "甄嬛传", "year": 2011},
    "庆余年":  {"id": 91949, "type": "tv", "title": "庆余年",
                "original_title": "庆余年", "year": 2019},
    "绝命毒师": {"id": 1396, "type": "tv", "title": "绝命毒师",
                 "original_title": "Breaking Bad", "year": 2008},
    "三体":    {"id": 120089, "type": "tv", "title": "三体",
                "original_title": "三体", "year": 2023},
}

_STRIP = re.compile(r"[\s·・\-—_.,:：!！?？\"'“”‘’()（）\[\]【】《》<>/\\]+")


def norm(s: str) -> str:
    return _STRIP.sub("", (s or "").lower())


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class Candidate:
    tmdb_id: int
    media_type: str
    title: str
    original_title: str
    year: int
    vote_average: float
    vote_count: int
    popularity: float
    poster: str
    backdrop: str
    overview: str
    score: float = 0.0
    reasons: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _year(s) -> int:
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else 0


def rank_candidates(query: str, raw_candidates: list,
                    prefer_tv: Optional[bool] = None) -> list:
    """
    对候选池打分排序。
    若查询命中人工锚点：
      - 锚点作品在候选池里 → 直接返回（跳过打分）
      - 锚点作品不在候选池里（showpaw 给错了）→ 用锚点信息合成一个候选置顶
    """
    q = norm(query)
    anchor = ANCHORS.get(query.strip())

    out = _score_all(query, raw_candidates, prefer_tv)

    if anchor:
        # 候选池里有没有锚点作品
        for c in out:
            if c.tmdb_id == anchor["id"]:
                c.score += 500
                c.reasons.append("★锚点命中")
                out.sort(key=lambda x: x.score, reverse=True)
                return out
        # 没有 → 合成锚点候选
        synth = Candidate(
            tmdb_id=anchor["id"], media_type=anchor.get("type", "tv"),
            title=anchor.get("title") or query,
            original_title=anchor.get("original_title") or "",
            year=anchor.get("year", 0),
            vote_average=0, vote_count=0, popularity=0,
            poster="", backdrop="", overview="",
            score=1000.0, reasons=["★锚点补齐(showpaw 结果不可信)"],
        )
        out.insert(0, synth)
        return out

    return out


def _score_all(query: str, raw_candidates: list, prefer_tv) -> list:
    q = norm(query)
    out = []

    for c in raw_candidates:
        title = c.get("title") or ""
        orig = c.get("original_title") or ""
        year = int(c.get("year") or 0)
        vc = int(c.get("vote_count") or 0)
        va = float(c.get("vote_average") or 0)
        pop = float(c.get("popularity") or 0)

        s = 0.0
        reasons = []

        # 1) 标题精确度
        s_title = max(sim(query, title), sim(query, orig))
        if q and (norm(title) == q or norm(orig) == q):
            s_title = 1.0
            reasons.append("标题完全一致")
        elif q and (norm(title).startswith(q) or norm(orig).startswith(q)
                    or q in norm(title) or q in norm(orig)):
            s_title = max(s_title, 0.80)
            reasons.append("标题包含")
        s += s_title * 100
        if s_title < 0.5:
            s -= 40
            reasons.append("标题不符%.2f" % s_title)

        # 2) 热度/票数
        if vc >= 20000:
            s += 22; reasons.append("超高票数")
        elif vc >= 5000:
            s += 15
        elif vc >= 1000:
            s += 9
        elif vc >= 200:
            s += 4
        elif vc >= 20:
            s += 1
        else:
            s -= 12; reasons.append("票数极少")

        # 3) 评分
        if va >= 7.5:
            s += 5
        elif va >= 6.5:
            s += 3

        # 4) 类型偏好
        if prefer_tv is True:
            s += 6 if c.get("media_type") == "tv" else -6
        elif prefer_tv is False:
            s += 6 if c.get("media_type") == "movie" else -6

        out.append(Candidate(
            tmdb_id=c.get("tmdb_id"), media_type=c.get("media_type") or "movie",
            title=title, original_title=orig, year=year,
            vote_average=va, vote_count=vc, popularity=pop,
            poster=c.get("poster") or "", backdrop=c.get("backdrop") or "",
            overview=c.get("overview") or "",
            score=round(s, 1), reasons=reasons,
        ))

    out.sort(key=lambda x: x.score, reverse=True)
    return out


def needs_confirmation(ranked: list, threshold: float = 18.0) -> bool:
    """
    第一二名差距太小 => 有歧义，应该让用户选一次。
    """
    if len(ranked) < 2:
        return False
    return (ranked[0].score - ranked[1].score) < threshold


def title_matches_resource(query: str, original_title: str, year: int,
                           resource_title: str) -> bool:
    """
    资源侧二次过滤：判断这条资源是不是真的属于这个片子。
    挡住「海贼王女」混进「海贼王」结果。
    """
    rt = resource_title or ""
    rn = norm(rt)
    names = [norm(query)]
    if original_title:
        names.append(norm(original_title))
    names = [n for n in names if n]

    if not any(n and n in rn for n in names):
        # 一个名字都没沾上 -> 不是
        return False

    # 如果有异片干扰词，且查询串本身不包含它
    qn = norm(query)
    for bad in ("海贼王女", "西门庆", "庆余生"):
        if bad in rn and bad not in qn:
            return False

    return True
