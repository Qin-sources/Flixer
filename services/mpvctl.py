# -*- coding: utf-8 -*-
"""mpv 进程与 IPC 控制（同步、轻量、带全屏）"""
import json
import os
import subprocess
import threading
import time

PIPE_NAME = r"\\.\pipe\flixer-mpv"
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MPV = os.path.join(_BASE, "bin", "mpv", "mpv.exe")

_proc = None
_pipe = None
_req = {"id": 0}
_io_lock = threading.RLock()
_fid = ""
_last_seen = {"t": 0.0}
_wd_started = {"v": False}
_cache = {"duration": 0.0, "volume": 100.0, "mute": False, "pause": False,
          "video_w": 0, "video_h": 0,
          "geometry": "1280x720+0+0", "fullscreen": False}


def available():
    return os.path.exists(_MPV)


def _open_pipe(timeout=8.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            return open(PIPE_NAME, "r+b", buffering=0)
        except Exception as e:
            last = e
            time.sleep(0.12)
    raise IOError("管道未就绪: %s" % last)


def _readline(timeout):
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ch = _pipe.read(1)
        except Exception:
            return None
        if not ch:
            time.sleep(0.005)
            continue
        if ch == b"\n":
            return buf.decode("utf-8", "replace")
        buf += ch
        if len(buf) > 1 << 20:
            return None
    return None


def _send(payload, timeout=2.0, want_reply=True):
    """发命令（全局串行，避免多线程抢同一个管道句柄）"""
    with _io_lock:
        return _send_locked(payload, timeout, want_reply)


def _send_locked(payload, timeout=2.0, want_reply=True):
    """跳过事件，只认 request_id 匹配的应答"""
    global _pipe
    _req["id"] += 1
    rid = _req["id"]
    payload = dict(payload)
    payload["request_id"] = rid
    try:
        _pipe.write((json.dumps(payload) + "\n").encode("utf-8"))
    except Exception:
        # 管道坏了，重连一次
        try:
            _pipe.close()
        except Exception:
            pass
        try:
            _pipe = _open_pipe(3.0)
            _pipe.write((json.dumps(payload) + "\n").encode("utf-8"))
        except Exception as e:
            return {"error": "write_failed: %s" % e}

    if not want_reply:
        return {"error": "success"}

    t0 = time.time()
    while time.time() - t0 < timeout:
        line = _readline(timeout)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("request_id") == rid:
            return obj
    return {"error": "timeout"}


def _get(prop, timeout=0.6):
    r = _send({"command": ["get_property", prop]}, timeout, want_reply=True)
    if isinstance(r, dict) and r.get("error") == "success":
        return r.get("data")
    return None




def _kill_strays():
    """清掉不属于当前会话的 mpv 进程，避免播放进不可见的旧实例"""
    try:
        keep = None
        try:
            if _proc is not None and _proc.poll() is None:
                keep = _proc.pid
        except Exception:
            keep = None
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mpv.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="gbk", errors="ignore").stdout
        killed = []
        for line in (out or "").strip().split("\n"):
            parts = [x.strip().strip('"') for x in line.split('","')]
            if len(parts) < 2 or parts[0].lower() != "mpv.exe":
                continue
            try:
                pid = int(parts[1])
            except Exception:
                continue
            if keep is not None and pid == keep:
                continue
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            killed.append(pid)
        return killed
    except Exception:
        return []


def _ensure():
    with _io_lock:
        return _ensure_locked()


def _ensure_locked():
    global _proc, _pipe
    if not available():
        raise IOError("未找到 mpv")
    if _proc is None or _proc.poll() is not None:
        args = [_MPV, "--no-config", "--idle=yes", "--pause=yes", "--vid=no", "--force-window=no",
                "--no-border", "--ontop", "--focus-on=never", "--keepaspect-window=no", "--hidpi-window-scale=no", 
                "--keep-open=no", "--hwdec=auto-copy-safe", "--gpu-api=auto", "--vo=gpu", "--osc=yes", "--osd-level=1", "--osd-duration=2000",
                "--input-ipc-server=" + PIPE_NAME]
        _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _pipe = _open_pipe()
    elif _pipe is None:
        _pipe = _open_pipe()
    return True


def is_running():
    return bool(_proc and _proc.poll() is None)



def _find_hwnd(pid):
    """按进程 ID 找可见主窗口句柄"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, lparam):
            p = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and user32.IsWindowVisible(hwnd):
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if (r.right - r.left) > 200 and (r.bottom - r.top) > 100:
                    found.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
        return found[0] if found else None
    except Exception:
        return None


def _window_rect(hwnd):
    """读窗口当前屏幕矩形 (x, y, w, h)"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        r = wt.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


