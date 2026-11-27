#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线验收：HTTP 服务 + 多账号提交 + 上传解码的失败路径（不碰真站点，全用假上游）。

跑： python tests/test_server.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILED: list[str] = []
PUTS: list[dict] = []


def check(cond: bool, label: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        FAILED.append(label)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Mock(BaseHTTPRequestHandler):
    """假上游：profile / 名单 / 三种提交。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/profile"):
            return self._j(200, {"id": 1, "user_no": "20260001", "name": "测试账号"})
        if "student_rollcalls" in self.path:
            return self._j(200, {"student_rollcalls": [
                {"user_no": "20260001", "rollcall_status": "on_call_fine", "status": "on_call"}]})
        if "modules/rollcalls" in self.path:
            return self._j(200, {"rollcalls": []})
        self._j(404, {"message": "not found"})

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        PUTS.append(body)
        data = str(body.get("data", ""))
        if data.startswith("1794355176"):
            return self._j(200, {"id": None, "status": "on_call"})
        return self._j(400, {"error_code": "qr_code_expired", "message": "qr_code_expired"})


def http(url: str, data: bytes | None = None, ctype: str = "application/json"):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main() -> int:
    mock_port, srv_port = free_port(), free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    # 两个账号的假会话放临时目录
    tmp = tempfile.mkdtemp(prefix="signkit-test-")
    for i in ("a", "b"):
        json.dump([{"name": "session", "value": "V2-test", "domain": "127.0.0.1", "path": "/"}],
                  open(os.path.join(tmp, f"cookies_{i}.json"), "w"))

    env = dict(os.environ,
               SIGN_BASE=f"http://127.0.0.1:{mock_port}",
               SIGN_COOKIES_DIR=tmp,
               SIGN_TARGET="")
    srv = subprocess.Popen([sys.executable, "-u", "server.py", "--port", str(srv_port), "--plain"],
                           cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace")
    base = f"http://127.0.0.1:{srv_port}"
    try:
        for _ in range(40):
            try:
                if http(base + "/health")[1].get("ok"):
                    break
            except Exception:
                time.sleep(0.25)
        else:
            print("服务没起来")
            print(srv.stdout.read()[-2000:] if srv.stdout else "")
            return 1

        print("[1] 静态页面")
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            html = r.read().decode("utf-8")
        check(r.status == 200, "GET / 200")
        check("开始扫描" in html, "页面含「开始扫描」")
        check("{{TARGET}}" not in html, "目标占位符已替换")
        for path in ("/app.js", "/style.css", "/vendor/jsQR.js"):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                check(r.status == 200, f"GET {path} 200")

        print("[2] 一码多签（两个账号各发一次 PUT）")
        PUTS.clear()
        payload = "/j?p=0~\x101bne!3~1794355176427f6c357a1f0c0e6f2efcc5317ab7e0!4~\x105gz2"
        _, j = http(base + "/scan", json.dumps({"qr": payload}).encode())
        check(j.get("ok") is True, "ok=true")
        check(len(PUTS) == 2, f"发了 {len(PUTS)} 次 PUT（期望 2）")
        check(all(p.get("deviceId") for p in PUTS), "每次提交都带 deviceId")
        check(len(j.get("results") or []) == 2, "results 两个账号")
        check(all(r.get("raw", {}).get("http") == 200 for r in j["results"]),
              "每条都带原始返回 http=200")

        print("[3] 过期码 → retry=true")
        expired = "/j?p=0~\x101bne!3~1700000000000000000000000000000000!4~\x105gz2"
        _, j2 = http(base + "/scan", json.dumps({"qr": expired}).encode())
        check(j2.get("ok") is False, "ok=false")
        check(j2.get("retry") is True, "retry=true（前端会自动继续扫）")

        print("[4] 上传解码的失败路径")
        _, j3 = http(base + "/decode", b"\x00" * 2048, "image/jpeg")
        check(j3.get("ok") is False, "垃圾字节不会 500")
        check("没解出" in (j3.get("detail") or ""), f"给出可读原因: {j3.get('detail')}")

        print("[5] 坏 JSON")
        try:
            http(base + "/scan", b"{oops")
            check(False, "坏 JSON 应回 400")
        except urllib.error.HTTPError as e:
            check(e.code == 400, f"坏 JSON → {e.code}")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        mock.shutdown()

    print()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
