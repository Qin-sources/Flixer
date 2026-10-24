"""
夸克账号管理
- Cookie 登录（手动粘贴）
- 状态检查
- 播放链路强制直连（不走代理）

注意：这里的 session 是【播放/账号】专用，trust_env=False 强制直连。
搜索链路用 dual._search_session，可以走代理。
"""
import json
import os
import re
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(ROOT, "data", "quark_cookie.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ★ 播放/账号链路：强制直连
_session = None


def _apply_to(s, cookie: str):
    """把 cookie 字符串塞进指定 session（不依赖全局 session，避免递归）"""
    s.cookies.clear()
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        try:
            s.cookies.set(k.strip(), v.strip(), domain=".quark.cn")
        except Exception:
            pass


def session():
    global _session
    if _session is None:
        s = requests.Session()
        s.trust_env = False                          # 忽略系统代理
        s.proxies = {"http": None, "https": None}    # 显式清空
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
        })
        _session = s
        # ★ 创建时就把已保存的 cookie 装上，
        #   否则任何忘记调 apply_cookie 的调用方都会以 guest 身份请求（401）
        ck = load_cookie()
        if ck:
            _apply_to(s, ck)
    return _session


# ---------------------------------------------------------------- 存取
def load_cookie() -> str:
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("cookie") or ""
    except Exception:
        return ""


def save_cookie(cookie: str):
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump({"cookie": cookie, "saved_at": int(time.time())}, f,
                  ensure_ascii=False)
    # 重置 session，让新 cookie 生效
    global _session
    _session = None


def clear_cookie():
    try:
        os.remove(COOKIE_FILE)
    except Exception:
        pass
    global _session
    _session = None


def apply_cookie(cookie: str):
    _apply_to(session(), cookie)


# ---------------------------------------------------------------- 验证
API = "https://drive-pc.quark.cn/1/clouddrive"


def _session_cookie_string() -> str:
    """把 session 里当前的 cookie 拼回 header 形式"""
    s = session()
    parts = []
    seen = set()
    for c in s.cookies:
        if c.name in seen:
            continue
        parts.append("%s=%s" % (c.name, c.value))
        seen.add(c.name)
    return "; ".join(parts)


def _sync_back(original: str):
    """
    服务端可能在响应里下了新的 Set-Cookie（续期），
    这里把它回写文件，避免一直用旧值直到过期。
    """
    try:
        now = _session_cookie_string()
    except Exception:
        return
    if not now or now == original:
        return
    # 只在关键字段都还在时才覆盖，避免把失效状态写进去
    if "__pus" not in now:
        return
    # 直接写文件，不触发 save_cookie 里的 session 重置
    try:
        import json as _json
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            _json.dump({"cookie": now, "saved_at": int(time.time()),
                        "refreshed": True}, f, ensure_ascii=False)
    except Exception:
        pass


def verify(cookie: str = None) -> dict:
    """
    用 cookie 调自己网盘根目录，判断是否真的登录。
    返回 {logged_in, nick, vip, member_type, error}
    """
    ck = cookie if cookie is not None else load_cookie()
    if not ck:
        return {"logged_in": False, "error": "没有 cookie"}

    apply_cookie(ck)
    s = session()

    try:
        r = s.get(API + "/config", params={
            "pr": "ucpro", "fr": "pc", "uc_param_str": "",
            "fetch_subscribe": "false",
        }, timeout=15)
        if r.status_code != 200:
            return {"logged_in": False, "error": "网络异常 (%s)" % r.status_code}
        d = r.json()
    except Exception as e:
        return {"logged_in": False, "error": "请求失败: %s" % str(e)[:80]}

    if d.get("status") != 200 or d.get("code") not in (0, None):
        msg = d.get("message") or "未登录"
        if "require login" in str(msg) or d.get("status") == 401:
            return {"logged_in": False, "error": "Cookie 已失效，请重新获取"}
        return {"logged_in": False, "error": msg}

    data = d.get("data") or {}
    member = data.get("member_type") or ""
    nick = data.get("nickname") or data.get("nick_name") or "夸克用户"
    vip = ""
    if member:
        VIP_MAP = {"SUPER_VIP": "超级会员", "VIP": "会员", "NORMAL": "普通用户"}
        vip = VIP_MAP.get(member, member)

    # 登录有效 -> 把可能被服务端续期的 cookie 回写，延长有效期
    _sync_back(ck)

    return {
        "logged_in": True,
        "nick": nick,
        "vip": vip,
        "member_type": member,
    }


def status() -> dict:
    ck = load_cookie()
    if not ck:
        return {"logged_in": False}
    v = verify(ck)
    if not v.get("logged_in"):
        # cookie 失效
        return {"logged_in": False, "error": v.get("error")}
    return v


def login_with_cookie(raw_cookie: str) -> dict:
    """粘贴 cookie 登录"""
    ck = (raw_cookie or "").strip()
    if not ck:
        return {"ok": False, "error": "cookie 为空"}
    # 简单清理：去掉前缀 "Cookie:"
    ck = re.sub(r"^\s*Cookie\s*:\s*", "", ck, flags=re.I).strip()
    if "=" not in ck:
        return {"ok": False, "error": "格式不对，需要 a=b; c=d 形式"}

    v = verify(ck)
    if not v.get("logged_in"):
        return {"ok": False, "error": v.get("error") or "验证失败，请检查 cookie"}

    save_cookie(ck)
    return {"ok": True, "nick": v.get("nick"), "vip": v.get("vip")}


# ---------------------------------------------------------------- 网盘操作
def list_my_root():
    """列自己网盘根目录（验证 cookie 是否真能用）"""
    s = session()
    try:
        r = s.get(API + "/file/sort", params={
            "pr": "ucpro", "fr": "pc", "pdir_fid": "0",
            "_page": "1", "_size": "20", "_fetch_total": "1",
            "_sort": "file_type:asc,updated_at:desc",
        }, timeout=15)
        d = r.json()
        if d.get("status") != 200:
            return {"ok": False, "error": d.get("message")}
        items = ((d.get("data") or {}).get("list")) or []
        return {"ok": True, "count": len(items),
                "sample": [i.get("file_name") for i in items[:5]]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


# ---------------------------------------------------------------- 续期
def touch() -> dict:
    """
    静默保活：用现有 cookie 访问一次网盘配置接口，
    服务端若下发新的 Set-Cookie 会被 _sync_back 回写。
    返回 {ok, error}
    """
    ck = load_cookie()
    if not ck:
        return {"ok": False, "error": "未登录"}
    v = verify(ck)
    if not v.get("logged_in"):
        return {"ok": False, "error": v.get("error") or "已失效"}
    return {"ok": True, "nick": v.get("nick"), "vip": v.get("vip")}


def cookie_age_days():
    """cookie 保存了多久（天）"""
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        ts = int(d.get("saved_at") or 0)
        if not ts:
            return None
        return round((time.time() - ts) / 86400.0, 1)
    except Exception:
        return None