def _fit_window(x, y, w, h, tries=12):
    """
    把 mpv 窗口摆到 (x, y) 尺寸 (w, h)。
    每轮先比对当前矩形：已经贴合就立刻返回（避免反复 SetWindowPos 造成闪烁）。
    """
    if not is_running() or not _proc:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        for _ in range(tries):
            hwnd = _find_hwnd(_proc.pid)
            if hwnd:
                cur = _window_rect(hwnd)
                if cur:
                    cx, cy, cw, ch = cur
                    if (abs(cx - x) <= 10 and abs(cy - y) <= 10
                            and abs(cw - w) <= 10 and abs(ch - h) <= 10):
                        return True          # 已到位，停手（不再动窗口）
                    user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                                        SWP_NOZORDER | SWP_NOACTIVATE)
            else:
                time.sleep(0.2)
                continue
            time.sleep(0.22)
        return True
    except Exception:
        return False



_GOOD_TITLE = ("original", "正片", "main", "main video", "原片", "正版")
# 假轨常见标题（防和谐提示画面）
_BAD_TITLE = ("notice", "warning", "提示", "说明", "声明", "公告", "广告")


def _pick_video_track(tracks):
    """
    从 mpv track-list 里挑真正的正片视频轨。
    假轨特征：标题带 original/提示语，或分辨率相同但排在前。
    """
    vids = [t for t in tracks if t.get("type") == "video"]
    if not vids:
        return None
    # 先剔除明显的假轨（Notice / 提示 / 广告）
    _real = [t for t in vids
             if not any(k in (t.get("title") or "").strip().lower() for k in _BAD_TITLE)]
    if _real:
        vids = _real
    # ① 标题命中关键字的（上传者通常把正片标为 original）
    for t in vids:
        title = (t.get("title") or "").strip().lower()
        if title and any(k in title for k in _GOOD_TITLE):
            return t.get("id")
    # ② 分辨率最大的
    def area(t):
        return int(t.get("demux-w") or 0) * int(t.get("demux-h") or 0)

    vids_sorted = sorted(vids, key=area, reverse=True)
    if len(vids_sorted) >= 1:
        return vids_sorted[0].get("id")
    return None


def _apply_video_track(tries=12):
    """等容器解析出轨道列表后，切到正片轨"""
    for _ in range(tries):
        try:
            r = _send({"command": ["get_property", "track-list"]}, 0.8)
            tracks = (r or {}).get("data")
            if isinstance(tracks, list) and tracks:
                vids = [t for t in tracks if t.get("type") == "video"]
                if not vids:
                    time.sleep(0.25)
                    continue          # 还没解析出视频轨，继续等（不要放弃）
                if len(vids) == 1:
                    # 只有一条也要显式打开，否则 --vid=no 会一直黑屏
                    try:
                        _send({"command": ["set_property", "vid", vids[0].get("id")]}, 0.8)
                    except Exception:
                        pass
                    return True
                want = _pick_video_track(tracks)
                cur = _get("vid", 0.6)
                if want is not None and str(want) != str(cur):
                    _send({"command": ["set_property", "vid", want]}, 0.8)
                # 校验：vid 必须真的打开（False 表示没开）
                got = _get("vid", 0.6)
                if got is False or got is None:
                    _send({"command": ["set_property", "vid", want if want is not None else 1]}, 0.8)
                    time.sleep(0.2)
                    got = _get("vid", 0.6)
                if got not in (False, None):
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False




def _touch():
    _last_seen["t"] = time.time()


def _watchdog():
    """页面长时间没有心跳 → 关掉 mpv（浏览器被关/崩溃时兜底）"""
    while True:
        try:
            time.sleep(5)
            if not is_running():
                continue
            if not _last_seen["t"]:
                continue
            if time.time() - _last_seen["t"] > 20:
                quit_player()
        except Exception:
            pass


def warm():
    """预热：提前把 mpv 进程和 IPC 通道建好，点播放时只 loadfile"""
    try:
        _ensure()
        if not _wd_started["v"]:
            _wd_started["v"] = True
            import threading as _th
            _th.Thread(target=_watchdog, daemon=True).start()
        return True
    except Exception:
        return False


def fit(x, y, w, h):
    """外部调用：立即把窗口摆到指定矩形（返回是否成功）"""
    return _fit_window(int(x), int(y), int(w), int(h))


