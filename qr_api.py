#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qr_api.py —— 上游签到接口客户端（手机端那套 HTTP 接口的最小复刻）

一句话：把「一张二维码载荷 / 一个数字码 / 一组坐标」变成上游的一次签到请求，
并把服务端的原始返回原样保留下来（`last_raw`），成功与否只由服务端裁决。

接口形状（实测，见 docs/FINDINGS.md）：
    PUT /api/rollcall/{rid}/answer_qr_rollcall      {"data": "...", "deviceId": "..."}
    PUT /api/rollcall/{rid}/answer_number_rollcall  {"numberCode": "1234", "deviceId": "..."}
    PUT /api/rollcall/{rid}/answer?api_version=1.1.2 {纬度, 经度, accuracy, deviceId, ...}
    GET /api/rollcall/{rid}/student_rollcalls        名单（用来回查"到底签上没有"）
    GET /api/course/{cid}/student/{user_no}/rollcalls 我在课内的进行中签到（高频轮询用）

鉴权两条路都认：Cookie `session=...` 或请求头 `x-session-id`（同一个值）。

环境变量：
    SIGN_BASE      上游站点根地址，默认 https://courses.example.edu.cn
    SIGN_TARGET    只给这个学号签（留空 = 不限）
    SIGN_COOKIES   指定会话文件，默认取目录下 cookies*.json 全部
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys
import urllib.parse
import uuid

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = (os.environ.get("SIGN_BASE") or "https://courses.example.edu.cn").rstrip("/")
TARGET = (os.environ.get("SIGN_TARGET") or "").strip()
DEVICE_FILE = os.path.join(HERE, "devices.json")

# 移动端 H5 容器的请求头：Origin/Referer 指向本地壳页，UA 带产品标识
XHR_UA = ("Mozilla/5.0 (Linux; Android {av}; {model} Build/{build}; wv) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
          "Chrome/151.0.7922.199 Mobile Safari/537.36")

# 提交响应里出现这些 status 就算签上了（名单里是 on_call_fine）
OK_STATUS = {"on_call", "on_call_fine", "answered", "on_call_late", "success"}

# 服务端 message / error_code → 人话
MSG = {
    "rollcall_closed": "签到已结束",
    "rollcall_already_closed": "点名已结束",
    "qr_code_expired": "二维码已过期",
    "QR_code_expired": "签到二维码已过期",
    "invalid_create_time_hash": "码的时间戳签名无效（过期或伪造）",
    "invalid_qr_create_time": "码的生成时间无效",
    "no_request_data": "服务端没收到 data",
    "device_used": "该设备已签到，请更换设备重新扫描",
    "deviceAlreadyInUse": "已有学生用该设备签到，请更换设备",
    "unknown_student": "你不在本课名单里",
    "failed": "签到失败",
    "wrongNumberCode": "签到密码错误",
    "rollcallFinished": "点名已结束",
    "retry": "签到失败，请重试",
    "getPositionFailed": "获取位置信息失败",
    "getPositionFailedTimeout": "获取位置信息超时",
    "outofScope": "定位异常，可能导致签到失败",
}


def human(msg: str | None) -> str:
    if not msg:
        return ""
    return MSG.get(msg, msg)


