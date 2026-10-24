"""
HLS 转码 —— 这是 LibreTV 能播 MKV 的真正原理

浏览器解不了 Matroska 容器（哪怕里面是 H.264），
但只要转成 HLS（m3u8 + ts 分片），配 hls.js 就能播，而且能边转边播。

关键优化：
  - 视频流 -c:v copy 直接复制，不重新编码（CPU 几乎不占，速度取决于带宽）
  - 音频 -c:a aac 重新编码（MKV 里常见 AC3/DTS，浏览器不支持）
  - 分片 4 秒，播完一片就能开始看，不用等整个文件转完

ffmpeg 需要带着 Cookie 去拉夸克直链，所以用 -headers 传。
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from . import play as PL

FFMPEG_CANDIDATES = [
    r"D:\Tools\ffmpeg\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    "ffmpeg",
]

TASKS = {}
# 本地代理入口：ffmpeg 从这里拉数据比直连 CDN 快得多
_LOCAL_STREAM = "http://127.0.0.1:7100/api/stream"
_LOCK = threading.Lock()
_MAX_TASKS = 4          # 同时最多跑几个转码，避免把机器拖死


def ffmpeg_path():
    for p in FFMPEG_CANDIDATES:
        if p == "ffmpeg":
            return p
        if os.path.exists(p):
            return p
    return "ffmpeg"


def _cleanup_old():
    """清掉超过 30 分钟没动的任务"""
    now = time.time()
    for tid in list(TASKS.keys()):
        t = TASKS.get(tid)
        if not t:
            continue
        if now - t["touched"] > 30 * 60:
            stop(tid)


def stop(tid):
    t = TASKS.pop(tid, None)
    if not t:
        return
    proc = t.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except Exception:
                proc.kill()
        except Exception:
            pass
    try:
        shutil.rmtree(t["dir"], ignore_errors=True)
    except Exception:
        pass


FFPROBE_CANDIDATES = [
    r"D:\Tools\ffmpeg\ffmpeg-9.0.2-full_build\bin\ffprobe.exe",
    r"C:\ffmpeg\bin\ffprobe.exe",
    "ffprobe",
]


def ffprobe_path():
    for p in FFPROBE_CANDIDATES:
        if p == "ffprobe":
            return p
        if os.path.exists(p):
            return p
    return "ffprobe"


_ENC_CACHE = [None]
_HW_CACHE = [None]


def _test_hwaccel(kind):
    """验证这个硬件解码器能不能用"""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
           "-hwaccel", kind,
           "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10:duration=0.3",
           "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def _pick_hwaccel():
    """挑一个可用的硬件解码器"""
    if _HW_CACHE[0] is not None:
        return _HW_CACHE[0]
    for kind in ("qsv", "d3d11va", "dxva2", "cuda", "auto"):
        if _test_hwaccel(kind):
            _HW_CACHE[0] = kind
            return kind
    _HW_CACHE[0] = ""
    return ""



def _test_encoder(name, extra):
    """跑 0.2 秒的空编码，验证这个编码器真能用"""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=0.3",
           "-c:v", name] + extra + ["-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def _pick_encoder():
    """选最快的可用编码器"""
    if _ENC_CACHE[0]:
        return _ENC_CACHE[0]

    candidates = [
        # 硬件优先，速度是软件的 5-20 倍
        ("h264_qsv",  ["-preset", "veryfast", "-global_quality", "23"]),
        ("h264_nvenc", ["-preset", "p1", "-rc", "vbr", "-cq", "23"]),
        ("h264_amf",  ["-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]),
        # 软件兜底
        ("libx264",   ["-preset", "ultrafast", "-crf", "23"]),
    ]
    for name, extra in candidates:
        if _test_encoder(name, extra):
            _ENC_CACHE[0] = (name, extra)
            return _ENC_CACHE[0]
    _ENC_CACHE[0] = ("libx264", ["-preset", "ultrafast", "-crf", "23"])
    return _ENC_CACHE[0]


def probe_codec(url, header_str, timeout=40):
    """
    返回 (vcodec, acodec)
    vcodec 常见：h264 / hevc / av1 / vp9
    """
    cmd = [ffprobe_path(), "-hide_banner", "-v", "error"]
    if header_str:
        cmd += ["-headers", header_str]
    cmd += [
           "-select_streams", "v:0",
           "-show_entries", "stream=codec_name",
           "-of", "default=nw=1:nk=1", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        vc = (r.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        vcodec = vc[0].strip() if vc else ""
    except Exception:
        vcodec = ""

    cmd2 = [ffprobe_path(), "-hide_banner", "-v", "error"]
    if header_str:
        cmd2 += ["-headers", header_str]
    cmd2 += [
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=nw=1:nk=1", url]
    try:
        r2 = subprocess.run(cmd2, capture_output=True, timeout=timeout)
        ac = (r2.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        acodec = ac[0].strip() if ac else ""
    except Exception:
        acodec = ""

    return vcodec, acodec


CHUNK_SIZE = 8 * 1024 * 1024
FIRST_CHUNK_BYTES = 16 * 1024 * 1024      # 先给 16MB 就开转（实测 3 秒出片）
TOTAL_CHUNK_BYTES = 96 * 1024 * 1024      # 后台补到 96MB      # 每块 8MB（夸克约 32MB 断连，8MB 安全）


def _download_range(stream_url, start, length, timeout=60):
    """下载 [start, start+length) 这一段，失败重试 3 次"""
    import requests as _rq
    sess = getattr(_download_range, "_sess", None)
    if sess is None:
        sess = _rq.Session()
        sess.trust_env = False
        sess.proxies = {"http": None, "https": None}
        _download_range._sess = sess

    end = start + length - 1
    hdrs = {"Range": "bytes=%d-%d" % (start, end)}
    for attempt in range(3):
        try:
            r = sess.get(stream_url, headers=hdrs, stream=True, timeout=(10, timeout))
            if r.status_code not in (200, 206):
                r.close()
                continue
            buf = bytearray()
            try:
                for ch in r.iter_content(chunk_size=256 * 1024):
                    if ch:
                        buf.extend(ch)
                        if len(buf) >= length:
                            break
            except Exception:
                pass
            finally:
                r.close()
            if len(buf) >= min(length, 64 * 1024):
                return bytes(buf[:length])
        except Exception:
            pass
        time.sleep(0.4 * (attempt + 1))
    return None


def _parse_mp4_boxes(head_bytes, file_size, stream_url):
    """
    解析 MP4 顶层 box，返回 [(type, offset, size), ...]
    遇到 size=0 或 1 的 box 用实际计算
    """
    boxes = []
    pos = 0
    n = len(head_bytes)
    while pos + 8 <= n:
        size = int.from_bytes(head_bytes[pos:pos + 4], "big")
        typ = head_bytes[pos + 4:pos + 8].decode("latin-1", "ignore")
        hdr = 8
        if size == 1:
            if pos + 16 > n:
                break
            size = int.from_bytes(head_bytes[pos + 8:pos + 16], "big")
            hdr = 16
        elif size == 0:
            size = file_size - pos
        if size < hdr or typ.strip("\x00") == "":
            break
        boxes.append((typ, pos, size))
        pos += size
    return boxes


def download_head_streaming(stream_url, first_bytes, total_bytes, outdir,
                            on_first_ready=None, stop_flag=None):
    """
    先下 first_bytes，回调通知可以开始转码，然后继续下到 total_bytes。
    返回 (path, got)
    """
    path = os.path.join(outdir, "src.bin")
    got = 0
    notified = False
    with open(path, "wb") as f:
        while got < total_bytes:
            if stop_flag and stop_flag():
                break
            want = min(CHUNK_SIZE, total_bytes - got)
            data = _download_range(stream_url, got, want)
            if not data:
                break
            f.write(data)
            f.flush()
            got += len(data)

            # 够第一段了，通知调用方启动 ffmpeg
            if not notified and got >= first_bytes:
                notified = True
                if on_first_ready:
                    on_first_ready(path, got)

            if len(data) < want:
                break
    if not notified and on_first_ready and got > 0:
        on_first_ready(path, got)
    return path, got


def build_readable_mp4(stream_url, outdir, want_bytes=120 * 1024 * 1024,
                       progress=None):
    """
    拼一个 ffmpeg 能直接打开的本地 MP4：
      moov 前置 + mdat 开头一段
    返回 (path, mdat_start_data_bytes) 或 (None, 0)
    """
    path = os.path.join(outdir, "src.mp4")

    # ① 取文件头，找 box 布局
    head = _download_range(stream_url, 0, 65536)
    if not head:
        return None, 0

    file_size = None
    # 先问一下总长度
    try:
        import requests as _rq
        r = _rq.head(stream_url, timeout=15)
        cl = r.headers.get("Content-Range") or ""
        if "/" in cl:
            file_size = int(cl.split("/")[-1])
        elif r.headers.get("Content-Length"):
            file_size = int(r.headers["Content-Length"])
        r.close()
    except Exception:
        pass
    if not file_size:
        return None, 0

    boxes = _parse_mp4_boxes(head, file_size, stream_url)
    moov = next((b for b in boxes if b[0] == "moov"), None)
    mdat = next((b for b in boxes if b[0] == "mdat"), None)

    if not moov:
        # moov 在末尾：从 file_size - 合理范围里倒着找
        probe = min(8 * 1024 * 1024, file_size)
        tail = _download_range(stream_url, file_size - probe, probe)
        if not tail:
            return None, 0
        idx = tail.rfind(b"moov")
        if idx < 4:
            return None, 0
        msize = int.from_bytes(tail[idx - 4:idx], "big")
        moff = file_size - probe + idx - 4
        if msize < 8 or msize > probe:
            msize = probe - (idx - 4)
        moov = ("moov", moff, msize)

    # ② 下 moov
    moov_data = _download_range(stream_url, moov[1], moov[2])
    if not moov_data:
        return None, 0
    if progress:
        progress(len(moov_data), want_bytes)

    # ③ 下 ftyp（如果有）和 mdat 开头
    ftyp_data = b""
    ft = next((b for b in boxes if b[0] == "ftyp"), None)
    if ft:
        ftyp_data = _download_range(stream_url, ft[1], ft[2]) or b""

    mdat_start = mdat[1] if mdat else 0
    mdat_hdr = _download_range(stream_url, mdat_start, 8) if mdat else None

    need = want_bytes - len(moov_data) - len(ftyp_data)
    body = _download_range(stream_url, mdat_start, max(need, 1024 * 1024))
    if not body:
        return None, 0
    if progress:
        progress(len(moov_data) + len(body), want_bytes)

    # ④ 拼接：ftyp + moov + mdat
    with open(path, "wb") as f:
        f.write(ftyp_data)
        f.write(moov_data)
        f.write(body)
    return path, len(body)


def download_head(stream_url, limit_bytes, outdir, progress=None):
    """
    分块下载文件开头 limit_bytes 字节到本地。
    progress(got, total) 回调
    """
    path = os.path.join(outdir, "src.bin")
    got = 0
    with open(path, "wb") as f:
        while got < limit_bytes:
            want = min(CHUNK_SIZE, limit_bytes - got)
            data = _download_range(stream_url, got, want)
            if not data:
                break
            f.write(data)
            got += len(data)
            if progress:
                progress(got, limit_bytes)
            if len(data) < want:
                break        # 到文件尾了
    return (path, got) if got > 0 else (None, 0)


def find_by_fid(fid):
    """按 fid 找正在跑的任务，复用避免重复起 ffmpeg"""
    _cleanup_old()
    for tid, t in list(TASKS.items()):
        if t.get("fid") == fid:
            proc = t.get("proc")
            if proc and proc.poll() is None:
                t["touched"] = time.time()
                return tid
            if os.path.exists(os.path.join(t["dir"], "index.m3u8")):
                t["touched"] = time.time()
                return tid
    return None



def start(fid, hls_time=4, force_copy=False):
    """
    启动 HLS 转码（定稿版）
    返回 {ok, task, error, reused}
    """
    _cleanup_old()

    exist = find_by_fid(fid)
    if exist:
        return {"ok": True, "task": exist, "reused": True}

    if len(TASKS) >= _MAX_TASKS:
        oldest = min(TASKS.items(), key=lambda kv: kv[1]["touched"])[0]
        stop(oldest)

    url, err = PL.direct_url(fid)
    if not url:
        return {"ok": False, "error": err or "拿不到直链"}

    outdir = tempfile.mkdtemp(prefix="hls_")
    playlist = os.path.join(outdir, "index.m3u8")
    seg = os.path.join(outdir, "seg%d.ts")

    # 决定了源文件是 mp4 还是 mkv
    name = ""
    try:
        info = PL.file_info(fid)
        name = (info or {}).get("file_name") or ""
    except Exception:
        pass
    is_mp4 = name.lower().endswith((".mp4", ".m4v", ".mov"))

    stream_url = _LOCAL_STREAM + "?fid=" + fid

    # ① 本地落下源文件
    #    mk4/mkv 索引位置不同：mp4 要把 moov 前置，mkv 直接下开头
    if is_mp4:
        src, mdat_len = build_readable_mp4(stream_url, outdir,
                                           want_bytes=TOTAL_CHUNK_BYTES)
    else:
        src, got = download_head(stream_url, FIRST_CHUNK_BYTES, outdir)

    if not src:
        shutil.rmtree(outdir, ignore_errors=True)
        return {"ok": False, "error": "拉取片源数据失败"}

    # ② 探测编码，决定要不要重编码
    vcodec, acodec = probe_codec(src, "")
    copy_v = force_copy or vcodec in ("h264", "hevc", "h265")

    # 估算已下载数据够播多久，限定时长，避免 ffmpeg 读到 EOF 报错
    seg_secs = 0
    try:
        sz = os.path.getsize(src)
        # 4K HEVC 约 5Mbps / 2K 约 2Mbps，取保守 6Mbps 估算
        seg_secs = int(sz * 8 / (6 * 1000 * 1000))
    except Exception:
        seg_secs = 0
    limit_secs = max(seg_secs - 5, 8)

    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
           "-err_detect", "ignore_err", "-fflags", "+discardcorrupt+genpts",
           "-i", src,
           "-t", str(limit_secs),
           "-map", "0:v:0", "-map", "0:a:0?"]

    if copy_v:
        cmd += ["-c:v", "copy"]
    else:
        enc, enc_args = _pick_encoder()
        cmd += ["-hwaccel", "auto", "-c:v", enc] + enc_args + [
            "-pix_fmt", "yuv420p",
            "-force_key_frames", "expr:gte(t,n_forced*%d)" % hls_time,
            "-sc_threshold", "0",
        ]

    # 音频：能 copy 就 copy（强转会因源音频损坏导致整任务失败）
    if acodec in ("aac", "mp3"):
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]

    cmd += ["-f", "hls",
            "-hls_time", str(hls_time),
            "-hls_list_size", "0",
            "-hls_segment_filename", seg,
            playlist]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as ex:
        shutil.rmtree(outdir, ignore_errors=True)
        return {"ok": False, "error": "启动 ffmpeg 失败: %s" % str(ex)[:100]}

    tid = uuid.uuid4().hex[:12]
    with _LOCK:
        TASKS[tid] = {
            "dir": outdir, "proc": proc, "fid": fid,
            "created": time.time(), "touched": time.time(),
            "log": [], "vcodec": vcodec, "acodec": acodec,
            "copied": copy_v, "src": src,
        }

    def _drain():
        try:
            for line in proc.stderr:
                t = line.decode("utf-8", "ignore").strip()
                if t:
                    ent = TASKS.get(tid)
                    if ent is not None:
                        ent["log"].append(t[:200])
                        if len(ent["log"]) > 40:
                            ent["log"] = ent["log"][-40:]
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return {"ok": True, "task": tid, "dir": outdir, "vcodec": vcodec,
            "acodec": acodec, "copied": copy_v}



def task_dir(tid):
    t = TASKS.get(tid)
    if not t:
        return None
    t["touched"] = time.time()
    return t["dir"]


def playlist_path(tid):
    d = task_dir(tid)
    if not d:
        return None
    p = os.path.join(d, "index.m3u8")
    return p if os.path.exists(p) else None


def segment_path(tid, name):
    d = task_dir(tid)
    if not d:
        return None
    # 只允许 seg<数字>.ts，防目录穿越
    if not re.fullmatch(r"seg\d+\.ts", name):
        return None
    p = os.path.join(d, name)
    return p if os.path.exists(p) else None


def status(tid):
    t = TASKS.get(tid)
    if not t:
        return {"ok": False, "error": "任务不存在"}
    d = t["dir"]
    segs = [f for f in os.listdir(d) if f.endswith(".ts")] if os.path.isdir(d) else []
    proc = t["proc"]
    return {
        "ok": True,
        "task": tid,
        "segments": len(segs),
        "ready": os.path.exists(os.path.join(d, "index.m3u8")),
        "running": proc.poll() is None if proc else False,
        "vcodec": t.get("vcodec"),
        "copied": t.get("copied"),
        "log": t.get("log", [])[-5:],
    }
