#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验收：载荷编解码 + 载荷解析（不联网）。"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import payload_codec
import qr_api

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        FAILED.append(label)


def main() -> int:
    print("[1] 编解码往返")
    payload = "0~\x101bne!3~1794355176427f6c357a1f0c0e6f2efcc5317ab7e0!4~\x105gz2"
    d = payload_codec.decode(payload)
    check(d["courseId"] == 61754, f"courseId=61754 实际 {d.get('courseId')}")
    check(d["rollcallId"] == 255278, f"rollcallId=255278 实际 {d.get('rollcallId')}")
    check(d["data"].endswith("427f6c357a1f0c0e6f2efcc5317ab7e0"), "data 完整")
    check(payload_codec.encode(d) == payload, "encode(decode(x)) == x")

    print("[2] 转义字符（! 与 ~）")
    obj = {"data": "a!b~c", "rollcallId": 1}
    back = payload_codec.encode(obj)
    check(back.startswith("3~"), "字段名按下标编码")
    check(payload_codec.decode(back)["data"] == "a!b~c", "特殊字符往返一致")

    print("[3] 载荷文本的几种形态")
    full = "/j?p=" + payload
    for name, text in [
        ("经典 /j?p=", full),
        ("/scanner-jumper?p=", "/scanner-jumper?p=" + payload),
        ("裸载荷", payload),
    ]:
        got = qr_api.parse_payload(text)
        check(got.get("rollcallId") == 255278, f"{name} → rollcallId 解出")

    print("[4] 非法输入不炸")
    for bad in ["", "不是码", "/j?p=", "0~"]:
        check(qr_api.parse_payload(bad) == {}, f"{bad!r} → {{}}")

    print()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
