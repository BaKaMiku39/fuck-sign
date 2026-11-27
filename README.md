# qr-sign-kit

> 手机当扫码枪：把「老师屏幕上的二维码」在两三秒内变成「账号上一次成功的签到」。
> 面向 TronClass（畅课）系教学平台的签到接口，多账号、Web 端、可自部署。

## 这是什么

课堂教学平台（TronClass / 畅课系）的二维码点名，码只活十几秒，而且载荷带服务端签名——
本地伪造不了（见 [docs/FINDINGS.md](docs/FINDINGS.md)）。所以最短路径是：

```
手机（浏览器）读码  →  服务端（拿会话）在窗口内提交  →  回查名单确认真的签上
```

本项目就是这个链路的完整实现：一个手机端网页 + 一个 Python 服务。

## 能力

| 能力 | 说明 |
|---|---|
| **一码多签** | 一张码给所有在册账号各提交一次（各自独立 `deviceId`） |
| 手机端扫码页 | 摄像头连续扫描 / 拖图 / 粘贴 / 系统相机拍照 / 手写文本，五种入口 |
| **服务端解码** | 浏览器解不出来的原图直接上传，服务端 zxing-cpp **六路重试**（原图 / 灰度对比 / 放大 / 裁剪放大 / 反色 / 锐化） |
| 抗模糊采集 | 高分辨率 + `focusMode=continuous` + `ImageCapture.takePhoto()` 原尺寸照片 + 手电筒 |
| 原始返回留档 | 每次提交的 `http / status / error_code / headers / json / body` 全保留，成功也留 |
| 名单回查 | 提交成功后回查 `rollcall_status`，区分"服务端收下了"和"老师那边真的算到" |
| 数字码 / 雷达 | 同样封装（`sign_number` / `sign_radar`），数字码可直接从名单里读到 |
| 多账号管理 | 一个学号一份 `cookies_<学号>.json`，登录一次能用一两周 |

## 快速开始

### 1. 装依赖

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 登录（每个账号一次）

```bash
export SIGN_BASE=https://courses.example.edu.cn      # 改成你的站点
python accounts.py login 20260001 '你的密码'
# → 生成 cookies_20260001.json
python accounts.py whoami                            # 验证会话还有效
```

密码不落盘：只有 cookie 写文件，记得把 `cookies*.json` 加进 `.gitignore`（仓库已默认忽略）。

### 3. 起服务

```bash
# 本机/局域网：自签 https（手机摄像头要求 secure context）
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj '/CN=sign.local'
python server.py --port 8443
# 手机浏览器打开打印出来的 https://<内网IP>:8443/
```

```bash
# 挂到公网（nginx 反代，示例见 deploy/nginx.conf.example）
python server.py --port 8080 --plain
```

Docker 与 systemd 模板在 `deploy/`。

## 页面怎么用

1. **开始扫描** —— 摄像头连续扫，解出码立刻提交（多账号一起签）。
2. **拍不清？用系统相机拍一张** —— 调系统相机（对焦/HDR/防抖都强于网页视频），拍完自动上传解码。
3. **拖图 / 选图 / Ctrl+V** —— 电脑上把手机发来的截图丢进去也行。
4. **手写文本** —— 微信/系统相机识别出的 `/j?p=...` 链接直接贴进去提交。

页面会自动：每 4 秒抓一张原尺寸照片丢给服务端解码；码过期时提示"再扫一次"。

## HTTP 接口

两个端点，返回结构一致：

```
POST /scan     {"qr": "/j?p=..."}          → 提交这段文本载荷
POST /decode   原始图片字节（image/*）      → 服务端解码后再提交
GET  /health                                → {"ok":true}
```

返回：

```json
{
  "ok": true, "retry": false,
  "detail": "rid=123456 张三✅ | 李四✅",
  "decode": "zxing-cpp/灰度+对比",
  "results": [
    {"tag": "cookies_20260001.json", "name": "张三", "user_no": "20260001",
     "ok": true, "msg": "ok", "status": "on_call_fine",
     "raw": {"http": 200, "status": "on_call", "error_code": "",
             "json": {"id": null, "status": "on_call"},
             "body": "{\"id\":null,\"status\":\"on_call\"}"}}
  ]
}
```

`retry=true` 表示"码已过期"，前端据此自动继续扫。

## 配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `SIGN_BASE` | `https://courses.example.edu.cn` | 上游站点根地址 |
| `SIGN_TARGET` | 空 | 只给这个学号签 |
| `SIGN_COOKIES` | 空 | 只用一个会话文件 |
| `SIGN_COOKIES_DIR` | 代码目录 | 会话文件所在目录（Docker/测试用） |
| `SIGN_RAW_LOG` | `scan_raw.log` | 原始返回落盘路径 |

## 已知边界

- **签名伪造不了**：`data` 里的 32 位 hex 是服务端私钥签名，客户端无法离线生成，
  所以"完全自动"必须依赖"在有效期内拿到一张真码"，或者拥有教师/助教权限去调取码接口。
- **单码十几秒就换**：链路慢于这个窗口必然失败（`qr_code_expired`）。
- **提交 200 不等于签上**：必须回查名单 `rollcall_status=on_call_fine`。
- **糊到看不清的图救不回来**：解码器只能救轻微失焦，采集质量才是决定性因素。

细节与复现方式见 [docs/FINDINGS.md](docs/FINDINGS.md)。

## 目录结构

```
qr_api.py            上游接口客户端（提交 / 名单 / 取码 / 原始返回留档）
payload_codec.py     二维码载荷编解码
accounts.py          CAS 登录、会话落盘、会话检查
server.py            HTTP 服务（/ , /scan, /decode, /health）
web/                 手机端页面（index.html / app.js / style.css / vendor/jsQR.js）
deploy/              systemd unit、nginx 片段、Dockerfile、compose
tools/probe_forge.py 安全研究：验证服务端是否校验签名
tests/               离线验收（假上游，不碰真站点）
docs/FINDINGS.md     实测结论
```

## 测试

```bash
python tests/test_payload_codec.py
python tests/test_server.py      # 起假上游 + 真服务，验证多账号提交/过期重试/解码失败路径
```

## 授权：禁止商用

本项目采用 **QR-Sign-Kit 非商业使用许可 v1.0**（见 [LICENSE](LICENSE)），不是 MIT、不是 Apache。

| 允许（免费） | 禁止（需另行书面授权） |
|---|---|
| 个人学习、教学、学术研究、竞赛 | 销售 / 出租 / 收费许可本软件 |
| 修改、二次开发 | 集成进商业产品、商业服务、商业平台 |
| 分发（须保留本许可全文 + 标注原作者 + 说明改动） | 基于本项目提供有偿服务（SaaS、托管、代运营、技术支持、培训、咨询） |
| 非营利组织内部使用 | 营利性组织内部用于生产经营；广告引流、会员收费等任何变现 |

> 简言之：**自己用、学习、教学随便用；拿它赚钱不行。** 商业授权请单独联系作者。

## 合规

只用于**你自己的账号**，或已获得账号本人明确同意的场景。把自己学校的学生信息系统
暴露给第三方使用可能违反校规或用户协议，部署到公网前务必加访问控制
（Basic Auth / token / IP 白名单），否则等于把别人的签到权公开。

## 致谢

- [jsQR](https://github.com/cozmo/jsQR) —— 浏览器端二维码解码（Apache-2.0，见 `NOTICE`）
- [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) —— 服务端多路解码
