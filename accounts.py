#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accounts.py —— 账号/会话管理：一次登录，落一份 cookies_<学号>.json

上游站点用 CAS（Apereo）→ 身份提供方 broker → 业务系统的 session cookie。
登录成功后把整包 cookie 存成 `cookies_<学号>.json`，后面所有请求都用它。

环境变量：
    SIGN_BASE    上游站点根地址（默认 https://courses.example.edu.cn）
    SIGN_LOGIN   登录入口（默认 SIGN_BASE + /login）

用法：
    python accounts.py login 学号 密码      # 登录并写 cookies_<学号>.json
    python accounts.py whoami               # 逐个检查现有会话是否还有效
    python accounts.py courses              # 列出该账号的课程
"""
from __future__ import annotations

import json
import os
import re
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


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]

    if cmd == "login":
        if len(sys.argv) < 4:
            print("用法: python accounts.py login 学号 密码")
            return 2
        return 0 if login(sys.argv[2], sys.argv[3]) else 1

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
