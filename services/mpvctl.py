# -*- coding: utf-8 -*-
"""mpv 进程与 IPC 控制（Windows 命名管道）"""
import json
import os
import subprocess
import threading
import time

PIPE_NAME = r"\\.\pipe\flixer-mpv"
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MPV = os.path.join(_BASE, "bin", "mpv", "mpv.exe")
_LOCK = threading.Lock()
_state = {"proc": None, "pipe": None, "fid": "", "url": ""}


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
            time.sleep(0.15)
    raise IOError("IRP 管道未就绪: %s" % last)


_REQ = {"id": 0}


def _read_line(pipe, timeout=2.0):
    """读一行（到 \n）"""
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ch = pipe.read(1)
        except Exception:
            return None
        if not ch:
            time.sleep(0.01)
            continue
        if ch == b"\n":
            return buf.decode("utf-8", "replace")
        buf += ch
    return None


def _send(pipe, payload, timeout=2.0):
    """发送命令，跳过 mpv 主动推送的事件，只返回 request_id 匹配的应答"""
    _REQ["id"] += 1
    rid = _REQ["id"]
    payload = dict(payload)
    payload["request_id"] = rid
    try:
        pipe.write((json.dumps(payload) + "\n").encode("utf-8"))
    except Exception as e:
        return {"error": "write_failed: %s" % e}

    t0 = time.time()
    while time.time() - t0 < timeout:
        line = _read_line(pipe, timeout=timeout)
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
        # 其它行是事件（event / request_id 不匹配），丢弃
    return {"error": "no_reply"}


def is_running():
    p = _state.get("proc")
    return bool(p and p.poll() is None)


def ensure_pipe():
    """确保进程和管道都在"""
    if not available():
        raise IOError("未找到 mpv")
    if not is_running():
        args = [_MPV, "--no-config", "--idle=yes", "--force-window=yes",
                "--no-border", "--ontop", "--no-input-default-bindings",
                "--keep-open=no", "--hwdec=auto-safe",
                "--input-ipc-server=" + PIPE_NAME]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _state["proc"] = proc
        _state["pipe"] = None
    if _state.get("pipe") is None:
        _state["pipe"] = _open_pipe()
    return _state["pipe"]


def play(url, title="", x=0, y=0, w=1280, h=720, fid=""):
    """起播（复用已有实例）"""
    with _LOCK:
        pipe = ensure_pipe()
        # 窗口几何
        try:
            _send(pipe, {"command": ["set_property", "geometry",
                                     "%dx%d+%d+%d" % (int(w), int(h), int(x), int(y))]})
        except Exception:
            pass
        for prop, val in (("ontop", True), ("border", False)):
            try:
                _send(pipe, {"command": ["set_property", prop, val]})
            except Exception:
                pass
        if title:
            try:
                _send(pipe, {"command": ["set_property", "title", title[:60]]})
            except Exception:
                pass
        r = _send(pipe, {"command": ["loadfile", url]}, timeout=3.0)
        _state["fid"] = fid
        _state["url"] = url
        return r


def cmd(name, value=None):
    """通用命令：pause / seek / volume / mute …"""
    with _LOCK:
        pipe = ensure_pipe()
        if name == "pause":
            return _send(pipe, {"command": ["set_property", "pause", bool(value)]})
        if name == "toggle_pause":
            cur = status().get("pause")
            return _send(pipe, {"command": ["set_property", "pause", not cur]})
        if name == "seek":
            return _send(pipe, {"command": ["seek", float(value), "absolute"]})
        if name == "volume":
            return _send(pipe, {"command": ["set_property", "volume", float(value)]})
        if name == "mute":
            return _send(pipe, {"command": ["set_property", "mute", bool(value)]})
        return {"error": "unknown_command"}


def _get(pipe, prop):
    r = _send(pipe, {"command": ["get_property", prop]}, timeout=1.5)
    if isinstance(r, dict) and r.get("error") == "success":
        return r.get("data")
    return None


def status():
    if not is_running():
        return {"running": False}
    try:
        pipe = _state.get("pipe") or _open_pipe()
        _state["pipe"] = pipe
        return {
            "running": True,
            "time": _get(pipe, "time-pos") or 0,
            "duration": _get(pipe, "duration") or 0,
            "pause": bool(_get(pipe, "pause")),
            "volume": _get(pipe, "volume") or 0,
            "mute": bool(_get(pipe, "mute")),
            "video_w": _get(pipe, "width") or 0,
            "video_h": _get(pipe, "height") or 0,
            "idle": bool(_get(pipe, "idle-active")),
            "fid": _state.get("fid", ""),
        }
    except Exception as e:
        return {"running": True, "error": str(e)[:80]}


def quit_player():
    with _LOCK:
        try:
            if _state.get("pipe"):
                _send(_state["pipe"], {"command": ["quit"]})
        except Exception:
            pass
        p = _state.get("proc")
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            if _state.get("pipe"):
                _state["pipe"].close()
        except Exception:
            pass
        _state.update({"proc": None, "pipe": None, "fid": "", "url": ""})
    return {"ok": True}
