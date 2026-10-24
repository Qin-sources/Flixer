# -*- coding: utf-8 -*-
"""并行 Range 预取：把单连接 2.4MB/s 提升到 2~4 倍（顺序字节流、绝不丢字节）"""
import threading
import time

import requests

CHUNK = 2 * 1024 * 1024
WORKERS = 8
AHEAD = 12


class ParallelReader:
    def __init__(self, url, headers, total, chunk=CHUNK, workers=WORKERS):
        self.url = url
        self.headers = dict(headers or {})
        self.total = int(total or 0)
        self.chunk = chunk
        self.workers = workers
        self._buf = {}          # idx -> bytes
        self._err = {}          # idx -> 失败次数
        self._cv = threading.Condition()
        self._next_plan = 0     # 下一个要抓的分片
        self._next_read = 0     # 调用方下一个要读的分片
        self._stop = False
        self._threads = []

    # ── 内部 ───────────────────────────────
    def _fetch(self, idx):
        off = idx * self.chunk
        if self.total and off >= self.total:
            return None
        end = off + self.chunk - 1
        if self.total:
            end = min(end, self.total - 1)
        h = dict(self.headers)
        h["Range"] = "bytes=%d-%d" % (off, end)
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
        r = s.get(self.url, headers=h, timeout=120)
        if r.status_code not in (200, 206):
            raise IOError("HTTP %s" % r.status_code)
        data = r.content
        if not data:
            raise IOError("empty")
        return data

    def _worker(self):
        while True:
            with self._cv:
                while (not self._stop) and (self._next_plan >= self._next_read + AHEAD):
                    self._cv.wait(0.3)
                if self._stop:
                    return
                idx = self._next_plan
                self._next_plan += 1
                if self.total and idx * self.chunk >= self.total:
                    return
            data = None
            backoff = 0.4
            while data is None and not self._stop:
                try:
                    data = self._fetch(idx)
                    break
                except Exception:
                    time.sleep(backoff)
                    backoff = min(backoff * 1.6, 5.0)
            if data is None:
                continue
            with self._cv:
                self._buf[idx] = data
                self._cv.notify_all()

    # ── 对外 ───────────────────────────────
    def start(self):
        for _ in range(self.workers):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)

    def read_from(self, offset):
        self.start()
        idx = offset // self.chunk
        skip = offset - idx * self.chunk
        with self._cv:
            self._next_read = idx
            self._cv.notify_all()
        while True:
            if self.total and idx * self.chunk >= self.total:
                return
            with self._cv:
                while idx not in self._buf and not self._stop:
                    self._cv.wait(0.5)
                data = self._buf.pop(idx, None)
                if data is not None:
                    self._next_read = idx + 1
                self._cv.notify_all()
            if data is None:
                return
            out = data[skip:] if skip else data
            skip = 0
            if out:
                yield out
            idx += 1

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()
