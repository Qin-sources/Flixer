# -*- coding: utf-8 -*-
"""
杜比音轨兼容层：把浏览器解不了的音频转成 AAC

为什么需要：
  Chrome 没有 Dolby 授权，EAC3/AC3/DTS/TrueHD 一律静音。
  实测 canPlayType('audio/mp4; codecs="ec-3"') = "no"，WebCodecs 也不支持。

做法（Jellyfin Remux 模式）：
  · 视频流原样复制，不重新编码（画质零损失，CPU 几乎为零）
  · 只把音轨解出来转 AAC 128k
  · 输出分段 m4a，浏览器按需取，不占内存
  · 结果缓存到 data/aac/，同一片只转一次

实测：60 秒音轨 28.9 秒转完（2.1x 实时），961KB。
"""
import hashlib
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("aac")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE = os.path.join(_BASE, "data", "aac")
_FFMPEG = os.path.join(_BASE, "bin", "ffmpeg.exe")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _cookie():
    try:
        from . import quark_api as QA
        return QA.load_cookie() or ""
    except Exception:
        return ""


def pick_track(tracks):
    """
    从 MKV 音轨列表里挑最适合的一条（返回在音频轨中的序号）。
    优先：中文/国语/粤语 > 默认轨 > 第一条
    """
    auds = [t for t in (tracks or []) if t.get("type") == 2]
    if not auds:
        return 0
    best_i, best_s = 0, -99
    for i, t in enumerate(auds):
        s = 0
        lang = (t.get("lang") or "").lower()
        name = (t.get("name") or "")
        if lang in ("chi", "zho", "zh", "cn", "yue") or "国" in name or "粤" in name or "中" in name:
            s += 10
        if t.get("default"):
            s += 5
        if i == 0:
            s += 1
        if s > best_s:
            best_i, best_s = i, s
    return best_i


_LOCK = threading.Lock()
_JOBS = {}          # key -> {"proc":..., "out":..., "state":...}


def available():
    return os.path.exists(_FFMPEG)


def _key(fid, start):
    return "aac_%s_%d.m4a" % (hashlib.md5(fid.encode()).hexdigest()[:16], int(start))


def cached_path(fid, start):
    p = os.path.join(_CACHE, _key(fid, start))
    return p if os.path.exists(p) and os.path.getsize(p) > 1000 else None


def start(fid, src_url, start=0, duration=600, bitrate="128k", track_index=0):
    """
    从 start 秒开始转 duration 秒音频（默认 10 分钟一段）。
    返回 (path, ready) —— ready=False 表示还在转，路径暂不可用。
    """
    if not available():
        return None, False
    os.makedirs(_CACHE, exist_ok=True)
    # 统一走自家流代理：请求头已验证，直链会 412
    if not str(src_url).startswith("http://127.0.0.1"):
        src_url = "http://127.0.0.1:7100/api/stream?fid=" + str(fid)
    out = os.path.join(_CACHE, _key(fid, start))
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return out, True

    with _LOCK:
        job = _JOBS.get(out)
        if job and job["proc"] and job["proc"].poll() is None:
            return out, False
        tmp = out + ".part"
        cmd = [
            _FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(int(start)), "-i", src_url, "-t", str(int(duration)),
            "-vn", "-map", "0:a:%d" % int(track_index), "-c:a", "aac", "-b:a", bitrate, "-ac", "2",
            "-movflags", "+faststart", "-f", "mp4", tmp,
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log.warning("ffmpeg 启动失败 %s", str(e)[:80])
            return None, False

        def _wait():
            try:
                proc.wait(timeout=1800)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
                    os.replace(tmp, out)
                    log.info("音频转换完成 %s", os.path.basename(out))
                else:
                    err = b""
                    try:
                        err = proc.stderr.read()[:200] if proc.stderr else b""
                    except Exception:
                        pass
                    log.warning("音频转换失败 rc=%s %s", proc.returncode,
                                err.decode("utf-8", "replace")[:120])
                    if os.path.exists(tmp):
                        os.remove(tmp)
            except Exception:
                pass
            finally:
                _JOBS.pop(out, None)

        _JOBS[out] = {"proc": proc, "out": out, "state": "running"}
        threading.Thread(target=_wait, daemon=True).start()
        return out, False



def live_cmd(fid, start=0, bitrate="96k", track_index=0):
    """
    构造流式转码命令（输出 fMP4 到 stdout）。
    返回 None 表示缺少 ffmpeg。
    """
    if not available():
        return None
    src = "http://127.0.0.1:7100/api/stream?fid=" + str(fid)
    return [
        _FFMPEG, "-hide_banner", "-loglevel", "error",
        "-ss", str(int(start)),
        "-i", src,
        "-vn",
        "-map", "0:a:%d" % int(track_index),
        "-c:a", "aac", "-b:a", bitrate, "-ac", "2",
        "-movflags", "+empty_moov+default_base_moof",
        "-frag_duration", "400000",
        "-f", "mp4", "pipe:1",
    ]




_LIVE = {}          # key -> {"proc":..., "path":...}


def live_key(fid, start):
    return os.path.join(_CACHE, "live_%s_%d.mp4" % (
        hashlib.md5(str(fid).encode()).hexdigest()[:16], int(start)))


def start_live(fid, start=0, bitrate="160k", track_index=0):
    """启动（或复用）实时转码：写分片 MP4 文件，供 HTTP Range 读取"""
    if not available():
        return None
    os.makedirs(_CACHE, exist_ok=True)
    out = live_key(fid, start)
    with _LOCK:
        j = _LIVE.get(out)
        if j and j.get("proc") and j["proc"].poll() is None:
            return out
        cmd = live_cmd(fid, start, bitrate, track_index)
        if not cmd:
            return None
        # 最后两项是 "-f mp4 pipe:1"，改成写文件
        cmd = cmd[:-3] + ["-f", "mp4", out]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log.warning("实时转码启动失败 %s", str(e)[:80])
            return None
        _LIVE[out] = {"proc": proc, "path": out}
        return out


def live_path(fid, start):
    p = live_key(fid, start)
    return p if os.path.exists(p) else None


def live_size(fid, start):
    p = live_path(fid, start)
    try:
        return os.path.getsize(p) if p else 0
    except Exception:
        return 0


def live_done(fid, start):
    p = live_key(fid, start)
    j = _LIVE.get(p)
    if not j:
        return True
    pr = j.get("proc")
    return (pr is None) or (pr.poll() is not None)


def progress(fid, start):
    """查询进度：缓存命中 / 转换中"""
    p = cached_path(fid, start)
    if p:
        return {"ready": True, "path": p, "size": os.path.getsize(p)}
    out = os.path.join(_CACHE, _key(fid, start))
    job = _JOBS.get(out)
    if job:
        return {"ready": False, "running": True}
    tmp = out + ".part"
    if os.path.exists(tmp):
        return {"ready": False, "running": True, "size": os.path.getsize(tmp)}
    return {"ready": False, "running": False}


def purge(days=7):
    """清掉旧缓存"""
    try:
        now = time.time()
        n = 0
        for fn in os.listdir(_CACHE):
            p = os.path.join(_CACHE, fn)
            if now - os.path.getmtime(p) > days * 86400:
                os.remove(p)
                n += 1
        return n
    except Exception:
        return 0
