#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
payload_codec.py —— 二维码载荷（`/j?p=...` 里那串东西）的编解码

前端用的是把 JSON "压扁"成一条紧凑串的写法：字段名下标记 + 值，用 `!` 连接，
特殊字符转义。字段名用下标（0,1,2... toString(36)）代替，省字节。

控制字符：
    \\x1e  记录分隔（转义后的 `!`）
    \\x1f  转义字面（转义后的 `~`）
    \\x1a  布尔标记（真的写 \\x1a1，假的写 \\x1a0）
    \\x10  数字前缀（值按 36 进制；小数写成 `整数部分.小数部分`）

    字段下标：0 courseId, 1 activityId, 2 activityType, 3 data,
              4 rollcallId, 5 groupSetId, 6 accessCode, 7 action,
              8 enableGroupRollcall, 9 createUser, 10 joinCourse

示例：
    /j?p=0~<FS>1bne!3~<timestamp+hash>!4~<FS>5gz2
    → {"courseId": 61754, "data": "1794355176427f6c...", "rollcallId": 255278}
"""
from __future__ import annotations

YO = chr(30)   # 记录分隔
ZO = chr(31)   # 转义字面
JO = chr(26)   # 布尔标记
QO = chr(16)   # 数字前缀

FIELDS = ["courseId", "activityId", "activityType", "data", "rollcallId",
          "groupSetId", "accessCode", "action", "enableGroupRollcall",
          "createUser", "joinCourse"]


def int_to36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def from36(s: str) -> int:
    return int(s, 36)


# 下标本身用 toString(36)：0..10 → "0".."a"
NA = {name: int_to36(i) for i, name in enumerate(FIELDS)}
IA = {v: k for k, v in NA.items()}


def decode(payload: str) -> dict:
    out: dict = {}
    for seg in payload.split("!"):
        if not seg:
            continue
        k, _, v = seg.partition("~")
        if not k:
            continue
        name = IA.get(k, k)
        if v.startswith(QO):
            body = v[1:]
            if "." in body:
                ip, fp = body.split(".", 1)
                out[name] = float(f"{from36(ip)}.{from36(fp)}")
            else:
                out[name] = from36(body)
        elif v.startswith(JO):
            out[name] = (v == JO + "1")
        else:
            out[name] = v.replace(ZO, "~").replace(YO, "!")
    return out


def encode(obj: dict) -> str:
    parts = []
    for k, v in obj.items():
        key = NA.get(k, k)
        if isinstance(v, bool):
            val = JO + ("1" if v else "0")
        elif isinstance(v, int):
            val = QO + int_to36(v)
        elif isinstance(v, float):
            ip, fp = str(v).split(".")
            val = QO + int_to36(int(ip)) + "." + int_to36(int(fp))
        elif isinstance(v, str):
            val = v.replace("~", ZO).replace("!", YO)
        else:
            val = "UNSUPPORTED"
        parts.append(f"{key}~{val}")
    return "!".join(parts)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    samples = [
        "/j?p=0~\x101bne!3~1794355176427f6c357a1f0c0e6f2efcc5317ab7e0!4~\x105gz2",
    ]
    for s in samples:
        body = s.split("p=", 1)[1]
        d = decode(body)
        print(f"载荷: {body!r}\n  解码: {d}\n  回编: {encode(d)}\n  一致: {encode(d) == body}")
