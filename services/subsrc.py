"""
在线中文字幕（assrt.net / 射手网伪站）

为什么用它：
  内嵌字幕必须读完整部片子（4GB ≈ 15-25 分钟）；
  外挂字幕一条 50-100 KB，几秒下完 —— 这才是「秒出」的正解。

实测链路（2026-10）：
  搜索  https://assrt.net/sub/?searchword=<片名>        → 结果页
  详情  https://assrt.net/xml/sub/<a>/<id>.xml          → HTML（含下载链接）
  下载  https://assrt.net/download/<id>/<name>.rar      → 压缩包，无需登录
  解压  7-Zip（本机已装）                               → .srt / .ass
  转换  services.subs.to_vtt                            → WebVTT

缓存：按详情页 URL 缓存转换后的 VTT，之后是毫秒级。
"""
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile

import requests

from . import subs as SUB

log = logging.getLogger("subsrc")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

BASE = "https://assrt.net"
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi")
ARCHIVE_EXTS = (".rar", ".zip", ".7z")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE_DIR, "data", "subs")

_SEVEN_ZIP = None
for _p in (r"C:\Program Files\7-Zip\7z.exe",
           r"C:\Program Files (x86)\7-Zip\7z.exe",
           r"C:\Program Files\WinRAR\UnRAR.exe"):
    if os.path.exists(_p):
        _SEVEN_ZIP = _p
        break

_SESS = None
_LAST = [0.0]
_GAP = 0.8
_MEM = {}          # 搜索缓存 memory
_MEM_TTL = 3600


def session():
    global _SESS
    if _SESS is None:
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
        s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": BASE + "/",
        })
        _SESS = s
    return _SESS


def _gate():
    wait = _LAST[0] + _GAP - time.time()
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.time()


# ------------------------------------------------------------------ 搜索

def search(kw, limit=20):
    """
    搜索字幕条目。返回 [{id, title, version, detail, langs, files, size}]
    """
    kw = (kw or "").strip()
    if not kw:
        return []
    key = "s:" + kw
    hit = _MEM.get(key)
    if hit and time.time() - hit[0] < _MEM_TTL:
        return hit[1]

    from bs4 import BeautifulSoup

    url = BASE + "/sub/?searchword=" + urllib.parse.quote(kw)
    _gate()
    try:
        r = session().get(url, timeout=20)
    except Exception as e:
        log.info("assrt 搜索失败: %s", str(e)[:100])
        return []
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()

    # 结果条目：链接指向 /xml/sub/<a>/<id>.xml
    for a in soup.find_all("a", href=True):
        h = a["href"]
        m = re.search(r"/xml/sub/(\d+)/(\d+)\.xml", h)
        if not m:
            continue
        if m.group(2) in seen:
            continue
        seen.add(m.group(2))
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        out.append({
            "id": m.group(2),
            "title": title[:120],
            "detail": urllib.parse.urljoin(BASE, h.split("#")[0]),
            "version": "",
            "langs": "",
            "size": "",
        })
        if len(out) >= limit:
            break

    # 补充：版本号 / 格式 / 体积通常在同一行的文本里
    try:
        for item in soup.select("div.subitem, li, div"):
            pass
    except Exception:
        pass

    _MEM[key] = (time.time(), out)
    return out


# ------------------------------------------------------------------ 详情

def detail(detail_url):
    """
    取详情页：下载链接、文件名、包含哪些字幕
    """
    from bs4 import BeautifulSoup
    _gate()
    try:
        r = session().get(detail_url, timeout=20)
    except Exception as e:
        log.info("assrt 详情失败: %s", str(e)[:80])
        return None
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    dl = None
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "/download/" in h:
            dl = urllib.parse.urljoin(BASE, h)
            break
    if not dl:
        return None

    fname = urllib.parse.unquote(dl.rstrip("/").split("/")[-1])
    # 页面里出现的字幕文件名（判断是否双语/简繁）
    names = re.findall(r"[\w\.\-]+\.(?:srt|ass|ssa|vtt|sub)", r.text)
    langs = ""
    low = r.text
    if "双语" in low:
        langs += "双语 "
    if re.search(r"\.chs\.|简体|简中", low):
        langs += "简 "
    if re.search(r"\.cht\.|繁体|繁中", low):
        langs += "繁 "

    # 体积
    size = ""
    m = re.search(r"下载字幕[^)]{0,20}?(\d+(?:\.\d+)?\s*[KMG]B)", r.text)
    if m:
        size = m.group(1)

    return {"download": dl, "filename": fname, "files": list(dict.fromkeys(names))[:8],
            "langs": langs.strip(), "size": size, "detail": detail_url}


