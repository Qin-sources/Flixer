"""
MKV 字幕抽取（顺序流式解析）

为什么顺序读：
  字幕块和视频块交错在整个文件里，要拿全就必须走一遍。
  但只需要「读字节 + 丢视频块」，不解码、不转码，CPU 几乎为零，
  速度上限就是带宽（实测夸克直连 5 MB/s）。

产出：
  边解析边写 WebVTT，写一部分就能用一部分（文件始终是合法的 VTT）。
"""
import logging
import time

from . import mkv

log = logging.getLogger("mkvextract")


class Extractor:
    """增量解析器：往里喂字节，吐出 VTT 文本"""

    def __init__(self, track_number, scale=1000000):
        self.track = int(track_number)
        self.scale = scale or 1000000
        self.buf = b""
        self.pos = 0          # 缓冲区内的解析位置
        self.abs_base = 0     # 缓冲区起点在文件中的绝对偏移
        self.cues = []        # [(start_s, end_s, text)]
        self.cluster_time = 0
        self.last_cluster = None   # (绝对偏移, 簇时间戳)
        self.pending = []     # 尚未确定结束时间的字幕 [(start, text)]
        self.done = False
        self.error = None

    # ---------------- 喂数据
    def feed(self, chunk):
        if not chunk or self.done:
            return
        self.buf += chunk
        try:
            self._parse()
        except Exception as e:
            self.error = str(e)[:120]
        # 压缩缓冲：保留未解析部分
        if self.pos > 4 * 1024 * 1024:
            self.buf = self.buf[self.pos:]
            self.abs_base += self.pos
            self.pos = 0

    # ---------------- 解析
    def _parse(self):
        buf = self.buf
        n = len(buf)
        while True:
            start = self.pos
            eid, p1 = mkv.read_id(buf, start)
            if eid is None:
                return
            size, p2 = mkv.vint(buf, p1)
            if size is None:
                return
            d0, d1 = p2, p2 + size

            # Segment 是整个文件容器（size 等于文件大小），不能等它"读完"，
            # 必须先进入内部继续解析
            if eid == mkv.SEGMENT:
                self.pos = d0
                continue

            if d1 > n:
                # 这个元素还没下完，等更多数据
                return

            if eid == mkv.CLUSTER:
                self._parse_cluster(d0, d1)
                self.last_cluster = (self.abs_base + start, self.cluster_time)
                self.pos = d1
            else:
                self.pos = d1
            if self.pos <= start:
                self.pos = start + 1

    def _parse_cluster(self, d0, d1):
        buf = self.buf
        pos = d0
        while pos < d1:
            eid, p1 = mkv.read_id(buf, pos)
            if eid is None:
                return
            size, p2 = mkv.vint(buf, p1)
            if size is None:
                return
            e0, e1 = p2, p2 + size
            if e1 > d1:
                return
            if eid == mkv.CLUSTER_TIME:
                self.cluster_time = mkv.read_uint(buf[e0:e1])
            elif eid == mkv.SIMPLE_BLOCK:
                self._block(buf[e0:e1], None)
            elif eid == mkv.BLOCK_GROUP:
                self._block_group(buf, e0, e1)
            pos = e1

    def _block_group(self, buf, d0, d1):
        pos = d0
        payload = None
        duration = None
        while pos < d1:
            eid, p1 = mkv.read_id(buf, pos)
            if eid is None:
                return
            size, p2 = mkv.vint(buf, p1)
            if size is None:
                return
            e0, e1 = p2, p2 + size
            if e1 > d1:
                return
            if eid == mkv.BLOCK:
                payload = buf[e0:e1]
            elif eid == mkv.BLOCK_DURATION:
                duration = mkv.read_uint(buf[e0:e1])
            pos = e1
        if payload:
            self._block(payload, duration)

    def _block(self, data, duration):
        """data = 一个 Block 的完整内容"""
        pos = 0
        track, p1 = mkv.vint(data, 0)
        if track is None:
            return
        if not isinstance(track, int):
            return
        if p1 + 3 > len(data):
            return
        rel = (data[p1] << 8) | data[p1 + 1]
        if rel >= 0x8000:
            rel -= 0x10000
        flags = data[p1 + 2]
        p_start = p1 + 3

        if track != self.track:
            return

        lacing = (flags >> 1) & 0x03
        if lacing == 0:
            frames = [(p_start, len(data))]
        else:
            frames = self._split_laced(data, p_start, lacing)
            if not frames:
                return

        start_s = (self.cluster_time + rel) * self.scale / 1e9
        dur_s = (duration * self.scale / 1e9) if duration else None

        for f0, f1 in frames:
            raw = data[f0:f1]
            try:
                txt = raw.decode("utf-8", "ignore")
            except Exception:
                txt = ""
            txt = txt.replace("\r\n", "\n").strip("\x00").strip()
            if txt:
                txt = txt.replace("\\N", "\n").replace("\\n", "\n")
                self._add_cue(start_s, dur_s, txt)

    def _split_laced(self, data, pos, lacing):
        """处理 laced Block（多帧）"""
        n = data[pos]
        pos += 1
        if n < 1:
            return []
        frames = []
        if lacing == 1:      # Xiph
            sizes = []
            while len(sizes) < n - 1:
                sz = 0
                while pos < len(data):
                    b = data[pos]
                    pos += 1
                    sz += b
                    if b != 255:
                        break
                sizes.append(sz)
            cur = pos
            for sz in sizes:
                frames.append((cur, cur + sz))
                cur += sz
            frames.append((cur, len(data)))
        elif lacing == 3:    # EBML
            sizes = []
            first = mkv.vint(data, pos, keep_marker=True) if hasattr(mkv, "vint") else (None, pos)
            # EBML lacing：第一帧用无符号 vint，之后是差值
            sz, p = mkv.vint(data, pos)
            if sz is None:
                return []
            pos = p
            sizes.append(sz)
            while len(sizes) < n - 1:
                d, p = mkv.vint(data, pos)
                if d is None:
                    return []
                pos = p
                bias = (1 << (7 * 1))  # 近似：差值按有符号处理
                d -= bias // 2
                sizes.append(sizes[-1] + d)
            cur = pos
            for sz in sizes:
                if sz < 0:
                    return []
                frames.append((cur, cur + sz))
                cur += sz
            frames.append((cur, len(data)))
        else:
            return []
        return [(a, b) for a, b in frames if b > a]

    # ---------------- 时间轴
    def _add_cue(self, start, dur, text):
        if dur is None and self.pending:
            # 用下一条的开始时间当作上一条的结束
            s, t = self.pending.pop()
            if start > s:
                self.cues.append((s, start, t))
        if dur:
            self.cues.append((start, start + dur, text))
        else:
            self.pending.append((start, text))
            if len(self.pending) > 1:
                s, t = self.pending.pop(0)
                nxt = self.pending[0][0] if self.pending else start
                if nxt > s:
                    self.cues.append((s, nxt, t))

    def finish(self):
        """收尾：把没有结束时间的字幕补上默认时长"""
        self.done = True
        self.flush_pending()

    def flush_pending(self):
        while self.pending:
            s, t = self.pending.pop(0)
            self.cues.append((s, s + 3.0, t))

    # ---------------- 输出
    def to_vtt(self):
        self.flush_pending()
        cues = sorted(self.cues, key=lambda x: x[0])
        out = ["WEBVTT", ""]
        for a, b, t in cues:
            out.append("%s --> %s" % (_fmt(a), _fmt(b)))
            out.extend(t.split("\n"))
            out.append("")
        return "\n".join(out)


def _fmt(sec):
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return "%02d:%02d:%06.3f" % (h, m, s)
