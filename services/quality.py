"""
片源质量解析器
把 showpaw 那种粗糙的 quality 字段（4K / 1080P / REMUX / null）
解析成结构化的质量描述，并按 2K 屏加权打分。
"""
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# ---------- 分辨率 ----------
_RES_PATTERNS = [
    (r"4320p|\b8K\b",                      "8K",   4320),
    (r"2160p|\b4K\b|\bUHD\b",              "4K",   2160),
    (r"1440p|\b2K\b",                      "2K",   1440),
    (r"1080[pPiI]\b|\bFHD\b|\bBD\b|蓝光原盘", "1080P", 1080),
    (r"720p|\bHD\b",                       "720P",  720),
    (r"576p|\bSD\b",                       "576P",  576),
    (r"480p",                              "480P",  480),
]

# ---------- 来源（决定画质含金量） ----------
_SOURCE_PATTERNS = [
    (r"REMUX|原盘|BDMV|蓝光原盘",            "REMUX",  100),
    (r"BluRay|Blu-ray|\bBDrip\b|蓝光",       "BluRay",  90),
    (r"WEB-?DL|WEBRip|DSNP|迪士尼",          "WEBDL",   80),
    (r"\bWEB\b|WEB-?HD",                     "WEB",     75),
    (r"HDTV|WEB-?TV",                        "HDTV",    60),
    (r"HDTC|TC|CAM|枪版|抢先",               "CAM",      5),
]

# ---------- HDR / 音频 ----------
_HDR_PATTERNS = [
    (r"Dolby\s*Vision|杜比视界|DV\b",  "DolbyVision"),
    (r"HDR10\+|HDR10Plus",             "HDR10+"),
    (r"HDR",                            "HDR10"),
]

_AUDIO_PATTERNS = [
    (r"Atmos|杜比音效|全景声",   "Atmos"),
    (r"TrueHD",                  "TrueHD"),
    (r"DTS-?HD|DTS-?MA",         "DTS-HD"),
    (r"\bDTS\b",                 "DTS"),
    (r"DD\+|EAC3|DDP",           "DDP"),
    (r"\bAC3\b|Dolby\s*Digital", "AC3"),
    (r"\bAAC\b",                 "AAC"),
]

# ---------- 语言 / 字幕 ----------
_LANG_PATTERNS = [
    (r"国英双语|中英双语|英语中字|简繁英|双语", "bilingual_cn_en"),
    (r"国语|国配|中配|中文配音",              "mandarin"),
    (r"粤语",                                  "cantonese"),
    (r"日语",                                  "japanese"),
]
# 顺序很重要：双语/多语在前，纯中文在后
_SUB_PATTERNS = [
    (r"内封简繁英|内封简英|内封双语|内嵌双语|简繁英|中英双语|国英双语",
     "embedded_bilingual"),
    (r"外挂|外挂字幕|单独字幕|外挂双语",           "external"),
    (r"无字|无字幕",                                "none"),
    (r"内封|内嵌|内挂|内封简中|内封繁中|中字|中文字幕|简中|繁中|简繁|国语中字",
     "embedded_cn"),
]

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(T|G|M)(?:B|iB)\b", re.I)
_BITRATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(Mbps|Mb/s)", re.I)
_YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")


@dataclass
class Quality:
    height: int = 0
    resolution: str = ""
    source: str = ""
    source_score: int = 0
    hdr: list = field(default_factory=list)
    audio: list = field(default_factory=list)
    subtitle: str = ""
    languages: list = field(default_factory=list)
    size_gb: float = 0.0
    bitrate_mbps: float = 0.0
    year: int = 0
    score: float = 0.0
    tags: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _first(patterns, text):
    for pat, val, *rest in patterns:
        if re.search(pat, text, re.I):
            score = rest[0] if rest else 0
            return val, score
    return "", 0


def parse_size_gb(text: str) -> float:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0.0
    v = float(m.group(1))
    unit = m.group(2).upper()
    if unit == "T":
        v *= 1024
    elif unit == "M":
        v /= 1024
    return round(v, 2)


