"""
夸克扫码登录（CDP 驱动本机 Chrome）

流程：
  1. 后端启动一个独立 profile 的 Chrome 窗口，直接打开夸克登录页
  2. 用户在里面扫码（手机夸克 App 扫）
  3. 后端通过 CDP 的 Network.getAllCookies 读出登录 cookie
  4. 验证通过后保存，并关掉那个 Chrome 窗口

这样用户不需要手动复制 cookie，点一下按钮就弹出扫码页。
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time

import requests
import websocket

from . import quark_auth as QA

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

PORT = 9223                      # 专用调试端口，避免和别的工具冲突
LOGIN_URL = "https://pan.quark.cn/"

_proc = None
_profile = None
_started_at = 0.0


def _chrome_path():
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _port_open(port, timeout=0.25):
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _cdp_tabs():
    try:
        r = requests.get("http://127.0.0.1:%d/json" % PORT, timeout=3)
        return r.json()
    except Exception:
        return []


def _cdp_call(ws, method, params=None, msg_id=1, timeout=5.0):
    """发一条 CDP 命令并按 id 匹配响应，忽略中间事件流"""
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except Exception:
            return None
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("id") == msg_id:
            return msg.get("result") or msg.get("error")
    return None


def _get_cookies():
    """通过 CDP 读取所有 cookie"""
    tabs = [t for t in _cdp_tabs() if t.get("type") == "page"]
    if not tabs:
        return None
    ws_url = tabs[0].get("webSocketDebuggerUrl")
    if not ws_url:
        return None
    try:
        ws = websocket.create_connection(ws_url, timeout=8, suppress_origin=True)
    except Exception:
        return None
    try:
        _cdp_call(ws, "Network.enable", {}, 1, timeout=3)
        # Storage.getCookies 不需要 urls，一次性返回全部
        res = _cdp_call(ws, "Storage.getCookies", {}, 2, timeout=5)
        if isinstance(res, dict) and "cookies" in res:
            return res["cookies"]
        # 兜底：Network.getAllCookies
        res2 = _cdp_call(ws, "Network.getAllCookies", {}, 3, timeout=5)
        if isinstance(res2, dict) and "cookies" in res2:
            return res2["cookies"]
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


# 判定「真的登录了」的 cookie 名
KEY_COOKIES = ("__pus", "__puus", "__kp", "b-user-id")


def collect_cookie_string():
    """
    保存【全部】quark.cn 域 cookie。
    夸克的鉴权不只依赖 __pus，缺字段会在过期校验时失效，
    所以这里不做挑选，整域全收。
    """
    cookies = _get_cookies()
    if not cookies:
        return ""
    picked = []
    seen = set()
    for c in cookies:
        name = c.get("name") or ""
        dom = (c.get("domain") or "").lower()
        if not name or name in seen:
            continue
        # 只要夸克域，或者是我们明确需要的几个
        if ("quark" in dom) or (name in KEY_COOKIES):
            picked.append("%s=%s" % (name, c.get("value", "")))
            seen.add(name)
    return "; ".join(picked)


def is_running():
    return _proc is not None and _proc.poll() is None


def start():
    """启动带调试端口的 Chrome，打开夸克登录页"""
    global _proc, _profile, _started_at

    if is_running():
        return {"ok": True, "already": True}

    exe = _chrome_path()
    if not exe:
        return {"ok": False, "error": "没找到 Chrome，请确认已安装"}

    # 若端口被占（残留进程），先清掉
    if _port_open(PORT):
        stop()
        time.sleep(0.4)

    _profile = tempfile.mkdtemp(prefix="quarklogin_")
    args = [
        exe,
        "--remote-debugging-port=%d" % PORT,
        "--remote-allow-origins=*",      # Chrome 111+ 默认拒绝来自本地页面的 CDP 连接
        "--user-data-dir=%s" % _profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--new-window",
        LOGIN_URL,
    ]
    try:
        _proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return {"ok": False, "error": "启动 Chrome 失败: %s" % str(e)[:80]}

    _started_at = time.time()
    for _ in range(80):                     # 最多等 20 秒
        if _port_open(PORT):
            time.sleep(0.5)                 # 再给页面一点加载时间
            return {"ok": True}
        if _proc.poll() is not None:
            return {"ok": False, "error": "Chrome 已退出"}
        time.sleep(0.25)

    return {"ok": False, "error": "Chrome 调试端口未就绪"}


def poll():
    """
    轮询一次。返回：
      {state: "idle"|"waiting"|"logged_in"|"error", ...}
    """
    if not is_running():
        return {"state": "idle"}

    ck = collect_cookie_string()
    if not ck or "__pus" not in ck:
        return {"state": "waiting"}

    v = QA.verify(ck)
    if v.get("logged_in"):
        QA.save_cookie(ck)
        stop()
        return {
            "state": "logged_in",
            "nick": v.get("nick"),
            "vip": v.get("vip"),
        }
    return {"state": "waiting"}


def stop():
    """关掉登录用的 Chrome 窗口并清理临时 profile"""
    global _proc, _profile
    if _proc is not None:
        try:
            if _proc.poll() is None:
                _proc.terminate()
                try:
                    _proc.wait(timeout=4)
                except Exception:
                    _proc.kill()
        except Exception:
            pass
    _proc = None

    if _profile:
        try:
            shutil.rmtree(_profile, ignore_errors=True)
        except Exception:
            pass
    _profile = None


def status():
    return {
        "running": is_running(),
        "elapsed": int(time.time() - _started_at) if is_running() else 0,
        "chrome": bool(_chrome_path()),
    }
