# qr-sign-kit

> 一个把「服务端下发的二维码」在两三秒内变成「账号上一次成功提交记录」的小工具。
> 手机负责读码，服务端负责在有效期内提交，Web 端多账号一键完成。

## 这是什么

一些考勤/签到场次会下发短时效二维码（十几秒一换），载荷里带服务端签名——
客户端算不出来。所以最短路径是：

```
手机（浏览器）读码  →  服务端（持会话）在窗口内提交  →  回查记录确认真的生效
```

本项目就是这个链路的完整实现：一个手机端网页 + 一个 Python 服务。

## 能力

| 能力 | 说明 |
|---|---|
| **一码多账号** | 一张码给所有在册账号各提交一次（各自独立 `deviceId`） |
| 手机端页面 | 摄像头连续扫描 / 拖图 / 粘贴 / 系统相机拍照 / 手写文本，五种入口 |
| **服务端解码** | 浏览器解不出来的原图直接上传，服务端 zxing-cpp **六路重试**（原图 / 灰度对比 / 放大 / 裁剪放大 / 反色 / 锐化） |
| 抗模糊采集 | 高分辨率 + `focusMode=continuous` + `ImageCapture.takePhoto()` 原尺寸照片 + 手电筒 |
| 原始返回留档 | 每次提交的 `http / status / error_code / headers / json / body` 全保留，成功也留 |
| 记录回查 | 提交成功后回查状态字段，区分"服务端收下了"和"记录里真的算数" |
| 数字码 / 定位 | 同样封装（`sign_number` / `sign_radar`） |
| 多账号管理 | 一个账号一份 `cookies_<id>.json`，登录一次能用一两周 |
| 记住密码 | 本机加密库（Windows DPAPI），会话过期自动重登 |

## 快速开始

### 1. 装依赖

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 登录（每个账号一次）

```bash
export SIGN_BASE=https://api.example.com             # 换成你的上游站点
python accounts.py login 20260001 '你的密码'          # 只登录，不记密码
# 或者：登录 + 把密码记进本机加密库（会话过期能自动重登）
python accounts.py add 20260001 '你的密码'
# → 生成 cookies_20260001.json
python accounts.py whoami                            # 验证会话还有效
```

### 3. 记住密码、自动续期（可选）

会话一般一两周就过期。把密码交给本机加密库后，过期能自己救回来：

```bash
python accounts.py list        # 谁还有效、谁失效
python accounts.py refresh     # 失效的自动重新登录（--dry 只看不登）
python accounts.py rm 20260001 # 从库里删掉
```

密码存在哪：

| 平台 | 方式 |
|---|---|
| Windows | **DPAPI**（`CryptProtectData`），密钥由「当前用户 + 当前机器」派生，文件拷走或换用户都解不开 |
| 其他 | `~/.config/qr-sign-kit/vault.dat`，文件权限 0600，启动时会明确打印"明文"警告 |

- 文件位置可用 `SIGN_VAULT` 覆盖；`vault.dat` 已在 `.gitignore` 里。
- 库里只有 `账号 → 加密密码`，**没有**明文密码、也没有 cookie。
- 想更保守：不建凭据库，沿用 `login` + 定期手动重登即可，其余功能不受影响。

### 4. 起服务

```bash
# 本机/局域网：自签 https（移动端浏览器要求 secure context）
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj '/CN=local.test'
python server.py --port 8443
# 手机浏览器打开打印出来的 https://<内网IP>:8443/
```

```bash
# 挂到公网（nginx 反代，示例见 deploy/nginx.conf.example）
python server.py --port 8080 --plain
```

Docker 与 systemd 模板在 `deploy/`。

## 页面怎么用

1. **开始扫描** —— 摄像头连续扫，解出码立刻提交（多账号一起）。
2. **拍不清？用系统相机拍一张** —— 调系统相机（对焦/HDR/防抖都强于网页视频），拍完自动上传解码。
3. **拖图 / 选图 / Ctrl+V** —— 电脑上把手机发来的截图丢进去也行。
4. **手写文本** —— 扫码识别出的 `/j?p=...` 链接直接贴进去提交。

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
  "detail": "rid=123456 A✅ | B✅",
  "decode": "zxing-cpp/灰度+对比",
  "results": [
    {"tag": "cookies_20260001.json", "name": "A", "user_no": "20260001",
     "ok": true, "msg": "ok", "status": "on_call_fine",
     "raw": {"http": 200, "status": "on_call", "error_code": "",
             "json": {"id": null, "status": "on_call"},
             "body": "{\"id\":null,\"status\":\"on_call\"}"}}
  ]
}
```

`retry=true` 表示"码已过期"，页面据此自动继续扫。

## 配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `SIGN_BASE` | `https://api.example.com` | 上游站点根地址 |
| `SIGN_TARGET` | 空 | 只给这个账号提交 |
| `SIGN_COOKIES` | 空 | 只用一个会话文件 |
| `SIGN_COOKIES_DIR` | 代码目录 | 会话文件所在目录（Docker/测试用） |
| `SIGN_VAULT` | 见上表 | 凭据库路径 |
| `SIGN_RAW_LOG` | `scan_raw.log` | 原始返回落盘路径 |

## 已知边界

- **签名不可伪造**：载荷里的 32 位 hex 是服务端私钥签名，客户端无法离线生成，
  所以"完全自动"必须依赖"在有效期内拿到一张真码"，或者持有高权限会话去调取码接口。
- **单码十几秒就换**：链路慢于这个窗口必然失败。
- **提交 200 不等于成功**：必须回查记录里的状态字段。
- **糊到看不清的图救不回来**：解码器只能救轻微失焦，采集质量才是决定性因素。

细节与复现方式见 [docs/FINDINGS.md](docs/FINDINGS.md)。

## 目录结构

```
qr_api.py            上游接口客户端（提交 / 记录查询 / 取码 / 原始返回留档）
payload_codec.py     二维码载荷编解码
accounts.py          CAS 登录、会话落盘、会话检查、凭据续期
vault.py             本机加密凭据库（Windows DPAPI）
server.py            HTTP 服务（/ , /scan, /decode, /health）
web/                 手机端页面（index.html / app.js / style.css / vendor/jsQR.js）
deploy/              systemd unit、nginx 片段、Dockerfile、compose
tools/probe_forge.py 安全研究：验证服务端是否校验签名
tools/gh_sync.py     push 不通时用 GitHub API 同步
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

> 简言之：**自己用、学习随便用；拿它赚钱不行。**

## 合规

只用于**你自己的账号**，或已获得账号本人明确同意的场景。把内部信息系统
暴露给第三方使用可能违反部署方的管理规定，上线前务必加访问控制
（Basic Auth / token / IP 白名单），否则等于把别人的提交权公开。

## 致谢

- [jsQR](https://github.com/cozmo/jsQR) —— 浏览器端二维码解码（Apache-2.0，见 `NOTICE`）
- [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) —— 服务端多路解码
