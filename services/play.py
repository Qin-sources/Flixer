"""
播放链路（最终版）

关键突破：
  夸克的 /file/download 对「浏览器 UA」有单文件大小限制（code 23018），
  换成【客户端 UA】就没有限制 —— 这就是电视端软件能播网盘大文件的原理。

流程：
  分享链接 -> 转存到自己网盘 -> 客户端UA 拿直链 -> 后端代理转发（带 Range）

注意：直链签名绑定领取时的 cookie，代理时必须原样带上，否则 412。
"""
import json
import re
import time

import requests

from . import quark as Q
from . import quark_auth as QA

API = "https://drive-pc.quark.cn/1/clouddrive"
BASE = {"pr": "ucpro", "fr": "pc"}

# ★ 关键：必须是客户端 UA，浏览器 UA 会被大小限制拦住
CLIENT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
             "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch")
REFERER = "https://pan.quark.cn"
CACHE_DIR_NAME = "Flixer缓存"      # 所有转存都放这里，不污染根目录
_cache_dir_fid = [None]

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".ts", ".avi", ".rmvb", ".flv",
             ".wmv", ".m2ts", ".mpg", ".webm")


def _hdr():
    return {"User-Agent": CLIENT_UA, "Referer": REFERER,
            "Accept": "application/json, text/plain, */*"}


def _get(path, params=None, timeout=25):
    try:
        r = QA.session().get(API + path, params={**BASE, **(params or {})},
                             headers=_hdr(), timeout=timeout)
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"_err": str(e)[:100]}


def _post(path, body, params=None, timeout=25):
    try:
        r = QA.session().post(API + path, json=body,
                              params={**BASE, **(params or {})},
                              headers=_hdr(), timeout=timeout)
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"_err": str(e)[:100]}


# ---------------------------------------------------------------- 分享
def _stoken(pwd_id, passcode=""):
    c, d = _post("/share/sharepage/token",
                 {"pwd_id": pwd_id, "passcode": passcode or ""})
    return (d.get("data") or {}).get("stoken")


def _list(pwd_id, stoken, pdir="0"):
    c, d = _get("/share/sharepage/detail", {
        "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir,
        "force": "0", "_page": "1", "_size": "100",
        "_fetch_banner": "0", "_fetch_share": "0", "_fetch_total": "1",
        "_sort": "file_type:asc,file_name:asc",
    })
    return ((d.get("data") or {}).get("list")) or []


def _save(pwd_id, stoken, items, to_pdir="0"):
    body = {
        "fid_list": [i["fid"] for i in items],
        "fid_token_list": [i.get("share_fid_token") for i in items],
        "to_pdir_fid": to_pdir,
        "pwd_id": pwd_id, "stoken": stoken,
        "pdir_fid": "0", "scene": "link",
    }
    c, d = _post("/share/sharepage/save", body)
    task = (d.get("data") or {}).get("task_id")
    if not task:
        return None, d.get("message") or "转存失败"
    for _ in range(30):
        time.sleep(1)
        c2, r2 = _get("/task", {"task_id": task, "retry_index": 0})
        dd = r2.get("data") or {}
        st = dd.get("status")
        if st == 2:
            fids = (dd.get("save_as") or {}).get("save_as_top_fids") or []
            return (fids[0] if fids else None), None
        if st == 3:
            return None, dd.get("message") or "转存失败"
    return None, "转存超时"


def _list_dir(pdir="0", size=100):
    c, d = _get("/file/sort", {
        "pdir_fid": pdir, "_page": "1", "_size": str(size),
        "_fetch_total": "1", "_sort": "file_type:asc,file_name:asc",
    })
    return ((d.get("data") or {}).get("list")) or []


def ensure_cache_dir():
    """确保专属缓存目录存在，返回它的 fid"""
    if _cache_dir_fid[0]:
        return _cache_dir_fid[0]
    for it in _list_dir("0", 200):
        if it.get("dir") and (it.get("file_name") or "").strip() == CACHE_DIR_NAME:
            _cache_dir_fid[0] = it["fid"]
            return it["fid"]
    c, d = _post("/file", {
        "dir_init_lock": False, "dir_path": "",
        "file_name": CACHE_DIR_NAME, "pdir_fid": "0",
    })
    # 冲突（已存在）也算成功，回头再查一次
    for it in _list_dir("0", 200):
        if it.get("dir") and (it.get("file_name") or "").strip() == CACHE_DIR_NAME:
            _cache_dir_fid[0] = it["fid"]
            return it["fid"]
    return None


