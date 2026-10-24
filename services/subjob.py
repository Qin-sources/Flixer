"""
内嵌字幕抽取任务管理

设计：
  · 每个 (fid, track) 一个任务，后台线程跑，边下边解析边写 VTT
  · 写出的 VTT 随时是合法的（先写头部，逐条追加）
  · 任务状态（进度/条数/速度/预计剩余）供前端轮询
  · 结果落盘 data/subs/emb_<fid>_<track>.vtt，重启用缓存秒回

速度参考：夸克直连约 5 MB/s，一部 4GB 的 45 分钟剧集约 13 分钟；
1080p 1.5GB 电影约 5 分钟。
"""
import io
import logging
import os
import threading
import time

import requests

from . import mkv
from . import mkvextract

log = logging.getLogger("subjob")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_BASE, "data", "subs")

_LOCK = threading.Lock()
_JOBS = {}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CHUNK = 64 * 1024 * 1024        # 64MB 一段，兼顾吞吐与进度粒度


def _out_path(fid, track):
    os.makedirs(_DIR, exist_ok=True)
    safe = "".join(c for c in (fid or "") if c.isalnum())[:40]
    return os.path.join(_DIR, "emb_%s_%02d.vtt" % (safe, int(track)))


def partial_path(fid, track):
    """抽取中的临时文件（已经有内容就能先给用户看）"""
    p = _out_path(fid, track) + ".part"
    try:
        if os.path.exists(p) and os.path.getsize(p) > 40:
            return p
    except Exception:
        pass
    return None


def read_partial(fid, track, max_bytes=8 * 1024 * 1024):
    """读 partial 文件，截到最后一个完整 cue，保证是合法 VTT"""
    p = partial_path(fid, track)
    if not p:
        return None
    try:
        with io.open(p, encoding="utf-8", errors="ignore") as f:
            txt = f.read(max_bytes)
    except Exception:
        return None
    if not txt.strip().startswith("WEBVTT"):
        return None
    cut = txt.rfind("\n\n")
    if cut <= 7:
        return None
    body = txt[:cut + 1]
    if "-->" not in body:
        return None
    return body + "\n"


def cached(fid, track):
    p = _out_path(fid, track)
    try:
        if os.path.exists(p) and os.path.getsize(p) > 20:
            return p
    except Exception:
        pass
    return None


def status(fid, track):
    key = (fid, int(track))
    with _LOCK:
        j = _JOBS.get(key)
        if j:
            return dict(j["state"])
    p = cached(fid, track)
    if p:
        return {"state": "done", "percent": 100.0, "cues": _count_cues(p),
                "cached": True, "bytes": os.path.getsize(p)}
    pp = partial_path(fid, track)
    if pp:
        return {"state": "partial", "percent": 0.0,
                "cues": _count_cues(pp), "partial": True,
                "bytes": os.path.getsize(pp)}
    return {"state": "idle", "percent": 0.0, "cues": 0}


