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


def _ensure():
    with _io_lock:
        return _ensure_locked()


def _ensure_locked():
    global _proc, _pipe
    if not available():
        raise IOError("未找到 mpv")
    if _proc is None or _proc.poll() is not None:
        args = [_MPV, "--no-config", "--idle=yes", "--force-window=yes",
                "--no-border", "--ontop", 
                "--keep-open=no", "--hwdec=auto-safe", "--osc=no",
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


def play(url, title="", x=0, y=0, w=1280, h=720, fid=""):
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
    _send({"command": ["set_property", "pause", False]}, 1.0)
    r = _send({"command": ["loadfile", url]}, 3.0)
    _fid = fid
    _cache["duration"] = 0.0
    _cache["pause"] = False

    # 等时长出现（最多 ~6 秒）；失败就重下一次
    for attempt in range(2):
        for _ in range(7):
            time.sleep(0.4)
            d = _get("duration", 0.6)
            if d and float(d) > 1:
                _cache["duration"] = float(d)
                break
        if _cache["duration"] > 1:
            break
        _send({"command": ["set_property", "pause", False]}, 0.8)
        r = _send({"command": ["loadfile", url]}, 3.0)

    _send({"command": ["set_property", "pause", False]}, 0.8)
    return r


def cmd(name, value=None):
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
    if not is_running():
        return {"running": False}
    _poll_n["n"] += 1
    t = _get("time-pos", 0.6)
    if _poll_n["n"] % 3 == 0:
        pv = _get("pause", 0.5)
        if pv is not None:
            _cache["pause"] = bool(pv)
    p = _cache.get("pause")
    return {
        "running": True,
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
