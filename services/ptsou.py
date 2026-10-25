# -*- coding: utf-8 -*-
"""
pansou.top 结果源（纯 HTTP，无需浏览器）

流程：
  1) GET /api/other/web_search?title=<kw>&is_type=0   返回 SSE 流
     data: {"title": "...", "url": "<加密串>", "password": ""}
  2) POST /api/other/save_url {"url": "<加密串>"}
     → {"data": {"url": "https://pan.quark.cn/s/xxxx"}}

只保留夸克（我们的播放链路只支持夸克）。
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from urllib.parse import quote

BASE = "https://pansou.top"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_S = requests.Session()
_S.trust_env = False
_S.proxies = {"http": None, "https": None}

_QUARK = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+")


def _headers(referer=""):
    h = {"User-Agent": UA, "Accept": "text/event-stream"}
    if referer:
        h["Referer"] = referer
    return h


def stream(kw, limit=30, timeout=25):
    """拉 SSE，返回 [{title, enc, password}]"""
    out = []
    url = BASE + "/api/other/web_search"
    try:
        ref = BASE + "/s/" + quote(str(kw))
        r = _S.get(url, params={"title": kw, "is_type": 0},
                   headers=_headers(ref), stream=True, timeout=timeout)
        if r.status_code != 200:
            return []
        r.encoding = "utf-8"
        t0 = time.time()
        buf = ""
        for chunk in r.iter_content(chunk_size=2048):
            if not chunk:
                continue
            buf += chunk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    j = json.loads(line[5:].strip())
                except Exception:
                    continue
                if j.get("title") and j.get("url"):
                    out.append(j)
            if len(out) >= limit or time.time() - t0 > timeout:
                break
    except Exception:
        pass
    return out[:limit]


def decrypt(enc, timeout=12):
    try:
        r = _S.post(BASE + "/api/other/save_url",
                    json={"url": enc, "title": ""},
                    headers={**_headers(BASE + "/"), "Content-Type": "application/json",
                             "Accept": "application/json"},
                    timeout=timeout)
        j = r.json()
        u = ((j.get("data") or {}) or {}).get("url") or ""
        if _QUARK.match(u or ""):
            return u
    except Exception:
        pass
    return ""


def locate(kw, limit=30, workers=8):
    """返回 [{title, url, passcode}]，只含夸克"""
    raw = stream(kw, limit=limit)
    if not raw:
        return []
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(decrypt, it["url"]): it for it in raw}
        for f in futs:
            it = futs[f]
            try:
                real = f.result(timeout=20)
            except Exception:
                real = ""
            if not real:
                continue
            rows.append({
                "title": it.get("title") or "",
                "url": real,
                "passcode": (it.get("password") or "").strip(),
            })
    return rows
