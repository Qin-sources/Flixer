"""
音轨兼容性判断

问题：Chrome 内置解码器不含 Dolby（EAC3/AC3/DTS/TrueHD），
      这类片源播放时画面正常、完全没声音。

策略：
  1. 先用文件名判断（零成本，覆盖九成名）
  2. 文件名没写音频编码又是 MKV 时，读文件头确认（只读 1-2MB）
  3. 结果缓存，并影响排序：可播音轨优先
"""
import logging
import re
import time

log = logging.getLogger("audio")

# 浏览器解不了的音轨
BAD_CODECS = {
    "eac3": "E-AC3(杜比数字+)",
    "ac3": "AC3(杜比)",
    "dts": "DTS",
    "dtshd": "DTS-HD",
    "truehd": "TrueHD",
    "atmos": "Atmos(杜比全景声)",
    "ddp": "DDP(杜比数字+)",
    "dd+": "DDP(杜比数字+)",
}
# 浏览器能解的
OK_CODECS = {
    "aac": "AAC",
    "mp3": "MP3",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "flac": "FLAC",
}

_CACHE = {}
_TTL = 3600 * 6


def from_name(name):
    """从文件名推断音轨（返回 dict，unknown 表示没线索）"""
    n = (name or "").lower()
    # 精确标记：DDP5.1 / DD+ / EAC3 / AC3 / DTS-HD / TrueHD / Atmos
    tests = [
        (r"truehd", "truehd"),
        (r"dts[\-\.]?hd|dts[\-\.]?x|dts", "dts"),
        (r"eac3|e-ac3", "eac3"),
        (r"ddp|dd\+|ddp5|ddp7", "ddp"),
        (r"atmos", "atmos"),
        (r"ac3|dd5\.1|dd2\.0", "ac3"),
        (r"aac", "aac"),
        (r"flac", "flac"),
        (r"opus", "opus"),
        (r"mp3", "mp3"),
    ]
    # 中文写法（国内资源站常用）
    cn = {"杜比全景声": "atmos", "全景声": "atmos", "杜比": "ddp",
          "杜比音效": "ddp", "无损音轨": "truehd"}
    for k, codec in cn.items():
        if k in n:
            return {"codec": codec, "label": BAD_CODECS[codec],
                    "browser_ok": False, "source": "name"}

    for pat, codec in tests:
        if re.search(pat, n):
            if codec in BAD_CODECS:
                return {"codec": codec, "label": BAD_CODECS[codec],
                        "browser_ok": False, "source": "name"}
            return {"codec": codec, "label": OK_CODECS.get(codec, codec),
                    "browser_ok": True, "source": "name"}
    return {"codec": "", "label": "", "browser_ok": None, "source": "none"}


def _cache_key(fid):
    return "a:" + (fid or "")


def get_cached(fid):
    hit = _CACHE.get(_cache_key(fid))
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def put_cached(fid, val):
    _CACHE[_cache_key(fid)] = (time.time(), val)


def probe_mkv_stream(fetch, total, window=2 * 1024 * 1024):
    """
    读 MKV 头部拿音轨编码。fetch(off, len) 由调用方提供。
    返回 {'codec','label','browser_ok','source': 'probe'}
    """
    try:
        from . import mkv
        rd = mkv.RangeReader(fetch, total, window=window)
        info = mkv.probe(rd)
        if not info.get("ok"):
            return None
        auds = [t for t in info.get("tracks", []) if t.get("type") == 2]
        if not auds:
            return None
        codec = (auds[0].get("codec") or "").upper()
        c = codec.replace("A_", "").lower()
        if ("EAC3" in codec) or c.startswith("eac3"):
            return {"codec": "eac3", "label": BAD_CODECS["eac3"], "browser_ok": False, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("ac3"):
            return {"codec": "ac3", "label": BAD_CODECS["ac3"], "browser_ok": False, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("dts"):
            return {"codec": "dts", "label": BAD_CODECS["dts"], "browser_ok": False, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("truehd"):
            return {"codec": "truehd", "label": BAD_CODECS["truehd"], "browser_ok": False, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("aac"):
            return {"codec": "aac", "label": OK_CODECS["aac"], "browser_ok": True, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("mp3"):
            return {"codec": "mp3", "label": OK_CODECS["mp3"], "browser_ok": True, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("opus"):
            return {"codec": "opus", "label": OK_CODECS["opus"], "browser_ok": True, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        if c.startswith("flac"):
            return {"codec": "flac", "label": OK_CODECS["flac"], "browser_ok": True, "source": "probe",
                    "all": [x.get("codec") for x in auds]}
        # 其它（PCM 等）算能播
        return {"codec": c, "label": codec, "browser_ok": True, "source": "probe",
                "all": [x.get("codec") for x in auds]}
    except Exception as e:
        log.info("音轨探测失败: %s", str(e)[:80])
        return None


def annotate_resources(resources):
    """给片源列表打上音轨标记，并让可播的排前面"""
    for r in resources or []:
        a = from_name(r.get("title") or "")
        if a["browser_ok"] is None:
            continue
        r["audio_codec"] = a["codec"]
        r["audio_label"] = a["label"]
        r["audio_ok"] = a["browser_ok"]
    # 轻微调权：可播音轨优先（不改动太多，避免盖过画质排序）
    for r in resources or []:
        if r.get("audio_ok") is True:
            r["score"] = r.get("score", 0) + 3
        elif r.get("audio_ok") is False:
            r["score"] = r.get("score", 0) - 4
    try:
        resources.sort(key=lambda x: x.get("score", 0), reverse=True)
    except Exception:
        pass
    return resources

# ── ffmpeg 兜底探测（mp4 / ts 等非 MKV 容器）──
import re as _re
import subprocess as _sp
import os as _os

_FFMPEG = None


def _ffmpeg_path():
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    p = _os.path.join(base, "bin", "ffmpeg.exe")
    _FFMPEG = p if _os.path.exists(p) else None
    return _FFMPEG


_CODEC_MAP = {
    "aac": "aac", "mp3": "mp3", "opus": "opus", "vorbis": "vorbis",
    "flac": "flac", "pcm_s16le": "pcm", "pcm_s24le": "pcm", "pcm_s32le": "pcm",
    "ac3": "ac3", "eac3": "eac3", "dts": "dts", "truehd": "truehd", "mlp": "truehd",
}


def probe_ffmpeg(url, headers=None, timeout=25):
    """用 ffmpeg 读文件头识别音轨编码；失败返回 None"""
    exe = _ffmpeg_path()
    if not exe or not url:
        return None
    cmd = [exe, "-hide_banner", "-loglevel", "info"]
    ua = (headers or {}).get("User-Agent")
    ref = (headers or {}).get("Referer")
    if ua:
        cmd += ["-user_agent", ua]
    if ref:
        cmd += ["-referer", ref]
    cmd += ["-probesize", "2000000", "-analyzeduration", "3000000",
            "-i", url, "-t", "0", "-f", "null", "-"]
    try:
        r = _sp.run(cmd, capture_output=True, timeout=timeout,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    txt = (r.stderr or b"").decode("utf-8", "replace")
    m = _re.search(r"Stream #\d+:\d+.*?: Audio: ([a-zA-Z0-9_]+)", txt)
    if not m:
        return None
    raw = m.group(1).lower()
    codec = _CODEC_MAP.get(raw, raw)
    bad = codec in BAD_CODECS
    return {
        "codec": codec,
        "label": BAD_CODECS.get(codec) or OK_CODECS.get(codec, codec),
        "browser_ok": (not bad),
        "source": "ffmpeg",
    }
