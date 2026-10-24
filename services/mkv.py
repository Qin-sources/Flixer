"""
MKV (Matroska) 字幕提取 —— 纯 Python，不依赖 ffmpeg

为什么自己写：
  本机 ffmpeg 是纯音频裁剪版（--disable-decoders，无字幕解码器、无 webvtt 封装），用不了。
  而 MKV 本身是 EBML 容器，字幕就是 Clusters 里的 Block，直接解析字节就能拿到。

为什么快：
  视频块体积巨大但不含字幕。我们只读「块头」判断类型，
  遇到不需要的块直接按 size 跳过（Range 请求换偏移），不下载视频数据。
  一部 16GB 的 4K 片子，实际只需读几十 MB。

支持的编码：
  S_TEXT/UTF8   → SRT
  S_TEXT/ASS    → ASS
  S_TEXT/SSA    → SSA
  S_HDMV/PGS    → 位图字幕，跳过（需要 OCR，不做）
  S_VOBSUB      → 位图，跳过
"""
import logging
import re
import struct

log = logging.getLogger("mkv")

# ── EBML 元素 ID
EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEKHEAD = 0x114D9B74
SEEK = 0x4DBB
SEEK_ID = 0x53AB
SEEK_POS = 0x53AC
INFO = 0x1549A966
TIMECODE_SCALE = 0x2AD7B1
DURATION = 0x4489
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_TYPE = 0x83
TRACK_NAME = 0x536E
TRACK_LANG = 0x22B59C
CODEC_ID = 0x86
CODEC_PRIVATE = 0x63A2
DEFAULT_DURATION = 0x23E383
CLUSTER = 0x1F43B675
CLUSTER_TIME = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
BLOCK_DURATION = 0x9B

TEXT_CODECS = ("S_TEXT/UTF8", "S_TEXT/ASCII", "S_TEXT/ASS", "S_TEXT/SSA", "S_SSA", "S_ASS")
BITMAP_CODECS = ("S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB", "S_HDMV/TEXTST")

TRACK_TYPE_SUB = 0x11


# ------------------------------------------------------------------ 基础读取

