# -*- coding: utf-8 -*-
"""
PanSou 随主服务自动启动

背景：网盘搜索依赖本地 PanSou（http://127.0.0.1:8888）。
      以前要手动启动，忘启动就只剩缓存，表现为"搜索很慢/没结果"。

行为：
  · 启动 Flixer 时检查 8888；没起就拉起 F:\\新建文件夹\\PanSou\\pansou-main\\pansou.exe
  · 带上 quark 相关的插件列表（默认不带任何插件，等于空搜）
  · 起不来不影响主服务，搜索会退化为缓存
"""
import os
import socket
import subprocess
import time

PANSOU_DIR = r"F:\新建文件夹\PanSou\pansou-main"
PANSOU_EXE = os.path.join(PANSOU_DIR, "pansou.exe")
HEALTH = "http://127.0.0.1:8888/api/health"

# 只启用夸克/通用检索类插件（目录名即插件名）
PLUGINS = ",".join([
    "qupansou", "qupanshe", "quarksoo", "quarktv", "quark4k", "quarkres",
    "jupansou", "pansearch", "panlian", "panwiki", "panta", "yunso",
])

_proc = None


def _port_open(port=8888, host="127.0.0.1", timeout=0.25):
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def running():
    return _port_open()


def ensure(wait=20.0, quiet=True):
    """确保 PanSou 在跑；返回 True 表示可用"""
    global _proc
    if running():
        return True
    if not os.path.exists(PANSOU_EXE):
        return False
    env = dict(os.environ)
    env.setdefault("ENABLED_PLUGINS", PLUGINS)
    env.setdefault("CACHE_TTL", "30m")
    env.setdefault("PLUGIN_TIMEOUT", "12s")
    # 搜索链路允许走代理（播放链路必须直连，两者互不影响）
    _px = os.environ.get("FLIXER_SEARCH_PROXY") or "http://127.0.0.1:7897"
    try:
        import socket as _sk
        with _sk.socket() as _s2:
            _s2.settimeout(0.25)
            _alive = _s2.connect_ex(("127.0.0.1", int(_px.rsplit(":", 1)[1]))) == 0
    except Exception:
        _alive = False
    if _alive:
        env.setdefault("HTTP_PROXY", _px)
        env.setdefault("HTTPS_PROXY", _px)
        env.setdefault("ALL_PROXY", _px)
        env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    # 走代理后单次请求 6-7 秒，默认 4 秒必然超时 → 全部放宽
    env.setdefault("ASYNC_RESPONSE_TIMEOUT", "15s")
    env.setdefault("PLUGIN_BATCH_TIMEOUT_SECONDS", "20")
    env.setdefault("TG_CHANNEL_TIMEOUT_SECONDS", "20")
    env.setdefault("TG_CHANNEL_REQUEST_TIMEOUT_SECONDS", "15")
    env.setdefault("ASYNC_PLUGIN_ENABLED", "true")
    try:
        _proc = subprocess.Popen(
            [PANSOU_EXE],
            cwd=PANSOU_DIR,
            env=env,
            stdout=open(os.path.join(PANSOU_DIR, "run.log"), "ab"),
            stderr=open(os.path.join(PANSOU_DIR, "run.err"), "ab"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if running():
            return True
        time.sleep(0.4)
    return running()