# ---------------------------------------------------------------- 设备指纹
def _load_devices() -> dict:
    try:
        return json.load(open(DEVICE_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_devices(d: dict) -> None:
    try:
        json.dump(d, open(DEVICE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except OSError:
        pass


def bind_device(alias: str, key: str) -> str:
    """同一个账号永久绑同一台"手机"：服务端按 deviceId 判定重复签到。"""
    d = _load_devices()
    if alias in d and d[alias].get("deviceId"):
        return alias
    d[alias] = device_profile(key)
    _save_devices(d)
    return alias


def device_profile(key: str) -> dict:
    """由账号键派生一组稳定的设备参数。

    `deviceId` 必须是合法 UUID 形态（服务端按它判"同一台手机不能签两个人"），
    用 uuid5 从账号键推导：同一个学号永远是同一个值，重启/换机器都不漂移。
    """
    d = _load_devices()
    rec = d.get(key)
    if rec and rec.get("deviceId"):
        return rec

    rnd = random.Random(f"signkit:{key}")
    profile = {
        "deviceId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"signkit:{key}")),
        "model": f"MI {rnd.randint(6, 13)}",
        "androidVersion": f"{rnd.randint(9, 14)}",
        "buildId": f"QKQ1.{rnd.randint(190000, 210000)}.001",
    }
    d[key] = profile
    _save_devices(d)
    return profile


def cookie_files() -> list[str]:
    """会话目录下所有 cookies*.json（一份 = 一个账号）。

    目录默认是代码所在目录，可用 SIGN_COOKIES_DIR 改（Docker/测试用）。
    """
    d = os.environ.get("SIGN_COOKIES_DIR") or HERE
    only = os.environ.get("SIGN_COOKIES")
    if only:
        p = only if os.path.isabs(only) else os.path.join(d, only)
        return [p] if os.path.exists(p) else []
    return sorted(glob.glob(os.path.join(d, "cookies*.json")))


# ---------------------------------------------------------------- 客户端
class TronClient:
    """一个 cookies*.json = 一个账号。"""

    def __init__(self, cookie_file: str, base: str | None = None, timeout: int = 20):
        self.cookie_file = cookie_file
        self.base = (base or BASE).rstrip("/")
        self.timeout = timeout
        self.tag = os.path.basename(cookie_file)
        self.s = requests.Session()
        # 内网/校园域名常被系统代理打断，这里强制直连
        self.s.trust_env = False
        self.s.proxies = {"http": None, "https": None}
        self._load_cookies()
        self._account_key = self.tag
        self._profile: dict | None = None
        # 最近一次提交的原始返回（HTTP 码 / 头 / body / 解析后的 json）
        self.last_raw: dict = {}

        prof = self.device_profile
        self.s.headers.update({
            "User-Agent": XHR_UA.format(av=prof["androidVersion"], model=prof["model"],
                                        build=prof["buildId"]),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "http://localhost",
            "Referer": "http://localhost/",
            "X-Requested-With": "XMLHttpRequest",
        })
        sid = self.session_id
        if sid:
            # 移动端接口按这个头认人；本部署它就是 session cookie 的值
            self.s.headers["x-session-id"] = sid

    # ------------------------------------------------------------ 内部
    def _load_cookies(self) -> None:
        for c in json.load(open(self.cookie_file, encoding="utf-8")):
            self.s.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                               path=c.get("path", "/"))

    def save_cookies(self) -> None:
        """服务端可能轮换 session，回写一份，别把磁盘上的会话放旧。"""
        try:
            data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                    for c in self.s.cookies]
            if data:
                json.dump(data, open(self.cookie_file, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
        except OSError:
            pass

    @property
    def session_id(self) -> str | None:
        for c in self.s.cookies:
            if c.name == "session":
                return c.value
        return None

    @property
    def device_profile(self) -> dict:
        return device_profile(self._account_key)

    @property
    def device_id(self) -> str:
        return self.device_profile["deviceId"]

    def req(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else self.base + path
        kw.setdefault("timeout", (10, self.timeout))
        kw.setdefault("headers", {})
        return self.s.request(method, url, **kw)

    def set_account_key(self, key: str) -> None:
        """拿到学号后再绑设备（同一学号跨 cookie 文件也认作同一台手机）。"""
        if not key or key == self._account_key:
            return
        self._account_key = bind_device(self._account_key, key)
        prof = self.device_profile
        self.s.headers["User-Agent"] = XHR_UA.format(
            av=prof["androidVersion"], model=prof["model"], build=prof["buildId"])

    # ------------------------------------------------------------ 读
    def profile(self) -> dict:
        r = self.req("GET", "/api/profile")
        if r.status_code != 200:
            return {}
        try:
            self._profile = r.json() or {}
        except ValueError:
            return {}
        self.set_account_key(str(self._profile.get("user_no") or self.tag))
        return self._profile

    def me(self) -> tuple[str, str]:
        p = self.profile()
        return str(p.get("user_no") or ""), str(p.get("name") or "?")

    def alive(self) -> bool:
        return bool(self.profile().get("id") is not None)

    def detail(self, rid) -> dict:
        r = self.req("GET", f"/api/rollcall/{rid}/student_rollcalls")
        if r.status_code != 200:
            return {}
        try:
            return r.json() or {}
        except ValueError:
            return {}

    def my_active(self, cid) -> list[dict]:
        """我在某课里进行中的签到（比拉全名单轻，适合高频轮询）。"""
        me, _ = self.me()
        if not me:
            return []
        r = self.req("GET", f"/api/course/{cid}/student/{me}/rollcalls")
        if r.status_code != 200:
            return []
        try:
            return (r.json() or {}).get("rollcalls") or []
        except ValueError:
            return []

    def get_qr_token(self, rid, cid) -> tuple[str | None, str]:
        """向服务端要当前二维码载荷（教师/助教权限才有；学生通常 403）。"""
        r = self.req("GET", f"/api/course/{cid}/rollcall/{rid}/qr_code")
        if r.status_code != 200:
            return None, f"取码接口 HTTP {r.status_code}"
        try:
            j = r.json() or {}
        except ValueError:
            return None, "取码接口响应不是 JSON"
        for k in ("data", "qr_code", "code"):
            v = j.get(k) if isinstance(j, dict) else None
            if isinstance(v, str) and v.strip():
                return v.strip(), ""
        return None, "取码接口没给载荷"

    def my_status(self, rid) -> str:
        for row in (self.detail(rid).get("student_rollcalls") or []):
            if str(row.get("user_no")) == str(self._profile_no()):
                return str(row.get("rollcall_status") or "")
        return ""

    def _profile_no(self) -> str:
        return str((self._profile or {}).get("user_no") or "")

    def signed_state(self, rid) -> str:
        """名单里两个字段都看：`rollcall_status` 和 `status`。

        返回 yes / no / unknown；unknown 时按"没签"处理，继续提交。
        """
        me = self._profile_no()
        rows = (self.detail(rid).get("student_rollcalls") or [])
        mine = [r for r in rows if str(r.get("user_no")) == me]
        if not mine:
            return "unknown"
        vals = {str(r.get("rollcall_status") or "").lower() for r in mine}
        vals |= {str(r.get("status") or "").lower() for r in mine}
        if not vals - {""}:
            return "unknown"
        if vals & {"on_call_fine", "on_call", "answered"}:
            return "yes"
        if vals <= {"absent", "no", ""}:
            return "no"
        return "unknown"

    def enrolled(self, rid) -> bool:
        me = self._profile_no()
        if not me:
            return False
        rows = (self.detail(rid).get("student_rollcalls") or [])
        return any(str(r.get("user_no")) == me for r in rows)

    # ------------------------------------------------------------ 写
    def sign_qr(self, rid, data: str) -> tuple[bool, str]:
        body = {"data": data, "deviceId": self.device_id}
        r = self.req("PUT", f"/api/rollcall/{rid}/answer_qr_rollcall", json=body)
        return self._judge(r, f"二维码签到 rid={rid}")

    def sign_number(self, rid, code: str) -> tuple[bool, str]:
        body = {"numberCode": str(code), "deviceId": self.device_id}
        r = self.req("PUT", f"/api/rollcall/{rid}/answer_number_rollcall", json=body)
        return self._judge(r, f"数字签到 rid={rid} code={code}")

    def sign_radar(self, rid, lat: float, lon: float,
                   accuracy: float | None = None) -> tuple[bool, str]:
        body = {
            "deviceId": self.device_id,
            "latitude": lat,
            "longitude": lon,
            "speed": None,
            "accuracy": accuracy if accuracy is not None else round(random.uniform(20, 40), 1),
            "altitude": 0,
            "altitudeAccuracy": None,
            "heading": None,
        }
        r = self.req("PUT", f"/api/rollcall/{rid}/answer?api_version=1.1.2", json=body)
        return self._judge(r, f"雷达签到 rid={rid} ({lat},{lon})")

    def _judge(self, r, label: str) -> tuple[bool, str]:
        try:
            j = r.json() or {}
        except ValueError:
            j = {}
        if isinstance(j, list):
            j = {}
        status = str(j.get("status") or "").lower()
        err = j.get("error_code") or j.get("message") or j.get("error") or ""
        msg = human(str(err))
        # 原始返回一律留存（成功也留）：status/message 之外的字段都在这里
        self.last_raw = {
            "label": label,
            "http": r.status_code,
            "status": status,
            "error_code": str(err),
            "headers": {k: v for k, v in r.headers.items()
                        if k.lower() in ("content-type", "content-length", "server",
                                         "date", "x-session-id")},
            "json": j,
            "body": (r.text or "")[:4000],
        }
        ok = r.status_code == 200 and (status in OK_STATUS or "on_call" in status)
        if ok:
            print(f"  [{self.tag}] {label} -> HTTP 200 status={status or '?'} 成功")
            return True, msg or "ok"
        if r.status_code == 401:
            print(f"  [{self.tag}] {label} -> HTTP 401 会话失效，重新登录")
            return False, "会话失效"
        print(f"  [{self.tag}] {label} -> HTTP {r.status_code} "
              f"status={status or '-'} code={err or '-'} {msg or r.text[:80]!r}")
        return False, msg or f"HTTP {r.status_code}"


# ---------------------------------------------------------------- 载荷 → 签到
def payload_of(text: str) -> str:
    """从扫码文本里取载荷，兼容前端 scanUrlAnalysis 的几种形态。"""
    t = (text or "").strip()
    if not t:
        return ""
    m = re.search(r"[?&](?:p|_p)=([^&\s]+)", t)
    if m:
        val = urllib.parse.unquote(m.group(1))
        if val.startswith("{"):
            try:
                obj = json.loads(val)
                if isinstance(obj, dict) and obj.get("p"):
                    return str(obj["p"])
            except ValueError:
                pass
        return val
    if t.startswith("/j?") or not t.startswith("/"):
        return t
    return t


def parse_payload(text: str) -> dict:
    """载荷文本 → {courseId, rollcallId, data, ...}，解不出返回 {}。"""
    import payload_codec

    body = payload_of(text)
    if not body:
        return {}
    for cand in (body, urllib.parse.unquote(body)):
        if "~" not in cand:
            continue
        try:
            d = payload_codec.decode(cand)
        except Exception:
            continue
        if d.get("rollcallId") is not None and d.get("data"):
            d["rollcallId"] = int(d["rollcallId"]) if str(d["rollcallId"]).isdigit() else d["rollcallId"]
            return d
    return {}


def submit_payload(text: str, dry: bool = False, files: list[str] | None = None) -> bool:
    """一张码 → 给所有在册账号各提交一次（各自独立 deviceId）。

    不做任何预判：拿到码就发，成不成看服务端返回；名单只用来回显。
    """
    d = parse_payload(text)
    rid, data = d.get("rollcallId"), d.get("data")
    if rid is None or not data:
        print(f"载荷解不出 rollcallId/data，原样: {text[:80]!r}")
        return False
    print(f"载荷: courseId={d.get('courseId')} rollcallId={rid} data={str(data)[:16]}…")

    targets = files or cookie_files()
    if not targets:
        print("没有 cookies*.json —— 先跑 python accounts.py login 学号")
        return False
    if dry:
        print(f"干跑：{len(targets)} 个账号，不提交")
        return True

    checked = 0
    signed: list[str] = []
    for cf in targets:
        try:
            c = TronClient(cf)
        except Exception as e:
            print(f"  [{os.path.basename(cf)}] 会话加载失败: {e}")
            continue
        me, name = c.me()
        if not me:
            print(f"  [{c.tag}] 会话无效（/api/profile 非 200），跳过")
            continue
        if TARGET and str(me) != TARGET:
            print(f"  [{c.tag}] {name}({me}) 不是指定账号 {TARGET}，跳过")
            continue
        checked += 1
        ok, msg = c.sign_qr(rid, data)
        raw = getattr(c, "last_raw", {}) or {}
        c.save_cookies()
        if raw.get("body"):
            print(f"  [{c.tag}] 原始返回: HTTP {raw.get('http')} {raw.get('body', '')[:400]}")
        if ok:
            st = c.my_status(rid)
            signed.append(f"{name}({me})")
            print(f"  [{c.tag}] {name}({me}) 提交成功；名单确认 rollcall_status={st or '-'}"
                  f"{'  ✅ 已到' if st == 'on_call_fine' else ''}")
            continue          # 继续给下一个账号签（一码多签）
        if "过期" in msg or "结束" in msg:
            print(f"  [{c.tag}] {msg} → 这张码在服务端已失效，换新图")
            if not signed:
                return False
    if signed:
        print(f"  本张码签上 {len(signed)} 人: {' | '.join(signed)}")
        return True
    if not checked:
        print("  没有任何账号在这场的名单里 → 这张码不属于手上的账号")
    return False


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="上游签到接口客户端")
    ap.add_argument("cmd", choices=["whoami", "qr", "number", "radar", "detail"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--cookies", default=None, help="指定 cookies 文件（默认全部）")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    files = [a.cookies] if a.cookies else None

    if a.cmd == "qr":
        if not a.args:
            ap.error("需要二维码载荷")
        return 0 if submit_payload(a.args[0], dry=a.dry, files=files) else 2

    targets = files or cookie_files()
    if not targets:
        print("没有 cookies*.json，先用 accounts.py login 登录")
        return 2
    rc = 0
    for cf in targets:
        c = TronClient(cf)
        if a.cmd == "whoami":
            me, name = c.me()
            print(f"{c.tag}: {name} ({me or '未登录'}) deviceId={c.device_id}")
            rc = rc or (0 if me else 1)
            continue
        if not c.me()[0]:
            print(f"{c.tag}: 会话无效，跳过")
            rc = 1
            continue
        if a.cmd == "detail":
            j = c.detail(int(a.args[0]))
            print(f"{c.tag}: status={j.get('status')} number_code={j.get('number_code')} "
                  f"名单 {len(j.get('student_rollcalls') or [])} 人")
        elif a.cmd == "number":
            ok, msg = c.sign_number(int(a.args[0]), a.args[1])
            print(f"{c.tag}: {'成功' if ok else '失败'} {msg}")
            rc = rc or (0 if ok else 1)
        elif a.cmd == "radar":
            ok, msg = c.sign_radar(int(a.args[0]), float(a.args[1]), float(a.args[2]))
            print(f"{c.tag}: {'成功' if ok else '失败'} {msg}")
            rc = rc or (0 if ok else 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
