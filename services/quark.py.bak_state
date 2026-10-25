"""
夸克网盘免登录接口
- 分享 token（sharepage/token）
- 分享列目录（sharepage/detail），递归展开剧集
两条都不需要登录，能在用户转存之前就把剧集列表展示出来。
"""
import re
import time
import requests
from dataclasses import dataclass, field, asdict

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DRIVE = "https://drive-pc.quark.cn/1/clouddrive"
PR = "pr=ucpro&fr=pc&uc_param_str="

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".rmvb", ".webm")

_session = requests.Session()
_session.headers.update({
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://pan.quark.cn",
    "Referer": "https://pan.quark.cn/",
})


def parse_share(url: str):
    """从分享链接抽出 pwd_id 和 passcode"""
    m = re.search(r"pan\.quark\.cn/s/([0-9a-zA-Z]+)", url or "")
    if not m:
        return None, None
    pwd_id = m.group(1)
    pm = re.search(r"(?:pwd|passcode|password)=([0-9a-zA-Z]{4})", url or "")
    return pwd_id, (pm.group(1) if pm else "")


def get_stoken(pwd_id: str, passcode: str = "") -> dict:
    try:
        r = _session.post(f"{DRIVE}/share/sharepage/token?{PR}",
                          json={"pwd_id": pwd_id, "passcode": passcode},
                          timeout=20)
        d = r.json()
        if d.get("status") == 200:
            data = d.get("data") or {}
            return {
                "ok": True,
                "stoken": data.get("stoken"),
                "title": data.get("title") or "",
                "expired_at": data.get("expired_at"),
                "author_avatar": (data.get("author") or {}).get("avatar_url") or "",
                "author_type": (data.get("author") or {}).get("member_type") or "",
            }
        return {"ok": False, "message": d.get("message") or "token 获取失败"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def list_dir(pwd_id: str, stoken: str, pdir_fid: str = "0",
             page: int = 1, size: int = 100) -> dict:
    params = {
        "pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
        "pdir_fid": pdir_fid, "force": "0", "_page": str(page), "_size": str(size),
        "_fetch_banner": "0", "_fetch_share": "1", "_fetch_total": "1",
        "_sort": "file_type:asc,file_name:asc",
    }
    try:
        r = _session.get(f"{DRIVE}/share/sharepage/detail", params=params, timeout=20)
        d = r.json()
        if d.get("status") != 200:
            return {"ok": False, "message": d.get("message") or "列目录失败", "items": []}
        data = d.get("data") or {}
        items = []
        for it in (data.get("list") or []):
            items.append({
                "fid": it.get("fid"),
                "name": it.get("file_name") or "",
                "is_dir": bool(it.get("dir")),
                "size": it.get("size") or 0,
                "duration": it.get("duration") or 0,
                "file_type": it.get("file_type"),
                "l_updated_at": it.get("l_updated_at") or 0,
                "share_fid_token": it.get("share_fid_token") or "",
                "format_type": it.get("format_type") or "",
            })
        return {"ok": True, "items": items,
                "total": (d.get("metadata") or {}).get("_total") or len(items)}
    except Exception as e:
        return {"ok": False, "message": str(e), "items": []}


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name or "")]


@dataclass
class Episode:
    fid: str
    name: str
    size: int = 0
    duration: int = 0
    quality: str = ""
    size_gb: float = 0.0
    season: int = 0
    episode: int = 0

    def to_dict(self):
        return asdict(self)


_SEASON_RE = re.compile(r"(?:第\s*([0-9一二三四五六七八九十]+)\s*季|S(?:eason)?\s*(\d+)|Season\s*(\d+))", re.I)
_EP_RE = re.compile(r"(?:第\s*(\d+)\s*[集话期]|E(?:P|pisode)?\s*(\d+)|EP(\d+)|\bS(\d+)E(\d+)\b)", re.I)
_EP_STRICT_RE = re.compile(r"(?:第\s*(\d+)\s*[集话期]|E(?:P|pisode)?\s*(\d+)|EP\d+)", re.I)
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_RE = re.compile(r"第\s*([一二三四五六七八九十]+)\s*[集话期]")