def find_existing(name, size, pdir):
    """在缓存目录里找同名同大小的文件，找到就复用，避免重复转存"""
    if not pdir or not name:
        return None
    try:
        for it in _list_dir(pdir, 200):
            if it.get("dir"):
                continue
            if (it.get("file_name") or "").strip() != name.strip():
                continue
            try:
                if int(it.get("size") or 0) == int(size or 0):
                    return it.get("fid")
            except Exception:
                pass
    except Exception:
        pass
    return None


def verify_resources(resources, max_workers=8, deadline=6.0):
    """
    并发验证资源里的夸克链接，原地写入 alive 字段。
    alive: True 可用 / False 已失效 / None 未验证（超时或非夸克）
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = [r for r in resources
               if (r.get("pan_type") or "") == "quark" and r.get("pan_url")]
    if not targets:
        return resources

    def one(r):
        try:
            return r, check_share_cached(r["pan_url"], r.get("passcode") or "")
        except Exception:
            return r, {"ok": False}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
        futs = [ex.submit(one, r) for r in targets]
        try:
            for f in as_completed(futs, timeout=deadline):
                try:
                    r, res = f.result(timeout=0.1)
                except Exception:
                    continue
                if res.get("ok"):
                    r["alive"] = bool(res.get("alive"))
                    r["alive_note"] = res.get("error") or ""
                    if res.get("need_pwd"):
                        r["alive_note"] = "需要提取码"
                else:
                    r["alive"] = None
        except Exception:
            pass   # 超时：没验完的保持 None

    # 补一下没轮到的
    for r in targets:
        if "alive" not in r:
            r["alive"] = None
    return resources


def cache_stats():
    """缓存目录占用情况"""
    pdir = ensure_cache_dir()
    if not pdir:
        return {"exists": False, "count": 0, "bytes": 0}
    items = _list_dir(pdir, 500)
    total = 0
    n = 0
    for it in items:
        if it.get("dir"):
            continue
        n += 1
        try:
            total += int(it.get("size") or 0)
        except Exception:
            pass
    return {"exists": True, "dir_fid": pdir, "count": n, "bytes": total,
            "gb": round(total / (1024 ** 3), 2)}


def clear_cache():
    """清空缓存目录里的文件"""
    pdir = ensure_cache_dir()
    if not pdir:
        return {"ok": False, "error": "缓存目录不存在"}
    items = [it for it in _list_dir(pdir, 500) if not it.get("dir")]
    if not items:
        return {"ok": True, "deleted": 0}
    fids = [it["fid"] for it in items]
    c, d = _post("/file/delete", {"fids": fids, "exclude_fids": []})
    # 异步任务，尽量等它完成
    task = (d.get("data") or {}).get("task_id")
    if task:
        for _ in range(20):
            time.sleep(1)
            c2, r2 = _get("/task", {"task_id": task, "retry_index": 0})
            if (r2.get("data") or {}).get("status") == 2:
                break
    return {"ok": True, "deleted": len(fids)}


def _find_subs(pwd_id, stoken, items, max_scan=120):
    """扫描分享目录树里的字幕文件"""
    from .subs import is_sub
    out, queue, scanned = [], [(it, 0) for it in items], 0
    while queue and scanned < max_scan:
        it, depth = queue.pop(0)
        scanned += 1
        if it.get("dir"):
            if depth < 3:
                queue.extend((s2, depth + 1) for s2 in _list(pwd_id, stoken, it["fid"]))
        elif is_sub(it.get("file_name")):
            out.append(it)
    return out


def _find_videos(pwd_id, stoken, items, max_dirs=80, max_files=4000):
    """展开目录，返回 (videos, groups)

    关键：名额只算「目录」，文件不占名额。
    否则大合集（9季+剧场版共 290+ 文件）会在中途就停，后面的季全丢。
    并发展开，避免逐层串行导致 18 秒的等待。
    """
    from concurrent.futures import ThreadPoolExecutor

    out = []
    buckets = {}
    order = []
    root_items = [it for it in items]

    def _bucket(path):
        if path not in buckets:
            buckets[path] = {"name": (path.split("/")[-1] if path else ""), "items": []}
            order.append(path)
        return buckets[path]

    # 第一层：根目录直接的文件
    for it in root_items:
        if not it.get("dir") and (it.get("file_name") or "").lower().endswith(VIDEO_EXT):
            out.append(it)
            _bucket("")["items"].append(it)

    dirs = []           # (fid, depth, prefix)
    for it in root_items:
        if it.get("dir"):
            dirs.append((it["fid"], 1, it.get("file_name") or it.get("name") or ""))

    dir_count = 0
    while dirs and dir_count < max_dirs:
        batch = dirs[:max_dirs - dir_count]
        dirs = dirs[len(batch):]
        dir_count += len(batch)

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda d: _list(pwd_id, stoken, d[0]), batch))

        for (fid, depth, prefix), children in zip(batch, results):
            for it in (children or []):
                if it.get("dir"):
                    if depth < 4:
                        dirs.append((it["fid"], depth + 1,
                                     prefix + "/" + (it.get("file_name") or it.get("name") or "")))
                    continue
                nm = it.get("file_name") or ""
                if not nm.lower().endswith(VIDEO_EXT):
                    continue
                out.append(it)
                _bucket(prefix)["items"].append(it)
                if len(out) >= max_files:
                    break

    groups = []
    for path in order:
        b = buckets[path]
        if not b["items"]:
            continue
        if not b["name"]:
            b["name"] = "全部文件"
        try:
            b["items"].sort(key=lambda x: _nat_key(x.get("file_name") or ""))
        except Exception:
            pass
        groups.append(b)
    return out, groups


# 浏览器原生尝试的容器（实测 Chrome 可直读 mkv 内的 HEVC / H264）
NATIVE_EXT = (".mp4", ".m4v", ".webm", ".mov", ".mkv", ".ts", ".m2ts", ".mts")
# 浏览器基本不可能解的容器，只给提示，不拦播放
UNSUPPORTED_EXT = (".avi", ".wmv", ".rmvb", ".rm", ".flv",
                   ".mpg", ".mpeg", ".vob", ".3gp", ".asf", ".f4v")
# 兼容旧代码引用
BROWSER_OK_EXT = NATIVE_EXT
MKV_EXT = (".mkv",)


def _ext(name):
    n = (name or "").lower()
    for e in VIDEO_EXT:
        if n.endswith(e):
            return e
    return ""


def _audio_of(name):
    """音轨兼容性：杜比系 Chrome 无声"""
    try:
        from . import audio
        a = audio.from_name(name)
        return {"codec": a.get("codec") or "", "label": a.get("label") or "",
                "browser_ok": a.get("browser_ok")}
    except Exception:
        return {"codec": "", "label": "", "browser_ok": None}



def _video_of(fid):
    """探测视频轨数量/编码（多条视频轨=有假轨，浏览器无法切轨）"""
    try:
        from . import audio as _AU3
        return _AU3.probe_video("http://127.0.0.1:7100/api/stream?fid=" + str(fid))
    except Exception:
        return None

def _nat_key(name):
    """自然排序键：把文件名里的数字按数值比较（第2集 < 第10集）"""
    import re as _re
    parts = _re.split(r"(\d+)", (name or "").lower())
    return [int(p) if p.isdigit() else p for p in parts]


_EP_RE = re.compile(r"(?:^|[^0-9])(?:第|EP|E|S\d+E)?\s*(\d{1,4})(?:[^0-9]|$)", re.I)
_QUALITY_HINT = ("2160", "4k", "uhd", "原盘", "remux")


def _ep_number(name):
    """从文件名里抠集号；抠不到返回 None"""
    n = (name or "")
    m = _EP_RE.search(n)
    if not m:
        return None
    try:
        v = int(m.group(1))
    except Exception:
        return None
    # 排除掉 2160/1080 这类分辨率数字
    if v in (2160, 1080, 720, 480, 264, 265, 60, 30, 24):
        return None
    return v


def _quality_rank(v):
    """同一集的多个版本里挑最好的：4K 优先，其次体积"""
    name = (v.get("file_name") or "").lower()
    q = 0
    for hint in _QUALITY_HINT:
        if hint in name:
            q = 2
            break
    try:
        size = int(v.get("size") or 0)
    except Exception:
        size = 0
    return (q, size)


def _build_groups(groups, vsorted=None):
    """把目录分组直接映射给前端（不再经过 vsorted 去重，点哪个文件播哪个）"""
    try:
        out = []
        for g in (groups or []):
            items = []
            for it in (g.get("items") or []):
                items.append({
                    "fid": it.get("fid"),
                    "name": it.get("file_name") or it.get("name") or "",
                    "size": it.get("size") or 0,
                })
            if items:
                out.append({"name": g.get("name") or "全部文件",
                            "count": len(items), "items": items})
        # 单组且只有 1 个文件时不显示分组（电影）
        if len(out) == 1 and out[0]["count"] <= 1:
            return []
        return out
    except Exception:
        return []


def _sort_episodes(videos):
    """
    按集号分组，每组只留最佳版本。

    返回 (选中的视频列表, 选集元数据列表)
      例如 30 集 × 3 版本 → 30 个条目，每集取 4K 那版
    """
    groups = {}
    singles = []
    for v in videos:
        num = _ep_number(v.get("file_name") or "")
        if num is None:
            singles.append(v)
        else:
            groups.setdefault(num, []).append(v)

    chosen = []
    if groups:
        for num in sorted(groups.keys()):
            best = max(groups[num], key=_quality_rank)
            chosen.append((num, best))
        # 抠不到集号的（花絮/样片之类）排最后
        for v in sorted(singles, key=lambda x: _nat_key(x.get("file_name") or "")):
            chosen.append((None, v))
    else:
        for v in sorted(videos, key=lambda x: _nat_key(x.get("file_name") or "")):
            chosen.append((None, v))

    vs = [c[1] for c in chosen]
    eps = []
    for i, (num, v) in enumerate(chosen):
        eps.append({
            "i": i,
            "ep": num,
            "name": v.get("file_name") or "",
            "size": v.get("size"),
        })
    return vs, eps


def _pick(items):
    """
    挑画质最好的那份，不再优先 mp4。

    既然浏览器能原生播 mkv/hevc，就没必要为了容器牺牲分辨率。
    规则：排除浏览器绝对解不了的容器 → 取最大文件。
    """
    if not items:
        return None

    def sz(i):
        try:
            return int(i.get("size") or 0)
        except Exception:
            return 0

    pool = [i for i in items if _ext(i.get("file_name")) not in UNSUPPORTED_EXT]
    if not pool:
        pool = items
    big = [i for i in pool if sz(i) > 200 * 1024 * 1024]
    return max(big or pool, key=sz)


# ---------------------------------------------------------------- 直链
_LINK_CACHE = {}

# 链接有效性缓存：url -> (alive, error, ts)
_CHECK_CACHE = {}
_CHECK_TTL = 60 * 60          # 1 小时


def check_share_cached(share_url, passcode=""):
    """
    判断分享是否还有效，两层缓存：
      内存（1 小时）→ SQLite（1 小时，重启不丢）→ 真正去问夸克
    """
    now = time.time()
    hit = _CHECK_CACHE.get(share_url)
    if hit and now - hit[2] < _CHECK_TTL:
        return {"ok": True, "alive": hit[0], "error": hit[1], "cached": True}

    # SQLite 层
    if not passcode:
        try:
            from . import store
            disk = store.get_link(share_url)
            if disk:
                _CHECK_CACHE[share_url] = (disk["alive"], disk["error"], now)
                return {"ok": True, "alive": disk["alive"],
                        "error": disk["error"], "cached": True, "from_disk": True}
        except Exception:
            pass

    r = check_share(share_url, passcode)
    if r.get("ok"):
        alive = bool(r.get("alive"))
        err = r.get("error") or ""
        _CHECK_CACHE[share_url] = (alive, err, now)
        if not passcode:
            try:
                from . import store
                code = "NEED_PASSCODE" if r.get("need_pwd") else (r.get("code") or "")
                if not code and not alive:
                    code = "SHARE_DEAD"
                store.put_link(share_url, alive, code, err)
            except Exception:
                pass
    return r


def direct_url(fid, force=False):
    """用客户端 UA 取下载直链（fid 必须是自己网盘里的文件）"""
    now = time.time()
    if not force:
        hit = _LINK_CACHE.get(fid)
        if hit and now < hit[1]:
            return hit[0], None

    c, d = _post("/file/download", {"fids": [fid]})
    if d.get("code") == 23018:
        return None, "SIZE_LIMIT"
    if c != 200 or d.get("status") != 200:
        return None, d.get("message") or ("HTTP %s" % c)
    arr = d.get("data") or []
    if not arr:
        return None, "没有返回直链"
    url = arr[0].get("download_url") or arr[0].get("url") or ""
    if not url:
        return None, "直链为空"
    _LINK_CACHE[fid] = (url, now + 50 * 60)
    return url, None


def stream_headers():
    """拉流必须带的头（缺 cookie 会 412）"""
    return {
        "User-Agent": CLIENT_UA,
        "Referer": REFERER,
        "Cookie": QA.load_cookie(),
    }


def file_info(fid):
    c, d = _get("/file/info", {"fid": fid})
    return d.get("data") or {}


# ---------------------------------------------------------------- 对外
def _err(code, msg, extra=None):
    out = {"ok": False, "error": msg, "code": code}
    if extra:
        out.update(extra)
    return out


def _verify_share_alive(pwd_id, passcode=""):
    """
    校验分享是否可用，返回 (stoken, err_dict)
    err_dict 为 None 表示可用
    """
    c, d = _post("/share/sharepage/token",
                 {"pwd_id": pwd_id, "passcode": passcode or ""})
    status = d.get("status")
    code = d.get("code")
    msg = d.get("message") or ""

    if status == 200 and code == 0:
        st = (d.get("data") or {}).get("stoken")
        if st:
            return st, None
        return None, _err("NO_STOKEN", "分享返回了空凭证（stoken），这个链接可能已失效")

    if code == 41011 or "失效" in msg or "不存在" in msg:
        return None, _err("SHARE_DEAD", "分享已失效")
    if code in (41010, 41012) or "提取码" in msg or "passcode" in msg.lower():
        return None, _err("NEED_PASSCODE", "需要提取码")
    if status == 401 or "login" in msg.lower():
        return None, _err("NEED_LOGIN", "夸克登录已过期，请重新登录")

    return None, _err("TOKEN_FAIL", "获取分享凭证失败：%s" % (msg[:60] or code))


def check_share(share_url, passcode=""):
    """
    轻量验证：只拿 stoken，不转存、不下载。
    返回 {ok, alive, error}
    """
    from . import quark as _Q
    pwd_id, pw = _Q.parse_share(share_url)
    if not pwd_id:
        return {"ok": False, "alive": False, "error": "链接无效"}
    c, d = _post("/share/sharepage/token",
                 {"pwd_id": pwd_id, "passcode": passcode or pw})
    code = d.get("code")
    if d.get("status") == 200:
        return {"ok": True, "alive": True, "title": (d.get("data") or {}).get("title") or ""}
    msg = d.get("message") or ""
    if code == 41011 or "失效" in msg or "不存在" in msg:
        return {"ok": True, "alive": False, "error": "分享已失效"}
    if code in (41010, 41012) or "提取码" in msg or "passcode" in str(msg).lower():
        return {"ok": True, "alive": True, "need_pwd": True, "error": "需要提取码"}
    return {"ok": True, "alive": False, "error": msg or ("code %s" % code)}


def resolve(share_url, passcode="", want_title="", ep=None, want_fid=None):
    """
    解析分享 -> 转存 -> 出直链
    每一步失败都返回可读的错误，不再让「stoken 找不到」这种底层异常透出去
    """
    if not QA.load_cookie():
        return {"ok": False, "need_login": True, "error": "需要先登录夸克"}

    pwd_id, pw = Q.parse_share(share_url)
    if not pwd_id:
        return _err("BAD_URL", "分享链接格式不对")

    # ① 校验分享是否还活着（这里能区分「失效」「要提取码」「登录过期」）
    st, verr = _verify_share_alive(pwd_id, passcode or pw)
    if verr:
        return verr

    # ② 列目录
    items = _list(pwd_id, st)
    if not items:
        return _err("EMPTY_SHARE", "分享内容为空（可能已删除）")

    videos, _groups = _find_videos(pwd_id, st, items)
    if not videos:
        return {"ok": False, "code": "NO_VIDEO", "error": "这个分享里没有视频文件"}

    # 多集时排序，供前端选集
    vsorted, episodes = _sort_episodes(videos)

    target = None
    ep_index = None

    # ① 明确指定了第几集
    try:
        if ep is not None and str(ep) != "":
            idx = int(ep)
            if 0 <= idx < len(vsorted):
                target = vsorted[idx]
                ep_index = idx
    except Exception:
        target = None

    # ② 按片名匹配
    if target is None and want_title:
        key = re.sub(r"[^\w\u4e00-\u9fa5]", "", want_title)[:12]
        if key:
            for i, v in enumerate(vsorted):
                if key in re.sub(r"[^\w\u4e00-\u9fa5]", "", v.get("file_name") or ""):
                    target = v
                    ep_index = i
                    break

    # ②b 直接指定了文件 fid（点目录里的某一个文件）
    if target is None and want_fid:
        for i, v in enumerate(vsorted):
            if str(v.get("fid")) == str(want_fid):
                target = v
                ep_index = i
                break
        if target is None:
            for v in videos:
                if str(v.get("fid")) == str(want_fid):
                    target = v
                    ep_index = 0
                    break

    # ③ 默认：只有一集就直接用；多集取第一集（不再是"最大的那个"）
    if target is None:
        if len(vsorted) == 1:
            target = vsorted[0]
            ep_index = 0
        else:
            target = vsorted[0]
            ep_index = 0

    # 分享内字幕：按集号匹配当前视频，最多带 2 个
    subs_out = []
    try:
        from . import subs as SUB
        raw_subs = _find_subs(pwd_id, st, items)
        matched = SUB.match_for_video(raw_subs, target.get("file_name") or "")
        for sf in matched[:2]:
            sname = sf.get("file_name") or ""
            ssaved = find_existing(sname, sf.get("size"), ensure_cache_dir())
            if not ssaved:
                ssaved, serr = _save(pwd_id, st, [sf], to_pdir=ensure_cache_dir() or "0")
            if ssaved:
                subs_out.append({"fid": ssaved, "name": sname,
                                 "ext": _ext(sname) or ".srt"})
    except Exception:
        subs_out = []

    # 先看缓存目录里有没有同样一份，有就直接用，不再重复转存
    name_t = target.get("file_name") or ""
    size_t = target.get("size")
    pdir = ensure_cache_dir()
    saved_fid = find_existing(name_t, size_t, pdir)

    # 再查「历史转存记录」：夸克重名会改名，光靠 find_existing 必失配
    if not saved_fid:
        try:
            from . import store
            import hashlib as _hl
            key = "saved:" + _hl.md5(
                ("%s|%s|%s" % (pwd_id, name_t, size_t)).encode("utf-8")).hexdigest()
            hit = store.get_search(key, 60 * 60 * 24 * 30)
            old_fid = (hit or {}).get("fid")
            if old_fid:
                u, _e = direct_url(old_fid)
                if u:
                    saved_fid = old_fid
        except Exception:
            saved_fid = None

    reused = bool(saved_fid)

    if not saved_fid:
        saved_fid, err = _save(pwd_id, st, [target], to_pdir=pdir or "0")
        if not saved_fid:
            return {"ok": False, "error": "转存失败：%s" % (err or "")}
        # 记录：这份分享的这个文件已经转存成哪个 fid
        try:
            from . import store
            import hashlib as _hl
            key = "saved:" + _hl.md5(
                ("%s|%s|%s" % (pwd_id, name_t, size_t)).encode("utf-8")).hexdigest()
            store.put_search(key, name_t, {"fid": saved_fid})
        except Exception:
            pass

    url, err2 = direct_url(saved_fid)
    if not url:
        return {"ok": False, "fid": saved_fid, "error": "取直链失败：%s" % (err2 or "")}

    name = target.get("file_name") or ""
    ext = _ext(name)
    # 一律交给浏览器原生播；只有公认解不了的容器才提示
    unsupported = ext in UNSUPPORTED_EXT
    return {
        "ok": True,
        "fid": saved_fid,
        "stream": "/api/stream?fid=" + saved_fid,
        "name": name,
        "size": target.get("size"),
        "direct": url,
        "ext": ext,
        "reused": reused,
        "playable": not unsupported,
        "native": True,
        "audio_info": _audio_of(name),
        "video_info": _video_of(saved_fid),
        "ep_index": ep_index if ep_index is not None else 0,
        "episodes": [
            {"i": e["i"], "ep": e.get("ep"), "name": e["name"], "size": e["size"]}
            for e in episodes
        ] if len(episodes) > 1 else [],
        "groups": _build_groups(_groups, vsorted),
        "ep_count": len(episodes),
        "subs": subs_out,
        "warn": ("%s 容器浏览器通常解不了，可能需要外部播放器"
                 % (ext or "未知格式")) if unsupported else None,
    }
