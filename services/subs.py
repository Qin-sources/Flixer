"""
字幕支持

实测：中文分享里基本不带外挂字幕（100 个条目 0 个 .srt/.ass），
所以字幕必须支持三种来源：

  1. 分享内字幕   分享目录里如果有 .srt/.ass，自动带上（有就用）
  2. 本地字幕     用户自己选文件（最可靠，随时可用）
  3. 内嵌字幕     mkv 里的字幕流，后续用 ffmpeg 抽取（暂时留接口）

本模块只负责：扫描、解码、格式转换（统一转 WebVTT）。
"""
import io
import logging
import os
import re

log = logging.getLogger("subs")

SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE, "data", "subs")


def _ensure_dir():
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except Exception:
        pass


# ------------------------------------------------------------------ 解码

def decode_bytes(b):
    """字幕编码五花八门：utf-8 / gbk / gb18030 / utf-16 都试一遍"""
    if not b:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "utf-16"):
        try:
            t = b.decode(enc)
            # 出现大量替换符说明解错了
            if t.count("\ufffd") <= 2:
                return t
        except Exception:
            continue
    return b.decode("utf-8", "ignore")


# ------------------------------------------------------------------ 格式转换

def _ts(sec):
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return "%02d:%02d:%06.3f" % (h, m, s)


def _parse_srt_time(t):
    t = t.strip().replace(",", ".")
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def srt_to_vtt(text):
    """SRT → WebVTT"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = ["WEBVTT", ""]
    for block in re.split(r"\n{2,}", text):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        # 找时间行
        idx = None
        for i, l in enumerate(lines):
            if "-->" in l:
                idx = i
                break
        if idx is None:
            continue
        times = lines[idx].split("-->")
        if len(times) != 2:
            continue
        a = _parse_srt_time(times[0])
        b = _parse_srt_time(times[1])
        if a is None or b is None:
            continue
        body = lines[idx + 1:]
        if not body:
            continue
        out.append("%s --> %s" % (_ts(a), _ts(b)))
        out.extend(body)
        out.append("")
    return "\n".join(out)


def ass_to_vtt(text):
    """ASS/SSA → WebVTT（去掉特效标签，保留文本）"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = ["WEBVTT", ""]
    fields = None
    for line in text.split("\n"):
        ls = line.strip()
        if ls.lower().startswith("format:") and "start" in ls.lower():
            fields = [x.strip().lower() for x in ls.split(":", 1)[1].split(",")]
            continue
        if not ls.lower().startswith("dialogue:"):
            continue
        body = ls.split(":", 1)[1]
        # 按逗号切，但 Dialogue 里的文本可能含逗号
        maxs = 9 if not fields else len(fields)
        parts = body.split(",", maxs - 1)
        if len(parts) < 3:
            continue
        if fields:
            try:
                i_start = fields.index("start")
                i_end = fields.index("end")
                i_text = fields.index("text")
            except ValueError:
                i_start, i_end, i_text = 1, 2, maxs - 1
        else:
            i_start, i_end, i_text = 1, 2, maxs - 1
        s = _parse_ass_time(parts[i_start])
        e = _parse_ass_time(parts[i_end])
        if s is None or e is None:
            continue
        txt = parts[i_text] if i_text < len(parts) else ""
        txt = re.sub(r"\{[^}]*\}", "", txt)          # 去特效标签
        txt = txt.replace("\\N", "\n").replace("\\n", "\n")
        txt = txt.strip()
        if not txt:
            continue
        out.append("%s --> %s" % (_ts(s), _ts(e)))
        out.extend(txt.split("\n"))
        out.append("")
    return "\n".join(out)


def _parse_ass_time(t):
    t = (t or "").strip()
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def to_vtt(text, name=""):
    """按扩展名挑转换器"""
    n = (name or "").lower()
    if n.endswith(".vtt"):
        t = text or ""
        return t if t.lstrip().startswith("WEBVTT") else ("WEBVTT\n\n" + t)
    if n.endswith((".ass", ".ssa")):
        return ass_to_vtt(text)
    # srt / sub / smi 一律当 srt 处理
    return srt_to_vtt(text)


# ------------------------------------------------------------------ 扫描分享内字幕

def is_sub(name):
    return (name or "").lower().endswith(SUB_EXT)


def collect(items, list_dir=None, max_scan=200, max_depth=3):
    """
    从分享目录树里收集字幕文件。

    list_dir(pdir) 是可选的分层列出函数（给了就递归）。
    """
    out = []
    queue = [(it, 0) for it in (items or [])]
    scanned = 0
    while queue and scanned < max_scan:
        it, depth = queue.pop(0)
        scanned += 1
        name = it.get("file_name") or ""
        if it.get("dir"):
            if list_dir and depth < max_depth:
                try:
                    queue.extend((s, depth + 1) for s in list_dir(it["fid"]))
                except Exception:
                    pass
        elif is_sub(name):
            out.append(it)
    return out


def _ep_num(name):
    m = re.search(r"(?:第|EP|E|S\d+E)?\s*(\d{1,3})(?:[^0-9]|$)", name or "", re.I)
    if not m:
        return None
    try:
        v = int(m.group(1))
    except Exception:
        return None
    if v in (2160, 1080, 720, 480, 264, 265):
        return None
    return v


def match_for_video(subs, video_name):
    """
    给当前视频挑最匹配的字幕：
      1. 对得上集号的
      2. 名字前缀相近的
      3. 都不匹配时，若只有 1 个字幕就直接用
    """
    if not subs:
        return []
    ep = _ep_num(video_name or "")
    base = re.sub(r"\.[a-z0-9]{2,4}$", "", (video_name or "")).lower()

    scored = []
    for s in subs:
        n = (s.get("file_name") or "")
        nl = n.lower()
        score = 0
        if ep is not None and _ep_num(n) == ep:
            score += 10
        # 文件名前缀重合度
        for k in range(min(len(base), len(nl)), 3, -1):
            if base[:k] == nl[:k]:
                score += 5
                break
        if nl.endswith(".vtt"):
            score += 1
        if nl.endswith(".ass"):
            score += 1
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    top = [s for sc, s in scored if sc > 0]
    return top or ([subs[0]] if len(subs) == 1 else [])


# ------------------------------------------------------------------ 缓存

def cache_path(fid):
    _ensure_dir()
    return os.path.join(_CACHE_DIR, "%s.txt" % re.sub(r"[^0-9a-zA-Z]", "", fid or "")[:40])


def cache_get(fid):
    p = cache_path(fid)
    try:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return io.open(p, encoding="utf-8").read()
    except Exception:
        pass
    return None


def cache_put(fid, text):
    try:
        _ensure_dir()
        io.open(cache_path(fid), "w", encoding="utf-8").write(text or "")
    except Exception:
        pass