# ------------------------------------------------------------------ 下载/解压

def _download(url, referer=None):
    _gate()
    try:
        r = session().get(url, headers={"Referer": referer or BASE + "/"},
                          timeout=60, allow_redirects=True)
        if r.status_code == 200 and len(r.content) > 800:
            return r.content
    except Exception as e:
        log.info("assrt 下载失败: %s", str(e)[:80])
    return None


def _extract(name, raw):
    """返回 [(内部文件名, bytes)]；不是压缩包就原样返回"""
    low = (name or "").lower()
    if low.endswith(SUB_EXTS):
        return [(name, raw)]

    if low.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            return [(i.filename, zf.read(i)) for i in zf.infolist()
                    if i.filename.lower().endswith(SUB_EXTS) and i.file_size < 8 * 1024 * 1024]
        except Exception as e:
            log.info("zip 解压失败 %s", str(e)[:60])
            return []

    if low.endswith((".rar", ".7z")) and _SEVEN_ZIP:
        tmp = tempfile.mkdtemp(prefix="sub_")
        try:
            src = os.path.join(tmp, "in" + (".rar" if low.endswith(".rar") else ".7z"))
            with open(src, "wb") as f:
                f.write(raw)
            outdir = os.path.join(tmp, "out")
            os.makedirs(outdir, exist_ok=True)
            cmd = [_SEVEN_ZIP, "x", src, "-o" + outdir, "-y", "-bso0", "-bsp0"]
            subprocess.run(cmd, capture_output=True, timeout=60)
            res = []
            for root, _dirs, files in os.walk(outdir):
                for fn in files:
                    if fn.lower().endswith(SUB_EXTS):
                        p = os.path.join(root, fn)
                        try:
                            if os.path.getsize(p) < 8 * 1024 * 1024:
                                res.append((fn, open(p, "rb").read()))
                        except Exception:
                            pass
            return res
        except Exception as e:
            log.info("rar 解压失败 %s", str(e)[:60])
            return []
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return []


def _score(name):
    """挑最合适的字幕：简中优先，双语其次，ass 加分"""
    n = (name or "").lower()
    s = 0
    if ".chs" in n or "chs" in n or "简" in n or "zh-cn" in n:
        s += 6
    if "双语" in n or "bilingual" in n:
        s += 5
    if ".ass" in n or ".ssa" in n:
        s += 2
    if ".cht" in n or "繁" in n or "zh-tw" in n:
        s -= 1
    if "eng" in n or ".en." in n:
        s -= 3
    return s


# ------------------------------------------------------------------ 缓存与主流程

def _cache_path(key):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    h = hashlib.md5((key or "").encode("utf-8")).hexdigest()[:20]
    return os.path.join(_CACHE_DIR, "online_%s.vtt" % h)


def cached_vtt(key):
    p = _cache_path(key)
    try:
        if os.path.exists(p) and os.path.getsize(p) > 60:
            return io.open(p, encoding="utf-8").read()
    except Exception:
        pass
    return None


def fetch(detail_url):
    """
    详情页 → 下载 → 解压 → 选最佳 → 转 VTT
    返回 {ok, name, vtt, langs, cached}
    """
    hit = cached_vtt(detail_url)
    if hit:
        return {"ok": True, "name": "online", "vtt": hit, "cached": True}

    info = detail(detail_url)
    if not info:
        return {"ok": False, "error": "取详情失败"}

    raw = _download(info["download"], referer=detail_url)
    if not raw:
        return {"ok": False, "error": "下载失败"}

    entries = _extract(info["filename"], raw)
    if not entries:
        return {"ok": False, "error": "压缩包里没有字幕文件"}

    entries.sort(key=lambda e: -_score(e[0]))
    for name, data in entries[:4]:
        text = SUB.decode_bytes(data)
        vtt = SUB.to_vtt(text, name)
        if vtt.count("-->") < 5:
            continue
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            io.open(_cache_path(detail_url), "w", encoding="utf-8").write(vtt)
        except Exception:
            pass
        return {"ok": True, "name": name, "vtt": vtt,
                "langs": info.get("langs") or "", "cached": False}

    return {"ok": False, "error": "字幕解析不出内容"}


def has_extractor():
    return bool(_SEVEN_ZIP)
