#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accounts.py —— 账号/会话管理：登录一次，落一份 cookies_<学号>.json

上游站点用 CAS（Apereo）→ 身份提供方 broker → 业务系统的 session cookie。
登录成功后把整包 cookie 存成 `cookies_<学号>.json`，后面所有请求都用它。

密码可以**记在本机加密库里**（vault.py，Windows 用 DPAPI 按"当前用户+机器"加密），
这样会话过期后能自动重新登录，不用人守着。

环境变量：
    SIGN_BASE    上游站点根地址（默认 https://courses.example.edu.cn）
    SIGN_LOGIN   登录入口（默认 SIGN_BASE + /login）
    SIGN_VAULT   凭据库路径（默认 %LOCALAPPDATA%\\qr-sign-kit\\vault.dat）

用法：
    python accounts.py login 学号 密码            # 登录 + 存 cookie（不记密码）
    python accounts.py add 学号 密码 [称呼]       # 登录 + 存 cookie + 记密码（推荐）
    python accounts.py refresh [--dry] [--user X] # 逐个检查，失效的自动重登
    python accounts.py list                       # 凭据库 + 会话状态
    python accounts.py rm 学号                    # 从凭据库删掉（cookie 保留）
    python accounts.py whoami                     # 只查会话
    python accounts.py courses                    # 列课程
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = (os.environ.get("SIGN_BASE") or "https://courses.example.edu.cn").rstrip("/")
LOGIN = (os.environ.get("SIGN_LOGIN") or (BASE + "/login")).rstrip("/")
UA = ("Mozilla/5.0 (Linux; Android 10; Mi 8 Build/QKQ1.190828.002; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
      "Chrome/86.0.4240.99 Mobile Safari/537.36")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def new_session() -> requests.Session:
    s = requests.Session()
    # 校园域名常被系统代理打断，强制直连
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                      "Referer": BASE + "/"})
    return s


def save_cookies(s: requests.Session, path: str) -> int:
    data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in s.cookies]
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return len(data)


def whoami(s: requests.Session) -> dict:
    try:
        r = s.get(BASE + "/api/profile", timeout=20,
                  headers={"Accept": "application/json, text/plain, */*"})
    except requests.RequestException as e:
        log(f"/api/profile 请求异常: {e}")
        return {}
    if r.status_code != 200:
        return {}
    try:
        return r.json() or {}
    except ValueError:
        return {}


def login(user: str, password: str) -> dict:
    """CAS 表单登录 → 业务系统 session。返回 profile（空 = 失败）。"""
    s = new_session()
    log(f"CAS 登录: {user}")
    r = s.get(LOGIN, timeout=30, allow_redirects=True)
    if "/cas/" not in r.url and "login" not in r.url:
        log(f"意外落地页: {r.url}")

    action = re.search(r'<form[^>]*id="fm1"[^>]*action="([^"]+)"', r.text)
    if not action:
        log("没找到 CAS 登录表单（页面改版或需要验证码）")
        return {}

    post_url = requests.compat.urljoin(r.url, action.group(1).replace("&amp;", "&"))
    hidden = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"', r.text))
    data = {
        "username": user,
        "password": password,
        "authType": hidden.get("authType", "0"),
        "lt": hidden.get("lt", ""),
        "execution": hidden.get("execution", "e1s1"),
        "_eventId": hidden.get("_eventId", "submit"),
        "validTime": hidden.get("validTime", "5"),
    }
    r2 = s.post(post_url, data=data, timeout=30, allow_redirects=True,
                headers={"Referer": r.url,
                         "Content-Type": "application/x-www-form-urlencoded"})

    err = re.search(r'<div[^>]*id="status"[^>]*class="errorMsg"[^>]*>(.*?)</div>', r2.text, re.S)
    if err:
        log("登录被拒: " + re.sub(r"<[^>]+>", "", err.group(1)).strip())
        return {}
    if "authenticationFailure" in r2.text:
        log("登录被拒（authenticationFailure）")
        return {}
    if r2.status_code >= 400:
        log(f"登录 HTTP {r2.status_code}")
        return {}

    prof = whoami(s)
    if not prof:
        log("会话校验失败：拿不到 /api/profile")
        return {}

    out = os.path.join(HERE, f"cookies_{user}.json")
    n = save_cookies(s, out)
    log(f"登录成功: {prof.get('name')} ({prof.get('user_no')})；cookies 已存 "
        f"{os.path.basename(out)}（{n} 条）")
    return prof