def _count_cues(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return sum(1 for line in f if "-->" in line)
    except Exception:
        return 0


def start(fid, track, total, direct_url, scale=1000000):
    """启动（或复用）抽取任务"""
    key = (fid, int(track))
    with _LOCK:
        j = _JOBS.get(key)
        if j and j["state"].get("state") == "running":
            return j["state"]
        st = {"state": "running", "percent": 0.0, "cues": 0, "bytes": 0,
              "speed": 0.0, "eta": None, "started": time.time(), "error": None}
        _JOBS[key] = {"state": st, "cancel": False}

    t = threading.Thread(target=_run, args=(key, fid, int(track), int(total),
                                            direct_url, scale), daemon=True)
    t.start()
    return _JOBS[key]["state"]


def cancel(fid, track):
    key = (fid, int(track))
    with _LOCK:
        j = _JOBS.get(key)
        if j:
            j["cancel"] = True
    return True


def _run(key, fid, track, total, url, scale):
    st = _JOBS[key]["state"]
    out = _out_path(fid, track)
    tmp = out + ".part"
    meta_p = out + ".resume.json"

    # 断点续传
    start_off = 0
    start_time = 0
    if os.path.exists(tmp) and os.path.exists(meta_p):
        try:
            import json as _json
            meta = _json.loads(io.open(meta_p, encoding="utf-8").read())
            start_off = int(meta.get("off") or 0)
            start_time = int(meta.get("time") or 0)
            if start_off <= 0:
                start_off, start_time = 0, 0
        except Exception:
            start_off, start_time = 0, 0

    ex = mkvextract.Extractor(track, scale)
    ex.cluster_time = start_time
    if start_off > 0 and os.path.exists(tmp):
        _load_existing_vtt(tmp, ex)
    got = start_off
    t0 = time.time()
    last_dump = [0.0]
    fh = None
    append = start_off > 0
    try:
        if append:
            fh = io.open(tmp, "a", encoding="utf-8")
        else:
            fh = io.open(tmp, "w", encoding="utf-8")
            fh.write("WEBVTT\n\n")
            fh.flush()

        sess = requests.Session()
        sess.trust_env = False
        sess.proxies = {"http": None, "https": None}
        try:
            from . import play as _P
            headers = _P.stream_headers()
        except Exception:
            headers = {"User-Agent": UA, "Referer": "https://pan.quark.cn/"}

        off = start_off
        while off < total:
            if _JOBS.get(key, {}).get("cancel"):
                st["state"] = "cancelled"
                break

            end = min(off + CHUNK - 1, total - 1)
            ok_chunk = False
            for attempt in range(3):
                try:
                    h = dict(headers)
                    h["Range"] = "bytes=%d-%d" % (off, end)
                    r = sess.get(url, headers=h, stream=True, timeout=(15, 300))
                    if r.status_code in (403, 412):
                        r.close()
                        # 直链过期：刷新
                        try:
                            from . import play as _P
                            u2, _e = _P.direct_url(fid, force=True)
                            if u2:
                                url = u2
                        except Exception:
                            pass
                        continue
                    if r.status_code not in (200, 206):
                        st["error"] = "取流失败 HTTP %s" % r.status_code
                        r.close()
                        break
                    for piece in r.iter_content(1024 * 1024):
                        if not piece:
                            continue
                        got += len(piece)
                        ex.feed(piece)
                        now = time.time()
                        if now - last_dump[0] > 2:
                            last_dump[0] = now
                            _dump(fh, ex)
                            _save_resume(meta_p, ex, got)
                            el = max(0.1, now - t0)
                            st.update({
                                "percent": round(min(99.9, got * 100.0 / max(1, total)), 1),
                                "cues": len(ex.cues),
                                "bytes": got,
                                "speed": round(got / 1024 / 1024 / el, 2),
                                "eta": int((total - got) / max(0.01, got / el)),
                            })
                    r.close()
                    ok_chunk = True
                    break
                except Exception as e:
                    st["error"] = str(e)[:120]
                    time.sleep(1.5 * (attempt + 1))
                    continue

            if not ok_chunk:
                st["state"] = "failed"
                break
            off = end + 1

        if not st.get("error") or ok_chunk:
            ex.finish()
            fh.flush()
            fh.close()
            fh = None
            if st["state"] != "cancelled":
                try:
                    os.replace(tmp, out)
                    if os.path.exists(meta_p):
                        os.remove(meta_p)
                except Exception:
                    pass
                st.update({"state": "done", "percent": 100.0,
                           "cues": len(ex.cues), "bytes": got})
                log.info("字幕抽取完成 fid=%s track=%s cues=%d",
                         fid[:8], track, len(ex.cues))
    except Exception as e:
        st["state"] = "failed"
        st["error"] = str(e)[:160]
        log.warning("字幕抽取失败 %s", str(e)[:160])
    finally:
        try:
            if fh:
                fh.close()
        except Exception:
            pass
        if st.get("state") == "failed":
            # 保留现场，下次可续
            try:
                _save_resume(meta_p, ex, got)
            except Exception:
                pass


def _load_existing_vtt(path, ex):
    """续传时把已写好的字幕读回解析器，避免 _dump 覆盖丢失"""
    try:
        txt = io.open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return 0
    if "-->" not in txt:
        return 0
    import re as _re
    pat = _re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)\s*-->\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
    blocks = txt.split("\n\n")
    n = 0
    for b in blocks:
        lines = [x for x in b.split("\n") if x.strip()]
        if len(lines) < 2:
            continue
        m = pat.search(lines[0]) or pat.search(lines[1] if len(lines) > 1 else "")
        if not m:
            continue
        try:
            a = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            z = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
        except Exception:
            continue
        idx = 1 if pat.search(lines[0]) else 2
        text = "\n".join(lines[idx:]).strip()
        if text:
            ex.cues.append((a, z, text))
            n += 1
    try:
        ex.cues.sort(key=lambda x: x[0])
    except Exception:
        pass
    return n


def _save_resume(path, ex, got):
    """记录断点：最后一个完整簇的位置与时间戳"""
    try:
        import json as _json
        lc = getattr(ex, "last_cluster", None)
        data = {"off": got, "time": ex.cluster_time or 0}
        if lc:
            data["off"] = lc[0]
            data["time"] = lc[1]
        io.open(path, "w", encoding="utf-8").write(_json.dumps(data))
    except Exception:
        pass


def _dump(fh, ex, out_append=True):
    """把目前解析到的字幕整段重写（简单可靠：条数不多，重写代价低）"""
    try:
        vtt = ex.to_vtt()
        fh.seek(0)
        fh.truncate()
        fh.write(vtt)
        fh.flush()
    except Exception:
        pass
