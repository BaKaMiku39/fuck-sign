#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 手机当扫码枪的 Web 服务（HTTP 服务端 + 静态页面）

为什么要"手机扫、服务器签"：二维码只活十几秒，且载荷带服务端签名，本地造不出来
（见 docs/FINDINGS.md）。所以最短路径是——手机负责把码读出来，服务器负责在窗口内提交。

端点：
    GET  /            扫码页（摄像头 / 拖图 / 粘贴 / 系统相机 / 手写文本）
    GET  /health      探活
    POST /scan        {"qr": "<载荷>"} → 多账号提交
    POST /decode      原始图片字节（image/*）→ 服务端解码（zxing-cpp 多路）→ 多账号提交

两者返回结构一致：
    {"ok": bool, "retry": bool, "detail": "...", "results": [...], "raw": [...]}
    每条 results[i] 带 {tag,name,user_no,ok,msg,status,raw{http,status,error_code,json,body}}

环境变量：
    SIGN_BASE       上游站点根地址
    SIGN_TARGET     只给这个学号签（页面标题会显示）
    SIGN_COOKIES    指定会话文件
    SIGN_RAW_LOG    原始返回落盘路径（默认 scan_raw.log）

用法：
    python server.py                  # 8443，自带 https（手机摄像头要求 secure context）
    python server.py --port 8080 --plain
    python server.py --dry            # 只解码不提交
"""
from __future__ import annotations

import argparse
import io
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WEB = os.path.join(HERE, "web")
CERT = os.path.join(HERE, "certs", "cert.pem")
KEY = os.path.join(HERE, "certs", "key.pem")
RAW_LOG = os.environ.get("SIGN_RAW_LOG") or os.path.join(HERE, "scan_raw.log")
MAX_UPLOAD = 12 * 1024 * 1024

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/vendor/jsQR.js": ("vendor/jsQR.js", "application/javascript; charset=utf-8"),
}


# ------------------------------------------------------------------ 解码
def decode_image_bytes(blob: bytes) -> tuple[str | None, str]:
    """原图 → 二维码文本。多路重试，返回 (载荷, 诊断串)。

    手机拍糊时，单次解码基本没戏；这里按"最容易救回来"的顺序试：
    原图 → 灰度+拉对比 → 小图放大 → 中心裁剪放大 → 反色 → 锐化。
    """
    try:
        from PIL import Image, ImageOps, ImageFilter
    except ImportError:
        return None, "服务端缺少 Pillow（pip install pillow）"
    try:
        import zxingcpp
    except ImportError:
        return None, "服务端缺少 zxing-cpp（pip install zxing-cpp）"

    def try_read(img) -> str | None:
        try:
            for b in zxingcpp.read_barcodes(img, text_mode=zxingcpp.TextMode.Plain):
                if getattr(b, "text", None):
                    return b.text
        except Exception:
            return None
        return None

    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
    except Exception as e:
        return None, f"不是有效图片: {e!r}"

    passes: list[tuple[str, object]] = []
    try:
        im = ImageOps.exif_transpose(im)
        passes.append(("原图", im.convert("RGB")))
        gray = ImageOps.autocontrast(im.convert("L"))
        passes.append(("灰度+对比", gray))
        long_side = max(im.size)
        if long_side < 1600:
            k = min(3.0, 2400 / max(1, long_side))
            big = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
            passes.append((f"放大{k:.1f}x", ImageOps.autocontrast(big.convert("L"))))
        w, h = im.size
        crop = im.crop((int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)))
        cw, ch = crop.size
        k2 = min(3.0, 2000 / max(1, max(cw, ch)))
        passes.append(("中心裁剪放大", ImageOps.autocontrast(
            crop.resize((int(cw * k2), int(ch * k2)), Image.LANCZOS).convert("L"))))
        passes.append(("反色", ImageOps.invert(ImageOps.autocontrast(im.convert("L")))))
        passes.append(("锐化", gray.filter(
            ImageFilter.UnsharpMask(radius=2, percent=180, threshold=3))))
    except Exception as e:
        return None, f"处理图片失败: {e!r}"

    for name, img in passes:
        txt = try_read(img)
        if txt:
            return txt, f"zxing-cpp/{name}"
    return None, "六路全没过（图太糊，或码只占画面极小一块）"


# ------------------------------------------------------------------ 签到
def sign_all(qr: str) -> tuple[bool, list[dict], str]:
    """一张码 → 交给所有在册账号各提交一次（各自独立 deviceId）。"""
    import qr_api as Q

    d = Q.parse_payload(qr)
    if not d:
        return False, [], "载荷解不出来"
    rid, data = d.get("rollcallId"), d.get("data")
    results: list[dict] = []
    for cf in Q.cookie_files():
        tag = os.path.basename(cf)
        try:
            c = Q.TronClient(cf)
        except Exception as e:
            results.append({"tag": tag, "name": "", "ok": False, "msg": f"会话加载失败 {e!r}"})
            continue
        me, name = c.me()
        if not me:
            results.append({"tag": tag, "name": "", "ok": False, "msg": "会话失效，需重新登录"})
            continue
        ok, msg = c.sign_qr(rid, data)
        raw = getattr(c, "last_raw", {}) or {}
        c.save_cookies()
        status = c.my_status(rid) if ok else ""
        if raw.get("body"):
            print(f"  [{tag}] 原始返回 HTTP {raw.get('http')} {raw.get('body', '')[:300]}",
                  flush=True)
        results.append({"tag": tag, "name": name, "user_no": me, "ok": ok,
                        "msg": msg or ("ok" if ok else ""), "status": status, "raw": raw})

    good = [r for r in results if r["ok"]]
    summary = " | ".join(
        f"{r.get('name') or r['tag']}{'✅' if r['ok'] else '❌'}"
        f"{'' if r['ok'] else ' ' + (r.get('msg') or '')}"
        for r in results) or "没有可用账号"
    return bool(good), results, f"rid={rid} {summary}"


def log_raw(qr: str, results: list[dict]) -> None:
    try:
        with open(RAW_LOG, "a", encoding="utf-8") as f:
            for r in results:
                raw = r.get("raw") or {}
                if raw:
                    f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "qr": qr[:300], **raw}, ensure_ascii=False) + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    dry = False

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------ 输出
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b'{"ok":true}', "application/json")
        item = STATIC.get(path)
        if not item:
            return self._send(404, b"not found", "text/plain")
        fname, ctype = item
        try:
            body = open(os.path.join(WEB, fname), "rb").read()
        except OSError:
            return self._send(404, f"{fname} missing".encode(), "text/plain")
        if fname == "index.html":
            target = os.environ.get("SIGN_TARGET") or "未指定（按名单选人）"
            body = body.replace("{{TARGET}}".encode(), target.encode())
        self._send(200, body, ctype)

    # ------------------------------------------------------------ POST
    def _sign(self, qr: str) -> tuple[bool, list[dict], str]:
        if self.dry:
            import qr_api as Q
            d = Q.parse_payload(qr)
            return bool(d), [], f"干跑: {d}"
        return sign_all(qr)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            return self._json({"ok": False, "detail": "图片太大（>12MB）",
                               "results": [], "raw": []}, 413)

        # ---------------- 原图上传（手机照片走这条）
        if path == "/decode":
            blob = self.rfile.read(n) if n else b""
            if not blob:
                return self._json({"ok": False, "detail": "空请求体", "results": [], "raw": []}, 400)
            payload, diag = decode_image_bytes(blob)
            print(f"[{time.strftime('%H:%M:%S')}] /decode {len(blob) // 1024}KB → {diag} "
                  f"{str(payload)[:80] if payload else '(没解出)'}", flush=True)
            if not payload:
                return self._json({"ok": False, "retry": False,
                                   "detail": f"图片里没解出二维码（{diag}）",
                                   "results": [], "raw": [], "decode": diag})
            try:
                ok, results, detail = self._sign(payload)
            except Exception as e:
                return self._json({"ok": False, "detail": f"提交异常: {e!r}", "results": [],
                                   "raw": [], "payload": payload, "decode": diag})
            log_raw(payload, results)
            return self._json({"ok": ok, "retry": False, "detail": detail, "results": results,
                               "raw": [r.get("raw") or {} for r in results],
                               "payload": payload, "decode": diag})

        if path != "/scan":
            return self._json({"ok": False, "detail": "not found"}, 404)

        # ---------------- 文本载荷
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"ok": False, "detail": "bad json",
                               "results": [], "raw": []}, 400)
        qr = str(body.get("qr") or "").strip()
        print(f"[{time.strftime('%H:%M:%S')}] 收到载荷: {qr[:100]!r}", flush=True)

        ok, results, detail = False, [], ""
        try:
            ok, results, detail = self._sign(qr)
        except Exception as e:
            detail = f"异常: {e!r}"
        retry = (not ok) and any(k in detail for k in
                                 ("过期", "invalid_create_time_hash", "QR_code_expired",
                                  "invalid_qr_create_time"))
        if retry:
            print("  码过期 → 让手机再扫一次（单码十几秒就换）", flush=True)
        print(f"  结果: {'成功' if ok else '未成功'} {detail}", flush=True)
        log_raw(qr, results)
        self._json({"ok": ok, "retry": retry, "detail": detail, "results": results,
                    "raw": [r.get("raw") or {} for r in results]})


def lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--dry", action="store_true", help="只解码不提交")
    ap.add_argument("--plain", action="store_true",
                    help="不用 https（手机浏览器会禁用摄像头）")
    a = ap.parse_args()

    Handler.dry = a.dry
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    scheme = "http"
    if not a.plain:
        if not (os.path.exists(CERT) and os.path.exists(KEY)):
            print(f"缺少证书 {CERT} / {KEY}，退回 http（手机摄像头会被浏览器拦）")
            print("一句话自签：openssl req -x509 -newkey rsa:2048 -nodes -days 3650 "
                  "-keyout certs/key.pem -out certs/cert.pem -subj '/CN=sign.local'")
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERT, KEY)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            scheme = "https"

    ip = lan_ip()
    print("=" * 62)
    print(f"手机浏览器打开： {scheme}://{ip}:{a.port}/")
    print("自签证书第一次点「高级 → 继续访问」")
    print(f"目标账号 SIGN_TARGET={os.environ.get('SIGN_TARGET') or '未指定（按名单选人）'}"
          f"  {'【干跑】' if a.dry else ''}")
    print("=" * 62, flush=True)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