def courses(s: requests.Session) -> list[dict]:
    r = s.get(BASE + "/api/user/recently-visited-courses", timeout=20)
    if r.status_code != 200:
        return []
    try:
        return (r.json() or {}).get("visited_courses") or []
    except ValueError:
        return []


def cookie_path(user_no: str) -> str:
    p = os.path.join(HERE, f"cookies_{user_no}.json")
    return p if os.path.exists(p) else os.path.join(HERE, "cookies.json")


def check(cookie_file: str) -> tuple[bool, str]:
    """直接用 curl 式请求验证一份会话；返回 (有效, 名字)。"""
    import qr_api
    try:
        c = qr_api.TronClient(cookie_file)
    except Exception:
        return False, ""
    me, name = c.me()
    return bool(me), name


# ------------------------------------------------------------------ 子命令
def cmd_add(user: str, password: str, name: str) -> int:
    import vault
    prof = login(user, password)
    if not prof:
        return 1
    vault.put(str(prof.get("user_no") or user), password, prof.get("name") or name)
    log(f"密码已存入本机凭据库（{vault.backend()}），会话过期可自动重登")
    return 0


def cmd_refresh(dry: bool = False, only: str | None = None) -> int:
    import vault
    rows = vault.list_accounts()
    if only:
        rows = [r for r in rows if r["user_no"] == only] or [{"user_no": only, "name": ""}]
    if not rows:
        print("凭据库是空的：先跑 python accounts.py add 学号 密码")
        return 1

    print(f"凭据库: {vault._path()}\n加密: {vault.backend()}\n")
    bad: list[str] = []
    for r in rows:
        user = r["user_no"]
        cf = cookie_path(user)
        print(f"[{user}] {r.get('name') or ''}")
        if not os.path.exists(cf):
            print("    没有 cookie 文件")
            ok = False
        else:
            ok, name = check(cf)
            print(f"    {'有效（' + name + '）' if ok else '失效'}")
        if ok:
            continue
        if dry:
            bad.append(user)
            continue
        pwd = vault.get(user)
        if not pwd:
            print("    凭据库里没有密码，跳过")
            bad.append(user)
            continue
        if login(user, pwd):
            print("    已自动重登")
        else:
            bad.append(user)

    print()
    print(f"问题账号 {len(bad)} 个" + (f"：{', '.join(bad)}" if bad else ""))
    return 0 if not bad else 1


def cmd_list() -> int:
    import vault
    print(f"凭据库: {vault._path()}\n加密: {vault.backend()}\n")
    rows = vault.list_accounts()
    if not rows:
        print("（空）")
        return 0
    for r in rows:
        cf = cookie_path(r["user_no"])
        ok, name = check(cf) if os.path.exists(cf) else (False, "")
        print(f"  {r['user_no']}\t{r.get('name') or name or '-'}\t"
              f"{'会话有效' if ok else '会话失效'}\t记于 {r.get('at') or '-'}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]

    if cmd == "login":
        if len(args) < 3:
            print("用法: python accounts.py login 学号 密码")
            return 2
        return 0 if login(args[1], args[2]) else 1

    if cmd == "add":
        if len(args) < 3:
            print("用法: python accounts.py add 学号 密码 [称呼]")
            return 2
        return cmd_add(args[1], args[2], args[3] if len(args) > 3 else "")

    if cmd == "refresh":
        dry = "--dry" in args
        only = args[args.index("--user") + 1] if "--user" in args and len(args) > args.index("--user") + 1 else None
        return cmd_refresh(dry=dry, only=only)

    if cmd == "list":
        return cmd_list()

    if cmd == "rm":
        if len(args) < 2:
            print("用法: python accounts.py rm 学号")
            return 2
        import vault
        return 0 if vault.delete(args[1]) else 1

    import qr_api
    files = qr_api.cookie_files()
    if not files:
        print("没有 cookies*.json")
        return 2

    if cmd == "whoami":
        rc = 0
        for f in files:
            c = qr_api.TronClient(f)
            me, name = c.me()
            print(f"{c.tag}: {name} ({me or '未登录'})")
            rc = rc or (0 if me else 1)
        return rc

    if cmd == "courses":
        for f in files:
            c = qr_api.TronClient(f)
            me, name = c.me()
            if not me:
                print(f"{c.tag}: 未登录，跳过")
                continue
            print(f"\n{name} ({me}) 的课程：")
            for cr in courses(c.s):
                print(f"  {cr.get('id')}\t{cr.get('name')}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