def parse_quality(title: str, file_size: str = "", raw_quality: str = "",
                  is_tv: bool = False) -> Quality:
    """把标题 + 体积 + 平台粗标签，解析成结构化质量。"""
    text = f"{title or ''} {raw_quality or ''}"
    q = Quality()

    # 分辨率
    q.resolution, q.height = _first(_RES_PATTERNS, text)
    # 平台标签兜底
    if not q.resolution and raw_quality:
        rq = raw_quality.upper()
        if rq in ("4K", "2160P"):
            q.resolution, q.height = "4K", 2160
        elif rq in ("1080P", "1080"):
            q.resolution, q.height = "1080P", 1080
        elif rq == "REMUX":
            q.resolution, q.height = "1080P", 1080

    # 来源
    q.source, q.source_score = _first(_SOURCE_PATTERNS, text)
    if not q.source and q.resolution in ("4K", "2160P"):
        q.source, q.source_score = "UNKNOWN", 50

    # HDR / 音频
    q.hdr = [v for pat, v, *r in _HDR_PATTERNS if re.search(pat, text, re.I)]
    q.audio = [v for pat, v, *r in _AUDIO_PATTERNS if re.search(pat, text, re.I)]

    # 字幕 / 语言
    q.subtitle, _ = _first(_SUB_PATTERNS, text)
    q.languages = [v for pat, v, *r in _LANG_PATTERNS if re.search(pat, text, re.I)]

    # 体积
    q.size_gb = parse_size_gb(file_size) or parse_size_gb(title)
    m = _BITRATE_RE.search(text)
    if m:
        q.bitrate_mbps = float(m.group(1))

    # 年份
    m = _YEAR_RE.search(title or "")
    if m:
        q.year = int(m.group(1))

    # 标签
    if re.search(r"合集|全\s*\d+\s*季|全集|两部", text):
        q.tags.append("collection")
    if re.search(r"杜比视界|DV\b", text, re.I):
        q.tags.append("dolby_vision")
    if is_tv:
        q.tags.append("tv")

    q.score = _score(q, is_tv)
    return q


def _score(q: Quality, is_tv: bool) -> float:
    """
    2K 屏（2560x1440）加权评分。
    核心原则：1080P 高码率 > 4K REMUX（屏幕到不了，还白占空间/转码慢）
    """
    s = 0.0

    # ---- 分辨率：1080P 是甜点，4K 不加分甚至减分 ----
    height_score = {
        2160: 62,   # 4K：超过屏幕，但仍有价值（下采样更锐利）——略低于 1080P
        1440: 70,
        1080: 85,   # ★ 甜点
        720: 35,
        576: 15,
        480: 5,
    }.get(q.height, 30)
    s += height_score

    # ---- 来源：1080P REMUX 是「原盘但不过量」，上限受控；4K REMUX 不再叠加 ----
    src = q.source_score * 0.35
    if q.source == "REMUX":
        if q.height >= 2160:
            src = 50 * 0.35            # 4K REMUX 已在体积项被扣，此处不额外奖
        else:
            src = min(src, 100 * 0.35)
    s += src

    # ---- 体积合理性（2K 屏 1080P 的理想体积）----
    if q.size_gb > 0:
        if is_tv:
            # 剧集：单集 1~3 GB 最好
            if 0.8 <= q.size_gb <= 4:
                s += 12
            elif q.size_gb > 20:
                s -= 10
        else:
            if 4 <= q.size_gb <= 12:
                s += 15          # 1080P 电影理想区间
            elif 12 < q.size_gb <= 30:
                s += 6
            elif q.size_gb > 60:
                s -= 14          # 87GB REMUX：空间和转码都遭罪
            elif q.size_gb < 1.5:
                s -= 12          # 太小 = 糊

    # ---- 字幕 ----
    s += {"embedded_bilingual": 18, "embedded_cn": 14, "external": 8,
          "none": -8}.get(q.subtitle, 0)

    # ---- 音频 ----
    if "Atmos" in q.audio:
        s += 5
    if "TrueHD" in q.audio or "DTS-HD" in q.audio:
        s += 4

    # ---- HDR ----
    if q.hdr:
        s += 2

    # ---- 惩罚 ----
    if q.source in ("CAM",):
        s -= 40
    if "collection" in q.tags and is_tv:
        s += 4

    return round(s, 1)


def explain(q: Quality) -> str:
    """给人看的简短说明"""
    bits = [q.resolution or "未知"]
    if q.source:
        bits.append(q.source)
    if q.hdr:
        bits.append("/".join(q.hdr))
    if q.size_gb:
        bits.append(f"{q.size_gb}GB")
    if q.subtitle:
        bits.append({"embedded_bilingual": "内封双语", "embedded_cn": "内嵌中文",
                     "external": "外挂字幕", "none": "无字幕"}.get(q.subtitle, q.subtitle))
    return " · ".join(bits)