def play(url, title="", x=0, y=0, w=1280, h=720, fid="", headers=None, vid_hint=None):
    # 单实例守卫：先把残留的 mpv 都清掉（否则可能播进看不见的旧窗口）
    try:
        _kill_strays()
    except Exception:
        pass
    _touch()
    global _fid
    _ensure()
    geo = "%dx%d+%d+%d" % (int(w), int(h), int(x), int(y))
    _cache["geometry"] = geo
    _cache["fullscreen"] = False
    _send({"command": ["set_property", "fullscreen", False]}, 1.0)
    _send({"command": ["set_property", "border", False]}, 1.0)
    _send({"command": ["set_property", "ontop", True]}, 1.0)
    _send({"command": ["set_property", "geometry", geo]}, 1.0)
    if title:
        _send({"command": ["set_property", "title", title[:60]]}, 1.0)
    # 先用隐藏窗口加载，摆好位再显示，避免看到那一下跳动
    # 直连时把 UA/Referer/Cookie 交给 mpv（缺 cookie 会 412）
    try:
        if headers:
            ua = headers.get("User-Agent") or headers.get("user-agent")
            if ua:
                _send({"command": ["set_property", "user-agent", ua]}, 0.8)
            hdr = []
            for k in ("Referer", "Cookie", "Origin"):
                v = headers.get(k)
                if v:
                    hdr.append("%s: %s" % (k, v))
            if hdr:
                _send({"command": ["set_property", "http-header-fields", ",".join(hdr)]}, 0.8)
    except Exception:
        pass
    _send({"command": ["set_property", "force-window", True]}, 1.0)
    _send({"command": ["set_property", "pause", False]}, 1.0)
    # 这台机器以前硬解失败过 → 直接软解，避免每次黑屏等待
    try:
        if _hw_cache_read().get("no_hw"):
            _send({"command": ["set_property", "hwdec", "no"]}, 1.0)
    except Exception:
        pass
    r = _send({"command": ["loadfile", url]}, 3.0)
    _fid = fid
    _cache["duration"] = 0.0
    _cache["pause"] = False

    _send({"command": ["set_property", "pause", False]}, 0.6)
    # 换片后 mpv 会重置窗口 → 用 Win32 精确摆位（geometry 会被 mpv 缩放，不可靠）
    try:
        import threading as _th

        def _place():
            _fit_window(x, y, w, h)

        _th.Thread(target=_place, daemon=True).start()
        # 有些片源带假视频轨（防和谐提示画面），自动切到正片轨
        _th.Thread(target=_apply_video_track, daemon=True).start()
    except Exception:
        pass

    # 有些文件加载后会停在暂停态，这里再确保一次在播
    # 不做阻塞等待：音频可以先播，视频轨由后台线程补开
    try:
        if vid_hint:
            _apply_video_track(tries=2)
    except Exception:
        pass
    _send({"command": ["set_property", "force-window", True]}, 1.0)
    _send({"command": ["set_property", "pause", False]}, 0.8)
    # 起播后盯 6 秒：解码器没出帧就自动改软解（解决部分显卡 HEVC 黑屏）
    try:
        import threading as _th2
        _th2.Thread(target=_ensure_video, args=(vid_hint,), daemon=True).start()
        _th2.Thread(target=_hw_watchdog, args=(9.0,), daemon=True).start()
    except Exception:
        pass
    return r






_HW_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "mpv_hw.json")