def _cn2int(s):
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    if s.startswith("十"):
        return 10 + _CN_NUM.get(s[1:], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return _CN_NUM.get(a, 1) * 10 + _CN_NUM.get(b, 0)
    return 0


def parse_episode_meta(name: str):
    """从文件名抽季/集/画质"""
    nm = name or ""
    season = ep = 0

    m = _SEASON_RE.search(nm)
    if m:
        season = _cn2int(m.group(1) or m.group(2) or m.group(3))

    # 1) 优先 SxxExx —— 最可靠
    m = re.search(r"\bS(\d+)E(\d+)\b", nm, re.I)
    if m:
        season = season or int(m.group(1))
        ep = int(m.group(2))
    else:
        # 2) 中文「第X集」——支持阿拉伯数字与中文数字
        m = _CN_RE.search(nm)
        if m:
            ep = _cn2int(m.group(1))
        else:
            # 3) 阿拉伯「第N集」
            m = re.search(r"第\s*(\d+)\s*[集话期]", nm)
            if m:
                ep = int(m.group(1))
            else:
                # 4) EPnn / Episode nn
                m = re.search(r"(?:E(?:P|pisode)?)\s*(\d+)", nm, re.I)
                if m:
                    ep = int(m.group(1))
                else:
                    # 5) 片名开头就是裸数字，如 "12.mkv" / "12 - 标题.mkv"
                    m = re.match(r"^\s*(\d{1,4})\b", nm)
                    if m:
                        ep = int(m.group(1))

    q = ""
    low = nm.lower()
    for pat, label in ((r"2160p|4k|uhd", "4K"), (r"1080p|fhd", "1080P"),
                       (r"1440p|2k", "2K"), (r"720p", "720P"), (r"480p", "480P")):
        if re.search(pat, low):
            q = label
            break
    return season, ep, q


def walk(pwd_id: str, stoken: str, max_depth: int = 3,
         max_items: int = 400) -> dict:
    """
    递归展开分享目录，抽出所有视频文件。
    返回 {ok, seasons: {季: [Episode]}, movies: [Episode], others: [...]}
    """
    videos = []
    stack = [("0", 0)]
    visited = 0

    while stack and visited < max_items:
        fid, depth = stack.pop(0)
        r = list_dir(pwd_id, stoken, fid)
        if not r.get("ok"):
            continue
        for it in r["items"]:
            visited += 1
            if it["is_dir"]:
                if depth < max_depth:
                    stack.append((it["fid"], depth + 1))
                continue
            nm = it["name"]
            if not nm.lower().endswith(VIDEO_EXT):
                continue
            season, ep, q = parse_episode_meta(nm)
            videos.append(Episode(
                fid=it["fid"], name=nm, size=it["size"],
                duration=it["duration"], quality=q,
                size_gb=round(it["size"] / 1024 ** 3, 2) if it["size"] else 0,
                season=season, episode=ep,
            ))
            if visited >= max_items:
                break

    videos.sort(key=lambda e: (e.season, e.episode or 9999, _natural_key(e.name)))

    seasons = {}
    movies = []
    for v in videos:
        if v.season > 0 or v.episode > 0:
            seasons.setdefault(v.season, []).append(v)
        else:
            movies.append(v)

    return {
        "ok": True,
        "is_series": bool(seasons),
        "total_videos": len(videos),
        "seasons": {str(k): [e.to_dict() for e in sorted(v, key=lambda x: (x.episode or 9999))]
                    for k, v in sorted(seasons.items())},
        "movies": [m.to_dict() for m in movies],
    }


def inspect_share(url: str, passcode: str = "", deep: bool = True) -> dict:
    """一步到位：分享链接 → stoken → 剧集/电影结构"""
    pwd_id, pwd = parse_share(url)
    if not pwd_id:
        return {"ok": False, "message": "不是有效的夸克分享链接"}
    if not pwd and passcode:
        pwd = passcode

    tk = get_stoken(pwd_id, pwd)
    if not tk.get("ok"):
        return {"ok": False, "message": tk.get("message"), "need_passcode": True}

    result = {
        "ok": True, "pwd_id": pwd_id, "stoken": tk["stoken"],
        "share_title": tk.get("title"), "author_type": tk.get("author_type"),
    }
    if not deep:
        root = list_dir(pwd_id, tk["stoken"], "0")
        result["root"] = root.get("items") or []
        return result

    result.update(walk(pwd_id, tk["stoken"]))
    return result
