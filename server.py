"""
Flixer 骨架服务
阶段目标：把「搜索 → 详情 → 剧集列表（免转存）」跑通。
播放相关（夸克转码 / 原始直链 / ffmpeg remux）留到下一阶段。
"""
import json
import os
import re
import sys
import time
import traceback

import requests
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import search as S
from services import quark as Q
from services import home as H
from services import dual as D
from services import sse as SSE
from services import quark_auth as QA
from services import quark_login as QL
from services import play as PL
from services import hls as HL
from services import filter as FL
from services import browse as BW
from services import recommend as RC

ROOT = os.path.dirname(os.path.abspath(__file__))
import threading


def _keepalive_loop():
    """
    后台静默保活：每 6 小时用现有 cookie 访问一次夸克，
    服务端下发的续期 cookie 会被自动回写，尽量拖长登录有效期。
    """
    time.sleep(20)                      # 等启动稳定
    while True:
        try:
            if QA.load_cookie():
                r = QA.touch()
                if r.get("ok"):
                    print("[keepalive] quark session refreshed")
                else:
                    print("[keepalive] quark session invalid:", r.get("error"))
        except Exception as e:
            print("[keepalive] error:", str(e)[:80])
        time.sleep(6 * 3600)


# 转码拉流用的连接池：复用 TCP/TLS，避免每次 Range 都重新握手
_STREAM_POOL = requests.Session()
_STREAM_POOL.trust_env = False
_STREAM_POOL.proxies = {"http": None, "https": None}
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=8, pool_maxsize=16, max_retries=0, pool_block=False)
_STREAM_POOL.mount("https://", _adapter)
_STREAM_POOL.mount("http://", _adapter)

app = Flask(__name__, static_folder=os.path.join(ROOT, "static"), static_url_path="")


def ok(data):
    return jsonify({"ok": True, "data": data})


def err(msg, code=400, **extra):
    body = {"ok": False, "error": msg}
    body.update(extra)
    return jsonify(body), code


@app.route("/")
def index():
    idx = os.path.join(ROOT, "static", "index.html")
    if os.path.exists(idx):
        resp = send_from_directory(os.path.join(ROOT, "static"), "index.html")
    # 首页永不缓存：否则改了代码看到的还是旧页面（今天踩过好几次）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
    return "<h1>Flixer</h1><p>UI 还没建。试试 <a href='/api/search?q=奥本海默'>/api/search?q=奥本海默</a>"