def _hw_cache_read():
    try:
        with open(_HW_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _hw_cache_write(obj):
    try:
        os.makedirs(os.path.dirname(_HW_FILE), exist_ok=True)
        with open(_HW_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    except Exception:
        pass


def _ensure_video(vid_hint=None, tries=60):
    """后台确保视频轨被打开（不阻塞音频播放）"""
    try:
        for _ in range(tries):
            time.sleep(0.5)
            if not is_running():
                return
            try:
                w = _get("width", 0.5)
            except Exception:
                w = None
            if w:
                return                     # 已经有画面了
            try:
                if _apply_video_track(tries=2):
                    cur = _get("vid", 0.5)
                    if cur not in (False, None):
                        # 有轨道了，打开输出
                        try:
                            _send({"command": ["set_property", "force-window", True]}, 0.6)
                        except Exception:
                            pass
                        return
            except Exception:
                pass
    except Exception:
        pass


def _hw_watchdog(wait=6.0):
    """硬解失败（有尺寸但没帧）→ 自动切软解并回到原位置"""
    try:
        time.sleep(wait)
        if not is_running():
            return
        w = _get("width", 0.6)
        if not w:
            return
        frames = _get("estimated-frame-number", 0.6) or 0
        try:
            frames = float(frames)
        except Exception:
            frames = 0
        if frames >= 5:
            return                       # 正常出帧，不用管
        pos = _get("time-pos", 0.6) or 0
        try:
            c = _hw_cache_read()
            c["no_hw"] = True
            c["reason"] = "hardware decode produced no frames"
            _hw_cache_write(c)
        except Exception:
            pass
        _send({"command": ["set_property", "hwdec", "no"]}, 1.0)
        time.sleep(0.4)
        try:
            _send({"command": ["seek", float(pos), "absolute"]}, 1.0)
        except Exception:
            pass
        _send({"command": ["set_property", "pause", False]}, 0.6)
    except Exception:
        pass


def append(url, title="", headers=None):
    """把一个文件追加到 mpv 播放列表尾部"""
    _touch()
    _ensure()
    try:
        if headers:
            ua = headers.get("User-Agent") or headers.get("user-agent")
            if ua:
                _send({"command": ["set_property", "user-agent", ua]}, 0.8)
            hdr = []
            for k in ("Referer", "Cookie", "Origin"):
                if headers.get(k):
                    hdr.append("%s: %s" % (k, headers[k]))
            if hdr:
                _send({"command": ["set_property", "http-header-fields", ",".join(hdr)]}, 0.8)
        return _send({"command": ["loadfile", url, "append"]}, 3.0)
    except Exception as e:
        return {"error": str(e)}


def cmd(name, value=None):
    _touch()
    _ensure()
    if name == "pause":
        _cache["pause"] = bool(value)
        return _send({"command": ["set_property", "pause", bool(value)]})
    if name == "toggle_pause":
        cur = bool(_get("pause", 0.6))
        return _send({"command": ["set_property", "pause", not cur]})
    if name == "seek":
        return _send({"command": ["seek", float(value), "absolute"]})
    if name == "volume":
        _cache["volume"] = float(value)
        return _send({"command": ["set_property", "volume", float(value)]})
    if name == "mute":
        _cache["mute"] = bool(value)
        return _send({"command": ["set_property", "mute", bool(value)]})
    if name == "fullscreen":
        on = bool(value)
        _cache["fullscreen"] = on
        r = _send({"command": ["set_property", "fullscreen", on]}, 1.5)
        if not on:
            _send({"command": ["set_property", "geometry", _cache["geometry"]]}, 1.0)
        return r
    if name == "toggle_fullscreen":
        on = not _cache["fullscreen"]
        _cache["fullscreen"] = on
        r = _send({"command": ["set_property", "fullscreen", on]}, 1.5)
        if not on:
            _send({"command": ["set_property", "geometry", _cache["geometry"]]}, 1.0)
        return r
    return {"error": "unknown_command"}


_poll_n = {"n": 0}


def status():
    _touch()
    if not is_running():
        return {"running": False}
    _poll_n["n"] += 1
    if not _cache.get("duration"):
        dv = _get("duration", 0.5)
        if dv and float(dv) > 1:
            _cache["duration"] = float(dv)
    t = _get("time-pos", 0.6)
    if _poll_n["n"] % 3 == 0:
        pv = _get("pause", 0.5)
        if pv is not None:
            _cache["pause"] = bool(pv)
    p = _cache.get("pause")
    frames = 0
    try:
        frames = int(float(_get("estimated-frame-number", 0.4) or 0))
    except Exception:
        frames = 0
    hw = None
    try:
        hw = _get("hwdec-current", 0.4)
    except Exception:
        pass
    file_loaded = False
    try:
        pth = _get("path", 0.4)
        file_loaded = bool(pth) or bool(_get("duration", 0.3))
    except Exception:
        pass
    idle = False
    try:
        idle = bool(_get("idle-active", 0.4))
    except Exception:
        pass
    vcodec = None
    try:
        vcodec = _get("video-codec", 0.4)
    except Exception:
        pass
    return {
        "running": True,
        "file_loaded": file_loaded,
        "idle": idle,
        "vcodec": vcodec,
        "width": int(_get("width", 0.4) or 0),
        "frames": frames,
        "hwdec": hw,
        "time": float(t or 0),
        "duration": float(_cache.get("duration") or 0),
        "pause": bool(p) if p is not None else False,
        "volume": float(_cache.get("volume") or 0),
        "mute": bool(_cache.get("mute")),
        "fullscreen": bool(_cache.get("fullscreen")),
        "fid": _fid,
    }


def quit_player():
    global _proc, _pipe, _fid
    try:
        if _pipe:
            _send({"command": ["quit"]}, 0.6, want_reply=False)
    except Exception:
        pass
    time.sleep(0.3)
    try:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=3)
            except Exception:
                _proc.kill()
    except Exception:
        pass
    try:
        if _pipe:
            _pipe.close()
    except Exception:
        pass
    # 兜底：本服务启动过 mpv 时，把残留实例一并结束（避免留下孤儿窗口）
    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/IM", "mpv.exe", "/F"],
                capture_output=True,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass

    _proc = None
    _pipe = None
    _fid = ""
    _cache.update({"duration": 0.0, "fullscreen": False})
    return {"ok": True}