def vint(buf, pos, keep_marker=False):
    """读 EBML 变长整数，返回 (value, next_pos)"""
    if pos >= len(buf):
        return None, pos
    b = buf[pos]
    if b == 0:
        return None, pos + 1
    length = 1
    mask = 0x80
    while not (b & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None, pos + 1
    val = b if keep_marker else (b & (mask - 1))
    for i in range(1, length):
        if pos + i >= len(buf):
            return None, pos
        val = (val << 8) | buf[pos + i]
    return val, pos + length


def read_id(buf, pos):
    """元素 ID：保留 marker 位的 vint"""
    return vint(buf, pos, keep_marker=True)


def read_uint(buf):
    v = 0
    for b in buf:
        v = (v << 8) | b
    return v


def read_str(buf):
    return buf.split(b"\x00")[0].decode("utf-8", "ignore").strip()


def read_float(buf):
    if len(buf) == 4:
        return struct.unpack(">f", buf)[0]
    if len(buf) == 8:
        return struct.unpack(">d", buf)[0]
    return 0.0


def iter_children(buf, pos, end):
    """遍历一段数据里的子元素，产出 (id, data_start, data_end)"""
    while pos < end:
        eid, p1 = read_id(buf, pos)
        if eid is None:
            break
        size, p2 = vint(buf, p1)
        if size is None:
            break
        d0 = p2
        d1 = d0 + size
        if d1 > end:
            d1 = end
        yield eid, d0, d1
        pos = d1


# ------------------------------------------------------------------ Range 读取器

class RangeReader:
    """
    带窗口缓存的随机读取器。

    fetch(offset, length) -> bytes 由一个外部函数提供（走我们的 /api/stream）。
    """

    def __init__(self, fetch, total, window=1024 * 1024):
        self.fetch = fetch
        self.total = total
        self.window = window
        self._buf = b""
        self._base = 0
        self.bytes_read = 0
        self.requests = 0

    def read(self, offset, length):
        if length <= 0:
            return b""
        if offset >= self.total:
            return b""
        length = min(length, self.total - offset)
        end = offset + length
        if self._base <= offset and end <= self._base + len(self._buf):
            s = offset - self._base
            return self._buf[s:s + length]
        # 换窗口：读 offset 起 window 字节（至少覆盖 length）
        want = max(self.window, length)
        data = self.fetch(offset, want) or b""
        self.bytes_read += len(data)
        self.requests += 1
        self._buf = data
        self._base = offset
        return data[:length]

    def skip(self, n):
        """跳过 n 字节（不下载）"""
        self._base = self._base + len(self._buf) + max(0, n - 0)  # 无效化窗口
        self._buf = b""

    def seek_to(self, offset):
        self._base = offset
        self._buf = b""


# ------------------------------------------------------------------ 探测

def _parse_track_entry(buf, start, end):
    t = {"number": None, "type": None, "codec": "", "name": "", "lang": ""}
    for eid, d0, d1 in iter_children(buf, start, end):
        data = buf[d0:d1]
        if eid == TRACK_NUMBER:
            t["number"] = read_uint(data)
        elif eid == TRACK_TYPE:
            t["type"] = read_uint(data)
        elif eid == CODEC_ID:
            t["codec"] = read_str(data)
        elif eid == TRACK_NAME:
            t["name"] = read_str(data)
        elif eid == TRACK_LANG:
            t["lang"] = read_str(data)
    return t


def probe(reader, progress=None):
    """
    只读文件头，返回：
      {ok, tracks:[{number,codec,name,lang,type}], duration, timecode_scale, segment_data_start}
    """
    head = reader.read(0, 512 * 1024)
    if len(head) < 4:
        return {"ok": False, "error": "读不到文件头"}

    eid, p = read_id(head, 0)
    if eid != EBML:
        # 可能不是 MKV
        return {"ok": False, "error": "不是 MKV 容器", "magic": head[:8].hex()}

    size, p2 = vint(head, p)
    pos = p2 + size          # 跳过 EBML 头

    out = {"ok": True, "tracks": [], "duration": 0, "scale": 1000000,
           "segment_start": 0, "tracks_offset": None}

    # 找 Segment
    while pos < len(head):
        eid, p1 = read_id(head, pos)
        if eid is None:
            break
        size, p2 = vint(head, p1)
        if size is None:
            break
        if eid == SEGMENT:
            out["segment_start"] = p2
            out["segment_size"] = size
            pos = p2
            break
        pos = p2 + size
    else:
        return {"ok": False, "error": "找不到 Segment"}

    # 在 Segment 里找 Info / Tracks / SeekHead
    seg0 = out["segment_start"]
    seg_end = min(seg0 + (out.get("segment_size") or 0), seg0 + 32 * 1024 * 1024)
    buf = reader.read(seg0, min(seg_end - seg0, 4 * 1024 * 1024)) if seg_end > seg0 else head[pos:]
    base = seg0

    seek_positions = {}
    i = 0
    while i < len(buf):
        eid, p1 = read_id(buf, i)
        if eid is None:
            break
        size, p2 = vint(buf, p1)
        if size is None:
            break
        if eid == SEEKHEAD:
            for se, sd0, sd1 in iter_children(buf, p2, min(p2 + size, len(buf))):
                if se == SEEK:
                    sid = spos = None
                    for x, a0, a1 in iter_children(buf, sd0, sd1):
                        if x == SEEK_ID:
                            sid, _ = read_id(buf[a0:a1], 0)
                        elif x == SEEK_POS:
                            spos = read_uint(buf[a0:a1])
                    if sid is not None and spos is not None:
                        seek_positions[sid] = spos
        elif eid == INFO:
            for x, a0, a1 in iter_children(buf, p2, min(p2 + size, len(buf))):
                data = buf[a0:a1]
                if x == TIMECODE_SCALE:
                    out["scale"] = read_uint(data) or 1000000
                elif x == DURATION:
                    out["duration"] = read_float(data)
        elif eid == TRACKS:
            out["tracks_offset"] = base + p2
            for te, t0, t1 in iter_children(buf, p2, min(p2 + size, len(buf))):
                if te == TRACK_ENTRY:
                    out["tracks"].append(_parse_track_entry(buf, t0, t1))
            break
        elif eid == CLUSTER:
            break
        i = p2 + size

    # Tracks 不在前 4MB 里：顺着 SeekHead 跳过去
    if not out["tracks"] and TRACKS in seek_positions:
        want = seek_positions[TRACKS]
        tb = reader.read(seg0 + want, 512 * 1024)
        i = 0
        eid, p1 = read_id(tb, i)
        if eid == TRACKS:
            size, p2 = vint(tb, p1)
            out["tracks_offset"] = seg0 + want + p2
            for te, t0, t1 in iter_children(tb, p2, min(p2 + size, len(tb))):
                if te == TRACK_ENTRY:
                    out["tracks"].append(_parse_track_entry(tb, t0, t1))

    out["subtitle_tracks"] = []
    for t in out["tracks"]:
        if t.get("type") != TRACK_TYPE_SUB:
            continue
        codec = (t.get("codec") or "").upper()
        item = dict(t)
        if any(codec.startswith(c.split("/")[0]) and c in codec for c in TEXT_CODECS) or \
           any(codec == c for c in TEXT_CODECS):
            item["kind"] = "text"
        elif any(c in codec for c in BITMAP_CODECS):
            item["kind"] = "bitmap"
        else:
            item["kind"] = "unknown"
        out["subtitle_tracks"].append(item)

    return out
