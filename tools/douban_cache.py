# -*- coding: utf-8 -*-
"""
豆瓣图片抓取缓存器

背景：豆瓣对部分图片（主要是冷门综艺）返回 EO_Bot_Ssid 反爬挑战页，
requests 拿不到图。这里用 headless Chrome 真正执行那段 JS 挑战，
拿到图片后按 URL 的 hash 存到 data/imgcache/，之后 /api/img 直接读缓存。

用法：
    python tools/douban_cache.py            # 抓取当前所有失败的封面
"""
import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import requests
import websocket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "imgcache")
PORT = 9224

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def chrome_path():
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def cache_key(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def cache_path(url):
    return os.path.join(CACHE_DIR, cache_key(url) + ".jpg")


def _port_open(port, t=0.25):
    try:
        with socket.socket() as s:
            s.settimeout(t)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _cdp(ws, method, params=None, mid=1, timeout=15):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = ws.recv()
        except Exception:
            return None
        if not raw:
            continue
        try:
            m = json.loads(raw)
        except Exception:
            continue
        if m.get("id") == mid:
            return m.get("result") or m.get("error")
    return None


JS_FETCH = """
(async () => {
  const r = await fetch(location.href, {cache: 'reload'});
  const ct = r.headers.get('content-type') || '';
  if (!(ct.includes('image'))) return {ok:false, ct, len:0};
  const b = await r.blob();
  const buf = await new Promise(res => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.readAsDataURL(b);
  });
  return {ok:true, ct, len: b.size, data: buf};
})()
"""


def grab_one(ws, url, tries=3):
    """导航到图片页并取回二进制"""
    _cdp(ws, "Page.enable", {}, 100)
    for attempt in range(tries):
        _cdp(ws, "Page.navigate", {"url": url}, 200)
        # 给反爬 JS 执行 + 自动 reload 留时间
        time.sleep(2.2 + attempt * 1.5)
        res = _cdp(ws, "Runtime.evaluate", {
            "expression": JS_FETCH,
            "awaitPromise": True,
            "returnByValue": True,
        }, 300, timeout=25)
        val = ((res or {}).get("result") or {}).get("value") or {}
        if val.get("ok") and val.get("data", "").startswith("data:"):
            b64 = val["data"].split(",", 1)[1]
            try:
                return base64.b64decode(b64), len(b64)
            except Exception:
                pass
    return None, 0


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 1) 找出当前加载失败的封面
    print("[1/3] 扫描首页封面…")
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:7100/api/home", timeout=180).read()
        d = json.loads(raw)
        data = d.get("data") or d
        rows = data.get("rows") or []
    except Exception as e:
        print("      取首页失败:", e)
        rows = []

    bad = []
    for r in rows:
        for it in (r.get("items") or []):
            p = it.get("poster")
            if not p:
                continue
            full = "http://127.0.0.1:7100" + p if p.startswith("/") else p
            try:
                rr = urllib.request.urlopen(full, timeout=25)
                ct = rr.headers.get("Content-Type", "")
                if "image" not in ct:
                    bad.append((it.get("title"), it.get("raw_cover") or p))
            except Exception:
                bad.append((it.get("title"), it.get("raw_cover") or p))

    # 已经是本地缓存的不必再抓
    todo = []
    for title, u in bad:
        if u.startswith("/api/img"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            u = (q.get("u") or [u])[0]
        if not u.startswith("http"):
            continue
        if os.path.exists(cache_path(u)):
            continue
        todo.append((title, u))

    print("      失败 %d 张，需抓取 %d 张" % (len(bad), len(todo)))
    if not todo:
        print("没有需要抓的，结束。")
        return

    # 2) 启 Chrome
    print("[2/3] 启动 Chrome…")
    exe = chrome_path()
    if not exe:
        print("      没找到 Chrome"); return
    profile = tempfile.mkdtemp(prefix="dbfetch_")
    args = [exe, "--headless=old", "--disable-gpu", "--no-sandbox",
            "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
            "--user-data-dir=%s" % profile, "--no-first-run",
            "--disable-features=Translate", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if _port_open(PORT):
            break
        time.sleep(0.25)
    time.sleep(1.0)

    tabs = requests.get("http://127.0.0.1:%d/json" % PORT, timeout=5).json()
    page = next((t for t in tabs if t.get("type") == "page"), None)
    if not page:
        print("      拿不到页面"); proc.terminate(); return

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30,
                                     suppress_origin=True)
    ok = 0
    try:
        print("[3/3] 逐张抓取…")
        for title, u in todo:
            data, _ = grab_one(ws, u)
            if data and len(data) > 2000:
                with open(cache_path(u), "wb") as f:
                    f.write(data)
                ok += 1
                print("      ✓ %-22s %d KB" % (str(title)[:22], len(data)//1024))
            else:
                print("      ✗ %-22s 未取到" % str(title)[:22])
    finally:
        try: ws.close()
        except Exception: pass
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass

    print("\n完成：成功 %d / 待抓 %d" % (ok, len(todo)))
    print("缓存目录:", CACHE_DIR)


if __name__ == "__main__":
    main()
