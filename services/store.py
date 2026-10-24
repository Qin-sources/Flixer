"""
持久化存储（SQLite）

解决两个问题：
  1. 搜索结果只存在内存里，重启就丢；每次搜都要重新打 PanSou（约 3 秒）
  2. 链接有效性也只存内存，重启后不知道哪些片源已经失效

表结构：
  search  key=查询指纹   payload=完整结果 JSON   ts=写入时间   hits=命中次数
  link    key=分享链接   alive/code/error        ts=校验时间

设计原则：
  · 存储层出任何问题都不能影响主流程（全部捕获，失败就当没缓存）
  · WAL 模式，读写并发不打架
  · 只存已经验证过有效性的结果，不做无脑缓存
"""
import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("store")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_BASE, "data")
_DB = os.path.join(_DIR, "flixer.db")

_LOCK = threading.RLock()
_CONN = [None]
_READY = [False]

# 缓存时长
SEARCH_TTL = 60 * 60 * 24        # 搜索结果保留 24 小时
LINK_TTL = 60 * 60               # 链接有效性保留 1 小时（和内存一致）


def init():
    """建库建表（幂等）"""
    if _READY[0]:
        return True
    with _LOCK:
        if _READY[0]:
            return True
        try:
            os.makedirs(_DIR, exist_ok=True)
            c = sqlite3.connect(_DB, timeout=10, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("""CREATE TABLE IF NOT EXISTS search(
                key TEXT PRIMARY KEY,
                query TEXT,
                payload TEXT NOT NULL,
                ts REAL NOT NULL,
                hits INTEGER DEFAULT 0
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS link(
                url TEXT PRIMARY KEY,
                alive INTEGER NOT NULL,
                code TEXT,
                error TEXT,
                ts REAL NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_search_ts ON search(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_link_ts ON link(ts)")
            c.commit()
            _CONN[0] = c
            _READY[0] = True
            log.info("SQLite 就绪: %s", _DB)
            return True
        except Exception as e:
            log.warning("SQLite 初始化失败: %s", str(e)[:160])
            return False


def _db():
    if not init():
        return None
    return _CONN[0]


# ------------------------------------------------------------------ 搜索结果

def get_search(key, ttl=SEARCH_TTL):
    """取缓存结果；过期返回 None"""
    c = _db()
    if not c:
        return None
    try:
        with _LOCK:
            row = c.execute(
                "SELECT payload, ts FROM search WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            payload, ts = row
            if time.time() - ts > ttl:
                return None
            c.execute("UPDATE search SET hits=hits+1 WHERE key=?", (key,))
            c.commit()
        return json.loads(payload)
    except Exception as e:
        log.warning("get_search 失败: %s", str(e)[:120])
        return None


def put_search(key, query, payload):
    c = _db()
    if not c:
        return False
    try:
        with _LOCK:
            c.execute(
                "INSERT INTO search(key,query,payload,ts,hits) VALUES(?,?,?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "ts=excluded.ts, query=excluded.query",
                (key, query or "", json.dumps(payload, ensure_ascii=False), time.time()))
            c.commit()
        return True
    except Exception as e:
        log.warning("put_search 失败: %s", str(e)[:120])
        return False


# ------------------------------------------------------------------ 链接有效性

def get_link(url, ttl=LINK_TTL):
    c = _db()
    if not c:
        return None
    try:
        with _LOCK:
            row = c.execute(
                "SELECT alive, code, error, ts FROM link WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        alive, code, error, ts = row
        if time.time() - ts > ttl:
            return None
        return {"alive": bool(alive), "code": code or "", "error": error or "",
                "ts": ts, "cached": True}
    except Exception as e:
        log.warning("get_link 失败: %s", str(e)[:120])
        return None


def put_link(url, alive, code="", error=""):
    c = _db()
    if not c:
        return False
    try:
        with _LOCK:
            c.execute(
                "INSERT INTO link(url,alive,code,error,ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET alive=excluded.alive, "
                "code=excluded.code, error=excluded.error, ts=excluded.ts",
                (url, 1 if alive else 0, code or "", error or "", time.time()))
            c.commit()
        return True
    except Exception as e:
        log.warning("put_link 失败: %s", str(e)[:120])
        return False


# ------------------------------------------------------------------ 维护

def stats():
    """给前端/调试看的统计"""
    c = _db()
    if not c:
        return {"ok": False, "error": "存储未就绪"}
    try:
        with _LOCK:
            s_cnt, s_hits = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits),0) FROM search").fetchone()
            l_cnt, l_dead = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN alive=0 THEN 1 ELSE 0 END),0) "
                "FROM link").fetchone()
        size = os.path.getsize(_DB) if os.path.exists(_DB) else 0
        return {"ok": True, "searches": s_cnt, "search_hits": s_hits,
                "links": l_cnt, "dead_links": l_dead,
                "size_kb": round(size / 1024, 1), "path": _DB}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def purge(days=7):
    """清掉过期记录"""
    c = _db()
    if not c:
        return 0
    cut = time.time() - days * 86400
    n = 0
    try:
        with _LOCK:
            r1 = c.execute("DELETE FROM search WHERE ts<?", (cut,)).rowcount
            r2 = c.execute("DELETE FROM link WHERE ts<?", (cut,)).rowcount
            c.commit()
            n = (r1 or 0) + (r2 or 0)
        try:
            c.execute("VACUUM")
        except Exception:
            pass
    except Exception as e:
        log.warning("purge 失败: %s", str(e)[:120])
    return n
