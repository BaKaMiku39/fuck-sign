#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vault.py —— 把账号/密码加密存在本机，用于"会话过期自动重新登录"

设计取舍：
  · 密码绝不能明文落盘。Windows 上用 DPAPI（CryptProtectData），密钥由操作系统
    按「当前用户 + 当前机器」派生 —— 文件被拷走、被别的用户读到都解不开。
  · 非 Windows 上 DPAPI 不存在，退化成 0600 权限的明文文件（并在调用处明确警告）。
  · 只用标准库（ctypes 调 crypt32），不引入第三方依赖。

存储位置：
    %LOCALAPPDATA%\\qr-sign-kit\\vault.dat      （Windows）
    ~/.config/qr-sign-kit/vault.dat            （其他）
可用环境变量 SIGN_VAULT 覆盖。

用法：
    import vault
    vault.put("20260001", "示例密码", name="张三")
    vault.get("20260001")          # → "示例密码" 或 None
    vault.list_accounts()          # → [{"user_no","name","at"}]
    vault.delete("20260001")
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

IS_WIN = os.name == "nt"
VERSION = 1


def _path() -> str:
    p = os.environ.get("SIGN_VAULT")
    if p:
        return p
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "qr-sign-kit", "vault.dat")
    return os.path.join(os.path.expanduser("~"), ".config", "qr-sign-kit", "vault.dat")


# ------------------------------------------------------------------ Windows DPAPI
def _dpapi(data: bytes, protect: bool = True) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    src, _keep = to_blob(data)
    dst = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = ([ctypes.byref(src)], None) if protect else ([ctypes.byref(src)], None)
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(src), "qr-sign-kit", None, None, None, 0, ctypes.byref(dst))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


def _seal(raw: bytes) -> bytes:
    return _dpapi(raw, protect=True) if IS_WIN else raw


def _open(raw: bytes) -> bytes:
    return _dpapi(raw, protect=False) if IS_WIN else raw


# ------------------------------------------------------------------ 读写
def _load() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {"version": VERSION, "entries": {}}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": VERSION, "entries": {}}
        return data
    except (OSError, ValueError):
        return {"version": VERSION, "entries": {}}


def _save(data: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def put(user_no: str, password: str, name: str = "") -> None:
    data = _load()
    blob = base64.b64encode(_seal(password.encode("utf-8"))).decode()
    old = data["entries"].get(str(user_no)) or {}
    data["entries"][str(user_no)] = {
        "name": name or old.get("name") or "",
        "secret": blob,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save(data)


def get(user_no: str) -> str | None:
    ent = _load()["entries"].get(str(user_no))
    if not ent:
        return None
    try:
        return _open(base64.b64decode(ent["secret"])).decode("utf-8")
    except Exception:
        return None


def delete(user_no: str) -> bool:
    data = _load()
    if str(user_no) not in data["entries"]:
        return False
    del data["entries"][str(user_no)]
    _save(data)
    return True


def list_accounts() -> list[dict]:
    return [{"user_no": k, **{kk: vv for kk, vv in v.items() if kk != "secret"}}
            for k, v in sorted(_load()["entries"].items())]


def backend() -> str:
    return "Windows DPAPI（按当前用户+机器加密）" if IS_WIN else \
        f"明文文件 0600（无 DPAPI）：{_path()}"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "put" and len(sys.argv) >= 4:
        name = sys.argv[4] if len(sys.argv) > 4 else ""
        put(sys.argv[2], sys.argv[3], name)
        print(f"已保存 {sys.argv[2]}（{name or '未命名'}）")
    elif cmd == "del" and len(sys.argv) >= 3:
        print("已删除" if delete(sys.argv[2]) else "没有这条")
    elif cmd == "test" and len(sys.argv) >= 3:
        v = get(sys.argv[2])
        print("解出:", "成功（长度 %d）" % len(v) if v else "失败/不存在")
    else:
        print(f"凭据库: {_path()}")
        print(f"加密方式: {backend()}")
        rows = list_accounts()
        if not rows:
            print("（空）")
        for r in rows:
            print(f"  {r['user_no']}\t{r.get('name') or '-'}\t{r.get('at') or '-'}")