@app.route("/api/health")
def health():
    return ok({
        "service": "flixer",
        "version": "0.1.0-stage1",
        "cache": S.cache_stats(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })


@app.route("/api/search/stream")
def api_search_stream():
    """
    流式搜索（SSE）
    阶段 1：showpaw —— 快，先让用户看到东西
    阶段 2：PanSou  —— 慢，量大，陆续补进来
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return err("缺少关键词", 400)

    def feed():
        seen = set()

        # ---------- 阶段 1：showpaw（通过 search.py） ----------
        try:
            sp = S.search(q, want_tv=None, verify=False, max_wait=40) or {}
        except Exception as e:
            sp = {}
            yield {"type": "warn", "message": "showpaw: %s" % str(e)[:80]}

        tm = sp.get("tmdb") or {}
        if tm:
            yield {"type": "meta", "tmdb": tm}
        items = sp.get("resources") or []

        for r in items:
            if not isinstance(r, dict):
                continue
            if (r.get("pan_type") or "") not in ("quark", "magnet", "ed2k"):
                continue
            key = (r.get("pan_url") or "") + "|" + (r.get("pan_type") or "")
            if key in seen:
                continue
            seen.add(key)
            yield {"type": "item", "phase": "fast", "item": r}

        yield {"type": "phase", "phase": "fast_done"}

        # ---------- 阶段 2：PanSou ----------
        try:
            ps = D.pansou_search(q, timeout=90) or []
        except Exception as e:
            ps = []
            yield {"type": "warn", "message": "PanSou: %s" % str(e)[:80]}

        for r in ps:
            if not isinstance(r, dict):
                continue
            if (r.get("pan_type") or "") not in ("quark", "magnet", "ed2k"):
                continue
            key = (r.get("pan_url") or "") + "|" + (r.get("pan_type") or "")
            if key in seen:
                continue
            seen.add(key)
            yield {"type": "item", "phase": "slow", "item": r}

    return SSE.make_stream(feed)


def _find_potplayer():
    """找本机 PotPlayer，找不到返回 None"""
    import os as _os
    env = _os.environ.get("POTPLAYER_PATH")
    if env and _os.path.exists(env):
        return env
    cands = [
        r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini.exe",
        r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
        r"C:\Program Files\PotPlayer\PotPlayerMini64.exe",
    ]
    for p in cands:
        if _os.path.exists(p):
            return p
    return None


@app.route("/api/player/probe")
def api_player_probe():
    pot = _find_potplayer()
    mpv = _find_mpv()
    return ok({
        "mpv": bool(mpv), "mpv_path": mpv or "",
        "potplayer": bool(pot), "potplayer_path": pot or "",
        "preferred": "mpv" if mpv else ("potplayer" if pot else ""),
    })


def _find_mpv():
    """找本机 mpv（优先项目自带绿色版）"""
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    cands = [
        _os.path.join(base, "bin", "mpv", "mpv.exe"),
        r"C:\Program Files\mpv\mpv.exe",
    ]
    env = _os.environ.get("MPV_PATH")
    if env:
        cands.insert(0, env)
    for p in cands:
        if _os.path.exists(p):
            return p
    return None


@app.route("/api/mpv/fit", methods=["POST", "GET"])
def api_mpv_fit():
    """把 mpv 窗口摆到指定矩形（起播后再校正一次，防 mpv 自己改小）"""
    from services import mpvctl as MPV
    def _n(k, d=0):
        try:
            return int(float(request.args.get(k) or d))
        except Exception:
            return d
    x, y = _n("x"), _n("y")
    w, h = _n("w", 900), _n("h", 500)
    ok = MPV.fit(x, y, w, h)
    return ok({"fitted": bool(ok), "rect": [x, y, w, h]})


@app.route("/api/mpv/play", methods=["POST", "GET"])
def api_mpv_play():
    """用 mpv 播放（网页当遥控器）"""
    from services import mpvctl as MPV
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)
    if not MPV.available():
        return jsonify({"ok": False, "error": "未找到 mpv", "code": "no_mpv"})

    url, e = PL.direct_url(fid)
    if not url:
        return err("文件不可用: %s" % (e or ""), 404)

    title = (request.args.get("title") or "").strip() or "Flixer"

    def _num(k, d=0):
        try:
            return int(float(request.args.get(k) or d))
        except Exception:
            return d

    x, y = _num("x"), _num("y")
    w, h = _num("w", 1280), _num("h", 720)
    if w < 320 or h < 200:
        w, h, x, y = 1280, 720, 0, 0

    stream = "http://127.0.0.1:7100/api/stream?fid=" + fid
    try:
        r = MPV.play(stream, title, x, y, w, h, fid)
    except Exception as ex:
        return err("启动 mpv 失败: %s" % ex, 500)
    return ok({"playing": True, "fid": fid, "title": title,
               "rect": [x, y, w, h], "reply": r})


@app.route("/api/mpv/status")
def api_mpv_status():
    from services import mpvctl as MPV
    return ok(MPV.status())


@app.route("/api/mpv/cmd", methods=["POST", "GET"])
def api_mpv_cmd():
    """name=pause|toggle_pause|seek|volume|mute  value=..."""
    from services import mpvctl as MPV
    name = (request.args.get("name") or "").strip()
    raw = request.args.get("value")
    val = None
    if raw is not None:
        low = str(raw).lower()
        if low in ("true", "1"):
            val = True
        elif low in ("false", "0"):
            val = False
        else:
            try:
                val = float(raw)
            except Exception:
                val = raw
    try:
        r = MPV.cmd(name, val)
    except Exception as ex:
        return err("命令失败: %s" % ex, 500)
    return ok({"cmd": name, "reply": r, "status": MPV.status()})


@app.route("/api/mpv/quit", methods=["POST", "GET"])
def api_mpv_quit():
    from services import mpvctl as MPV
    return ok(MPV.quit_player())


@app.route("/api/open/mpv", methods=["POST", "GET"])
def api_open_mpv():
    """
    用 mpv 打开指定文件，无边框贴到网页播放器区域
    ?fid=xxx&x=&y=&w=&h=&title=
    """
    import subprocess as _sp
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)
    exe = _find_mpv()
    if not exe:
        return jsonify({"ok": False, "error": "未找到 mpv", "code": "no_mpv"})

    try:
        u, e = PL.direct_url(fid)
        if not u:
            return err("文件不可用: %s" % (e or ""), 404)
    except Exception as ex:
        return err("文件不可用: %s" % ex, 404)

    url = "http://127.0.0.1:7100/api/stream?fid=" + fid
    title = (request.args.get("title") or "").strip() or "Flixer"

    def _num(k, d=0):
        try:
            return int(float(request.args.get(k) or d))
        except Exception:
            return d

    x, y = _num("x"), _num("y")
    w, h = _num("w"), _num("h")
    if w < 320 or h < 200:
        w, h = 1280, 720
        x = y = 0

    # 复用带 IPC 的实例，避免出现两个窗口
    from services import mpvctl as MPV
    try:
        MPV.play(url, title, max(0, x), max(0, y), w, h, fid)
    except Exception as ex:
        return err("启动 mpv 失败: %s" % ex, 500)
    return ok({"opened": True, "player": exe, "url": url,
               "rect": [x, y, w, h]})


@app.route("/api/open/external", methods=["POST", "GET"])
def api_open_external():
    """
    用本机 PotPlayer 播放指定文件（杜比原声、秒拖）
    ?fid=xxx&title=片名
    """
    import subprocess as _sp
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)

    exe = _find_potplayer()
    if not exe:
        return jsonify({"ok": False, "error": "未找到 PotPlayer", "code": "no_player"})

    # 确认 fid 有效（能取到直链）
    try:
        u, e = PL.direct_url(fid)
        if not u:
            return err("文件不可用: %s" % (e or ""), 404)
    except Exception as ex:
        return err("文件不可用: %s" % ex, 404)

    url = "http://127.0.0.1:7100/api/stream?fid=" + fid
    title = (request.args.get("title") or "").strip()
    args = [exe, url]
    if title:
        # PotPlayer 支持 /title= 指定窗口标题
        args = [exe, url, "/title=" + title[:60]]
    try:
        _sp.Popen(args, close_fds=True,
                  creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception as ex:
        return err("启动 PotPlayer 失败: %s" % ex, 500)

    return ok({"opened": True, "player": exe, "url": url})


@app.route("/api/audio/live")
def api_audio_live():
    """
    杜比音轨实时转 AAC（流式）
    ?fid=xxx&start=0  → 返回可直接喂给 <audio> 的地址
    """
    from services import aac as AAC
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)
    if not AAC.available():
        return jsonify({"ok": False, "error": "缺少 ffmpeg"})
    try:
        start = max(0, int(float(request.args.get("start") or 0)))
    except Exception:
        start = 0
    return ok({
        "url": "/api/audio/live/file?fid=%s&start=%d" % (fid, start),
        "start": start,
    })


@app.route("/api/audio/live/file")
def api_audio_live_file():
    """
    实时转码结果按 Range 提供（可边转边播、可拖动）
    """
    from services import aac as AAC
    import time as _t
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)
    try:
        start = max(0, int(float(request.args.get("start") or 0)))
    except Exception:
        start = 0

    if not AAC.start_live(fid, start):
        return err("无法启动转码", 500)

    # 等到文件出现（最多 20s）
    t0 = _t.time()
    while _t.time() - t0 < 60:
        if AAC.live_size(fid, start) > 2048:
            break
        _t.sleep(0.1)

    rng = request.headers.get("Range") or ""
    a, b = 0, None
    if rng.startswith("bytes="):
        part = rng[6:].split("-")
        try:
            a = int(part[0]) if part[0] else 0
            b = int(part[1]) if len(part) > 1 and part[1] else None
        except Exception:
            a, b = 0, None

    # 等待目标区间可读（最多 3s）
    t0 = _t.time()
    while _t.time() - t0 < 3:
        size = AAC.live_size(fid, start)
        if b is None:
            if size > a + 65536:
                break
        elif size > b:
            break
        if AAC.live_done(fid, start):
            break
        _t.sleep(0.1)

    path = AAC.live_path(fid, start)
    if not path:
        return err("转码文件不存在", 503)
    size = AAC.live_size(fid, start)
    if a >= size:
        return err("区间未就绪", 416)

    endpos = size - 1 if b is None else min(b, size - 1)
    length = endpos - a + 1
    if length <= 0:
        return err("区间未就绪", 416)

    if not rng:
        # 跟随式流：文件还在写就继续读，避免播放器以为「到结尾了」
        def gen_follow():
            pos = 0
            idle = 0
            while True:
                size = AAC.live_size(fid, start)
                if pos < size:
                    with open(path, "rb") as f:
                        f.seek(pos)
                        data = f.read(min(262144, size - pos))
                    if data:
                        pos += len(data)
                        idle = 0
                        yield data
                        continue
                if AAC.live_done(fid, start):
                    return
                idle += 1
                if idle > 600:      # 约 60 秒无新增，放弃
                    return
                _t.sleep(0.1)

        resp = Response(gen_follow(), mimetype="audio/mp4")
        resp.headers["Accept-Ranges"] = "none"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def gen():
        with open(path, "rb") as f:
            f.seek(a)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    resp = Response(gen(), status=206, mimetype="audio/mp4")
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Range"] = "bytes %d-%d/%d" % (a, endpos, max(size, endpos + 1))
    resp.headers["Content-Length"] = str(length)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/audio/aac")
def api_audio_aac():
    """
    杜比音轨兼容层：把浏览器解不了的音频转成 AAC（视频不动）

    用法：?fid=xxx&start=0  返回 {ok, ready, url}
      第一次调用开始转码，前端轮询 ready，好了就切换音轨。
    """
    from services import aac as AAC
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)
    try:
        start = max(0, int(request.args.get("start") or 0))
    except Exception:
        start = 0

    if not AAC.available():
        return jsonify({"ok": False, "error": "缺少 ffmpeg"})

    # 已经有缓存直接给
    p = AAC.cached_path(fid, start)
    if p:
        return ok({"ready": True,
                   "url": "/api/audio/aac/file?fid=%s&start=%d" % (fid, start),
                   "size": os.path.getsize(p)})

    try:
        track_idx = max(0, int(request.args.get("track") or 0))
    except Exception:
        track_idx = 0

    url, e = PL.direct_url(fid)
    if not url:
        return jsonify({"ok": False, "error": "取直链失败"})

    out, ready = AAC.start(fid, url, start=start, track_index=track_idx)
    if not out:
        return jsonify({"ok": False, "error": "无法启动转码"})
    if ready:
        return ok({"ready": True,
                   "url": "/api/audio/aac/file?fid=%s&start=%d" % (fid, start)})

    st = AAC.progress(fid, start)
    return ok({"ready": False, "running": st.get("running", True),
               "size": st.get("size", 0)})


@app.route("/api/audio/aac/file")
def api_audio_aac_file():
    """输出转好的 AAC 音频（支持 Range，供 audio 元素拖动）"""
    from services import aac as AAC
    fid = (request.args.get("fid") or "").strip()
    try:
        start = max(0, int(request.args.get("start") or 0))
    except Exception:
        start = 0
    p = AAC.cached_path(fid, start)
    if not p:
        return err("音频还没准备好", 404)

    size = os.path.getsize(p)
    rng = request.headers.get("Range")
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else size - 1
            b = min(b, size - 1)
            with open(p, "rb") as f:
                f.seek(a)
                data = f.read(b - a + 1)
            resp = Response(data, status=206, mimetype="audio/mp4")
            resp.headers["Content-Range"] = "bytes %d-%d/%d" % (a, b, size)
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Content-Length"] = str(len(data))
            return resp
    resp = send_from_directory(os.path.dirname(p), os.path.basename(p),
                               mimetype="audio/mp4")
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/api/audio")
def api_audio():
    """探测音轨编码：MKV 读头部，其他容器用 ffmpeg 兜底，结果缓存"""
    from services import audio as AU
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)

    hit = AU.get_cached(fid)
    if hit and hit.get("codec"):
        return ok(hit)

    url, e = PL.direct_url(fid)
    if not url:
        return jsonify({"ok": False, "error": "取直链失败"})

    hdrs = PL.stream_headers()
    sess = requests.Session()
    sess.trust_env = False
    sess.proxies = {"http": None, "https": None}

    total = 0
    try:
        r = sess.get(url, headers=dict(hdrs, Range="bytes=0-0"), timeout=(10, 30),
                     stream=True)
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            total = int(cr.split("/")[-1])
        r.close()
    except Exception:
        pass

    info = None

    # ① MKV：按 EBML 解析（快，1 秒内）
    if total:
        def fetch(off, length):
            hh = dict(hdrs)
            hh["Range"] = "bytes=%d-%d" % (off, off + length - 1)
            rr = sess.get(url, headers=hh, timeout=(10, 90))
            return rr.content

        try:
            info = AU.probe_mkv_stream(fetch, total)
            if info and not info.get("codec"):
                info = None
        except Exception:
            info = None

    # ② 其它容器（mp4 等）：ffmpeg 读文件头
    if not info:
        # ffmpeg 带不上夸克需要的 Cookie，改让它读本地代理
        try:
            info = AU.probe_ffmpeg("http://127.0.0.1:7100/api/stream?fid=" + fid)
        except Exception:
            info = None

    if not info:
        return jsonify({"ok": False, "error": "音轨探测失败"})

    AU.put_cached(fid, info)
    return ok(info)


@app.route("/api/meta")
def api_meta():
    """只取元数据（豆瓣 suggest，约 0.3 秒），详情页先用它渲染"""
    from services import meta as META
    q = (request.args.get("q") or "").strip()
    if not q:
        return err("缺少参数 q", 400)
    year = (request.args.get("year") or "").strip()
    did = (request.args.get("douban_id") or "").strip()
    wt = request.args.get("type")
    want_tv = True if wt == "tv" else (False if wt == "movie" else None)
    try:
        m = META.quick(q, want_tv, timeout=2.5, year_hint=year, douban_id=did) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]})
    return ok({
        "title": m.get("title") or q,
        "original_title": m.get("originalTitle") or "",
        "year": m.get("year") or 0,
        "poster": m.get("posterUrl") or "",
        "backdrop": m.get("backdropUrl") or "",
        "overview": m.get("overview") or "",
        "vote_average": m.get("voteAverage") or 0,
        "genres": m.get("genres") or [],
        "douban_id": m.get("douban_id") or "",
        "type": m.get("type") or "",
    })


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return err("缺少参数 q")
    wt = request.args.get("type")  # tv | movie
    want_tv = True if wt == "tv" else (False if wt == "movie" else None)
    yr = (request.args.get("year") or "").strip()   # 年份线索，用于同名作品消歧
    dual = request.args.get("dual", "1") != "0"   # 默认双源
    try:
        if dual:
            # ── 快路径：豆瓣元数据（0.1-0.4s）+ PanSou 插件搜片源
            # 不再经过 showpaw（它单次 6-13s 且资源恒为 0）
            try:
                from services import meta as META
                m = META.quick(q, want_tv, timeout=2.5, year_hint=yr)
                if m:
                    is_tv = (m.get("type") == "tv") or (want_tv is True)
                    tm = {
                        "title": m.get("title") or q,
                        "original_title": m.get("originalTitle") or "",
                        "year": m.get("year") or 0,
                        "poster": m.get("posterUrl") or "",
                        "backdrop": m.get("backdropUrl") or "",
                        "overview": m.get("overview") or "",
                        "vote_average": m.get("voteAverage") or 0,
                        "genres": m.get("genres") or [],
                        "douban_id": m.get("douban_id") or "",
                    }
                    merged = D.dual_search(q, tm, is_tv, max_wait=6, year_hint=yr)
                    # 搜不到就换 TMDB 的正式片名 / 原名再试（"一个夜晚和…" vs "…与…"）
                    try:
                        if not (merged.get("resources") or merged.get("unknown_resolution")):
                            _alts = []
                            for _c in (tm.get("title"), tm.get("original_title")):
                                _c = (_c or "").strip()
                                if _c and _c != q and _c not in _alts:
                                    _alts.append(_c)
                            for _c in _alts[:2]:
                                _m2 = D.dual_search(_c, tm, is_tv, max_wait=5, year_hint=yr)
                                if _m2.get("resources") or _m2.get("unknown_resolution"):
                                    merged = _m2
                                    merged["query_used"] = _c
                                    break
                    except Exception:
                        pass
                    out = {
                        "query": q,
                        "tmdb": tm,
                        "is_tv": is_tv,
                        "resources": merged["resources"],
                        "unknown_resolution": merged.get("unknown_resolution") or [],
                        "weak_resources": merged.get("weak") or [],
                        "stats": dict(merged.get("stats") or {}, dual=True, fast_path=True),
                        "elapsed_ms": merged.get("elapsed_ms") or 0,
                        "cached": bool(merged.get("cached")),
                    }
                    return ok(out)
            except Exception as _fe:
                traceback.print_exc()

            # ── 兜底路径：老的 showpaw 流程
            base = S.search(q, want_tv=want_tv, verify=False)
            if base.get("error"):
                return err(base["error"], 502)
            tm = base.get("tmdb") or {}
            is_tv = bool(base.get("is_tv"))
            merged = D.dual_search(q, tm, is_tv, max_wait=8, year_hint=yr)
            out = dict(base)
            out["resources"] = merged["resources"]
            out["unknown_resolution"] = merged.get("unknown_resolution") or []
            out["weak_resources"] = merged["weak"]
            st = dict(base.get("stats") or {})
            st.update(merged["stats"])
            st["dual"] = True
            out["stats"] = st
            out["elapsed_ms"] = (base.get("elapsed_ms") or 0) + merged.get("elapsed_ms", 0)
            out["cached"] = base.get("cached") or merged.get("cached")
            return ok(out)

        data = S.search(q, want_tv=want_tv)
        if data.get("error"):
            return err(data["error"], 502, **{k: v for k, v in data.items() if k != "error"})
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err("搜索异常: %s" % e, 500)


@app.route("/api/source/status")
def api_source_status():
    """两个数据源的健康状态"""
    pansou_ok = D.pansou_available()
    return ok({
        "showpaw": True,
        "pansou": pansou_ok,
        "pansou_url": D.PANSOU,
    })


@app.route("/api/home")
def api_home():
    """首页：豆瓣榜单单 + showpaw 元数据"""
    try:
        maxn = int(request.args.get("n") or 20)
        data = H.build_home(max_per_row=max(6, min(30, maxn)))
        data["hero"] = H.hero_pick(data)
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err("首页加载失败: %s" % e, 500)


@app.route("/api/img")
def api_img():
    """图片代理：豆瓣封面有防盗链，必须服务端带 Referer 转发"""
    u = request.args.get("u") or ""
    if not u.startswith("http"):
        return err("bad url", 400)
    # 只允许图片域名，防开放代理
    allow = ("doubanio.com", "tmdb.org", "douban.com", "image.tmdb.org")
    from urllib.parse import urlparse
    host = (urlparse(u).hostname or "").lower()
    if not any(host.endswith(a) for a in allow):
        return err("domain not allowed", 403)
    try:
        body, ct = H.fetch_image(u)
        resp = Response(body, mimetype=ct)
        # 只有确实拿到图片才允许缓存；失败的响应绝不缓存，
        # 否则浏览器会把一次失败记 24 小时，导致封面长期空白
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception as e:
        resp = err("图片获取失败: %s" % e, 502)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp


@app.route("/api/quark/status")
def api_quark_status():
    try:
        return ok(QA.status())
    except Exception as e:
        return ok({"logged_in": False, "error": str(e)[:100]})


@app.route("/api/quark/qr/start", methods=["POST"])
def api_quark_qr_start():
    """弹出一个真实 Chrome 窗口打开夸克登录页，用户扫码"""
    try:
        return jsonify(QL.start())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:120]})


@app.route("/api/quark/qr/poll")
def api_quark_qr_poll():
    """前端轮询：登录成功会自动保存 cookie 并关掉窗口"""
    try:
        return jsonify(QL.poll())
    except Exception as e:
        return jsonify({"state": "error", "error": str(e)[:120]})


@app.route("/api/quark/qr/cancel", methods=["POST"])
def api_quark_qr_cancel():
    try:
        QL.stop()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]})


@app.route("/api/quark/login", methods=["POST"])
def api_quark_login():
    body = request.get_json(silent=True) or {}
    raw = body.get("cookie") or ""
    try:
        r = QA.login_with_cookie(raw)
        if not r.get("ok"):
            return err(r.get("error") or "登录失败", 400)
        # 登录成功后顺手验证能列目录
        probe = QA.list_my_root()
        return ok({"nick": r.get("nick"), "vip": r.get("vip"),
                   "files": probe.get("count", 0)})
    except Exception as e:
        traceback.print_exc()
        return err("登录异常: %s" % e, 500)


@app.route("/api/quark/logout", methods=["POST"])
def api_quark_logout():
    QA.clear_cookie()
    return ok({"logged_in": False})


@app.route("/api/hls/start", methods=["POST", "GET"])
def api_hls_start():
    """为一个 fid 启动 HLS 转码（MKV 也能播）"""
    fid = request.args.get("fid") or ""
    if not fid:
        return jsonify({"ok": False, "error": "缺少 fid"})
    if not QA.load_cookie():
        return jsonify({"ok": False, "error": "需要先登录夸克", "need_login": True})
    try:
        want_copy = request.args.get("copy") == "1"
        r = HL.start(fid, force_copy=want_copy)
        return jsonify(r)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:140]})


@app.route("/api/hls/codec")
def api_hls_codec():
    """告诉前端这个文件是什么编码，好决定要不要真的转码"""
    fid = request.args.get("fid") or ""
    if not fid:
        return jsonify({"ok": False, "error": "缺少 fid"})
    url, e = PL.direct_url(fid)
    if not url:
        return jsonify({"ok": False, "error": e or "拿不到直链"})
    h = PL.stream_headers()
    hdr = "User-Agent: %s\r\nReferer: %s\r\nCookie: %s\r\n" % (
        h["User-Agent"], h["Referer"], h["Cookie"])
    try:
        vcodec, acodec = HL.probe_codec(url, hdr)
        return jsonify({"ok": True, "vcodec": vcodec, "acodec": acodec})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:100]})


@app.route("/api/hls/status")
def api_hls_status():
    return jsonify(HL.status(request.args.get("task") or ""))


@app.route("/api/hls/<task>/index.m3u8")
def api_hls_playlist(task):
    p = HL.playlist_path(task)
    if not p:
        return err("播放列表还没生成", 404)
    with open(p, "rb") as f:
        body = f.read()
    resp = Response(body, mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/hls/<task>/<name>")
def api_hls_segment(task, name):
    p = HL.segment_path(task, name)
    if not p:
        return err("分片不存在", 404)
    try:
        with open(p, "rb") as f:
            body = f.read()
    except Exception:
        return err("读取分片失败", 500)
    resp = Response(body, mimetype="video/mp2t")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/stream")
def api_stream():
    """
    夸克文件流代理（带断点续传）

    ffmpeg 和浏览器都用这个接口。夸克会在 32MB 处掐断连接，
    所以这里自己负责重连，对外表现为一条完整的流。
    """
    fid = request.args.get("fid") or ""
    if not fid:
        return err("缺少 fid", 400)
    if not QA.load_cookie():
        return err("需要先登录夸克", 401)

    url, e = PL.direct_url(fid)
    if not url:
        return err("取直链失败：%s" % (e or ""), 502)

    hdrs = PL.stream_headers()
    rng = request.headers.get("Range")

    # 解析请求的起始/结束
    start_byte = 0
    end_byte = None
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start_byte = int(m.group(1))
            if m.group(2):
                end_byte = int(m.group(2))

    # 先摸一下总长度（一次 HEAD 或空 Range）
    total = None
    try:
        probe = _STREAM_POOL.get(url, headers=dict(hdrs, **{"Range": "bytes=0-0"}),
                                 timeout=(10, 30))
        cr = probe.headers.get("Content-Range") or ""
        if "/" in cr:
            total = int(cr.split("/")[-1])
        probe.close()
    except Exception:
        pass

    # 夸克对「开放式 Range（bytes=X-）」限速到 0.1MB/s，
    # 有界分块（bytes=X-Y）能跑 4.5MB/s，所以内部一律按 8MB 有界块取。
    SEG = 8 * 1024 * 1024

    def gen():
        """生产者线程预取下一段，消费端零空档"""
        import queue as _q
        import threading as _th

        stop = _th.Event()
        q = _q.Queue(maxsize=24)          # 24 × 256KB ≈ 6MB 缓冲

        def fetch_seg(p, seg_end, use_url):
            """取一段，成功返回 (response, url)；失败返回 (None, url)"""
            h = dict(hdrs)
            h["Range"] = "bytes=%d-%d" % (p, seg_end)
            try:
                up = _STREAM_POOL.get(use_url, headers=h, stream=True, timeout=(10, 90))
            except Exception:
                return None, use_url, "conn"
            if up.status_code in (403, 412):
                up.close()
                return None, use_url, "expired"
            if up.status_code not in (200, 206):
                up.close()
                return None, use_url, "http%d" % up.status_code
            return up, use_url, None

        def producer():
            pos = start_byte
            url_cur = url
            refreshed = False
            try:
                while not stop.is_set():
                    if end_byte is not None and pos > end_byte:
                        break
                    if total is not None and pos >= total:
                        break
                    if end_byte is not None:
                        seg_end = min(pos + SEG - 1, end_byte)
                    elif total is not None:
                        seg_end = min(pos + SEG - 1, total - 1)
                    else:
                        seg_end = pos + SEG - 1

                    up, url_cur, err = fetch_seg(pos, seg_end, url_cur)
                    if up is None and not refreshed:
                        refreshed = True
                        try:
                            u2, _ = PL.direct_url(fid, force=True)
                            if u2:
                                url_cur = u2
                                continue
                        except Exception:
                            pass
                        break
                    if up is None:
                        break

                    got_any = False
                    try:
                        for ch in up.iter_content(chunk_size=256 * 1024):
                            if stop.is_set():
                                break
                            if not ch:
                                continue
                            got_any = True
                            pos += len(ch)
                            while True:
                                if stop.is_set():
                                    return
                                try:
                                    q.put(ch, timeout=0.5)
                                    break
                                except _q.Full:
                                    continue
                            if pos > seg_end:
                                break
                    except Exception:
                        pass
                    finally:
                        try:
                            up.close()
                        except Exception:
                            pass
                    if not got_any:
                        break
            finally:
                try:
                    q.put(None, timeout=2)
                except Exception:
                    pass

        th = _th.Thread(target=producer, daemon=True)
        th.start()
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield item
        finally:
            stop.set()

    code = 206 if (rng and total) else 200
    resp = Response(stream_with_context(gen()), status=code)
    resp.headers["Content-Type"] = "video/mp4"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    if total:
        resp.headers["Content-Length"] = str(
            (total - start_byte) if end_byte is None else (end_byte - start_byte + 1))
        if code == 206:
            resp.headers["Content-Range"] = "bytes %d-%d/%d" % (
                start_byte, (end_byte if end_byte is not None else total - 1), total)
    return resp


@app.route("/api/sub")
def api_sub():
    """
    取字幕并转成 WebVTT（浏览器只认 vtt）

    参数：fid（自己网盘里的字幕文件）、name（文件名，用来判断格式）
    """
    fid = (request.args.get("fid") or "").strip()
    name = (request.args.get("name") or "").strip()
    if not fid:
        return err("缺少 fid", 400)

    from services import subs as SUB

    # 缓存命中
    cached = SUB.cache_get(fid)
    if cached:
        return Response(cached, mimetype="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=86400"})

    url, e = PL.direct_url(fid)
    if not url:
        return err("取字幕直链失败：%s" % (e or ""), 502)

    try:
        h = PL.stream_headers()
        r = requests.get(url, headers=h, timeout=(10, 30))
        if r.status_code not in (200, 206):
            return err("下载字幕失败：HTTP %s" % r.status_code, 502)
        raw = r.content[: 8 * 1024 * 1024]
    except Exception as ex:
        return err("下载字幕失败：%s" % str(ex)[:100], 502)

    text = SUB.decode_bytes(raw)
    vtt = SUB.to_vtt(text, name)
    if not (vtt or "").strip().startswith("WEBVTT"):
        return err("这个字幕解析不出内容", 400)

    SUB.cache_put(fid, vtt)
    return Response(vtt, mimetype="text/vtt; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- 内嵌字幕

_PROBE_CACHE = {}


def _probe_embedded(fid, force=False):
    """探测文件里的字幕轨（只读头部，约 1-3 秒）"""
    import time as _t
    from services import mkv as _MKV
    hit = _PROBE_CACHE.get(fid)
    if hit and not force and _t.time() - hit[0] < 3600:
        return hit[1]

    url, e = PL.direct_url(fid)
    if not url:
        return {"ok": False, "error": "取直链失败：%s" % (e or "")}

    sess = requests.Session()
    sess.trust_env = False
    sess.proxies = {"http": None, "https": None}
    # 必须带登录 Cookie，否则夸克返回防盗链 HTML（412/200+DOCTYPE）
    headers = PL.stream_headers()

    # 总长度（夸克对 bytes=0-0 有时不回 Content-Range，要兜底）
    total = 0
    try:
        r = sess.get(url, headers=dict(headers, Range="bytes=0-0"), timeout=(15, 60))
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            total = int(cr.split("/")[-1])
        elif r.headers.get("Content-Length"):
            total = int(r.headers["Content-Length"])
        r.close()
    except Exception:
        pass
    if not total:
        try:
            r = sess.head(url, headers=headers, timeout=(15, 60))
            total = int(r.headers.get("Content-Length") or 0)
            r.close()
        except Exception:
            pass
    if not total:
        return {"ok": False, "error": "取文件长度失败"}

    def fetch(off, length):
        h = dict(headers)
        h["Range"] = "bytes=%d-%d" % (off, off + length - 1)
        rr = sess.get(url, headers=h, timeout=(15, 120))
        return rr.content

    rd = _MKV.RangeReader(fetch, total, window=2 * 1024 * 1024)
    try:
        info = _MKV.probe(rd)
    except Exception as ex:
        return {"ok": False, "error": "解析失败：%s" % str(ex)[:80]}

    if not info.get("ok"):
        out = {"ok": False, "error": info.get("error") or "不是 MKV",
               "total": total, "magic": info.get("magic")}
    else:
        subs = info.get("subtitle_tracks") or []
        out = {
            "ok": True,
            "total": total,
            "scale": info.get("scale") or 1000000,
            "duration": info.get("duration", 0),
            "tracks": [{"number": s["number"], "codec": s["codec"],
                        "name": s["name"], "lang": s["lang"], "kind": s["kind"]}
                       for s in subs],
            "text_tracks": [s for s in subs if s.get("kind") == "text"],
            "url": url,
        }
    _PROBE_CACHE[fid] = (_t.time(), out)
    return out


@app.route("/api/subtitle/tracks")
def api_subtitle_tracks():
    fid = (request.args.get("fid") or "").strip()
    if not fid:
        return err("缺少 fid", 400)
    info = _probe_embedded(fid, force=request.args.get("force") == "1")
    if not info.get("ok"):
        return jsonify({"ok": False, "error": info.get("error"),
                        "magic": info.get("magic")})
    return ok({
        "total": info.get("total"),
        "duration": info.get("duration"),
        "tracks": info.get("tracks") or [],
        "text_count": len(info.get("text_tracks") or []),
    })


@app.route("/api/subtitle/extract", methods=["POST", "GET"])
def api_subtitle_extract():
    from services import subjob
    fid = (request.args.get("fid") or "").strip()
    tr = request.args.get("track")
    if not fid or tr is None:
        return err("缺少 fid / track", 400)
    try:
        track = int(tr)
    except Exception:
        return err("track 必须是数字", 400)

    info = _probe_embedded(fid)
    if not info.get("ok"):
        return jsonify({"ok": False, "error": info.get("error")})

    if subjob.cached(fid, track):
        return ok(subjob.status(fid, track))

    st = subjob.start(fid, track, info["total"], info["url"],
                      info.get("scale") or 1000000)
    return ok(st)


@app.route("/api/subtitle/status")
def api_subtitle_status():
    from services import subjob
    fid = (request.args.get("fid") or "").strip()
    tr = request.args.get("track")
    if not fid or tr is None:
        return err("缺少 fid / track", 400)
    return ok(subjob.status(fid, int(tr)))


@app.route("/api/subtitle/cancel", methods=["POST", "GET"])
def api_subtitle_cancel():
    from services import subjob
    fid = (request.args.get("fid") or "").strip()
    tr = request.args.get("track")
    if not fid or tr is None:
        return err("缺少 fid / track", 400)
    subjob.cancel(fid, int(tr))
    return ok({"cancelled": True})


@app.route("/api/subtitle/vtt")
def api_subtitle_vtt():
    from services import subjob
    fid = (request.args.get("fid") or "").strip()
    tr = request.args.get("track")
    if not fid or tr is None:
        return err("缺少 fid / track", 400)
    track = int(tr)
    p = subjob.cached(fid, track)
    if p:
        txt = io.open(p, encoding="utf-8").read()
        return Response(txt, mimetype="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})
    # 抽取中：先给已有部分（合法 VTT，能立刻看）
    txt = subjob.read_partial(fid, track)
    if txt:
        return Response(txt, mimetype="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-store"})
    return err("字幕还没抽取完成", 404)


# ---------------------------------------------------------------- 在线字幕

@app.route("/api/subtitle/online")
def api_sub_online():
    """
    按片名搜外挂中文字幕（assrt）。
    返回候选列表，前端按文件名挑最匹配的一条自动下载。
    """
    from services import subsrc
    title = (request.args.get("title") or "").strip()
    if not title:
        return err("缺少 title", 400)
    try:
        items = subsrc.search(title, limit=20)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]})
    return ok({"items": items, "extractor": subsrc.has_extractor(),
               "count": len(items)})


@app.route("/api/subtitle/online/vtt")
def api_sub_online_vtt():
    """下载指定字幕并转成 WebVTT（结果会缓存）"""
    from services import subsrc
    url = (request.args.get("url") or "").strip()
    if not url or "/xml/sub/" not in url:
        return err("url 不合法", 400)
    try:
        res = subsrc.fetch(url)
    except Exception as e:
        return err("抓取失败：%s" % str(e)[:120], 502)
    if not res.get("ok"):
        return err(res.get("error") or "抓取失败", 404)
    return Response(res["vtt"], mimetype="text/vtt; charset=utf-8",
                    headers={"Cache-Control": "no-cache",
                             "X-Sub-Name": (res.get("name") or "")[:80],
                             "X-Sub-Cached": "1" if res.get("cached") else "0"})


@app.route("/api/play")
def api_play():
    """把一个夸克分享链接解析成可播放直链（会转存到自己网盘）"""
    share = request.args.get("share") or request.args.get("u") or ""
    title = request.args.get("title") or ""
    pwd = request.args.get("pwd") or ""
    ep = request.args.get("ep")          # 第几集（0 基），不传则自动
    if not share:
        return err("缺少 share 参数", 400)
    try:
        r = PL.resolve(share, pwd, title, ep=ep)
        return jsonify({"ok": True, "data": r})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:160]})


@app.route("/api/similar")
def api_similar():
    title = (request.args.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "缺少 title"})
    genres = [x.strip() for x in (request.args.get("genres") or "").split(",") if x.strip()]
    media = (request.args.get("media") or "movie").strip()
    try:
        items = RC.similar(title, genres, media=media if media in ("movie","tv") else "movie")
        return jsonify({"ok": True, "data": {"items": items}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:140]})


@app.route("/api/daily")
def api_daily():
    try:
        items = RC.daily()
        return jsonify({"ok": True, "data": {"items": items, "date": RC.daily_date()}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:140]})


@app.route("/api/browse")
def api_browse():
    """电影 / 剧集 的按题材分行内容"""
    media = (request.args.get("media") or "movie").strip()
    if media not in ("movie", "tv"):
        media = "movie"
    try:
        r = BW.browse(media=media)
        return jsonify({"ok": True, "data": r})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:140]})


@app.route("/api/filter/options")
def api_filter_options():
    return jsonify({"ok": True, "data": FL.options()})


@app.route("/api/filter")
def api_filter():
    """按 类型/地区/年份 筛选豆瓣条目"""
    def split(k):
        v = request.args.get(k) or ""
        return [x.strip() for x in v.split(",") if x.strip()]

    genres = split("genres")
    regions = split("regions")
    year = (request.args.get("year") or "").strip()
    media = (request.args.get("media") or "movie").strip()
    try:
        start = int(request.args.get("start") or 0)
    except Exception:
        start = 0
    try:
        sort = (request.args.get("sort") or "U").strip().upper()
        if sort not in ("U", "S", "R"):
            sort = "U"
        r = FL.search(genres=genres, regions=regions, year=year or None,
                      media=media, start=start, limit=60, sort=sort)
        return jsonify({"ok": True, "data": r})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:140]})


@app.route("/api/check-share")
def api_check_share():
    """
    判断一个网盘分享链接是否还有效（前端用它给片源打角标）

    返回 {ok, alive, code, error, cached}
      alive=False 且 code=SHARE_DEAD     → 分享已失效
      alive=False 且 code=NEED_PASSCODE  → 需要提取码
      alive=True                         → 可用
    """
    share = (request.args.get("share") or "").strip()
    pwd = (request.args.get("pwd") or "").strip()
    if not share:
        return jsonify({"ok": False, "error": "缺少参数 share"})
    try:
        r = PL.check_share_cached(share, pwd)
        err = r.get("error") or ""
        code = r.get("code") or ""
        alive = bool(r.get("alive"))
        # 需要提取码：分享本身还在，只是缺密码
        need_pwd = (code == "NEED_PASSCODE") or ("提取码" in err)
        if not code:
            code = "NEED_PASSCODE" if need_pwd else ("" if alive else "SHARE_DEAD")
        return jsonify({
            "ok": True,
            "alive": alive,
            "need_pwd": need_pwd,
            "code": code,
            "error": err,
            "cached": bool(r.get("cached")),
            "from_disk": bool(r.get("from_disk")),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]}), 500


@app.route("/api/share")
def api_share():
    """给一个夸克分享链接，返回剧集/电影结构（免登录）"""
    url = (request.args.get("url") or "").strip()
    pwd = (request.args.get("pwd") or "").strip()
    if not url:
        return err("缺少参数 url")
    try:
        data = Q.inspect_share(url, pwd)
        if not data.get("ok"):
            return err(data.get("message") or "解析失败", 400, **data)
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err("解析异常: %s" % e, 500)


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    import sqlite3
    try:
        with S._lock:
            conn = S._db()
            n = conn.execute("DELETE FROM kv").rowcount
            conn.commit()
            conn.close()
        return ok({"cleared": n})
    except Exception as e:
        return err(str(e), 500)



def _prewarm_mpv():
    """后台预热 mpv，让第一次点播放也接近秒开"""
    try:
        import time as _t
        _t.sleep(3)
        from services import mpvctl as _MPV
        if _MPV.available():
            _MPV.warm()
    except Exception:
        pass


try:
    import threading as _th
    _th.Thread(target=_prewarm_mpv, daemon=True).start()
except Exception:
    pass

if __name__ == "__main__":
    port = int(os.environ.get("FLIXER_PORT", "7100"))
    print("Flixer stage2 -> http://127.0.0.1:%d" % port)
    threading.Thread(target=_keepalive_loop, daemon=True).start()

    # 存储：建表 + 清掉 7 天前的旧记录
    try:
        from services import store as _ST
        if _ST.init():
            n = _ST.purge(7)
            print("[store] SQLite 就绪，清理过期记录 %d 条" % n)
        else:
            print("[store] SQLite 不可用，缓存退化为内存")
    except Exception as _e:
        print("[store] 初始化异常:", str(_e)[:100])

    # 预热：让第一个访客不用等（首页 / 榜单 / 每日推荐）
    def _prewarm():
        try:
            time.sleep(2)
            from services import home as _H
            _H.build_home(max_per_row=20)
            print("[warm] 首页就绪")
        except Exception as _e:
            print("[warm] 首页失败", str(_e)[:80])
        try:
            from services import browse as _BW
            _BW.browse(media="movie")
            print("[warm] 电影榜单就绪")
        except Exception as _e:
            print("[warm] 电影榜单失败", str(_e)[:80])
        try:
            from services import browse as _BW2
            _BW2.browse(media="tv")
            print("[warm] 剧集榜单就绪")
        except Exception as _e:
            print("[warm] 剧集榜单失败", str(_e)[:80])
        try:
            from services import recommend as _RC
            _RC.daily()
            print("[warm] 每日推荐就绪")
        except Exception as _e:
            print("[warm] 每日推荐失败", str(_e)[:80])

    try:
        threading.Thread(target=_prewarm, daemon=True).start()
        print("[warm] 预热线程已启动")
    except Exception:
        pass

    # 优先用 waitress：多线程 + 更好的吞吐，播放大码率视频不卡
    # 退化到 werkzeug 只作为兜底
    try:
        from waitress import serve
        print("Flixer stage2 -> http://127.0.0.1:%d  (waitress)" % port)
        serve(app, host="127.0.0.1", port=port, threads=24,
              channel_timeout=120, cleanup_interval=30,
              connection_limit=200, backlog=200, ident="flixer")
    except Exception as e:
        print("[waitress 不可用，退回 werkzeug]", e)
        from werkzeug.serving import make_server
        srv = make_server("127.0.0.1", port, app, threaded=True)
        srv.serve_forever()
