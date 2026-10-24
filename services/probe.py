"""
死链检测 + 优选
showpaw 的 isAlive 不可信（庆余年 221 条只标 4 条存活），
我们自己用夸克免登录接口进去看有没有文件，比它的标记准。
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import quark as Q

# showpaw 的 panType -> 我们能不能免登录验
VERIFIABLE = {"quark"}

# 夸克错误码 -> 人话
QUARK_ERRORS = {
    "好友已取消了分享": "分享已被取消",
    "分享已过期": "分享已过期",
    "分享不存在": "分享不存在",
    "提取码错误": "需要提取码",
    "该分享已被删除": "分享已删除",
    "来晚了，该分享文件已过期": "分享已过期",
    "分享文件已过期": "分享已过期",
}


def human_error(msg: str) -> str:
    m = (msg or "").strip()
    for k, v in QUARK_ERRORS.items():
        if k in m:
            return v
    if "passcode" in m.lower() or "提取码" in m:
        return "需要提取码"
    return m or "未知错误"


def probe_quark(url: str, passcode: str = "", timeout: int = 25) -> dict:
    """
    轻量探测：只取 stoken + 列根目录，不递归。
    返回 {alive, reason, title, has_files, root_count}
    """
    pwd_id, pwd = Q.parse_share(url)
    if not pwd_id:
        return {"alive": None, "reason": "不是夸克链接"}
    pwd = pwd or (passcode or "")
    tk = Q.get_stoken(pwd_id, pwd)
    if not tk.get("ok"):
        return {"alive": False, "reason": human_error(tk.get("message", ""))}
    lst = Q.list_dir(pwd_id, tk["stoken"], "0")
    if not lst.get("ok"):
        return {"alive": False, "reason": human_error(lst.get("message", "")),
                "title": tk.get("title")}
    items = lst.get("items") or []
    return {
        "alive": len(items) > 0,
        "reason": "" if items else "分享里没有文件",
        "title": tk.get("title") or "",
        "has_files": len(items) > 0,
        "root_count": len(items),
    }


def verify_resources(resources: list, max_check: int = 12, workers: int = 6) -> list:
    """
    对排好序的资源做并发存活验证，把结果写回每条。
    只验夸克（其他网盘没有免登录接口）。
    """
    targets = []
    for r in resources:
        if r.get("pan_type") in VERIFIABLE and len(targets) < max_check:
            targets.append(r)

    if not targets:
        return resources

    def work(r):
        return r, probe_quark(r.get("pan_url", ""), r.get("passcode", ""))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, r) for r in targets]
        for f in as_completed(futs, timeout=90):
            try:
                r, res = f.result()
            except Exception:
                continue
            r["verified"] = res

    # 评分修正：验证通过的提权，死的直接沉底
    for r in resources:
        v = r.get("verified")
        if not v:
            continue
        if v.get("alive") is True:
            r["score"] = r.get("score", 0) + 12
            r["live_state"] = "ok"
            if v.get("title"):
                r["quark_title"] = v["title"]
        elif v.get("alive") is False:
            r["score"] = r.get("score", 0) - 60
            r["live_state"] = "dead"
            r["dead_reason"] = v.get("reason") or ""
        else:
            r["live_state"] = "unknown"

    resources.sort(key=lambda x: x.get("score", 0), reverse=True)
    return resources
